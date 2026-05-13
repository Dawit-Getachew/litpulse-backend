"""
Notification Models for LitPulse v2.1
In-app inbox/activity notifications
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal, List
from datetime import datetime

# Notification types
NotificationType = Literal["reply", "mention", "briefing"]

# ============================================================
# RESPONSE MODELS
# ============================================================

class UnreadCountResponse(BaseModel):
    """Response model for unread notification count"""
    unread_count: int

class NotificationItem(BaseModel):
    """Single notification item"""
    notification_id: str
    type: NotificationType
    actor_name: Optional[str] = None
    specialty_name: Optional[str] = None
    thread_id: Optional[str] = None  # Optional for briefing notifications
    thread_title: Optional[str] = None
    comment_id: Optional[str] = None
    summary_text: str  # PHI-safe summary, e.g. "New reply in Cardiology"
    created_at: str
    read_at: Optional[str] = None
    is_read: bool = False
    # Briefing notification fields
    briefing_id: Optional[str] = None
    digest_id: Optional[str] = None

class NotificationListResponse(BaseModel):
    """Response model for notification list"""
    notifications: List[NotificationItem]
    total: int
    has_more: bool = False
    next_cursor: Optional[str] = None

# ============================================================
# REQUEST MODELS
# ============================================================

class MarkReadRequest(BaseModel):
    """Request model for marking notifications as read"""
    notification_ids: Optional[List[str]] = None
    mark_all: bool = False

class MarkThreadReadRequest(BaseModel):
    """Request model for marking thread notifications as read"""
    thread_id: str

class MarkReadResponse(BaseModel):
    """Response model for mark read operation"""
    message: str
    marked_count: int
