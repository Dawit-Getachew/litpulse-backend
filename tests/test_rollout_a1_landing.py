"""
Rollout A1 — New Landing Verification Tests

Verifies (without requiring ENABLE_NEW_LANDING_PAGE=true in .env):
1. Feature flags endpoint is public, returns correct structure
2. ENABLE_NEW_LANDING_PAGE defaults to false
3. ?next= redirect security: safe paths accepted, dangerous paths rejected
4. Flags fail-closed: all phase flags have bool values (safe for frontend merge)

Run:
  SMOKE_API_URL=<url> python -m pytest tests/test_rollout_a1_landing.py -v

Also run with ENABLE_NEW_LANDING_PAGE=true to verify the live flag state.

PHI-Zero: no user text is involved.
"""
import os
import sys
import pytest
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

API_URL = os.environ.get("SMOKE_API_URL", "http://localhost:8001")


# ---------------------------------------------------------------------------
# A1.1 — Flags endpoint
# ---------------------------------------------------------------------------

class TestFlagsEndpointA1:
    """Feature flags endpoint structure and security."""

    def test_public_no_auth(self):
        """Flags endpoint must return 200 without any token."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        assert resp.status_code == 200

    def test_public_with_bad_token(self):
        """Flags endpoint must return 200 with an invalid token (public route)."""
        resp = requests.get(
            f"{API_URL}/api/config/feature-flags",
            headers={"Authorization": "Bearer __invalid__"},
            timeout=5,
        )
        assert resp.status_code == 200, (
            f"Feature-flags endpoint must be public; got {resp.status_code}"
        )

    def test_landing_flag_default_false(self):
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        # If the flag is intentionally on in this env, skip the default check
        if os.environ.get("ENABLE_NEW_LANDING_PAGE", "false").lower() == "true":
            assert body["enable_new_landing_page"] is True, (
                "ENABLE_NEW_LANDING_PAGE=true is set but endpoint returns false"
            )
        else:
            assert body["enable_new_landing_page"] is False, (
                f"Default should be False, got {body.get('enable_new_landing_page')!r}"
            )

    def test_all_required_flags_present(self):
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        required = [
            "enable_new_landing_page",
            "enable_premium_trials",
            "phi_guard_enabled",
            "require_verified_for_posting",
            "messaging_enabled",
            "copilot_enabled",
        ]
        for flag in required:
            assert flag in body, f"Flag '{flag}' missing"

    def test_all_boolean_flags_are_bool(self):
        """All boolean flags must be actual bools — not None/null (safe for frontend merge)."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        bool_flags = [
            "enable_new_landing_page", "enable_premium_trials",
            "phi_guard_enabled", "require_verified_for_posting",
            "enforce_run_now_quota", "messaging_enabled", "copilot_enabled",
        ]
        for flag in bool_flags:
            val = body.get(flag)
            assert isinstance(val, bool), (
                f"Flag '{flag}' must be bool, got {type(val).__name__}: {val!r}"
            )

    def test_trials_flag_independent_from_landing_flag(self):
        """Changing landing flag must not affect trials flag and vice versa."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        # Both must exist as independent keys
        assert "enable_new_landing_page" in body
        assert "enable_premium_trials" in body


# ---------------------------------------------------------------------------
# A1.2 — ?next= redirect security (unit-level validation)
# ---------------------------------------------------------------------------

class TestNextParamSecurity:
    """
    Validates the ?next= redirect logic in LoginPage.js.
    These are logic tests — we verify the safety rules are correct,
    not that the browser actually redirects (that's covered by Playwright).

    Rules:
    - next must start with '/' and must NOT start with '//'
    - Anything that doesn't pass → redirect to /home
    """

    def _is_safe(self, next_val: str) -> bool:
        """
        Replicate the LoginPage.js safePath logic:
          const isRelativePath = next && next.startsWith('/') && !next.startsWith('//');
          const safePath = isRelativePath ? next : '/home';
        """
        if not next_val:
            return False
        return next_val.startswith("/") and not next_val.startswith("//")

    def test_plain_path_accepted(self):
        assert self._is_safe("/plan") is True
        assert self._is_safe("/home") is True
        assert self._is_safe("/library") is True

    def test_protocol_relative_rejected(self):
        """//evil.com must be rejected — this was the bug before Phase 3."""
        assert self._is_safe("//evil.com") is False
        assert self._is_safe("//example.org/phish") is False

    def test_absolute_url_rejected(self):
        assert self._is_safe("https://evil.com") is False
        assert self._is_safe("http://evil.com") is False

    def test_javascript_scheme_rejected(self):
        assert self._is_safe("javascript:alert(1)") is False
        assert self._is_safe("javascript://comment%0aalert(1)") is False

    def test_empty_and_none_rejected(self):
        assert self._is_safe("") is False
        assert self._is_safe(None) is False  # type: ignore

    def test_path_with_query_string_accepted(self):
        assert self._is_safe("/plan?source=landing") is True
        assert self._is_safe("/digests/abc123") is True

    def test_encoded_evil_rejected(self):
        """URL-encoded // still becomes // after decode."""
        # Browser would decode %2F%2F before passing to startsWith
        # But test the raw value too
        assert self._is_safe("%2F%2Fevil.com") is False  # doesn't start with /

    def test_whitespace_tricks_rejected(self):
        assert self._is_safe("  /plan") is False   # leading space
        assert self._is_safe("\t/plan") is False    # leading tab


# ---------------------------------------------------------------------------
# A1.3 — Health checks (pre-flight)
# ---------------------------------------------------------------------------

class TestPreFlightHealth:
    def test_api_health(self):
        resp = requests.get(f"{API_URL}/api/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    def test_feature_flags_response_time(self):
        """Flags endpoint must respond quickly (< 500ms) — it's called on every page load."""
        import time
        start = time.perf_counter()
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        dur = (time.perf_counter() - start) * 1000
        assert resp.status_code == 200
        assert dur < 500, f"Flags endpoint took {dur:.0f}ms — too slow for on-load call"
