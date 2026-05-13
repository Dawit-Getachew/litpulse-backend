"""
Subscription and Verification Utilities for LitPulse v2.1
Handles subscription level checks and verification status
"""
from typing import Optional, Dict, Any

# Feature flags
REQUIRE_VERIFIED_FOR_POSTING = False  # Future enforcement hook - DO NOT ENABLE NOW

def get_subscription_level(user: Dict[str, Any]) -> int:
    """
    Safely get user's subscription level.
    Returns 1 (free tier) if field is missing or invalid.
    
    Subscription levels:
    - 1: Free tier (default)
    - 2: Level 2 (Professional verification available)
    """
    if not user:
        return 1
    level = user.get("subscription_level")
    if level is None or not isinstance(level, (int, float)):
        return 1
    return int(level)

def is_level_2_subscriber(user: Dict[str, Any]) -> bool:
    """Check if user is Level 2 or higher subscriber"""
    return get_subscription_level(user) >= 2

def get_verification_status(verification_doc: Optional[Dict[str, Any]]) -> str:
    """
    Safely get verification status from verification document.
    Returns "not_submitted" if document is missing or status is invalid.
    
    Valid statuses:
    - not_submitted: User hasn't submitted verification
    - pending: Verification submitted, awaiting review/confirmation
    - verified: User is verified
    - rejected: Verification was rejected
    """
    if not verification_doc:
        return "not_submitted"
    status = verification_doc.get("status")
    if status not in ["not_submitted", "pending", "verified", "rejected"]:
        return "not_submitted"
    return status

def is_verified(verification_doc: Optional[Dict[str, Any]]) -> bool:
    """Check if user has verified status"""
    return get_verification_status(verification_doc) == "verified"

def can_post_in_discussions(user: Dict[str, Any], verification_doc: Optional[Dict[str, Any]]) -> tuple[bool, str]:
    """
    Check if user can post in discussions.
    Currently always returns True (REQUIRE_VERIFIED_FOR_POSTING = False).
    Future: Will enforce verification when flag is enabled.
    
    Returns: (can_post: bool, reason: str)
    """
    if not REQUIRE_VERIFIED_FOR_POSTING:
        return True, "ok"
    
    # Future enforcement logic (not active now)
    if not is_verified(verification_doc):
        return False, "Professional verification required to post. Please verify your credentials in Settings."
    
    return True, "ok"
