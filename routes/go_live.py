"""
Go-Live Readiness Routes for LitPulse Admin.
Returns config/integration status without exposing secrets.
Live checks are admin-only and explicitly triggered.
Step 16: Enhanced Stripe + S3 checks, write test support.
"""
from fastapi import APIRouter, HTTPException, Depends, status, Query
from datetime import datetime, timezone
import os
import logging

from auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-readiness"])

db = None
_admin_email = ""
_scheduler_ref = None


def set_db(database):
    global db
    db = database


def set_admin_email(email: str):
    global _admin_email
    _admin_email = email.lower()


def set_scheduler(scheduler):
    global _scheduler_ref
    _scheduler_ref = scheduler


async def _verify_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not _admin_email:
        raise HTTPException(status_code=403, detail="Admin not configured")
    user = await db.users.find_one({"user_id": current_user["user_id"]}, {"_id": 0, "email": 1})
    if user and user.get("email", "").lower() == _admin_email:
        return current_user
    raise HTTPException(status_code=403, detail="Admin access required")


def _bool_env(key: str) -> bool:
    return os.environ.get(key, "false").lower() == "true"


def _has_env(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())


def _mask(val: str, show: int = 4) -> str:
    if not val or len(val) < show + 4:
        return "***"
    return val[:show] + "..." + val[-4:]


def _detect_stripe_key_mode(key: str) -> str:
    """Detect Stripe key mode from prefix (no secret exposure)."""
    if not key:
        return "unknown"
    if key.startswith("sk_live_"):
        return "live"
    if key.startswith("sk_test_"):
        return "test"
    return "unknown"


def _is_placeholder_key(key: str) -> bool:
    """Check if key appears to be a placeholder."""
    if not key:
        return True
    placeholders = ["REPLACE_ME", "YOUR_KEY", "placeholder", "xxx", "emergent", "test_emergent"]
    key_lower = key.lower()
    for p in placeholders:
        if p.lower() in key_lower:
            return True
    # Very short keys are likely placeholders
    if len(key) < 20:
        return True
    return False


# ---------------------------------------------------------------------------
# GET /api/admin/go-live-status (config presence, no secrets)
# ---------------------------------------------------------------------------

