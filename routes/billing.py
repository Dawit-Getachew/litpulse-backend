"""
Stripe Billing Routes for LitPulse Pro — Full Subscription Lifecycle.
Uses official Stripe SDK for portal + emergentintegrations for checkout.
PHI-Zero: stores only Stripe IDs, status, period dates. No patient data.

Phase 2 addition: POST /billing/start-trial — opt-in 30-day trial (no Stripe).
"""
from fastapi import APIRouter, HTTPException, Request, Depends, status
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from typing import Optional
import os
import uuid
import logging
import stripe as stripe_sdk

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

db = None
PRO_PRICE_MONTHLY = 9.99
TRIAL_DAYS = 30


def set_db(database):
    global db
    db = database


def _billing_enabled() -> bool:
    return (
        os.environ.get("ENABLE_STRIPE_BILLING", "false").lower() == "true"
        and bool(os.environ.get("STRIPE_API_KEY"))
    )


def _require_billing():
    if not _billing_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "billing_not_configured", "message": "Billing is not enabled."},
        )


def _portal_available(has_customer: bool) -> bool:
    """True only when all conditions met for real portal."""
    return (
        _billing_enabled()
        and has_customer
        and bool(os.environ.get("STRIPE_API_KEY"))
    )


def _trial_enabled() -> bool:
    from utils.feature_flags import get_feature_flags
    return get_feature_flags().get("enable_premium_trials", False)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CheckoutSessionRequest(BaseModel):
    origin_url: str

class PortalSessionRequest(BaseModel):
    origin_url: str


# ---------------------------------------------------------------------------
# GET /api/billing/me
# ---------------------------------------------------------------------------

@router.get("/me")
async def get_billing_status(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    billing_enabled = _billing_enabled()
    trials_enabled = _trial_enabled()

    from utils.capabilities import derive_plan_tier, _is_new_trial_active
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()

    user = await db.users.find_one(
        {"user_id": user_id},
        {"_id": 0, "plan_tier": 1, "subscription_level": 1, "trial_ends_at": 1,
         "trial_expires_at": 1, "trial_used": 1, "trial_started_at": 1},
    )
    plan_tier = derive_plan_tier(user or {})

    sub = await db.subscriptions.find_one({"user_id": user_id, "provider": "stripe"}, {"_id": 0})
    has_customer = sub is not None and bool(sub.get("customer_id"))
    pa = _portal_available(has_customer)

    # Phase-2 trial status
    trial_expires_at = user.get("trial_expires_at") if user else None
    trial_active = _is_new_trial_active(user or {}, flags)
    days_remaining = 0
    if trial_active and trial_expires_at:
        try:
            exp_dt = datetime.fromisoformat(trial_expires_at.replace("Z", "+00:00"))
            days_remaining = max(0, (exp_dt - datetime.now(timezone.utc)).days)
        except (ValueError, TypeError):
            pass

    trial_used = bool(user.get("trial_used")) if user else False

    return {
        "billing_enabled": False,           # Stripe not deployed yet
        "plan_tier": plan_tier,
        "subscription_status": sub.get("status") if sub else None,
        "current_period_end": sub.get("current_period_end") if sub else None,
        "cancel_at_period_end": sub.get("cancel_at_period_end") if sub else None,
        "has_customer": has_customer,
        "portal_available": pa,
        "portal_mode": "real" if pa else "disabled",
        # Phase-2 trial fields
        "trial_enabled": trials_enabled,
        "trial_active": trial_active,
        "trial_expires_at": trial_expires_at if trial_active else None,
        "days_remaining": days_remaining,
        "trial_used": trial_used,
    }


# ---------------------------------------------------------------------------
# POST /api/billing/start-trial  (Phase 2 — no Stripe required)
# ---------------------------------------------------------------------------

@router.post("/start-trial")
async def start_trial(current_user: dict = Depends(get_current_user)):
    """Start a 30-day Pro trial for the authenticated user.

    Rules:
    - ENABLE_PREMIUM_TRIALS must be true (returns 503 otherwise)
    - User must not already be premium (plan_tier or Stripe sub)
    - trial_used must be false (returns 409 if already used)
    - Atomic: uses find_one_and_update with trial_used=false filter to prevent races
    PHI-Zero: no user text is logged.
    """
    if not _trial_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "feature_disabled",
                "message": "Free trials are not currently available.",
            },
        )

    user_id = current_user["user_id"]

    # Pre-check: already premium?
    user_doc = await db.users.find_one(
        {"user_id": user_id},
        {"_id": 0, "plan_tier": 1, "subscription_level": 1,
         "trial_ends_at": 1, "trial_used": 1},
    )
    if not user_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    from utils.capabilities import derive_plan_tier
    if derive_plan_tier(user_doc) == "premium":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "already_premium",
                "message": "You already have a Pro subscription.",
            },
        )

    # Atomic update: only proceed if trial_used is false/missing
    now = datetime.now(timezone.utc)
    trial_expires_at = (now + timedelta(days=TRIAL_DAYS)).isoformat()
    now_iso = now.isoformat()

    updated = await db.users.find_one_and_update(
        {
            "user_id": user_id,
            "$or": [{"trial_used": {"$ne": True}}, {"trial_used": {"$exists": False}}],
        },
        {
            "$set": {
                "trial_used": True,
                "trial_started_at": now_iso,
                "trial_expires_at": trial_expires_at,
                "updated_at": now_iso,
            }
        },
        return_document=True,
    )

    if not updated:
        # Another concurrent request already activated it, OR trial was already used
        current = await db.users.find_one({"user_id": user_id}, {"_id": 0, "trial_used": 1})
        if current and current.get("trial_used"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "trial_already_used",
                    "message": "You have already used your free trial.",
                },
            )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to start trial")

    logger.info("TRIAL: started user=%s expires=%s", user_id, trial_expires_at)

    # Build fresh capabilities so the client can update state immediately
    verification_doc = await db.professional_verifications.find_one(
        {"user_id": user_id}, {"_id": 0}
    )
    from utils.feature_flags import get_feature_flags
    from utils.capabilities import compute_capabilities, derive_peer_verification_status
    flags = get_feature_flags()

    # Remove _id from updated doc before passing to capabilities
    updated.pop("_id", None)
    capabilities = compute_capabilities(updated, verification_doc, flags)

    return {
        "trial_started": True,
        "trial_started_at": now_iso,
        "trial_expires_at": trial_expires_at,
        "days_remaining": TRIAL_DAYS,
        "capabilities": capabilities,
    }


