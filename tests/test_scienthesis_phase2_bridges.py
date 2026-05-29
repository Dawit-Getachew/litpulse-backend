"""Tests for LitPulse Phase-2 Identity + LitHub delegation bridges.

Covers identity_bridge (signup/login/shadow/legacy-migration) and lithub_bridge
(mirror-save / read-from-LitHub / one-shot backfill) against a fake Mongo and
respx-mocked Identity + LitHub HTTP services.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))


# ── Fake Mongo ──────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, n):
        return [dict(d) for d in self._docs[:n]]


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    @staticmethod
    def _match(doc, query):
        for k, v in query.items():
            if isinstance(v, dict):
                # operator queries ($gte etc.) — ignored in these unit tests
                continue
            if doc.get(k) != v:
                return False
        return True

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if self._match(d, query):
                return dict(d)
        return None

    def find(self, query, projection=None):
        return _FakeCursor([d for d in self.docs if self._match(d, query)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": "fake"})()

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._match(d, query):
                d.update(update.get("$set", {}))
                return type("R", (), {"modified_count": 1})()
        if upsert:
            nd: dict = {}
            nd.update({k: v for k, v in query.items() if not isinstance(v, dict)})
            nd.update(update.get("$set", {}))
            nd.update(update.get("$setOnInsert", {}))
            self.docs.append(nd)
        return type("R", (), {"modified_count": 0})()


class _FakeDB:
    def __init__(self):
        self.users = _FakeCollection()
        self.library = _FakeCollection()


@pytest.fixture
def db():
    return _FakeDB()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("IDENTITY_BASE_URL", "http://identity.test")
    monkeypatch.setenv("IDENTITY_JWT_ISSUER", "scienthesis-identity")
    monkeypatch.setenv("IDENTITY_JWT_AUDIENCE", "litpulse")
    monkeypatch.setenv("LITHUB_BASE_URL", "http://lithub.test")
    monkeypatch.setenv("SERVICE_TOKEN_SECRET", "shared-secret-for-tests")
    monkeypatch.setenv("JWT_SECRET_KEY", "legacy-litpulse-secret-32-chars-minimum-aa")
    monkeypatch.setenv("LITPULSE_USE_IDENTITY", "true")
    monkeypatch.setenv("LITPULSE_USE_LITHUB", "true")
    monkeypatch.setenv("LITPULSE_DUAL_WRITE_LITHUB", "true")
    import identity_client
    import lithub_client
    import service_token
    identity_client.reset_singleton_for_tests()
    lithub_client.reset_singleton_for_tests()
    service_token.reset_cache()


def _identity_user(sub=None, email="alice@example.com"):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(sub or uuid4()),
        "email": email,
        "full_name": "Alice",
        "is_verified": False,
        "is_active": True,
        "timezone": "UTC",
        "auth_methods": ["password"],
        "trial_used": False,
        "created_at": now,
        "updated_at": now,
    }


# ── ensure_mongo_shadow ─────────────────────────────────────────────


async def test_ensure_shadow_creates_new(db):
    from identity_bridge import ensure_mongo_shadow

    iu = _identity_user()
    shadow = await ensure_mongo_shadow(db, iu, give_trial=True)
    assert shadow["user_id"] == iu["id"]
    assert shadow["identity_id"] == iu["id"]
    assert shadow["trial_used"] is True
    assert len(db.users.docs) == 1


async def test_ensure_shadow_links_by_email(db):
    from identity_bridge import ensure_mongo_shadow

    legacy_uid = str(uuid4())
    db.users.docs.append({"user_id": legacy_uid, "email": "alice@example.com"})
    iu = _identity_user(email="ALICE@example.com")
    shadow = await ensure_mongo_shadow(db, iu)
    assert shadow["user_id"] == legacy_uid
    assert shadow["identity_id"] == iu["id"]
    assert len(db.users.docs) == 1  # linked, not duplicated


async def test_ensure_shadow_idempotent_by_identity_id(db):
    from identity_bridge import ensure_mongo_shadow

    iu = _identity_user()
    await ensure_mongo_shadow(db, iu)
    await ensure_mongo_shadow(db, iu)
    assert len(db.users.docs) == 1


# ── identity_signup ─────────────────────────────────────────────────


@respx.mock
async def test_identity_signup_creates_shadow_with_trial(db):
    from identity_bridge import identity_signup

    iu = _identity_user()
    respx.post("http://identity.test/api/v1/auth/signup").mock(
        return_value=httpx.Response(201, json={
            "access_token": "a", "refresh_token": "r", "token_type": "bearer",
            "expires_in": 1800, "user": iu,
        }),
    )
    shadow = await identity_signup(
        db, email="alice@example.com", password="StrongPass1!aa",
        full_name="Alice", timezone_str="UTC",
    )
    assert shadow["identity_id"] == iu["id"]
    assert shadow["trial_used"] is True


@respx.mock
async def test_identity_signup_duplicate_maps_to_400(db):
    from fastapi import HTTPException
    from identity_bridge import identity_signup

    respx.post("http://identity.test/api/v1/auth/signup").mock(
        return_value=httpx.Response(409, json={"detail": "already exists"}),
    )
    with pytest.raises(HTTPException) as ei:
        await identity_signup(
            db, email="alice@example.com", password="StrongPass1!aa",
            full_name="Alice", timezone_str="UTC",
        )
    assert ei.value.status_code == 400


# ── identity_login ──────────────────────────────────────────────────


@respx.mock
async def test_identity_login_success_returns_identity_token(db):
    from identity_bridge import identity_login

    iu = _identity_user()
    respx.post("http://identity.test/api/v1/auth/login").mock(
        return_value=httpx.Response(200, json={
            "access_token": "identity-rs256-token", "refresh_token": "r",
            "token_type": "bearer", "expires_in": 1800,
        }),
    )
    respx.get("http://identity.test/api/v1/auth/me").mock(
        return_value=httpx.Response(200, json=iu),
    )
    shadow, token = await identity_login(db, email="alice@example.com", password="StrongPass1!aa")
    assert token == "identity-rs256-token"
    assert shadow["identity_id"] == iu["id"]


@respx.mock
async def test_identity_login_legacy_migration(db):
    """A Mongo-only legacy user is migrated into Identity on first sign-in."""
    from auth_utils import hash_password
    from identity_bridge import identity_login

    legacy_uid = str(uuid4())
    db.users.docs.append({
        "user_id": legacy_uid,
        "email": "bob@example.com",
        "hashed_password": hash_password("StrongPass1!aa"),
        "is_active": True,
        "is_verified": True,
        "timezone": "UTC",
    })
    iu = _identity_user(email="bob@example.com")

    # First login attempt 401s (user not in Identity yet); then upsert; then login OK.
    login_route = respx.post("http://identity.test/api/v1/auth/login")
    login_route.side_effect = [
        httpx.Response(401, json={"detail": "Invalid email or password."}),
        httpx.Response(200, json={
            "access_token": "migrated-token", "refresh_token": "r",
            "token_type": "bearer", "expires_in": 1800,
        }),
    ]
    respx.post("http://identity.test/api/v1/internal/users/upsert-by-legacy").mock(
        return_value=httpx.Response(200, json={"user": iu, "created": True, "linked_by_email": False}),
    )
    respx.get("http://identity.test/api/v1/auth/me").mock(
        return_value=httpx.Response(200, json=iu),
    )

    shadow, token = await identity_login(db, email="bob@example.com", password="StrongPass1!aa")
    assert token == "migrated-token"
    assert shadow["user_id"] == legacy_uid           # keeps legacy id
    assert shadow["identity_id"] == iu["id"]          # now linked to Identity


@respx.mock
async def test_identity_login_wrong_password_for_legacy_user(db):
    from fastapi import HTTPException
    from auth_utils import hash_password
    from identity_bridge import identity_login

    db.users.docs.append({
        "user_id": str(uuid4()),
        "email": "bob@example.com",
        "hashed_password": hash_password("RightPass1!aa"),
        "is_active": True,
    })
    respx.post("http://identity.test/api/v1/auth/login").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid email or password."}),
    )
    with pytest.raises(HTTPException) as ei:
        await identity_login(db, email="bob@example.com", password="WrongPass1!aa")
    assert ei.value.status_code == 401
    assert ei.value.detail["error_code"] == "wrong_password"


@respx.mock
async def test_identity_login_unknown_account(db):
    from fastapi import HTTPException
    from identity_bridge import identity_login

    respx.post("http://identity.test/api/v1/auth/login").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid email or password."}),
    )
    respx.get("http://identity.test/api/v1/internal/users/by-email").mock(
        return_value=httpx.Response(200, json={"user": None, "exists": False}),
    )
    with pytest.raises(HTTPException) as ei:
        await identity_login(db, email="ghost@example.com", password="StrongPass1!aa")
    assert ei.value.status_code == 401
    assert ei.value.detail["error_code"] == "account_not_found"


# ── lithub_bridge mirror + read ─────────────────────────────────────


class _Payload:
    """Minimal stand-in for LibrarySavePayload (has model_dump)."""

    def __init__(self, **kw):
        self._d = {
            "pmid": None, "doi": None, "folder": "Inbox", "title": None,
            "journal": None, "year": None, "full_text_status": None,
            "best_full_text_url": None, "publication_type": None,
            "recommended": False, "selected": False, "source": "search",
            "answer_context_id": None, "portal_engine_record_id": None,
        }
        self._d.update(kw)

    def model_dump(self):
        return dict(self._d)


@respx.mock
async def test_mirror_save_uses_identity_sub(db):
    from lithub_bridge import mirror_save_to_lithub

    sub = str(uuid4())
    db.users.docs.append({"user_id": "litpulse-uid", "identity_id": sub, "email": "a@b.com"})

    route = respx.post("http://lithub.test/api/v1/internal/library/save").mock(
        return_value=httpx.Response(200, json={
            "message": "ok", "article_id": str(uuid4()), "paper_id": str(uuid4()),
            "library_entry_id": str(uuid4()), "dedup_key": "pmid:123",
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }),
    )
    await mirror_save_to_lithub(db, "litpulse-uid", _Payload(pmid="123", title="T"))
    assert route.called
    sent = route.calls.last.request
    import json as _json
    body = _json.loads(sent.content)
    assert body["user_id"] == sub          # keyed by Identity sub, not litpulse uid
    assert body["item"]["pmid"] == "123"


async def test_mirror_save_skips_without_identity_id(db):
    from lithub_bridge import mirror_save_to_lithub

    db.users.docs.append({"user_id": "litpulse-uid", "email": "a@b.com"})  # no identity_id
    # Should not raise and not call LitHub (no respx route registered).
    await mirror_save_to_lithub(db, "litpulse-uid", _Payload(pmid="123", title="T"))


@respx.mock
async def test_read_from_lithub_backfills_then_lists(db):
    from lithub_bridge import read_library_from_lithub

    sub = str(uuid4())
    db.users.docs.append({"user_id": "litpulse-uid", "identity_id": sub, "email": "a@b.com"})
    # Pre-existing Mongo library entry that should be backfilled.
    db.library.docs.append({
        "user_id": "litpulse-uid", "pmid": "999", "title": "Legacy Paper",
        "journal": "J", "folder": "Inbox", "source": "search",
    })

    backfill = respx.post("http://lithub.test/api/v1/internal/library/bulk-import").mock(
        return_value=httpx.Response(200, json={
            "user_id": sub, "imported": 1, "skipped_duplicate": 0,
            "skipped_invalid": 0, "articles": [],
        }),
    )
    listing = respx.get("http://lithub.test/api/v1/internal/library").mock(
        return_value=httpx.Response(200, json={
            "articles": [{
                "pmid": "999", "doi": None, "title": "Legacy Paper", "journal": "J",
                "pub_date": None, "authors": None, "abstract": None, "ai_summary": None,
                "design_tags": None, "mesh_terms": None, "url": None,
                "saved_at": datetime.now(timezone.utc).isoformat(), "folder": "Inbox",
                "paper_id": str(uuid4()), "library_entry_id": str(uuid4()),
                "source": "search", "full_text_status": None, "best_full_text_url": None,
                "recommended": False, "selected": False, "answer_context_id": None,
                "portal_engine_record_id": None, "notes": None,
            }],
            "total": 1, "next_cursor": None,
        }),
    )
    result = await read_library_from_lithub(db, "litpulse-uid")
    assert backfill.called
    assert listing.called
    assert result is not None
    assert result["total"] == 1
    assert result["articles"][0]["pmid"] == "999"
    # Backfill flag set so a second read does not re-import.
    user = await db.users.find_one({"user_id": "litpulse-uid"})
    assert user.get("lithub_backfilled_at")


async def test_read_from_lithub_returns_none_when_disabled(db, monkeypatch):
    monkeypatch.setenv("LITPULSE_USE_LITHUB", "false")
    from lithub_bridge import read_library_from_lithub

    sub = str(uuid4())
    db.users.docs.append({"user_id": "u", "identity_id": sub})
    result = await read_library_from_lithub(db, "u")
    assert result is None


@respx.mock
async def test_read_from_lithub_falls_back_to_mongo_on_lithub_down(db):
    from lithub_bridge import read_library_from_lithub

    sub = str(uuid4())
    db.users.docs.append({"user_id": "u", "identity_id": sub, "lithub_backfilled_at": "x"})
    respx.get("http://lithub.test/api/v1/internal/library").mock(
        return_value=httpx.Response(503, text="down"),
    )
    result = await read_library_from_lithub(db, "u")
    assert result is None  # signals caller to fall back to Mongo


async def test_mirror_save_noops_when_lithub_url_unset(db, monkeypatch):
    """Regression: dual-write defaults ON, but with no LITHUB_BASE_URL the mirror
    must be a strict no-op (never raise) so an un-configured deploy still works."""
    monkeypatch.delenv("LITHUB_BASE_URL", raising=False)
    monkeypatch.setenv("LITPULSE_DUAL_WRITE_LITHUB", "true")
    import lithub_client
    lithub_client.reset_singleton_for_tests()
    from lithub_bridge import mirror_save_to_lithub

    sub = str(uuid4())
    db.users.docs.append({"user_id": "u", "identity_id": sub, "email": "a@b.com"})
    # Must not raise even though a user with identity_id exists and dual-write is on.
    await mirror_save_to_lithub(db, "u", _Payload(pmid="123", title="T"))


async def test_read_from_lithub_returns_none_when_url_unset(db, monkeypatch):
    monkeypatch.delenv("LITHUB_BASE_URL", raising=False)
    monkeypatch.setenv("LITPULSE_USE_LITHUB", "true")
    import lithub_client
    lithub_client.reset_singleton_for_tests()
    from lithub_bridge import read_library_from_lithub

    sub = str(uuid4())
    db.users.docs.append({"user_id": "u", "identity_id": sub})
    result = await read_library_from_lithub(db, "u")
    assert result is None
