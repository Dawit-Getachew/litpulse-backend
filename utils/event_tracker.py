"""
PHI-safe event tracking for LitPulse founder dashboard.

Records structured events to the `analytics_events` collection.
NEVER logs user-entered text — only event types, user IDs, and metadata.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Module-level db reference
_db = None


def set_db(database):
    global _db
    _db = database


async def track_event(
    event_type: str,
    user_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
):
    """Record a PHI-safe analytics event.
    
    Args:
        event_type: e.g. 'signup', 'login', 'digest_generated', 'audio_generated'
        user_id: the acting user (empty for anonymous events)
        metadata: optional dict of safe metadata (no user text!)
    """
    if _db is None:
        return
    try:
        doc = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "user_id": user_id,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await _db.analytics_events.insert_one(doc)
    except Exception as e:
        logger.debug("EVENT_TRACK: failed to record %s: %s", event_type, type(e).__name__)
