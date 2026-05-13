"""
Test Step 5: Premium Audio Takeaway + Commute Mode Playlists
Tests:
- Audio summary endpoints for premium vs free users
- Audio file serving
- Auth/me capabilities for audio
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_EMAIL = "demo@litpulse.com"
PREMIUM_PASS = "DemoPass123!"
FREE_EMAIL = "test@litpulse.com"
FREE_PASS = "TestPass123!"

# Test article PMID (has audio already generated)
TEST_PMID = "12345678"


@pytest.fixture(scope="module")
def premium_token():
    """Login as premium user and get token"""
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": PREMIUM_EMAIL, "password": PREMIUM_PASS}
    )
    assert response.status_code == 200, f"Premium login failed: {response.text}"
    data = response.json()
    return data.get("access_token")


@pytest.fixture(scope="module")
def free_token():
    """Login as free user and get token"""
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": FREE_EMAIL, "password": FREE_PASS}
    )
    assert response.status_code == 200, f"Free login failed: {response.text}"
    data = response.json()
    return data.get("access_token")


class TestAuthMeCapabilities:
    """Test GET /api/auth/me returns correct audio capabilities"""

    def test_premium_user_has_audio_capabilities(self, premium_token):
        """Premium user should have premium_audio=true, audio_generations_per_24h=20"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Auth/me failed: {response.text}"
        data = response.json()
        
        # Verify capabilities exist
        assert "capabilities" in data, "Missing capabilities in response"
        caps = data["capabilities"]
        
        # Verify audio capabilities for premium
        assert caps.get("premium_audio") is True, f"Expected premium_audio=true, got {caps.get('premium_audio')}"
        assert caps.get("audio_generations_per_24h") == 20, f"Expected audio_generations_per_24h=20, got {caps.get('audio_generations_per_24h')}"
        print(f"✓ Premium user capabilities: premium_audio={caps.get('premium_audio')}, audio_generations_per_24h={caps.get('audio_generations_per_24h')}")

    def test_free_user_no_audio_capabilities(self, free_token):
        """Free user should have premium_audio=false, audio_generations_per_24h=0"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 200, f"Auth/me failed: {response.text}"
        data = response.json()
        
        # Verify capabilities exist
        assert "capabilities" in data, "Missing capabilities in response"
        caps = data["capabilities"]
        
        # Verify audio capabilities for free
        assert caps.get("premium_audio") is False, f"Expected premium_audio=false, got {caps.get('premium_audio')}"
        assert caps.get("audio_generations_per_24h") == 0, f"Expected audio_generations_per_24h=0, got {caps.get('audio_generations_per_24h')}"
        print(f"✓ Free user capabilities: premium_audio={caps.get('premium_audio')}, audio_generations_per_24h={caps.get('audio_generations_per_24h')}")


class TestAudioSummaryEndpoints:
    """Test article audio summary endpoints"""

    def test_premium_get_audio_summary_ready(self, premium_token):
        """Premium user GET /api/articles/12345678/audio-summary -> status=ready with audio_url and transcript"""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        data = response.json()
        
        assert data.get("status") == "ready", f"Expected status=ready, got {data.get('status')}"
        assert data.get("audio_url") is not None, "Missing audio_url in response"
        assert data.get("transcript") is not None, "Missing transcript in response"
        assert "/api/audio/files/" in data.get("audio_url", ""), f"Invalid audio_url format: {data.get('audio_url')}"
        print(f"✓ Premium audio summary: status={data.get('status')}, audio_url={data.get('audio_url')}")

    def test_premium_generate_audio_idempotent(self, premium_token):
        """Premium user POST /api/articles/12345678/audio-summary/generate -> idempotent, returns ready"""
        response = requests.post(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        data = response.json()
        
        # Should return ready since audio already exists
        assert data.get("status") == "ready", f"Expected status=ready, got {data.get('status')}"
        print(f"✓ Generate audio (idempotent): status={data.get('status')}")

    def test_free_get_audio_summary_forbidden(self, free_token):
        """Free user GET /api/articles/12345678/audio-summary -> 403 premium_required"""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        data = response.json()
        
        detail = data.get("detail", {})
        assert detail.get("error_code") == "premium_required", f"Expected error_code=premium_required, got {detail}"
        print(f"✓ Free user blocked from GET audio: {detail}")

    def test_free_generate_audio_forbidden(self, free_token):
        """Free user POST /api/articles/12345678/audio-summary/generate -> 403 premium_required"""
        response = requests.post(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        data = response.json()
        
        detail = data.get("detail", {})
        assert detail.get("error_code") == "premium_required", f"Expected error_code=premium_required, got {detail}"
        print(f"✓ Free user blocked from generate audio: {detail}")


class TestAudioFileServing:
    """Test audio file serving endpoint"""

    def test_serve_audio_file(self):
        """GET /api/audio/files/{filename} -> 200 serves WAV file"""
        # Using the known audio file from the test article
        filename = "12345678_5987d79d.wav"
        response = requests.get(
            f"{BASE_URL}/api/audio/files/{filename}",
            timeout=10
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        
        # Verify it's a WAV file
        content_type = response.headers.get("content-type", "")
        assert "audio/wav" in content_type, f"Expected audio/wav, got {content_type}"
        
        # Verify content has data
        assert len(response.content) > 0, "Audio file is empty"
        
        # Verify WAV header (RIFF)
        assert response.content[:4] == b'RIFF', "Invalid WAV file header"
        print(f"✓ Audio file served: {filename}, size={len(response.content)} bytes, content-type={content_type}")

    def test_serve_nonexistent_file_404(self):
        """GET /api/audio/files/nonexistent.wav -> 404"""
        response = requests.get(
            f"{BASE_URL}/api/audio/files/nonexistent_file.wav",
            timeout=10
        )
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print(f"✓ Nonexistent audio file returns 404")


class TestPlaylistEndpoints:
    """Test playlist endpoints for premium users"""

    def test_free_user_playlist_forbidden(self, free_token):
        """Free user cannot access digest playlists"""
        response = requests.get(
            f"{BASE_URL}/api/playlists/digest/test-digest-id",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        # Should be 403 (premium required) or 404 (digest not found)
        assert response.status_code in [403, 404], f"Expected 403 or 404, got {response.status_code}"
        if response.status_code == 403:
            data = response.json()
            detail = data.get("detail", {})
            assert detail.get("error_code") == "premium_required"
        print(f"✓ Free user blocked from playlist: {response.status_code}")


class TestLibraryExportRegression:
    """Regression test - verify library export still works"""

    def test_premium_csv_export(self, premium_token):
        """Premium user can still export CSV"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=csv",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"CSV export failed: {response.status_code}"
        assert "text/csv" in response.headers.get("content-type", "")
        print(f"✓ Library CSV export still works")

    def test_free_export_blocked(self, free_token):
        """Free user still blocked from export"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=csv",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        print(f"✓ Free user still blocked from export")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
