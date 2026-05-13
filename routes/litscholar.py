"""
LitScholar Routes — Structured PHI-safe expertise profile management.

Endpoints:
  GET  /api/litscholar/profile          — Get user's expertise profile
  POST /api/litscholar/profile/rebuild  — Rebuild profile from app activity
  GET  /api/litscholar/artifacts        — List saved artifacts
  POST /api/litscholar/artifacts        — Save an artifact (PHI-guarded)
  DELETE /api/litscholar/artifacts/{id} — Delete an artifact

Collection: litscholar_state (one document per user)
PHI-Zero: no raw text logs stored; all artifact text passes PHI guard.
"""
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone
from typing import Optional, List, Dict
import uuid
import logging
from collections import Counter

from auth_utils import get_current_user
from utils.capabilities import require_premium
from utils.feature_flags import get_feature_flags
from utils.phi_guard import scan_for_phi, enforce_phi_guard

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/litscholar", tags=["litscholar"])

db: AsyncIOMotorDatabase = None


def set_db(database: AsyncIOMotorDatabase):
    global db
    db = database


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class ExpertiseProfile(BaseModel):
    specialty_ids: List[str] = []
    subspecialty_ids: List[str] = []
    topic_weights: Dict[str, int] = {}
    journal_weights: Dict[str, int] = {}
    study_design_preferences: Dict[str, int] = {}
    recent_library_clusters: List[str] = []


class LitScholarState(BaseModel):
    user_id: str
    expertise_profile: ExpertiseProfile = ExpertiseProfile()
    saved_artifacts: List[dict] = []
    last_updated_at: Optional[str] = None
    version: int = 1


class SaveArtifactRequest(BaseModel):
    artifact_type: str = Field(..., pattern=r"^(evidence_brief|ask_answer|comparison)$")
    title: str = Field(..., min_length=1, max_length=200)
    summary_text: str = Field(..., min_length=1, max_length=5000)
    pmids: List[str] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_or_create_state(user_id: str) -> dict:
    """Fetch or initialize the litscholar_state document for a user."""
    doc = await db.litscholar_state.find_one(
        {"user_id": user_id}, {"_id": 0}
    )
    if doc:
        return doc
    now = datetime.now(timezone.utc).isoformat()
    new_state = {
        "user_id": user_id,
        "expertise_profile": {
            "specialty_ids": [],
            "subspecialty_ids": [],
            "topic_weights": {},
            "journal_weights": {},
            "study_design_preferences": {},
            "recent_library_clusters": [],
        },
        "expertise_summary": "",
        "saved_artifacts": [],
        "last_updated_at": now,
        "last_rebuild_completed_at": None,
        "version": 1,
    }
    await db.litscholar_state.insert_one(new_state)
    return await db.litscholar_state.find_one(
        {"user_id": user_id}, {"_id": 0}
    )


