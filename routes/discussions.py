"""
Discussion Routes for LitPulse v2 Community Features
Implements REST API endpoints for threads, comments, reactions, and specialty rooms
"""
from fastapi import APIRouter, HTTPException, Depends, status, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone
import uuid
import logging
from typing import Optional, List

from auth_utils import get_current_user
from discussion_models import (
    ThreadCreate,
    ThreadResponse,
    ThreadListResponse,
    ThreadDetailResponse,
    CommentCreate,
    CommentUpdate,
    CommentResponse,
    AttachedArticle,
    AttachableArticle,
    AttachableArticlesResponse,
    ReactionRequest,
    SpecialtyRoom,
    SpecialtyRoomListResponse,
    ReportRequest,
    ReportResponse,
    ContextType
)

logger = logging.getLogger(__name__)

# Router will be initialized with db dependency
router = APIRouter(prefix="/discussions", tags=["discussions"])

# Database reference (set by main app)
db: AsyncIOMotorDatabase = None

def set_db(database: AsyncIOMotorDatabase):
    """Set the database reference for the router"""
    global db
    db = database

# ============================================================
# HELPER FUNCTIONS
# ============================================================

async def get_user_name(user_id: str) -> Optional[str]:
    """Get user's full name by user_id"""
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0, "full_name": 1, "email": 1})
    if user:
        return user.get("full_name") or user.get("email", "").split("@")[0]
    return None


async def batch_get_user_names(user_ids: List[str]) -> dict:
    """Batch resolve user names for multiple users. Returns dict mapping user_id -> display_name."""
    if not user_ids:
        return {}
    unique_ids = list(set(user_ids))
    users = await db.users.find(
        {"user_id": {"$in": unique_ids}},
        {"_id": 0, "user_id": 1, "full_name": 1, "email": 1}
    ).to_list(len(unique_ids))
    result = {}
    for u in users:
        result[u["user_id"]] = u.get("full_name") or u.get("email", "").split("@")[0]
    return result

async def batch_get_user_verification_status(user_ids: List[str]) -> dict:
    """
    Batch resolve verification status for multiple users.
    Returns dict mapping user_id -> is_verified (bool)
    """
    if not user_ids:
        return {}
    
    # Get all verification docs for these users in one query
    verifications = await db.professional_verifications.find(
        {"user_id": {"$in": list(set(user_ids))}, "status": "verified"},
        {"_id": 0, "user_id": 1}
    ).to_list(len(user_ids))
    
    verified_user_ids = {v["user_id"] for v in verifications}
    return {uid: uid in verified_user_ids for uid in user_ids}

async def is_user_verified(user_id: str) -> bool:
    """Check if a single user is verified"""
    verification = await db.professional_verifications.find_one(
        {"user_id": user_id, "status": "verified"},
        {"_id": 0, "user_id": 1}
    )
    return verification is not None

async def get_article_previews(article_ids: List[str], user_id: str = None) -> List[AttachedArticle]:
    """Get article preview data for attached articles with library status"""
    if not article_ids:
        return []
    
    articles = await db.articles.find(
        {"pmid": {"$in": article_ids}},
        {"_id": 0, "pmid": 1, "title": 1, "journal": 1, "pub_date": 1, "design_tags": 1}
    ).to_list(len(article_ids))
    
    # Check library status if user_id provided
    library_pmids = set()
    if user_id:
        # Get all user_articles that are in library
        user_articles = await db.user_articles.find(
            {"user_id": user_id, "saved_to_library": True},
            {"_id": 0, "article_id": 1}
        ).to_list(100)
        
        if user_articles:
            # Get article IDs from user_articles
            article_obj_ids = [ua["article_id"] for ua in user_articles]
            # Find corresponding PMIDs
            library_articles = await db.articles.find(
                {"_id": {"$in": [__import__('bson').ObjectId(aid) if len(aid) == 24 else aid for aid in article_obj_ids]}},
                {"_id": 0, "pmid": 1}
            ).to_list(100)
            library_pmids = {a["pmid"] for a in library_articles if a.get("pmid")}
    
    return [AttachedArticle(
        pmid=article["pmid"],
        title=article["title"],
        journal=article.get("journal"),
        pub_date=article.get("pub_date"),
        design_tags=article.get("design_tags"),
        is_in_library=article["pmid"] in library_pmids
    ) for article in articles]

async def count_thread_comments(thread_id: str) -> int:
    """Count non-deleted comments in a thread"""
    return await db.discussion_comments.count_documents({
        "thread_id": thread_id,
        "deleted_at": None
    })


async def batch_count_thread_comments(thread_ids: List[str]) -> dict:
    """Batch count comments for multiple threads. Returns dict mapping thread_id -> count."""
    if not thread_ids:
        return {}
    pipeline = [
        {"$match": {"thread_id": {"$in": thread_ids}, "deleted_at": None}},
        {"$group": {"_id": "$thread_id", "count": {"$sum": 1}}},
    ]
    results = await db.discussion_comments.aggregate(pipeline).to_list(len(thread_ids))
    return {r["_id"]: r["count"] for r in results}


async def batch_get_latest_previews(thread_ids: List[str]) -> dict:
    """Batch get latest comment preview for multiple threads. Returns dict mapping thread_id -> preview_text."""
    if not thread_ids:
        return {}
    pipeline = [
        {"$match": {"thread_id": {"$in": thread_ids}, "deleted_at": None}},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$thread_id", "body": {"$first": "$body"}}},
    ]
    results = await db.discussion_comments.aggregate(pipeline).to_list(len(thread_ids))
    previews = {}
    for r in results:
        body = r.get("body", "")
        previews[r["_id"]] = body[:100] + "..." if len(body) > 100 else body
    return previews


