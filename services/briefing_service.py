"""
Daily Briefing Service for LitPulse Premium.
After a digest is generated, auto-generates audio for all articles
and sends an email + in-app notification: "Your briefing is ready."
PHI-Zero: only uses article metadata for audio.
"""
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


async def create_daily_briefing(
    db: AsyncIOMotorDatabase,
    user_id: str,
    digest_id: str,
    article_count: int,
) -> Optional[Dict]:
    """
    Create a daily briefing after digest generation.
    - Generates audio for all articles in the digest (async, non-blocking errors)
    - Creates in-app notification
    - Sends briefing email
    Only called for premium users when ENABLE_AUDIO_TAKEAWAY is true.
    """
    try:
        from utils.feature_flags import get_feature_flags
        from utils.capabilities import derive_plan_tier

        flags = get_feature_flags()
        if not flags.get("enable_audio_takeaway"):
            return None

        # Verify user is premium
        user = await db.users.find_one(
            {"user_id": user_id},
            {"_id": 0, "email": 1, "full_name": 1, "plan_tier": 1, "subscription_level": 1},
        )
        if not user or derive_plan_tier(user) != "premium":
            return None

        # Get digest articles
        digest = await db.digests.find_one(
            {"digest_id": digest_id}, {"_id": 0, "articles": 1}
        )
        if not digest or not digest.get("articles"):
            return None

        from bson import ObjectId

        article_ids = digest["articles"]
        pmids = []
        for aid in article_ids:
            if ObjectId.is_valid(aid):
                art = await db.articles.find_one(
                    {"_id": ObjectId(aid)}, {"_id": 0, "pmid": 1}
                )
                if art and art.get("pmid"):
                    pmids.append(art["pmid"])

        if not pmids:
            return None

        # Generate audio for all articles (best-effort, don't block)
        from services.audio_service import AudioService

        audio_svc = AudioService(db)
        ready_count = 0
        for pmid in pmids:
            try:
                result = await audio_svc.generate_audio(pmid, user_id)
                if result and result.get("status") == "ready":
                    ready_count += 1
            except Exception:
                pass  # Non-critical; some may fail

        # Estimate duration (rough: ~5s per mock article)
        total_duration_min = max(1, round(ready_count * 5 / 60))

        # Save briefing record
        now = datetime.now(timezone.utc).isoformat()
        briefing_id = str(uuid.uuid4())
        briefing_doc = {
            "briefing_id": briefing_id,
            "user_id": user_id,
            "digest_id": digest_id,
            "article_count": len(pmids),
            "audio_ready_count": ready_count,
            "estimated_minutes": total_duration_min,
            "created_at": now,
        }
        await db.daily_briefings.insert_one(briefing_doc)

        # Create in-app notification
        user_name = user.get("full_name") or user.get("email", "").split("@")[0]
        summary_text = f"Your {total_duration_min}-minute literature briefing is ready ({ready_count} audio takeaways)"

        notification = {
            "notification_id": str(uuid.uuid4()),
            "user_id": user_id,
            "actor_user_id": "system",
            "type": "briefing",
            "thread_id": None,
            "thread_title": None,
            "specialty_name": None,
            "comment_id": None,
            "actor_name": "LitPulse",
            "summary_text": summary_text,
            "briefing_id": briefing_id,
            "digest_id": digest_id,
            "created_at": now,
            "read_at": None,
        }
        await db.user_notifications.insert_one(notification)
        logger.info(
            "BRIEFING: created for user=%s digest=%s articles=%d audio_ready=%d",
            user_id, digest_id, len(pmids), ready_count,
        )

        # Send email
        try:
            from email_service import send_briefing_email

            send_briefing_email(
                email=user["email"],
                name=user_name,
                article_count=len(pmids),
                audio_ready=ready_count,
                duration_min=total_duration_min,
                digest_id=digest_id,
            )
        except Exception as e:
            logger.warning("BRIEFING: email send failed: %s", str(e))

        return briefing_doc

    except Exception as e:
        logger.error("BRIEFING: creation failed for user=%s: %s", user_id, str(e))
        return None
