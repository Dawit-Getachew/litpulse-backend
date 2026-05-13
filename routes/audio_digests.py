"""
Audio Digests V2 Routes — Phase 7 + 7.1
Behind ENABLE_LIBRARY_AUDIO_DIGESTS_V2

POST   /api/audio-digests             — Create from library multi-select
GET    /api/audio-digests             — History list (most-recent first)
GET    /api/audio-digests/{id}        — Detail + playlist items with presigned URLs
GET    /api/audio-digests/{id}/download.zip — Stream ZIP of audio files (hardened)

Retention: newest 10 per user, oldest soft-deleted automatically.
PHI-Zero: titles are not logged (only counts + IDs).

Phase 7.1 Hardening:
- Max tracks per ZIP (default 25)
- Max total bytes (default 200MB)
- Per-user rate limiting (5/minute)
- Filename sanitization (no path traversal)
- Strict authz (user can only download own digests)
"""
from __future__ import annotations

import io
import os
import re
import uuid
import zipfile
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio-digests", tags=["audio-digests-v2"])
db = None
_audio_service = None

RETENTION_LIMIT = 10

# Phase 7.1: ZIP download limits (configurable via env vars)
MAX_ZIP_TRACKS = int(os.environ.get("MAX_ZIP_TRACKS", "25"))
MAX_ZIP_BYTES = int(os.environ.get("MAX_ZIP_BYTES", str(200 * 1024 * 1024)))  # 200MB
ZIP_RATE_LIMIT_WINDOW_SECONDS = 60
ZIP_RATE_LIMIT_MAX = int(os.environ.get("ZIP_RATE_LIMIT_MAX", "5"))  # 5 downloads per minute


def set_db(database):
    global db, _audio_service
    db = database
    from services.audio_service import AudioService
    _audio_service = AudioService(database)


# ---------------------------------------------------------------------------
# Feature check helper
# ---------------------------------------------------------------------------

def _require_v2():
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    if not flags.get("enable_library_audio_digests_v2", False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "feature_disabled", "message": "Audio Digests V2 is not enabled."},
        )
    if not flags.get("enable_audio_takeaway", False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "feature_disabled", "message": "Audio takeaway is not enabled."},
        )
    return flags


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AudioDigestCreate(BaseModel):
    pmids: List[str] = Field(..., min_length=1, max_length=30)
    title: Optional[str] = Field(None, max_length=100)
    auto_generate_missing: bool = True
    mode: str = Field("playlist", pattern="^(playlist|combined_summary)$")


# ---------------------------------------------------------------------------
# Retention enforcement (idempotent, safe)
# ---------------------------------------------------------------------------

async def _derive_specialty_from_pmids(pmids: List[str]) -> dict:
    """
    Derive specialty/subspecialty from the given PMIDs by majority vote.
    Returns the most common specialty among the articles.
    """
    specialty_counts = {}
    
    for pmid in pmids:
        article = await db.articles.find_one(
            {"pmid": pmid},
            {"_id": 0, "specialty": 1, "specialty_id": 1, "subspecialty": 1, "subspecialty_id": 1}
        )
        if article and article.get("specialty_id"):
            key = (
                article.get("specialty_id", ""),
                article.get("specialty", ""),
                article.get("subspecialty_id", ""),
                article.get("subspecialty", "")
            )
            specialty_counts[key] = specialty_counts.get(key, 0) + 1
    
    if not specialty_counts:
        return {}
    
    # Get the most common specialty
    most_common = max(specialty_counts.items(), key=lambda x: x[1])[0]
    return {
        "specialty_id": most_common[0] or None,
        "specialty_name": most_common[1] or None,
        "subspecialty_id": most_common[2] or None,
        "subspecialty_name": most_common[3] or None,
    }