async def batch_count_replies(comment_ids: List[str], thread_id: str) -> dict:
    """Batch count replies for multiple comments in a thread. Returns dict mapping comment_id -> reply_count."""
    if not comment_ids:
        return {}
    pipeline = [
        {"$match": {"thread_id": thread_id, "parent_comment_id": {"$in": comment_ids}, "deleted_at": None}},
        {"$group": {"_id": "$parent_comment_id", "count": {"$sum": 1}}},
    ]
    results = await db.discussion_comments.aggregate(pipeline).to_list(len(comment_ids))
    return {r["_id"]: r["count"] for r in results}

async def get_user_specialty(user_id: str) -> Optional[str]:
    """Get user's specialty_id from preferences"""
    preferences = await db.preferences.find_one(
        {"user_id": user_id},
        {"_id": 0, "specialty_id": 1}
    )
    return preferences.get("specialty_id") if preferences else None


# ============================================================
# PHASE 6: COMMUNITY V2 ELIGIBILITY HELPERS
# ============================================================

async def get_user_eligible_communities(user_id: str, flags: dict) -> dict:
    """
    Return a dict of specialty/subspecialty communities the user can access based on their digests.
    
    Returns:
        {
            "active": [  # Communities user currently has access to (has active digest)
                {"specialty_id": "...", "subspecialty_id": "...", "last_digest_at": "..."},
                ...
            ],
            "frozen": [  # Communities user previously had access to (digest deleted/lost)
                {"specialty_id": "...", "subspecialty_id": "...", "frozen_at": "...", "last_digest_at": "..."},
                ...
            ]
        }
    
    PHI-Zero: logs only user_id + count, never specialty names.
    """
    active_communities = []
    frozen_communities = []
    
    # Get all digests for this user (including deleted ones for frozen state)
    all_digests = await db.digests.find(
        {"user_id": user_id},
        {"_id": 0, "specialty_id": 1, "subspecialty_id": 1, "generated_at": 1, "deleted_at": 1}
    ).to_list(100)
    
    # Group by specialty/subspecialty combination
    community_map = {}
    for digest in all_digests:
        spec_id = digest.get("specialty_id", "")
        subspec_id = digest.get("subspecialty_id", "")
        if not spec_id:
            continue
        
        key = f"{spec_id}:{subspec_id}"
        generated_at = digest.get("generated_at")
        deleted_at = digest.get("deleted_at")
        
        if key not in community_map:
            community_map[key] = {
                "specialty_id": spec_id,
                "subspecialty_id": subspec_id,
                "last_digest_at": generated_at,
                "has_active_digest": False,
                "frozen_at": None
            }
        
        # Update last_digest_at if this digest is more recent
        if generated_at and (not community_map[key]["last_digest_at"] or generated_at > community_map[key]["last_digest_at"]):
            community_map[key]["last_digest_at"] = generated_at
        
        # Check if this digest is active (not deleted)
        if not deleted_at:
            community_map[key]["has_active_digest"] = True
        elif deleted_at and (not community_map[key]["frozen_at"] or deleted_at > community_map[key]["frozen_at"]):
            community_map[key]["frozen_at"] = deleted_at
    
    # Categorize into active and frozen
    for key, data in community_map.items():
        if data["has_active_digest"]:
            active_communities.append({
                "specialty_id": data["specialty_id"],
                "subspecialty_id": data["subspecialty_id"],
                "last_digest_at": data["last_digest_at"]
            })
        else:
            # Frozen - had digests before but all are now deleted
            frozen_communities.append({
                "specialty_id": data["specialty_id"],
                "subspecialty_id": data["subspecialty_id"],
                "frozen_at": data["frozen_at"],
                "last_digest_at": data["last_digest_at"]
            })
    
    return {"active": active_communities, "frozen": frozen_communities}


async def get_user_eligible_specialties(user_id: str, flags: dict) -> set:
    """
    Return the set of specialty IDs this user is eligible to enter (has active digests for).

    When ENABLE_COMMUNITY_V2=true: reads actual digests created by user
    When false: returns all specialties (no gating)

    PHI-Zero: logs only user_id + count, never specialty names.
    """
    if not _community_v2_on(flags):
        # No gating when community v2 is off
        return set()  # Empty set means "all allowed" in legacy mode
    
    communities = await get_user_eligible_communities(user_id, flags)
    # Return specialty IDs from active communities only
    return {c["specialty_id"] for c in communities.get("active", [])}


def _community_v2_on(flags: dict) -> bool:
    return flags.get("enable_community_v2", False)


async def get_user_community_access_state(user_id: str, specialty_id: str, subspecialty_id: str, flags: dict) -> dict:
    """
    Get the user's access state for a specific community.
    
    Returns:
        {
            "access": "active" | "frozen" | "none",
            "can_read": bool,
            "can_write": bool,
            "frozen_at": str | None,  # ISO timestamp when access was frozen
            "last_digest_at": str | None  # Last digest creation time
        }
    """
    if not _community_v2_on(flags):
        # No gating - full access
        return {"access": "active", "can_read": True, "can_write": True, "frozen_at": None, "last_digest_at": None}
    
    communities = await get_user_eligible_communities(user_id, flags)
    
    # Check active communities first
    for c in communities.get("active", []):
        if c["specialty_id"] == specialty_id:
            # For now, allow access if specialty matches (subspecialty filtering can be added later)
            if not subspecialty_id or c.get("subspecialty_id") == subspecialty_id:
                return {
                    "access": "active",
                    "can_read": True,
                    "can_write": True,
                    "frozen_at": None,
                    "last_digest_at": c.get("last_digest_at")
                }
    
    # Check frozen communities
    for c in communities.get("frozen", []):
        if c["specialty_id"] == specialty_id:
            if not subspecialty_id or c.get("subspecialty_id") == subspecialty_id:
                return {
                    "access": "frozen",
                    "can_read": True,
                    "can_write": False,
                    "frozen_at": c.get("frozen_at"),
                    "last_digest_at": c.get("last_digest_at")
                }
    
    # No access
    return {"access": "none", "can_read": False, "can_write": False, "frozen_at": None, "last_digest_at": None}