@router.get("/go-live-status")
async def get_go_live_status(admin_user: dict = Depends(_verify_admin)):
    env = os.environ.get("ENVIRONMENT", "development")
    cors = os.environ.get("CORS_ORIGINS", "")
    jwt_key = os.environ.get("JWT_SECRET_KEY", "")
    jwt_ok = bool(jwt_key) and "insecure" not in jwt_key.lower() and "dev" not in jwt_key.lower()

    billing_enabled = _bool_env("ENABLE_STRIPE_BILLING")
    audio_enabled = _bool_env("ENABLE_AUDIO_TAKEAWAY")
    tts_provider = os.environ.get("AUDIO_TTS_PROVIDER", "mock")
    storage_backend = os.environ.get("AUDIO_STORAGE_BACKEND", "local")
    
    # Stripe config (Step 16 enhanced)
    stripe_key = os.environ.get("STRIPE_API_KEY", "")
    stripe_mode = _detect_stripe_key_mode(stripe_key)
    stripe_key_placeholder = _is_placeholder_key(stripe_key)
    
    # Copilot config (Step 14)
    copilot_enabled = _bool_env("ENABLE_COPILOT")
    copilot_provider = os.environ.get("COPILOT_PROVIDER", "mock")
    copilot_model = os.environ.get("COPILOT_MODEL", "")
    copilot_key_configured = _has_env("OPENAI_API_KEY") or _has_env("EMERGENT_LLM_KEY")

    # Scheduler status
    sched_info = {"running": False, "last_tick_at": None, "lock_present": False, "tick_seconds": None, "lock_owner_masked": None, "lock_expires_at": None}
    if _scheduler_ref:
        try:
            s = await _scheduler_ref.get_status()
            sched_info["running"] = s.get("running", False)
            sched_info["lock_present"] = s.get("has_lock", False)
            sched_info["tick_seconds"] = s.get("tick_seconds")
            lock_st = s.get("lock_status", {})
            sched_info["lock_expires_at"] = lock_st.get("expires_at")
            owner = lock_st.get("owner_id", "")
            sched_info["lock_owner_masked"] = _mask(owner, 6) if owner else None
        except Exception:
            pass

    # Audio stats with storage_backend breakdown (Step 16)
    audio_pending = await db.article_audio_summaries.count_documents({"status": "pending"})
    audio_failed = await db.article_audio_summaries.count_documents({"status": "failed"})
    audio_ready = await db.article_audio_summaries.count_documents({"status": "ready"})
    audio_local = await db.article_audio_summaries.count_documents({"status": "ready", "storage_backend": "local"})
    audio_s3 = await db.article_audio_summaries.count_documents({"status": "ready", "storage_backend": "s3"})
    # Legacy audio without storage_backend field
    audio_legacy = await db.article_audio_summaries.count_documents({"status": "ready", "storage_backend": {"$exists": False}})
    
    active_subs = await db.subscriptions.count_documents({"status": "active"})

    logger.info("ADMIN: go-live-status checked by user=%s", admin_user["user_id"])

    # Production fail-fast check for Stripe
    stripe_production_ok = True
    if env == "production" and billing_enabled:
        if stripe_key_placeholder:
            stripe_production_ok = False

    # Amber warning: env != production but URL looks like prod
    app_url = os.environ.get("APP_BASE_URL", "")
    env_mismatch_warning = None
    if env != "production" and app_url:
        from urllib.parse import urlparse
        try:
            host = (urlparse(app_url).hostname or "").lower()
            if any(kw in host for kw in (".com", ".io", ".ai", ".org", ".net")) and "localhost" not in host and "preview" not in host:
                env_mismatch_warning = f"ENVIRONMENT={env} but APP_BASE_URL looks like production ({host})"
        except Exception:
            pass

    return {
        "environment": {
            "name": env,
            "config_validation_ok": env != "production" or (jwt_ok and _has_env("APP_BASE_URL") and _has_env("SENDGRID_API_KEY") and cors != "*" and stripe_production_ok),
            "cors_is_wildcard": cors == "*",
            "app_base_url_set": _has_env("APP_BASE_URL"),
            "jwt_secret_set": jwt_ok,
            "env_mismatch_warning": env_mismatch_warning,
        },
        "integrations": {
            "sendgrid": {"configured": _has_env("SENDGRID_API_KEY")},
            "stripe": {
                "billing_enabled": billing_enabled,
                "secret_key_configured": _has_env("STRIPE_API_KEY"),
                "key_looks_placeholder": stripe_key_placeholder,
                "mode_detected": stripe_mode,
                "webhook_secret_configured": _has_env("STRIPE_WEBHOOK_SECRET"),
                "price_id_configured": _has_env("STRIPE_PRICE_ID_PRO_MONTHLY"),
                "production_ok": stripe_production_ok,
            },
            "audio": {
                "enabled_flag": audio_enabled,
                "tts_provider": tts_provider,
                "openai_key_configured": _has_env("OPENAI_API_KEY") or _has_env("EMERGENT_LLM_KEY"),
                "storage_backend": storage_backend,
                "s3_configured": _has_env("AUDIO_S3_BUCKET") if storage_backend == "s3" else None,
                "s3_bucket_set": _has_env("AUDIO_S3_BUCKET"),
                "s3_credentials_set": _has_env("AUDIO_S3_ACCESS_KEY_ID") and _has_env("AUDIO_S3_SECRET_ACCESS_KEY"),
            },
            "copilot": {
                "enabled_flag": copilot_enabled,
                "provider": copilot_provider,
                "model_configured": bool(copilot_model) or copilot_provider == "mock",
                "provider_key_configured": copilot_key_configured if copilot_provider != "mock" else True,
            },
        },
        "operations": {
            "scheduler": sched_info,
            "audio": {
                "pending_count": audio_pending,
                "failed_count": audio_failed,
                "ready_count": audio_ready,
                "storage_breakdown": {
                    "local": audio_local + audio_legacy,
                    "s3": audio_s3,
                    "legacy_no_backend_field": audio_legacy,
                },
            },
            "billing": {"active_subscriptions_count": active_subs},
        },
    }


