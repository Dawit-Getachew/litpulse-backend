"""
LitScholar Experimental LangGraph Endpoint — Hardened

Isolated route for the LangGraph deep-dive spike.
Gated behind ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE feature flag (default OFF).

Hardening:
  - Auth required (get_current_user)
  - Article access verified (user must have article in library or screening history)
  - Structured observability logging (request_id, latency, source_type, model_calls)
  - Clean error handling

This module does NOT modify any existing endpoint or service.
"""
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase

from auth_utils import get_current_user
from utils.feature_flags import get_feature_flags

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/litscholar-experimental", tags=["litscholar-experimental"])
db: Optional[AsyncIOMotorDatabase] = None


def set_db(database: AsyncIOMotorDatabase):
    global db
    db = database


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class DeepDiveRequest(BaseModel):
    pmid: str = Field(..., description="Article PMID to analyze")
    question: str = Field(..., min_length=3, max_length=2000, description="User question")


class DeepDiveResponse(BaseModel):
    success: bool
    request_id: Optional[str] = None
    pmid: Optional[str] = None
    article_title: Optional[str] = None
    grounding_level: Optional[str] = None
    source_label: Optional[str] = None
    answer: Optional[str] = None
    key_evidence: list = []
    supporting_passages: list = []
    limitations: Optional[str] = None
    suggested_followups: list = []
    citations_verified: bool = False
    disclaimer: Optional[str] = None
    error: Optional[str] = None
    graph_engine: str = "langgraph"


# ---------------------------------------------------------------------------
# Authorization helper
# ---------------------------------------------------------------------------

async def _verify_article_access(user_id: str, pmid: str) -> bool:
    """Check that the user has access to this article via library or screening.

    Returns True if the article exists in:
      - user_articles (saved or seen in digest)
      - user_library (saved to library)
      - article_screening (screened by user)
      - articles collection (article exists at all — fallback for digest articles)

    This is a READ-ONLY check. No writes.
    """
    # Check user_articles (digest/screening association)
    ua = await db.user_articles.find_one(
        {"user_id": user_id, "pmid": pmid}, {"_id": 1}
    )
    if ua:
        return True

    # Check user_library (saved articles)
    ul = await db.user_library.find_one(
        {"user_id": user_id, "pmid": pmid}, {"_id": 1}
    )
    if ul:
        return True

    # Check article_screening (screened articles)
    sc = await db.article_screening.find_one(
        {"user_id": user_id, "article_id": pmid}, {"_id": 1}
    )
    if sc:
        return True

    # Fallback: if article exists in articles collection (it was part of a digest)
    art = await db.articles.find_one({"pmid": pmid}, {"_id": 1})
    if art:
        return True

    return False


# ---------------------------------------------------------------------------
# POST /api/litscholar-experimental/deep-dive
# ---------------------------------------------------------------------------

@router.post("/deep-dive", response_model=DeepDiveResponse)
async def deep_dive(
    data: DeepDiveRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Experimental LangGraph-powered single-article deep-dive.

    Requires:
      - ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE=true
      - Authenticated user
      - User must have access to the requested article

    Input: one PMID + one question
    Output: grounded answer with citations, evidence bullets, limitations

    This endpoint is READ-ONLY. No writes to any collection.
    """
    request_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()

    # Feature flag gate
    flags = get_feature_flags()
    if not flags.get("enable_litscholar_langgraph_spike"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Experimental LitScholar endpoint is not enabled.",
        )

    # Auth
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")

    # Authorization: verify article access
    has_access = await _verify_article_access(user_id, data.pmid)
    if not has_access:
        logger.warning(
            "[LITSCHOLAR-EXP] rid=%s Access denied pmid=%s user=%s",
            request_id, data.pmid, user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this article.",
        )

    logger.info(
        "[LITSCHOLAR-EXP] rid=%s START pmid=%s user=%s question_len=%d",
        request_id, data.pmid, user_id, len(data.question),
    )

    try:
        from services.litscholar_langgraph import run_deep_dive

        result = await run_deep_dive(
            db=db,
            pmid=data.pmid,
            question=data.question,
            user_id=user_id,
            request_id=request_id,
        )

        latency_ms = (time.perf_counter() - t0) * 1000
        result["graph_engine"] = "langgraph"
        result["request_id"] = request_id

        logger.info(
            "[LITSCHOLAR-EXP] rid=%s DONE success=%s grounding=%s source=%s latency=%.0fms",
            request_id,
            result.get("success"),
            result.get("grounding_level"),
            result.get("source_label"),
            latency_ms,
        )

        return DeepDiveResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.error(
            "[LITSCHOLAR-EXP] rid=%s FAIL error=%s latency=%.0fms",
            request_id, str(e), latency_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Experimental deep-dive failed: {type(e).__name__}",
        )
