from pydantic import BaseModel, Field, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime
import re

class PracticeProfile(BaseModel):
    """Optional practice details collected at signup."""
    primary_specialty: Optional[str] = None
    specialty_2: Optional[str] = None
    subspecialties: Optional[List[str]] = Field(default=None, max_length=3)
    current_stage: Optional[str] = None
    years_in_practice: Optional[str] = None
    country: Optional[str] = None
    state_province: Optional[str] = None
    city: Optional[str] = None
    practice_setting: Optional[str] = None
    clinical_environment: Optional[str] = None


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    timezone: Optional[str] = "UTC"
    invite_code: Optional[str] = None
    practice_profile: Optional[PracticeProfile] = None
    
    @field_validator('password')
    @classmethod
    def validate_password(cls, v):
        """Validate password strength"""
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not re.search(r'[a-z]', v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not re.search(r'\d', v):
            raise ValueError('Password must contain at least one digit')
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', v):
            raise ValueError('Password must contain at least one special character')
        return v

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ---------------------------------------------------------------------------
# Week-1 LitPortal merger — canonical save-to-LitHub payload (proposal §3.4)
# ---------------------------------------------------------------------------

class LibrarySavePayload(BaseModel):
    """Canonical body for ``POST /api/library/save`` after the LitPortal merger.

    LitHub keys by PMID first, DOI second. Records lacking both are rejected
    with HTTP 422. Additional fields are persisted verbatim onto db.library
    and db.user_articles so future features can rely on them without a fresh
    metadata round-trip.
    """
    pmid: Optional[str] = None
    doi: Optional[str] = None
    folder: str = "Inbox"
    title: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[int] = None
    full_text_status: Optional[str] = None  # "available" | "unavailable" | "unknown"
    best_full_text_url: Optional[str] = None
    publication_type: Optional[List[str]] = None
    recommended: bool = False
    selected: bool = False
    source: str = "search"  # "litportal" | "search" | "digest" | "litscholar"
    answer_context_id: Optional[str] = None
    portal_engine_record_id: Optional[str] = None

class TokenVerificationRequest(BaseModel):
    token: str

class PasswordResetRequest(BaseModel):
    email: EmailStr

class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str
    
    @field_validator('new_password')
    @classmethod
    def validate_password(cls, v):
        """Validate password strength"""
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not re.search(r'[a-z]', v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not re.search(r'\d', v):
            raise ValueError('Password must contain at least one digit')
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', v):
            raise ValueError('Password must contain at least one special character')
        return v

class UserResponse(BaseModel):
    user_id: str
    email: str
    full_name: Optional[str] = None
    is_verified: bool
    is_active: bool
    timezone: str
    created_at: str
    updated_at: str
    # v3.0: Enhanced /me response (additive, None for login/signup)
    plan_tier: Optional[str] = None
    peer_verification_status: Optional[str] = None
    capabilities: Optional[dict] = None
    # v3.0 Step 4: Trial info (legacy Stripe trial)
    trial_ends_at: Optional[str] = None
    trial_active: Optional[bool] = None
    has_subscription: Optional[bool] = None
    # Phase 2: Explicit opt-in trial fields
    trial_expires_at: Optional[str] = None   # set by POST /api/billing/start-trial
    trial_used: Optional[bool] = None        # true once trial has been activated

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse

class ArticleSearchRequest(BaseModel):
    query: str
    topics: Optional[list] = None
    journals: Optional[list] = None
    study_designs: Optional[list] = None
    date_range_days: int = Field(default=30, ge=1, le=365)

class MoveArticleRequest(BaseModel):
    article_id: str
    folder: Optional[str] = None

class RunDigestRequest(BaseModel):
    send_email: bool = False
    profile_id: Optional[str] = None  # Optional: run a specific profile

class FeedbackRequest(BaseModel):
    pmid: str
    feedback: str  # "useful" or "not_relevant"


# ============================================================
# PHASE A v2 MODELS - Notes, Reading Tracking, Article Detail
# ============================================================

# Notes Models (A3)
class NoteCreate(BaseModel):
    article_id: str
    body: str = Field(..., min_length=1, max_length=10000)

class NoteUpdate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000)

class NoteResponse(BaseModel):
    note_id: str
    user_id: str
    article_id: str
    body: str
    created_at: str
    updated_at: str

# Reading Tracking Models (A4)
class ReadingOpenedRequest(BaseModel):
    article_id: str

class MarkReadRequest(BaseModel):
    article_id: str
    is_read: bool = True

class ReadingProgressResponse(BaseModel):
    goal_weekly: int
    read_count_this_week: int
    opened_count_this_week: int
    start_of_week: str
    end_of_week: str

# Article Detail Models (A1, A2)
class UserArticleState(BaseModel):
    saved_to_library: bool = False
    saved_at: Optional[str] = None
    relevance_feedback: Optional[str] = None
    last_opened_at: Optional[str] = None
    opened_count: int = 0
    is_read: bool = False
    read_at: Optional[str] = None
    folder: Optional[str] = None

class ArticleDetailResponse(BaseModel):
    # Core article fields
    pmid: Optional[str] = None
    article_id: str
    title: str
    journal: Optional[str] = None
    pub_date: Optional[str] = None
    authors: Optional[str] = None
    abstract: Optional[str] = None
    url: Optional[str] = None
    ai_summary: Optional[str] = None
    key_findings: Optional[List[str]] = None
    design_tags: Optional[List[str]] = None
    mesh_terms: Optional[List[str]] = None
    # User-specific state
    user_state: UserArticleState
    note_count: int = 0

# Topic Dashboard Models (A2)
class TopicSummary(BaseModel):
    topic_name: str
    total_saved_count: int
    new_this_week_count: int
    new_this_month_count: int

class TopicsDashboardResponse(BaseModel):
    topics: List[TopicSummary]
    total_articles: int
    total_read: int