# ---------------------------------------------------------------------------
# POST /api/admin/go-live-status/run-live-checks (explicit, admin-only)
# Step 16: Enhanced with Stripe price retrieval and optional S3 write test
# ---------------------------------------------------------------------------

@router.post("/go-live-status/run-live-checks")
async def run_live_checks(
    admin_user: dict = Depends(_verify_admin),
    include_write_tests: bool = Query(False, description="Include destructive write tests (S3 put/delete)"),
):
    """Run integration checks. Set include_write_tests=true for S3 write permission verification."""
    results = {}

    # 1. Stripe: retrieve configured price ID (Step 16 enhanced)
    try:
        import stripe as stripe_sdk
        api_key = os.environ.get("STRIPE_API_KEY")
        price_id = os.environ.get("STRIPE_PRICE_ID_PRO_MONTHLY")
        
        if not api_key:
            results["stripe"] = {"status": "skipped", "message": "No API key configured"}
        elif _is_placeholder_key(api_key):
            results["stripe"] = {"status": "failed", "error_code": "placeholder_key", "message": "API key appears to be a placeholder"}
        else:
            stripe_sdk.api_key = api_key
            mode = _detect_stripe_key_mode(api_key)
            
            # Test 1: Basic API call
            stripe_sdk.Price.list(limit=1)
            
            # Test 2: Retrieve configured price ID if set
            if price_id:
                try:
                    price = stripe_sdk.Price.retrieve(price_id)
                    results["stripe"] = {
                        "status": "ok",
                        "message": "API key valid, price ID retrievable",
                        "mode": mode,
                        "price_active": price.active,
                        "price_currency": price.currency,
                    }
                except stripe_sdk.error.InvalidRequestError:
                    results["stripe"] = {
                        "status": "failed",
                        "error_code": "price_not_found",
                        "message": f"Price ID not found: {_mask(price_id, 6)}",
                        "mode": mode,
                    }
            else:
                results["stripe"] = {
                    "status": "ok",
                    "message": "API key valid, no price ID configured",
                    "mode": mode,
                }
    except Exception as e:
        err_msg = str(e)[:100]
        # Redact any secret patterns that might appear in error messages
        from utils.redaction import redact_uri
        err_msg = redact_uri(err_msg)
        results["stripe"] = {"status": "failed", "error_code": type(e).__name__, "message": err_msg}

    # 2. S3: head_bucket + optional write test (Step 16 enhanced)
    storage_backend = os.environ.get("AUDIO_STORAGE_BACKEND", "local")
    if storage_backend == "s3" and _has_env("AUDIO_S3_BUCKET"):
        try:
            import boto3
            s3 = boto3.client(
                "s3",
                region_name=os.environ.get("AUDIO_S3_REGION", "us-east-1"),
                aws_access_key_id=os.environ.get("AUDIO_S3_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("AUDIO_S3_SECRET_ACCESS_KEY"),
                endpoint_url=os.environ.get("AUDIO_S3_ENDPOINT_URL") or None,
            )
            bucket = os.environ.get("AUDIO_S3_BUCKET")
            
            # Test 1: Read access (head_bucket)
            s3.head_bucket(Bucket=bucket)
            
            # Test 2: Presign generation
            s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "readiness-check"}, ExpiresIn=60)
            
            s3_result = {
                "status": "ok",
                "message": "Bucket reachable, presign works",
                "bucket": _mask(bucket, 6),
                "write_test": "skipped",
            }
            
            # Test 3: Optional write test
            if include_write_tests:
                sentinel_key = "_litpulse_sentinel.txt"
                try:
                    # Put sentinel object
                    s3.put_object(
                        Bucket=bucket,
                        Key=sentinel_key,
                        Body=b"LitPulse readiness check sentinel",
                        ContentType="text/plain",
                    )
                    # Delete sentinel object
                    s3.delete_object(Bucket=bucket, Key=sentinel_key)
                    s3_result["write_test"] = "ok"
                    s3_result["message"] = "Bucket reachable, presign works, write/delete ok"
                except Exception as write_err:
                    s3_result["write_test"] = "failed"
                    s3_result["write_error"] = str(write_err)[:100]
            
            results["s3"] = s3_result
        except Exception as e:
            results["s3"] = {"status": "failed", "error_code": type(e).__name__, "message": str(e)[:100]}
    else:
        results["s3"] = {"status": "skipped", "message": f"Storage backend is '{storage_backend}'"}

    # 3. OpenAI TTS: verify key exists (no audio generation)
    tts_provider = os.environ.get("AUDIO_TTS_PROVIDER", "mock")
    if tts_provider == "openai":
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("EMERGENT_LLM_KEY")
        if key:
            results["openai_tts"] = {"status": "ok", "message": "API key present", "model": os.environ.get("OPENAI_TTS_MODEL", "tts-1")}
        else:
            results["openai_tts"] = {"status": "failed", "error_code": "missing_key", "message": "No EMERGENT_LLM_KEY or OPENAI_API_KEY"}
    else:
        results["openai_tts"] = {"status": "skipped", "message": f"Provider is '{tts_provider}'"}

    # 4. SendGrid: verify key presence
    if _has_env("SENDGRID_API_KEY"):
        results["sendgrid"] = {"status": "ok", "message": "API key present"}
    else:
        results["sendgrid"] = {"status": "failed", "error_code": "missing_key", "message": "SENDGRID_API_KEY not set"}

    # 5. Scheduler health
    if _scheduler_ref:
        try:
            s = await _scheduler_ref.get_status()
            results["scheduler"] = {"status": "ok" if s.get("running") else "failed", "running": s.get("running", False), "has_lock": s.get("has_lock", False)}
        except Exception as e:
            results["scheduler"] = {"status": "failed", "message": str(e)[:100]}
    else:
        results["scheduler"] = {"status": "skipped", "message": "Scheduler not initialized"}

    # 6. Copilot: check provider configuration and optionally run minimal LLM call (Step 14)
    copilot_enabled = os.environ.get("ENABLE_COPILOT", "false").lower() == "true"
    copilot_provider = os.environ.get("COPILOT_PROVIDER", "mock")
    
    if not copilot_enabled:
        results["copilot"] = {"status": "skipped", "message": "ENABLE_COPILOT=false"}
    elif copilot_provider == "mock":
        results["copilot"] = {"status": "ok", "message": "Using mock provider (no external calls)", "provider": "mock"}
    else:
        # Real provider check
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("EMERGENT_LLM_KEY")
        if not key:
            results["copilot"] = {"status": "failed", "error_code": "missing_key", "message": "No EMERGENT_LLM_KEY or OPENAI_API_KEY configured"}
        else:
            # Minimal LLM call to verify key works (admin-triggered, may consume tokens)
            try:
                from utils.copilot_provider import create_copilot_provider
                provider = create_copilot_provider()
                # Simple, PHI-safe prompt that returns minimal JSON
                test_response = await provider.generate(
                    'Return exactly this JSON: {"ok": true}',
                    'You are a test assistant. Return valid JSON only.'
                )
                # Try to parse response
                import json
                try:
                    parsed = json.loads(test_response.strip().replace("```json", "").replace("```", "").strip())
                    if parsed.get("ok"):
                        results["copilot"] = {"status": "ok", "message": "Provider responded correctly", "provider": copilot_provider}
                    else:
                        results["copilot"] = {"status": "ok", "message": "Provider responded (format varied)", "provider": copilot_provider}
                except json.JSONDecodeError:
                    results["copilot"] = {"status": "ok", "message": "Provider responded (non-JSON)", "provider": copilot_provider}
            except Exception as e:
                results["copilot"] = {"status": "failed", "error_code": type(e).__name__, "message": str(e)[:100]}

    logger.info("ADMIN: live checks run by user=%s include_write=%s results=%s", admin_user["user_id"], include_write_tests, {k: v["status"] for k, v in results.items()})
    return {"checks": results, "timestamp": datetime.now(timezone.utc).isoformat(), "write_tests_included": include_write_tests}
