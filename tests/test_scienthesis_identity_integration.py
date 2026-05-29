"""Tests for the LitPulse ↔ Identity integration paths.

Covers:
* ``identity_client.decode_identity_access_token`` — validates Identity-issued
  RS256 tokens against a mocked JWKS endpoint.
* ``auth_utils.decode_token`` — accepts Identity tokens for "access" requests
  and translates them to the legacy ``{user_id, type, email, _identity}``
  payload shape.
* ``auth_utils._resolve_identity_user_to_mongo`` — lazy linking of an
  Identity-authenticated user to an existing Mongo ``user_id`` by email +
  ``identity_id`` stamping.

These run as pure unit tests — no Mongo, no Identity instance, just an
in-memory RSA keypair, monkey-patched httpx requests, and a fake DB.
"""

from __future__ import annotations

import base64
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# Make the litpulse-backend root importable so `import identity_client` /
# `import auth_utils` work just like server.py uses them.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))


@pytest.fixture(scope="module")
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub, key


@pytest.fixture(autouse=True)
def configure_env(rsa_keypair, monkeypatch):
    _, _, key = rsa_keypair
    monkeypatch.setenv("IDENTITY_BASE_URL", "http://identity.test")
    monkeypatch.setenv("IDENTITY_JWKS_URL", "http://identity.test/.well-known/jwks.json")
    monkeypatch.setenv("IDENTITY_JWT_ISSUER", "scienthesis-identity")
    monkeypatch.setenv("IDENTITY_JWT_AUDIENCE", "litpulse")
    monkeypatch.setenv("SERVICE_TOKEN_SECRET", "test-secret-for-service-token")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-legacy-jwt-secret-32chars-or-more-aaaaaaaa")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("LITPULSE_USE_IDENTITY", "true")

    # Reset cached singletons between tests.
    import identity_client
    identity_client.reset_jwks_cache_for_tests()
    identity_client.reset_singleton_for_tests()


@pytest.fixture
def mock_jwks(rsa_keypair, monkeypatch):
    """Stub the JWKS cache directly so the fetch path is bypassed entirely."""
    import identity_client

    _, pub_pem, _ = rsa_keypair
    pub = serialization.load_pem_public_key(pub_pem.encode())
    numbers = pub.public_numbers()  # type: ignore[attr-defined]

    def _b64(value: int) -> str:
        n = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(n, "big")).rstrip(b"=").decode()

    key_record = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-key",
        "n": _b64(numbers.n),
        "e": _b64(numbers.e),
    }

    keys_by_kid = {"test-key": key_record}
    monkeypatch.setattr(
        identity_client._jwks_cache,
        "fetch_sync",
        lambda: dict(keys_by_kid),
    )


def _mint(rsa_keypair, *, sub=None, email="alice@example.com", kid="test-key", aud="litpulse", exp_delta=timedelta(hours=1)):
    from jose import jwt

    priv, _, _ = rsa_keypair
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub or str(uuid4()),
        "email": email,
        "type": "access",
        "iss": "scienthesis-identity",
        "aud": aud,
        "iat": now,
        "exp": now + exp_delta,
    }
    return jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid})


# ── identity_client.decode_identity_access_token ────────────────────


def test_decode_identity_returns_payload_for_valid_token(rsa_keypair, mock_jwks):
    import identity_client

    sub = str(uuid4())
    token = _mint(rsa_keypair, sub=sub, email="alice@example.com")
    payload = identity_client.decode_identity_access_token(token)
    assert payload is not None
    assert payload["sub"] == sub
    assert payload["email"] == "alice@example.com"
    assert payload["type"] == "access"


def test_decode_identity_accepts_real_multi_audience_token(rsa_keypair, mock_jwks):
    """The real Identity token carries aud=[litpulse,litportal,lithub]; it must validate here."""
    import identity_client

    sub = str(uuid4())
    token = _mint(rsa_keypair, sub=sub, aud=["litpulse", "litportal", "lithub"])
    payload = identity_client.decode_identity_access_token(token)
    assert payload is not None
    assert payload["sub"] == sub


