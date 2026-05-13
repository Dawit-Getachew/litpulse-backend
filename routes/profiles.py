"""
Digest Profiles Routes — Phase 5 + Phase UX-D
Behind ENABLE_MULTI_DIGEST_PROFILES

New endpoints:
  GET    /api/preferences/profiles           — list user's active profiles
  POST   /api/preferences/profiles           — create a profile
  PUT    /api/preferences/profiles/{id}      — update a profile
  DELETE /api/preferences/profiles/{id}      — soft-delete + cascade to digests

Phase UX-D: Full Preferences Wizard per Digest Profile
When ENABLE_DIGEST_PROFILE_FULL_WIZARD=true, each profile supports:
  - topics_selected, custom_topics
  - journals_selected, custom_journals
  - max_articles_per_digest
  - email_notifications_enabled, email_suppress_until
  - advanced_preferences (clinical_notes, journal_notes)
  - timezone

Backward compat:
  GET /api/preferences/me      — untouched, still reads from `preferences` collection
  POST /api/preferences        — untouched

Limits:
  Free users: 1 active profile
  Premium / trial-active users: 5 active profiles

Migration:
  On first GET /api/preferences/profiles call, if no profiles exist, create a
  default profile from existing preferences (silent, idempotent).

PHI-Zero: profile names and custom_keywords are never logged.
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel, Field

from auth_utils import get_current_user
from date_utils import compute_next_run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/preferences", tags=["profiles"])
db = None


def set_db(database):
    global db
    db = database


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_PROFILES_FREE = 1
MAX_PROFILES_VERIFIED = 3
MAX_PROFILES_PREMIUM = 5
SUPPORT_EMAIL = "info@scienthesis.ai"

# Phase UX-D: Valid suppress options in days (for new values)
VALID_SUPPRESS_DAYS = [1, 3, 7, 14, 28]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ProfileSchedule(BaseModel):
    frequency: str = "weekly"          # daily | weekly | biweekly | monthly
    day_of_week: Optional[str] = None  # Monday…Sunday
    day_of_month: Optional[int] = None
    hour: int = 9
    minute: int = 0
    time_local: Optional[str] = None   # HH:MM format (Phase UX-D)
    timezone: Optional[str] = None     # IANA timezone (Phase UX-D)


class ProfileAdvancedPreferences(BaseModel):
    """Phase UX-D: Per-profile advanced preferences."""
    clinical_notes: Optional[str] = None
    journal_notes: Optional[str] = None


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    specialty_id: str
    subspecialty_id: Optional[str] = None
    subspecialties: List[str] = Field(default_factory=list, max_length=3)  # Phase UX-E: max 3 subspecialties
    custom_keywords: List[str] = Field(default_factory=list)
    schedule: ProfileSchedule = Field(default_factory=ProfileSchedule)
    # Phase UX-C: community subspecialty selection (max 3)
    community_subspecialty_ids: List[str] = Field(default_factory=list)
    # Phase UX-D: Full wizard fields
    topics_selected: List[str] = Field(default_factory=list)
    custom_topics: List[str] = Field(default_factory=list)
    journals_selected: List[str] = Field(default_factory=list)
    custom_journals: List[str] = Field(default_factory=list)
    max_articles_per_digest: int = Field(default=10, ge=5, le=20)
    email_notifications_enabled: bool = True
    email_suppress_until: Optional[str] = None  # ISO date string
    advanced_preferences: Optional[ProfileAdvancedPreferences] = None
    # Phase UX-E: Primary profile flag
    is_primary: bool = False


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    specialty_id: Optional[str] = None
    subspecialty_id: Optional[str] = None
    subspecialties: Optional[List[str]] = None  # Phase UX-E: max 3 subspecialties
    custom_keywords: Optional[List[str]] = None
    schedule: Optional[ProfileSchedule] = None
    is_active: Optional[bool] = None
    # Phase UX-C: community subspecialty selection (max 3)
    community_subspecialty_ids: Optional[List[str]] = None
    # Phase UX-D: Full wizard fields
    topics_selected: Optional[List[str]] = None
    custom_topics: Optional[List[str]] = None
    journals_selected: Optional[List[str]] = None
    custom_journals: Optional[List[str]] = None
    max_articles_per_digest: Optional[int] = Field(None, ge=5, le=20)
    email_notifications_enabled: Optional[bool] = None
    email_suppress_until: Optional[str] = None  # ISO date string or None to clear
    advanced_preferences: Optional[ProfileAdvancedPreferences] = None
    # Phase UX-E: Primary profile flag
    is_primary: Optional[bool] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _feature_check():
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    if not flags.get("enable_multi_digest_profiles", False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "feature_disabled", "message": "Digest Profiles not enabled."},
        )
    return flags


def _get_max_profiles(user: dict, flags: dict) -> int:
    """Return profile limit based on plan + trial + verification status."""
    from utils.capabilities import derive_plan_tier, _is_new_trial_active
    plan = derive_plan_tier(user)
    trial = _is_new_trial_active(user, flags)
    if plan == "premium" or trial:
        return MAX_PROFILES_PREMIUM
    # Check clinician verification for middle tier
    # Note: verification_doc lookup is sync-safe here since we only need the flag
    # The actual enforcement uses the capabilities engine
    return MAX_PROFILES_FREE


async def _get_max_profiles_async(user: dict, user_id: str, flags: dict) -> int:
    """Async version that checks verification status for the 3-tier model."""
    from utils.capabilities import derive_plan_tier, _is_new_trial_active, is_clinician_verified
    plan = derive_plan_tier(user)
    trial = _is_new_trial_active(user, flags)
    if plan == "premium" or trial:
        return MAX_PROFILES_PREMIUM
    # Check clinician verification for middle tier
    verification_doc = await db.professional_verifications.find_one(
        {"user_id": user_id}, {"_id": 0, "status": 1}
    )
    if is_clinician_verified(verification_doc):
        return MAX_PROFILES_VERIFIED
    return MAX_PROFILES_FREE


async def enforce_profile_limits(user_id: str, flags: dict) -> int:
    """Idempotent: ensure only max_profiles profiles are active.
    
    If active_count > max_profiles, deactivate extras:
      1) Keep primary profile
      2) Keep oldest created_at
    Sets frozen_reason='trial_expired' and frozen_at on deactivated ones.
    Returns the number of profiles frozen in this call.
    """
    from datetime import datetime, timezone as _tz
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        return 0
    max_profiles = await _get_max_profiles_async(user, user_id, flags)

    active_profiles = await db.digest_profiles.find(
        {"user_id": user_id, "is_active": True, "deleted_at": None},
        {"_id": 0, "profile_id": 1, "is_primary": 1, "created_at": 1},
    ).sort([("is_primary", -1), ("created_at", 1)]).to_list(20)

    if len(active_profiles) <= max_profiles:
        # Under limit — also clear frozen_reason on any active profiles that had it
        await db.digest_profiles.update_many(
            {"user_id": user_id, "is_active": True, "frozen_reason": {"$ne": None}},
            {"$set": {"frozen_reason": None, "frozen_at": None}},
        )
        return 0

    # Keep first max_profiles (primary first, then oldest)
    keep_ids = {p["profile_id"] for p in active_profiles[:max_profiles]}
    freeze_ids = [p["profile_id"] for p in active_profiles if p["profile_id"] not in keep_ids]

    now_iso = datetime.now(_tz.utc).isoformat()
    if freeze_ids:
        await db.digest_profiles.update_many(
            {"user_id": user_id, "profile_id": {"$in": freeze_ids}},
            {"$set": {
                "is_active": False,
                "frozen_reason": "trial_expired",
                "frozen_at": now_iso,
            }},
        )
        logger.info("[PROFILES] Frozen %d profiles for user=%s (max=%d)", len(freeze_ids), user_id, max_profiles)

    # Ensure kept profiles don't have stale frozen_reason
    await db.digest_profiles.update_many(
        {"user_id": user_id, "profile_id": {"$in": list(keep_ids)}, "frozen_reason": {"$ne": None}},
        {"$set": {"frozen_reason": None, "frozen_at": None}},
    )

    return len(freeze_ids)




def _is_premium_or_trial(user: dict, flags: dict) -> bool:
    """Check if user is premium or has active trial."""
    from utils.capabilities import derive_plan_tier, _is_new_trial_active
    plan = derive_plan_tier(user)
    trial = _is_new_trial_active(user, flags)
    return plan == "premium" or trial


MAX_COMMUNITY_SUBSPECIALTIES = 3


def _validate_community_subspecialties(
    community_subspecialty_ids: List[str],
    subspecialty_id: Optional[str],
    user: dict,
    flags: dict,
) -> None:
    """
    Validate community_subspecialty_ids based on user tier.
    
    Rules:
    - Max 3 subspecialties for all users
    - Free users: can only select subspecialty_id if it exists (their digest subspecialty)
      OR empty list (no community subspecialties)
    - Premium/trial: up to 3 any subspecialties
    
    Raises HTTPException if validation fails.
    """
    if not community_subspecialty_ids:
        return  # Empty list is always valid
    
    # Check max limit
    if len(community_subspecialty_ids) > MAX_COMMUNITY_SUBSPECIALTIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "too_many_community_subspecialties",
                "message": f"Maximum {MAX_COMMUNITY_SUBSPECIALTIES} community subspecialties allowed.",
                "max_allowed": MAX_COMMUNITY_SUBSPECIALTIES,
            },
        )
    
    # Check duplicates
    if len(community_subspecialty_ids) != len(set(community_subspecialty_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "duplicate_subspecialties",
                "message": "Duplicate subspecialty IDs are not allowed.",
            },
        )
    
    # Premium/trial users can select any subspecialties
    if _is_premium_or_trial(user, flags):
        return
    
    # Free users: can only select their digest subspecialty (if any)
    if subspecialty_id:
        # Free user has a digest subspecialty - they can only select that one
        if community_subspecialty_ids != [subspecialty_id]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "free_tier_subspecialty_limit",
                    "message": "Free users can only select their digest subspecialty for community access.",
                    "allowed_subspecialty": subspecialty_id,
                },
            )
    else:
        # Free user has no digest subspecialty - they cannot select any
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "free_tier_no_subspecialty",
                "message": "Free users must have a digest subspecialty to select community subspecialties. Upgrade to Pro for more options.",
            },
        )


def _clean(doc: dict) -> dict:
    """Remove MongoDB internal fields."""
    doc.pop("_id", None)
    return doc


# ---------------------------------------------------------------------------
# Phase UX-E: Dual-write + Primary Profile Helpers
# ---------------------------------------------------------------------------

MAX_SUBSPECIALTIES = 3  # Phase UX-E: Limit subspecialties to 3


async def _dual_write_to_legacy_preferences(profile: dict, user_id: str, flags: dict) -> None:
    """
    Phase UX-E: Dual-write profile data to legacy preferences collection.
    
    This ensures the legacy scheduler continues to work when:
    - ENABLE_PREFERENCES_DUAL_WRITE=true
    - ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER=false
    
    Only writes if the profile is marked as primary (or is the only profile).
    PHI-Zero: no user text is logged.
    """
    if not flags.get("enable_preferences_dual_write", False):
        return
    
    # Only dual-write if scheduler is NOT using profiles yet
    if flags.get("enable_multi_digest_profiles_scheduler", False):
        return
    
    # Check if this profile is primary (or the only active profile)
    is_primary = profile.get("is_primary", False)
    if not is_primary:
        # Check if this is the only active profile
        count = await db.digest_profiles.count_documents({
            "user_id": user_id,
            "deleted_at": None,
            "is_active": True,
        })
        if count > 1:
            return  # Not primary and not the only profile - skip dual-write
    
    # Build legacy preferences payload
    schedule = {
        "frequency": profile.get("digest_frequency", "weekly"),
        "day_of_week": profile.get("day_of_week"),
        "day_of_month": profile.get("day_of_month"),
        "hour": 9,
        "minute": 0,
        "timezone": profile.get("schedule_timezone"),
    }
    
    # Parse delivery time if present
    if profile.get("delivery_time"):
        try:
            parts = profile["delivery_time"].split(":")
            schedule["hour"] = int(parts[0])
            schedule["minute"] = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            pass
    
    legacy_prefs = {
        "user_id": user_id,
        "specialty_id": profile.get("specialty_id", ""),
        "subspecialty_id": profile.get("subspecialty_id"),
        "subspecialties": profile.get("subspecialties", []),
        "topics_selected": profile.get("topics_selected", []),
        "custom_topics": profile.get("custom_topics", []),
        "journals_selected": profile.get("journals_selected", []),
        "custom_journals": profile.get("custom_journals", []),
        "max_articles_per_digest": profile.get("max_articles_per_digest", 10),
        "schedule": schedule,
        "email_notifications_enabled": profile.get("email_notifications_enabled", True),
        "email_suppress_until": profile.get("email_suppress_until"),
        "advanced_preferences": profile.get("advanced_preferences"),
        "is_active": profile.get("is_active", True),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Upsert to legacy preferences
    await db.preferences.update_one(
        {"user_id": user_id},
        {"$set": legacy_prefs, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    logger.info("[PROFILES] Dual-write to legacy preferences for user=%s", user_id)


async def _ensure_single_primary(user_id: str, primary_profile_id: str) -> None:
    """
    Phase UX-E: Ensure only one profile is marked as primary.
    Unsets is_primary on all other profiles for this user.
    """
    await db.digest_profiles.update_many(
        {
            "user_id": user_id,
            "profile_id": {"$ne": primary_profile_id},
            "deleted_at": None,
        },
        {"$set": {"is_primary": False}},
    )


def _validate_subspecialties_limit(subspecialties: List[str]) -> None:
    """Phase UX-E: Validate subspecialties limit (max 3)."""
    if subspecialties and len(subspecialties) > MAX_SUBSPECIALTIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "too_many_subspecialties",
                "message": f"Maximum {MAX_SUBSPECIALTIES} subspecialties allowed.",
                "max_allowed": MAX_SUBSPECIALTIES,
            },
        )


async def _ensure_default_profile(user_id: str, flags: dict) -> None:
    """
    Migration: if no profiles exist, create one from the legacy preferences doc.
    Idempotent — safe to call repeatedly.
    
    Phase UX-D: Now copies ALL legacy preference fields into the default profile.
    PHI-Zero: no user text is logged.
    """
    count = await db.digest_profiles.count_documents(
        {"user_id": user_id, "deleted_at": None}
    )
    if count > 0:
        return  # already have profiles

    prefs = await db.preferences.find_one({"user_id": user_id}, {"_id": 0})
    if not prefs:
        return  # no legacy prefs to migrate from

    now = datetime.now(timezone.utc)
    schedule_dict = prefs.get("schedule", {})
    # Handle None values explicitly - use default if value is None or missing
    schedule = ProfileSchedule(
        frequency=schedule_dict.get("frequency") or "weekly",
        day_of_week=schedule_dict.get("day_of_week"),
        day_of_month=schedule_dict.get("day_of_month"),
        hour=schedule_dict.get("hour") if schedule_dict.get("hour") is not None else 9,
        minute=schedule_dict.get("minute") if schedule_dict.get("minute") is not None else 0,
        time_local=schedule_dict.get("time_local"),
        timezone=schedule_dict.get("timezone"),
    )
    next_run = compute_next_run(now, schedule_dict)

    # Build a descriptive default name from specialty/subspecialty IDs
    # (IDs, not user-entered text — PHI-Zero safe)
    spec = prefs.get("specialty_id", "")
    sub = prefs.get("subspecialty_id", "")
    name_parts = [p for p in [spec, sub] if p]
    default_name = " – ".join(name_parts) if name_parts else "My Digest"

    # Phase UX-D: Copy ALL legacy fields
    profile = {
        "profile_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": default_name,
        # Specialty/subspecialty
        "specialty_id": prefs.get("specialty_id", ""),
        "subspecialty_id": prefs.get("subspecialty_id"),
        "subspecialties": prefs.get("subspecialties", []),
        # Topics
        "topics_selected": prefs.get("topics_selected", []),
        "custom_topics": prefs.get("custom_topics", []),
        "custom_keywords": prefs.get("custom_topics", []),  # Alias for backward compat
        # Journals
        "journals_selected": prefs.get("journals_selected", []),
        "custom_journals": prefs.get("custom_journals", []),
        # Article limits
        "max_articles_per_digest": prefs.get("max_articles_per_digest", 10),
        # Schedule
        "digest_frequency": schedule.frequency,
        "delivery_time": f"{schedule.hour:02d}:{schedule.minute:02d}",
        "schedule_timezone": schedule.timezone or prefs.get("schedule", {}).get("timezone"),
        "next_run_timestamp": next_run.isoformat(),
        # Email controls
        "email_notifications_enabled": prefs.get("email_notifications_enabled", True),
        "email_suppress_until": prefs.get("email_suppress_until"),
        # Advanced preferences
        "advanced_preferences": prefs.get("advanced_preferences"),
        # Metadata
        "is_active": True,
        "deleted_at": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    await db.digest_profiles.insert_one(profile)
    logger.info("[PROFILES] Migrated default profile for user=%s", user_id)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

@router.get("/onboarding-status")
async def get_onboarding_status(current_user: dict = Depends(get_current_user)):
    """
    Phase UX-E: Check if user needs to complete onboarding wizard.
    
    Returns:
    - needs_onboarding: True if user has no preferences AND no digest profiles
    - has_legacy_preferences: True if user has legacy preferences
    - has_profiles: True if user has digest profiles
    - profile_count: Number of active digest profiles
    """
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    user_id = current_user["user_id"]
    
    # Check if onboarding wizard is enabled
    onboarding_enabled = flags.get("enable_onboarding_wizard_v2", False)
    
    # Check for legacy preferences
    legacy_prefs = await db.preferences.find_one({"user_id": user_id}, {"_id": 0, "specialty_id": 1})
    has_legacy = legacy_prefs is not None and bool(legacy_prefs.get("specialty_id"))
    
    # Check for digest profiles
    profile_count = await db.digest_profiles.count_documents({
        "user_id": user_id,
        "deleted_at": None,
    })
    has_profiles = profile_count > 0
    
    # User needs onboarding if they have neither legacy prefs nor profiles
    needs_onboarding = onboarding_enabled and not has_legacy and not has_profiles
    
    return {
        "needs_onboarding": needs_onboarding,
        "onboarding_enabled": onboarding_enabled,
        "has_legacy_preferences": has_legacy,
        "has_profiles": has_profiles,
        "profile_count": profile_count,
    }


@router.get("/profiles")
async def list_profiles(current_user: dict = Depends(get_current_user)):
    """List all active (non-deleted) digest profiles for the current user."""
    flags = _feature_check()
    user_id = current_user["user_id"]

    # Migrate legacy prefs on first access
    await _ensure_default_profile(user_id, flags)

    # Enforce profile limits (idempotent — freezes extras if over limit)
    await enforce_profile_limits(user_id, flags)

    profiles = await db.digest_profiles.find(
        {"user_id": user_id, "deleted_at": None},
        {"_id": 0},
    ).sort("created_at", 1).to_list(MAX_PROFILES_PREMIUM + 2)

    # Fetch user to compute limit
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    max_profiles = await _get_max_profiles_async(user or {}, user_id, flags)

    return {
        "profiles": [_clean(p) for p in profiles],
        "count": len(profiles),
        "max_profiles": max_profiles,
        "at_limit": len([p for p in profiles if p.get("is_active")]) >= max_profiles,
        "support_email": SUPPORT_EMAIL,
    }


@router.post("/profiles", status_code=status.HTTP_201_CREATED)
async def create_profile(
    data: ProfileCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a new digest profile."""
    flags = _feature_check()
    user_id = current_user["user_id"]

    # PHI-Zero: scan name and custom_keywords for protected health information
    from utils.phi_guard import enforce_phi_guard
    phi_mode = flags.get("phi_guard_mode", "block")
    phi_enabled = flags.get("enable_phi_guard", True)
    
    # Scan profile name
    enforce_phi_guard(
        text=data.name,
        endpoint="POST /api/preferences/profiles",
        user_id=user_id,
        mode=phi_mode,
        enabled=phi_enabled,
    )
    # Scan each custom keyword
    for kw in data.custom_keywords:
        enforce_phi_guard(
            text=kw,
            endpoint="POST /api/preferences/profiles",
            user_id=user_id,
            mode=phi_mode,
            enabled=phi_enabled,
        )

    # Check limit
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    # Stage 0c: Use async helper that honours the verified middle tier.
    # The previous sync _get_max_profiles() skipped the verified-clinician
    # check and always returned MAX_PROFILES_FREE for non-premium users,
    # incorrectly blocking verified clinicians from creating 2nd/3rd profiles.
    max_profiles = await _get_max_profiles_async(user or {}, user_id, flags)
    active_count = await db.digest_profiles.count_documents(
        {"user_id": user_id, "deleted_at": None}
    )
    if active_count >= max_profiles:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "profile_limit_reached",
                "message": (
                    f"You have reached the maximum of {max_profiles} digest profiles. "
                    f"Need more than {MAX_PROFILES_PREMIUM}? Email {SUPPORT_EMAIL}"
                ),
                "max_profiles": max_profiles,
                "support_email": SUPPORT_EMAIL,
            },
        )

    # Phase UX-C: validate community_subspecialty_ids
    if flags.get("enable_community_subspecialty_selection", False) and data.community_subspecialty_ids:
        _validate_community_subspecialties(
            data.community_subspecialty_ids,
            data.subspecialty_id,
            user or {},
            flags,
        )

    # Phase UX-E: Validate subspecialties limit (max 3)
    if data.subspecialties:
        _validate_subspecialties_limit(data.subspecialties)

    # Phase UX-E: Scan custom_topics and custom_journals for PHI
    for topic in data.custom_topics:
        enforce_phi_guard(
            text=topic,
            endpoint="POST /api/preferences/profiles",
            user_id=user_id,
            mode=phi_mode,
            enabled=phi_enabled,
        )
    for journal in data.custom_journals:
        enforce_phi_guard(
            text=journal,
            endpoint="POST /api/preferences/profiles",
            user_id=user_id,
            mode=phi_mode,
            enabled=phi_enabled,
        )

    now = datetime.now(timezone.utc)
    schedule_dict = {
        "frequency": data.schedule.frequency,
        "day_of_week": data.schedule.day_of_week,
        "day_of_month": data.schedule.day_of_month,
        "hour": data.schedule.hour,
        "minute": data.schedule.minute,
    }
    next_run = compute_next_run(now, schedule_dict)

    # Phase UX-E: Determine if this should be primary
    # First profile created is automatically primary
    is_primary = data.is_primary or (active_count == 0)

    # Phase UX-D: Build profile with all fields
    profile = {
        "profile_id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": data.name,
        # Specialty/subspecialty
        "specialty_id": data.specialty_id,
        "subspecialty_id": data.subspecialty_id,
        "subspecialties": data.subspecialties[:MAX_SUBSPECIALTIES] if data.subspecialties else [],
        # Keywords (backward compat alias for custom_topics)
        "custom_keywords": data.custom_keywords,
        # Community
        "community_subspecialty_ids": data.community_subspecialty_ids or [],
        # Phase UX-D: Topics
        "topics_selected": data.topics_selected or [],
        "custom_topics": data.custom_topics or [],
        # Phase UX-D: Journals
        "journals_selected": data.journals_selected or [],
        "custom_journals": data.custom_journals or [],
        # Phase UX-D: Article limits
        "max_articles_per_digest": data.max_articles_per_digest,
        # Schedule
        "digest_frequency": data.schedule.frequency,
        "delivery_time": f"{data.schedule.hour:02d}:{data.schedule.minute:02d}",
        "schedule_timezone": data.schedule.timezone,
        "day_of_week": data.schedule.day_of_week,
        "day_of_month": data.schedule.day_of_month,
        "next_run_timestamp": next_run.isoformat(),
        # Phase UX-D: Email controls
        "email_notifications_enabled": data.email_notifications_enabled,
        "email_suppress_until": data.email_suppress_until,
        # Phase UX-D: Advanced preferences
        "advanced_preferences": data.advanced_preferences.model_dump() if data.advanced_preferences else None,
        # Phase UX-E: Primary flag
        "is_primary": is_primary,
        # Metadata
        "is_active": True,
        "deleted_at": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    await db.digest_profiles.insert_one(profile)
    
    # Phase UX-E: Ensure only one primary profile
    if is_primary:
        await _ensure_single_primary(user_id, profile["profile_id"])
    
    # Phase UX-E: Dual-write to legacy preferences if enabled
    await _dual_write_to_legacy_preferences(profile, user_id, flags)
    
    # PHI-Zero: log only user_id + profile_id, not name/keywords
    logger.info("[PROFILES] Created profile_id=%s user=%s is_primary=%s", profile["profile_id"], user_id, is_primary)
    return _clean(profile)


@router.put("/profiles/{profile_id}")
async def update_profile(
    profile_id: str,
    data: ProfileUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update a digest profile."""
    flags = _feature_check()
    user_id = current_user["user_id"]

    # PHI-Zero: scan name and custom_keywords for protected health information
    from utils.phi_guard import enforce_phi_guard
    phi_mode = flags.get("phi_guard_mode", "block")
    phi_enabled = flags.get("enable_phi_guard", True)
    
    if data.name is not None:
        enforce_phi_guard(
            text=data.name,
            endpoint="PUT /api/preferences/profiles",
            user_id=user_id,
            mode=phi_mode,
            enabled=phi_enabled,
        )
    if data.custom_keywords is not None:
        for kw in data.custom_keywords:
            enforce_phi_guard(
                text=kw,
                endpoint="PUT /api/preferences/profiles",
                user_id=user_id,
                mode=phi_mode,
                enabled=phi_enabled,
            )

    existing = await db.digest_profiles.find_one(
        {"profile_id": profile_id, "user_id": user_id, "deleted_at": None},
        {"_id": 0},
    )
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    # Block edits to frozen profiles (except is_active toggle via set-active endpoint)
    if existing.get("frozen_reason") and not existing.get("is_active"):
        # Allow only is_primary and is_active changes on frozen profiles
        has_core_changes = any([
            data.name is not None, data.specialty_id is not None,
            data.topics_selected is not None, data.journals_selected is not None,
            data.schedule is not None, data.custom_keywords is not None,
            data.custom_topics is not None, data.custom_journals is not None,
        ])
        if has_core_changes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "profile_frozen",
                    "message": "This profile is frozen. Upgrade your plan or use 'Set Active' to choose which profiles to keep active.",
                },
            )

    now = datetime.now(timezone.utc)
    updates: dict = {"updated_at": now.isoformat()}

    if data.name is not None:
        updates["name"] = data.name
    if data.specialty_id is not None:
        updates["specialty_id"] = data.specialty_id
    if data.subspecialty_id is not None:
        updates["subspecialty_id"] = data.subspecialty_id
    if data.subspecialties is not None:
        updates["subspecialties"] = data.subspecialties
    if data.custom_keywords is not None:
        updates["custom_keywords"] = data.custom_keywords
    if data.is_active is not None:
        updates["is_active"] = data.is_active
    if data.schedule is not None:
        schedule_dict = {
            "frequency": data.schedule.frequency,
            "day_of_week": data.schedule.day_of_week,
            "day_of_month": data.schedule.day_of_month,
            "hour": data.schedule.hour,
            "minute": data.schedule.minute,
        }
        next_run = compute_next_run(now, schedule_dict)
        updates["digest_frequency"] = data.schedule.frequency
        updates["delivery_time"] = f"{data.schedule.hour:02d}:{data.schedule.minute:02d}"
        updates["next_run_timestamp"] = next_run.isoformat()
        if data.schedule.timezone is not None:
            updates["schedule_timezone"] = data.schedule.timezone

    # Phase UX-D: Topics and journals
    if data.topics_selected is not None:
        updates["topics_selected"] = data.topics_selected
    if data.custom_topics is not None:
        updates["custom_topics"] = data.custom_topics
    if data.journals_selected is not None:
        updates["journals_selected"] = data.journals_selected
    if data.custom_journals is not None:
        updates["custom_journals"] = data.custom_journals
    
    # Phase UX-D: Article limits
    if data.max_articles_per_digest is not None:
        updates["max_articles_per_digest"] = data.max_articles_per_digest
    
    # Phase UX-D: Email controls
    if data.email_notifications_enabled is not None:
        updates["email_notifications_enabled"] = data.email_notifications_enabled
    if data.email_suppress_until is not None:
        updates["email_suppress_until"] = data.email_suppress_until
    # Allow clearing suppress by setting to empty string
    if data.email_suppress_until == "":
        updates["email_suppress_until"] = None
    
    # Phase UX-D: Advanced preferences
    if data.advanced_preferences is not None:
        updates["advanced_preferences"] = data.advanced_preferences.model_dump()

    # Phase UX-C: validate and update community_subspecialty_ids
    if data.community_subspecialty_ids is not None:
        if flags.get("enable_community_subspecialty_selection", False):
            # Get the effective subspecialty_id (from update or existing)
            effective_subspecialty_id = data.subspecialty_id if data.subspecialty_id is not None else existing.get("subspecialty_id")
            user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
            _validate_community_subspecialties(
                data.community_subspecialty_ids,
                effective_subspecialty_id,
                user or {},
                flags,
            )
        updates["community_subspecialty_ids"] = data.community_subspecialty_ids

    # Phase UX-E: Validate subspecialties limit (max 3)
    if data.subspecialties is not None:
        _validate_subspecialties_limit(data.subspecialties)
        updates["subspecialties"] = data.subspecialties[:MAX_SUBSPECIALTIES]

    # Phase UX-E: PHI guard for custom_topics and custom_journals
    if data.custom_topics is not None:
        for topic in data.custom_topics:
            enforce_phi_guard(
                text=topic,
                endpoint="PUT /api/preferences/profiles",
                user_id=user_id,
                mode=phi_mode,
                enabled=phi_enabled,
            )
    if data.custom_journals is not None:
        for journal in data.custom_journals:
            enforce_phi_guard(
                text=journal,
                endpoint="PUT /api/preferences/profiles",
                user_id=user_id,
                mode=phi_mode,
                enabled=phi_enabled,
            )

    # Phase UX-E: Handle is_primary flag
    if data.is_primary is True:
        updates["is_primary"] = True

    updated = await db.digest_profiles.find_one_and_update(
        {"profile_id": profile_id, "user_id": user_id, "deleted_at": None},
        {"$set": updates},
        return_document=True,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    # Phase UX-E: Ensure only one primary profile
    if data.is_primary is True:
        await _ensure_single_primary(user_id, profile_id)

    # Phase UX-E: Dual-write to legacy preferences if this is the primary profile
    updated_dict = {k: v for k, v in updated.items() if k != "_id"}
    await _dual_write_to_legacy_preferences(updated_dict, user_id, flags)

    logger.info("[PROFILES] Updated profile_id=%s user=%s", profile_id, user_id)
    return _clean(updated)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_200_OK)
async def delete_profile(
    profile_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Soft-delete a digest profile and cascade to its digests.
    Cascaded digests are hidden from GET /api/digests (when flag ON).
    """
    _feature_check()
    user_id = current_user["user_id"]

    existing = await db.digest_profiles.find_one(
        {"profile_id": profile_id, "user_id": user_id, "deleted_at": None},
        {"_id": 0, "profile_id": 1},
    )
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    # Must keep at least one profile
    active_count = await db.digest_profiles.count_documents(
        {"user_id": user_id, "deleted_at": None}
    )
    if active_count <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "cannot_delete_last_profile",
                "message": "You must have at least one active digest profile.",
            },
        )

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Soft-delete profile
    await db.digest_profiles.update_one(
        {"profile_id": profile_id, "user_id": user_id},
        {"$set": {"deleted_at": now_iso, "updated_at": now_iso}},
    )

    # 2. Cascade: soft-delete digests generated by this profile
    result = await db.digests.update_many(
        {"user_id": user_id, "profile_id": profile_id, "deleted_at": None},
        {"$set": {"deleted_at": now_iso}},
    )
    logger.info(
        "[PROFILES] Deleted profile_id=%s user=%s cascaded_digests=%d",
        profile_id, user_id, result.modified_count,
    )
    return {"deleted": True, "digests_hidden": result.modified_count}