# ---------------------------------------------------------------------------
# POST /api/billing/stripe/checkout-session
# ---------------------------------------------------------------------------

@router.post("/stripe/checkout-session")
async def create_stripe_checkout(data: CheckoutSessionRequest, current_user: dict = Depends(get_current_user)):
    _require_billing()
    try:
        from emergentintegrations.payments.stripe.checkout import StripeCheckout, CheckoutSessionRequest as StripeCSR

        api_key = os.environ.get("STRIPE_API_KEY")
        user_id = current_user["user_id"]
        user = await db.users.find_one({"user_id": user_id}, {"_id": 0, "email": 1})
        email = user.get("email", "") if user else ""

        origin = data.origin_url.rstrip("/")
        success_url = f"{origin}/plan?billing=success&session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = f"{origin}/plan?billing=cancel"
        webhook_url = f"{origin}/api/billing/stripe/webhook"

        sc = StripeCheckout(api_key=api_key, webhook_url=webhook_url)
        req = StripeCSR(
            amount=PRO_PRICE_MONTHLY, currency="usd",
            success_url=success_url, cancel_url=cancel_url,
            metadata={"user_id": user_id, "email": email, "plan": "pro_monthly"},
        )
        session = await sc.create_checkout_session(req)

        now = datetime.now(timezone.utc).isoformat()
        await db.payment_transactions.update_one(
            {"session_id": session.session_id},
            {"$set": {
                "session_id": session.session_id, "user_id": user_id, "email": email,
                "amount": PRO_PRICE_MONTHLY, "currency": "usd", "plan": "pro_monthly",
                "payment_status": "initiated", "updated_at": now,
            }, "$setOnInsert": {"transaction_id": str(uuid.uuid4()), "created_at": now}},
            upsert=True,
        )

        logger.info("BILLING: checkout user=%s session=%s", user_id, session.session_id)
        return {"url": session.url, "session_id": session.session_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("BILLING: checkout error: %s", str(e))
        raise HTTPException(status_code=500, detail="Failed to create checkout session")


# ---------------------------------------------------------------------------
# POST /api/billing/stripe/portal-session (REAL Stripe SDK)
# ---------------------------------------------------------------------------

@router.post("/stripe/portal-session")
async def create_portal_session(data: PortalSessionRequest, current_user: dict = Depends(get_current_user)):
    """Create a real Stripe Customer Portal session."""
    _require_billing()

    user_id = current_user["user_id"]
    sub = await db.subscriptions.find_one(
        {"user_id": user_id, "provider": "stripe"}, {"_id": 0, "customer_id": 1}
    )
    customer_id = sub.get("customer_id") if sub else None

    if not customer_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "portal_unavailable", "message": "No subscription found. Subscribe first."},
        )

    try:
        api_key = os.environ.get("STRIPE_API_KEY")
        stripe_sdk.api_key = api_key
        origin = data.origin_url.rstrip("/")
        return_url = f"{origin}/plan"

        portal_session = stripe_sdk.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )

        logger.info("BILLING: portal session user=%s customer=%s", user_id, customer_id)
        return {"url": portal_session.url}
    except stripe_sdk.error.InvalidRequestError as e:
        logger.error("BILLING: portal error (invalid request): %s", str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "portal_unavailable", "message": "Plan management is not available. Contact support."},
        )
    except Exception as e:
        logger.error("BILLING: portal error: %s", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "portal_unavailable", "message": "Plan management is not available. Contact support."},
        )


