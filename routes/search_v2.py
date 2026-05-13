"""
Explore Literature V2 — Backend Routes
Phase 4: ENABLE_EXPLORE_TOPIC_SEARCH_V2

Endpoints:
  POST /api/articles/search-v2      — Bucketed search with preference context
  GET  /api/articles/topic-suggest  — Typeahead topic suggestions

PHI-Zero:
  - Query strings are NEVER logged (only result counts, bucket names, timing)
  - Topic suggestions are logged as suggestion_count only, never raw query text
"""
from __future__ import annotations

import asyncio
import time
import logging
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search-v2"])
db = None  # injected by server.py


def set_db(database):
    global db
    db = database


# ---------------------------------------------------------------------------
# Study-type → PubMed publication-type mapping
# ---------------------------------------------------------------------------

STUDY_TYPE_LABELS: Dict[str, str] = {
    "systematic_review": "Systematic review / Meta-analysis",
    "guidelines":        "Guidelines",
    "review":            "Review articles",
    "rct":               "Randomized controlled trials",
    "observational":     "Observational / Cohort",
}

_PT_FILTER: Dict[str, str] = {
    "systematic_review": '"systematic review"[pt] OR "meta-analysis"[pt]',
    "guidelines":        '"practice guideline"[pt] OR "guideline"[pt]',
    "review":            '"review"[pt]',
    "rct":               '"randomized controlled trial"[pt]',
    "observational":     '"observational study"[pt] OR "cohort study"[pt]',
}

# Bucket priority when NO filters selected (index = priority, lower = higher priority)
BUCKET_PRIORITY: List[str] = [
    "systematic_review",
    "guidelines",
    "review",
    "rct",
    "observational",
]

# Lookback years → calendar days (0 = all time, capped at 20 years)
_LOOKBACK_DAYS: Dict[int, Optional[int]] = {
    0:  None,
    1:  365,
    3:  1095,
    5:  1825,
    10: 3650,
}
RECENT_DAYS = 1095  # 3 years — used for within-bucket tie-breaking

MAX_RESULTS_PER_BUCKET = 12
MAX_TOTAL_RESULTS = 50
MIN_FILL_THRESHOLD = 5  # fetch fallback query when pref-filtered yields fewer
SUGGEST_CACHE_TTL_S = 3600  # 1 hour
SUGGEST_RATE_LIMIT_S = 1.0  # min seconds between ESpell calls per cache miss

# In-memory LRU-ish suggestion cache: key → (timestamp, list[str])
_suggest_cache: OrderedDict = OrderedDict()
_SUGGEST_CACHE_MAX = 500


# ---------------------------------------------------------------------------
# Curated clinical topic list (fast, deterministic, no external call needed)
# ---------------------------------------------------------------------------

CLINICAL_TOPICS: List[str] = [
    "heart failure", "atrial fibrillation", "coronary artery disease", "hypertension",
    "diabetes mellitus type 2", "diabetes mellitus type 1", "obesity", "metabolic syndrome",
    "stroke", "ischemic stroke", "hemorrhagic stroke", "transient ischemic attack",
    "myocardial infarction", "acute coronary syndrome", "venous thromboembolism",
    "pulmonary embolism", "deep vein thrombosis", "chronic obstructive pulmonary disease",
    "asthma", "pneumonia", "COVID-19", "sepsis", "acute kidney injury",
    "chronic kidney disease", "chronic liver disease", "cirrhosis",
    "colorectal cancer", "breast cancer", "lung cancer", "prostate cancer",
    "non-hodgkin lymphoma", "acute myeloid leukemia", "multiple myeloma",
    "rheumatoid arthritis", "systemic lupus erythematosus", "inflammatory bowel disease",
    "crohn disease", "ulcerative colitis", "celiac disease",
    "alzheimer disease", "parkinson disease", "multiple sclerosis", "epilepsy",
    "depression", "anxiety disorder", "bipolar disorder", "schizophrenia",
    "hypothyroidism", "hyperthyroidism", "adrenal insufficiency", "cushing syndrome",
    "osteoporosis", "osteoarthritis", "gout", "fibromyalgia",
    "anemia", "sickle cell disease", "thrombocytopenia",
    "glaucoma", "age-related macular degeneration", "diabetic retinopathy",
    "kidney transplantation", "liver transplantation", "cardiac surgery",
    "antibiotic resistance", "staphylococcus aureus", "clostridium difficile",
    "influenza", "hepatitis B", "hepatitis C", "HIV", "tuberculosis",
    "preeclampsia", "gestational diabetes", "preterm birth",
    "sleep apnea", "insomnia", "restless leg syndrome",
    "chronic pain", "neuropathic pain", "palliative care",
    "preventive cardiology", "lipid lowering therapy", "statin therapy",
    "immunotherapy", "checkpoint inhibitors", "CAR-T cell therapy",
    "GLP-1 receptor agonists", "sodium-glucose cotransporter 2 inhibitors",
    "direct oral anticoagulants", "antiplatelet therapy",
    "mechanical ventilation", "acute respiratory distress syndrome",
    "gut microbiome", "fecal transplantation", "probiotics",
]


