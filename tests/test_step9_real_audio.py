"""
Test Step 9: Real OpenAI TTS Audio + S3-ready Storage + Enhanced Response Fields
Tests:
- Audio summary endpoints return additional fields (audio_format, audio_content_type, error_code)
- MP3 audio file serves correctly
- Admin metrics includes audio section (pending_count, ready_count, etc)
- Auth/me capabilities for premium_audio
- Free user access control (403 premium_required)
- Token invalidation regression (verify-email reuse -> token_already_used)
- Billing/me regression (portal_available, portal_mode)
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

# Test article PMID (has real OpenAI TTS mp3 audio generated)
TEST_PMID = "12345678"
# MP3 audio file path (from storage)
MP3_FILENAME = "12345678_5987d79d_nova.mp3"


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


class TestAudioSummaryEnhancedFields:
    """Test GET /api/articles/{pmid}/audio-summary returns enhanced fields"""

    def test_premium_audio_summary_has_format_fields(self, premium_token):
        """Premium GET /api/articles/12345678/audio-summary returns audio_format=mp3, audio_content_type=audio/mpeg"""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        data = response.json()
        
        # Core status
        assert data.get("status") == "ready", f"Expected status=ready, got {data.get('status')}"
        
        # NEW: Verify audio_format field
        assert data.get("audio_format") == "mp3", f"Expected audio_format=mp3, got {data.get('audio_format')}"
        
        # NEW: Verify audio_content_type field
        assert data.get("audio_content_type") == "audio/mpeg", f"Expected audio_content_type=audio/mpeg, got {data.get('audio_content_type')}"
        
        # NEW: Verify error_code field exists (should be None for ready status)
        assert "error_code" in data, "Missing error_code field in response"
        assert data.get("error_code") is None, f"Expected error_code=None for ready status, got {data.get('error_code')}"
        
        # Existing fields
        assert data.get("audio_url") is not None, "Missing audio_url"
        assert data.get("transcript") is not None, "Missing transcript"
        assert data.get("duration_seconds") is not None, "Missing duration_seconds"
        
        print(f"✓ Audio summary enhanced fields: audio_format={data.get('audio_format')}, audio_content_type={data.get('audio_content_type')}, error_code={data.get('error_code')}")

    def test_free_user_audio_summary_forbidden(self, free_token):
        """Free user GET /api/articles/12345678/audio-summary -> 403 premium_required"""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        data = response.json()
        
        detail = data.get("detail", {})
        assert detail.get("error_code") == "premium_required", f"Expected error_code=premium_required, got {detail}"
        print(f"✓ Free user blocked with 403 premium_required")


class TestAudioFileServing:
    """Test audio file serving - both MP3 and WAV"""

    def test_serve_mp3_audio_file(self):
        """GET /api/audio/files/{filename} serves MP3 with audio/mpeg content-type"""
        response = requests.get(
            f"{BASE_URL}/api/audio/files/{MP3_FILENAME}",
            timeout=15
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        
        # Verify MP3 content-type
        content_type = response.headers.get("content-type", "")
        assert "audio/mpeg" in content_type, f"Expected audio/mpeg, got {content_type}"
        
        # Verify content has data (the file is ~687KB)
        assert len(response.content) > 10000, f"Audio file too small: {len(response.content)} bytes"
        
        # Verify MP3 header (ID3 or FFxxx)
        # MP3 files can start with ID3 tag or directly with sync bits (0xFF)
        first_bytes = response.content[:3]
        is_mp3 = first_bytes == b'ID3' or response.content[0] == 0xFF
        assert is_mp3, f"Invalid MP3 file header: {first_bytes.hex()}"
        
        print(f"✓ MP3 audio served: {MP3_FILENAME}, size={len(response.content)} bytes, content-type={content_type}")

    def test_serve_wav_audio_file(self):
        """GET /api/audio/files/{filename} serves WAV file correctly (regression)"""
        filename = "12345678_5987d79d.wav"
        response = requests.get(
            f"{BASE_URL}/api/audio/files/{filename}",
            timeout=10
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        
        content_type = response.headers.get("content-type", "")
        assert "audio/wav" in content_type, f"Expected audio/wav, got {content_type}"
        assert response.content[:4] == b'RIFF', "Invalid WAV file header"
        print(f"✓ WAV audio served (regression): {filename}")

    def test_serve_nonexistent_file_404(self):
        """GET /api/audio/files/nonexistent.mp3 -> 404"""
        response = requests.get(
            f"{BASE_URL}/api/audio/files/nonexistent_file.mp3",
            timeout=10
        )
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print(f"✓ Nonexistent audio file returns 404")


class TestAdminMetricsAudioSection:
    """Test GET /api/admin/metrics includes audio section"""

    def test_admin_metrics_has_audio_stats(self, premium_token):
        """Admin GET /api/admin/metrics returns audio section with counts"""
        response = requests.get(
            f"{BASE_URL}/api/admin/metrics",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        data = response.json()
        
        # Verify audio section exists
        assert "audio" in data, f"Missing 'audio' section in admin metrics: {list(data.keys())}"
        audio = data["audio"]
        
        # Verify expected fields
        expected_fields = ["pending_count", "ready_count", "failed_count", "generated_last_24h", "failures_last_24h"]
        for field in expected_fields:
            assert field in audio, f"Missing '{field}' in audio section"
            # Values should be integers
            assert isinstance(audio[field], int), f"Expected {field} to be int, got {type(audio[field])}"
        
        # At least one ready audio should exist (from test article)
        assert audio["ready_count"] >= 1, f"Expected at least 1 ready audio, got {audio['ready_count']}"
        
        print(f"✓ Admin metrics audio section: {audio}")


class TestAuthMeCapabilities:
    """Test GET /api/auth/me returns correct premium_audio capability"""

    def test_premium_user_premium_audio_true(self, premium_token):
        """Premium user capabilities.premium_audio = true"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Auth/me failed: {response.text}"
        data = response.json()
        
        caps = data.get("capabilities", {})
        assert caps.get("premium_audio") is True, f"Expected premium_audio=true, got {caps.get('premium_audio')}"
        print(f"✓ Premium user capabilities.premium_audio = {caps.get('premium_audio')}")

    def test_free_user_premium_audio_false(self, free_token):
        """Free user capabilities.premium_audio = false"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 200, f"Auth/me failed: {response.text}"
        data = response.json()
        
        caps = data.get("capabilities", {})
        assert caps.get("premium_audio") is False, f"Expected premium_audio=false, got {caps.get('premium_audio')}"
        print(f"✓ Free user capabilities.premium_audio = {caps.get('premium_audio')}")


class TestBillingMeRegression:
    """Regression: billing/me returns portal_available, portal_mode"""

    def test_billing_me_portal_fields(self, premium_token):
        """GET /api/billing/me returns portal_available and portal_mode"""
        response = requests.get(
            f"{BASE_URL}/api/billing/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        data = response.json()
        
        # Verify portal fields exist
        assert "portal_available" in data, f"Missing portal_available: {data.keys()}"
        assert "portal_mode" in data, f"Missing portal_mode: {data.keys()}"
        
        # Verify values (from Step 8 - portal disabled)
        assert data["portal_available"] is False, f"Expected portal_available=false, got {data['portal_available']}"
        assert data["portal_mode"] == "disabled", f"Expected portal_mode=disabled, got {data['portal_mode']}"
        
        print(f"✓ billing/me portal fields: portal_available={data['portal_available']}, portal_mode={data['portal_mode']}")


class TestTokenInvalidationRegression:
    """Regression: token invalidation still works (verify-email reuse -> token_already_used)"""

    def test_verify_email_token_reuse_blocked(self):
        """Verify-email token reuse returns 400 token_already_used"""
        import jwt
        from datetime import datetime, timezone, timedelta
        import time
        
        # Server uses insecure default key in dev mode
        jwt_secret = "dev-only-insecure-key-do-not-use-in-production-32chars"
        
        # Create a verification token for a unique test user_id
        test_user_id = f"test-step9-verify-{int(time.time() * 1000)}"
        expire = datetime.now(timezone.utc) + timedelta(hours=24)
        test_token = jwt.encode({
            "user_id": test_user_id,
            "type": "verification",
            "exp": expire
        }, jwt_secret, algorithm="HS256")
        
        # First attempt - should fail (user not found) but mark token as used
        response1 = requests.post(
            f"{BASE_URL}/api/auth/verify-email",
            json={"token": test_token}
        )
        first_status = response1.status_code
        # First call should return 404 (user not found)
        assert first_status == 404, f"Expected 404 on first use, got {first_status}"
        
        # Second attempt with same token - should get token_already_used
        response2 = requests.post(
            f"{BASE_URL}/api/auth/verify-email",
            json={"token": test_token}
        )
        
        # Should be 400 with token_already_used
        assert response2.status_code == 400, f"Expected 400, got {response2.status_code}"
        data = response2.json()
        detail = data.get("detail", {})
        
        if isinstance(detail, dict):
            assert detail.get("error_code") == "token_already_used", f"Expected error_code=token_already_used: {detail}"
        else:
            assert "already" in str(detail).lower() or "used" in str(detail).lower(), f"Expected token_already_used: {detail}"
        
        print(f"✓ Token reuse blocked: first={first_status}, second=400 token_already_used")


class TestGenerateAudioEndpoint:
    """Test POST /api/articles/{pmid}/audio-summary/generate"""

    def test_premium_generate_idempotent(self, premium_token):
        """Premium POST generate returns ready for existing audio (idempotent)"""
        response = requests.post(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Failed: {response.status_code} - {response.text}"
        data = response.json()
        
        assert data.get("status") == "ready", f"Expected status=ready, got {data.get('status')}"
        print(f"✓ Generate audio (idempotent): status={data.get('status')}")

    def test_free_user_generate_forbidden(self, free_token):
        """Free user POST generate -> 403 premium_required"""
        response = requests.post(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        print(f"✓ Free user blocked from generate: 403")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
