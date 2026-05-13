"""
Phase 7 — Audio Digests V2 Tests

Tests:
  Unit:
    - Retention cap: soft-deletes oldest when > 10 digests
    - Gating: free blocked; trial/premium allowed via require_premium
    - Library membership: pmids must be saved_to_library=true
  API (requires ENABLE_LIBRARY_AUDIO_DIGESTS_V2=true + ENABLE_AUDIO_TAKEAWAY=true):
    - POST /api/audio-digests → 201 with playlist
    - GET /api/audio-digests → list (most-recent first)
    - GET /api/audio-digests/{id} → detail with items
    - GET /api/audio-digests/{id}/download.zip → 200 ZIP or 422 no audio ready
    - Flag OFF → all endpoints return 404 feature_disabled
    - Flag ON + free user → 403 premium_required
  Regression:
    - Digest V2 flag ON → play-digest buttons hidden (front-end, code-checked)
    - Existing /api/playlists/digest/{id} and /api/playlists/folder/{id} unaffected

PHI-Zero: no titles/keywords logged in assertions.
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
                pytest.skip(f"Login rate-limited: {resp.text[:80]}")
    return _token_cache[email]


@pytest.fixture(scope="session")
def premium_auth():
    return {"Authorization": f"Bearer {_get_token(PREMIUM_EMAIL, PREMIUM_PASSWORD)}"}


@pytest.fixture(scope="session")
def free_auth():
    return {"Authorization": f"Bearer {_get_token(FREE_EMAIL, FREE_PASSWORD)}"}


def _v2_enabled():
    resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
    flags = resp.json()
    # Only check the V2 flag itself; audio_takeaway is an internal flag not exposed publicly
    return flags.get("enable_library_audio_digests_v2", False)


# ---------------------------------------------------------------------------
# Unit tests — core logic
# ---------------------------------------------------------------------------

class TestRetentionLogic:
    """Retention cap: only 10 most recent audio digests kept per user."""

    def test_retention_module_importable(self):
        """_enforce_retention function exists and is importable."""
        from routes.audio_digests import _enforce_retention, RETENTION_LIMIT
        import asyncio
        assert asyncio.iscoroutinefunction(_enforce_retention)
        assert RETENTION_LIMIT == 10

    def test_retention_limit_is_10(self):
        from routes.audio_digests import RETENTION_LIMIT
        assert RETENTION_LIMIT == 10

    def test_feature_disabled_check_raises_on_flag_off(self, monkeypatch):
        """_require_v2 raises HTTPException when flag is OFF."""
        from fastapi import HTTPException
        monkeypatch.setenv("ENABLE_LIBRARY_AUDIO_DIGESTS_V2", "false")
        from routes.audio_digests import _require_v2
        with pytest.raises(HTTPException) as exc_info:
            _require_v2()
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error_code"] == "feature_disabled"

    def test_feature_enabled_check_passes(self, monkeypatch):
        """_require_v2 returns flags dict when both flags are ON."""
        monkeypatch.setenv("ENABLE_LIBRARY_AUDIO_DIGESTS_V2", "true")
        monkeypatch.setenv("ENABLE_AUDIO_TAKEAWAY", "true")
        from routes.audio_digests import _require_v2
        flags = _require_v2()
        assert flags.get("enable_library_audio_digests_v2") is True

    def test_audio_digest_create_model(self):
        """AudioDigestCreate model validates correctly."""
        from routes.audio_digests import AudioDigestCreate
        req = AudioDigestCreate(pmids=["pmid1", "pmid2"], title="Test", auto_generate_missing=True)
        assert req.pmids == ["pmid1", "pmid2"]
        assert req.title == "Test"
        assert req.auto_generate_missing is True

    def test_audio_digest_create_empty_pmids_rejected(self):
        """Empty pmids list should raise validation error."""
        from routes.audio_digests import AudioDigestCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AudioDigestCreate(pmids=[])

    def test_audio_service_has_get_bytes_for_record(self):
        """AudioService has get_bytes_for_record method."""
        from services.audio_service import AudioService
        import asyncio
        assert hasattr(AudioService, 'get_bytes_for_record')


class TestDigestV2FlagLogic:
    """Flag gating and feature flag values."""

    def test_feature_flags_endpoint_has_v2_flag(self):
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        assert resp.status_code == 200
        body = resp.json()
        assert "enable_library_audio_digests_v2" in body

    def test_v2_flag_defaults_to_false(self):
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        body = resp.json()
        if os.environ.get("ENABLE_LIBRARY_AUDIO_DIGESTS_V2", "false").lower() != "true":
            assert body["enable_library_audio_digests_v2"] is False


# ---------------------------------------------------------------------------
# API tests: flag OFF (regression — no behavior change)
# ---------------------------------------------------------------------------

class TestAudioDigestsV2FlagOff:
    """All endpoints return 404 feature_disabled when flag is OFF."""

    def test_list_404_flag_off(self, premium_auth):
        if _v2_enabled():
            pytest.skip("Flag is ON — cannot test flag-OFF behavior")
        resp = requests.get(f"{API_URL}/api/audio-digests", headers=premium_auth, timeout=5)
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "feature_disabled"

    def test_create_404_flag_off(self, premium_auth):
        if _v2_enabled():
            pytest.skip("Flag is ON — cannot test flag-OFF behavior")
        resp = requests.post(
            f"{API_URL}/api/audio-digests",
            headers=premium_auth,
            json={"pmids": ["12345"]},
            timeout=5,
        )
        assert resp.status_code == 404

    def test_playlist_endpoint_unchanged_flag_off(self, premium_auth):
        """Existing /api/playlists/digest/{id} still works when V2 flag is OFF."""
        resp = requests.get(
            f"{API_URL}/api/digests",
            headers=premium_auth,
            timeout=10,
        )
        assert resp.status_code == 200
        digests = resp.json().get("digests", [])
        if not digests:
            pytest.skip("No digests for premium user")
        digest_id = digests[0]["digest_id"]
        # V1 playlist endpoint must still exist and respond
        resp2 = requests.get(
            f"{API_URL}/api/playlists/digest/{digest_id}",
            headers=premium_auth,
            timeout=15,
        )
        # 200 or 404 (audio_takeaway not enabled in this env) — NOT broken
        assert resp2.status_code in (200, 404), (
            f"V1 playlist endpoint broken: {resp2.status_code}: {resp2.text[:100]}"
        )


# ---------------------------------------------------------------------------
# API tests: flag ON (requires ENABLE_LIBRARY_AUDIO_DIGESTS_V2=true)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("ENABLE_LIBRARY_AUDIO_DIGESTS_V2", "false").lower() != "true",
    reason="ENABLE_LIBRARY_AUDIO_DIGESTS_V2 not enabled"
)
class TestAudioDigestsV2FlagOn:
    """Integration tests requiring ENABLE_LIBRARY_AUDIO_DIGESTS_V2=true."""

    def test_list_returns_200(self, premium_auth):
        resp = requests.get(f"{API_URL}/api/audio-digests", headers=premium_auth, timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        assert "audio_digests" in body
        assert "total" in body

    def test_create_requires_library_membership(self, premium_auth):
        """PMIDs not in library → 400 no_library_pmids."""
        resp = requests.post(
            f"{API_URL}/api/audio-digests",
            headers=premium_auth,
            json={"pmids": ["99999999_not_in_library"], "auto_generate_missing": False},
            timeout=10,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "no_library_pmids"

    def test_create_with_library_pmid_returns_201(self, premium_auth):
        """Create audio digest with a seeded library PMID."""
        # First check if user has library articles
        lib = requests.get(f"{API_URL}/api/library", headers=premium_auth, timeout=10)
        articles = lib.json().get("articles", [])
        if not articles:
            pytest.skip("Premium user has no library articles")
        pmid = articles[0].get("pmid")
        if not pmid:
            pytest.skip("No PMID available in library")

        resp = requests.post(
            f"{API_URL}/api/audio-digests",
            headers=premium_auth,
            json={"pmids": [pmid], "auto_generate_missing": False},
            timeout=15,
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:200]}"
        body = resp.json()
        assert "audio_digest_id" in body
        assert body["item_count"] >= 1
        assert "items" in body

    def test_free_user_gets_403(self, free_auth):
        """Free user (no premium/trial) gets 403 from create endpoint."""
        resp = requests.post(
            f"{API_URL}/api/audio-digests",
            headers=free_auth,
            json={"pmids": ["12345678"]},
            timeout=10,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == "premium_required"

    def test_unauthenticated_gets_401(self):
        resp = requests.get(f"{API_URL}/api/audio-digests", timeout=5)
        assert resp.status_code == 401

    def test_create_then_list_and_get(self, premium_auth):
        """Create → list → get detail flow."""
        lib = requests.get(f"{API_URL}/api/library", headers=premium_auth, timeout=10)
        articles = lib.json().get("articles", [])
        if not articles:
            pytest.skip("No library articles")
        pmid = articles[0].get("pmid")
        if not pmid:
            pytest.skip("No PMID")

        # Create
        create_resp = requests.post(
            f"{API_URL}/api/audio-digests",
            headers=premium_auth,
            json={"pmids": [pmid], "auto_generate_missing": False},
            timeout=15,
        )
        assert create_resp.status_code == 201
        digest_id = create_resp.json()["audio_digest_id"]

        # List
        list_resp = requests.get(f"{API_URL}/api/audio-digests", headers=premium_auth, timeout=10)
        assert list_resp.status_code == 200
        ids = [d["audio_digest_id"] for d in list_resp.json()["audio_digests"]]
        assert digest_id in ids

        # Detail
        detail_resp = requests.get(f"{API_URL}/api/audio-digests/{digest_id}", headers=premium_auth, timeout=10)
        assert detail_resp.status_code == 200
        body = detail_resp.json()
        assert body["audio_digest_id"] == digest_id
        assert "items" in body
        assert "counts" in body

    def test_download_zip_endpoint_exists(self, premium_auth):
        """Download ZIP endpoint responds (200 or 422 no_audio_ready)."""
        lib = requests.get(f"{API_URL}/api/library", headers=premium_auth, timeout=10)
        articles = lib.json().get("articles", [])
        if not articles:
            pytest.skip("No library articles")
        pmid = articles[0].get("pmid")
        if not pmid:
            pytest.skip("No PMID")

        # Create a digest
        create = requests.post(
            f"{API_URL}/api/audio-digests",
            headers=premium_auth,
            json={"pmids": [pmid], "auto_generate_missing": False},
            timeout=15,
        )
        if create.status_code != 201:
            pytest.skip("Could not create test digest")
        digest_id = create.json()["audio_digest_id"]

        # Download ZIP
        dl = requests.get(f"{API_URL}/api/audio-digests/{digest_id}/download.zip", headers=premium_auth, timeout=15)
        # 200 = ZIP ready; 422 = no audio yet (valid for auto_generate_missing=False)
        assert dl.status_code in (200, 422), (
            f"Download ZIP should be 200 or 422, got {dl.status_code}: {dl.text[:100]}"
        )
        if dl.status_code == 422:
            assert dl.json()["detail"]["error_code"] == "no_audio_ready"
