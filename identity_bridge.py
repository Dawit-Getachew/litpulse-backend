"""Bridge between LitPulse's Mongo-backed endpoints and the Scienthesis Identity Service.

When ``LITPULSE_USE_IDENTITY`` is enabled, signup/login delegate account
ownership to the Identity Service (the single source of truth for who a user
is across LitPulse + LitPortal). LitPulse keeps a thin Mongo "shadow" document
per user so all the existing Mongo-dependent features (trials, capabilities,
practice profile, beta, digests, library read-state) keep working unchanged.

Canonical identity model:
  * Identity ``sub`` (UUID) is the platform-wide user id.
  * LitPulse Mongo ``db.users`` rows carry ``identity_id == sub``.
  * New Identity-native users use ``user_id == sub`` so there is a single id.
  * Pre-existing Mongo users keep their old ``user_id`` and get ``identity_id``
    stamped on first Identity login (lazy migration).

This module raises FastAPI ``HTTPException`` with the SAME error_code detail
shapes the legacy endpoints used, so the frontend sees no behavioral change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status

from auth_utils import hash_password, verify_password  # noqa: F401 (hash_password reserved for future use)
from identity_client import (
    IdentityClientError,
    IdentityUpstreamError,
    get_identity_client,
    is_identity_enabled,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def ensure_mongo_shadow(
    db: Any,
    identity_user: dict[str, Any],
    *,
    give_trial: bool = False,
    legacy_password_hash: str | None = None,
    practice_profile: dict | None = None,
) -> dict[str, Any]:
    """Find-or-create the Mongo shadow doc for an Identity user.

    Resolution precedence:
      1. By ``identity_id`` (repeat-contact fast path).
      2. By ``email`` — link the existing row by stamping ``identity_id``.
      3. Create a new shadow keyed by ``user_id == identity sub``.

    ``give_trial`` mirrors the legacy signup's 30-day Pro trial for brand-new
    accounts so capability computation is identical to the old path.
    """
    sub = str(identity_user["id"])
    email = (identity_user.get("email") or "").strip().lower()
    now = _now_iso()

    identity_verified = bool(identity_user.get("is_verified", False))

    existing = await db.users.find_one({"identity_id": sub})
    if existing:
        # Keep the Mongo shadow's verification state in sync with Identity, so a
        # verify-code / password-reset / OTP that verified the email in Identity
        # is reflected on /auth/me (which reads the shadow).
        if identity_verified and not existing.get("is_verified"):
            await db.users.update_one(
                {"user_id": existing["user_id"]},
                {"$set": {"is_verified": True, "updated_at": now}},
            )
            existing["is_verified"] = True
        return existing

    if email:
        by_email = await db.users.find_one({"email": email})
        if by_email:
            patch: dict[str, Any] = {"identity_id": sub, "updated_at": now}
            if identity_verified and not by_email.get("is_verified"):
                patch["is_verified"] = True
            await db.users.update_one(
                {"user_id": by_email["user_id"]},
                {"$set": patch},
            )
            by_email["identity_id"] = sub
            if identity_verified:
                by_email["is_verified"] = True
            return by_email

    new_doc: dict[str, Any] = {
        "user_id": sub,
        "identity_id": sub,
        "email": email,
        "full_name": identity_user.get("full_name"),
        "is_verified": bool(identity_user.get("is_verified", False)),
        "is_active": bool(identity_user.get("is_active", True)),
        "timezone": identity_user.get("timezone") or "UTC",
        "created_at": identity_user.get("created_at") or now,
        "updated_at": now,
    }
    if give_trial:
        trial_ends = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        new_doc.update(
            trial_ends_at=trial_ends,
            trial_expires_at=trial_ends,
            trial_used=True,
            trial_started_at=now,
        )
    if legacy_password_hash:
        new_doc["hashed_password"] = legacy_password_hash
    if practice_profile:
        new_doc["practice_profile"] = practice_profile

    await db.users.insert_one(new_doc)
    logger.info(f"Created Mongo shadow for identity user sub={sub}")
    return new_doc


async def identity_signup(
    db: Any,
    *,
    email: str,
    password: str,
    full_name: str | None,
    timezone_str: str | None,
    invite_code: str | None = None,
    practice_profile: dict | None = None,
) -> dict[str, Any]:
    """Delegate signup to Identity, then provision the Mongo shadow.

    Returns the Mongo shadow doc (the same shape the legacy signup inserted),
    so the caller can build the existing ``UserResponse``.
    """
    client = get_identity_client()
    body = {
        "email": email,
        "signup_method": "password",
        "password": password,
        "full_name": full_name,
        "timezone": timezone_str or "UTC",
    }
    if invite_code:
        body["invite_code"] = invite_code

    try:
        result = await client.signup(body)
    except IdentityClientError as exc:
        if exc.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        if exc.status_code in (400, 422):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=exc.detail if isinstance(exc.detail, str) else "Invalid signup data",
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Identity service error during signup",
        )
    except IdentityUpstreamError as exc:
        logger.error(f"Identity upstream error during signup: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service unavailable",
        )

    identity_user = result["user"]
    shadow = await ensure_mongo_shadow(
        db,
        identity_user,
        give_trial=True,
        practice_profile=practice_profile,
    )
    return shadow


async def identity_login(
    db: Any,
    *,
    email: str,
    password: str,
) -> tuple[dict[str, Any], str]:
    """Delegate login to Identity. Returns ``(mongo_shadow_doc, access_token)``.

    The returned ``access_token`` is the Identity-issued RS256 token, so the
    same token validates on LitPortal too (single-sign-on). Lazy migration:
    a legacy Mongo-only user whose Identity login 401s is verified against the
    Mongo bcrypt hash, provisioned into Identity via ``upsert-by-legacy``, and
    logged in again — all transparently on first sign-in.

    Raises HTTPException with the legacy error_code detail shapes so the
    frontend behavior is unchanged.
    """
    email_lower = email.strip().lower()
    client = get_identity_client()

    try:
        result = await client.login(email_lower, password)
        access_token = result["access_token"]
        # Identity's /auth/login returns only the token pair; fetch the user
        # via /auth/me so we can provision/refresh the Mongo shadow.
        identity_user = await client.get_me(access_token)
        shadow = await ensure_mongo_shadow(db, identity_user)
        return shadow, access_token
    except IdentityUpstreamError as exc:
        logger.error(f"Identity upstream error during login: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service unavailable",
        )
    except IdentityClientError as exc:
        if exc.status_code != 401:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Identity service error during login",
            )

    # Identity returned 401. Attempt lazy migration of a legacy Mongo-only user.
    mongo = await db.users.find_one({"email": email_lower})
    if mongo and mongo.get("hashed_password") and verify_password(password, mongo["hashed_password"]):
        try:
            await client.internal_upsert_by_legacy(
                {
                    "email": email_lower,
                    "full_name": mongo.get("full_name"),
                    "password_hash": mongo["hashed_password"],
                    "auth_methods": ["password"],
                    "is_verified": mongo.get("is_verified", False),
                    "is_active": mongo.get("is_active", True),
                    "timezone": mongo.get("timezone", "UTC"),
                    "litpulse_legacy_id": mongo["user_id"],
                },
            )
            result = await client.login(email_lower, password)
            access_token = result["access_token"]
            identity_user = await client.get_me(access_token)
        except (IdentityClientError, IdentityUpstreamError) as exc:
            logger.error(f"Legacy migration login failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Identity service unavailable",
            )
        await db.users.update_one(
            {"user_id": mongo["user_id"]},
            {"$set": {"identity_id": str(identity_user["id"]), "updated_at": _now_iso()}},
        )
        mongo["identity_id"] = str(identity_user["id"])
        return mongo, access_token

    # Not a migratable legacy user. Distinguish wrong-password from unknown-account
    # to preserve the legacy error_code UX.
    if mongo:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "wrong_password", "message": "Incorrect password. Please try again or reset your password."},
        )
    try:
        lookup = await client.internal_lookup_by_email(email_lower)
        exists_in_identity = bool(lookup.get("exists"))
    except (IdentityClientError, IdentityUpstreamError):
        exists_in_identity = False
    if exists_in_identity:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "wrong_password", "message": "Incorrect password. Please try again or reset your password."},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error_code": "account_not_found", "message": "Account does not exist. Please sign up."},
    )


async def get_identity_user_id(db: Any, user_id: str) -> str | None:
    """Return the Identity ``sub`` for a LitPulse Mongo ``user_id`` (or None)."""
    doc = await db.users.find_one({"user_id": user_id}, {"_id": 0, "identity_id": 1})
    if not doc:
        return None
    return doc.get("identity_id")


__all__ = [
    "ensure_mongo_shadow",
    "get_identity_user_id",
    "identity_login",
    "identity_signup",
    "is_identity_enabled",
]