async def _enforce_retention(user_id: str):
    """Soft-delete the oldest digests beyond RETENTION_LIMIT (10)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    all_active = await db.user_audio_digests.find(
        {"user_id": user_id, "deleted_at": None},
        {"_id": 0, "audio_digest_id": 1, "created_at": 1}
    ).sort("created_at", -1).to_list(RETENTION_LIMIT + 20)

    if len(all_active) > RETENTION_LIMIT:
        to_prune = all_active[RETENTION_LIMIT:]
        ids = [d["audio_digest_id"] for d in to_prune]
        await db.user_audio_digests.update_many(
            {"audio_digest_id": {"$in": ids}},
            {"$set": {"deleted_at": now_iso}},
        )
        logger.info("[AUDIO_DIGESTS_V2] Pruned %d old digests for user=%s", len(ids), user_id)


# ---------------------------------------------------------------------------
# Combined-summary creation (new mode)
# ---------------------------------------------------------------------------

MAX_COMBINED_ARTICLES = 5


async def _create_combined_summary(
    user_id: str, valid_pmids: List[str], title: Optional[str], flags: dict
) -> dict:
    """
    Create a single grounded narration covering 1-5 articles.

    Uses the shared grounded_article_context_service to build source packets,
    then generates a script with the copilot_provider, converts to audio via
    the existing TTS pipeline, and stores as one combined audio file.
    """
    from utils.feature_flags import get_feature_flags
    current_flags = get_feature_flags()

    if not current_flags.get("enable_library_combined_audio_summary", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "feature_disabled", "message": "Combined audio summary is not enabled."},
        )

    if len(valid_pmids) > MAX_COMBINED_ARTICLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "too_many_articles",
                "message": f"Combined summary supports up to {MAX_COMBINED_ARTICLES} articles, got {len(valid_pmids)}.",
            },
        )

    from utils.capabilities import require_premium
    await require_premium(user_id, db)

    # Build grounded context
    from services.grounded_article_context_service import build_grounded_context
    context = await build_grounded_context(db, valid_pmids)

    if context["article_count"] == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "no_articles_found", "message": "Could not fetch article data for the selected PMIDs."},
        )

    # Generate narration script
    script = await _generate_combined_script(context)
    grounding_level = context["overall_grounding_level"]

    # Generate audio via existing TTS pipeline
    combined_audio_url = None
    combined_storage_key = None
    try:
        tts_result = await _audio_service.generate_tts_audio(script, user_id)
        if tts_result:
            combined_audio_url = tts_result.get("audio_url")
            combined_storage_key = tts_result.get("storage_key")
    except Exception as e:
        logger.warning("[AUDIO_DIGESTS_V2] TTS failed for combined summary user=%s: %s", user_id, type(e).__name__)

    # Build evidence map for traceability
    evidence_map = []
    for packet in context["source_packets"]:
        evidence_map.append({
            "pmid": packet["pmid"],
            "title": packet["title"],
            "grounding_level": packet["grounding_level"],
            "anchor_count": len(packet["evidence_anchors"]),
        })

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    auto_title = f"Combined Summary – {now.strftime('%Y-%m-%d')}"

    # Derive specialty/subspecialty from selected articles
    specialty_info = await _derive_specialty_from_pmids(valid_pmids)

    doc = {
        "audio_digest_id": str(uuid.uuid4()),
        "user_id": user_id,
        "title": (title or "").strip() or auto_title,
        "pmids": valid_pmids,
        "mode": "combined_summary",
        "created_at": now_iso,
        "last_played_at": None,
        "deleted_at": None,
        # Specialty/subspecialty tags for filtering
        "specialty_id": specialty_info.get("specialty_id"),
        "specialty_name": specialty_info.get("specialty_name"),
        "subspecialty_id": specialty_info.get("subspecialty_id"),
        "subspecialty_name": specialty_info.get("subspecialty_name"),
        # Combined-summary specific fields
        "combined_audio_storage_key": combined_storage_key,
        "combined_audio_url": combined_audio_url,
        "combined_transcript": script,
        "grounding_level": grounding_level,
        "evidence_map": evidence_map,
        "summary_title": (title or "").strip() or auto_title,
    }

    await db.user_audio_digests.insert_one(doc)
    doc.pop("_id", None)

    logger.info(
        "[AUDIO_DIGESTS_V2] Created combined_summary audio_digest_id=%s user=%s pmid_count=%d grounding=%s",
        doc["audio_digest_id"], user_id, len(valid_pmids), grounding_level,
    )

    await _enforce_retention(user_id)

    return {
        **doc,
        "item_count": len(valid_pmids),
        "items": [],  # No individual playlist items for combined summary
    }


async def _generate_combined_script(context: dict) -> str:
    """
    Generate a structured combined narration script from grounded source packets.

    Structure:
      1. Overview of the selected evidence set
      2. Article-by-article key findings
      3. Cross-study comparison
      4. Limitations and differences
      5. Careful takeaways supported only by the selected article text

    Uses copilot_provider if available; falls back to deterministic template.
    """
    packets = context["source_packets"]
    grounding_level = context["overall_grounding_level"]
    grounding_note = (
        "Note: This summary is grounded only in the article abstracts available in LitPulse."
        if grounding_level == "abstract_only"
        else "Note: This summary is grounded in the article text available in LitPulse."
    )

    # Try LLM-based script generation
    try:
        from utils.copilot_provider import create_copilot_provider
        provider = create_copilot_provider()

        article_blocks = []
        for i, p in enumerate(packets, 1):
            findings = "; ".join(p["key_findings"]) if p["key_findings"] else "No key findings extracted."
            limitations = "; ".join(p["limitations"]) if p["limitations"] else "No limitations noted."
            article_blocks.append(
                f"Article {i}: {p['title']} ({p['citation_metadata'].get('journal', 'Unknown')}, "
                f"{p['citation_metadata'].get('pub_date', 'N/A')})\n"
                f"Study type: {p['study_type']}\n"
                f"Key findings: {findings}\n"
                f"Limitations: {limitations}"
            )

        prompt = f"""You are a medical literature narrator. Create a concise audio summary script.
{grounding_note}

