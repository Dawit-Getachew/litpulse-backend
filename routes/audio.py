"""
Audio Takeaway + Playlist Routes for LitPulse Premium.
PHI-Zero: audio generated only from article metadata/AI summary.
"""
from fastapi import APIRouter, HTTPException, Depends, status, Query
from fastapi.responses import FileResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone, timedelta
from typing import Optional
import os
import logging

from auth_utils import get_current_user
from utils.capabilities import require_premium, compute_capabilities, derive_plan_tier
from utils.feature_flags import get_feature_flags

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio"])

db: AsyncIOMotorDatabase = None
_audio_service = None


def set_db(database: AsyncIOMotorDatabase):
    global db, _audio_service
    db = database
    from services.audio_service import AudioService
    _audio_service = AudioService(database)


# ---------------------------------------------------------------------------
# Audio file serving (local storage)
# ---------------------------------------------------------------------------

@router.get("/audio/files/{filename}")
async def serve_audio_file(filename: str):
    """Serve locally stored audio files."""
    path = os.path.join("/app/backend/storage/audio", filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    ct = "audio/wav" if filename.endswith(".wav") else "audio/mpeg"
    return FileResponse(path, media_type=ct)


# ---------------------------------------------------------------------------
# My Audio Summaries — user's audio history
# ---------------------------------------------------------------------------

@router.get("/audio/my-summaries")
async def get_my_audio_summaries(current_user: dict = Depends(get_current_user)):
    """List all audio summaries a user has generated, with play URLs."""
    flags = get_feature_flags()
    if not flags.get("enable_audio_takeaway"):
        raise HTTPException(status_code=404, detail="Audio takeaway not enabled")

    user_id = current_user["user_id"]

    # Get all audio generation events for this user (with pmid)
    events = await db.user_usage_events.find(
        {"user_id": user_id, "event_type": "audio_generate", "pmid": {"$exists": True}},
        {"_id": 0, "pmid": 1, "created_at": 1},
    ).sort("created_at", -1).to_list(200)

    # Deduplicate by pmid, keeping most recent
    seen = {}
    for ev in events:
        pmid = ev.get("pmid")
        if pmid and pmid not in seen:
            seen[pmid] = ev["created_at"]

    if not seen:
        # Fallback: check article_audio_summaries directly (for legacy events without pmid)
        all_audio = await db.article_audio_summaries.find(
            {"status": "ready"},
            {"_id": 0, "pmid": 1, "created_at": 1},
        ).sort("created_at", -1).to_list(100)

        for doc in all_audio:
            pmid = doc.get("pmid")
            if pmid and pmid not in seen:
                seen[pmid] = doc.get("created_at", "")

    # Build response with article details and audio status
    summaries = []
    for pmid, generated_at in seen.items():
        audio = await _audio_service.get_audio_status(pmid)
        if not audio or audio.get("status") != "ready":
            continue

        audio_url = await _audio_service.get_url_for_record(audio)
        article = await db.articles.find_one(
            {"pmid": pmid}, {"_id": 0, "title": 1, "journal": 1, "pub_date": 1}
        )

        summaries.append({
            "pmid": pmid,
            "title": (article or {}).get("title", f"PMID {pmid}"),
            "journal": (article or {}).get("journal", ""),
            "pub_date": (article or {}).get("pub_date", ""),
            "audio_url": audio_url,
            "duration_seconds": audio.get("duration_seconds"),
            "audio_format": audio.get("audio_format", "mp3"),
            "generated_at": generated_at,
            "storage_backend": audio.get("storage_backend", "local"),
        })

    return {"audio_summaries": summaries, "total": len(summaries)}


# ---------------------------------------------------------------------------
# Article audio endpoints
# ---------------------------------------------------------------------------

@router.get("/articles/{pmid}/audio-summary")
async def get_audio_summary(pmid: str, current_user: dict = Depends(get_current_user)):
    """Get audio takeaway status for an article (premium-only)."""
    flags = get_feature_flags()
    if not flags.get("enable_audio_takeaway"):
        raise HTTPException(status_code=404, detail="Audio takeaway not enabled")

    await require_premium(current_user["user_id"], db)

    audio = await _audio_service.get_audio_status(pmid)
    if not audio:
        return {"status": "missing", "audio_url": None, "transcript": None}

    audio_url = None
    if audio.get("status") == "ready" and audio.get("storage_key"):
        # Step 16: Use hybrid URL getter for storage-aware playback
        audio_url = await _audio_service.get_url_for_record(audio)

    return {
        "status": audio.get("status", "missing"),
        "audio_url": audio_url,
        "transcript": audio.get("transcript"),
        "duration_seconds": audio.get("duration_seconds"),
        "audio_format": audio.get("audio_format"),
        "audio_content_type": audio.get("audio_content_type"),
        "storage_backend": audio.get("storage_backend", "local"),  # Step 16: Expose for debugging
        "error_code": audio.get("error_code"),
        "updated_at": audio.get("updated_at"),
    }


@router.post("/articles/{pmid}/audio-summary/generate")
async def generate_audio_summary(pmid: str, current_user: dict = Depends(get_current_user)):
    """Generate audio takeaway for an article (premium-only, async, idempotent)."""
    flags = get_feature_flags()
    if not flags.get("enable_audio_takeaway"):
        raise HTTPException(status_code=404, detail="Audio takeaway not enabled")

    await require_premium(current_user["user_id"], db)

    # Quota check
    user_id = current_user["user_id"]
    if flags.get("enforce_audio_quota", False):
        user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0, "subscription_level": 1, "plan_tier": 1, "email": 1})
        caps = compute_capabilities(user_doc or {}, feature_flags=flags)
        limit = caps.get("audio_generations_per_24h", 20)
        window_start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        count = await db.user_usage_events.count_documents({
            "user_id": user_id, "event_type": "audio_generate",
            "created_at": {"$gte": window_start}
        })
        if count >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error_code": "audio_quota_exceeded", "message": f"Audio generation limit ({limit}/day) reached.", "retry_after_seconds": 3600},
            )

    result = await _audio_service.generate_audio(pmid, user_id)
    return {"status": result.get("status", "pending")}


