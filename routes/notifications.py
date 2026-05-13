"""
Notification Routes for LitPulse v2.1
In-app inbox/activity notifications for replies
"""
from fastapi import APIRouter, HTTPException, Depends, status, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime, timezone
import uuid
import logging
from typing import Optional, List

from auth_utils import get_current_user
from notification_models import (
    UnreadCountResponse,
    NotificationItem,
    NotificationListResponse,
    MarkReadRequest,
    MarkReadResponse
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

# Database reference (set by main app)
db: AsyncIOMotorDatabase = None

def set_db(database: AsyncIOMotorDatabase):
    """Set the database reference for the router"""
    global db
    db = database

# ============================================================
# HELPER FUNCTIONS
# ============================================================

async def create_reply_notification(
    recipient_user_id: str,
    actor_user_id: str,
    thread_id: str,
    comment_id: str
):
    """
    Create a notification when someone replies to a user's comment.
    Called from the discussions router when a reply is created.
    """
    try:
        # Don't notify yourself
        if recipient_user_id == actor_user_id:
            return
        
        # Get actor name
        actor = await db.users.find_one(
            {"user_id": actor_user_id},
            {"_id": 0, "full_name": 1, "email": 1}
        )
        actor_name = (actor.get("full_name") or actor.get("email", "").split("@")[0]) if actor else "Someone"
        
        # Get thread info
        thread = await db.discussion_threads.find_one(
            {"thread_id": thread_id},
            {"_id": 0, "title": 1, "specialty_id": 1}
        )
        thread_title = thread.get("title", "a discussion") if thread else "a discussion"
        specialty_id = thread.get("specialty_id") if thread else None
        
        # Get specialty name if available
        specialty_name = None
        if specialty_id:
            from pathlib import Path
            import json
            config_path = Path(__file__).parent.parent / "config" / "specialty_config.json"
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                for spec in config.get("specialties", []):
                    if spec["id"] == specialty_id:
                        specialty_name = spec.get("label") or spec.get("name")
                        break
            except:
                pass
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Build PHI-safe summary
        if specialty_name:
            summary_text = f"{actor_name} replied to your comment in {specialty_name}"
        else:
            summary_text = f"{actor_name} replied to your comment"
        
        notification = {
            "notification_id": str(uuid.uuid4()),
            "user_id": recipient_user_id,
            "actor_user_id": actor_user_id,
            "type": "reply",
            "thread_id": thread_id,
            "thread_title": thread_title[:100],  # Truncate for safety
            "specialty_name": specialty_name,
            "comment_id": comment_id,
            "actor_name": actor_name,
            "summary_text": summary_text,
            "created_at": now,
            "read_at": None
        }
        
        await db.user_notifications.insert_one(notification)
        logger.info(f"Created reply notification for user {recipient_user_id}")
        
    except Exception as e:
        logger.error(f"Error creating reply notification: {str(e)}")
        # Don't fail the main operation

# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(current_user: dict = Depends(get_current_user)):
    """Get count of unread notifications for the current user"""
    try:
        user_id = current_user["user_id"]
        
        count = await db.user_notifications.count_documents({
            "user_id": user_id,
            "read_at": None
        })
        
        return UnreadCountResponse(unread_count=count)
        
    except Exception as e:
        logger.error(f"Get unread count error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get unread count"
        )

@router.get("/", response_model=NotificationListResponse)
async def get_notifications(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    unread_only: bool = Query(default=False),
    current_user: dict = Depends(get_current_user)
):
    """Get paginated list of notifications for the current user"""
    try:
        user_id = current_user["user_id"]
        
        # Build query
        query = {"user_id": user_id}
        
        if unread_only:
            query["read_at"] = None
        
        # Add cursor pagination if provided
        if cursor:
            query["created_at"] = {"$lt": cursor}
        
        # Get notifications sorted by created_at descending
        notifications = await db.user_notifications.find(
            query,
            {"_id": 0}
        ).sort("created_at", -1).limit(limit + 1).to_list(limit + 1)
        
        # Check if there are more
        has_more = len(notifications) > limit
        if has_more:
            notifications = notifications[:limit]
        
        # Build response items
        items = []
        for notif in notifications:
            items.append(NotificationItem(
                notification_id=notif["notification_id"],
                type=notif["type"],
                actor_name=notif.get("actor_name"),
                specialty_name=notif.get("specialty_name"),
                thread_id=notif.get("thread_id"),  # Now optional for briefing notifications
                thread_title=notif.get("thread_title"),
                comment_id=notif.get("comment_id"),
                summary_text=notif.get("summary_text", "New notification"),
                created_at=notif["created_at"],
                read_at=notif.get("read_at"),
                is_read=notif.get("read_at") is not None,
                # Briefing notification fields
                briefing_id=notif.get("briefing_id"),
                digest_id=notif.get("digest_id")
            ))
        
        # Get next cursor
        next_cursor = notifications[-1]["created_at"] if has_more and notifications else None
        
        # Get total count
        total = await db.user_notifications.count_documents({"user_id": user_id})
        
        return NotificationListResponse(
            notifications=items,
            total=total,
            has_more=has_more,
            next_cursor=next_cursor
        )
        
    except Exception as e:
        logger.error(f"Get notifications error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get notifications"
        )

@router.post("/mark-read", response_model=MarkReadResponse)
async def mark_notifications_read(
    data: MarkReadRequest,
    current_user: dict = Depends(get_current_user)
):
    """Mark notifications as read (specific IDs or all)"""
    try:
        user_id = current_user["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        
        if data.mark_all:
            # Mark all unread notifications as read
            result = await db.user_notifications.update_many(
                {"user_id": user_id, "read_at": None},
                {"$set": {"read_at": now}}
            )
            marked_count = result.modified_count
        elif data.notification_ids:
            # Mark specific notifications as read
            result = await db.user_notifications.update_many(
                {
                    "user_id": user_id,
                    "notification_id": {"$in": data.notification_ids},
                    "read_at": None
                },
                {"$set": {"read_at": now}}
            )
            marked_count = result.modified_count
        else:
            marked_count = 0
        
        return MarkReadResponse(
            message="Notifications marked as read",
            marked_count=marked_count
        )
        
    except Exception as e:
        logger.error(f"Mark notifications read error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark notifications as read"
        )

@router.post("/mark-thread-read", response_model=MarkReadResponse)
async def mark_thread_notifications_read(
    thread_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Mark all notifications for a specific thread as read"""
    try:
        user_id = current_user["user_id"]
        now = datetime.now(timezone.utc).isoformat()
        
        result = await db.user_notifications.update_many(
            {"user_id": user_id, "thread_id": thread_id, "read_at": None},
            {"$set": {"read_at": now}}
        )
        
        return MarkReadResponse(
            message=f"Thread notifications marked as read",
            marked_count=result.modified_count
        )
        
    except Exception as e:
        logger.error(f"Mark thread notifications read error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark thread notifications as read"
        )