async def _build_expertise_profile(user_id: str) -> dict:
    """
    Build a structured expertise profile from existing LitPulse data.
    Sources: preferences, digest_profiles, user_articles (library + reads).
    PHI-Zero: only metadata fields are used (no free-text user content).
    """
    profile = {
        "specialty_ids": [],
        "subspecialty_ids": [],
        "topic_weights": {},
        "journal_weights": {},
        "study_design_preferences": {},
        "recent_library_clusters": [],
    }

    seen_specialties = set()
    seen_subspecialties = set()
    topic_counter = Counter()
    journal_counter = Counter()
    design_counter = Counter()
    cluster_topics = []

    # --- Source 1: User preferences ---
    prefs = await db.preferences.find_one(
        {"user_id": user_id}, {"_id": 0}
    )
    if prefs:
        if prefs.get("specialty_id"):
            seen_specialties.add(prefs["specialty_id"])
        for sub in (prefs.get("subspecialties") or []):
            seen_subspecialties.add(sub)
        if prefs.get("subspecialty_id"):
            seen_subspecialties.add(prefs["subspecialty_id"])
        for t in (prefs.get("topics_selected") or []):
            topic_counter[t] += 3  # high weight for explicit selection
        for t in (prefs.get("custom_topics") or []):
            topic_counter[t] += 3
        for j in (prefs.get("journals_selected") or []):
            journal_counter[j] += 3
        for j in (prefs.get("custom_journals") or []):
            journal_counter[j] += 3

    # --- Source 2: Digest profiles ---
    async for dp in db.digest_profiles.find(
        {"user_id": user_id, "deleted_at": None},
        {"_id": 0, "specialty_id": 1, "subspecialty_id": 1,
         "custom_keywords": 1}
    ):
        if dp.get("specialty_id"):
            seen_specialties.add(dp["specialty_id"])
        if dp.get("subspecialty_id"):
            seen_subspecialties.add(dp["subspecialty_id"])
        for kw in (dp.get("custom_keywords") or []):
            topic_counter[kw] += 2

    # --- Source 3: Library articles (recent 200) ---
    cursor = db.user_articles.find(
        {"user_id": user_id, "saved_to_library": True},
        {"_id": 0, "article_id": 1}
    ).sort("saved_at", -1).limit(200)

    article_ids = []
    async for ua in cursor:
        article_ids.append(ua["article_id"])

    if article_ids:
        # Batch fetch article metadata
        art_cursor = db.articles.find(
            {"pmid": {"$in": article_ids}},
            {"_id": 0, "journal": 1, "design_tags": 1,
             "mesh_terms": 1, "specialty": 1, "subspecialty": 1}
        )
        async for art in art_cursor:
            if art.get("journal"):
                journal_counter[art["journal"]] += 1
            for tag in (art.get("design_tags") or []):
                design_counter[tag] += 1
            for term in (art.get("mesh_terms") or []):
                topic_counter[term] += 1
                cluster_topics.append(term)
            if art.get("specialty"):
                seen_specialties.add(art["specialty"])
            if art.get("subspecialty"):
                seen_subspecialties.add(art["subspecialty"])

    # --- Source 4: Read articles (recent 100) ---
    read_cursor = db.user_articles.find(
        {"user_id": user_id, "is_read": True},
        {"_id": 0, "article_id": 1}
    ).sort("read_at", -1).limit(100)

    read_ids = []
    async for ua in read_cursor:
        if ua["article_id"] not in article_ids:
            read_ids.append(ua["article_id"])

    if read_ids:
        art_cursor = db.articles.find(
            {"pmid": {"$in": read_ids}},
            {"_id": 0, "journal": 1, "design_tags": 1, "mesh_terms": 1}
        )
        async for art in art_cursor:
            if art.get("journal"):
                journal_counter[art["journal"]] += 1
            for tag in (art.get("design_tags") or []):
                design_counter[tag] += 1
            for term in (art.get("mesh_terms") or []):
                topic_counter[term] += 1

    # --- Assemble profile ---
    profile["specialty_ids"] = sorted(seen_specialties)
    profile["subspecialty_ids"] = sorted(seen_subspecialties)

    # Top-N weights (limit to most relevant)
    profile["topic_weights"] = dict(topic_counter.most_common(30))
    profile["journal_weights"] = dict(journal_counter.most_common(20))
    profile["study_design_preferences"] = dict(design_counter.most_common(10))

    # Cluster top topics as "recent library clusters"
    if cluster_topics:
        cluster_counter = Counter(cluster_topics)
        profile["recent_library_clusters"] = [
            t for t, _ in cluster_counter.most_common(10)
        ]

    return profile


def _build_expertise_summary(profile: dict) -> str:
    """
    Precompute a compact expertise summary string from the full profile.
    This is stored alongside the profile and sent to LLM prompts directly,
    avoiding recomputation on every copilot request.
    """
    parts = []
    if profile.get("specialty_ids"):
        parts.append(f"User specialties: {', '.join(profile['specialty_ids'])}")
    if profile.get("subspecialty_ids"):
        parts.append(f"Subspecialties: {', '.join(profile['subspecialty_ids'])}")
    tw = profile.get("topic_weights", {})
    if tw:
        top = sorted(tw, key=tw.get, reverse=True)[:5]
        parts.append(f"Key interests: {', '.join(top)}")
    jw = profile.get("journal_weights", {})
    if jw:
        top = sorted(jw, key=jw.get, reverse=True)[:3]
        parts.append(f"Frequently read journals: {', '.join(top)}")
    dw = profile.get("study_design_preferences", {})
    if dw:
        top = sorted(dw, key=dw.get, reverse=True)[:3]
        parts.append(f"Preferred study designs: {', '.join(top)}")
    if not parts:
        return ""
    return (
        "\n\n[User Expertise Context — use to tailor language and focus, "
        "but do NOT use as evidence source]\n" + "\n".join(parts)
    )


def _format_expertise_context(profile: dict) -> str:
    """Backward-compat wrapper — delegates to _build_expertise_summary."""
    return _build_expertise_summary(profile)


# ---------------------------------------------------------------------------
# GET /api/litscholar/profile
# ---------------------------------------------------------------------------

@router.get("/profile")
async def get_profile(current_user: dict = Depends(get_current_user)):
    flags = get_feature_flags()
    if not flags.get("enable_litscholar_profile_memory"):
        raise HTTPException(
            status_code=503,
            detail={"error_code": "feature_disabled",
                    "message": "LitScholar profile memory is not enabled."}
        )
    await require_premium(current_user["user_id"], db)
    state = await _get_or_create_state(current_user["user_id"])
    return state


# ---------------------------------------------------------------------------
# POST /api/litscholar/profile/rebuild
# ---------------------------------------------------------------------------

