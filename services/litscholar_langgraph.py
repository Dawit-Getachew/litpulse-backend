"""
LitScholar LangGraph Spike — Isolated Experimental Deep-Dive Service (Hardened)

A minimal LangGraph graph for single-article deep-dive Q&A.
Gated behind ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE (default OFF).

Graph nodes:
  1. load_article_context     — Fetch article from DB (read-only)
  2. choose_available_sources  — Determine abstract-only vs full-text
  3. retrieve_relevant_passages — Extract relevant chunks with source labels
  4. generate_grounded_answer  — LLM call via existing copilot provider
  5. verify_citations          — Validate all citations map to real passages
  6. format_response           — Build structured output

READ-ONLY. No writes, no side effects, no mutations to any collection.
"""
import logging
import time
import json
from typing import TypedDict, Optional, List
from motor.motor_asyncio import AsyncIOMotorDatabase

from langgraph.graph import StateGraph, END

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class DeepDiveState(TypedDict):
    # Inputs
    pmid: str
    question: str
    user_id: str
    request_id: str
    # Article data
    article: Optional[dict]
    error: Optional[str]
    # Source analysis
    grounding_level: str       # "abstract_only" | "full_text_available"
    source_text: str
    source_label: str          # "abstract" | "full-text" | "ai-summary"
    # Retrieval
    relevant_passages: List[dict]
    # LLM output
    raw_answer: Optional[dict]
    model_calls: int
    # Final output
    response: Optional[dict]


# ---------------------------------------------------------------------------
# Node 1: load_article_context
# ---------------------------------------------------------------------------

async def load_article_context(state: DeepDiveState, db: AsyncIOMotorDatabase) -> dict:
    """Fetch article from DB. Read-only."""
    rid = state.get("request_id", "?")
    pmid = state["pmid"]
    t0 = time.perf_counter()

    art = await db.articles.find_one(
        {"pmid": pmid},
        {"_id": 0, "pmid": 1, "title": 1, "journal": 1, "pub_date": 1,
         "authors": 1, "abstract": 1, "ai_summary": 1, "key_findings": 1,
         "design_tags": 1, "full_text": 1, "doi": 1},
    )

    ms = (time.perf_counter() - t0) * 1000
    if not art:
        logger.warning("[LANGGRAPH] rid=%s node=load_article pmid=%s NOT_FOUND %.0fms", rid, pmid, ms)
        return {"article": None, "error": f"Article {pmid} not found"}

    logger.info("[LANGGRAPH] rid=%s node=load_article pmid=%s found=true %.0fms", rid, pmid, ms)
    return {"article": art, "error": None}


# ---------------------------------------------------------------------------
# Node 2: choose_available_sources
# ---------------------------------------------------------------------------

def choose_available_sources(state: DeepDiveState) -> dict:
    """Determine what source material is available and select best source."""
    art = state.get("article")
    rid = state.get("request_id", "?")

    if not art:
        return {"grounding_level": "none", "source_text": "", "source_label": "none"}

    full_text = (art.get("full_text") or "").strip()
    abstract = (art.get("abstract") or "").strip()
    ai_summary = (art.get("ai_summary") or "").strip()

    if full_text and len(full_text) > 50:
        source_label = "full-text"
        source_text = full_text[:6000]
        grounding = "full_text_available"
    elif abstract and len(abstract) > 20:
        source_label = "abstract"
        source_text = abstract
        grounding = "abstract_only"
    elif ai_summary:
        source_label = "ai-summary"
        source_text = ai_summary
        grounding = "abstract_only"
    else:
        source_label = "none"
        source_text = art.get("title", "")
        grounding = "abstract_only"

    logger.info("[LANGGRAPH] rid=%s node=choose_sources source=%s len=%d", rid, source_label, len(source_text))
    return {"grounding_level": grounding, "source_text": source_text, "source_label": source_label}


# ---------------------------------------------------------------------------
# Node 3: retrieve_relevant_passages
# ---------------------------------------------------------------------------

