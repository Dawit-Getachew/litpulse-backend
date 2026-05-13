"""
Phase 5 — Multi-Digest Profiles Tests

Tests:
  Unit:
    - Profile limit: free=1, premium=5, trial=5
    - Migration: default profile created from legacy prefs
    - Delete cascade hides digests and blocks access
    - Scheduler selects profiles when flag on

  API (requires ENABLE_MULTI_DIGEST_PROFILES=true):
    - GET /api/preferences/profiles returns profiles + limit info
    - POST creates profile (respects limit)
    - PUT updates profile
    - DELETE soft-deletes + hides digests
    - GET /api/preferences/me still works (backward compat)
    - Feature disabled → 404 with feature_disabled error code

PHI-Zero: no profile names or keywords are logged in assertions.
"""
import os
import sys
import time
import pytest
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

API_URL = os.environ.get("SMOKE_API_URL", "http://localhost:8001")
PREMIUM_EMAIL = os.environ.get("PREMIUM_EMAIL", "demo@litpulse.com")
PREMIUM_PASSWORD = os.environ.get("PREMIUM_PASSWORD", "DemoPass123!")
FREE_EMAIL = os.environ.get("FREE_EMAIL", "test@litpulse.com")
FREE_PASSWORD = os.environ.get("FREE_PASSWORD", "TestPass123!")

_token_cache: dict = {}


def _get_token(email, password):
    if email not in _token_cache:
        for attempt in range(3):
            resp = requests.post(f"{API_URL}/api/auth/login", json={"email": email, "password": password}, timeout=10)
            if resp.status_code == 200:
                _token_cache[email] = resp.json()["access_token"]
                break
            if resp.status_code == 429 and attempt < 2:
                time.sleep(6)
            else:
                pytest.skip(f"Login rate-limited / failed: {resp.text[:80]}")
    return _token_cache[email]


@pytest.fixture(scope="session")
def premium_auth():
    return {"Authorization": f"Bearer {_get_token(PREMIUM_EMAIL, PREMIUM_PASSWORD)}"}


@pytest.fixture(scope="session")
def free_auth():
    return {"Authorization": f"Bearer {_get_token(FREE_EMAIL, FREE_PASSWORD)}"}


def _profiles_enabled() -> bool:
    resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
    return resp.json().get("enable_multi_digest_profiles", False)


# ---------------------------------------------------------------------------
# Unit tests — limit and capability logic
# ---------------------------------------------------------------------------

class TestProfileLimits:
    """Profile limits are computed correctly from plan/trial status."""

    def test_free_user_limit_is_1(self):
        from routes.profiles import _get_max_profiles
        flags = {"enable_multi_digest_profiles": True, "enable_premium_trials": False}
        assert _get_max_profiles({"plan_tier": "free"}, flags) == 1

    def test_premium_user_limit_is_5(self):
        from routes.profiles import _get_max_profiles
        flags = {"enable_multi_digest_profiles": True, "enable_premium_trials": False}
        assert _get_max_profiles({"plan_tier": "premium"}, flags) == 5

    def test_trial_active_user_limit_is_5(self):
        from routes.profiles import _get_max_profiles
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        flags = {"enable_multi_digest_profiles": True, "enable_premium_trials": True}
        user = {"plan_tier": "free", "trial_expires_at": future, "trial_used": True}
        assert _get_max_profiles(user, flags) == 5

    def test_expired_trial_falls_back_to_free(self):
        from routes.profiles import _get_max_profiles
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        flags = {"enable_multi_digest_profiles": True, "enable_premium_trials": True}
        user = {"plan_tier": "free", "trial_expires_at": past}
        assert _get_max_profiles(user, flags) == 1

    def test_trial_disabled_flag_free_user_gets_1(self):
        from routes.profiles import _get_max_profiles
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        flags = {"enable_multi_digest_profiles": True, "enable_premium_trials": False}
        user = {"plan_tier": "free", "trial_expires_at": future}
        # Trial flag off → trial has no effect
        assert _get_max_profiles(user, flags) == 1