async def _check_specialty_access(user_id: str, specialty_id: str, flags: dict) -> bool:
    """
    Returns True if the user can enter the given specialty community (active or frozen).
    When flag OFF: always True (no gating).
    """
    if not _community_v2_on(flags):
        return True
    access_state = await get_user_community_access_state(user_id, specialty_id, "", flags)
    return access_state["can_read"]


async def _require_specialty_access(user_id: str, specialty_id: str, flags: dict) -> None:
    """
    Raise 403 community_locked if user cannot enter this specialty at all.
    No-op when ENABLE_COMMUNITY_V2=false.
    """
    if not _community_v2_on(flags):
        return
    access_state = await get_user_community_access_state(user_id, specialty_id, "", flags)
    if not access_state["can_read"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "community_locked",
                "message": (
                    "Create a digest in this specialty to join this community. "
                    "Go to Preferences and generate a digest to gain access."
                ),
                "specialty_id": specialty_id,
            },
        )


async def _require_specialty_write_access(user_id: str, specialty_id: str, flags: dict) -> None:
    """
    Raise 403 if user cannot write in this specialty community.
    Handles both locked (no access) and frozen (read-only) states.
    """
    if not _community_v2_on(flags):
        return
    access_state = await get_user_community_access_state(user_id, specialty_id, "", flags)
    
    if not access_state["can_read"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "community_locked",
                "message": (
                    "Create a digest in this specialty to join this community."
                ),
                "specialty_id": specialty_id,
            },
        )
    
    if not access_state["can_write"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "community_frozen",
                "message": (
                    "Your access to this community is read-only. "
                    "Create a new digest in this specialty to regain full access."
                ),
                "specialty_id": specialty_id,
                "frozen_at": access_state.get("frozen_at"),
            },
        )


# ============================================================
# PHASE UX-C: COMMUNITY SUBSPECIALTY VISIBILITY HELPERS
# ============================================================

MAX_VISIBLE_SUBSPECIALTIES = 3


async def get_user_community_subspecialties(user_id: str, specialty_id: str, flags: dict) -> list:
    """
    Get the visible subspecialties for a user in a given specialty.
    
    Union across all profiles with matching specialty_id.
    Truncate deterministically to MAX_VISIBLE_SUBSPECIALTIES.
    
    Returns: List of subspecialty IDs (max 3)
    """
    if not flags.get("enable_community_subspecialty_selection", False):
        return []
    
    if not flags.get("enable_multi_digest_profiles", False):
        # Legacy mode: no subspecialty selection
        return []
    
    profiles = await db.digest_profiles.find(
        {
            "user_id": user_id,
            "specialty_id": specialty_id,
            "is_active": True,
            "deleted_at": None,
        },
        {"_id": 0, "community_subspecialty_ids": 1},
    ).to_list(20)
    
    # Union all subspecialty IDs from matching profiles
    all_subspecialties = set()
    for p in profiles:
        ids = p.get("community_subspecialty_ids", [])
        all_subspecialties.update(ids)
    
    # Deterministic truncation (sort alphabetically, take first MAX)
    sorted_subspecialties = sorted(all_subspecialties)
    return sorted_subspecialties[:MAX_VISIBLE_SUBSPECIALTIES]


