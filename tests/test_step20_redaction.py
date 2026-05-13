"""
Tests for secret redaction utilities.
Verifies no raw secrets can leak via logs, config snapshots, or error messages.
"""
import os
import pytest
from utils.redaction import redact_uri, redact_secret, safe_config_snapshot, sanitize_exception, _SECRET_ENV_KEYS


class TestRedactUri:
    def test_mongodb_srv(self):
        uri = "mongodb+srv://myuser:s3cretP@ss@cluster.example.net/mydb?retryWrites=true"
        result = redact_uri(uri)
        assert "s3cretP" not in result
        assert "myuser" not in result
        assert result == "mongodb+srv://***:***@cluster.example.net/mydb?retryWrites=true"

    def test_mongodb_standard(self):
        uri = "mongodb://admin:password123@localhost:27017/testdb"
        result = redact_uri(uri)
        assert "password123" not in result
        assert "admin" not in result
        assert "***:***@localhost:27017" in result

    def test_https_uri(self):
        uri = "https://user:token@api.example.com/v1"
        result = redact_uri(uri)
        assert "token" not in result
        assert "***:***@api.example.com" in result

    def test_no_credentials(self):
        uri = "mongodb+srv://cluster.example.net/mydb"
        result = redact_uri(uri)
        assert result == uri  # No change needed

    def test_empty(self):
        assert redact_uri("") == ""
        assert redact_uri(None) == ""

    def test_special_chars_in_password(self):
        uri = "mongodb+srv://user:p%40ss%23word@host/db"
        result = redact_uri(uri)
        assert "p%40ss%23word" not in result
        assert "***:***@host" in result


class TestRedactSecret:
    def test_normal_key(self):
        result = redact_secret("sk_test_abc123xyz789")
        assert "sk_t" in result
        assert result.startswith("sk_t")
        assert "abc123" not in result

    def test_short_key(self):
        assert redact_secret("abc") == "***"
        assert redact_secret("") == "***"
        assert redact_secret(None) == "***"

    def test_long_key(self):
        # Placeholder shaped like a SendGrid key (NOT a real secret). Tests only
        # verify length-reduction + prefix-preservation, never the value itself.
        key = "SG." + "A" * 22 + "." + "B" * 43
        result = redact_secret(key)
        assert len(result) < len(key)
        assert "SG.S" in result


class TestSafeConfigSnapshot:
    def test_never_contains_raw_secrets(self):
        """The snapshot must never contain raw values of secret env vars."""
        # Set some test secrets
        original_vals = {}
        test_secrets = {
            "MONGO_URL": "mongodb+srv://user:supersecretpass@host/db",
            "JWT_SECRET_KEY": "my-super-secret-jwt-key-for-testing-purposes",
            "STRIPE_API_KEY": "stripe-placeholder-not-a-real-key-do-not-scan",
            "SENDGRID_API_KEY": "SG.test1234567890abcdefghij",
        }
        for k, v in test_secrets.items():
            original_vals[k] = os.environ.get(k)
            os.environ[k] = v

        try:
            snapshot = safe_config_snapshot()
            snapshot_str = str(snapshot)

            # Raw secrets must not appear anywhere in the serialized snapshot
            for key, raw_value in test_secrets.items():
                assert raw_value not in snapshot_str, f"Raw value of {key} leaked in snapshot"

            # But keys should be present with masked values
            for key in test_secrets:
                assert key in snapshot
                assert snapshot[key]["present"] is True
                assert "masked" in snapshot[key]
        finally:
            for k, v in original_vals.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_missing_keys_shown_as_absent(self):
        """Keys not in env should show present=False."""
        key = "STRIPE_WEBHOOK_SECRET"
        original = os.environ.pop(key, None)
        try:
            snapshot = safe_config_snapshot()
            assert snapshot[key]["present"] is False
        finally:
            if original is not None:
                os.environ[key] = original

    def test_all_secret_keys_covered(self):
        """Ensure _SECRET_ENV_KEYS covers the critical keys."""
        expected = {"MONGO_URL", "JWT_SECRET_KEY", "STRIPE_API_KEY", "SENDGRID_API_KEY", "EMERGENT_LLM_KEY"}
        assert expected.issubset(_SECRET_ENV_KEYS)


class TestSanitizeException:
    def test_exception_with_uri(self):
        exc = Exception("Connection failed: mongodb+srv://user:pass@host.net/db timed out")
        result = sanitize_exception(exc)
        assert "pass" not in result
        assert "***:***@host.net" in result

    def test_exception_without_uri(self):
        exc = Exception("timeout after 30s")
        result = sanitize_exception(exc)
        assert result == "timeout after 30s"
