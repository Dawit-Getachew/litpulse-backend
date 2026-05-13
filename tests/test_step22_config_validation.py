"""
Tests for production config validation (Step 22).
Verifies fail-fast behavior and secret safety.
"""
import os
import pytest


def _set_env(overrides: dict, base: dict = None):
    """Set env vars for testing, returning originals for cleanup."""
    defaults = {
        "ENVIRONMENT": "production",
        "CORS_ORIGINS": "https://litpulse.com",
        "JWT_SECRET_KEY": "a-very-strong-production-key-that-is-at-least-32-chars",
        "APP_BASE_URL": "https://litpulse.com",
        "SENDGRID_API_KEY": "SG.realkey1234567890",
        "ENABLE_STRIPE_BILLING": "false",
        "ENABLE_AUDIO_TAKEAWAY": "false",
        "ENABLE_COPILOT": "false",
    }
    if base:
        defaults.update(base)
    defaults.update(overrides)
    originals = {}
    for k, v in defaults.items():
        originals[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return originals


def _restore_env(originals: dict):
    for k, v in originals.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestProductionValidationFailFast:
    def test_wildcard_cors_fails(self):
        """Production with CORS_ORIGINS='*' must fail."""
        orig = _set_env({"CORS_ORIGINS": "*"})
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="CORS_ORIGINS must be a specific allowlist"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_missing_jwt_fails(self):
        """Production with empty JWT_SECRET_KEY must fail."""
        orig = _set_env({"JWT_SECRET_KEY": ""})
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_dev_jwt_fails(self):
        """Production with a dev/insecure JWT key must fail."""
        orig = _set_env({"JWT_SECRET_KEY": "insecure-dev-key-for-development-testing-only-placeholder"})
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_short_jwt_fails(self):
        """Production with a short JWT key must fail."""
        orig = _set_env({"JWT_SECRET_KEY": "short"})
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_missing_app_base_url_fails(self):
        """Production without APP_BASE_URL must fail."""
        orig = _set_env({"APP_BASE_URL": ""})
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="APP_BASE_URL"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_missing_sendgrid_fails(self):
        """Production without SENDGRID_API_KEY must fail."""
        orig = _set_env({"SENDGRID_API_KEY": ""})
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="SENDGRID_API_KEY"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_stripe_requires_all_keys(self):
        """Production with billing enabled must have all Stripe keys."""
        orig = _set_env({
            "ENABLE_STRIPE_BILLING": "true",
            "STRIPE_API_KEY": "",
            "STRIPE_WEBHOOK_SECRET": "",
            "STRIPE_PRICE_ID_PRO_MONTHLY": "",
        })
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="STRIPE_API_KEY"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_audio_mock_fails_production(self):
        """Production with audio enabled and mock provider must fail."""
        orig = _set_env({
            "ENABLE_AUDIO_TAKEAWAY": "true",
            "AUDIO_TTS_PROVIDER": "mock",
        })
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError, match="AUDIO_TTS_PROVIDER must not be 'mock'"):
                validate_production_config()
        finally:
            _restore_env(orig)

    def test_valid_production_config_passes(self):
        """A complete production config must pass."""
        orig = _set_env({})  # All defaults are valid
        try:
            from utils.config_validation import validate_production_config
            validate_production_config()  # Should not raise
        finally:
            _restore_env(orig)


class TestErrorMessagesSecretSafe:
    def test_errors_never_contain_secret_values(self):
        """Error messages must describe WHICH key is wrong, not its value."""
        secret_value = "sk_live_SUPER_SECRET_12345678901234567890"
        orig = _set_env({
            "CORS_ORIGINS": "*",
            "JWT_SECRET_KEY": secret_value,
            "SENDGRID_API_KEY": "SG.secret_sendgrid_key_value",
        })
        try:
            from utils.config_validation import validate_production_config
            with pytest.raises(RuntimeError) as exc_info:
                validate_production_config()
            error_msg = str(exc_info.value)
            # Error should mention the key name but NOT the secret value
            assert secret_value not in error_msg
            assert "SG.secret_sendgrid_key_value" not in error_msg
        finally:
            _restore_env(orig)


class TestDevelopmentModeSkips:
    def test_development_skips_validation(self):
        """Development mode must skip validation (no fail-fast)."""
        orig = _set_env({"ENVIRONMENT": "development", "CORS_ORIGINS": "*", "JWT_SECRET_KEY": ""})
        try:
            from utils.config_validation import validate_production_config
            validate_production_config()  # Should not raise
        finally:
            _restore_env(orig)
