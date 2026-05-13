"""
Beta Gate — Invite-only signup, waitlist, and capacity management.

Beta statuses:
  - invited: invite sent, not yet signed up
  - active_beta: signed up and activated for beta
  - waitlist: signed up but capacity full, on waitlist
  - paused: admin paused access
  - removed: admin removed from beta

Controlled by env vars:
  ENABLE_INVITE_ONLY_BETA=true
  BETA_ACTIVE_CAPACITY=30
  BETA_WAITLIST_CAPACITY=20
  BETA_SPECIALTY_ID=internal_medicine
"""
import os
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def is_beta_enabled() -> bool:
    return os.environ.get("ENABLE_INVITE_ONLY_BETA", "false").lower() == "true"


def get_beta_specialty() -> str:
    return os.environ.get("BETA_SPECIALTY_ID", "internal_medicine")


def get_active_capacity() -> int:
    return int(os.environ.get("BETA_ACTIVE_CAPACITY", "30"))


def get_waitlist_capacity() -> int:
    return int(os.environ.get("BETA_WAITLIST_CAPACITY", "20"))


async def check_invite_code(db, invite_code: str) -> Optional[dict]:
    """Validate an invite code. Returns invite doc or None."""
    if not invite_code:
        return None
    doc = await db.beta_invites.find_one(
        {"invite_code": invite_code, "used": False},
        {"_id": 0},
    )
    return doc


async def mark_invite_used(db, invite_code: str, user_id: str):
    """Mark an invite code as used."""
    await db.beta_invites.update_one(
        {"invite_code": invite_code},
        {"$set": {"used": True, "used_by": user_id, "used_at": datetime.now(timezone.utc).isoformat()}},
    )


async def determine_beta_status(db) -> str:
    """Determine what status a new signup should get: active_beta or waitlist."""
    active_count = await db.users.count_documents({"beta_status": "active_beta"})
    if active_count < get_active_capacity():
        return "active_beta"
    waitlist_count = await db.users.count_documents({"beta_status": "waitlist"})
    if waitlist_count < get_waitlist_capacity():
        return "waitlist"
    return "waitlist"  # over capacity, still waitlist (admin decides)


async def enforce_beta_access(db, user_id: str):
    """Check if user has active beta access. Raises 403 if not."""
    from fastapi import HTTPException, status
    if not is_beta_enabled():
        return  # beta gate off, everyone allowed
    user = await db.users.find_one(
        {"user_id": user_id},
        {"_id": 0, "beta_status": 1, "email": 1},
    )
    if not user:
        raise HTTPException(status_code=403, detail={"error_code": "beta_access_required", "message": "Beta access required."})
    # Admin always passes
    admin_email = os.environ.get("ADMIN_EMAIL", "")
    if admin_email and user.get("email", "").lower() == admin_email.lower():
        return
    beta_status = user.get("beta_status", "")
    if beta_status not in ("active_beta",):
        msg = "You are on the waitlist." if beta_status == "waitlist" else "Beta access required."
        raise HTTPException(status_code=403, detail={"error_code": "beta_access_required", "message": msg, "beta_status": beta_status})
