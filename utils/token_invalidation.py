"""
Single-use token invalidation for auth tokens (verify email, reset password).
Uses auth_token_uses collection with unique index + TTL.
"""
import hashlib
import uuid
import logging
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

_db: AsyncIOMotorDatabase = None


def set_db(database: AsyncIOMotorDatabase):
    global _db
    _db = database


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def check_and_mark_token_used(
    token: str,
    purpose: str,
    user_id: str = "",
    expires_at: str = "",
) -> None:
    """
    Check if a token has been used. If not, mark it as used.
    Raises 400 if already used. Must be called BEFORE applying the state change.
    """
    if _db is None:
        return  # DB not set — skip (dev safety)

    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc).isoformat()

    try:
        await _db.auth_token_uses.insert_one({
            "token_use_id": str(uuid.uuid4()),
            "token_hash": token_hash,
            "purpose": purpose,
            "user_id": user_id,
            "used_at": now,
            "expires_at": expires_at or now,
        })
    except Exception as e:
        # Duplicate key = token already used
        if "duplicate key" in str(e).lower() or "E11000" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "token_already_used",
                    "message": "This link has already been used. Please request a new one.",
                },
            )
        raise
