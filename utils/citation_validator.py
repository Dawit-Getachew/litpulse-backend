"""
Citation Validation / Sanitization Utility for Copilot.
Ensures LLM-generated citations are valid subsets of input PMIDs.
Never displays hallucinated citations.
"""
from typing import List, Dict, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


async def validate_citations(
    input_pmids: List[str],
    citations: List[Dict[str, Any]],
    db=None,
    validate_in_db: bool = True,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Sanitize citations to ensure they are a valid subset of input PMIDs.
    
    Args:
        input_pmids: List of PMIDs that were provided as input to the Copilot endpoint.
        citations: List of citation dicts returned by the LLM (each should have 'pmid' key).
        db: Optional database connection for validating PMIDs exist in articles collection.
        validate_in_db: Whether to also check that PMIDs exist in local DB.
    
    Returns:
        Tuple of (sanitized_citations, citations_sanitized_flag)
        - sanitized_citations: Only citations whose PMIDs are in input_pmids (and optionally in DB)
        - citations_sanitized_flag: True if any citations were removed
    """
    if not citations:
        return [], False
    
    input_pmids_set = set(str(p).strip() for p in input_pmids if p)
    sanitized = []
    removed_count = 0
    
    # If DB validation is requested and db is available, get valid PMIDs
    valid_db_pmids = None
    if validate_in_db and db is not None:
        try:
            # Fetch which of the input PMIDs actually exist in our articles collection
            existing = await db.articles.find(
                {"pmid": {"$in": list(input_pmids_set)}},
                {"_id": 0, "pmid": 1}
            ).to_list(len(input_pmids_set))
            valid_db_pmids = set(doc.get("pmid") for doc in existing if doc.get("pmid"))
        except Exception as e:
            logger.warning("Citation validation DB check failed: %s", type(e).__name__)
            valid_db_pmids = None
    
    for citation in citations:
        if not isinstance(citation, dict):
            removed_count += 1
            continue
        
        pmid = str(citation.get("pmid", "")).strip()
        if not pmid:
            removed_count += 1
            continue
        
        # Check if PMID is in the input set
        if pmid not in input_pmids_set:
            removed_count += 1
            logger.info("CITATION_SANITIZED: Removed hallucinated PMID=%s not in input", pmid)
            continue
        
        # Optionally check if PMID exists in DB
        if valid_db_pmids is not None and pmid not in valid_db_pmids:
            removed_count += 1
            logger.info("CITATION_SANITIZED: Removed PMID=%s not found in DB", pmid)
            continue
        
        sanitized.append(citation)
    
    citations_sanitized = removed_count > 0
    
    if citations_sanitized:
        logger.info("CITATION_SANITIZED: Removed %d invalid citation(s) from response", removed_count)
    
    return sanitized, citations_sanitized


def get_citation_warning() -> str:
    """Return the standard warning message when citations were sanitized."""
    return "Some citations were removed because they didn't match the selected articles."