# ---------------------------------------------------------------------------
# Checkout status polling (legacy compat + activation)
# ---------------------------------------------------------------------------

@router.get("/checkout/status/{session_id}")
async def get_checkout_status(session_id: str, current_user: dict = Depends(get_current_user)):
    try:
        from emergentintegrations.payments.stripe.checkout import StripeCheckout
        sc = StripeCheckout(api_key=os.environ.get("STRIPE_API_KEY"), webhook_url="")
        cs = await sc.get_checkout_status(session_id)
        now = datetime.now(timezone.utc).isoformat()
        await db.payment_transactions.update_one(
            {"session_id": session_id},
            {"$set": {"payment_status": cs.payment_status, "status": cs.status, "updated_at": now}},
        )
        if cs.payment_status == "paid":
            await _activate_premium(session_id, now)
        return {"status": cs.status, "payment_status": cs.payment_status, "amount_total": cs.amount_total, "currency": cs.currency}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("BILLING: status error: %s", str(e))
        raise HTTPException(status_code=500, detail="Failed to check payment status")


# Legacy compat
@router.post("/checkout")
async def legacy_create_checkout(data: CheckoutSessionRequest, current_user: dict = Depends(get_current_user)):
    return await create_stripe_checkout(data, current_user)


# ---------------------------------------------------------------------------
# Webhook (hardened: signature verification + idempotency)
# ---------------------------------------------------------------------------

@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events. Signature-verified when billing enabled."""
    try:
        body = await request.body()
        sig_header = request.headers.get("Stripe-Signature", "")
        api_key = os.environ.get("STRIPE_API_KEY")

        if not api_key:
            return {"status": "ok"}

        # Use emergentintegrations webhook handler
        from emergentintegrations.payments.stripe.checkout import StripeCheckout
        sc = StripeCheckout(api_key=api_key, webhook_url="")

        try:
            webhook_response = await sc.handle_webhook(body, sig_header)
        except Exception:
            # Signature validation may fail with test keys
            return {"status": "ok"}

        if not webhook_response:
            return {"status": "ok"}

        event_id = webhook_response.event_id or str(uuid.uuid4())
        event_type = webhook_response.event_type or ""
        session_id = webhook_response.session_id
        payment_status = webhook_response.payment_status
        metadata = webhook_response.metadata or {}

        # Idempotency
        now = datetime.now(timezone.utc).isoformat()
        try:
            await db.processed_webhook_events.insert_one({
                "provider": "stripe", "event_id": event_id, "received_at": now,
            })
        except Exception:
            logger.info("WEBHOOK: duplicate event_id=%s", event_id)
            return {"status": "ok"}

        user_id = metadata.get("user_id")
        logger.info("WEBHOOK: type=%s id=%s user=%s", event_type, event_id, user_id)

        if payment_status == "paid" and session_id:
            await _activate_premium(session_id, now)

        return {"status": "ok"}
    except Exception as e:
        logger.error("WEBHOOK: error: %s", str(e))
        return {"status": "ok"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _activate_premium(session_id: str, now: str):
    """Idempotently activate premium for a paid session."""
    tx = await db.payment_transactions.find_one(
        {"session_id": session_id}, {"_id": 0, "user_id": 1, "activated": 1}
    )
    if not tx or tx.get("activated"):
        return

    user_id = tx["user_id"]
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"plan_tier": "premium", "subscription_level": 2, "updated_at": now}},
    )
    await db.subscriptions.update_one(
        {"user_id": user_id, "provider": "stripe"},
        {"$set": {"status": "active", "updated_at": now}},
        upsert=True,
    )
    await db.payment_transactions.update_one(
        {"session_id": session_id},
        {"$set": {"activated": True, "activated_at": now}},
    )
    logger.info("BILLING: Pro activated user=%s session=%s", user_id, session_id)


def map_stripe_status_to_plan_tier(stripe_status: str) -> str:
    if stripe_status in ("active", "trialing"):
        return "premium"
    return "free"
