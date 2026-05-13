"""
Capabilities Engine — Single source of truth for user entitlements.

plan_tier:  'free' | 'premium'  (derived from user doc)
peer_verification_status:  'none' | 'pending' | 'verified' | 'verified_provisional' | 'rejected'
capabilities:  full object with safe defaults

Tier model:
  - During trial: full Pro access + community posting for everyone
  - Post-trial Free: 1 digest profile, library, community read-only
  - Post-trial Verified Clinician: 3 digest profiles + community posting
  - Post-trial Pro (paid): 5 digest profiles + audio + LitScholar, but NO community posting unless verified
"""
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from fastapi import HTTPException, status


# ---------------------------------------------------------------------------
# Trial helpers
# ---------------------------------------------------------------------------

def _is_new_trial_active(user: Dict[str, Any], flags: Dict[str, Any]) -> bool:
    """
    Returns True ONLY when:
      1. ENABLE_PREMIUM_TRIALS=true
      2. user.trial_expires_at is set and is in the future

    PHI-Zero: no user text is logged or returned.
    """
    if not flags.get("enable_premium_trials", False):
        return False
    trial_exp = user.get("trial_expires_at")
    if not trial_exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(trial_exp.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < exp_dt
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Derivation helpers
# ---------------------------------------------------------------------------

def derive_plan_tier(user: Dict[str, Any]) -> str:
    """Derive plan_tier from user document.
    Priority: active subscription > active trial > legacy subscription_level > free.

    NOTE: This only checks the OLD trial field (trial_ends_at, set automatically on signup).
    The new Phase-2 trial (trial_expires_at) is handled separately in compute_capabilities()
    and require_premium() and does NOT change plan_tier.
    """
    if not user:
        return "free"
    # Explicit plan_tier from Stripe subscription
    pt = user.get("plan_tier")
    if pt == "premium":
        return "premium"
    # Active trial check (OLD signup-based trial, unchanged)
    trial_ends = user.get("trial_ends_at")
    if trial_ends:
        try:
            end_dt = datetime.fromisoformat(trial_ends.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < end_dt:
                return "premium"
        except (ValueError, TypeError):
            pass
    # Legacy fallback
    level = user.get("subscription_level")
    if isinstance(level, (int, float)) and int(level) >= 2:
        return "premium"
    return "free"


def derive_peer_verification_status(
    verification_doc: Optional[Dict[str, Any]],
) -> str:
    if not verification_doc:
        return "none"
    s = verification_doc.get("status")
    if s in ("verified", "verified_provisional", "pending", "rejected"):
        return s
    return "none"


def is_clinician_verified(verification_doc: Optional[Dict[str, Any]]) -> bool:
    """Returns True if the user is a verified clinician (full or provisional)."""
    if not verification_doc:
        return False
    s = verification_doc.get("status")
    return s in ("verified", "verified_provisional")


# ---------------------------------------------------------------------------
# Capabilities computation
# ---------------------------------------------------------------------------

def compute_capabilities(
    user: Dict[str, Any],
    verification_doc: Optional[Dict[str, Any]] = None,
    feature_flags: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute the full capabilities object for a user.

    Community posting policy:
      - trial_active => can post (everyone gets community access during trial)
      - trial expired + verified clinician => can post
      - trial expired + Pro (paid, not verified) => CANNOT post
      - trial expired + free => CANNOT post

    Digest profile limits:
      - Pro/Trial: 5
      - Verified clinician (not pro, trial expired): 3
      - Free: 1
    """
    flags = feature_flags or {}
    plan_tier = derive_plan_tier(user)

    # Trial grants premium capabilities without changing plan_tier
    trial_active = _is_new_trial_active(user, flags)
    is_premium = (plan_tier == "premium") or trial_active

    peer_status = derive_peer_verification_status(verification_doc)
    is_admin = user.get("email") == flags.get("admin_email", "")
    verified_clinician = is_clinician_verified(verification_doc)

    # Community write:
    #   When require_verified_for_posting=true (beta): only verified clinicians + admin
    #   When flag off: trial_active => everyone can post, else verified clinicians only
    if flags.get("require_verified_for_posting", False):
        can_write = verified_clinician or is_admin
    else:
        can_write = trial_active or verified_clinician or is_admin

    # Digest profile limits: Pro/Trial=5, Verified=3, Free=1
    if is_premium:
        max_profiles = 5
    elif verified_clinician:
        max_profiles = 3
    else:
        max_profiles = 1

    return {
        # ---- Community ----
        "community_read": True,
        "community_write": can_write,
        "community_react": can_write,
        "community_attach": can_write,
        "community_moderate": is_admin,

        # ---- Premium export ----
        "premium_export_csv": is_premium,
        "premium_export_ris": is_premium,

        # ---- Premium features ----
        "premium_audio": is_premium and flags.get("enable_audio_takeaway", False),
        "premium_copilot": is_premium,

        # ---- Messaging (future) ----
        "messaging_read": False,
        "messaging_write": False,

        # ---- Copilot ----
        "copilot_basic": True,
        "copilot_premium": is_premium,

        # ---- Rate / quota limits ----
        "max_digests_per_day": 10 if is_premium else 3,
        "max_digest_profiles": max_profiles,
        "max_notes_per_article": 50 if is_premium else 10,
        "max_threads_per_day": 20 if is_premium else 5,
        "max_library_articles": 1000 if is_premium else 100,
        "max_articles_per_digest": 25 if is_premium else 10,
        "run_now_per_24h": 5 if is_premium else 1,
        "audio_generations_per_24h": 20 if is_premium else 0,
        "copilot_calls_per_24h": 50 if is_premium else 0,

        # ---- Copilot surfaces (respect kill switch) ----
        "copilot_evidence_brief": is_premium and flags.get("enable_copilot", False),
        "copilot_ask_article": is_premium and flags.get("enable_copilot", False),
        "copilot_compare_studies": is_premium and flags.get("enable_copilot", False),
        "copilot_draft_post": is_premium and flags.get("enable_copilot", False),
    }


# ---------------------------------------------------------------------------
# Enforcement helpers
# ---------------------------------------------------------------------------

async def require_verified_peer(user_id: str, db) -> None:
    """Raise 403 if user cannot post in community.
    
    Policy (controlled by REQUIRE_VERIFIED_FOR_POSTING flag):

    When flag ON (beta-safe mode):
      - admin => allowed
      - work-email verified (status=verified) => allowed
      - NPI verified_provisional => allowed ONLY if ALLOW_NPI_SELF_ATTESTATION=true
      - everyone else (including trial users) => blocked
      This prevents unverified trial users from posting.

    When flag OFF (original permissive mode):
      - trial_active => allowed (everyone posts during trial)
      - verified clinician => allowed
      - everyone else => blocked
    """
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()

    user = await db.users.find_one(
        {"user_id": user_id},
        {"_id": 0, "trial_expires_at": 1, "trial_ends_at": 1,
         "plan_tier": 1, "subscription_level": 1, "email": 1, "beta_status": 1},
    )

    # Admin override (always)
    admin_email = flags.get("admin_email", "")
    if user and admin_email and user.get("email") == admin_email:
        return

    # Beta gate: must be active_beta to post (when invite-only beta is on)
    import os
    if os.environ.get("ENABLE_INVITE_ONLY_BETA", "false").lower() == "true":
        if not user or user.get("beta_status") != "active_beta":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "beta_access_required", "message": "Active beta access required to post."},
            )

    # Beta-safe posting policy
    if flags.get("require_verified_for_posting", False):
        verification_doc = await db.professional_verifications.find_one(
            {"user_id": user_id}, {"_id": 0, "status": 1, "method": 1}
        )
        if not verification_doc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "verification_required",
                    "message": "Professional verification required to post. Verify via work email or NPI.",
                },
            )
        ver_status = verification_doc.get("status")
        # Full verification (work-email) always allowed
        if ver_status == "verified":
            return
        # NPI provisional allowed only if self-attestation flag is on
        if ver_status == "verified_provisional" and flags.get("allow_npi_self_attestation", False):
            return
        # Pending or rejected — not allowed
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "verification_required",
                "message": "Professional verification required to post. Verify via work email or NPI.",
            },
        )

    # --- Original permissive policy (flag OFF) ---
    if user and _is_new_trial_active(user, flags):
        return  # Trial active — posting allowed

    # Check old trial (trial_ends_at)
    if user:
        trial_ends = user.get("trial_ends_at")
        if trial_ends:
            try:
                end_dt = datetime.fromisoformat(trial_ends.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < end_dt:
                    return  # Old trial still active — posting allowed
            except (ValueError, TypeError):
                pass

    # Check clinician verification
    verification_doc = await db.professional_verifications.find_one(
        {"user_id": user_id}, {"_id": 0, "status": 1}
    )
    if is_clinician_verified(verification_doc):
        return  # Verified clinician — posting allowed

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "verification_required",
            "message": "Clinician verification required to participate in discussions. Verify via work email or NPI.",
        },
    )


async def require_premium(user_id: str, db) -> None:
    """Raise 403 if user does not have premium access.

    Checks (in order):
    1. plan_tier == 'premium' (Stripe or manual)
    2. Phase-2 trial active (only when ENABLE_PREMIUM_TRIALS=true)
    """
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()

    # Fetch only the fields we need — no user-entered text involved
    user = await db.users.find_one(
        {"user_id": user_id},
        {"_id": 0, "subscription_level": 1, "plan_tier": 1,
         "trial_ends_at": 1, "trial_expires_at": 1},
    )
    plan = derive_plan_tier(user or {})
    trial_active = _is_new_trial_active(user or {}, flags)

    if plan != "premium" and not trial_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "premium_required",
                "message": "Pro subscription required for this feature.",
            },
        )