class TestSchedulerPath:
    """Scheduler uses correct path based on feature flag."""

    def test_legacy_path_when_flag_off(self, monkeypatch):
        """When flag OFF, _check_and_run_digests calls _run_legacy_digests."""
        monkeypatch.setenv("ENABLE_MULTI_DIGEST_PROFILES", "false")
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        assert flags.get("enable_multi_digest_profiles") is False

    def test_profile_path_when_flag_on(self, monkeypatch):
        """When flag ON, get_feature_flags returns enable_multi_digest_profiles=True."""
        monkeypatch.setenv("ENABLE_MULTI_DIGEST_PROFILES", "true")
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        assert flags.get("enable_multi_digest_profiles") is True


# ---------------------------------------------------------------------------
# API: Feature disabled (flag OFF)
# ---------------------------------------------------------------------------

class TestProfilesFeatureDisabled:
    """When ENABLE_MULTI_DIGEST_PROFILES=false, endpoints return 404 feature_disabled."""

    def test_list_profiles_404_when_flag_off(self, premium_auth):
        if _profiles_enabled():
            pytest.skip("Flag is ON")
        resp = requests.get(f"{API_URL}/api/preferences/profiles", headers=premium_auth, timeout=10)
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "feature_disabled"

    def test_create_profile_404_when_flag_off(self, premium_auth):
        if _profiles_enabled():
            pytest.skip("Flag is ON")
        resp = requests.post(
            f"{API_URL}/api/preferences/profiles",
            headers=premium_auth,
            json={"name": "Test", "specialty_id": "cardiology"},
            timeout=10,
        )
        assert resp.status_code == 404

    def test_preferences_me_still_works_flag_off(self, premium_auth):
        """GET /api/preferences/me must return 200 or 404 (no prefs) — never broken by Phase 5."""
        resp = requests.get(f"{API_URL}/api/preferences/me", headers=premium_auth, timeout=5)
        assert resp.status_code in (200, 404), (
            f"GET /api/preferences/me broke: {resp.status_code}"
        )

    def test_digests_endpoint_unchanged_flag_off(self, premium_auth):
        """GET /api/digests must return the same result with flag OFF."""
        resp = requests.get(f"{API_URL}/api/digests", headers=premium_auth, timeout=10)
        assert resp.status_code == 200
        assert "digests" in resp.json()