CRITICAL RULES:
- Use ONLY the information provided below. Do not add outside medical knowledge.
- If information is insufficient, say so explicitly.
- Do not make unsupported claims.

ARTICLES:
{chr(10).join(article_blocks)}

Structure your script as:
1. Brief overview of this evidence set ({len(packets)} articles)
2. Key findings from each article
3. Cross-study comparison (similarities, differences)
4. Limitations across studies
5. Careful takeaways supported only by these articles

Keep the tone professional and clear. Target 2-3 minutes of reading time."""

        result = await provider.generate(prompt)
        if result and isinstance(result, str) and len(result) > 100:
            return result
    except Exception as e:
        logger.info("[AUDIO_DIGESTS_V2] LLM script generation unavailable (%s), using template", type(e).__name__)

    # Fallback: deterministic template-based script
    lines = [grounding_note, ""]
    lines.append(f"This combined summary covers {len(packets)} articles.\n")

    for i, p in enumerate(packets, 1):
        lines.append(f"Article {i}: {p['title']}.")
        journal = p["citation_metadata"].get("journal", "")
        pub_date = p["citation_metadata"].get("pub_date", "")
        if journal:
            lines.append(f"Published in {journal}, {pub_date}.")
        lines.append(f"Study type: {p['study_type']}.")
        if p["key_findings"] and p["key_findings"][0] != "Insufficient information in available article text.":
            lines.append("Key findings: " + "; ".join(p["key_findings"]) + ".")
        else:
            lines.append("Key findings were not available in the article text.")
        if p["limitations"] and p["limitations"][0] != "Insufficient information in available article text.":
            lines.append("Limitations: " + "; ".join(p["limitations"]) + ".")
        lines.append("")

    lines.append("In summary, these articles provide a range of perspectives on the topic.")
    lines.append("Readers should review the full publications for complete evidence.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# POST /api/audio-digests
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_audio_digest(
    data: AudioDigestCreate,
    current_user: dict = Depends(get_current_user),
):
    """
    Create an audio digest from library-selected PMIDs.
    Requires premium/trial + ENABLE_LIBRARY_AUDIO_DIGESTS_V2=true.
    PHI-Zero: title not logged; scanned for PHI.
    """
    flags = _require_v2()
    user_id = current_user["user_id"]

    # PHI-Zero: scan title for protected health information
    if data.title:
        from utils.phi_guard import enforce_phi_guard
        enforce_phi_guard(
            text=data.title,
            endpoint="POST /api/audio-digests",
            user_id=user_id,
            mode=flags.get("phi_guard_mode", "block"),
            enabled=flags.get("enable_phi_guard", True),
        )

    from utils.capabilities import require_premium
    await require_premium(user_id, db)

    # Validate PMIDs are in user's library
    from bson import ObjectId
    valid_pmids: List[str] = []
    for pmid in data.pmids:
        article = await db.articles.find_one({"pmid": pmid}, {"_id": 1})
        if not article:
            continue
        article_id = str(article["_id"])
        ua = await db.user_articles.find_one(
            {"user_id": user_id, "article_id": article_id, "saved_to_library": True},
            {"_id": 0, "article_id": 1},
        )
        if ua:
            valid_pmids.append(pmid)

    if not valid_pmids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "no_library_pmids", "message": "None of the selected articles are in your library."},
        )

    # -----------------------------------------------------------------------
    # Route by mode
    # -----------------------------------------------------------------------
    if data.mode == "combined_summary":
        return await _create_combined_summary(
            user_id=user_id,
            valid_pmids=valid_pmids,
            title=data.title,
            flags=flags,
        )

    # Default: playlist mode (existing behavior)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    auto_title = f"Audio Digest – {now.strftime('%Y-%m-%d')}"

    # Derive specialty/subspecialty from selected articles (majority vote)
    specialty_info = await _derive_specialty_from_pmids(valid_pmids)

    doc = {
        "audio_digest_id": str(uuid.uuid4()),
        "user_id": user_id,
        "title": (data.title or "").strip() or auto_title,
        "pmids": valid_pmids,
        "created_at": now_iso,
        "last_played_at": None,
        "deleted_at": None,
        # Specialty/subspecialty tags for filtering
        "specialty_id": specialty_info.get("specialty_id"),
        "specialty_name": specialty_info.get("specialty_name"),
        "subspecialty_id": specialty_info.get("subspecialty_id"),
        "subspecialty_name": specialty_info.get("subspecialty_name"),
    }
    await db.user_audio_digests.insert_one(doc)
    doc.pop("_id", None)
    # PHI-Zero: log only ID + count, not title
    logger.info("[AUDIO_DIGESTS_V2] Created audio_digest_id=%s user=%s pmid_count=%d",
                doc["audio_digest_id"], user_id, len(valid_pmids))

    # Auto-generate missing audio (quota-checked, non-blocking on failure)
    if data.auto_generate_missing and flags.get("enable_audio_takeaway"):
        for pmid in valid_pmids:
            audio = await _audio_service.get_audio_status(pmid)
            if not audio or audio.get("status") not in ("ready", "pending"):
                try:
                    # Check quota
                    if flags.get("enforce_audio_quota", False):
                        from utils.capabilities import compute_capabilities
                        user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
                        caps = compute_capabilities(user_doc or {}, feature_flags=flags)
                        limit = caps.get("audio_generations_per_24h", 20)
                        from datetime import timedelta
                        window = (now - timedelta(hours=24)).isoformat()
                        count = await db.user_usage_events.count_documents({
                            "user_id": user_id, "event_type": "audio_generate",
                            "created_at": {"$gte": window},
                        })
                        if count >= limit:
                            break  # quota hit — stop generating
                    await _audio_service.generate_audio(pmid, user_id)
                except Exception:
                    pass  # Best-effort; never fail the create request

    # Enforce retention
    await _enforce_retention(user_id)

    # Return with playlist items
    items = await _audio_service.get_playlist_items(valid_pmids)
    return {**doc, "items": items, "item_count": len(valid_pmids)}


# ---------------------------------------------------------------------------
# GET /api/audio-digests
# ---------------------------------------------------------------------------

@router.get("")
async def list_audio_digests(current_user: dict = Depends(get_current_user)):
    """List user's audio digests (most recent first), excluding deleted."""
    _require_v2()
    user_id = current_user["user_id"]

    digests = await db.user_audio_digests.find(
        {"user_id": user_id, "deleted_at": None},
        {"_id": 0},
    ).sort("created_at", -1).to_list(RETENTION_LIMIT + 5)

    for d in digests:
        d["item_count"] = len(d.get("pmids", []))

    return {"audio_digests": digests, "total": len(digests)}


