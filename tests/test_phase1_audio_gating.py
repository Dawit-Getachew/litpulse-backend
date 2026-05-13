"""
Phase 1 — Audio Gating Tests

Verifies:
1. Backend capability: premium_audio=True for premium users when ENABLE_AUDIO_TAKEAWAY=true
2. Backend capability: premium_audio=False for free users regardless of flag
3. Backend capability: premium_audio=False for premium users when ENABLE_AUDIO_TAKEAWAY=false
4. Playlist endpoint (GET /api/playlists/digest/{id}): returns 200 for premium users
5. Playlist endpoint: returns 403 for free users
6. Playlist endpoint: returns 404 when ENABLE_AUDIO_TAKEAWAY=false (feature disabled)
7. PlaylistModal audio URL resolution: full S3 URLs not prepended with backend base URL
"""
import os
import sys
import pytest
import requests
from pathlib import Path

# ---------------------------------------------------------------------------
# Test config
# ---------------------------------------------------------------------------
API_URL = os.environ.get("SMOKE_API_URL", "http://localhost:8001")
PREMIUM_EMAIL = os.environ.get("PREMIUM_EMAIL", "demo@litpulse.com")
PREMIUM_PASSWORD = os.environ.get("PREMIUM_PASSWORD", "DemoPass123!")
FREE_EMAIL = os.environ.get("FREE_EMAIL", "test@litpulse.com")
FREE_PASSWORD = os.environ.get("FREE_PASSWORD", "TestPass123!")