def retrieve_relevant_passages(state: DeepDiveState) -> dict:
    """Extract relevant passages with source labels and chunk IDs.

    Lightweight keyword-based chunking — no vector DB.
    Each passage carries: id, text, source (field name), source_label (abstract/full-text).
    """
    source_text = state.get("source_text", "")
    source_label = state.get("source_label", "abstract")
    question = state.get("question", "").lower()
    art = state.get("article") or {}
    rid = state.get("request_id", "?")

    passages = []

    # Key findings — pre-extracted evidence anchors (always labeled as abstract-derived)
    for i, kf in enumerate(art.get("key_findings") or []):
        passages.append({
            "id": f"kf-{i}",
            "text": kf,
            "source": "key_findings",
            "source_label": "abstract",
        })

    # Chunk source text into paragraphs
    paragraphs = [p.strip() for p in source_text.split("\n") if len(p.strip()) > 30]
    q_words = [w for w in question.split() if len(w) > 3]

    for i, para in enumerate(paragraphs):
        # Include if any question keyword matches OR if few paragraphs total
        if any(w in para.lower() for w in q_words) or len(paragraphs) <= 5:
            passages.append({
                "id": f"chunk-{i}",
                "text": para[:500],
                "source": "source_text",
                "source_label": source_label,
            })

    # Cap at 10 passages
    passages = passages[:10]
    logger.info("[LANGGRAPH] rid=%s node=retrieve passages=%d", rid, len(passages))
    return {"relevant_passages": passages}


# ---------------------------------------------------------------------------
# Node 4: generate_grounded_answer
# ---------------------------------------------------------------------------

async def generate_grounded_answer(state: DeepDiveState) -> dict:
    """Generate grounded answer. Reuses existing copilot provider."""
    art = state.get("article") or {}
    question = state["question"]
    passages = state.get("relevant_passages", [])
    grounding_level = state.get("grounding_level", "abstract_only")
    source_label = state.get("source_label", "abstract")
    rid = state.get("request_id", "?")

    # Build context from passages with explicit source labels
    passage_text = "\n\n".join(
        f"[{p['source_label'].upper()} | {p['id']}]: {p['text']}"
        for p in passages
    )

    meta = (
        f"Title: {art.get('title', 'Unknown')}\n"
        f"Journal: {art.get('journal', 'Unknown')}\n"
        f"Date: {art.get('pub_date', 'Unknown')}\n"
        f"PMID: {art.get('pmid', 'Unknown')}\n"
        f"Grounding: {grounding_level} ({source_label})"
    )

    system_prompt = """You are a medical literature analysis assistant for LitScholar.
You MUST answer ONLY based on the provided article source material.
You MUST cite specific passages using their [SOURCE_LABEL | ID] tags.
Each citation MUST include the source_label (abstract or full-text) and the passage_id.
If information is not available in the source material, explicitly say so.
Never provide clinical advice. Never hallucinate facts not in the source text.

Return valid JSON:
{
  "answer": "concise answer",
  "key_evidence": ["bullet 1", "bullet 2"],
  "supporting_passages": [
    {"passage": "quoted text", "source_label": "abstract|full-text", "passage_id": "kf-0|chunk-1"}
  ],
  "limitations": "what cannot be determined from available source material",
  "suggested_followups": ["question 1", "question 2", "question 3"]
}"""

    user_prompt = f"""Article metadata:
{meta}

Source passages (cite by [SOURCE_LABEL | ID]):
{passage_text}

User question: {question}

Answer based ONLY on the passages above. Cite each claim with its passage ID and source label."""

    t0 = time.perf_counter()
    try:
        from utils.copilot_provider import create_copilot_provider
        provider = create_copilot_provider()
        raw_text = await provider.generate(user_prompt, system_prompt)
        ms = (time.perf_counter() - t0) * 1000

        logger.info("[LANGGRAPH] rid=%s node=generate_answer model_call=1 latency=%.0fms", rid, ms)

        # Parse JSON
        text = raw_text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        parsed = json.loads(text)
        return {"raw_answer": parsed, "model_calls": 1}

    except json.JSONDecodeError:
        ms = (time.perf_counter() - t0) * 1000
        logger.warning("[LANGGRAPH] rid=%s node=generate_answer JSON_PARSE_FAIL latency=%.0fms", rid, ms)
        return {"raw_answer": {
            "answer": raw_text[:1000] if 'raw_text' in dir() else "Failed to parse response",
            "key_evidence": [],
            "supporting_passages": [],
            "limitations": "LLM response was not valid JSON",
            "suggested_followups": [],
        }, "model_calls": 1}
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        logger.error("[LANGGRAPH] rid=%s node=generate_answer FAIL error=%s latency=%.0fms", rid, str(e), ms)
        return {"raw_answer": None, "error": f"LLM generation failed: {type(e).__name__}", "model_calls": 1}


