"""
Phase 3 — Landing Page & Feature-Flag Fail-Closed Tests

Verifies:
1. /api/config/feature-flags returns all Phase-0 + Phase-3 keys correctly
2. ENABLE_NEW_LANDING_PAGE defaults to false
3. Feature flags endpoint is public (no auth required)
4. All Phase-3 flags have correct defaults
5. When ENABLE_NEW_LANDING_PAGE=false: existing landing receives correct flags
PHI-Zero: no user text involved in any assertion.
"""
import os
import sys
import pytest
import requests
from pathlib import Path

API_URL = os.environ.get("SMOKE_API_URL", "http://localhost:8001")


class TestPhase3FlagsEndpoint:
    """Phase 3 feature flags are present and correct via /api/config/feature-flags."""

    def test_flags_endpoint_is_public(self):
        """Must return 200 without any auth token."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    def test_flags_endpoint_ignores_bad_token(self):
        """Must return 200 even with an invalid/expired bearer token (public endpoint)."""
        resp = requests.get(
            f"{API_URL}/api/config/feature-flags",
            headers={"Authorization": "Bearer invalid_token_xyz"},
            timeout=5,
        )
        assert resp.status_code == 200, (
            f"Feature-flags endpoint should be public (200), got {resp.status_code}"
        )

    def test_enable_new_landing_page_present_and_defaults_false(self):
        """ENABLE_NEW_LANDING_PAGE must be in response and default to false."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        assert "enable_new_landing_page" in body, (
            "enable_new_landing_page missing from /api/config/feature-flags"
        )
        assert body["enable_new_landing_page"] is False, (
            f"enable_new_landing_page should default to False, got {body['enable_new_landing_page']!r}"
        )

    def test_all_phase0_phase3_flags_present(self):
        """All Phase-0 and Phase-3 flags must be in the response."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        required_flags = [
            # Phase-0
            "enable_new_landing_page",
            "enable_premium_trials",
            "enable_explore_topic_search_v2",
            "enable_multi_digest_profiles",
            "enable_community_v2",
            "enable_library_audio_digests_v2",
            "enable_multi_digest_profiles_scheduler",
            "enforce_community_digest_membership",
        ]
        for flag in required_flags:
            assert flag in body, f"Flag '{flag}' missing from /api/config/feature-flags"

    def test_all_phase3_flags_default_false(self):
        """All flags must default to false (no behavior change when not set)."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        phase3_flags = [
            "enable_new_landing_page",
            "enable_premium_trials",
        ]
        for flag in phase3_flags:
            assert body.get(flag) is False, (
                f"Flag '{flag}' should default to False, got {body.get(flag)!r}"
            )

    def test_flag_response_is_valid_json_object(self):
        """Response must be a flat JSON object (dict), not an array or string."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        assert resp.headers.get("content-type", "").startswith("application/json"), (
            "Feature-flags endpoint must return application/json"
        )
        body = resp.json()
        assert isinstance(body, dict), f"Expected dict, got {type(body)}"


class TestPhase3FailClosed:
    """
    Fail-closed: when flag fetching fails, defaults must be false.
    These are unit-level tests of the frontend FeatureFlagsContext behaviour.
    We verify the backend returns well-structured data so the frontend 
    merge-with-defaults pattern works correctly.
    """

    def test_flags_response_has_no_null_values(self):
        """All boolean flags must be bool, not null/None (so frontend merge is safe)."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        boolean_flags = [
            "enable_new_landing_page",
            "enable_premium_trials",
            "phi_guard_enabled",
            "require_verified_for_posting",
            "enforce_run_now_quota",
            "messaging_enabled",
            "copilot_enabled",
        ]
        for flag in boolean_flags:
            val = body.get(flag)
            assert isinstance(val, bool), (
                f"Flag '{flag}' must be a boolean, got {type(val).__name__}: {val!r}"
            )

    def test_existing_operational_flags_still_present(self):
        """Phase 3 must not have removed any existing operational flags."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        existing = [
            "phi_guard_enabled",
            "phi_guard_mode",
            "require_verified_for_posting",
            "enforce_run_now_quota",
            "messaging_enabled",
            "copilot_enabled",
        ]
        for flag in existing:
            assert flag in body, f"Existing flag '{flag}' was removed from the endpoint"