@router.post("/profile/rebuild")
async def rebuild_profile(current_user: dict = Depends(get_current_user)):
    flags = get_feature_flags()
    if not flags.get("enable_litscholar_profile_memory"):
        raise HTTPException(
            status_code=503,
            detail={"error_code": "feature_disabled",
                    "message": "LitScholar profile memory is not enabled."}
        )
    await require_premium(current_user["user_id"], db)

    user_id = current_user["user_id"]
    new_profile = await _build_expertise_profile(user_id)
    summary = _build_expertise_summary(new_profile)
    now = datetime.now(timezone.utc).isoformat()

    await db.litscholar_state.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "expertise_profile": new_profile,
                "expertise_summary": summary,
                "last_updated_at": now,
                "last_rebuild_completed_at": now,
            },
            "$inc": {"version": 1},
            "$setOnInsert": {
                "user_id": user_id,
                "saved_artifacts": [],
            },
        },
        upsert=True,
    )

    state = await db.litscholar_state.find_one(
        {"user_id": user_id}, {"_id": 0}
    )
    logger.info("LITSCHOLAR: rebuilt profile for user=%s specialties=%s topics=%d",
                user_id, new_profile.get("specialty_ids"), len(new_profile.get("topic_weights", {})))
    return state


# ---------------------------------------------------------------------------
# GET /api/litscholar/artifacts
# ---------------------------------------------------------------------------

@router.get("/artifacts")
async def list_artifacts(
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
):
    flags = get_feature_flags()
    if not flags.get("enable_litscholar_profile_memory"):
        raise HTTPException(
            status_code=503,
            detail={"error_code": "feature_disabled",
                    "message": "LitScholar profile memory is not enabled."}
        )
    await require_premium(current_user["user_id"], db)

    state = await _get_or_create_state(current_user["user_id"])
    artifacts = state.get("saved_artifacts", [])
    # Return newest first, limited
    artifacts_sorted = sorted(
        artifacts, key=lambda a: a.get("created_at", ""), reverse=True
    )
    return {"artifacts": artifacts_sorted[:limit], "total": len(artifacts)}


# ---------------------------------------------------------------------------
# POST /api/litscholar/artifacts
# ---------------------------------------------------------------------------

@router.post("/artifacts", status_code=201)
async def save_artifact(
    data: SaveArtifactRequest,
    current_user: dict = Depends(get_current_user),
):
    flags = get_feature_flags()
    if not flags.get("enable_litscholar_profile_memory"):
        raise HTTPException(
            status_code=503,
            detail={"error_code": "feature_disabled",
                    "message": "LitScholar profile memory is not enabled."}
        )
    await require_premium(current_user["user_id"], db)

    user_id = current_user["user_id"]

    # PHI guard on user-supplied text fields
    enforce_phi_guard(
        text=data.title,
        endpoint="POST /api/litscholar/artifacts",
        user_id=user_id,
        mode=flags.get("phi_guard_mode", "block"),
        enabled=flags.get("enable_phi_guard", True),
    )
    enforce_phi_guard(
        text=data.summary_text,
        endpoint="POST /api/litscholar/artifacts",
        user_id=user_id,
        mode=flags.get("phi_guard_mode", "block"),
        enabled=flags.get("enable_phi_guard", True),
    )

    artifact = {
        "artifact_id": str(uuid.uuid4()),
        "artifact_type": data.artifact_type,
        "title": data.title,
        "summary_text": data.summary_text,
        "pmids": data.pmids,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Cap artifacts at 100 per user
    MAX_ARTIFACTS = 100
    state = await _get_or_create_state(user_id)
    current_count = len(state.get("saved_artifacts", []))

    if current_count >= MAX_ARTIFACTS:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "artifact_limit_reached",
                "message": f"Maximum {MAX_ARTIFACTS} saved artifacts. Delete older ones first.",
            }
        )

    await db.litscholar_state.update_one(
        {"user_id": user_id},
        {
            "$push": {"saved_artifacts": artifact},
            "$set": {"last_updated_at": datetime.now(timezone.utc).isoformat()},
        },
    )

    logger.info("LITSCHOLAR: saved artifact user=%s type=%s pmids=%s",
                user_id, data.artifact_type, data.pmids)
    return artifact


# ---------------------------------------------------------------------------
# DELETE /api/litscholar/artifacts/{artifact_id}
# ---------------------------------------------------------------------------

@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(
    artifact_id: str,
    current_user: dict = Depends(get_current_user),
):
    flags = get_feature_flags()
    if not flags.get("enable_litscholar_profile_memory"):
        raise HTTPException(
            status_code=503,
            detail={"error_code": "feature_disabled",
                    "message": "LitScholar profile memory is not enabled."}
        )
    await require_premium(current_user["user_id"], db)

    user_id = current_user["user_id"]
    # Check artifact exists before attempting delete
    exists = await db.litscholar_state.find_one(
        {"user_id": user_id, "saved_artifacts.artifact_id": artifact_id},
        {"_id": 1},
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Artifact not found")

    await db.litscholar_state.update_one(
        {"user_id": user_id},
        {
            "$pull": {"saved_artifacts": {"artifact_id": artifact_id}},
            "$set": {"last_updated_at": datetime.now(timezone.utc).isoformat()},
        },
    )

    return {"message": "Artifact deleted", "artifact_id": artifact_id}