# ---------------------------------------------------------------------------
# GET /api/audio-digests/{audio_digest_id}
# ---------------------------------------------------------------------------

@router.get("/{audio_digest_id}")
async def get_audio_digest(
    audio_digest_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get an audio digest with full playlist items and presigned URLs."""
    _require_v2()
    user_id = current_user["user_id"]

    doc = await db.user_audio_digests.find_one(
        {"audio_digest_id": audio_digest_id, "user_id": user_id, "deleted_at": None},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio digest not found")

    mode = doc.get("mode", "playlist")

    # Update last_played_at (best-effort)
    try:
        await db.user_audio_digests.update_one(
            {"audio_digest_id": audio_digest_id, "user_id": user_id},
            {"$set": {"last_played_at": datetime.now(timezone.utc).isoformat()}},
        )
    except Exception:
        pass

    if mode == "combined_summary":
        # Combined summary: return single audio + evidence map
        combined_url = doc.get("combined_audio_url")
        if not combined_url and doc.get("combined_audio_storage_key"):
            try:
                combined_url = await _audio_service.storage.get_url(doc["combined_audio_storage_key"])
            except Exception:
                pass
        return {
            **doc,
            "combined_audio_url": combined_url,
            "items": [],
            "counts": {"total": len(doc.get("pmids", [])), "ready": 1 if combined_url else 0},
        }

    # Default: playlist mode (original behavior)
    pmids = doc.get("pmids", [])
    items = await _audio_service.get_playlist_items(pmids)

    counts = {"total": len(items), "ready": 0, "pending": 0, "failed": 0, "missing": 0}
    for item in items:
        s = item.get("status", "missing")
        if s in counts:
            counts[s] += 1

    return {**doc, "items": items, "counts": counts}


# ---------------------------------------------------------------------------
# DELETE /api/audio-digests/{audio_digest_id}
# ---------------------------------------------------------------------------

@router.delete("/{audio_digest_id}", status_code=status.HTTP_200_OK)
async def delete_audio_digest(
    audio_digest_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Soft-delete an audio digest. Only the owner can delete.
    Idempotent: repeated deletes return 200 without error.
    """
    _require_v2()
    user_id = current_user["user_id"]

    doc = await db.user_audio_digests.find_one(
        {"audio_digest_id": audio_digest_id, "user_id": user_id},
        {"_id": 0, "deleted_at": 1},
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio digest not found")

    # Already deleted — idempotent
    if doc.get("deleted_at"):
        return {"status": "deleted", "audio_digest_id": audio_digest_id}

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.user_audio_digests.update_one(
        {"audio_digest_id": audio_digest_id, "user_id": user_id},
        {"$set": {"deleted_at": now_iso}},
    )

    logger.info(
        "[AUDIO_DIGESTS_V2] Deleted audio_digest_id=%s user=%s",
        audio_digest_id, user_id,
    )

    return {"status": "deleted", "audio_digest_id": audio_digest_id}


# ---------------------------------------------------------------------------
# Phase 7.1: Helper functions for ZIP hardening
# ---------------------------------------------------------------------------

def _sanitize_filename(filename: str) -> str:
    """
    Sanitize filename to prevent path traversal and ensure deterministic names.
    Only allow alphanumeric, underscore, hyphen, and dot characters.
    """
    # Remove any path components
    filename = os.path.basename(filename)
    # Replace any dangerous characters
    safe = re.sub(r'[^a-zA-Z0-9_\-.]', '_', filename)
    # Ensure no double dots (path traversal)
    safe = re.sub(r'\.{2,}', '.', safe)
    # Limit length
    if len(safe) > 100:
        safe = safe[:100]
    return safe or "audio"


async def _check_zip_rate_limit(user_id: str) -> bool:
    """
    Check if user has exceeded ZIP download rate limit.
    Returns True if rate limit exceeded.
    """
    window = (datetime.now(timezone.utc) - timedelta(seconds=ZIP_RATE_LIMIT_WINDOW_SECONDS)).isoformat()
    count = await db.user_usage_events.count_documents({
        "user_id": user_id,
        "event_type": "zip_download",
        "created_at": {"$gte": window},
    })
    return count >= ZIP_RATE_LIMIT_MAX


async def _record_zip_download(user_id: str) -> None:
    """Record a ZIP download usage event for rate limiting."""
    await db.user_usage_events.insert_one({
        "event_id": str(uuid.uuid4()),
        "user_id": user_id,
        "event_type": "zip_download",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


async def _streaming_zip_generator(audio_files: list) -> AsyncIterator[bytes]:
    """
    Generate ZIP file as a stream to avoid holding entire file in memory.
    Uses an in-memory buffer but processes in chunks.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for filename, data_bytes, _ in audio_files:
            safe_name = _sanitize_filename(filename)
            zf.writestr(safe_name, data_bytes)
    
    buf.seek(0)
    # Stream in 64KB chunks
    chunk_size = 65536
    while True:
        chunk = buf.read(chunk_size)
        if not chunk:
            break
        yield chunk


# ---------------------------------------------------------------------------
# GET /api/audio-digests/{audio_digest_id}/download.zip
# ---------------------------------------------------------------------------

@router.get("/{audio_digest_id}/download.zip")
async def download_audio_digest_zip(
    audio_digest_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Stream a ZIP archive of audio files for the digest.
    
    Phase 7.1 Hardening:
    - Max tracks: MAX_ZIP_TRACKS (default 25)
    - Max total bytes: MAX_ZIP_BYTES (default 200MB)
    - Per-user rate limiting (5/minute)
    - Sanitized filenames (no path traversal)
    - Strict authz (user can only download own digests)
    
    PHI-Zero: ZIP entries named by PMID, not article title.
    """
    _require_v2()
    user_id = current_user["user_id"]

    # Phase 7.1: Rate limiting check
    if await _check_zip_rate_limit(user_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error_code": "zip_rate_limited",
                "message": "Too many downloads. Please wait a minute before trying again.",
                "limit": ZIP_RATE_LIMIT_MAX,
                "window_seconds": ZIP_RATE_LIMIT_WINDOW_SECONDS,
            },
        )

    # Strict authz: user can only download their own digests
    doc = await db.user_audio_digests.find_one(
        {"audio_digest_id": audio_digest_id, "user_id": user_id, "deleted_at": None},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio digest not found")

    pmids = doc.get("pmids", [])
    if not pmids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Audio digest is empty")

    # Phase 7.1: Enforce max tracks cap
    if len(pmids) > MAX_ZIP_TRACKS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error_code": "zip_too_large",
                "message": f"Audio digest has too many tracks ({len(pmids)}). Maximum allowed: {MAX_ZIP_TRACKS}.",
                "track_count": len(pmids),
                "max_tracks": MAX_ZIP_TRACKS,
            },
        )

    # Collect audio bytes for each ready PMID
    audio_files: list[tuple[str, bytes, str]] = []  # (filename, bytes, extension)
    total_bytes = 0

    for i, pmid in enumerate(pmids, start=1):
        audio = await db.article_audio_summaries.find_one(
            {"pmid": pmid, "status": "ready"}, {"_id": 0}
        )
        if not audio or not audio.get("storage_key"):
            continue
        try:
            audio_bytes = await _audio_service.get_bytes_for_record(audio)
            if audio_bytes:
                # Phase 7.1: Check cumulative size
                total_bytes += len(audio_bytes)
                if total_bytes > MAX_ZIP_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail={
                            "error_code": "zip_too_large",
                            "message": f"Audio digest exceeds maximum download size ({MAX_ZIP_BYTES // (1024*1024)}MB).",
                            "max_bytes": MAX_ZIP_BYTES,
                        },
                    )
                
                ext = audio.get("audio_format", "wav")
                # Phase 7.1: Sanitize filename
                raw_filename = f"{i:02d}_{pmid}.{ext}"
                filename = _sanitize_filename(raw_filename)
                audio_files.append((filename, audio_bytes, ext))
        except HTTPException:
            raise  # Re-raise size limit errors
        except Exception:
            continue  # Skip unavailable tracks

    if not audio_files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error_code": "no_audio_ready", "message": "No audio tracks are ready for download."},
        )

    # Phase 7.1: Record rate limit event
    await _record_zip_download(user_id)

    # Build safe ZIP filename
    safe_digest_id = _sanitize_filename(audio_digest_id[:8])
    zip_filename = f"litpulse_audio_digest_{safe_digest_id}.zip"
    
    logger.info(
        "[AUDIO_DIGESTS_V2] ZIP download audio_digest_id=%s user=%s tracks=%d bytes=%d",
        audio_digest_id, user_id, len(audio_files), total_bytes
    )

    # Phase 7.1: Stream the ZIP (chunked to avoid memory spikes)
    return StreamingResponse(
        _streaming_zip_generator(audio_files),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={zip_filename}",
            "X-Track-Count": str(len(audio_files)),
            "X-Total-Bytes": str(total_bytes),
        },
    )
