"""
Beta Admin Routes — Founder dashboard for invite-only beta.

Endpoints:
  POST /api/beta-admin/invite          — Create invite code
  POST /api/beta-admin/activate        — Move user to active_beta
  POST /api/beta-admin/pause           — Pause a beta user
  POST /api/beta-admin/remove          — Remove a beta user
  GET  /api/beta-admin/users           — List all beta users
  GET  /api/beta-admin/dashboard       — Founder overview dashboard
  GET  /api/beta-admin/funnel          — Activation funnel metrics
  GET  /api/beta-admin/feature-usage   — Feature usage metrics
  GET  /api/beta-admin/reliability     — System reliability metrics
  GET  /api/beta-admin/user/{user_id}  — User drill-down
  GET  /api/beta-admin/ai-health       — AI provider health checks
"""
from fastapi import APIRouter, HTTPException, Depends, status, Query
from pydantic import BaseModel, Field
from datetime import datetime, timezone, timedelta
from typing import Optional, List
import os
import uuid
import secrets
import logging
import time

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/beta-admin", tags=["beta-admin"])

db = None
_admin_email = ""


def set_db(database):
    global db
    db = database


def set_admin_email(email: str):
    global _admin_email
    _admin_email = email.lower()


async def _verify_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not _admin_email:
        raise HTTPException(status_code=403, detail="Admin not configured")
    user = await db.users.find_one({"user_id": current_user["user_id"]}, {"_id": 0, "email": 1})
    if user and user.get("email", "").lower() == _admin_email:
        return current_user
    raise HTTPException(status_code=403, detail="Admin access required")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class InviteRequest(BaseModel):
    email: Optional[str] = None
    count: int = Field(default=1, ge=1, le=20)

class UserActionRequest(BaseModel):
    user_id: Optional[str] = None
    email: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# POST /api/beta-admin/invite
# ---------------------------------------------------------------------------

@router.post("/invite")
async def create_invites(data: InviteRequest, admin: dict = Depends(_verify_admin)):
    """Create invite codes. Optionally pre-assign to an email."""
    codes = []
    now = datetime.now(timezone.utc).isoformat()
    for _ in range(data.count):
        code = secrets.token_urlsafe(8)
        doc = {
            "invite_code": code,
            "email": data.email.lower() if data.email else None,
            "created_at": now,
            "created_by": admin["user_id"],
            "used": False,
            "used_by": None,
            "used_at": None,
        }
        await db.beta_invites.insert_one(doc)
        codes.append(code)
    return {"codes": codes, "count": len(codes)}



# ---------------------------------------------------------------------------
# GET /api/beta-admin/invites — List all invite codes with status
# ---------------------------------------------------------------------------

@router.get("/invites")
async def list_invites(admin: dict = Depends(_verify_admin)):
    """List all invite codes with their current status."""
    invites_cursor = db.beta_invites.find(
        {},
        {"_id": 0}
    ).sort("created_at", -1)
    
    invites = await invites_cursor.to_list(200)
    
    result = []
    for inv in invites:
        # Determine display status
        if inv.get("used", False):
            status = "used"
        else:
            status = "active"
        
        result.append({
            "invite_code": inv.get("invite_code", ""),
            "email": inv.get("email"),
            "status": status,
            "used": inv.get("used", False),
            "used_by": inv.get("used_by"),
            "used_at": inv.get("used_at"),
            "created_at": inv.get("created_at"),
            "created_by": inv.get("created_by"),
        })
    
    return {"invites": result, "total": len(result)}


# ---------------------------------------------------------------------------
# POST /api/beta-admin/activate
# ---------------------------------------------------------------------------