async def can_user_post_in_community(user_id: str, specialty_id: str, flags: dict) -> bool:
    """
    Check if user can post in a specialty community.
    
    Policy:
      - trial_active => can post (everyone gets community access during trial)
      - verified clinician => can post
      - Pro alone (not trial, not verified) => CANNOT post
      - Free => CANNOT post
    
    Returns: True if user can post, False otherwise
    """
    from utils.capabilities import _is_new_trial_active
    
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        return False
    
    # Trial active => everyone can post
    if _is_new_trial_active(user, flags):
        return True
    
    # Check old trial (trial_ends_at)
    trial_ends = user.get("trial_ends_at")
    if trial_ends:
        try:
            from datetime import datetime, timezone
            end_dt = datetime.fromisoformat(trial_ends.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < end_dt:
                return True
        except (ValueError, TypeError):
            pass
    
    # Professionally verified users can post
    if await is_user_verified(user_id):
        return True
    
    # Admin override
    admin_email = flags.get("admin_email", "")
    if admin_email and user.get("email") == admin_email:
        return True
    
    # Everyone else (including Pro without verification) => cannot post
    return False

# ============================================================
# ATTACHABLE ARTICLES ENDPOINT
# ============================================================

@router.get("/attachable-articles", response_model=AttachableArticlesResponse)
async def get_attachable_articles(
    current_user: dict = Depends(get_current_user)
):
    """Get articles user can attach to comments (library + recent digest)"""
    try:
        from bson import ObjectId
        user_id = current_user["user_id"]
        
        library_articles = []
        digest_articles = []
        library_pmids = set()
        
        # 1. Get user's library articles (up to 50 most recent)
        user_articles = await db.user_articles.find(
            {"user_id": user_id, "saved_to_library": True},
            {"_id": 0, "article_id": 1, "saved_at": 1}
        ).sort("saved_at", -1).to_list(50)
        
        if user_articles:
            article_ids = [ua["article_id"] for ua in user_articles]
            # Convert to ObjectId if needed
            obj_ids = []
            for aid in article_ids:
                try:
                    obj_ids.append(ObjectId(aid) if len(str(aid)) == 24 else aid)
                except:
                    pass
            
            if obj_ids:
                articles = await db.articles.find(
                    {"_id": {"$in": obj_ids}},
                    {"_id": 0, "pmid": 1, "title": 1, "journal": 1, "pub_date": 1, "design_tags": 1}
                ).to_list(50)
                
                for article in articles:
                    if article.get("pmid"):
                        library_pmids.add(article["pmid"])
                        library_articles.append(AttachableArticle(
                            pmid=article["pmid"],
                            title=article["title"],
                            journal=article.get("journal"),
                            pub_date=article.get("pub_date"),
                            design_tags=article.get("design_tags"),
                            source="library",
                            is_in_library=True
                        ))
        
        # 2. Get articles from user's most recent digest (up to 20)
        latest_digest = await db.digests.find_one(
            {"user_id": user_id, "status": "completed"},
            {"_id": 0, "articles": 1},
            sort=[("generated_at", -1)]
        )
        
        if latest_digest and latest_digest.get("articles"):
            digest_article_ids = latest_digest["articles"][:20]
            
            # Convert to ObjectId if needed
            obj_ids = []
            for aid in digest_article_ids:
                try:
                    obj_ids.append(ObjectId(aid) if len(str(aid)) == 24 else aid)
                except:
                    pass
            
            if obj_ids:
                articles = await db.articles.find(
                    {"_id": {"$in": obj_ids}},
                    {"_id": 0, "pmid": 1, "title": 1, "journal": 1, "pub_date": 1, "design_tags": 1}
                ).to_list(20)
                
                for article in articles:
                    if article.get("pmid") and article["pmid"] not in library_pmids:
                        digest_articles.append(AttachableArticle(
                            pmid=article["pmid"],
                            title=article["title"],
                            journal=article.get("journal"),
                            pub_date=article.get("pub_date"),
                            design_tags=article.get("design_tags"),
                            source="digest",
                            is_in_library=False
                        ))
        
        total = len(library_articles) + len(digest_articles)
        
        return AttachableArticlesResponse(
            library_articles=library_articles,
            digest_articles=digest_articles,
            total=total
        )
        
    except Exception as e:
        logger.error(f"Get attachable articles error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get attachable articles"
        )

# ============================================================
# THREAD ENDPOINTS
# ============================================================

@router.post("/threads", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED)
async def create_thread(
    data: ThreadCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new discussion thread"""
    try:
        user_id = current_user["user_id"]
        
        # PHI-Zero enforcement on title
        from utils.feature_flags import get_feature_flags
        from utils.phi_guard import enforce_phi_guard
        flags = get_feature_flags()
        enforce_phi_guard(
            text=data.title,
            endpoint="POST /api/discussions/threads",
            user_id=user_id,
            mode=flags.get("phi_guard_mode", "block"),
            enabled=flags.get("enable_phi_guard", True),
        )
        
        # Trust gate: require verified peer if flag is on
        from utils.capabilities import require_verified_peer
        await require_verified_peer(user_id, db)

        # Phase 6: require digest eligibility for specialty communities
        if data.context_type == "specialty" and data.specialty_id:
            await _require_specialty_write_access(user_id, data.specialty_id, flags)
            
            # PATCH UX-C: Check posting permission (premium OR professionally verified)
            can_post = await can_user_post_in_community(user_id, data.specialty_id, flags)
            if not can_post:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "community_read_only",
                        "message": "Upgrade to Pro or verify your work email to participate.",
                    },
                )
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Sanitize user-generated content (XSS prevention)
        from utils.sanitize import sanitize_plain
        safe_title = sanitize_plain(data.title)
        
        thread = {
            "thread_id": str(uuid.uuid4()),
            "context_type": data.context_type,
            "context_id": data.context_id,
            "specialty_id": data.specialty_id,
            "title": safe_title,
            "created_by": user_id,
            "created_at": now,
            "last_activity_at": now,
            "is_pinned": False,
            # Phase 6: article-linked thread (Discuss flow)
            "primary_article_pmid": data.primary_article_pmid or None,
        }
        
        await db.discussion_threads.insert_one(thread)
        thread.pop("_id", None)
        
        # Add creator name and verification status
        creator_name = await get_user_name(user_id)
        creator_is_verified = await is_user_verified(user_id)
        
        logger.info(f"Thread created: {thread['thread_id']} by user {user_id}")

        # Track community post event
        try:
            from utils.event_tracker import track_event
            await track_event("community_post", user_id)
        except Exception:
            pass
        
        return ThreadResponse(
            **thread,
            comment_count=0,
            preview_comment=None,
            creator_name=creator_name,
            creator_is_verified=creator_is_verified
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create thread error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create thread"
        )

@router.get("/threads", response_model=ThreadListResponse)
async def get_threads(
    context_type: ContextType = Query(...),
    context_id: str = Query(...),
    primary_article_pmid: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    """Get threads for a specific context (article, digest, topic, etc.)
    Phase 6: primary_article_pmid filter for article-linked threads.
    """
    try:
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()

        # Phase 6: gate specialty community reads on digest eligibility
        if context_type == "specialty" and _community_v2_on(flags):
            await _require_specialty_access(current_user["user_id"], context_id, flags)

        query: dict = {"context_type": context_type, "context_id": context_id}
        if primary_article_pmid:
            query["primary_article_pmid"] = primary_article_pmid

        threads = await db.discussion_threads.find(
            query, {"_id": 0}
        ).sort([("is_pinned", -1), ("last_activity_at", -1)]).to_list(100)

        if not threads:
            return ThreadListResponse(threads=[], total=0)

        thread_ids = [t["thread_id"] for t in threads]
        creator_ids = [t["created_by"] for t in threads]

        verification_map = await batch_get_user_verification_status(creator_ids)
        name_map = await batch_get_user_names(creator_ids)
        count_map = await batch_count_thread_comments(thread_ids)
        preview_map = await batch_get_latest_previews(thread_ids)

        # Phase 6: resolve primary article titles
        pmids_needed = list({t.get("primary_article_pmid") for t in threads if t.get("primary_article_pmid")})
        article_title_map: dict = {}
        if pmids_needed:
            arts = await db.articles.find(
                {"pmid": {"$in": pmids_needed}},
                {"_id": 0, "pmid": 1, "title": 1}
            ).to_list(len(pmids_needed))
            article_title_map = {a["pmid"]: a.get("title", "") for a in arts}

        enriched_threads = []
        for thread in threads:
            tid = thread["thread_id"]
            cid = thread["created_by"]
            pa_pmid = thread.get("primary_article_pmid")
            enriched_threads.append(ThreadResponse(
                **thread,
                comment_count=count_map.get(tid, 0),
                preview_comment=preview_map.get(tid),
                creator_name=name_map.get(cid),
                creator_is_verified=verification_map.get(cid, False),
                primary_article_title=article_title_map.get(pa_pmid) if pa_pmid else None,
            ))

        return ThreadListResponse(threads=enriched_threads, total=len(enriched_threads))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get threads error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get threads"
        )

@router.get("/threads/{thread_id}", response_model=ThreadDetailResponse)
async def get_thread_detail(
    thread_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get thread with all its comments"""
    try:
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()

        # Get thread
        thread = await db.discussion_threads.find_one(
            {"thread_id": thread_id},
            {"_id": 0}
        )
        
        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Thread not found"
            )

        # Phase 6: gate thread detail on digest eligibility
        if thread.get("specialty_id") and _community_v2_on(flags):
            await _require_specialty_access(current_user["user_id"], thread["specialty_id"], flags)
        
        # Get comments sorted by created_at (include soft-deleted for moderation placeholder)
        comments = await db.discussion_comments.find(
            {"thread_id": thread_id},
            {"_id": 0}
        ).sort("created_at", 1).to_list(500)
        
        # Batch resolve all data upfront
        author_ids = [comment["user_id"] for comment in comments] + [thread["created_by"]]
        verification_map = await batch_get_user_verification_status(author_ids)
        name_map = await batch_get_user_names(author_ids)
        
        # Batch reply counts for non-deleted comments
        non_deleted_ids = [c["comment_id"] for c in comments if not c.get("deleted_at")]
        reply_count_map = await batch_count_replies(non_deleted_ids, thread_id)
        
        # Batch article previews: collect all attached article IDs
        all_attached_ids = set()
        for comment in comments:
            if not comment.get("deleted_at"):
                for aid in comment.get("attached_article_ids", []):
                    all_attached_ids.add(aid)
        
        # Pre-fetch all attached articles in one query
        attached_articles_map = {}
        if all_attached_ids:
            articles = await db.articles.find(
                {"pmid": {"$in": list(all_attached_ids)}},
                {"_id": 0, "pmid": 1, "title": 1, "journal": 1, "pub_date": 1, "design_tags": 1}
            ).to_list(len(all_attached_ids))
            for a in articles:
                attached_articles_map[a["pmid"]] = a
        
        # Pre-fetch user library PMIDs once
        library_pmids = set()
        if all_attached_ids:
            user_articles = await db.user_articles.find(
                {"user_id": current_user["user_id"], "saved_to_library": True},
                {"_id": 0, "article_id": 1}
            ).to_list(100)
            if user_articles:
                article_obj_ids = [ua["article_id"] for ua in user_articles]
                from bson import ObjectId as _ObjId
                obj_ids = []
                for aid in article_obj_ids:
                    try:
                        obj_ids.append(_ObjId(aid) if len(str(aid)) == 24 else aid)
                    except Exception:
                        pass
                if obj_ids:
                    lib_arts = await db.articles.find(
                        {"_id": {"$in": obj_ids}},
                        {"_id": 0, "pmid": 1}
                    ).to_list(100)
                    library_pmids = {a["pmid"] for a in lib_arts if a.get("pmid")}
        
        # Enrich comments with batched data
        enriched_comments = []
        for comment in comments:
            if comment.get("deleted_at"):
                enriched_comments.append(CommentResponse(
                    comment_id=comment["comment_id"],
                    thread_id=comment["thread_id"],
                    user_id=comment["user_id"],
                    body="[This comment was removed by moderation.]",
                    parent_comment_id=comment.get("parent_comment_id"),
                    reactions={},
                    created_at=comment.get("created_at", ""),
                    updated_at=comment.get("updated_at", ""),
                    deleted_at=comment.get("deleted_at"),
                    user_name=None,
                    author_is_verified=False,
                    reply_count=0,
                ))
                continue

            # Build attached articles from pre-fetched data
            attached_articles = []
            for aid in comment.get("attached_article_ids", []):
                art = attached_articles_map.get(aid)
                if art:
                    attached_articles.append(AttachedArticle(
                        pmid=art["pmid"],
                        title=art["title"],
                        journal=art.get("journal"),
                        pub_date=art.get("pub_date"),
                        design_tags=art.get("design_tags"),
                        is_in_library=art["pmid"] in library_pmids
                    ))
            
            enriched_comments.append(CommentResponse(
                **comment,
                user_name=name_map.get(comment["user_id"]),
                attached_articles=attached_articles,
                author_is_verified=verification_map.get(comment["user_id"], False),
                reply_count=reply_count_map.get(comment["comment_id"], 0)
            ))
        
        creator_name = name_map.get(thread["created_by"])
        creator_is_verified = verification_map.get(thread["created_by"], False)
        
        return ThreadDetailResponse(
            **thread,
            comment_count=len(enriched_comments),
            creator_name=creator_name,
            creator_is_verified=creator_is_verified,
            comments=enriched_comments
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get thread detail error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get thread detail"
        )

# ============================================================
# COMMENT ENDPOINTS
# ============================================================

@router.post("/threads/{thread_id}/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    thread_id: str,
    data: CommentCreate,
    current_user: dict = Depends(get_current_user)
):
    """Add a comment to a thread"""
    try:
        user_id = current_user["user_id"]
        
        # PHI-Zero enforcement on comment body
        from utils.feature_flags import get_feature_flags
        from utils.phi_guard import enforce_phi_guard
        flags = get_feature_flags()
        enforce_phi_guard(
            text=data.body,
            endpoint="POST /api/discussions/comments",
            user_id=user_id,
            mode=flags.get("phi_guard_mode", "block"),
            enabled=flags.get("enable_phi_guard", True),
        )
        
        # Trust gate: require verified peer if flag is on
        from utils.capabilities import require_verified_peer
        await require_verified_peer(user_id, db)
        
        # Import sanitization
        from utils.sanitize import sanitize_rich
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Verify thread exists
        thread = await db.discussion_threads.find_one({"thread_id": thread_id})
        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Thread not found"
            )
        
        # PATCH UX-C: Check posting permission (premium OR professionally verified)
        if thread.get("context_type") == "specialty" and thread.get("specialty_id"):
            # Phase 6: Check specialty write access (blocks frozen users)
            await _require_specialty_write_access(user_id, thread["specialty_id"], flags)
            
            can_post = await can_user_post_in_community(user_id, thread["specialty_id"], flags)
            if not can_post:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "community_read_only",
                        "message": "Upgrade to Pro or verify your work email to participate.",
                    },
                )
        
        # If replying to a comment, verify parent exists
        parent_comment_user_id = None
        if data.parent_comment_id:
            parent = await db.discussion_comments.find_one({
                "comment_id": data.parent_comment_id,
                "thread_id": thread_id,
                "deleted_at": None
            })
            if not parent:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Parent comment not found"
                )
            parent_comment_user_id = parent.get("user_id")
        
        comment = {
            "comment_id": str(uuid.uuid4()),
            "thread_id": thread_id,
            "user_id": user_id,
            "body": sanitize_rich(data.body),
            "parent_comment_id": data.parent_comment_id,
            "attached_article_ids": data.attached_article_ids or [],
            "reactions": {},
            "created_at": now,
            "updated_at": now,
            "deleted_at": None
        }
        
        await db.discussion_comments.insert_one(comment)
        comment.pop("_id", None)
        
        # Update thread's last activity
        await db.discussion_threads.update_one(
            {"thread_id": thread_id},
            {"$set": {"last_activity_at": now}}
        )
        
        # Create notification for parent comment author (if reply)
        if parent_comment_user_id and parent_comment_user_id != user_id:
            try:
                from routes.notifications import create_reply_notification
                await create_reply_notification(
                    recipient_user_id=parent_comment_user_id,
                    actor_user_id=user_id,
                    thread_id=thread_id,
                    comment_id=comment["comment_id"]
                )
            except Exception as notif_error:
                logger.error(f"Failed to create reply notification: {notif_error}")
        
        # Enrich response
        user_name = await get_user_name(user_id)
        attached_articles = await get_article_previews(comment.get("attached_article_ids", []), user_id)
        author_is_verified = await is_user_verified(user_id)
        
        logger.info(f"Comment created: {comment['comment_id']} in thread {thread_id}")
        
        return CommentResponse(
            **comment,
            user_name=user_name,
            attached_articles=attached_articles,
            author_is_verified=author_is_verified,
            reply_count=0
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create comment error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create comment"
        )

