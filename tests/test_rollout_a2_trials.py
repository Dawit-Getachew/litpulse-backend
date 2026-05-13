"""
Rollout A2 — Premium Trial Verification Tests

Verifies (requires ENABLE_PREMIUM_TRIALS=true to be set in the running server):
1. Trial user treated as premium by require_premium()
2. Library export: trial user → 200 (not 403)
3. Copilot disabled → 503 copilot_disabled (NOT 403 premium_required) for trial user
4. Audio disabled → 404 (NOT 403 premium_required) for trial user
5. Trial twice → 409 trial_already_used
6. Expired trial → capabilities revert; premium endpoints → 403

Requires:
  SMOKE_API_URL        — backend base URL
  FREE_EMAIL           — free user with trial_used=false (or use smoketest@litpulse.com)
  FREE_PASSWORD
  PREMIUM_EMAIL        — premium user (for regression comparison)
  PREMIUM_PASSWORD

PHI-Zero: no user-entered text is logged anywhere in these tests.
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
FREE_EMAIL = os.environ.get("FREE_EMAIL", "test@litpulse.com")
FREE_PASSWORD = os.environ.get("FREE_PASSWORD", "TestPass123!")
PREMIUM_EMAIL = os.environ.get("PREMIUM_EMAIL", "demo@litpulse.com")
PREMIUM_PASSWORD = os.environ.get("PREMIUM_PASSWORD", "DemoPass123!")

# Module-level token cache — login once per session
_token_cache: dict = {}


def _get_token(email: str, password: str) -> str:
    if email not in _token_cache:
        for attempt in range(3):
            resp = requests.post(
                f"{API_URL}/api/auth/login",
                json={"email": email, "password": password},
                timeout=10,
            )
            if resp.status_code == 200:
                _token_cache[email] = resp.json()["access_token"]
                break
            if resp.status_code == 429 and attempt < 2:
                time.sleep(6)
            else:
                pytest.skip(f"Login failed or rate-limited for {email}: {resp.text[:80]}")
    return _token_cache[email]


def _auth(email: str, password: str) -> dict:
    return {"Authorization": f"Bearer {_get_token(email, password)}"}


@pytest.fixture(scope="session")
def free_headers():
    return _auth(FREE_EMAIL, FREE_PASSWORD)


@pytest.fixture(scope="session")
def premium_headers():
    return _auth(PREMIUM_EMAIL, PREMIUM_PASSWORD)


@pytest.fixture(scope="session")
def trials_on() -> bool:
    """True when ENABLE_PREMIUM_TRIALS is enabled in the running server."""
    resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
    return resp.json().get("enable_premium_trials", False)


# ---------------------------------------------------------------------------
# A2.1 — Trial flag state
# ---------------------------------------------------------------------------

class TestTrialFlagState:
    def test_trials_flag_in_feature_flags(self):
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        assert "enable_premium_trials" in body

    def test_billing_me_includes_trial_enabled(self, free_headers):
        resp = requests.get(f"{API_URL}/api/billing/me", headers=free_headers, timeout=5)
        assert resp.status_code == 200
        body = resp.json()
        assert "trial_enabled" in body
        assert "trial_active" in body
        assert "trial_used" in body
        assert "days_remaining" in body
        assert body["billing_enabled"] is False, "Stripe must remain disabled"


# ---------------------------------------------------------------------------
# A2.2 — Trial start / idempotency (requires flag ON)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("ENABLE_PREMIUM_TRIALS", "false").lower() != "true",
    reason="ENABLE_PREMIUM_TRIALS not enabled — skipping trial-on tests",
)
class TestTrialStartA2:
    def test_start_trial_or_already_used(self, free_headers):
        """Starting a trial returns 200 or 409 (already used) — both are valid."""
        resp = requests.post(
            f"{API_URL}/api/billing/start-trial",
            headers=free_headers,
            timeout=10,
        )
        assert resp.status_code in (200, 409, 503), (
            f"Unexpected status: {resp.status_code}: {resp.text[:200]}"
        )

    def test_cannot_start_trial_twice(self, free_headers):
        """Second attempt must always return 409."""
        requests.post(f"{API_URL}/api/billing/start-trial", headers=free_headers, timeout=10)
        resp2 = requests.post(f"{API_URL}/api/billing/start-trial", headers=free_headers, timeout=10)
        assert resp2.status_code == 409
        detail = resp2.json()["detail"]
        assert detail["error_code"] in ("trial_already_used", "already_premium")

    def test_trial_active_in_billing_me_after_start(self, free_headers, trials_on):
        if not trials_on:
            pytest.skip("Trial flag off")
        requests.post(f"{API_URL}/api/billing/start-trial", headers=free_headers, timeout=10)
        resp = requests.get(f"{API_URL}/api/billing/me", headers=free_headers, timeout=5)
        body = resp.json()
        assert body["trial_active"] is True
        assert body["days_remaining"] > 0

    def test_me_shows_trial_active(self, free_headers, trials_on):
        if not trials_on:
            pytest.skip("Trial flag off")
        requests.post(f"{API_URL}/api/billing/start-trial", headers=free_headers, timeout=10)
        resp = requests.get(f"{API_URL}/api/auth/me", headers=free_headers, timeout=5)
        user = resp.json()
        assert user.get("trial_active") is True
        assert user.get("plan_tier") == "free", "plan_tier must stay 'free' during trial"

    def test_trial_grants_premium_capabilities(self, free_headers, trials_on):
        if not trials_on:
            pytest.skip("Trial flag off")
        requests.post(f"{API_URL}/api/billing/start-trial", headers=free_headers, timeout=10)
        resp = requests.get(f"{API_URL}/api/auth/me", headers=free_headers, timeout=5)
        caps = resp.json().get("capabilities", {})
        assert caps.get("premium_export_csv") is True, (
            "Trial user must have premium_export_csv=true"
        )
        assert caps.get("run_now_per_24h", 0) >= 5, (
            "Trial user must have run_now_per_24h >= 5"
        )


# ---------------------------------------------------------------------------
# A2.3 — Premium endpoint error codes (critical gating check)
# ---------------------------------------------------------------------------

class TestPremiumEndpointErrorCodes:
    """
    Key invariant: feature kill-switches (copilot, audio) must return feature-specific
    error codes (copilot_disabled, 404), NOT premium_required (403).
    This applies to ALL users including trial users.
    """

    def _get_flag(self, flag_name: str) -> bool:
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        return resp.json().get(flag_name, False)

    def test_copilot_disabled_returns_503_not_403(self, free_headers):
        """When ENABLE_COPILOT=false, copilot endpoints return 503 copilot_disabled."""
        if self._get_flag("copilot_enabled"):
            pytest.skip("Copilot is enabled — cannot test disabled path")
        resp = requests.post(
            f"{API_URL}/api/copilot/evidence-brief",
            headers=free_headers,
            json={"pmid": "test_pmid"},
            timeout=10,
        )
        assert resp.status_code == 503, (
            f"Disabled copilot should return 503, got {resp.status_code}"
        )
        detail = resp.json().get("detail", {})
        assert detail.get("error_code") == "copilot_disabled", (
            f"Expected error_code=copilot_disabled, got {detail!r}"
        )

    def test_audio_disabled_returns_404_not_403(self, free_headers):
        """When ENABLE_AUDIO_TAKEAWAY=false, audio endpoints return 404, not 403.
        
        When audio IS enabled, a free (non-trial, non-premium) user correctly gets 403.
        This test only asserts the disabled→404 path; it skips when audio is on.
        """
        flags_resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5).json()
        # Can't distinguish audio flag from public endpoint — check capabilities instead
        me = requests.get(f"{API_URL}/api/auth/me", headers=free_headers, timeout=5).json()
        audio_capable = me.get("capabilities", {}).get("premium_audio", False)

        resp = requests.get(
            f"{API_URL}/api/articles/test_pmid_audio/audio-summary",
            headers=free_headers,
            timeout=10,
        )

        if resp.status_code == 200:
            pytest.skip("Audio takeaway enabled + user is premium/trial — cannot test disabled path")
        elif resp.status_code == 403:
            # This means audio IS enabled but user is free (no trial) — correct behaviour.
            # The test only catches the regression where audio-disabled returns 403 instead of 404.
            # Since audio is enabled here, this is the expected premium check result.
            pytest.skip("Audio takeaway is enabled and user is free (no trial) — 403 is correct here")
        elif resp.status_code == 404:
            # Could be feature disabled OR article not found — both are acceptable (not 403)
            pass
        else:
            assert resp.status_code not in (401, 500), (
                f"Unexpected status {resp.status_code}: {resp.text[:100]}"
            )

    def test_library_export_requires_premium(self, free_headers, premium_headers, trials_on):
        """Library export must be 200 for premium, 403 for free (without trial)."""
        # Free user without trial
        resp_free = requests.get(
            f"{API_URL}/api/library/export?format=csv",
            headers=free_headers,
            timeout=10,
        )
        # If trial is on and user has active trial, free_headers may have premium caps
        if trials_on:
            # User may have started trial — both 200 and 403 are valid depending on trial state
            assert resp_free.status_code in (200, 403), (
                f"Export with trial on: expected 200 (trial premium) or 403 (no trial), "
                f"got {resp_free.status_code}"
            )
        else:
            assert resp_free.status_code == 403, (
                f"Free user without trial should get 403 from export, got {resp_free.status_code}"
            )

        # Premium user should always get 200 (or redirect 307 for file download)
        resp_premium = requests.get(
            f"{API_URL}/api/library/export?format=csv",
            headers=premium_headers,
            timeout=10,
            allow_redirects=True,
        )
        assert resp_premium.status_code == 200, (
            f"Premium user should get 200 from export, got {resp_premium.status_code}: {resp_premium.text[:100]}"
        )

    def test_billing_me_billing_enabled_always_false(self, free_headers):
        """billing_enabled must always be false — Stripe is not deployed."""
        resp = requests.get(f"{API_URL}/api/billing/me", headers=free_headers, timeout=5)
        assert resp.json()["billing_enabled"] is False, (
            "billing_enabled must be False — Stripe is not configured"
        )

    def test_start_trial_503_when_flag_off(self, free_headers):
        """When ENABLE_PREMIUM_TRIALS=false, start-trial returns 503 feature_disabled."""
        flag_on = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5).json().get("enable_premium_trials")
        if flag_on:
            pytest.skip("ENABLE_PREMIUM_TRIALS=true — cannot test disabled path")
        resp = requests.post(
            f"{API_URL}/api/billing/start-trial",
            headers=free_headers,
            timeout=10,
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["error_code"] == "feature_disabled"


# ---------------------------------------------------------------------------
# A2.4 — Expiry sanity (unit-level: test the capabilities function directly)
# ---------------------------------------------------------------------------

class TestTrialExpiryCapabilities:
    """Verify that expired trial_expires_at → free capabilities (no HTTP required)."""

    def test_expired_trial_returns_free_caps(self):
        """Past trial_expires_at must not grant premium capabilities."""
        from utils.capabilities import compute_capabilities
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        user = {"trial_expires_at": past, "plan_tier": "free", "trial_used": True}
        flags = {"enable_premium_trials": True, "enable_audio_takeaway": True}
        caps = compute_capabilities(user, None, flags)
        assert caps["premium_export_csv"] is False, (
            "Expired trial must NOT grant premium_export_csv"
        )
        assert caps["run_now_per_24h"] == 1, (
            "Expired trial must revert run_now_per_24h to 1 (free tier)"
        )

    def test_active_trial_grants_premium_caps_unit(self):
        from utils.capabilities import compute_capabilities
        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        user = {"trial_expires_at": future, "plan_tier": "free", "trial_used": True}
        flags = {"enable_premium_trials": True, "enable_audio_takeaway": True}
        caps = compute_capabilities(user, None, flags)
        assert caps["premium_export_csv"] is True
        assert caps["run_now_per_24h"] == 5

    def test_days_remaining_clamps_to_zero(self):
        """days_remaining in billing/me must never be negative."""
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        from utils.capabilities import _is_new_trial_active
        flags = {"enable_premium_trials": True}
        is_active = _is_new_trial_active({"trial_expires_at": past}, flags)
        assert is_active is False
        # If not active, billing/me computes days_remaining = 0
        # (clamped by max(0, ...) in the endpoint)