def _match_topics(q: str, limit: int = 8) -> List[str]:
    """Fast prefix + substring match against clinical topic list. Deterministic."""
    q_lower = q.lower().strip()
    if len(q_lower) < 2:
        return []
    prefix = [t for t in CLINICAL_TOPICS if t.startswith(q_lower)]
    substring = [t for t in CLINICAL_TOPICS if q_lower in t and t not in prefix]
    return (prefix + substring)[:limit]


# ---------------------------------------------------------------------------
# PubMed ESpell (optional enhancement, cached, PHI-Zero safe)
# ---------------------------------------------------------------------------

async def _espell_suggest(query: str) -> List[str]:
    """
    Call PubMed ESpell for a spell-corrected suggestion.
    Returns list of 0–1 items. Never logs the raw query.
    PHI-Zero: logs only suggestion_count.
    """
    import aiohttp
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/espell.fcgi"
    params = {"db": "pubmed", "term": query, "retmode": "json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                corrected = data.get("espellresult", {}).get("correctedquery", "")
                if corrected and corrected.lower() != query.lower():
                    return [corrected]
    except Exception:
        pass
    return []


async def get_topic_suggestions(q: str) -> List[str]:
    """
    Return topic suggestions for query `q`.
    1. Match against local clinical topic list (instant).
    2. Optionally enhance with ESpell (cached, rate-limited).
    PHI-Zero: does not log `q`.
    """
    # Check cache
    now = time.time()
    cache_key = q.lower().strip()[:80]  # normalize, cap length
    cached = _suggest_cache.get(cache_key)
    if cached and (now - cached[0]) < SUGGEST_CACHE_TTL_S:
        return cached[1]

    local = _match_topics(q, limit=8)

    # ESpell enhancement (non-blocking, best-effort)
    spell = []
    try:
        spell = await asyncio.wait_for(_espell_suggest(q), timeout=2.5)
    except Exception:
        pass

    # Merge: local matches first, then spell correction if not duplicate
    results = list(local)
    for s in spell:
        if s not in results:
            results.append(s)
    results = results[:8]

    # Update cache (evict oldest if full)
    if len(_suggest_cache) >= _SUGGEST_CACHE_MAX:
        _suggest_cache.popitem(last=False)
    _suggest_cache[cache_key] = (now, results)

    logger.debug("[SUGGEST] suggestion_count=%d", len(results))  # PHI-Zero: count only
    return results


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SearchV2Request(BaseModel):
    query: str = Field(..., min_length=1, max_length=300)
    use_preferences_context: bool = Field(True)
    study_types: List[str] = Field(default_factory=list)
    lookback_years: int = Field(0, ge=0, le=10)
    limit: int = Field(30, ge=1, le=50)

    class Config:
        json_schema_extra = {
            "example": {
                "query": "heart failure management",
                "use_preferences_context": True,
                "study_types": [],
                "lookback_years": 3,
                "limit": 30,
            }
        }


# ---------------------------------------------------------------------------
# Core search helpers
# ---------------------------------------------------------------------------

def _build_date_window(lookback_years: int):
    """Return (start_date, end_date) for the lookback window."""
    end = datetime.now(timezone.utc)
    days = _LOOKBACK_DAYS.get(lookback_years)
    if days is None:
        start = end - timedelta(days=7300)  # ~20 years cap
    else:
        start = end - timedelta(days=days)
    return start, end


def _build_pt_clause(study_types: List[str]) -> str:
    """Build PubMed publication-type clause from study type list."""
    parts = []
    for st in study_types:
        pt = _PT_FILTER.get(st)
        if pt:
            parts.append(f"({pt})")
    if not parts:
        return ""
    return " OR ".join(parts)


def _build_pref_context_query(query: str, preferences: dict) -> str:
    """
    Incorporate preferences into the search query.
    First pass: topic AND (spec_term1 OR spec_term2 OR custom_topic1 OR ...)
    PHI-Zero: this never logs query or pref terms.
    """
    terms = []
    if preferences:
        topics = preferences.get("topics_selected") or []
        custom = preferences.get("custom_topics") or []
        all_topics = list({t.strip() for t in (topics + custom) if t.strip()})[:10]
        if all_topics:
            escaped = [t.replace('"', '').replace('\\', '') for t in all_topics]
            terms = [f'"{t}"[Title/Abstract]' for t in escaped]

    if terms:
        pref_clause = " OR ".join(terms)
        return f"({query}) AND ({pref_clause})"
    return query


def _dedupe(articles: List[dict]) -> List[dict]:
    """Deduplicate articles by PMID, preserving order."""
    seen: set = set()
    result = []
    for a in articles:
        pid = a.get("pmid")
        if pid and pid not in seen:
            seen.add(pid)
            result.append(a)
    return result


def _is_recent(article: dict) -> bool:
    """True if article pub_date is within RECENT_DAYS (3 years)."""
    pub_date = article.get("pub_date") or ""
    if not pub_date:
        return False
    try:
        # Format: "YYYY Mon DD" or "YYYY-MM-DD" or "YYYY"
        year = int(pub_date[:4])
        cutoff = datetime.now(timezone.utc).year - 3
        return year >= cutoff
    except (ValueError, TypeError):
        return False


async def _run_bucket_query(
    query: str,
    bucket: str,
    start_date: datetime,
    end_date: datetime,
    max_results: int,
) -> List[dict]:
    """Run a single bucket query and return articles, recent ones first."""
    from agents import PubMedSearchAgent
    pt = _PT_FILTER.get(bucket, "")
    if pt:
        full_query = f"({query}) AND ({pt})"
    else:
        full_query = query

    agent = PubMedSearchAgent()
    # Fetch up to 2× to allow sorting recent/older
    raw = await agent.search(
        query=full_query,
        start_date=start_date,
        end_date=end_date,
        max_results=max_results * 2,
    )
    # Within bucket: recent articles first
    recent = [a for a in raw if _is_recent(a)]
    older = [a for a in raw if not _is_recent(a)]
    return (recent + older)[:max_results]


async def _bucketed_search(
    query: str,
    start_date: datetime,
    end_date: datetime,
    limit: int,
) -> List[dict]:
    """
    Run one query per bucket sequentially with rate limiting to avoid PubMed 429 errors.
    Deduplicates by PMID. Returns up to `limit` results.
    """
    per_bucket = max(MAX_RESULTS_PER_BUCKET, limit // len(BUCKET_PRIORITY) + 2)
    
    merged: List[dict] = []
    
    # Run buckets sequentially with rate limiting to avoid 429 errors
    for i, bucket in enumerate(BUCKET_PRIORITY):
        try:
            # Add delay between requests (except for first one)
            if i > 0:
                await asyncio.sleep(0.4)  # 400ms delay = ~2.5 req/sec (under 3/sec limit)
            
            result = await _run_bucket_query(query, bucket, start_date, end_date, per_bucket)
            for art in result:
                art["_bucket"] = bucket  # annotate for ordering
            merged.extend(result)
            
            # Early exit if we have enough results
            if len(_dedupe(merged)) >= limit:
                break
                
        except Exception as e:
            logger.debug("[SEARCH_V2] bucket=%s error=%s", bucket, type(e).__name__)
            continue

    deduped = _dedupe(merged)
    return deduped[:limit]


async def _filtered_search(
    query: str,
    study_types: List[str],
    start_date: datetime,
    end_date: datetime,
    limit: int,
) -> List[dict]:
    """
    Search with explicit study type filter(s). Sort by pub_date desc.
    """
    from agents import PubMedSearchAgent
    pt_clause = _build_pt_clause(study_types)
    full_query = f"({query}) AND ({pt_clause})" if pt_clause else query
    agent = PubMedSearchAgent()
    articles = await agent.search(
        query=full_query,
        start_date=start_date,
        end_date=end_date,
        max_results=limit,
    )
    # Sort by pub_date descending
    articles.sort(key=lambda a: a.get("pub_date") or "", reverse=True)
    return articles[:limit]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

@router.post("/articles/search-v2")
async def search_articles_v2(
    request: SearchV2Request,
    current_user: dict = Depends(get_current_user),
):
    """
    V2 article search with bucketed ordering + preference context.

    PHI-Zero: query string is never logged. Only counts and timing are logged.

    When NO study_types are selected: returns results in priority-bucket order
    (systematic reviews first, then guidelines, reviews, RCTs, observational).
    Recent articles (last 3 years) appear before older ones within each bucket.

    When study_types are specified: applies PT filter and sorts by pub_date desc.
    """
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    if not flags.get("enable_explore_topic_search_v2", False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "feature_disabled", "message": "Search V2 is not enabled."},
        )

    t0 = time.perf_counter()
    user_id = current_user["user_id"]

    # Validate study types
    valid_types = [st for st in request.study_types if st in _PT_FILTER]

    # Date window
    start_date, end_date = _build_date_window(request.lookback_years)

    # Preference context (PHI-Zero: query terms from prefs config, not user text)
    preferences = None
    pref_query = request.query  # default: bare query
    if request.use_preferences_context and db is not None:
        try:
            preferences = await db.preferences.find_one(
                {"user_id": user_id}, {"_id": 0}
            )
            if preferences:
                pref_query = _build_pref_context_query(request.query, preferences)
        except Exception:
            pass  # fail open: use bare query

    # Primary search
    articles: List[dict] = []
    if valid_types:
        articles = await _filtered_search(pref_query, valid_types, start_date, end_date, request.limit)
    else:
        articles = await _bucketed_search(pref_query, start_date, end_date, request.limit)

    # Fallback fill: if pref-contextualised query is sparse, top-up with bare query
    if request.use_preferences_context and preferences and len(articles) < MIN_FILL_THRESHOLD:
        if valid_types:
            fallback = await _filtered_search(request.query, valid_types, start_date, end_date, request.limit)
        else:
            fallback = await _bucketed_search(request.query, start_date, end_date, request.limit)
        combined = articles + fallback
        articles = _dedupe(combined)[: request.limit]

    # Skip AI summaries for search results - they're too slow for inline processing
    # AI summaries will be generated when articles are saved to library or viewed in detail
    # This keeps search fast and responsive
    # from digest_agents import SummarizationAgent
    # try:
    #     summarizer = SummarizationAgent()
    #     articles = await summarizer.summarize_articles(articles)
    # except Exception:
    #     pass  # summaries are best-effort

    dur_ms = (time.perf_counter() - t0) * 1000
    # PHI-Zero: log counts + timing only, never the query string
    logger.info(
        "[SEARCH_V2] user=%s results=%d buckets=%s lookback_years=%d dur=%.0fms",
        user_id,
        len(articles),
        "filtered" if valid_types else "bucketed",
        request.lookback_years,
        dur_ms,
    )

    # Clean internal fields from response
    for art in articles:
        art.pop("_bucket", None)

    return {
        "article_count": len(articles),
        "articles": articles,
        "search_context": {
            "preferences_applied": bool(request.use_preferences_context and preferences),
            "study_types": valid_types,
            "lookback_years": request.lookback_years,
            "bucket_mode": len(valid_types) == 0,
        },
    }


@router.get("/articles/v2/suggest")
async def topic_suggestions(
    q: str = Query(..., min_length=2, max_length=100),
    current_user: dict = Depends(get_current_user),
):
    """
    Return typeahead topic suggestions for the given partial query.
    URL is /articles/v2/suggest to avoid conflict with /articles/{article_id} param route.
    Results come from a curated clinical topic list + optional PubMed ESpell.
    Cached (1 hour) and rate-limited to protect the external dependency.
    PHI-Zero: query `q` is never logged; only suggestion_count is logged.
    """
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    if not flags.get("enable_explore_topic_search_v2", False):
        return {"suggestions": []}

    suggestions = await get_topic_suggestions(q)
    return {"suggestions": suggestions}