@router.patch("/comments/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: str,
    data: CommentUpdate,
    current_user: dict = Depends(get_current_user)
):
    """Update a comment (only by owner)"""
    try:
        user_id = current_user["user_id"]
        
        # PHI-Zero enforcement on updated body
        from utils.feature_flags import get_feature_flags
        from utils.phi_guard import enforce_phi_guard
        flags = get_feature_flags()
        enforce_phi_guard(
            text=data.body,
            endpoint="PATCH /api/discussions/comments",
            user_id=user_id,
            mode=flags.get("phi_guard_mode", "block"),
            enabled=flags.get("enable_phi_guard", True),
        )
        
        # Trust gate: require verified peer if flag is on
        from utils.capabilities import require_verified_peer
        await require_verified_peer(user_id, db)
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Find comment and verify ownership
        comment = await db.discussion_comments.find_one({
            "comment_id": comment_id,
            "deleted_at": None
        })
        
        if not comment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found"
            )
        
        if comment["user_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only edit your own comments"
            )
        
        # Update comment
        await db.discussion_comments.update_one(
            {"comment_id": comment_id},
            {"$set": {"body": data.body.strip(), "updated_at": now}}
        )
        
        # Get updated comment
        updated_comment = await db.discussion_comments.find_one(
            {"comment_id": comment_id},
            {"_id": 0}
        )
        
        # Enrich response
        user_name = await get_user_name(user_id)
        attached_articles = await get_article_previews(updated_comment.get("attached_article_ids", []), user_id)
        author_is_verified = await is_user_verified(user_id)
        reply_count = await db.discussion_comments.count_documents({
            "parent_comment_id": comment_id,
            "deleted_at": None
        })
        
        logger.info(f"Comment updated: {comment_id} by user {user_id}")
        
        return CommentResponse(
            **updated_comment,
            user_name=user_name,
            attached_articles=attached_articles,
            author_is_verified=author_is_verified,
            reply_count=reply_count
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update comment error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update comment"
        )

