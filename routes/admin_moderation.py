"""
Admin Moderation Routes for LitPulse v3.0 Step 3
Report management, content removal, user suspension.
All endpoints require admin authentication.
"""
from fastapi import APIRouter, HTTPException, Depends, status, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Optional, List, Literal
import logging

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-moderation"])

db: AsyncIOMotorDatabase = None
_admin_email: str = ""


def set_db(database: AsyncIOMotorDatabase):
    global db
    db = database


def set_admin_email(email: str):
    global _admin_email
    _admin_email = email.lower()


async def verify_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not _admin_email:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin not configured")
    user = await db.users.find_one({"user_id": current_user["user_id"]}, {"_id": 0, "email": 1})
    if user and user.get("email", "").lower() == _admin_email:
        return current_user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

ReasonCategory = Literal["phi", "spam", "harassment", "misinformation", "other"]

class ReportListItem(BaseModel):
    report_id: str
    comment_id: str
    thread_id: str
    reason_category: Optional[str] = "other"
    reason: Optional[str] = None
    reported_by: str
    reported_user_id: str
    status: str
    created_at: str

class ReportListResponse(BaseModel):
    reports: List[ReportListItem]
    total: int

class ReportDetailResponse(BaseModel):
    report_id: str
    comment_id: str
    thread_id: str
    reason_category: Optional[str] = "other"
    reason: Optional[str] = None
    reported_by: str
    reported_user_id: str
    status: str
    created_at: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None
    thread_title: Optional[str] = None
    specialty_id: Optional[str] = None
    comment_author_name: Optional[str] = None
    comment_created_at: Optional[str] = None
    comment_body_preview: Optional[str] = None

class RemoveCommentRequest(BaseModel):
    comment_id: str
    reason: str = Field(..., min_length=1, max_length=500)

class SuspendUserRequest(BaseModel):
    user_id: str
    reason: str = Field(..., min_length=1, max_length=500)
    duration_hours: Optional[int] = None

class UnsuspendUserRequest(BaseModel):
    user_id: str

class ResolveReportRequest(BaseModel):
    resolution_note: str = Field(..., min_length=1, max_length=1000)

class AdminCommentResponse(BaseModel):
    comment_id: str
    thread_id: str
    user_id: str
    user_name: Optional[str] = None
    body: Optional[str] = None
    created_at: str
    deleted_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _get_user_name(user_id: str) -> Optional[str]:
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0, "full_name": 1, "email": 1})
    if not user:
        return None
    return user.get("full_name") or user.get("email", "").split("@")[0]


# ---------------------------------------------------------------------------
# Report list / detail
# ---------------------------------------------------------------------------

@router.get("/reports", response_model=ReportListResponse)
async def list_reports(
    report_status: Optional[str] = Query("pending", alias="status"),
    reason: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    admin_user: dict = Depends(verify_admin),
):
    query = {}
    if report_status and report_status != "all":
        query["status"] = report_status
    if reason:
        query["reason_category"] = reason

    total = await db.discussion_reports.count_documents(query)
    docs = await db.discussion_reports.find(query, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)

    items = [
        ReportListItem(
            report_id=d["report_id"],
            comment_id=d.get("comment_id", ""),
            thread_id=d.get("thread_id", ""),
            reason_category=d.get("reason_category", "other"),
            reason=d.get("reason"),
            reported_by=d.get("reported_by", ""),
            reported_user_id=d.get("reported_user_id", ""),
            status=d.get("status", "pending"),
            created_at=d.get("created_at", ""),
        )
        for d in docs
    ]
    return ReportListResponse(reports=items, total=total)


