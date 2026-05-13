"""
Grounded Article Context Service

Shared service that builds normalized, evidence-grounded source packets from
article data already present in LitPulse. Used by both audio summary pipelines
and LitScholar (rebranded Copilot) flows.

Grounding Rules:
  - ONLY uses article metadata/text already stored in LitPulse (title, abstract,
    ai_summary, key_findings, design_tags, mesh_terms).
  - Does NOT retrieve or inject outside medical knowledge.
  - Marks each packet with grounding_level: "abstract_only" or "full_text_available".
  - When source text is insufficient, returns explicit insufficient-information markers.

PHI-Zero: operates on article metadata only; no user-entered content is processed here.
"""
import logging
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

# Maximum articles per request (consistent across audio + LitScholar)
MAX_ARTICLES = 5

# Fields we project from the articles collection
_ARTICLE_PROJECTION = {
    "_id": 0,
    "pmid": 1,
    "title": 1,
    "journal": 1,
    "pub_date": 1,
    "abstract": 1,
    "authors": 1,
    "ai_summary": 1,
    "key_findings": 1,
    "design_tags": 1,
    "mesh_terms": 1,
    "full_text": 1,       # may not exist in most docs
    "doi": 1,
}


async def get_article_context(db: AsyncIOMotorDatabase, pmid: str) -> Optional[dict]:
    """
    Fetch a single article's metadata from the articles collection.

    Returns the raw article dict (no _id) or None if not found.
    This is the canonical article-fetch used by all grounded flows.
    """
    return await db.articles.find_one(
        {"pmid": pmid},
        _ARTICLE_PROJECTION,
    )


def build_article_text(art: dict) -> str:
    """
    Build a plain-text representation of an article for LLM prompts.

    Includes only data that exists in the article document — never injects
    outside knowledge. Used by Copilot/LitScholar prompts and audio scripts.
    """
    parts = [f"PMID: {art.get('pmid', 'N/A')}"]
    parts.append(f"Title: {art.get('title', 'Untitled')}")
    if art.get("journal"):
        parts.append(f"Journal: {art['journal']}")
    if art.get("pub_date"):
        parts.append(f"Date: {art['pub_date']}")
    if art.get("authors"):
        parts.append(f"Authors: {art['authors']}")
    if art.get("design_tags"):
        parts.append(f"Design: {', '.join(art['design_tags'])}")
    if art.get("abstract"):
        parts.append(f"Abstract: {art['abstract']}")
    if art.get("ai_summary"):
        parts.append(f"AI Summary: {art['ai_summary']}")
    if art.get("key_findings"):
        parts.append(f"Key Findings: {'; '.join(art['key_findings'])}")
    if art.get("full_text"):
        parts.append(f"Full Text Excerpt: {art['full_text'][:3000]}")
    return "\n".join(parts)


def make_citation(art: dict) -> dict:
    """Build a minimal citation dict from an article document."""
    return {
        "pmid": art.get("pmid", ""),
        "title": art.get("title", ""),
        "journal": art.get("journal", ""),
        "pub_date": art.get("pub_date", ""),
    }


def _determine_grounding_level(art: dict) -> str:
    """Determine whether we have full text or only abstract-level data."""
    if art.get("full_text"):
        return "full_text_available"
    return "abstract_only"


def _extract_evidence_anchors(art: dict) -> List[dict]:
    """
    Extract evidence anchors (source spans) from available article text.

    Each anchor references a specific piece of evidence found in the source text.
    If only title+abstract are available, anchors are derived from the abstract.
    """
    anchors = []
    abstract = art.get("abstract", "")
    key_findings = art.get("key_findings") or []

    # Key findings are the strongest anchors (already extracted from abstract)
    for i, finding in enumerate(key_findings):
        anchors.append({
            "anchor_id": f"kf-{i}",
            "source": "key_findings",
            "text": finding,
            "pmid": art.get("pmid", ""),
        })

    # If no key findings but we have an abstract, use abstract as a single anchor
    if not anchors and abstract:
        anchors.append({
            "anchor_id": "abs-0",
            "source": "abstract",
            "text": abstract[:500],
            "pmid": art.get("pmid", ""),
        })

    return anchors


def _extract_from_text(art: dict, field: str) -> List[str]:
    """
    Return existing extracted data for a field, or an insufficient-info marker.

    For key_findings and limitations — if the article document already has them
    stored (from the digest pipeline), return them. Otherwise return a marker
    indicating the data is not available from the source text.
    """
    existing = art.get(field)
    if existing and isinstance(existing, list) and len(existing) > 0:
        return existing
    return ["Insufficient information in available article text."]


def build_source_packet(art: dict) -> dict:
    """
    Build a normalized, grounded source packet for one article.

    This is the canonical data structure consumed by audio combined summary
    and LitScholar preset flows. Every field is derived only from stored
    article data — never from outside knowledge.
    """
    grounding_level = _determine_grounding_level(art)
    available_text = art.get("full_text") or art.get("abstract") or ""

    return {
        "pmid": art.get("pmid", ""),
        "title": art.get("title", "Untitled"),
        "citation_metadata": {
            "journal": art.get("journal", ""),
            "pub_date": art.get("pub_date", ""),
            "authors": art.get("authors", ""),
            "doi": art.get("doi", ""),
        },
        "available_article_text": available_text,
        "study_type": (art.get("design_tags") or ["Unknown"])[0],
        "key_findings": _extract_from_text(art, "key_findings"),
        "limitations": _extract_from_text(art, "limitations"),
        "evidence_anchors": _extract_evidence_anchors(art),
        "grounding_level": grounding_level,
    }


async def build_grounded_context(
    db: AsyncIOMotorDatabase,
    pmids: List[str],
) -> dict:
    """
    Build grounded context for 1-5 articles.

    Returns:
        {
            "source_packets": [...],       # one per found article
            "missing_pmids": [...],        # PMIDs not found in LitPulse
            "overall_grounding_level": "abstract_only" | "mixed" | "full_text_available",
            "article_count": int,
            "article_texts": str,          # combined text for LLM prompts
        }

    Raises ValueError if pmids list is empty or exceeds MAX_ARTICLES.
    """
    if not pmids:
        raise ValueError("At least 1 PMID is required.")
    if len(pmids) > MAX_ARTICLES:
        raise ValueError(f"Maximum {MAX_ARTICLES} articles allowed, got {len(pmids)}.")

    source_packets = []
    missing_pmids = []
    article_texts = []
    grounding_levels = set()

    for pmid in pmids:
        art = await get_article_context(db, pmid)
        if not art:
            missing_pmids.append(pmid)
            continue

        packet = build_source_packet(art)
        source_packets.append(packet)
        article_texts.append(build_article_text(art))
        grounding_levels.add(packet["grounding_level"])

    # Determine overall grounding level
    if not grounding_levels:
        overall = "abstract_only"
    elif len(grounding_levels) == 1:
        overall = grounding_levels.pop()
    else:
        overall = "mixed"

    return {
        "source_packets": source_packets,
        "missing_pmids": missing_pmids,
        "overall_grounding_level": overall,
        "article_count": len(source_packets),
        "article_texts": "\n\n---\n\n".join(article_texts),
    }