@router.post("/activate")
async def activate_user(data: UserActionRequest, admin: dict = Depends(_verify_admin)):
    """Move a user from waitlist/invited/paused to active_beta."""
    query = {}
    if data.user_id:
        query["user_id"] = data.user_id
    elif data.email:
        query["email"] = data.email.lower()
    else:
        raise HTTPException(status_code=400, detail="Provide user_id or email")

    result = await db.users.update_one(
        query,
        {"$set": {"beta_status": "active_beta", "beta_activated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")

    # Track event
    from utils.event_tracker import track_event
    uid = data.user_id or ""
    await track_event("beta_activated", uid, {"by": admin["user_id"]})

    return {"status": "active_beta", "message": "User activated for beta."}


# ---------------------------------------------------------------------------
# POST /api/beta-admin/pause
# ---------------------------------------------------------------------------

@router.post("/pause")
async def pause_user(data: UserActionRequest, admin: dict = Depends(_verify_admin)):
    """Pause a beta user's access."""
    query = {}
    if data.user_id:
        query["user_id"] = data.user_id
    elif data.email:
        query["email"] = data.email.lower()
    else:
        raise HTTPException(status_code=400, detail="Provide user_id or email")

    result = await db.users.update_one(
        query,
        {"$set": {"beta_status": "paused", "beta_paused_at": datetime.now(timezone.utc).isoformat(), "beta_pause_reason": data.reason or ""}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "paused", "message": "User paused."}


# ---------------------------------------------------------------------------
# POST /api/beta-admin/remove
# ---------------------------------------------------------------------------

@router.post("/remove")
async def remove_user(data: UserActionRequest, admin: dict = Depends(_verify_admin)):
    """Remove a user from beta."""
    query = {}
    if data.user_id:
        query["user_id"] = data.user_id
    elif data.email:
        query["email"] = data.email.lower()
    else:
        raise HTTPException(status_code=400, detail="Provide user_id or email")

    result = await db.users.update_one(
        query,
        {"$set": {"beta_status": "removed", "beta_removed_at": datetime.now(timezone.utc).isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "removed", "message": "User removed from beta."}


# ---------------------------------------------------------------------------
# GET /api/beta-admin/users
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_beta_users(
    beta_status: Optional[str] = Query(None),
    admin: dict = Depends(_verify_admin),
):
    """List beta users with optional status filter."""
    query = {}
    if beta_status:
        query["beta_status"] = beta_status
    users = await db.users.find(
        query,
        {"_id": 0, "hashed_password": 0, "password_hash": 0},
    ).sort("created_at", -1).to_list(200)
    return {"users": users, "total": len(users)}


# ---------------------------------------------------------------------------
# GET /api/beta-admin/dashboard — Overview metrics
# ---------------------------------------------------------------------------

@router.get("/dashboard")
async def get_dashboard(admin: dict = Depends(_verify_admin)):
    """Founder overview dashboard."""
    now = datetime.now(timezone.utc)
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()

    total_users = await db.users.count_documents({})
    invited = await db.beta_invites.count_documents({"used": False})
    active_beta = await db.users.count_documents({"beta_status": "active_beta"})
    waitlist = await db.users.count_documents({"beta_status": "waitlist"})
    paused = await db.users.count_documents({"beta_status": "paused"})
    verified_email = await db.users.count_documents({"is_verified": True})
    verified_clinician = await db.professional_verifications.count_documents(
        {"status": {"$in": ["verified", "verified_provisional"]}}
    )

    # Active users (logged in last 7 days)
    active_7d = await db.analytics_events.count_documents(
        {"event_type": "login", "created_at": {"$gte": seven_days_ago}}
    )
    # Engaged users (any non-login event in last 14 days)
    engaged_14d_pipeline = [
        {"$match": {"event_type": {"$ne": "login"}, "created_at": {"$gte": fourteen_days_ago}}},
        {"$group": {"_id": "$user_id"}},
        {"$count": "count"},
    ]
    engaged_result = await db.analytics_events.aggregate(engaged_14d_pipeline).to_list(1)
    engaged_14d = engaged_result[0]["count"] if engaged_result else 0

    from utils.beta_gate import get_active_capacity, get_waitlist_capacity
    return {
        "total_users": total_users,
        "pending_invites": invited,
        "active_beta": active_beta,
        "active_capacity": get_active_capacity(),
        "waitlist": waitlist,
        "waitlist_capacity": get_waitlist_capacity(),
        "paused": paused,
        "verified_email": verified_email,
        "verified_clinician": verified_clinician,
        "active_7d": active_7d,
        "engaged_14d": engaged_14d,
    }


# ---------------------------------------------------------------------------
# GET /api/beta-admin/funnel — Activation funnel
# ---------------------------------------------------------------------------

@router.get("/funnel")
async def get_funnel(admin: dict = Depends(_verify_admin)):
    """Activation funnel metrics."""
    async def _count_event(event_type: str) -> int:
        pipeline = [
            {"$match": {"event_type": event_type}},
            {"$group": {"_id": "$user_id"}},
            {"$count": "count"},
        ]
        result = await db.analytics_events.aggregate(pipeline).to_list(1)
        return result[0]["count"] if result else 0

    total_signups = await db.users.count_documents({})
    email_verified = await db.users.count_documents({"is_verified": True})
    work_email_verified = await db.professional_verifications.count_documents({"status": "verified"})
    has_preferences = await db.preferences.count_documents({})
    has_profiles = await db.digest_profiles.count_documents({"deleted_at": None})

    first_digest = await _count_event("digest_generated")
    first_digest_opened = await _count_event("digest_opened")
    first_article_saved = await _count_event("article_saved")
    first_audio_played = await _count_event("audio_played")
    first_deepdive = await _count_event("deepdive_request")
    first_community_post = await _count_event("community_post")

    return {
        "signup_completed": total_signups,
        "email_verified": email_verified,
        "work_email_verified": work_email_verified,
        "onboarding_completed": has_preferences + has_profiles,
        "first_digest_generated": first_digest,
        "first_digest_opened": first_digest_opened,
        "first_article_saved": first_article_saved,
        "first_audio_played": first_audio_played,
        "first_deepdive_used": first_deepdive,
        "first_community_post": first_community_post,
    }


# ---------------------------------------------------------------------------
# GET /api/beta-admin/feature-usage
# ---------------------------------------------------------------------------

@router.get("/feature-usage")
async def get_feature_usage(admin: dict = Depends(_verify_admin)):
    """Feature usage counts."""
    async def _count(event_type: str) -> int:
        return await db.analytics_events.count_documents({"event_type": event_type})

    return {
        "digests_generated": await _count("digest_generated"),
        "digests_opened": await _count("digest_opened"),
        "articles_saved": await _count("article_saved"),
        "audio_generated": await _count("audio_generated"),
        "audio_played": await _count("audio_played"),
        "summary_requests": await _count("summary_request"),
        "deepdive_sessions": await _count("deepdive_request"),
        "community_posts": await _count("community_post"),
        "community_comments": await _count("community_comment"),
        "community_reactions": await _count("community_reaction"),
    }


# ---------------------------------------------------------------------------
# GET /api/beta-admin/reliability
# ---------------------------------------------------------------------------

@router.get("/reliability")
async def get_reliability(admin: dict = Depends(_verify_admin)):
    """System reliability metrics."""
    # Scheduler status
    scheduler_status = None
    try:
        lock = await db.scheduler_lock.find_one({}, {"_id": 0})
        scheduler_status = lock
    except Exception:
        pass

    # Recent errors from analytics
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(hours=24)).isoformat()

    audio_success = await db.analytics_events.count_documents({"event_type": "audio_generated", "created_at": {"$gte": day_ago}})
    audio_fail = await db.analytics_events.count_documents({"event_type": "audio_generation_failed", "created_at": {"$gte": day_ago}})
    summary_success = await db.analytics_events.count_documents({"event_type": "summary_request", "created_at": {"$gte": day_ago}})
    summary_fail = await db.analytics_events.count_documents({"event_type": "summary_failed", "created_at": {"$gte": day_ago}})
    deepdive_success = await db.analytics_events.count_documents({"event_type": "deepdive_request", "created_at": {"$gte": day_ago}})
    deepdive_fail = await db.analytics_events.count_documents({"event_type": "deepdive_failed", "created_at": {"$gte": day_ago}})
    phi_triggers = await db.analytics_events.count_documents({"event_type": "phi_guard_trigger", "created_at": {"$gte": day_ago}})

    # Audio storage check
    audio_backend = os.environ.get("AUDIO_STORAGE_BACKEND", "local")

    return {
        "scheduler": scheduler_status,
        "audio_storage_backend": audio_backend,
        "last_24h": {
            "audio_success": audio_success,
            "audio_failures": audio_fail,
            "summary_success": summary_success,
            "summary_failures": summary_fail,
            "deepdive_success": deepdive_success,
            "deepdive_failures": deepdive_fail,
            "phi_guard_triggers": phi_triggers,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/beta-admin/user/{user_id} — User drill-down
# ---------------------------------------------------------------------------

@router.get("/user/{user_id}")
async def get_user_detail(user_id: str, admin: dict = Depends(_verify_admin)):
    """Detailed view of a single beta user."""
    user = await db.users.find_one(
        {"user_id": user_id},
        {"_id": 0, "hashed_password": 0, "password_hash": 0},
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    verification = await db.professional_verifications.find_one(
        {"user_id": user_id}, {"_id": 0, "status": 1, "method": 1, "verified_at": 1}
    )
    profile_count = await db.digest_profiles.count_documents({"user_id": user_id, "deleted_at": None})
    digest_count = await db.digests.count_documents({"user_id": user_id})
    library_count = await db.user_articles.count_documents({"user_id": user_id, "saved_to_library": True})

    # Event counts
    async def _user_event_count(event_type: str) -> int:
        return await db.analytics_events.count_documents({"user_id": user_id, "event_type": event_type})

    last_login_event = await db.analytics_events.find_one(
        {"user_id": user_id, "event_type": "login"},
        {"_id": 0, "created_at": 1},
        sort=[("created_at", -1)],
    )

    return {
        "user": user,
        "verification": verification,
        "profile_count": profile_count,
        "digest_count": digest_count,
        "library_count": library_count,
        "last_login": last_login_event.get("created_at") if last_login_event else None,
        "audio_uses": await _user_event_count("audio_generated"),
        "deepdive_uses": await _user_event_count("deepdive_request"),
        "summary_uses": await _user_event_count("summary_request"),
        "community_posts": await _user_event_count("community_post"),
        "community_comments": await _user_event_count("community_comment"),
        # Practice profile drill-down
        "practice_profile": user.get("practice_profile"),
        "practice_profile_complete": bool(user.get("practice_profile")),
        "practice_field_count": len(user.get("practice_profile", {}) or {}),
        "personalization_signals": _get_personalization_signals(user.get("practice_profile")),
    }


def _get_personalization_signals(profile: Optional[dict]) -> dict:
    """Return which personalization signals are active for a user."""
    if not profile:
        return {"active": False, "signals": []}
    signals = []
    if profile.get("primary_specialty"):
        signals.append({"field": "primary_specialty", "weight": "moderate", "value": profile["primary_specialty"]})
    if profile.get("subspecialties"):
        signals.append({"field": "subspecialties", "weight": "secondary", "value": profile["subspecialties"]})
    if profile.get("practice_setting"):
        signals.append({"field": "practice_setting", "weight": "light", "value": profile["practice_setting"]})
    if profile.get("clinical_environment"):
        signals.append({"field": "clinical_environment", "weight": "light", "value": profile["clinical_environment"]})
    return {"active": len(signals) > 0, "signals": signals}


# ---------------------------------------------------------------------------
# GET /api/beta-admin/demographics — Practice demographics
# ---------------------------------------------------------------------------

@router.get("/demographics")
async def get_demographics(admin: dict = Depends(_verify_admin)):
    """Aggregate practice profile demographics for founder dashboard."""
    all_users = await db.users.find(
        {},
        {"_id": 0, "user_id": 1, "practice_profile": 1},
    ).to_list(500)

    total = len(all_users)
    with_profile = [u for u in all_users if u.get("practice_profile")]
    completion_rate = round(len(with_profile) / total * 100) if total > 0 else 0

    # Aggregate distributions
    from collections import Counter
    specialty_dist = Counter()
    subspecialty_dist = Counter()
    stage_dist = Counter()
    years_dist = Counter()
    country_dist = Counter()
    state_dist = Counter()
    setting_dist = Counter()
    env_dist = Counter()

    for u in with_profile:
        p = u["practice_profile"]
        if p.get("primary_specialty"):
            specialty_dist[p["primary_specialty"]] += 1
        if p.get("specialty_2"):
            specialty_dist[p["specialty_2"]] += 1
        for sub in (p.get("subspecialties") or []):
            if sub:
                subspecialty_dist[sub] += 1
        if p.get("current_stage"):
            stage_dist[p["current_stage"]] += 1
        if p.get("years_in_practice"):
            years_dist[p["years_in_practice"]] += 1
        if p.get("country"):
            country_dist[p["country"]] += 1
        if p.get("state_province"):
            state_dist[p["state_province"]] += 1
        if p.get("practice_setting"):
            setting_dist[p["practice_setting"]] += 1
        if p.get("clinical_environment"):
            env_dist[p["clinical_environment"]] += 1

    def _top(counter, n=10):
        return [{"value": k, "count": v} for k, v in counter.most_common(n)]

    # Engagement by segment (specialty)
    engagement_by_specialty = []
    for spec, count in specialty_dist.most_common(10):
        spec_user_ids = [
            u["user_id"] for u in with_profile
            if (u["practice_profile"].get("primary_specialty") == spec or
                u["practice_profile"].get("specialty_2") == spec)
        ]
        if spec_user_ids:
            events = await db.analytics_events.count_documents(
                {"user_id": {"$in": spec_user_ids}, "event_type": {"$ne": "login"}}
            )
            engagement_by_specialty.append({"specialty": spec, "users": count, "total_events": events})

    return {
        "total_users": total,
        "profiles_completed": len(with_profile),
        "completion_rate_pct": completion_rate,
        "specialty_distribution": _top(specialty_dist),
        "top_subspecialties": _top(subspecialty_dist),
        "stage_distribution": _top(stage_dist),
        "years_distribution": _top(years_dist),
        "country_distribution": _top(country_dist),
        "state_distribution": _top(state_dist, 15),
        "setting_distribution": _top(setting_dist),
        "environment_distribution": _top(env_dist),
        "engagement_by_specialty": engagement_by_specialty,
    }


# ---------------------------------------------------------------------------
# GET /api/beta-admin/ai-health — AI provider health checks
# ---------------------------------------------------------------------------

@router.get("/ai-health")
async def ai_health_check(admin: dict = Depends(_verify_admin)):
    """Check connectivity of all AI providers. Uses 60s TTL cache."""
    from utils.health_cache import get_cached, set_cached

    CACHE_KEY = "beta_admin_ai_health"

    # Return cached result if available
    cached = get_cached(CACHE_KEY)
    if cached is not None:
        return cached

    results = {}

    # Summary provider
    summary_provider_name = os.environ.get("SUMMARY_PROVIDER", "mock")
    summary_model = os.environ.get("SUMMARY_MODEL", "gpt-5-mini")
    results["summary"] = {"provider": summary_provider_name, "model": summary_model, "reachable": False, "latency_ms": None, "error": None}
    try:
        from utils.ai_providers import get_summary_provider
        import asyncio as _asyncio
        provider = get_summary_provider()
        t0 = time.perf_counter()
        resp = await _asyncio.wait_for(
            provider.generate("Respond with exactly: OK", "You are a test assistant."),
            timeout=5.0
        )
        latency = (time.perf_counter() - t0) * 1000
        results["summary"]["reachable"] = bool(resp)
        results["summary"]["latency_ms"] = round(latency)
    except Exception as e:
        results["summary"]["error"] = "timeout" if "TimeoutError" in type(e).__name__ else type(e).__name__

    # Deep Dive provider
    dd_provider_name = os.environ.get("DEEPDIVE_PROVIDER", "mock")
    dd_model = os.environ.get("DEEPDIVE_MODEL", "gpt-5.2")
    results["deepdive"] = {"provider": dd_provider_name, "model": dd_model, "reachable": False, "latency_ms": None, "error": None}
    try:
        from utils.ai_providers import get_deepdive_provider
        import asyncio as _asyncio
        provider = get_deepdive_provider()
        t0 = time.perf_counter()
        resp = await _asyncio.wait_for(
            provider.generate("Respond with exactly: OK", "You are a test assistant."),
            timeout=5.0
        )
        latency = (time.perf_counter() - t0) * 1000
        results["deepdive"]["reachable"] = bool(resp)
        results["deepdive"]["latency_ms"] = round(latency)
    except Exception as e:
        results["deepdive"]["error"] = "timeout" if "TimeoutError" in type(e).__name__ else type(e).__name__

    # TTS provider
    tts_provider = os.environ.get("AUDIO_TTS_PROVIDER", "mock")
    results["tts"] = {"provider": tts_provider, "configured": tts_provider != "mock"}

    # S3 storage
    s3_bucket = os.environ.get("AUDIO_S3_BUCKET", "")
    results["audio_storage"] = {
        "backend": os.environ.get("AUDIO_STORAGE_BACKEND", "local"),
        "s3_configured": bool(s3_bucket),
    }

    # Cache the result
    set_cached(CACHE_KEY, results)
    return results


# ---------------------------------------------------------------------------
# POST /api/beta-admin/change-tier — Change user subscription tier
# ---------------------------------------------------------------------------

class ChangeTierRequest(BaseModel):
    user_id: Optional[str] = None
    email: Optional[str] = None
    tier: str = Field(..., description="Target tier: free, verified, premium")
    trial_days: Optional[int] = Field(default=None, ge=1, le=365)

@router.post("/change-tier")
async def change_user_tier(data: ChangeTierRequest, admin: dict = Depends(_verify_admin)):
    """Change a user's plan tier. Optionally grant a trial period."""
    from datetime import datetime, timezone, timedelta

    # Find user
    filt = {}
    if data.user_id:
        filt["user_id"] = data.user_id
    elif data.email:
        filt["email"] = data.email.lower()
    else:
        raise HTTPException(status_code=400, detail="Provide user_id or email")

    user = await db.users.find_one(filt)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc)
    update: dict = {"plan_tier": data.tier, "updated_at": now.isoformat()}

    if data.tier == "premium" and data.trial_days:
        update["trial_active"] = True
        update["trial_expires_at"] = (now + timedelta(days=data.trial_days)).isoformat()
    elif data.tier == "premium":
        update["has_subscription"] = True
        update["trial_active"] = False
    elif data.tier == "free":
        update["has_subscription"] = False
        update["trial_active"] = False

    await db.users.update_one({"_id": user["_id"]}, {"$set": update})
    return {"message": f"User tier changed to {data.tier}", "user_id": user.get("user_id")}


# ---------------------------------------------------------------------------
# GET /api/beta-admin/queue-stats — System-wide queue generation stats
# ---------------------------------------------------------------------------

@router.get("/queue-stats")
async def get_queue_stats(admin: dict = Depends(_verify_admin)):
    """Get system-wide literature queue generation statistics."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    month_ago = (now - timedelta(days=30)).isoformat()

    # Total digests
    total_digests = await db.digests.count_documents({})
    digests_24h = await db.digests.count_documents({"generated_at": {"$gte": day_ago}})
    digests_7d = await db.digests.count_documents({"generated_at": {"$gte": week_ago}})
    digests_30d = await db.digests.count_documents({"generated_at": {"$gte": month_ago}})

    # Total profiles
    total_profiles = await db.digest_profiles.count_documents({"deleted_at": None, "is_active": True})

    # Email stats
    emails_sent = await db.digests.count_documents({"status": "sent"})
    emails_failed = await db.digests.count_documents({"status": "failed"})

    # Articles screened
    total_screenings = await db.article_screening.count_documents({})
    saves = await db.article_screening.count_documents({"decision": "keep"})
    skips = await db.article_screening.count_documents({"decision": "skip"})

    # Per-specialty breakdown
    pipeline = [
        {"$match": {"deleted_at": None, "is_active": True}},
        {"$group": {"_id": "$specialty_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 15},
    ]
    specialty_breakdown = await db.digest_profiles.aggregate(pipeline).to_list(15)

    return {
        "queues": {
            "total": total_digests,
            "last_24h": digests_24h,
            "last_7d": digests_7d,
            "last_30d": digests_30d,
        },
        "profiles": {"active": total_profiles},
        "emails": {"sent": emails_sent, "failed": emails_failed},
        "screening": {"total": total_screenings, "saved": saves, "skipped": skips},
        "specialty_breakdown": [{"specialty": s["_id"], "profiles": s["count"]} for s in specialty_breakdown],
    }