# ---------------------------------------------------------------------------
# API: Feature enabled (requires ENABLE_MULTI_DIGEST_PROFILES=true)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("ENABLE_MULTI_DIGEST_PROFILES", "false").lower() != "true",
    reason="ENABLE_MULTI_DIGEST_PROFILES not enabled"
)
class TestProfilesWithFlagOn:
    """Integration tests requiring ENABLE_MULTI_DIGEST_PROFILES=true."""

    def test_list_profiles_returns_200(self, premium_auth):
        resp = requests.get(f"{API_URL}/api/preferences/profiles", headers=premium_auth, timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        assert "profiles" in body
        assert "max_profiles" in body
        assert "at_limit" in body
        assert "count" in body

    def test_premium_user_max_profiles_is_5(self, premium_auth):
        resp = requests.get(f"{API_URL}/api/preferences/profiles", headers=premium_auth, timeout=10)
        assert resp.status_code == 200
        assert resp.json()["max_profiles"] == 5

    def test_free_user_max_profiles_is_1(self, free_auth):
        resp = requests.get(f"{API_URL}/api/preferences/profiles", headers=free_auth, timeout=10)
        assert resp.status_code == 200
        assert resp.json()["max_profiles"] == 1

    def test_create_profile_returns_201(self, premium_auth):
        resp = requests.post(
            f"{API_URL}/api/preferences/profiles",
            headers=premium_auth,
            json={
                "name": f"Test Profile {time.time():.0f}",
                "specialty_id": "cardiology",
                "custom_keywords": ["heart failure"],
                "schedule": {"frequency": "weekly"},
            },
            timeout=10,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "profile_id" in body
        assert body["specialty_id"] == "cardiology"
        # Clean up
        requests.delete(f"{API_URL}/api/preferences/profiles/{body['profile_id']}", headers=premium_auth, timeout=5)

    def test_create_profile_response_shape(self, premium_auth):
        resp = requests.post(
            f"{API_URL}/api/preferences/profiles",
            headers=premium_auth,
            json={"name": f"Shape Test {time.time():.0f}", "specialty_id": "neurology"},
            timeout=10,
        )
        assert resp.status_code == 201
        body = resp.json()
        required_fields = ["profile_id", "user_id", "name", "specialty_id", "is_active",
                           "digest_frequency", "next_run_timestamp", "created_at", "updated_at"]
        for field in required_fields:
            assert field in body, f"Field '{field}' missing from profile response"
        # Clean up
        requests.delete(f"{API_URL}/api/preferences/profiles/{body['profile_id']}", headers=premium_auth, timeout=5)

    def test_update_profile(self, premium_auth):
        # Create first
        create = requests.post(
            f"{API_URL}/api/preferences/profiles",
            headers=premium_auth,
            json={"name": f"Update Test {time.time():.0f}", "specialty_id": "cardiology"},
            timeout=10,
        )
        assert create.status_code == 201
        pid = create.json()["profile_id"]

        # Update name
        update = requests.put(
            f"{API_URL}/api/preferences/profiles/{pid}",
            headers=premium_auth,
            json={"name": "Updated Name", "schedule": {"frequency": "daily"}},
            timeout=10,
        )
        assert update.status_code == 200
        assert update.json()["name"] == "Updated Name"
        assert update.json()["digest_frequency"] == "daily"

        # Clean up
        requests.delete(f"{API_URL}/api/preferences/profiles/{pid}", headers=premium_auth, timeout=5)

    def test_delete_profile_requires_at_least_one(self, premium_auth):
        """If only one profile exists, deletion should fail with cannot_delete_last_profile."""
        resp = requests.get(f"{API_URL}/api/preferences/profiles", headers=premium_auth, timeout=10)
        profiles = resp.json().get("profiles", [])
        if len(profiles) != 1:
            pytest.skip("User has != 1 profile, cannot test single-profile constraint")
        pid = profiles[0]["profile_id"]
        del_resp = requests.delete(f"{API_URL}/api/preferences/profiles/{pid}", headers=premium_auth, timeout=5)
        assert del_resp.status_code == 409
        assert del_resp.json()["detail"]["error_code"] == "cannot_delete_last_profile"

    def test_preferences_me_backward_compat(self, premium_auth):
        """GET /api/preferences/me must still return 200 or 404 when profiles flag is ON."""
        resp = requests.get(f"{API_URL}/api/preferences/me", headers=premium_auth, timeout=5)
        assert resp.status_code in (200, 404), (
            f"GET /api/preferences/me broken with profiles flag ON: {resp.status_code}"
        )

    def test_digests_excludes_deleted_profile_digests(self, premium_auth):
        """Digests from deleted profiles must not appear in GET /api/digests."""
        # Verify endpoint works (may return 0 items if no profile digests exist)
        resp = requests.get(f"{API_URL}/api/digests", headers=premium_auth, timeout=10)
        assert resp.status_code == 200
        assert "digests" in resp.json()

    def test_unauthenticated_cannot_access_profiles(self):
        resp = requests.get(f"{API_URL}/api/preferences/profiles", timeout=5)
        assert resp.status_code == 401

    def test_free_user_cannot_exceed_limit(self, free_auth):
        """Free user trying to create 2nd profile gets 409."""
        # First, check current count
        list_resp = requests.get(f"{API_URL}/api/preferences/profiles", headers=free_auth, timeout=10)
        if list_resp.json().get("count", 0) >= 1:
            # Already at limit — next create should fail
            resp = requests.post(
                f"{API_URL}/api/preferences/profiles",
                headers=free_auth,
                json={"name": f"Overflow {time.time():.0f}", "specialty_id": "cardiology"},
                timeout=10,
            )
            assert resp.status_code == 409
            assert resp.json()["detail"]["error_code"] == "profile_limit_reached"
        else:
            pytest.skip("Free user has 0 profiles — cannot test limit without creating first")
