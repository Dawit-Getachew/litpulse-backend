"""
Multi-Digest Profile Migration Helper — Phase 7.1 (Part C)

Provides safe, idempotent migration from legacy preferences to digest_profiles.
Called by:
- Scheduler (when running profile-based digests)
- Community eligibility checks (get_user_eligible_specialties)
- Any endpoint that needs to ensure profiles exist

PHI-Zero: never logs user-entered text (names, keywords).
"""
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


async def ensure_user_has_profile(
    db: AsyncIOMotorDatabase,
    user_id: str,
    flags: Optional[dict] = None,
) -> bool:
    """
    Ensure user has at least one active digest_profile.
    
    If ENABLE_MULTI_DIGEST_PROFILES is ON and user has legacy preferences 
    but no active digest_profiles, automatically creates a default profile 
    from the legacy preferences.
    
    This is called transparently by:
    - Scheduler before running profile digests
    - get_user_eligible_specialties before checking community access
    
    Returns True if a profile was created, False if not needed or not possible.
    
    PHI-Zero: only logs user_id and profile_id, never names/keywords.
    """
    if flags is None:
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
    
    # Only needed when multi-digest profiles feature is ON
    if not flags.get("enable_multi_digest_profiles", False):
        return False
    
    # Check if user already has active profiles
    existing_count = await db.digest_profiles.count_documents({
        "user_id": user_id,
        "is_active": True,
        "deleted_at": None,
    })
    
    if existing_count > 0:
        return False  # Already has profiles, no migration needed
    
    # Try to get legacy preferences
    prefs = await db.preferences.find_one({"user_id": user_id}, {"_id": 0})
    if not prefs:
        return False  # No legacy preferences to migrate
    
    # Check we have minimum required fields
    specialty_id = prefs.get("specialty_id")
    if not specialty_id:
        return False  # Can't create profile without specialty
    
    # Import date utility for schedule calculation
    from date_utils import compute_next_run
    
    now = datetime.now(timezone.utc)
    schedule_dict = prefs.get("schedule", {})
    
    # Build schedule from legacy prefs
    frequency = schedule_dict.get("frequency", "weekly")
    hour = schedule_dict.get("hour") or 9
    minute = schedule_dict.get("minute") or 0
    
    next_run = compute_next_run(now, schedule_dict)
    
    # Build a descriptive default name from specialty/subspecialty IDs
    # (IDs only — PHI-Zero safe)
    subspecialty = prefs.get("subspecialty_id", "")
    name_parts = [specialty_id]
    if subspecialty:
        name_parts.append(subspecialty)
    default_name = " – ".join(name_parts) if name_parts else "My Digest"
    
    profile_id = str(uuid.uuid4())
    
    profile = {
        "profile_id": profile_id,
        "user_id": user_id,
        "name": default_name,
        "specialty_id": specialty_id,
        "subspecialty_id": subspecialty or None,
        "custom_keywords": prefs.get("custom_topics", []),
        "digest_frequency": frequency,
        "delivery_time": f"{hour:02d}:{minute:02d}",
        "next_run_timestamp": next_run.isoformat(),
        "is_active": True,
        "deleted_at": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "_migrated_from_legacy": True,  # Mark as auto-migrated
    }
    
    try:
        await db.digest_profiles.insert_one(profile)
        logger.info(
            "[MIGRATION] Auto-created default profile_id=%s for user=%s from legacy preferences",
            profile_id, user_id
        )
        return True
    except Exception as e:
        # Could be duplicate key if concurrent request also migrated
        # This is safe to ignore
        logger.warning(
            "[MIGRATION] Failed to create profile for user=%s: %s",
            user_id, type(e).__name__
        )
        return False


async def ensure_profiles_for_scheduler(
    db: AsyncIOMotorDatabase,
    flags: Optional[dict] = None,
) -> int:
    """
    Batch migration helper for scheduler startup.
    
    Ensures all users with legacy preferences but no profiles get migrated.
    Called once at scheduler initialization when ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER is enabled.
    
    Returns count of profiles created.
    """
    if flags is None:
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
    
    if not flags.get("enable_multi_digest_profiles", False):
        return 0
    
    # Find users with preferences but no profiles
    # This is a set difference operation
    users_with_prefs = await db.preferences.distinct("user_id", {"is_active": True})
    users_with_profiles = await db.digest_profiles.distinct("user_id", {"deleted_at": None})
    
    users_needing_migration = set(users_with_prefs) - set(users_with_profiles)
    
    if not users_needing_migration:
        logger.info("[MIGRATION] No users need profile migration")
        return 0
    
    logger.info("[MIGRATION] Found %d users needing profile migration", len(users_needing_migration))
    
    created_count = 0
    for user_id in users_needing_migration:
        if await ensure_user_has_profile(db, user_id, flags):
            created_count += 1
    
    logger.info("[MIGRATION] Completed migration: %d profiles created", created_count)
    return created_count