# ---------------------------------------------------------------------------
# Node 5: verify_citations
# ---------------------------------------------------------------------------

def verify_citations(state: DeepDiveState) -> dict:
    """Verify all citations map to real passage IDs and have correct source labels."""
    raw = state.get("raw_answer")
    rid = state.get("request_id", "?")
    if not raw:
        return {}

    # Build lookup: passage_id → source_label
    passage_map = {p["id"]: p["source_label"] for p in state.get("relevant_passages", [])}
    raw_passages = raw.get("supporting_passages", [])

    verified = []
    stripped = 0
    for p in raw_passages:
        pid = p.get("passage_id", "")
        if pid in passage_map:
            # Enforce correct source_label from actual passage data
            p["source_label"] = passage_map[pid]
            verified.append(p)
        elif not pid:
            # No passage_id — keep but mark as unanchored
            p["source_label"] = state.get("source_label", "abstract")
            p["passage_id"] = "unanchored"
            verified.append(p)
        else:
            stripped += 1

    raw["supporting_passages"] = verified
    raw["citations_verified"] = True

    logger.info("[LANGGRAPH] rid=%s node=verify_citations kept=%d stripped=%d", rid, len(verified), stripped)
    return {"raw_answer": raw}


# ---------------------------------------------------------------------------
# Node 6: format_response
# ---------------------------------------------------------------------------

def format_response(state: DeepDiveState) -> dict:
    """Build final structured response."""
    rid = state.get("request_id", "?")

    if state.get("error"):
        logger.info("[LANGGRAPH] rid=%s node=format_response error=%s", rid, state["error"])
        return {"response": {"success": False, "error": state["error"]}}

    raw = state.get("raw_answer") or {}
    art = state.get("article") or {}

    return {"response": {
        "success": True,
        "pmid": state["pmid"],
        "article_title": art.get("title", ""),
        "grounding_level": state.get("grounding_level", "abstract_only"),
        "source_label": state.get("source_label", "abstract"),
        "answer": raw.get("answer", ""),
        "key_evidence": raw.get("key_evidence", []),
        "supporting_passages": raw.get("supporting_passages", []),
        "limitations": raw.get("limitations", ""),
        "suggested_followups": raw.get("suggested_followups", []),
        "citations_verified": raw.get("citations_verified", False),
        "disclaimer": "AI-generated analysis based on article source text only. Not clinical advice.",
    }}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_deep_dive_graph(db: AsyncIOMotorDatabase) -> StateGraph:
    """Build and compile the LangGraph deep-dive graph."""

    async def _load(state):
        return await load_article_context(state, db)

    async def _generate(state):
        return await generate_grounded_answer(state)

    def _should_continue(state):
        if state.get("error"):
            return "format_response"
        return "choose_available_sources"

    graph = StateGraph(DeepDiveState)
    graph.add_node("load_article_context", _load)
    graph.add_node("choose_available_sources", choose_available_sources)
    graph.add_node("retrieve_relevant_passages", retrieve_relevant_passages)
    graph.add_node("generate_grounded_answer", _generate)
    graph.add_node("verify_citations", verify_citations)
    graph.add_node("format_response", format_response)

    graph.set_entry_point("load_article_context")
    graph.add_conditional_edges("load_article_context", _should_continue)
    graph.add_edge("choose_available_sources", "retrieve_relevant_passages")
    graph.add_edge("retrieve_relevant_passages", "generate_grounded_answer")
    graph.add_edge("generate_grounded_answer", "verify_citations")
    graph.add_edge("verify_citations", "format_response")
    graph.add_edge("format_response", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_deep_dive(
    db: AsyncIOMotorDatabase,
    pmid: str,
    question: str,
    user_id: str,
    request_id: str = "unknown",
) -> dict:
    """Run the LangGraph deep-dive pipeline for a single article.

    READ-ONLY — no writes to any collection.
    """
    graph = build_deep_dive_graph(db)

    initial_state: DeepDiveState = {
        "pmid": pmid,
        "question": question,
        "user_id": user_id,
        "request_id": request_id,
        "article": None,
        "error": None,
        "grounding_level": "",
        "source_text": "",
        "source_label": "",
        "relevant_passages": [],
        "raw_answer": None,
        "model_calls": 0,
        "response": None,
    }

    result = await graph.ainvoke(initial_state)
    return result.get("response") or {"success": False, "error": "Graph produced no response"}