# ---------------------------------------------------------------------------
# Batch 2: Bulk set-active endpoint
# ---------------------------------------------------------------------------

class SetActiveRequest(BaseModel):
    active_profile_ids: List[str] = Field(..., min_length=1)


@router.post("/profiles/set-active")
async def set_active_profiles(
    data: SetActiveRequest,
    current_user: dict = Depends(get_current_user),
):
    """Bulk-set which profiles are active (within entitlement limit).
    
    Activates the specified profiles; deactivates all others with frozen_reason.
    Enforces max_profiles limit.
    """
    flags = _feature_check()
    user_id = current_user["user_id"]

    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    max_profiles = await _get_max_profiles_async(user, user_id, flags)

    if len(data.active_profile_ids) > max_profiles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "over_limit",
                "message": f"Cannot activate more than {max_profiles} profiles. You selected {len(data.active_profile_ids)}.",
                "max_profiles": max_profiles,
            },
        )

    # Verify all requested IDs belong to this user and are non-deleted
    owned = await db.digest_profiles.find(
        {"user_id": user_id, "deleted_at": None, "profile_id": {"$in": data.active_profile_ids}},
        {"_id": 0, "profile_id": 1},
    ).to_list(20)
    owned_ids = {p["profile_id"] for p in owned}
    invalid = set(data.active_profile_ids) - owned_ids
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "invalid_ids", "message": "Some profile IDs are invalid or deleted."},
        )

    from datetime import datetime, timezone as _tz
    now_iso = datetime.now(_tz.utc).isoformat()

    # Activate selected
    await db.digest_profiles.update_many(
        {"user_id": user_id, "profile_id": {"$in": data.active_profile_ids}},
        {"$set": {"is_active": True, "frozen_reason": None, "frozen_at": None, "updated_at": now_iso}},
    )

    # Deactivate all others (non-deleted)
    await db.digest_profiles.update_many(
        {"user_id": user_id, "deleted_at": None, "profile_id": {"$nin": data.active_profile_ids}},
        {"$set": {"is_active": False, "frozen_reason": "trial_expired", "frozen_at": now_iso, "updated_at": now_iso}},
    )

    logger.info("[PROFILES] set-active user=%s activated=%d max=%d", user_id, len(data.active_profile_ids), max_profiles)

    return {
        "activated": len(data.active_profile_ids),
        "max_profiles": max_profiles,
        "message": f"{len(data.active_profile_ids)} profile(s) activated.",
    }


