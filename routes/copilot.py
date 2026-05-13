"""
Copilot Routes for LitPulse Premium.
Literature assistant: evidence briefs, article Q&A, study comparison, draft discussion posts.
PHI-Zero: uses only article metadata. User questions screened by phi_guard.

Step 14: Quota + Citation Validation
- Cache hits do NOT consume quota
- Citations sanitized to subset of input PMIDs
- Never show hallucinated PMIDs
"""
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone, timedelta
from typing import Optional, List
import json
import uuid
import logging

from auth_utils import get_current_user
from utils.capabilities import require_premium, require_verified_peer
from utils.phi_guard import enforce_phi_guard
from utils.feature_flags import get_feature_flags
from utils.copilot_provider import create_copilot_provider
from utils.citation_validator import validate_citations, get_citation_warning
from services.grounded_article_context_service import (
    get_article_context as _get_article_context,
    build_article_text as _build_article_text,
    make_citation as _make_citation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])

db: AsyncIOMotorDatabase = None
_provider = None


def set_db(database: AsyncIOMotorDatabase):
    global db, _provider
    db = database
    _provider = create_copilot_provider()


DISCLAIMER = "AI-generated summary from abstract; verify in full paper."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# NOTE: _get_article_context, _build_article_text, _make_citation are imported
# from services.grounded_article_context_service (shared layer). The imported
# functions require a `db` argument; we wrap them for backward compatibility.


async def _fetch_article(pmid: str) -> Optional[dict]:
    """Backward-compat wrapper: calls shared get_article_context with module-level db."""
    return await _get_article_context(db, pmid)


async def _get_expertise_context(user_id: str) -> str:
    """Fetch precomputed expertise_summary if available. Returns empty string if not."""
    try:
        flags = get_feature_flags()
        if not flags.get("enable_litscholar_profile_memory"):
            return ""
        doc = await db.litscholar_state.find_one(
            {"user_id": user_id}, {"_id": 0, "expertise_summary": 1}
        )
        if doc and doc.get("expertise_summary"):
            return doc["expertise_summary"]
    except Exception as e:
        logger.debug("COPILOT: expertise context fetch skipped: %s", e)
    return ""


async def _check_quota(user_id: str):
    """Check and enforce copilot quota. Raises 429 if exceeded."""
    flags = get_feature_flags()
    if not flags.get("enforce_copilot_quota", False):
        return
    from utils.capabilities import compute_capabilities, derive_plan_tier
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0, "subscription_level": 1, "plan_tier": 1, "email": 1, "trial_ends_at": 1})
    caps = compute_capabilities(user or {}, feature_flags=flags)
    limit = caps.get("copilot_calls_per_24h", 50)
    window = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    count = await db.user_usage_events.count_documents({"user_id": user_id, "event_type": "copilot_call", "created_at": {"$gte": window}})
    if count >= limit:
        # Calculate seconds until reset (approximate: 1 hour minimum)
        retry_after = 3600
        raise HTTPException(status_code=429, detail={
            "error_code": "copilot_quota_exceeded",
            "message": f"Copilot limit ({limit}/day) reached. Try again later.",
            "retry_after_seconds": retry_after,
            "limit": limit,
            "used": count,
        })


