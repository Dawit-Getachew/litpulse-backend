"""
Workspace Shell V1 — Bootstrap and routing support APIs.

Provides context-aware routing data for the new task-first workspace shell.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
import logging
import asyncio
import os

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspace", tags=["workspace"])

# Global db reference - will be set by set_db()
_db = None

async def _regenerate_ai_summaries(db, pmids: List[str]):
    """Background task to regenerate AI summaries for articles with valid abstracts."""
    try:
        from digest_agents import SummarizationAgent
        
        summarizer = SummarizationAgent()
        
        for pmid in pmids[:5]:  # Limit to 5 at a time to avoid overload
            try:
                article = await db.articles.find_one({"pmid": pmid})
                if not article:
                    continue
                    
                abstract = article.get("abstract", "")
                if not abstract or abstract.lower() in ["no abstract available", "abstract not available"]:
                    continue
                
                # Generate summary for this article
                summary_data = await summarizer._generate_summary({
                    "pmid": pmid,
                    "title": article.get("title", ""),
                    "abstract": abstract,
                    "journal": article.get("journal", "")
                })
                
                # Update the article in the database
                if summary_data and summary_data.get("summary"):
                    await db.articles.update_one(
                        {"pmid": pmid},
                        {"$set": {
                            "ai_summary": summary_data.get("summary", ""),
                            "key_findings": summary_data.get("key_findings", []),
                            "population": summary_data.get("population", ""),
                            "study_size": summary_data.get("study_size", ""),
                            "key_questions": summary_data.get("key_questions", ""),
                            "updated_at": datetime.now(timezone.utc).isoformat()
                        }}
                    )
                    logger.info(f"Regenerated AI summary for article {pmid}")
                
                # Rate limit
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Failed to regenerate summary for {pmid}: {str(e)}")
                continue
                
    except Exception as e:
        logger.error(f"Error in background AI summary regeneration: {str(e)}")


class RecentArticle(BaseModel):
    pmid: str
    title: str
    saved_at: Optional[str] = None


class RecentDigest(BaseModel):
    digest_id: str
    generated_at: Optional[str] = None
    article_count: int = 0


class WorkspaceBootstrap(BaseModel):
    """Context for smart workspace routing."""
    has_preferences: bool = False
    unscreened_count: int = 0
    latest_digest_id: Optional[str] = None
    has_library_items: bool = False
    library_count: int = 0
    recent_articles: List[RecentArticle] = []
    recent_digests: List[RecentDigest] = []


@router.get("/bootstrap", response_model=WorkspaceBootstrap)
async def get_workspace_bootstrap(current_user: dict = Depends(get_current_user)):
    """
    Get workspace bootstrap data for smart routing.
    
    Used by WorkspaceHomeRouter to determine where to send the user:
    - No preferences → /onboarding/preferences
    - Unscreened digest items → /screen
    - Has library but no unscreened → /learn
    - No library items → /search
    """
    from server import db
    
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    try:
        # Check if user has preferences set up
        prefs = await db.preferences.find_one({"user_id": user_id})
        has_preferences = bool(prefs and prefs.get("specialty_id"))
        
        # Get library count
        library_count = await db.library.count_documents({"user_id": user_id})
        has_library_items = library_count > 0
        
        # Get recent library articles (last 5)
        recent_articles = []
        if has_library_items:
            cursor = db.library.find(
                {"user_id": user_id},
                {"pmid": 1, "title": 1, "saved_at": 1}
            ).sort("saved_at", -1).limit(5)
            
            async for doc in cursor:
                saved_at = doc.get("saved_at")
                # Handle both datetime objects and ISO strings
                if saved_at:
                    saved_at_str = saved_at.isoformat() if hasattr(saved_at, 'isoformat') else str(saved_at)
                else:
                    saved_at_str = None
                recent_articles.append(RecentArticle(
                    pmid=doc.get("pmid", ""),
                    title=doc.get("title", "Untitled"),
                    saved_at=saved_at_str
                ))
        
        # Get recent digests and count unscreened articles
        recent_digests = []
        unscreened_count = 0
        latest_digest_id = None
        
        # Get user's digests (most recent first)
        digest_cursor = db.digests.find(
            {"user_id": user_id, "status": {"$in": ["completed", "sent"]}}
        ).sort("generated_at", -1).limit(5)
        
        async for digest in digest_cursor:
            digest_id = digest.get("digest_id")
            if not latest_digest_id:
                latest_digest_id = digest_id
            
            generated_at = digest.get("generated_at")
            article_count = digest.get("article_count", 0)
            
            # Handle both datetime objects and ISO strings
            if generated_at:
                generated_at_str = generated_at.isoformat() if hasattr(generated_at, 'isoformat') else str(generated_at)
            else:
                generated_at_str = None
            
            recent_digests.append(RecentDigest(
                digest_id=digest_id,
                generated_at=generated_at_str,
                article_count=article_count
            ))
        
        # Count unscreened articles (articles in digests not yet in library and not skipped)
        # For now, we'll estimate based on recent digest articles not in library
        if latest_digest_id:
            latest_digest = await db.digests.find_one({"digest_id": latest_digest_id})
            if latest_digest:
                digest_pmids = latest_digest.get("article_pmids", [])
                
                # Check how many are already in library
                if digest_pmids:
                    library_pmids = set()
                    lib_cursor = db.library.find(
                        {"user_id": user_id, "pmid": {"$in": digest_pmids}},
                        {"pmid": 1}
                    )
                    async for lib_doc in lib_cursor:
                        library_pmids.add(lib_doc.get("pmid"))
                    
                    # Check screening decisions
                    screened_pmids = set()
                    screen_cursor = db.article_screening.find(
                        {"user_id": user_id, "article_id": {"$in": digest_pmids}}
                    )
                    async for screen_doc in screen_cursor:
                        screened_pmids.add(screen_doc.get("article_id"))
                    
                    # Unscreened = not in library AND not screened
                    unscreened_count = len([
                        p for p in digest_pmids 
                        if p not in library_pmids and p not in screened_pmids
                    ])
        
        return WorkspaceBootstrap(
            has_preferences=has_preferences,
            unscreened_count=unscreened_count,
            latest_digest_id=latest_digest_id,
            has_library_items=has_library_items,
            library_count=library_count,
            recent_articles=recent_articles,
            recent_digests=recent_digests
        )
        
    except Exception as e:
        logger.error(f"Error getting workspace bootstrap: {e}")
        # Return safe defaults on error
        return WorkspaceBootstrap()


class ScreeningDecision(BaseModel):
    article_id: str
    digest_id: Optional[str] = None
    decision: str  # "keep", "later", "skip"


class ScreeningDecisionResponse(BaseModel):
    success: bool
    message: str
    saved_to_library: bool = False


@router.post("/screening/decision", response_model=ScreeningDecisionResponse)
async def record_screening_decision(
    decision: ScreeningDecision,
    current_user: dict = Depends(get_current_user)
):
    """
    Record a screening decision for an article.
    
    - "keep": Save article to library
    - "later": Mark for later review
    - "skip": Skip this article
    """
    from server import db
    
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    if decision.decision not in ["keep", "later", "skip"]:
        raise HTTPException(status_code=400, detail="Invalid decision. Must be: keep, later, skip")
    
    try:
        # Record the screening decision
        await db.article_screening.update_one(
            {"user_id": user_id, "article_id": decision.article_id},
            {
                "$set": {
                    "user_id": user_id,
                    "article_id": decision.article_id,
                    "digest_id": decision.digest_id,
                    "decision": decision.decision,
                    "decided_at": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        
        saved_to_library = False
        
        # If "keep", also save to library
        if decision.decision == "keep":
            # Check if already in library
            existing = await db.library.find_one({
                "user_id": user_id,
                "pmid": decision.article_id
            })
            
            if not existing:
                # Get article details from articles collection
                article = await db.articles.find_one({"pmid": decision.article_id})
                
                if article:
                    # Determine folder based on digest's specialty
                    folder = "Ungrouped"
                    if decision.digest_id:
                        # Get the digest to find its specialty
                        digest = await db.digests.find_one({"digest_id": decision.digest_id})
                        if digest:
                            specialty_id = digest.get("specialty_id")
                            if specialty_id:
                                # Get specialty config for label
                                specialty_config = await db.specialty_config.find_one({}) or {}
                                for spec in specialty_config.get("specialties", []):
                                    if spec.get("id") == specialty_id:
                                        folder = spec.get("label", specialty_id.replace("_", " ").title())
                                        break
                                else:
                                    folder = specialty_id.replace("_", " ").title()
                    
                    library_entry = {
                        "user_id": user_id,
                        "pmid": decision.article_id,
                        "title": article.get("title", ""),
                        "abstract": article.get("abstract", ""),
                        "journal": article.get("journal", ""),
                        "authors": article.get("authors", ""),
                        "pub_date": article.get("pub_date"),
                        "ai_summary": article.get("ai_summary", ""),
                        "design_tags": article.get("design_tags", []),
                        "saved_at": datetime.now(timezone.utc),
                        "source": "screening",
                        "digest_id": decision.digest_id,
                        "folder": folder
                    }
                    await db.library.insert_one(library_entry)
                    
                    # Also update user_articles for backward compatibility
                    # Stage 1A: use legacy-aware filter and set pmid
                    from utils.user_article_compat import ua_match_filter
                    article_id = str(article.get("_id"))
                    screening_pmid = decision.article_id  # frontend sends PMID
                    now = datetime.now(timezone.utc).isoformat()
                    ua_filter = ua_match_filter(user_id, pmid=screening_pmid, article_obj_id=article_id)
                    await db.user_articles.update_one(
                        ua_filter,
                        {
                            "$set": {
                                "saved_to_library": True,
                                "saved_at": now,
                                "updated_at": now,
                                "folder": folder,
                                "pmid": screening_pmid,
                            },
                            "$setOnInsert": {
                                "user_id": user_id,
                                "article_id": article_id,
                                "created_at": now,
                                "seen_in_digest_at": None,
                            }
                        },
                        upsert=True
                    )
                    
                    saved_to_library = True
        
        return ScreeningDecisionResponse(
            success=True,
            message=f"Decision '{decision.decision}' recorded",
            saved_to_library=saved_to_library
        )
        
    except Exception as e:
        logger.error(f"Error recording screening decision: {e}")
        raise HTTPException(status_code=500, detail="Failed to record decision")


class PendingScreeningResponse(BaseModel):
    articles: List[dict]
    total_pending: int
    digest_id: Optional[str] = None


@router.get("/screening/pending", response_model=PendingScreeningResponse)
async def get_pending_screening(
    digest_id: Optional[str] = None,
    limit: int = 20,
    current_user: dict = Depends(get_current_user)
):
    """
    Get articles pending screening/triage.
    
    Returns articles from recent digests that haven't been screened yet.
    """
    from server import db
    
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    try:
        # Get the target digest
        if digest_id:
            digest = await db.digests.find_one({
                "digest_id": digest_id,
                "user_id": user_id
            })
        else:
            # Get most recent completed digest
            digest = await db.digests.find_one(
                {"user_id": user_id, "status": {"$in": ["completed", "sent"]}},
                sort=[("generated_at", -1)]
            )
        
        if not digest:
            return PendingScreeningResponse(
                articles=[],
                total_pending=0,
                digest_id=None
            )
        
        target_digest_id = digest.get("digest_id")
        article_pmids = digest.get("article_pmids", [])
        
        if not article_pmids:
            return PendingScreeningResponse(
                articles=[],
                total_pending=0,
                digest_id=target_digest_id
            )
        
        # Get already screened articles
        screened_cursor = db.article_screening.find(
            {"user_id": user_id, "article_id": {"$in": article_pmids}}
        )
        screened_ids = set()
        async for doc in screened_cursor:
            screened_ids.add(doc.get("article_id"))
        
        # Get articles already in library
        library_cursor = db.library.find(
            {"user_id": user_id, "pmid": {"$in": article_pmids}},
            {"pmid": 1}
        )
        library_ids = set()
        async for doc in library_cursor:
            library_ids.add(doc.get("pmid"))
        
        # Filter to pending only
        pending_pmids = [
            p for p in article_pmids 
            if p not in screened_ids and p not in library_ids
        ]
        
        total_pending = len(pending_pmids)
        
        # Get article details for pending items
        articles = []
        if pending_pmids:
            pmids_to_fetch = pending_pmids[:limit]
            article_cursor = db.articles.find(
                {"pmid": {"$in": pmids_to_fetch}}
            )
            
            async for article in article_cursor:
                articles.append({
                    "pmid": article.get("pmid"),
                    "title": article.get("title", ""),
                    "abstract": article.get("abstract", ""),
                    "journal": article.get("journal", ""),
                    "authors": article.get("authors", ""),
                    "pub_date": article.get("pub_date"),
                    "ai_summary": article.get("ai_summary", ""),
                    "design_tags": article.get("design_tags", []),
                    "digest_id": target_digest_id
                })
        
        return PendingScreeningResponse(
            articles=articles,
            total_pending=total_pending,
            digest_id=target_digest_id
        )
        
    except Exception as e:
        logger.error(f"Error getting pending screening: {e}")
        raise HTTPException(status_code=500, detail="Failed to get pending articles")


# ============================================================
# SCREEN QUEUE ENDPOINTS (Step 3 - Enhanced Triage Workflow)
# ============================================================

class DigestSession(BaseModel):
    """Digest session for the screen sidebar."""
    digest_id: str
    generated_at: Optional[str] = None
    article_count: int = 0
    unscreened_count: int = 0
    saved_count: int = 0
    deferred_count: int = 0
    skipped_count: int = 0
    is_latest: bool = False


class ScreenProgress(BaseModel):
    """Progress summary for screening."""
    total: int = 0
    saved: int = 0
    deferred: int = 0
    skipped: int = 0
    remaining: int = 0


class ScreenQueueResponse(BaseModel):
    """Response for screen queue endpoint."""
    articles: List[dict]
    progress: ScreenProgress
    digest_id: Optional[str] = None
    digest_date: Optional[str] = None
    filter_status: str = "unscreened"


class DigestListResponse(BaseModel):
    """Response for digest list endpoint."""
    digests: List[DigestSession]
    latest_digest_id: Optional[str] = None


@router.get("/screen/digests", response_model=DigestListResponse)
async def get_screen_digests(
    limit: int = 10,
    current_user: dict = Depends(get_current_user)
):
    """
    Get list of digests for the screen sidebar.
    Includes progress stats for each digest.
    """
    from server import db
    
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    try:
        digests_list = []
        latest_digest_id = None
        
        # Get recent digests
        cursor = db.digests.find(
            {"user_id": user_id, "status": {"$in": ["completed", "sent"]}}
        ).sort("generated_at", -1).limit(limit)
        
        digest_docs = []
        async for doc in cursor:
            digest_docs.append(doc)
        
        for i, digest in enumerate(digest_docs):
            digest_id = digest.get("digest_id")
            if i == 0:
                latest_digest_id = digest_id
            
            article_pmids = digest.get("article_pmids", [])
            article_count = len(article_pmids)
            
            # Get screening stats for this digest
            saved_count = 0
            deferred_count = 0
            skipped_count = 0
            
            if article_pmids:
                # Count by decision type
                pipeline = [
                    {"$match": {"user_id": user_id, "article_id": {"$in": article_pmids}}},
                    {"$group": {"_id": "$decision", "count": {"$sum": 1}}}
                ]
                async for stat in db.article_screening.aggregate(pipeline):
                    decision = stat.get("_id")
                    count = stat.get("count", 0)
                    if decision == "keep":
                        saved_count = count
                    elif decision == "later":
                        deferred_count = count
                    elif decision == "skip":
                        skipped_count = count
                
                # Also count library saves not from screening
                lib_count = await db.library.count_documents({
                    "user_id": user_id,
                    "pmid": {"$in": article_pmids}
                })
                # Adjust saved count to include library saves
                saved_count = max(saved_count, lib_count)
            
            unscreened = article_count - saved_count - deferred_count - skipped_count
            
            generated_at = digest.get("generated_at")
            if generated_at:
                generated_at_str = generated_at.isoformat() if hasattr(generated_at, 'isoformat') else str(generated_at)
            else:
                generated_at_str = None
            
            digests_list.append(DigestSession(
                digest_id=digest_id,
                generated_at=generated_at_str,
                article_count=article_count,
                unscreened_count=max(0, unscreened),
                saved_count=saved_count,
                deferred_count=deferred_count,
                skipped_count=skipped_count,
                is_latest=(i == 0)
            ))
        
        return DigestListResponse(
            digests=digests_list,
            latest_digest_id=latest_digest_id
        )
        
    except Exception as e:
        logger.error(f"Error getting screen digests: {e}")
        raise HTTPException(status_code=500, detail="Failed to get digests")


@router.get("/screen/queue", response_model=ScreenQueueResponse)
async def get_screen_queue(
    digest_id: Optional[str] = None,
    status: str = "unscreened",  # unscreened, deferred, saved, all
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """
    Get screening queue with filtering by status.
    
    Status options:
    - unscreened: Articles not yet triaged
    - deferred: Articles marked for later
    - saved: Articles saved to library
    - all: All articles in digest
    """
    from server import db
    
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    if status not in ["unscreened", "deferred", "saved", "all"]:
        status = "unscreened"
    
    try:
        # Get target digest
        if digest_id:
            digest = await db.digests.find_one({
                "digest_id": digest_id,
                "user_id": user_id
            })
        else:
            digest = await db.digests.find_one(
                {"user_id": user_id, "status": {"$in": ["completed", "sent"]}},
                sort=[("generated_at", -1)]
            )
        
        if not digest:
            return ScreenQueueResponse(
                articles=[],
                progress=ScreenProgress(),
                digest_id=None,
                filter_status=status
            )
        
        target_digest_id = digest.get("digest_id")
        
        # Get article PMIDs - prefer article_pmids, fallback to looking up articles by _id
        article_pmids = digest.get("article_pmids", [])
        
        # Backward compatibility: if no article_pmids, try to get PMIDs from article _ids
        if not article_pmids:
            article_ids = digest.get("articles", [])
            if article_ids:
                # These might be ObjectId strings, try to lookup articles
                from bson import ObjectId
                pmids_from_ids = []
                for aid in article_ids:
                    try:
                        # Try to find by _id (could be ObjectId string)
                        article = await db.articles.find_one({"_id": ObjectId(aid)})
                        if article and article.get("pmid"):
                            pmids_from_ids.append(article.get("pmid"))
                    except Exception:
                        # If not a valid ObjectId, maybe it's already a pmid
                        article = await db.articles.find_one({"pmid": aid})
                        if article:
                            pmids_from_ids.append(aid)
                article_pmids = pmids_from_ids
        
        generated_at = digest.get("generated_at")
        if generated_at:
            digest_date = generated_at.isoformat() if hasattr(generated_at, 'isoformat') else str(generated_at)
        else:
            digest_date = None
        
        if not article_pmids:
            return ScreenQueueResponse(
                articles=[],
                progress=ScreenProgress(),
                digest_id=target_digest_id,
                digest_date=digest_date,
                filter_status=status
            )
        
        # Get screening decisions for all articles
        screening_map = {}
        screening_cursor = db.article_screening.find({
            "user_id": user_id,
            "article_id": {"$in": article_pmids}
        })
        async for doc in screening_cursor:
            screening_map[doc.get("article_id")] = doc.get("decision")
        
        # Get library PMIDs
        library_pmids = set()
        lib_cursor = db.library.find(
            {"user_id": user_id, "pmid": {"$in": article_pmids}},
            {"pmid": 1}
        )
        async for doc in lib_cursor:
            library_pmids.add(doc.get("pmid"))
        
        # Calculate progress
        saved_count = sum(1 for d in screening_map.values() if d == "keep")
        deferred_count = sum(1 for d in screening_map.values() if d == "later")
        skipped_count = sum(1 for d in screening_map.values() if d == "skip")
        
        # Include library saves not from screening
        for pmid in library_pmids:
            if pmid not in screening_map:
                saved_count += 1
        
        total = len(article_pmids)
        remaining = total - saved_count - deferred_count - skipped_count
        
        progress = ScreenProgress(
            total=total,
            saved=saved_count,
            deferred=deferred_count,
            skipped=skipped_count,
            remaining=max(0, remaining)
        )
        
        # Filter PMIDs based on status
        if status == "unscreened":
            filtered_pmids = [
                p for p in article_pmids
                if p not in screening_map and p not in library_pmids
            ]
        elif status == "deferred":
            filtered_pmids = [
                p for p in article_pmids
                if screening_map.get(p) == "later"
            ]
        elif status == "saved":
            filtered_pmids = [
                p for p in article_pmids
                if screening_map.get(p) == "keep" or p in library_pmids
            ]
        else:  # all
            filtered_pmids = article_pmids
        
        # Get article details
        articles = []
        articles_needing_summary = []  # Track articles that need AI summary regeneration
        
        if filtered_pmids:
            pmids_to_fetch = filtered_pmids[:limit]
            article_cursor = db.articles.find({"pmid": {"$in": pmids_to_fetch}})
            
            async for article in article_cursor:
                pmid = article.get("pmid")
                abstract = article.get("abstract", "")
                ai_summary = article.get("ai_summary", "")
                
                # Check if article has valid abstract but invalid/missing AI summary
                has_valid_abstract = abstract and abstract.lower() not in ["no abstract available", "abstract not available", ""]
                has_invalid_summary = not ai_summary or "not available" in ai_summary.lower() or ai_summary.strip() == ""
                
                if has_valid_abstract and has_invalid_summary:
                    articles_needing_summary.append(pmid)
                
                articles.append({
                    "pmid": pmid,
                    "title": article.get("title", ""),
                    "abstract": abstract,
                    "journal": article.get("journal", ""),
                    "authors": article.get("authors", ""),
                    "pub_date": article.get("pub_date"),
                    "ai_summary": ai_summary,
                    "design_tags": article.get("design_tags", []),
                    "topic_tags": article.get("topic_tags", []),
                    "digest_id": target_digest_id,
                    "screening_status": screening_map.get(pmid),
                    "is_in_library": pmid in library_pmids,
                    "needs_ai_summary": has_valid_abstract and has_invalid_summary,
                })
        
        # NOTE: Background AI summary regeneration removed from GET handler
        # to keep it side-effect free (REST compliance).
        # Use POST /workspace/screen/regenerate-summaries instead.
        
        return ScreenQueueResponse(
            articles=articles,
            progress=progress,
            digest_id=target_digest_id,
            digest_date=digest_date,
            filter_status=status
        )
        
    except Exception as e:
        logger.error(f"Error getting screen queue: {e}")
        raise HTTPException(status_code=500, detail="Failed to get queue")



# ============================================================
# SUMMARY REGENERATION (moved from GET side-effect)
# ============================================================

class RegenerateSummariesRequest(BaseModel):
    """Request body for triggering AI summary regeneration."""
    pmids: List[str]


@router.post("/screen/regenerate-summaries")
async def regenerate_summaries(
    body: RegenerateSummariesRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Trigger background AI summary regeneration for articles with missing/invalid summaries.
    
    This replaces the previous side-effect that was triggered by GET /screen/queue.
    Now the frontend explicitly calls this endpoint when it detects articles needing summaries.
    """
    from server import db
    
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    if not body.pmids:
        return {"status": "ok", "regenerating": 0}
    
    # Cap to prevent abuse
    pmids_to_process = body.pmids[:50]
    
    logger.info(f"User {user_id} requested AI summary regeneration for {len(pmids_to_process)} articles")
    
    import asyncio
    asyncio.create_task(_regenerate_ai_summaries(db, pmids_to_process))
    
    return {"status": "ok", "regenerating": len(pmids_to_process)}