@router.post("/profiles/{profile_id}/set-primary")
async def set_primary_profile(
    profile_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Set a profile as the primary digest profile.
    
    The primary profile is shown first in the UI and used for dual-write
    to legacy preferences collection.
    """
    _feature_check()
    user_id = current_user["user_id"]

    # Verify profile exists and belongs to user
    existing = await db.digest_profiles.find_one(
        {"profile_id": profile_id, "user_id": user_id, "deleted_at": None},
        {"_id": 0, "profile_id": 1, "is_primary": 1},
    )
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    # If already primary, return early
    if existing.get("is_primary"):
        return {"message": "Profile is already primary", "profile_id": profile_id}

    now_iso = datetime.now(timezone.utc).isoformat()

    # Set this profile as primary
    await db.digest_profiles.update_one(
        {"profile_id": profile_id, "user_id": user_id},
        {"$set": {"is_primary": True, "updated_at": now_iso}},
    )

    # Unset primary on all other profiles
    await _ensure_single_primary(user_id, profile_id)

    logger.info("[PROFILES] Set primary profile_id=%s user=%s", profile_id, user_id)
    return {"message": "Profile set as primary", "profile_id": profile_id}


@router.get("/profiles/screen-stats")
async def get_profiles_screen_stats(current_user: dict = Depends(get_current_user)):
    """Get digest profile stats for the LitScreen page.
    
    Returns profiles with:
    - Specialty/subspecialty labels
    - Last digest date
    - Unscreened count
    - Saved percentage
    - Primary flag
    
    Sorted: Primary first, then by last digest date descending.
    """
    flags = _feature_check()
    user_id = current_user["user_id"]

    # Migrate legacy prefs on first access
    await _ensure_default_profile(user_id, flags)

    # Get active profiles
    profiles = await db.digest_profiles.find(
        {"user_id": user_id, "deleted_at": None, "is_active": True},
        {"_id": 0},
    ).to_list(10)

    if not profiles:
        return {"profiles": [], "total_unscreened": 0}

    # Get specialty config for labels
    specialty_config = await db.specialty_config.find_one({}) or {}
    specialties_map = {}
    subspecialties_map = {}
    for spec in specialty_config.get("specialties", []):
        specialties_map[spec.get("id")] = spec.get("label", spec.get("id"))
        for sub in spec.get("subspecialties", []):
            subspecialties_map[sub.get("id")] = sub.get("label", sub.get("id"))

    # Get screening stats for each profile
    profile_stats = []
    total_unscreened = 0

    for profile in profiles:
        profile_id = profile.get("profile_id")
        specialty_id = profile.get("specialty_id", "")
        subspecialty_id = profile.get("subspecialty_id", "")

        # Get latest digest for this profile
        latest_digest = await db.digests.find_one(
            {"user_id": user_id, "profile_id": profile_id, "deleted_at": None},
            {"_id": 0, "digest_id": 1, "generated_at": 1, "article_count": 1},
            sort=[("generated_at", -1)],
        )

        # Count unscreened articles from this profile's latest digest
        unscreened_count = 0
        saved_count = 0
        total_articles = 0

        if latest_digest:
            # Get the latest digest's article PMIDs
            full_digest = await db.digests.find_one(
                {"digest_id": latest_digest["digest_id"]},
                {"_id": 0, "article_pmids": 1, "articles": 1},
            )
            article_pmids = (full_digest or {}).get("article_pmids", [])
            
            # Fallback: if no article_pmids, try to resolve from articles field
            if not article_pmids:
                article_ids = (full_digest or {}).get("articles", [])
                if article_ids:
                    from bson import ObjectId
                    for aid in article_ids:
                        try:
                            art = await db.articles.find_one({"_id": ObjectId(aid)}, {"_id": 0, "pmid": 1})
                            if art and art.get("pmid"):
                                article_pmids.append(art["pmid"])
                        except Exception:
                            article_pmids.append(aid)
            
            total_articles = len(article_pmids)
            
            if article_pmids:
                # Check screening decisions from article_screening collection
                screening_cursor = db.article_screening.find(
                    {"user_id": user_id, "article_id": {"$in": article_pmids}},
                    {"_id": 0, "article_id": 1, "decision": 1},
                )
                screened_decisions = {}
                async for doc in screening_cursor:
                    screened_decisions[doc.get("article_id")] = doc.get("decision")
                
                screened_count = len(screened_decisions)
                unscreened_count = total_articles - screened_count
                saved_count = sum(1 for d in screened_decisions.values() if d == "keep")

        total_unscreened += unscreened_count

        # Calculate saved percentage
        screened = total_articles - unscreened_count
        saved_percent = round((saved_count / screened * 100) if screened > 0 else 0)

        profile_stats.append({
            "profile_id": profile_id,
            "name": profile.get("name", ""),
            "specialty_id": specialty_id,
            "specialty_label": specialties_map.get(specialty_id, specialty_id.replace("_", " ").title()),
            "subspecialty_id": subspecialty_id,
            "subspecialty_label": subspecialties_map.get(subspecialty_id, subspecialty_id.replace("_", " ").title() if subspecialty_id else ""),
            "is_primary": profile.get("is_primary", False),
            "last_digest_date": latest_digest.get("generated_at") if latest_digest else None,
            "last_digest_id": latest_digest.get("digest_id") if latest_digest else None,
            "unscreened_count": unscreened_count,
            "total_articles": total_articles,
            "saved_percent": saved_percent,
        })

    # Sort: primary first, then by last_digest_date descending
    profile_stats.sort(key=lambda p: (
        not p.get("is_primary", False),  # Primary first (False < True, so negate)
        -(datetime.fromisoformat(p["last_digest_date"]).timestamp() if p.get("last_digest_date") else 0),
    ))

    return {
        "profiles": profile_stats,
        "total_unscreened": total_unscreened,
    }



# ---------------------------------------------------------------------------
# Run Digest Now
# ---------------------------------------------------------------------------

class RunDigestResponse(BaseModel):
    message: str
    profile_id: str
    status: str

@router.post("/profiles/{profile_id}/run-digest")
async def run_digest_now(
    profile_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """
    Manually trigger a digest run for the specified profile.
    Actually runs the digest generation pipeline in a background task.
    """
    _feature_check()
    user_id = current_user["user_id"]

    # Verify profile exists and belongs to user
    profile = await db.digest_profiles.find_one(
        {"profile_id": profile_id, "user_id": user_id, "deleted_at": None},
        {"_id": 0},
    )
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    # Check if profile is active
    if not profile.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot run digest for inactive profile"
        )

    # Update timestamps
    now = datetime.now(timezone.utc)
    await db.digest_profiles.update_one(
        {"profile_id": profile_id},
        {
            "$set": {
                "next_run_timestamp": now.isoformat(),
                "manual_run_requested": True,
                "manual_run_requested_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
        },
    )

    # Actually trigger digest generation in background
    async def _run_digest_background():
        try:
            from digest_orchestrator import DigestOrchestrator
            orchestrator = DigestOrchestrator(db)
            result = await orchestrator.generate_digest_for_profile(
                user_id=user_id,
                profile=profile,
                send_email=True,
            )
            logger.info("[PROFILES] Background digest completed profile_id=%s result=%s", profile_id, result)
            # Clear manual run flag
            await db.digest_profiles.update_one(
                {"profile_id": profile_id},
                {"$set": {"manual_run_requested": False}},
            )
        except Exception as e:
            logger.error("[PROFILES] Background digest failed profile_id=%s error=%s", profile_id, str(e))

    background_tasks.add_task(_run_digest_background)

    logger.info("[PROFILES] Manual digest run requested profile_id=%s user=%s", profile_id, user_id)
    
    return RunDigestResponse(
        message="Digest run started. New articles will appear shortly.",
        profile_id=profile_id,
        status="queued"
    )


# ---------------------------------------------------------------------------
# Suppress Digest
# ---------------------------------------------------------------------------

class SuppressRequest(BaseModel):
    value: int = Field(..., ge=1, le=10, description="Number of time units")
    unit: str = Field(..., description="Time unit: days, weeks, or months")

class SuppressResponse(BaseModel):
    message: str
    profile_id: str
    suppressed_until: str

@router.post("/profiles/{profile_id}/suppress")
async def suppress_digest(
    profile_id: str,
    data: SuppressRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Suppress (pause) digest generation for a specified duration.
    The profile will not generate new digests until the suppression period ends.
    """
    _feature_check()
    user_id = current_user["user_id"]

    # Validate unit
    valid_units = ["days", "weeks", "months"]
    if data.unit not in valid_units:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid unit. Must be one of: {', '.join(valid_units)}"
        )

    # Verify profile exists and belongs to user
    profile = await db.digest_profiles.find_one(
        {"profile_id": profile_id, "user_id": user_id, "deleted_at": None},
        {"_id": 0},
    )
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    # Calculate suppression end date
    now = datetime.now(timezone.utc)
    if data.unit == "days":
        suppress_until = now + timedelta(days=data.value)
    elif data.unit == "weeks":
        suppress_until = now + timedelta(weeks=data.value)
    else:  # months
        # Approximate months as 30 days
        suppress_until = now + timedelta(days=data.value * 30)

    # Update profile with suppression
    await db.digest_profiles.update_one(
        {"profile_id": profile_id},
        {
            "$set": {
                "email_suppress_until": suppress_until.isoformat(),
                "digest_suppressed_until": suppress_until.isoformat(),
                "updated_at": now.isoformat(),
            }
        },
    )

    logger.info(
        "[PROFILES] Digest suppressed profile_id=%s user=%s until=%s",
        profile_id, user_id, suppress_until.isoformat()
    )
    
    return SuppressResponse(
        message=f"Digest suppressed for {data.value} {data.unit}",
        profile_id=profile_id,
        suppressed_until=suppress_until.isoformat()
    )
