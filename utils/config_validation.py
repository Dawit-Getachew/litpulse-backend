"""
Production config validation — fail fast if misconfigured.
Called during app startup. In production mode, missing/invalid config is fatal.
PHI-Zero: never logs raw secret values in error messages.
"""
import os
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _bool_env(key: str) -> bool:
    return os.environ.get(key, "false").lower() == "true"


def _has_env(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())


def validate_production_config() -> None:
    """Validate config and log startup environment summary.
    
    Always logs a safe environment summary (no secrets).
    JWT secret validation is now handled by auth_utils at module load.
    In production mode, fails fast on all missing/invalid required config.
    """
    env = os.environ.get("ENVIRONMENT", "development").lower()
    cors = os.environ.get("CORS_ORIGINS", "")
    app_base_url = os.environ.get("APP_BASE_URL", "")

    # --- Always log safe startup summary ---
    cors_display = "wildcard (*)" if cors == "*" else f"allowlist ({len(cors.split(','))} origin(s))" if cors else "(not set)"
    base_host = ""
    if app_base_url:
        try:
            base_host = urlparse(app_base_url).hostname or app_base_url
        except Exception:
            base_host = "(parse error)"

    logger.info(
        "[STARTUP] env=%s | cors=%s | app_base_url=%s | config_validation=%s",
        env,
        cors_display,
        base_host or "(not set)",
        "enforced" if env == "production" else "beta-hardened",
    )

    # --- ALWAYS warn on wildcard CORS (enforcement moved to security.py) ---
    if cors == "*":
        logger.warning(
            "[STARTUP] WARNING: CORS_ORIGINS=* allows any website to call your API. "
            "Set to your frontend domain for beta safety."
        )

    # --- Amber warning: looks like prod but not marked as production ---
    if env != "production" and app_base_url:
        host_lower = base_host.lower()
        if any(kw in host_lower for kw in (".com", ".io", ".ai", ".org", ".net")) and "localhost" not in host_lower and "preview" not in host_lower:
            logger.warning(
                "[STARTUP] WARNING: ENVIRONMENT=%s but APP_BASE_URL (%s) looks like a production domain. "
                "Set ENVIRONMENT=production to enable full config validation.",
                env, base_host,
            )

    if env != "production":
        # Even outside production, validate S3 config if explicitly enabled
        storage = os.environ.get("AUDIO_STORAGE_BACKEND", "local")
        if storage == "s3":
            missing_s3 = [k for k in ("AUDIO_S3_BUCKET", "AUDIO_S3_ACCESS_KEY_ID", "AUDIO_S3_SECRET_ACCESS_KEY") if not _has_env(k)]
            if missing_s3:
                raise RuntimeError(
                    f"FATAL: AUDIO_STORAGE_BACKEND=s3 but missing required credentials: {', '.join(missing_s3)}. "
                    "Either set the S3 credentials or change AUDIO_STORAGE_BACKEND=local."
                )
        return

    # --- Production mode: strict validation ---
    errors = []

    # CORS validation (enforcement now in security.py, but log here for clarity)
    if cors == "*" or not cors:
        errors.append("CORS_ORIGINS must be a specific allowlist (not '*' or empty)")

    if not app_base_url:
        errors.append("APP_BASE_URL must be set")

    if not _has_env("SENDGRID_API_KEY"):
        errors.append("SENDGRID_API_KEY must be set")

    # Stripe billing (if enabled)
    if _bool_env("ENABLE_STRIPE_BILLING"):
        if not _has_env("STRIPE_API_KEY"):
            errors.append("STRIPE_API_KEY required when ENABLE_STRIPE_BILLING=true")
        if not _has_env("STRIPE_WEBHOOK_SECRET"):
            errors.append("STRIPE_WEBHOOK_SECRET required when ENABLE_STRIPE_BILLING=true")
        if not _has_env("STRIPE_PRICE_ID_PRO_MONTHLY"):
            errors.append("STRIPE_PRICE_ID_PRO_MONTHLY required when ENABLE_STRIPE_BILLING=true")

    # Audio (if enabled)
    if _bool_env("ENABLE_AUDIO_TAKEAWAY"):
        provider = os.environ.get("AUDIO_TTS_PROVIDER", "mock")
        if provider == "mock":
            errors.append("AUDIO_TTS_PROVIDER must not be 'mock' in production")
        if provider == "openai":
            if not (_has_env("EMERGENT_LLM_KEY") or _has_env("OPENAI_API_KEY")):
                errors.append("EMERGENT_LLM_KEY or OPENAI_API_KEY required when AUDIO_TTS_PROVIDER=openai")
        storage = os.environ.get("AUDIO_STORAGE_BACKEND", "local")
        if storage == "s3":
            for key in ("AUDIO_S3_BUCKET", "AUDIO_S3_ACCESS_KEY_ID", "AUDIO_S3_SECRET_ACCESS_KEY"):
                if not _has_env(key):
                    errors.append(f"{key} required when AUDIO_STORAGE_BACKEND=s3")

    # Copilot (if enabled and not mock)
    if _bool_env("ENABLE_COPILOT"):
        copilot_provider = os.environ.get("COPILOT_PROVIDER", "mock")
        if copilot_provider != "mock":
            if not (_has_env("EMERGENT_LLM_KEY") or _has_env("OPENAI_API_KEY")):
                errors.append(f"Provider key required when COPILOT_PROVIDER={copilot_provider}")

    if errors:
        for e in errors:
            logger.critical(f"CONFIG ERROR: {e}")
        raise RuntimeError(
            f"Production config validation failed ({len(errors)} error(s)): " + "; ".join(errors)
        )

    logger.info("[STARTUP] Production config validation passed (%d checks OK)", 4 + len([
        k for k in ("ENABLE_STRIPE_BILLING", "ENABLE_AUDIO_TAKEAWAY", "ENABLE_COPILOT")
        if _bool_env(k)
    ]))