@router.get("/reports/{report_id}", response_model=ReportDetailResponse)
async def get_report_detail(report_id: str, admin_user: dict = Depends(verify_admin)):
    doc = await db.discussion_reports.find_one({"report_id": report_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Report not found")

    # Gather context
    thread = await db.discussion_threads.find_one({"thread_id": doc.get("thread_id")}, {"_id": 0, "title": 1, "specialty_id": 1})
    comment = await db.discussion_comments.find_one({"comment_id": doc.get("comment_id")}, {"_id": 0, "user_id": 1, "body": 1, "created_at": 1})

    comment_author_name = None
    comment_created_at = None
    comment_body_preview = None
    if comment:
        comment_author_name = await _get_user_name(comment["user_id"])
        comment_created_at = comment.get("created_at")
        body = comment.get("body", "")
        comment_body_preview = body[:200] if body else None

    return ReportDetailResponse(
        report_id=doc["report_id"],
        comment_id=doc.get("comment_id", ""),
        thread_id=doc.get("thread_id", ""),
        reason_category=doc.get("reason_category", "other"),
        reason=doc.get("reason"),
        reported_by=doc.get("reported_by", ""),
        reported_user_id=doc.get("reported_user_id", ""),
        status=doc.get("status", "pending"),
        created_at=doc.get("created_at", ""),
        resolved_at=doc.get("resolved_at"),
        resolved_by=doc.get("resolved_by"),
        resolution_note=doc.get("resolution_note"),
        thread_title=thread.get("title") if thread else None,
        specialty_id=thread.get("specialty_id") if thread else None,
        comment_author_name=comment_author_name,
        comment_created_at=comment_created_at,
        comment_body_preview=comment_body_preview,
    )


@router.post("/reports/{report_id}/resolve")
async def resolve_report(report_id: str, data: ResolveReportRequest, admin_user: dict = Depends(verify_admin)):
    result = await db.discussion_reports.update_one(
        {"report_id": report_id},
        {"$set": {
            "status": "resolved",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolved_by": admin_user["user_id"],
            "resolution_note": data.resolution_note,
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Report not found")
    logger.info("MODERATION: report=%s resolved by admin=%s", report_id, admin_user["user_id"])
    return {"message": "Report resolved", "report_id": report_id}


# ---------------------------------------------------------------------------
# Content moderation actions
# ---------------------------------------------------------------------------

@router.post("/moderation/remove-comment")
async def remove_comment(data: RemoveCommentRequest, admin_user: dict = Depends(verify_admin)):
    now = datetime.now(timezone.utc).isoformat()
    result = await db.discussion_comments.update_one(
        {"comment_id": data.comment_id, "deleted_at": None},
        {"$set": {"deleted_at": now, "moderated_by": admin_user["user_id"], "moderation_reason": data.reason}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found or already removed")
    logger.info("MODERATION: comment=%s removed by admin=%s reason_len=%d", data.comment_id, admin_user["user_id"], len(data.reason))
    return {"message": "Comment removed", "comment_id": data.comment_id}


@router.post("/moderation/suspend-user")
async def suspend_user(data: SuspendUserRequest, admin_user: dict = Depends(verify_admin)):
    target = await db.users.find_one({"user_id": data.user_id}, {"_id": 0, "email": 1})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # Don't allow suspending admin
    if target.get("email", "").lower() == _admin_email:
        raise HTTPException(status_code=400, detail="Cannot suspend admin user")

    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"user_id": data.user_id},
        {"$set": {"is_active": False, "suspended_at": now, "suspension_reason": data.reason, "updated_at": now}},
    )
    logger.info("MODERATION: user=%s suspended by admin=%s", data.user_id, admin_user["user_id"])
    return {"message": "User suspended", "user_id": data.user_id}


@router.post("/moderation/unsuspend-user")
async def unsuspend_user(data: UnsuspendUserRequest, admin_user: dict = Depends(verify_admin)):
    now = datetime.now(timezone.utc).isoformat()
    result = await db.users.update_one(
        {"user_id": data.user_id},
        {"$set": {"is_active": True, "updated_at": now}, "$unset": {"suspended_at": "", "suspension_reason": ""}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("MODERATION: user=%s unsuspended by admin=%s", data.user_id, admin_user["user_id"])
    return {"message": "User unsuspended", "user_id": data.user_id}


# ---------------------------------------------------------------------------
# Admin set plan tier
# ---------------------------------------------------------------------------

class SetPlanTierRequest(BaseModel):
    email: str
    plan_tier: Literal["free", "premium"]


@router.post("/users/set-plan-tier")
async def set_plan_tier(data: SetPlanTierRequest, admin_user: dict = Depends(verify_admin)):
    # Safety: block manual overrides when Stripe billing is source of truth
    import os
    billing_enabled = os.environ.get("ENABLE_STRIPE_BILLING", "false").lower() == "true"
    allow_override = os.environ.get("ALLOW_MANUAL_PLAN_OVERRIDE", "true").lower() == "true"
    if billing_enabled and not allow_override:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manual plan overrides are disabled when Stripe billing is active. Set ALLOW_MANUAL_PLAN_OVERRIDE=true to bypass.",
        )

    email_lower = data.email.lower()
    now = datetime.now(timezone.utc).isoformat()
    sub_level = 2 if data.plan_tier == "premium" else 1
    result = await db.users.update_one(
        {"email": email_lower},
        {"$set": {"plan_tier": data.plan_tier, "subscription_level": sub_level, "updated_at": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("ADMIN: plan_tier=%s set for email=%s by admin=%s", data.plan_tier, email_lower, admin_user["user_id"])
    return {"email": email_lower, "plan_tier": data.plan_tier, "updated_at": now}


# ---------------------------------------------------------------------------
# Admin comment viewer (PHI-safe)
# ---------------------------------------------------------------------------

@router.get("/comments/{comment_id}", response_model=AdminCommentResponse)
async def admin_get_comment(
    comment_id: str,
    include_body: bool = Query(False),
    admin_user: dict = Depends(verify_admin),
):
    comment = await db.discussion_comments.find_one({"comment_id": comment_id}, {"_id": 0})
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    user_name = await _get_user_name(comment["user_id"])
    body = comment.get("body") if include_body else (comment.get("body", "")[:200] if comment.get("body") else None)

    return AdminCommentResponse(
        comment_id=comment["comment_id"],
        thread_id=comment["thread_id"],
        user_id=comment["user_id"],
        user_name=user_name,
        body=body,
        created_at=comment.get("created_at", ""),
        deleted_at=comment.get("deleted_at"),
    )