@router.post("/articles/{pmid}/audio-summary/retry")
async def retry_audio_summary(pmid: str, current_user: dict = Depends(get_current_user)):
    """Retry failed audio generation (premium-only)."""
    flags = get_feature_flags()
    if not flags.get("enable_audio_takeaway"):
        raise HTTPException(status_code=404, detail="Audio takeaway not enabled")

    await require_premium(current_user["user_id"], db)

    existing = await _audio_service.get_audio_status(pmid)
    if not existing or existing.get("status") != "failed":
        raise HTTPException(status_code=400, detail="Can only retry failed audio")

    # Reset to allow regeneration
    await db.article_audio_summaries.update_one(
        {"pmid": pmid},
        {"$set": {"status": "missing", "updated_at": datetime.now(timezone.utc).isoformat()}}
    )
    result = await _audio_service.generate_audio(pmid, current_user["user_id"])
    return {"status": result.get("status", "pending")}


# ---------------------------------------------------------------------------
# Playlist endpoints
# ---------------------------------------------------------------------------

@router.get("/playlists/digest/{digest_id}")
async def get_digest_playlist(
    digest_id: str,
    auto_generate_missing: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """Get playlist for a digest (premium-only)."""
    flags = get_feature_flags()
    if not flags.get("enable_audio_takeaway"):
        raise HTTPException(status_code=404, detail="Audio takeaway not enabled")

    await require_premium(current_user["user_id"], db)

    digest = await db.digests.find_one(
        {"digest_id": digest_id, "user_id": current_user["user_id"]},
        {"_id": 0, "articles": 1}
    )
    if not digest:
        raise HTTPException(status_code=404, detail="Digest not found")

    # Get article PMIDs from digest
    from bson import ObjectId
    article_ids = digest.get("articles", [])
    pmids = []
    for aid in article_ids:
        if ObjectId.is_valid(aid):
            art = await db.articles.find_one({"_id": ObjectId(aid)}, {"_id": 0, "pmid": 1})
            if art and art.get("pmid"):
                pmids.append(art["pmid"])

    items = await _audio_service.get_playlist_items(pmids)

    if auto_generate_missing:
        for item in items:
            if item["status"] == "missing":
                try:
                    await _audio_service.generate_audio(item["pmid"], current_user["user_id"])
                except Exception:
                    pass

    # Recount
    counts = {"total": len(items), "ready": 0, "pending": 0, "failed": 0, "missing": 0}
    for item in items:
        s = item["status"]
        if s in counts:
            counts[s] += 1

    return {"playlist_id": f"digest_{digest_id}", "items": items, "counts": counts}


@router.get("/playlists/folder/{folder_name}")
async def get_folder_playlist(
    folder_name: str,
    auto_generate_missing: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """Get playlist for a library folder (premium-only)."""
    flags = get_feature_flags()
    if not flags.get("enable_audio_takeaway"):
        raise HTTPException(status_code=404, detail="Audio takeaway not enabled")

    await require_premium(current_user["user_id"], db)

    user_articles = await db.user_articles.find(
        {"user_id": current_user["user_id"], "saved_to_library": True, "folder": folder_name},
        {"_id": 0, "article_id": 1}
    ).to_list(200)

    from bson import ObjectId
    pmids = []
    for ua in user_articles:
        if ObjectId.is_valid(ua["article_id"]):
            art = await db.articles.find_one({"_id": ObjectId(ua["article_id"])}, {"_id": 0, "pmid": 1})
            if art and art.get("pmid"):
                pmids.append(art["pmid"])

    items = await _audio_service.get_playlist_items(pmids)

    if auto_generate_missing:
        for item in items:
            if item["status"] == "missing":
                try:
                    await _audio_service.generate_audio(item["pmid"], current_user["user_id"])
                except Exception:
                    pass

    counts = {"total": len(items), "ready": 0, "pending": 0, "failed": 0, "missing": 0}
    for item in items:
        s = item["status"]
        if s in counts:
            counts[s] += 1

    return {"playlist_id": f"folder_{folder_name}", "items": items, "counts": counts}