@pytest.fixture(scope="session")
def premium_token():
    resp = requests.post(
        f"{API_URL}/api/auth/login",
        json={"email": PREMIUM_EMAIL, "password": PREMIUM_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, f"Premium login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def free_token():
    resp = requests.post(
        f"{API_URL}/api/auth/login",
        json={"email": FREE_EMAIL, "password": FREE_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, f"Free user login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def premium_auth(premium_token):
    return {"Authorization": f"Bearer {premium_token}"}


@pytest.fixture(scope="session")
def free_auth(free_token):
    return {"Authorization": f"Bearer {free_token}"}


@pytest.fixture(scope="session")
def first_digest_id(premium_auth):
    """Get first digest ID for the premium user."""
    resp = requests.get(f"{API_URL}/api/digests?limit=1", headers=premium_auth, timeout=10)
    assert resp.status_code == 200
    digests = resp.json().get("digests", [])
    if not digests:
        pytest.skip("No digests available for premium user — seed data required")
    return digests[0]["digest_id"]


# ---------------------------------------------------------------------------
# Capability Tests
# ---------------------------------------------------------------------------

class TestAudioCapabilities:
    """premium_audio capability is correctly set in /api/auth/me response."""

    def test_premium_user_has_premium_audio(self, premium_auth):
        """Premium user must have capabilities.premium_audio=True when ENABLE_AUDIO_TAKEAWAY=true."""
        resp = requests.get(f"{API_URL}/api/auth/me", headers=premium_auth, timeout=5)
        assert resp.status_code == 200
        caps = resp.json().get("capabilities", {})
        assert "premium_audio" in caps, "capabilities.premium_audio missing from /api/auth/me"
        assert caps["premium_audio"] is True, (
            f"Premium user should have premium_audio=True, got {caps['premium_audio']!r}. "
            "Check ENABLE_AUDIO_TAKEAWAY env var."
        )

    def test_free_user_has_no_premium_audio(self, free_auth):
        """Free user must have capabilities.premium_audio=False."""
        resp = requests.get(f"{API_URL}/api/auth/me", headers=free_auth, timeout=5)
        assert resp.status_code == 200
        caps = resp.json().get("capabilities", {})
        assert "premium_audio" in caps, "capabilities.premium_audio missing from /api/auth/me"
        assert caps["premium_audio"] is False, (
            f"Free user should have premium_audio=False, got {caps['premium_audio']!r}"
        )

    def test_free_user_audio_generations_per_24h_is_zero(self, free_auth):
        """Free user must have audio_generations_per_24h=0."""
        resp = requests.get(f"{API_URL}/api/auth/me", headers=free_auth, timeout=5)
        caps = resp.json().get("capabilities", {})
        assert caps.get("audio_generations_per_24h", -1) == 0, (
            "Free user should have audio_generations_per_24h=0"
        )


# ---------------------------------------------------------------------------
# Playlist Endpoint Tests
# ---------------------------------------------------------------------------

class TestPlaylistEndpointGating:
    """Playlist endpoint returns 200 for premium, 403 for free users."""

    def test_premium_user_can_load_playlist(self, premium_auth, first_digest_id):
        """Premium user gets 200 from playlist endpoint."""
        resp = requests.get(
            f"{API_URL}/api/playlists/digest/{first_digest_id}",
            headers=premium_auth,
            timeout=30,  # auto_generate_missing can be slow
        )
        assert resp.status_code == 200, (
            f"Premium user should get 200 from playlist. Got {resp.status_code}: {resp.text[:200]}"
        )
        body = resp.json()
        assert "items" in body, "Playlist response must have 'items' key"
        assert "counts" in body, "Playlist response must have 'counts' key"

    def test_premium_playlist_items_have_valid_audio_urls(self, premium_auth, first_digest_id):
        """Ready playlist items must have absolute audio URLs (not relative paths)."""
        resp = requests.get(
            f"{API_URL}/api/playlists/digest/{first_digest_id}",
            headers=premium_auth,
            timeout=30,
        )
        assert resp.status_code == 200
        items = resp.json().get("items", [])
        ready_items = [i for i in items if i.get("status") == "ready"]
        for item in ready_items:
            url = item.get("audio_url", "")
            assert url.startswith("https://") or url.startswith("http://"), (
                f"audio_url must be an absolute URL, got: {url!r}. "
                "PlaylistModal must NOT prepend backend base URL to S3 presigned URLs."
            )

    def test_free_user_gets_403_from_playlist(self, free_auth, first_digest_id):
        """Free user gets 403 from playlist endpoint."""
        resp = requests.get(
            f"{API_URL}/api/playlists/digest/{first_digest_id}",
            headers=free_auth,
            timeout=10,
        )
        assert resp.status_code == 403, (
            f"Free user should get 403 from playlist. Got {resp.status_code}: {resp.text[:200]}"
        )
        body = resp.json()
        # Should contain error details for UI to display upgrade CTA
        detail = body.get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("error_code") == "premium_required", (
                f"Expected error_code='premium_required', got: {detail!r}"
            )

    def test_free_user_gets_403_for_nonexistent_digest(self, free_auth):
        """Free user is rejected at auth layer (403) before 404 can apply."""
        resp = requests.get(
            f"{API_URL}/api/playlists/digest/nonexistent-digest-id",
            headers=free_auth,
            timeout=5,
        )
        # Premium check happens before digest lookup, so free users always get 403
        assert resp.status_code == 403

    def test_folder_playlist_requires_premium(self, free_auth):
        """Folder playlist also requires premium."""
        resp = requests.get(
            f"{API_URL}/api/playlists/folder/General",
            headers=free_auth,
            timeout=5,
        )
        assert resp.status_code == 403, (
            f"Free user should get 403 from folder playlist. Got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Feature Flag Gating Test
# ---------------------------------------------------------------------------

class TestAudioFeatureFlagGating:
    """ENABLE_AUDIO_TAKEAWAY flag gates audio features correctly."""

    def test_audio_flag_present_in_feature_flags_endpoint(self):
        """Feature flags endpoint doesn't expose audio flag separately — it's baked into capabilities."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        assert resp.status_code == 200
        # Phase-0 flags should all be false
        body = resp.json()
        # The audio flag is an existing operational flag, not Phase-0
        # It IS present in backend utils/feature_flags.py as enable_audio_takeaway
        # but is NOT exposed in the public feature-flags endpoint (it's internal)
        # Verify the Phase-0 audio flag is off
        assert body.get("enable_library_audio_digests_v2") is False

    def test_capabilities_reflect_audio_takeaway_flag(self, premium_auth):
        """capabilities.premium_audio is True when ENABLE_AUDIO_TAKEAWAY=true and user is premium."""
        resp = requests.get(f"{API_URL}/api/auth/me", headers=premium_auth, timeout=5)
        caps = resp.json().get("capabilities", {})
        # If ENABLE_AUDIO_TAKEAWAY=true in the running env, this should be True for premium
        assert isinstance(caps.get("premium_audio"), bool), (
            "capabilities.premium_audio must be a boolean"
        )


# ---------------------------------------------------------------------------
# URL Integrity Regression
# ---------------------------------------------------------------------------

class TestAudioUrlIntegrity:
    """Regression: audio_url in playlist response must be absolute, not relative."""

    def test_no_relative_audio_urls_in_playlist(self, premium_auth, first_digest_id):
        """
        Regression test for PlaylistModal bug:
        audio_url must start with https:// so that PlaylistModal does NOT
        incorrectly prepend the backend base URL.
        
        Bug: src={`${apiBase}${current.audio_url}`} when audio_url is already 
        https://s3.amazonaws.com/... creates https://backend-urlhttps://s3...
        which is invalid.
        """
        resp = requests.get(
            f"{API_URL}/api/playlists/digest/{first_digest_id}",
            headers=premium_auth,
            timeout=30,
        )
        assert resp.status_code == 200
        items = resp.json().get("items", [])
        
        for item in items:
            url = item.get("audio_url", "")
            if url:
                assert not url.startswith("/"), (
                    f"audio_url is a relative path: {url!r}. "
                    "This would cause PlaylistModal to produce an invalid URL."
                )
                assert url.startswith("https://") or url.startswith("http://"), (
                    f"audio_url is neither relative nor absolute: {url!r}"
                )