async def _record_usage(user_id: str, surface: str = "unknown"):
    await db.user_usage_events.insert_one({
        "event_id": str(uuid.uuid4()),
        "user_id": user_id,
        "event_type": "copilot_call",
        "surface": surface,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # Track for analytics dashboard
    from utils.event_tracker import track_event
    await track_event("deepdive_request", user_id, {"surface": surface})


def _parse_json_response(text: str) -> dict:
    """Try to parse JSON from LLM response, handling markdown code blocks."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"raw_text": text}


import hashlib as _hashlib

def _cache_key(surface: str, pmids: list) -> str:
    """Deterministic cache key from surface + sorted pmids."""
    data = f"{surface}:{','.join(sorted(pmids))}"
    return _hashlib.sha256(data.encode()).hexdigest()


async def _cache_get(surface: str, pmids: list) -> Optional[dict]:
    """Check copilot_cache for a hit."""
    key = _cache_key(surface, pmids)
    now = datetime.now(timezone.utc).isoformat()
    doc = await db.copilot_cache.find_one(
        {"cache_key": key, "expires_at": {"$gt": now}}, {"_id": 0, "response_json": 1}
    )
    if doc:
        return doc.get("response_json")
    return None


async def _cache_set(surface: str, pmids: list, response: dict, ttl_days: int = 14):
    """Store in copilot_cache."""
    import os
    key = _cache_key(surface, pmids)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=ttl_days)).isoformat()
    await db.copilot_cache.update_one(
        {"cache_key": key},
        {"$set": {
            "cache_key": key, "surface": surface, "pmids": sorted(pmids),
            "model_provider": os.environ.get("COPILOT_PROVIDER", "mock"),
            "response_json": response, "expires_at": expires, "updated_at": now.isoformat(),
        }, "$setOnInsert": {"created_at": now.isoformat()}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class EvidenceBriefRequest(BaseModel):
    pmid: str
    use_expertise_context: bool = False

class AskArticleRequest(BaseModel):
    pmid: str
    question: str = Field(..., min_length=1, max_length=1000)
    use_expertise_context: bool = False

class CompareStudiesRequest(BaseModel):
    pmids: List[str] = Field(..., min_length=2, max_length=5)
    question_optional: Optional[str] = None
    use_expertise_context: bool = False

class DraftPostRequest(BaseModel):
    specialty_id: str
    thread_title_optional: Optional[str] = None
    pmids: List[str] = Field(..., min_length=1, max_length=5)
    tone: str = "neutral"
    prompt_optional: Optional[str] = None
    use_expertise_context: bool = False


# ---------------------------------------------------------------------------
# GET /api/copilot/health — smoke test for LLM provider reachability
# ---------------------------------------------------------------------------

@router.get("/health")
async def copilot_health():
    """Check whether the copilot LLM provider is reachable.
    
    Returns provider type and a simple connectivity check.
    Does NOT require authentication — safe for monitoring.
    Uses in-memory TTL cache (60s) to avoid repeated LLM calls.
    """
    from utils.health_cache import get_cached, set_cached
    import os as _os
    import time as _time

    CACHE_KEY = "copilot_health"

    # Return cached result if available
    cached = get_cached(CACHE_KEY)
    if cached is not None:
        return cached

    t0 = _time.perf_counter()
    provider_name = _os.environ.get("COPILOT_PROVIDER", "mock")
    flags = get_feature_flags()
    copilot_enabled = flags.get("enable_copilot", False)

    result = {
        "copilot_enabled": copilot_enabled,
        "provider": provider_name,
        "reachable": False,
        "error": None,
    }

    if provider_name == "mock":
        result["reachable"] = True
        result["note"] = "Using mock provider — no real LLM calls"
        set_cached(CACHE_KEY, result)
        return result

    if not copilot_enabled:
        result["note"] = "Copilot flag is OFF. Set ENABLE_COPILOT=true to activate."
        set_cached(CACHE_KEY, result)
        return result

    # Try a minimal LLM call to verify connectivity (with timeout)
    try:
        import asyncio as _asyncio
        test_response = await _asyncio.wait_for(
            _provider.generate("Respond with exactly: OK", "You are a test assistant. Respond with exactly one word: OK"),
            timeout=5.0  # 5 second timeout for health check
        )
        result["reachable"] = bool(test_response and len(test_response) > 0)
        result["latency_ms"] = round((_time.perf_counter() - t0) * 1000)
    except Exception as e:
        error_name = type(e).__name__
        if "TimeoutError" in error_name or "timeout" in str(e).lower():
            result["error"] = "timeout"
            result["note"] = "LLM provider is slow to respond (>5s). It may still be functional."
        else:
            result["error"] = error_name
        result["latency_ms"] = round((_time.perf_counter() - t0) * 1000)
        logger.warning("COPILOT_HEALTH: provider=%s error=%s latency=%dms", provider_name, result.get("error", "unknown"), result["latency_ms"])

    # Cache the result (even failures) for TTL period
    set_cached(CACHE_KEY, result)
    return result


# ---------------------------------------------------------------------------
# POST /api/copilot/evidence-brief
# ---------------------------------------------------------------------------

@router.post("/evidence-brief")
async def evidence_brief(data: EvidenceBriefRequest, current_user: dict = Depends(get_current_user)):
    flags = get_feature_flags()
    if not flags.get("enable_copilot"):
        raise HTTPException(status_code=503, detail={"error_code": "copilot_disabled", "message": "Copilot is currently unavailable."})
    await require_premium(current_user["user_id"], db)

    art = await _fetch_article(data.pmid)
    if not art:
        raise HTTPException(status_code=404, detail="Article not found")

    # Cache check FIRST — cache hits do NOT consume quota
    cached = await _cache_get("evidence_brief", [data.pmid])
    if cached:
        cached["cached"] = True
        cached["disclaimer"] = DISCLAIMER
        # Note: DO NOT record usage for cache hits
        return cached

    # Quota check ONLY for cache misses (before provider call)
    await _check_quota(current_user["user_id"])

    art_text = _build_article_text(art)
    system = """You are a medical literature analysis assistant. Respond ONLY in valid JSON.
Based on the provided article metadata and abstract, generate a structured evidence brief.
Never fabricate data. If information is not available, say "Not available from abstract."
Include the PMID as a citation."""

    # Optionally enrich with expertise context
    if data.use_expertise_context:
        expertise_ctx = await _get_expertise_context(current_user["user_id"])
        if expertise_ctx:
            system += expertise_ctx

    prompt = f"""Generate an evidence brief for this article:

{art_text}

Return JSON with this exact structure:
{{"title":"...","one_line_takeaway":"...","evidence_brief":{{"summary":"...","key_findings":["..."],"study_design":"...","population":"...","intervention_exposure":"...","outcomes":["..."],"limitations":["..."],"clinical_bottom_line":"..."}},"citations":[{{"pmid":"...","title":"...","journal":"...","pub_date":"..."}}]}}"""

    try:
        import time as _time
        _t0 = _time.perf_counter()
        raw = await _provider.generate(prompt, system)
        _dur = (_time.perf_counter() - _t0) * 1000
        logger.info("COPILOT: evidence-brief provider call %.0fms", _dur)
        result = _parse_json_response(raw)
    except Exception as e:
        logger.error("COPILOT: evidence-brief failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Copilot generation failed")

    # Citation validation: only allow the input PMID
    input_pmids = [data.pmid]
    sanitized_citations, citations_sanitized = await validate_citations(
        input_pmids, result.get("citations", []), db=db
    )
    
    # If no valid citations, use the article's citation
    if not sanitized_citations:
        sanitized_citations = [_make_citation(art)]
    
    result["citations"] = sanitized_citations
    result["citations_sanitized"] = citations_sanitized
    if citations_sanitized:
        result["citation_warning"] = get_citation_warning()
    
    result["disclaimer"] = DISCLAIMER
    result["cached"] = False

    # Store in cache
    await _cache_set("evidence_brief", [data.pmid], result)

    # Record usage AFTER successful provider call
    await _record_usage(current_user["user_id"], surface="evidence_brief")
    return result


# ---------------------------------------------------------------------------
# POST /api/copilot/ask-article
# ---------------------------------------------------------------------------

@router.post("/ask-article")
async def ask_article(data: AskArticleRequest, current_user: dict = Depends(get_current_user)):
    flags = get_feature_flags()
    if not flags.get("enable_copilot"):
        raise HTTPException(status_code=503, detail={"error_code": "copilot_disabled", "message": "Copilot is currently unavailable."})
    await require_premium(current_user["user_id"], db)

    # PHI guard on question
    enforce_phi_guard(text=data.question, endpoint="POST /api/copilot/ask-article", user_id=current_user["user_id"], mode=flags.get("phi_guard_mode", "block"), enabled=flags.get("enable_phi_guard", True))

    art = await _fetch_article(data.pmid)
    if not art:
        raise HTTPException(status_code=404, detail="Article not found")

    # Quota check before provider call (ask-article is never cached)
    await _check_quota(current_user["user_id"])

    art_text = _build_article_text(art)
    system = """You are a medical literature Q&A assistant. Answer ONLY based on the provided article.
If the answer cannot be determined from the abstract/summary, say so and suggest what to check in the full paper.
Never provide clinical advice. Respond in valid JSON."""

    # Optionally enrich with expertise context
    if data.use_expertise_context:
        expertise_ctx = await _get_expertise_context(current_user["user_id"])
        if expertise_ctx:
            system += expertise_ctx

    prompt = f"""Article context:
{art_text}

Question: {data.question}

Return JSON: {{"answer":"...","confidence":"low|medium|high","citations":[{{"pmid":"...","title":"..."}}],"what_to_check_in_full_text":["..."]}}"""

    try:
        import time as _time
        _t0 = _time.perf_counter()
        raw = await _provider.generate(prompt, system)
        _dur = (_time.perf_counter() - _t0) * 1000
        logger.info("COPILOT: ask-article provider call %.0fms", _dur)
        result = _parse_json_response(raw)
    except Exception as e:
        logger.error("COPILOT: ask-article failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Copilot generation failed")

    # Citation validation: only allow the input PMID
    input_pmids = [data.pmid]
    sanitized_citations, citations_sanitized = await validate_citations(
        input_pmids, result.get("citations", []), db=db
    )
    
    if not sanitized_citations:
        sanitized_citations = [_make_citation(art)]
    
    result["citations"] = sanitized_citations
    result["citations_sanitized"] = citations_sanitized
    if citations_sanitized:
        result["citation_warning"] = get_citation_warning()
    
    result["disclaimer"] = DISCLAIMER

    await _record_usage(current_user["user_id"], surface="ask_article")
    return result


# ---------------------------------------------------------------------------
# POST /api/copilot/compare-studies
# ---------------------------------------------------------------------------

@router.post("/compare-studies")
async def compare_studies(data: CompareStudiesRequest, current_user: dict = Depends(get_current_user)):
    flags = get_feature_flags()
    if not flags.get("enable_copilot"):
        raise HTTPException(status_code=503, detail={"error_code": "copilot_disabled", "message": "Copilot is currently unavailable."})
    await require_premium(current_user["user_id"], db)

    if data.question_optional:
        enforce_phi_guard(text=data.question_optional, endpoint="POST /api/copilot/compare-studies", user_id=current_user["user_id"], mode=flags.get("phi_guard_mode", "block"), enabled=flags.get("enable_phi_guard", True))

    # Cache check FIRST — cache hits do NOT consume quota (only when no user question)
    if not data.question_optional:
        cached = await _cache_get("compare_studies", data.pmids)
        if cached:
            cached["cached"] = True
            cached["disclaimer"] = DISCLAIMER
            # Note: DO NOT record usage for cache hits
            return cached

    # Quota check ONLY for cache misses
    await _check_quota(current_user["user_id"])

    articles = []
    for pmid in data.pmids:
        art = await _fetch_article(pmid)
        if art:
            articles.append(art)
    if len(articles) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 valid articles to compare")

    arts_text = "\n\n---\n\n".join(_build_article_text(a) for a in articles)
    extra_q = f"\nAdditional focus: {data.question_optional}" if data.question_optional else ""
    system = """You are a medical literature comparison assistant. Compare the provided studies.
Create a structured comparison table. Respond in valid JSON only."""

    # Optionally enrich with expertise context
    if data.use_expertise_context:
        expertise_ctx = await _get_expertise_context(current_user["user_id"])
        if expertise_ctx:
            system += expertise_ctx

    prompt = f"""Compare these {len(articles)} studies:{extra_q}

{arts_text}

Return JSON: {{"comparison_title":"...","table":{{"columns":["Study","Design","Population","Intervention","Key Outcome","Limitations"],"rows":[["...","...","...","...","...","..."]]}},"synthesis":"...","citations":[{{"pmid":"...","title":"..."}}]}}"""

    try:
        import time as _time
        _t0 = _time.perf_counter()
        raw = await _provider.generate(prompt, system)
        _dur = (_time.perf_counter() - _t0) * 1000
        logger.info("COPILOT: compare-studies provider call %.0fms", _dur)
        result = _parse_json_response(raw)
    except Exception as e:
        logger.error("COPILOT: compare-studies failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Copilot generation failed")

    # Citation validation: only allow the input PMIDs
    sanitized_citations, citations_sanitized = await validate_citations(
        data.pmids, result.get("citations", []), db=db
    )
    
    if not sanitized_citations:
        sanitized_citations = [_make_citation(a) for a in articles]
    
    result["citations"] = sanitized_citations
    result["citations_sanitized"] = citations_sanitized
    if citations_sanitized:
        result["citation_warning"] = get_citation_warning()
    
    result["disclaimer"] = DISCLAIMER
    result["cached"] = False

    # Store in cache (only when no user question)
    if not data.question_optional:
        await _cache_set("compare_studies", data.pmids, result)

    await _record_usage(current_user["user_id"], surface="compare_studies")
    return result


# ---------------------------------------------------------------------------
# POST /api/copilot/draft-discussion-post
# ---------------------------------------------------------------------------

@router.post("/draft-discussion-post")
async def draft_discussion_post(data: DraftPostRequest, current_user: dict = Depends(get_current_user)):
    flags = get_feature_flags()
    if not flags.get("enable_copilot"):
        raise HTTPException(status_code=503, detail={"error_code": "copilot_disabled", "message": "Copilot is currently unavailable."})
    await require_premium(current_user["user_id"], db)
    await require_verified_peer(current_user["user_id"], db)

    if data.thread_title_optional:
        enforce_phi_guard(text=data.thread_title_optional, endpoint="POST /api/copilot/draft-discussion-post", user_id=current_user["user_id"], mode=flags.get("phi_guard_mode", "block"), enabled=flags.get("enable_phi_guard", True))
    if data.prompt_optional:
        enforce_phi_guard(text=data.prompt_optional, endpoint="POST /api/copilot/draft-discussion-post", user_id=current_user["user_id"], mode=flags.get("phi_guard_mode", "block"), enabled=flags.get("enable_phi_guard", True))

    # Quota check before provider call (draft-post is never cached)
    await _check_quota(current_user["user_id"])

    articles = []
    for pmid in data.pmids:
        art = await _fetch_article(pmid)
        if art:
            articles.append(art)
    if not articles:
        raise HTTPException(status_code=400, detail="No valid articles found")

    arts_text = "\n\n---\n\n".join(_build_article_text(a) for a in articles)
    title_ctx = f"\nThread title: {data.thread_title_optional}" if data.thread_title_optional else ""
    user_prompt = f"\nUser direction: {data.prompt_optional}" if data.prompt_optional else ""
    system = """You are a medical literature discussion assistant. Draft a professional discussion post.
The post MUST start with: "Literature discussion only — no patient identifiers."
Frame as evidence discussion, not clinical advice. Cite PMIDs. Tone: """ + data.tone + "."

    # Optionally enrich with expertise context
    if data.use_expertise_context:
        expertise_ctx = await _get_expertise_context(current_user["user_id"])
        if expertise_ctx:
            system += expertise_ctx

    prompt = f"""Draft a discussion post for specialty: {data.specialty_id}{title_ctx}{user_prompt}

Based on these articles:
{arts_text}

Return JSON: {{"draft_post":"...","suggested_questions":["...","..."],"citations":[{{"pmid":"...","title":"..."}}]}}"""

    try:
        import time as _time
        _t0 = _time.perf_counter()
        raw = await _provider.generate(prompt, system)
        _dur = (_time.perf_counter() - _t0) * 1000
        logger.info("COPILOT: draft-post provider call %.0fms", _dur)
        result = _parse_json_response(raw)
    except Exception as e:
        logger.error("COPILOT: draft-post failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Copilot generation failed")

    # Citation validation: only allow the input PMIDs
    sanitized_citations, citations_sanitized = await validate_citations(
        data.pmids, result.get("citations", []), db=db
    )
    
    if not sanitized_citations:
        sanitized_citations = [_make_citation(a) for a in articles]
    
    result["citations"] = sanitized_citations
    result["citations_sanitized"] = citations_sanitized
    if citations_sanitized:
        result["citation_warning"] = get_citation_warning()
    
    result["disclaimer"] = DISCLAIMER

    await _record_usage(current_user["user_id"], surface="draft_post")
    return result
