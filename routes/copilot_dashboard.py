"""
Copilot & Audio Usage Dashboard — Admin-only analytics endpoints.
Aggregates data from user_usage_events, copilot_cache, and article_audio_summaries.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone, timedelta
from typing import Optional
import os
import logging

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/copilot-dashboard", tags=["admin-copilot-dashboard"])

db: AsyncIOMotorDatabase = None
_admin_email: str = ""


def set_db(database: AsyncIOMotorDatabase):
    global db
    db = database


def set_admin_email(email: str):
    global _admin_email
    _admin_email = email


async def _verify_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not _admin_email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin not configured")
    user = await db.users.find_one({"user_id": current_user["user_id"]}, {"_id": 0, "email": 1})
    if user and user.get("email", "").lower() == _admin_email.lower():
        return current_user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


# Cost estimates per call (USD) — configurable via env vars
def _get_cost_config():
    return {
        "copilot_cost_per_call": float(os.environ.get("COPILOT_COST_PER_CALL", "0.015")),
        "audio_cost_per_call": float(os.environ.get("AUDIO_COST_PER_CALL", "0.03")),
    }


@router.get("")
async def get_dashboard(
    days: int = Query(30, ge=1, le=90),
    admin_user: dict = Depends(_verify_admin),
):
    """Comprehensive copilot & audio usage dashboard."""
    costs = _get_cost_config()
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=days)).isoformat()

    # --- Copilot usage ---
    copilot_filter = {"event_type": "copilot_call", "created_at": {"$gte": window_start}}
    total_copilot = await db.user_usage_events.count_documents(copilot_filter)

    # By surface type
    surface_pipeline = [
        {"$match": copilot_filter},
        {"$group": {"_id": {"$ifNull": ["$surface", "unknown"]}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    surface_cursor = db.user_usage_events.aggregate(surface_pipeline)
    by_surface = {doc["_id"]: doc["count"] async for doc in surface_cursor}

    # Daily time series
    daily_pipeline = [
        {"$match": copilot_filter},
        {"$addFields": {"date_str": {"$substr": ["$created_at", 0, 10]}}},
        {"$group": {"_id": "$date_str", "copilot": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    daily_copilot = {doc["_id"]: doc["copilot"] async for doc in db.user_usage_events.aggregate(daily_pipeline)}

    # --- Audio usage ---
    audio_filter = {"event_type": "audio_generate", "created_at": {"$gte": window_start}}
    total_audio = await db.user_usage_events.count_documents(audio_filter)

    audio_daily_pipeline = [
        {"$match": audio_filter},
        {"$addFields": {"date_str": {"$substr": ["$created_at", 0, 10]}}},
        {"$group": {"_id": "$date_str", "audio": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    daily_audio = {doc["_id"]: doc["audio"] async for doc in db.user_usage_events.aggregate(audio_daily_pipeline)}

    # Merge daily time series
    all_dates = sorted(set(list(daily_copilot.keys()) + list(daily_audio.keys())))
    daily_series = [
        {
            "date": d,
            "copilot": daily_copilot.get(d, 0),
            "audio": daily_audio.get(d, 0),
            "total": daily_copilot.get(d, 0) + daily_audio.get(d, 0),
        }
        for d in all_dates
    ]

    # --- Top users (copilot) ---
    top_users_pipeline = [
        {"$match": copilot_filter},
        {"$group": {"_id": "$user_id", "calls": {"$sum": 1}}},
        {"$sort": {"calls": -1}},
        {"$limit": 10},
    ]
    top_users_raw = []
    async for doc in db.user_usage_events.aggregate(top_users_pipeline):
        user = await db.users.find_one({"user_id": doc["_id"]}, {"_id": 0, "email": 1, "full_name": 1})
        top_users_raw.append({
            "user_id": doc["_id"],
            "email": (user or {}).get("email", "unknown"),
            "name": (user or {}).get("full_name", ""),
            "copilot_calls": doc["calls"],
        })

    # Add audio counts for top users
    for u in top_users_raw:
        u["audio_calls"] = await db.user_usage_events.count_documents({
            "event_type": "audio_generate",
            "user_id": u["user_id"],
            "created_at": {"$gte": window_start},
        })

    # --- Cache stats ---
    total_cache_entries = await db.copilot_cache.count_documents({})
    active_cache = await db.copilot_cache.count_documents({"expires_at": {"$gt": now.isoformat()}})

    # --- Audio file stats ---
    audio_ready = await db.article_audio_summaries.count_documents({"status": "ready"})
    audio_failed = await db.article_audio_summaries.count_documents({"status": "failed"})
    audio_pending = await db.article_audio_summaries.count_documents({"status": "pending"})

    # --- Cost estimates ---
    est_copilot_cost = round(total_copilot * costs["copilot_cost_per_call"], 2)
    est_audio_cost = round(total_audio * costs["audio_cost_per_call"], 2)
    est_total_cost = round(est_copilot_cost + est_audio_cost, 2)

    # --- 24h / 7d sub-windows ---
    h24_start = (now - timedelta(hours=24)).isoformat()
    d7_start = (now - timedelta(days=7)).isoformat()

    copilot_24h = await db.user_usage_events.count_documents({"event_type": "copilot_call", "created_at": {"$gte": h24_start}})
    copilot_7d = await db.user_usage_events.count_documents({"event_type": "copilot_call", "created_at": {"$gte": d7_start}})
    audio_24h = await db.user_usage_events.count_documents({"event_type": "audio_generate", "created_at": {"$gte": h24_start}})
    audio_7d = await db.user_usage_events.count_documents({"event_type": "audio_generate", "created_at": {"$gte": d7_start}})

    # --- Unique users ---
    unique_copilot_pipeline = [
        {"$match": copilot_filter},
        {"$group": {"_id": "$user_id"}},
        {"$count": "total"},
    ]
    unique_copilot_result = await db.user_usage_events.aggregate(unique_copilot_pipeline).to_list(1)
    unique_copilot_users = unique_copilot_result[0]["total"] if unique_copilot_result else 0

    return {
        "period_days": days,
        "generated_at": now.isoformat(),
        "summary": {
            "copilot_calls_total": total_copilot,
            "copilot_calls_24h": copilot_24h,
            "copilot_calls_7d": copilot_7d,
            "audio_calls_total": total_audio,
            "audio_calls_24h": audio_24h,
            "audio_calls_7d": audio_7d,
            "unique_copilot_users": unique_copilot_users,
            "cache_entries_active": active_cache,
            "cache_entries_total": total_cache_entries,
        },
        "costs": {
            "estimated_copilot_cost_usd": est_copilot_cost,
            "estimated_audio_cost_usd": est_audio_cost,
            "estimated_total_cost_usd": est_total_cost,
            "copilot_rate_per_call": costs["copilot_cost_per_call"],
            "audio_rate_per_call": costs["audio_cost_per_call"],
        },
        "by_feature": by_surface,
        "daily_series": daily_series,
        "top_users": top_users_raw,
        "audio_files": {
            "ready": audio_ready,
            "failed": audio_failed,
            "pending": audio_pending,
        },
    }
