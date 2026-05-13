"""
===============================================================================
TEMPORARY Admin Endpoint — Stage 1A Migration Dry-Run Audit
===============================================================================

*** THIS ENDPOINT IS TEMPORARY AND MUST BE REMOVED AFTER MIGRATION ***

This module provides a single admin-only, feature-gated, dry-run-only
endpoint to run the Stage 1A PMID migration audit against the live database
and return structured JSON results.

Security Controls:
  - Requires admin authentication (same pattern as beta_admin.py)
  - Feature-gated by ENABLE_ADMIN_MIGRATION_DRYRUN env var (default: FALSE)
  - The env var MUST NOT be set to 'true' in default repo config or .env.example
  - Only the production deployment environment should enable this endpoint
  - Always runs in read-only / dry-run mode — no option to enable writes
  - User identifiers are redacted in anomaly samples
  - No secrets, connection strings, or raw env values are returned

REMOVAL INSTRUCTIONS:
  1. After Stage 1A migration is complete and validated
  2. Delete this file: routes/admin_migration_dryrun.py
  3. Remove import and router registration from server.py
  4. Remove ENABLE_ADMIN_MIGRATION_DRYRUN from .env files
  5. Remove the env var from deployment configuration

Created: 2025-03-XX for Stage 1A PMID migration
Expected Removal: After migration validation is complete
===============================================================================
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth_utils import get_current_user
from utils.migration_core import run_migration_dryrun

logger = logging.getLogger(__name__)

# ── Router setup ────────────────────────────────────────────────
router = APIRouter(prefix="/admin", tags=["admin-migration"])

# ── Module-level state (injected by server.py at startup) ───────
db = None
_admin_email = ""


def set_db(database):
    """Set the database reference. Called from server.py lifespan."""
    global db
    db = database


def set_admin_email(email: str):
    """Set the admin email for auth verification. Called from server.py lifespan."""
    global _admin_email
    _admin_email = email.lower() if email else ""


# ── Admin auth (same pattern as beta_admin.py) ──────────────────

async def _verify_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Verify the current user is an admin.
    
    Uses the same ADMIN_EMAIL-based pattern used throughout LitPulse.
    """
    if not _admin_email:
        raise HTTPException(status_code=403, detail="Admin not configured")
    user = await db.users.find_one(
        {"user_id": current_user["user_id"]},
        {"_id": 0, "email": 1}
    )
    if user and user.get("email", "").lower() == _admin_email:
        return current_user
    raise HTTPException(status_code=403, detail="Admin access required")


# ── Feature gate ────────────────────────────────────────────────

def _is_migration_dryrun_enabled() -> bool:
    """Check if the migration dry-run endpoint is enabled via env var."""
    raw = os.environ.get("ENABLE_ADMIN_MIGRATION_DRYRUN", "false")
    return raw.strip().lower() == "true"


# ── Request / Response models ───────────────────────────────────

class MigrationDryrunRequest(BaseModel):
    """Request body for the migration dry-run endpoint."""
    user_id: Optional[str] = Field(
        default=None,
        description="Optional: scope dry-run to a single user_id"
    )
    phases: str = Field(
        default="ABCD",
        description="Which phases to run (A=Inspect, B=Backfill, C=Merge, D=Reconcile)",
        pattern=r"^[ABCDabcd]{1,4}$"
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=10000,
        description="Optional: max documents to scan per phase (safety cap: 10000)"
    )


# ── Endpoint ────────────────────────────────────────────────────

MAX_LIMIT_CAP = 10000
DEFAULT_SAMPLE_LIMIT = 5


@router.post("/migration-dryrun", summary="[TEMPORARY] Stage 1A Migration Dry-Run Audit")
async def migration_dryrun(
    body: MigrationDryrunRequest,
    admin_user: dict = Depends(_verify_admin),
):
    """
    TEMPORARY — Run Stage 1A PMID migration audit in DRY-RUN mode.

    This endpoint is:
      - Admin-only (requires ADMIN_EMAIL match)
      - Feature-gated (ENABLE_ADMIN_MIGRATION_DRYRUN must be 'true')
      - Read-only (never mutates data, regardless of input)
      - Redacted (user identifiers masked in anomaly samples)

    Returns structured JSON with phase stats and anomaly samples.

    REMOVE THIS ENDPOINT after migration is complete and validated.
    """
    # ── Feature gate check ──
    if not _is_migration_dryrun_enabled():
        raise HTTPException(
            status_code=404,
            detail="Not found"  # Intentionally vague when disabled
        )

    # ── DB check ──
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    # ── Enforce limit cap ──
    safe_limit = min(body.limit, MAX_LIMIT_CAP) if body.limit else None

    # ── Log the audit request (redacted) ──
    admin_uid = admin_user.get("user_id", "unknown")
    redacted_admin = admin_uid[:4] + "..." + admin_uid[-4:] if len(admin_uid) > 10 else "***"
    logger.info(
        "ADMIN MIGRATION DRY-RUN requested by admin=%s phases=%s user_scope=%s limit=%s",
        redacted_admin,
        body.phases,
        "scoped" if body.user_id else "all",
        safe_limit,
    )

    # ── Run dry-run (ALWAYS apply=False — enforced in migration_core) ──
    try:
        result = await run_migration_dryrun(
            db=db,
            user_id=body.user_id,
            phases=body.phases,
            limit=safe_limit,
            sample_limit=DEFAULT_SAMPLE_LIMIT,
        )
    except Exception as e:
        logger.error("Migration dry-run failed: %s", str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Dry-run failed: {type(e).__name__}"
        )

    # ── Stamp metadata ──
    result["endpoint"] = "POST /api/admin/migration-dryrun"
    result["requested_by"] = redacted_admin
    result["note"] = (
        "This is a READ-ONLY dry-run. No data was modified. "
        "User identifiers in anomaly samples are redacted."
    )

    logger.info(
        "ADMIN MIGRATION DRY-RUN completed. ua_total=%s",
        result.get("phases", {}).get("A_inspect", {}).get("ua_total", "N/A"),
    )

    return result