# ============================================================
# ANALYTICS ENDPOINTS
# ============================================================

class AnalyticsEvent(BaseModel):
    """A single analytics event."""
    event: str
    properties: Optional[dict] = None
    timestamp: Optional[str] = None


class AnalyticsEventBatch(BaseModel):
    """Batch of analytics events."""
    events: List[AnalyticsEvent]


@router.post("/analytics/events")
async def track_analytics_events(
    batch: AnalyticsEventBatch,
    current_user: dict = Depends(get_current_user)
):
    """
    Receive and store analytics events from the frontend.
    
    Events are stored in the analytics_events collection for later analysis.
    This enables measuring activation and engagement in the Search/Screen/Learn workflow.
    """
    from server import db
    
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not authenticated")
    
    try:
        if not batch.events:
            return {"status": "ok", "events_received": 0}
        
        # Prepare events for insertion
        now = datetime.now(timezone.utc).isoformat()
        events_to_insert = []
        
        for event in batch.events:
            events_to_insert.append({
                "user_id": user_id,
                "event_type": event.event,
                "properties": event.properties or {},
                "client_timestamp": event.timestamp,
                "server_timestamp": now,
            })
        
        # Insert events
        if events_to_insert:
            await db.analytics_events.insert_many(events_to_insert)
            logger.info(f"Stored {len(events_to_insert)} analytics events for user {user_id}")
        
        return {"status": "ok", "events_received": len(events_to_insert)}
        
    except Exception as e:
        logger.error(f"Error storing analytics events: {e}")
        # Don't fail the request - analytics should be non-blocking
        return {"status": "partial", "error": str(e)}


