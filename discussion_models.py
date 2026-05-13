"""
Discussion Models for LitPulse v2 Community Features
Supports threads, comments, reactions, and specialty rooms
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict
from datetime import datetime

# ============================================================
# DISCUSSION TYPES
# ============================================================

ContextType = Literal["article", "digest", "topic", "specialty", "general"]
ReactionType = Literal["helpful", "insightful", "question"]

# ============================================================
# THREAD MODELS
# ============================================================

class ThreadCreate(BaseModel):
    """Request model for creating a new thread"""
    context_type: ContextType
    context_id: str
    specialty_id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=500)
    # Phase 6: article-linked thread (Discuss flow)
    primary_article_pmid: Optional[str] = None

class ThreadResponse(BaseModel):
    """Response model for a discussion thread"""
    thread_id: str
    context_type: ContextType
    context_id: str
    specialty_id: Optional[str] = None
    title: str
    created_by: str
    created_at: str
    last_activity_at: str
    is_pinned: bool = False
    comment_count: int = 0
    preview_comment: Optional[str] = None
    creator_name: Optional[str] = None
    creator_is_verified: bool = False  # v2.1: Verified badge support
    # Phase 6: article-linked thread
    primary_article_pmid: Optional[str] = None
    primary_article_title: Optional[str] = None

class ThreadListResponse(BaseModel):
    """Response model for list of threads"""
    threads: List[ThreadResponse]
    total: int

# ============================================================
# COMMENT MODELS
# ============================================================

class CommentCreate(BaseModel):
    """Request model for creating a comment"""
    body: str = Field(..., min_length=1, max_length=5000)
    parent_comment_id: Optional[str] = None
    attached_article_ids: Optional[List[str]] = None

class CommentUpdate(BaseModel):
    """Request model for updating a comment"""
    body: str = Field(..., min_length=1, max_length=5000)

class AttachedArticle(BaseModel):
    """Article preview for attached articles in comments"""
    pmid: str
    title: str
    journal: Optional[str] = None
    pub_date: Optional[str] = None
    design_tags: Optional[List[str]] = None
    is_in_library: bool = False

class AttachableArticle(BaseModel):
    """Article available for attachment to comments"""
    pmid: str
    title: str
    journal: Optional[str] = None
    pub_date: Optional[str] = None
    design_tags: Optional[List[str]] = None
    source: str  # "library" or "digest"
    is_in_library: bool = True

class AttachableArticlesResponse(BaseModel):
    """Response model for attachable articles"""
    library_articles: List[AttachableArticle] = []
    digest_articles: List[AttachableArticle] = []
    total: int = 0

class CommentResponse(BaseModel):
    """Response model for a comment"""
    comment_id: str
    thread_id: str
    user_id: str
    body: str
    parent_comment_id: Optional[str] = None
    attached_article_ids: Optional[List[str]] = None
    attached_articles: Optional[List[AttachedArticle]] = None
    reactions: Optional[Dict[str, List[str]]] = None  # {reaction_type: [user_ids]}
    created_at: str
    updated_at: str
    deleted_at: Optional[str] = None
    user_name: Optional[str] = None
    author_is_verified: bool = False  # v2.1: Verified badge support
    reply_count: int = 0

class ThreadDetailResponse(BaseModel):
    """Response model for thread with comments"""
    thread_id: str
    context_type: ContextType
    context_id: str
    specialty_id: Optional[str] = None
    title: str
    created_by: str
    created_at: str
    last_activity_at: str
    is_pinned: bool = False
    comment_count: int = 0
    creator_name: Optional[str] = None
    creator_is_verified: bool = False  # v2.1: Verified badge support
    comments: List[CommentResponse] = []

# ============================================================
# REACTION MODELS
# ============================================================

class ReactionRequest(BaseModel):
    """Request model for adding/toggling a reaction"""
    reaction_type: ReactionType

# ============================================================
# SPECIALTY ROOM MODELS
# ============================================================

class SpecialtyRoom(BaseModel):
    """Response model for a specialty room"""
    specialty_id: str
    specialty_name: str
    thread_count: int = 0
    member_count: int = 0
    last_activity: Optional[str] = None
    # Phase 6: V2 gating
    can_enter: Optional[bool] = None     # None = not evaluated (V1 mode)
    subspecialties: Optional[List[dict]] = None  # [{id, label}] from config
    # Phase UX-C: community subspecialty visibility
    eligible_subspecialties: Optional[List[dict]] = None  # Full list from config
    visible_subspecialties: Optional[List[dict]] = None   # User's selected subspecialties (max 3)
    can_post: Optional[bool] = None  # False for free users
    # Community V2: Access state based on digests
    access_state: Optional[str] = None  # "active" | "frozen" | "none" | None (V1 mode)
    frozen_at: Optional[str] = None  # ISO timestamp when access was frozen

class SpecialtyRoomListResponse(BaseModel):
    """Response model for list of specialty rooms"""
    rooms: List[SpecialtyRoom]

# ============================================================
# REPORT MODELS
# ============================================================

ReasonCategory = Literal["phi", "spam", "harassment", "misinformation", "other"]

class ReportRequest(BaseModel):
    """Request model for reporting a comment"""
    reason: str = Field(..., min_length=1, max_length=1000)
    reason_category: Optional[ReasonCategory] = "other"

class ReportResponse(BaseModel):
    """Response model for report submission"""
    message: str
    report_id: str