@router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Soft-delete a comment (only by owner)"""
    try:
        user_id = current_user["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        
        # Find comment and verify ownership
        comment = await db.discussion_comments.find_one({
            "comment_id": comment_id,
            "deleted_at": None
        })
        
        if not comment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found"
            )
        
        if comment["user_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only delete your own comments"
            )
        
        # Soft delete
        await db.discussion_comments.update_one(
            {"comment_id": comment_id},
            {"$set": {"deleted_at": now, "body": "[deleted]"}}
        )
        
        logger.info(f"Comment soft-deleted: {comment_id} by user {user_id}")
        return None
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete comment error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete comment"
        )

# ============================================================
# REACTION ENDPOINTS
# ============================================================

@router.post("/comments/{comment_id}/react", response_model=CommentResponse)
async def toggle_reaction(
    comment_id: str,
    data: ReactionRequest,
    current_user: dict = Depends(get_current_user)
):
    """Add or remove a reaction on a comment (toggle behavior)"""
    try:
        user_id = current_user["user_id"]
        
        # Trust gate: require verified peer if flag is on
        from utils.capabilities import require_verified_peer
        await require_verified_peer(user_id, db)
        
        # Find comment
        comment = await db.discussion_comments.find_one({
            "comment_id": comment_id,
            "deleted_at": None
        })
        
        if not comment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found"
            )
        
        reactions = comment.get("reactions", {})
        reaction_type = data.reaction_type
        
        # Initialize reaction type if not present
        if reaction_type not in reactions:
            reactions[reaction_type] = []
        
        # Toggle: if user already reacted, remove; otherwise add
        if user_id in reactions[reaction_type]:
            reactions[reaction_type].remove(user_id)
        else:
            reactions[reaction_type].append(user_id)
        
        # Clean up empty reaction lists
        reactions = {k: v for k, v in reactions.items() if v}
        
        # Update comment
        await db.discussion_comments.update_one(
            {"comment_id": comment_id},
            {"$set": {"reactions": reactions}}
        )
        
        # Get updated comment
        updated_comment = await db.discussion_comments.find_one(
            {"comment_id": comment_id},
            {"_id": 0}
        )
        
        # Enrich response
        user_name = await get_user_name(updated_comment["user_id"])
        attached_articles = await get_article_previews(updated_comment.get("attached_article_ids", []), user_id)
        author_is_verified = await is_user_verified(updated_comment["user_id"])
        reply_count = await db.discussion_comments.count_documents({
            "parent_comment_id": comment_id,
            "deleted_at": None
        })
        
        logger.info(f"Reaction toggled on comment {comment_id}: {reaction_type} by user {user_id}")
        
        return CommentResponse(
            **updated_comment,
            user_name=user_name,
            attached_articles=attached_articles,
            author_is_verified=author_is_verified,
            reply_count=reply_count
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Toggle reaction error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle reaction"
        )

# ============================================================
# SPECIALTY ROOM ENDPOINTS
# ============================================================

@router.get("/specialty-rooms", response_model=SpecialtyRoomListResponse)
async def get_specialty_rooms(current_user: dict = Depends(get_current_user)):
    """
    Get list of specialty rooms with activity stats.
    V2 (ENABLE_COMMUNITY_V2=true): Only returns communities user has digests for (active or frozen).
    V1 (flag OFF): Returns all communities.
    """
    try:
        from pathlib import Path
        import json
        from utils.feature_flags import get_feature_flags

        flags = get_feature_flags()
        v2 = _community_v2_on(flags)
        user_id = current_user["user_id"]

        # Get user's eligible communities with access state
        user_communities = {}
        if v2:
            communities_data = await get_user_eligible_communities(user_id, flags)
            # Build lookup: specialty_id -> access_state info
            for c in communities_data.get("active", []):
                user_communities[c["specialty_id"]] = {
                    "access_state": "active",
                    "can_enter": True,
                    "can_post": True,
                    "frozen_at": None
                }
            for c in communities_data.get("frozen", []):
                # Only add frozen if not already active
                if c["specialty_id"] not in user_communities:
                    user_communities[c["specialty_id"]] = {
                        "access_state": "frozen",
                        "can_enter": True,
                        "can_post": False,
                        "frozen_at": c.get("frozen_at")
                    }

        config_path = Path(__file__).parent.parent / "config" / "specialty_config.json"
        with open(config_path, 'r') as f:
            config = json.load(f)

        rooms = []
        for specialty in config.get("specialties", []):
            specialty_id = specialty["id"]
            specialty_name = specialty.get("label") or specialty.get("name") or specialty_id

            # V2: Skip communities user doesn't have access to
            if v2 and specialty_id not in user_communities:
                continue

            thread_count = await db.discussion_threads.count_documents(
                {"specialty_id": specialty_id}
            )
            member_pipeline = [
                {"$match": {"specialty_id": specialty_id}},
                {"$group": {"_id": "$created_by"}},
                {"$count": "count"}
            ]
            member_result = await db.discussion_threads.aggregate(member_pipeline).to_list(1)
            member_count = member_result[0]["count"] if member_result else 0

            latest_thread = await db.discussion_threads.find_one(
                {"specialty_id": specialty_id},
                {"_id": 0, "last_activity_at": 1},
                sort=[("last_activity_at", -1)]
            )
            last_activity = latest_thread["last_activity_at"] if latest_thread else None

            # Phase 6: V2 fields
            can_enter = None
            subspecialties = None
            eligible_subspecialties = None
            visible_subspecialties = None
            can_post = None
            access_state = None
            frozen_at = None
            
            if v2:
                community_access = user_communities.get(specialty_id, {})
                can_enter = community_access.get("can_enter", False)
                can_post = community_access.get("can_post", False)
                access_state = community_access.get("access_state", "none")
                frozen_at = community_access.get("frozen_at")
                
                subs = specialty.get("subspecialties", [])
                subspecialties = [{"id": s["id"], "label": s.get("label", s["id"])} for s in subs]
                
                # Phase UX-C: subspecialty visibility
                if flags.get("enable_community_subspecialty_selection", False):
                    eligible_subspecialties = subspecialties  # Full list from config
                    
                    # Get user's selected subspecialties for this specialty
                    user_subspecialty_ids = await get_user_community_subspecialties(user_id, specialty_id, flags)
                    # Map IDs to full objects with labels
                    id_to_sub = {s["id"]: s for s in subs}
                    visible_subspecialties = [
                        {"id": sid, "label": id_to_sub.get(sid, {}).get("label", sid)}
                        for sid in user_subspecialty_ids
                        if sid in id_to_sub or sid == f"{specialty_id}_core"  # Include core
                    ]

            rooms.append(SpecialtyRoom(
                specialty_id=specialty_id,
                specialty_name=specialty_name,
                thread_count=thread_count,
                member_count=member_count,
                last_activity=last_activity,
                can_enter=can_enter,
                subspecialties=subspecialties,
                eligible_subspecialties=eligible_subspecialties,
                visible_subspecialties=visible_subspecialties,
                can_post=can_post,
                access_state=access_state,
                frozen_at=frozen_at,
            ))

        rooms.sort(key=lambda r: r.thread_count, reverse=True)
        return SpecialtyRoomListResponse(rooms=rooms)

    except Exception as e:
        logger.error(f"Get specialty rooms error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get specialty rooms"
        )


@router.get("/specialties/{specialty_id}", response_model=ThreadListResponse)
async def get_specialty_threads(
    specialty_id: str,
    primary_article_pmid: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    """Get threads in a specialty room. Optionally filter by primary_article_pmid."""
    try:
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()

        # Phase 6: gate READ on digest eligibility
        await _require_specialty_access(current_user["user_id"], specialty_id, flags)

        query: dict = {"specialty_id": specialty_id}
        if primary_article_pmid:
            query["primary_article_pmid"] = primary_article_pmid

        threads = await db.discussion_threads.find(
            query, {"_id": 0}
        ).sort([("is_pinned", -1), ("last_activity_at", -1)]).to_list(100)

        if not threads:
            return ThreadListResponse(threads=[], total=0)

        thread_ids = [t["thread_id"] for t in threads]
        creator_ids = [t["created_by"] for t in threads]

        verification_map = await batch_get_user_verification_status(creator_ids)
        name_map = await batch_get_user_names(creator_ids)
        count_map = await batch_count_thread_comments(thread_ids)
        preview_map = await batch_get_latest_previews(thread_ids)

        # Phase 6: resolve primary article titles for article-linked threads
        pmids_needed = list({t.get("primary_article_pmid") for t in threads if t.get("primary_article_pmid")})
        article_title_map: dict = {}
        if pmids_needed:
            arts = await db.articles.find(
                {"pmid": {"$in": pmids_needed}},
                {"_id": 0, "pmid": 1, "title": 1}
            ).to_list(len(pmids_needed))
            article_title_map = {a["pmid"]: a.get("title", "") for a in arts}

        enriched_threads = []
        for thread in threads:
            tid = thread["thread_id"]
            cid = thread["created_by"]
            pa_pmid = thread.get("primary_article_pmid")
            enriched_threads.append(ThreadResponse(
                **thread,
                comment_count=count_map.get(tid, 0),
                preview_comment=preview_map.get(tid),
                creator_name=name_map.get(cid),
                creator_is_verified=verification_map.get(cid, False),
                primary_article_title=article_title_map.get(pa_pmid) if pa_pmid else None,
            ))

        return ThreadListResponse(threads=enriched_threads, total=len(enriched_threads))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get specialty threads error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get specialty threads"
        )

# ============================================================
# REPORT ENDPOINT
# ============================================================

@router.post("/comments/{comment_id}/report", response_model=ReportResponse)
async def report_comment(
    comment_id: str,
    data: ReportRequest,
    current_user: dict = Depends(get_current_user)
):
    """Report a comment for review"""
    try:
        user_id = current_user["user_id"]
        
        # PHI-Zero enforcement on report reason
        from utils.feature_flags import get_feature_flags
        from utils.phi_guard import enforce_phi_guard
        flags = get_feature_flags()
        enforce_phi_guard(
            text=data.reason,
            endpoint="POST /api/discussions/report",
            user_id=user_id,
            mode=flags.get("phi_guard_mode", "block"),
            enabled=flags.get("enable_phi_guard", True),
        )
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Verify comment exists
        comment = await db.discussion_comments.find_one({
            "comment_id": comment_id,
            "deleted_at": None
        })
        
        if not comment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found"
            )
        
        # Create report
        report = {
            "report_id": str(uuid.uuid4()),
            "comment_id": comment_id,
            "thread_id": comment["thread_id"],
            "reported_by": user_id,
            "reported_user_id": comment["user_id"],
            "reason": data.reason.strip(),
            "reason_category": data.reason_category or "other",
            "status": "pending",
            "created_at": now
        }
        
        await db.discussion_reports.insert_one(report)
        
        logger.info(f"Comment {comment_id} reported by user {user_id}")
        
        return ReportResponse(
            message="Report submitted. Thank you for helping keep our community safe. Remember: Do not share patient identifiers in discussions.",
            report_id=report["report_id"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Report comment error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit report"
        )