def test_decode_identity_returns_none_for_hs256_token(rsa_keypair, mock_jwks):
    """A legacy LitPulse HS256 token must not be accidentally validated as Identity."""
    import identity_client
    from jose import jwt

    legacy = jwt.encode(
        {"user_id": str(uuid4()), "type": "access", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )
    assert identity_client.decode_identity_access_token(legacy) is None


def test_decode_identity_returns_none_for_unknown_kid(rsa_keypair, mock_jwks):
    import identity_client

    token = _mint(rsa_keypair, kid="unknown-key-id")
    assert identity_client.decode_identity_access_token(token) is None


def test_decode_identity_raises_on_expired_token(rsa_keypair, mock_jwks):
    import identity_client
    from jose import JWTError

    token = _mint(rsa_keypair, exp_delta=timedelta(seconds=-1))
    with pytest.raises(JWTError):
        identity_client.decode_identity_access_token(token)


def test_decode_identity_rejects_wrong_audience(rsa_keypair, mock_jwks):
    import identity_client
    from jose import JWTError

    token = _mint(rsa_keypair, aud="some-other-service")
    with pytest.raises(JWTError):
        identity_client.decode_identity_access_token(token)


# ── auth_utils.decode_token (dual validator) ────────────────────────


def test_decode_token_accepts_identity_access_token(rsa_keypair, mock_jwks):
    import auth_utils

    sub = str(uuid4())
    token = _mint(rsa_keypair, sub=sub, email="alice@example.com")
    payload = auth_utils.decode_token(token, "access")
    assert payload["user_id"] == sub
    assert payload["email"] == "alice@example.com"
    assert payload["_identity"] is True


def test_decode_token_falls_back_to_legacy_hs256(rsa_keypair, mock_jwks):
    import auth_utils
    from jose import jwt

    user_id = str(uuid4())
    legacy = jwt.encode(
        {
            "user_id": user_id,
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )
    payload = auth_utils.decode_token(legacy, "access")
    assert payload["user_id"] == user_id
    assert payload.get("_identity") is not True


def test_decode_token_rejects_garbage(rsa_keypair, mock_jwks):
    import auth_utils
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        auth_utils.decode_token("not-a-jwt", "access")


def test_decode_token_only_uses_identity_for_access(rsa_keypair, mock_jwks):
    """A non-access expected_type forces the legacy HS256 path (Identity does not issue those)."""
    import auth_utils
    from jose import jwt

    legacy_verification = jwt.encode(
        {
            "user_id": str(uuid4()),
            "type": "verification",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )
    payload = auth_utils.decode_token(legacy_verification, "verification")
    assert payload["type"] == "verification"


# ── _resolve_identity_user_to_mongo lazy upsert ─────────────────────


class _FakeUsersCollection:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def find_one(self, query: dict, _projection: dict | None = None):
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                return dict(row)
        return None

    async def update_one(self, query: dict, update: dict):
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                row.update(update["$set"])
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()


class _FakeDB:
    def __init__(self) -> None:
        self.users = _FakeUsersCollection()


@pytest.fixture
def fake_db():
    return _FakeDB()


async def test_resolve_identity_user_returns_existing_mongo_id_via_identity_id(fake_db):
    import auth_utils

    identity_id = str(uuid4())
    legacy_user_id = str(uuid4())
    fake_db.users.rows.append(
        {"user_id": legacy_user_id, "email": "alice@example.com", "identity_id": identity_id},
    )
    auth_utils.set_db_for_auth(fake_db)

    resolved = await auth_utils._resolve_identity_user_to_mongo(
        {"user_id": identity_id, "email": "alice@example.com"},
    )
    assert resolved == legacy_user_id


async def test_resolve_identity_user_links_by_email_first_contact(fake_db):
    import auth_utils

    identity_id = str(uuid4())
    legacy_user_id = str(uuid4())
    fake_db.users.rows.append(
        {"user_id": legacy_user_id, "email": "alice@example.com"},  # no identity_id yet
    )
    auth_utils.set_db_for_auth(fake_db)

    resolved = await auth_utils._resolve_identity_user_to_mongo(
        {"user_id": identity_id, "email": "alice@example.com"},
    )
    assert resolved == legacy_user_id
    # ``identity_id`` stamped onto the row for the next request.
    assert fake_db.users.rows[0]["identity_id"] == identity_id


async def test_resolve_identity_user_no_match_returns_identity_uuid(fake_db):
    import auth_utils

    identity_id = str(uuid4())
    auth_utils.set_db_for_auth(fake_db)

    resolved = await auth_utils._resolve_identity_user_to_mongo(
        {"user_id": identity_id, "email": "ghost@example.com"},
    )
    # When no Mongo row exists yet, callers see the Identity UUID so they can
    # provision the row on demand from the endpoint.
    assert resolved == identity_id


async def test_resolve_identity_user_email_normalized(fake_db):
    import auth_utils

    identity_id = str(uuid4())
    legacy_user_id = str(uuid4())
    fake_db.users.rows.append(
        {"user_id": legacy_user_id, "email": "alice@example.com"},
    )
    auth_utils.set_db_for_auth(fake_db)

    resolved = await auth_utils._resolve_identity_user_to_mongo(
        {"user_id": identity_id, "email": "Alice@Example.COM"},
    )
    assert resolved == legacy_user_id
