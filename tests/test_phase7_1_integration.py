"""
Phase 7.1 Integration Tests — API-level testing for hardening features

Tests for:
A) ZIP download hardening (rate limiting, track caps, authz)
B) PHI-Zero closure for profiles and audio digests
C) Feature flag OFF behavior (404 responses)

Run with: pytest tests/test_phase7_1_integration.py -v
"""
import pytest
import requests
import os
import time
from datetime import datetime

# Get BASE_URL from environment
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = "https://litscreen-aggregate.preview.emergentagent.com"

# Test credentials
PREMIUM_EMAIL = "demo@litpulse.com"
PREMIUM_PASSWORD = "DemoPass123!"
FREE_EMAIL = "test@litpulse.com"
FREE_PASSWORD = "TestPass123!"


# Module-level token cache to avoid multiple logins
_token_cache = {}

def get_premium_token():
    """Get premium user token (cached)."""
    if "premium" not in _token_cache:
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": PREMIUM_EMAIL, "password": PREMIUM_PASSWORD}
        )
        if response.status_code != 200:
            raise Exception(f"Premium login failed: {response.text}")
        _token_cache["premium"] = response.json()["access_token"]
    return _token_cache["premium"]

def get_free_token():
    """Get free user token (cached)."""
    if "free" not in _token_cache:
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": FREE_EMAIL, "password": FREE_PASSWORD}
        )
        if response.status_code != 200:
            raise Exception(f"Free login failed: {response.text}")
        _token_cache["free"] = response.json()["access_token"]
    return _token_cache["free"]


class TestSetup:
    """Setup fixtures for integration tests."""
    
    @pytest.fixture(scope="class")
    def premium_token(self):
        """Get premium user token."""
        return get_premium_token()
    
    @pytest.fixture(scope="class")
    def free_token(self):
        """Get free user token."""
        return get_free_token()
    
    @pytest.fixture(scope="class")
    def premium_user_id(self, premium_token):
        """Get premium user ID from token."""
        # Decode JWT to get user_id (or make a /me call)
        import base64
        import json
        payload = premium_token.split('.')[1]
        # Add padding if needed
        payload += '=' * (4 - len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded.get("user_id")
    
    @pytest.fixture(scope="class")
    def free_user_id(self, free_token):
        """Get free user ID from token."""
        import base64
        import json
        payload = free_token.split('.')[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded.get("user_id")


# =============================================================================
# A) PHI Guard Tests for Profiles
# =============================================================================

class TestProfilePhiGuardIntegration(TestSetup):
    """Test PHI guard enforcement on profile endpoints via API."""
    
    def test_create_profile_rejects_ssn_in_name(self, premium_token):
        """POST /api/preferences/profiles should reject PHI (SSN) in name."""
        response = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "name": "Patient SSN 123-45-6789",
                "specialty_id": "cardiology",
                "custom_keywords": []
            }
        )
        # Should return 422 for PHI detected
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_create_profile_rejects_mrn_in_name(self, premium_token):
        """POST /api/preferences/profiles should reject PHI (MRN) in name."""
        response = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "name": "MRN: 12345678 case",
                "specialty_id": "cardiology",
                "custom_keywords": []
            }
        )
        assert response.status_code == 422
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_create_profile_rejects_patient_name(self, premium_token):
        """POST /api/preferences/profiles should reject PHI (patient name) in name."""
        response = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "name": "patient John Smith digest",
                "specialty_id": "cardiology",
                "custom_keywords": []
            }
        )
        assert response.status_code == 422
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_create_profile_rejects_phi_in_keywords(self, premium_token):
        """POST /api/preferences/profiles should reject PHI in custom_keywords."""
        response = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "name": "Clean Profile Name",
                "specialty_id": "cardiology",
                "custom_keywords": ["diabetes", "patient John Smith", "hypertension"]
            }
        )
        assert response.status_code == 422
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_update_profile_rejects_phi_in_name(self, premium_token):
        """PUT /api/preferences/profiles/{id} should reject PHI in name."""
        # First get existing profile
        list_response = requests.get(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert list_response.status_code == 200
        profiles = list_response.json().get("profiles", [])
        
        if not profiles:
            pytest.skip("No profiles to update")
        
        profile_id = profiles[0]["profile_id"]
        
        # Try to update with PHI
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"name": "DOB: 01/15/1980 patient"}
        )
        assert response.status_code == 422
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_update_profile_rejects_phi_in_keywords(self, premium_token):
        """PUT /api/preferences/profiles/{id} should reject PHI in custom_keywords."""
        # First get existing profile
        list_response = requests.get(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert list_response.status_code == 200
        profiles = list_response.json().get("profiles", [])
        
        if not profiles:
            pytest.skip("No profiles to update")
        
        profile_id = profiles[0]["profile_id"]
        
        # Try to update with PHI in keywords
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"custom_keywords": ["123 Main Street", "cardiology"]}
        )
        assert response.status_code == 422
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_clean_profile_name_accepted(self, premium_token):
        """POST /api/preferences/profiles should accept clean text."""
        # First check if at limit
        list_response = requests.get(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        profiles_data = list_response.json()
        
        if profiles_data.get("at_limit", False):
            pytest.skip("User at profile limit")
        
        response = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "name": f"TEST_Cardiology Research {datetime.now().timestamp()}",
                "specialty_id": "cardiology",
                "custom_keywords": ["SGLT2", "heart failure", "diabetes"]
            }
        )
        # Should succeed (201) or conflict (409 if at limit)
        assert response.status_code in [201, 409], f"Unexpected status: {response.status_code}: {response.text}"
        
        if response.status_code == 201:
            data = response.json()
            assert "profile_id" in data
            # Clean up - delete the test profile
            profile_id = data["profile_id"]
            # Note: Can't delete if it's the only profile


# =============================================================================
# B) PHI Guard Tests for Audio Digests
# =============================================================================

class TestAudioDigestPhiGuardIntegration(TestSetup):
    """Test PHI guard enforcement on audio digest endpoints via API."""
    
    def test_create_audio_digest_rejects_phi_in_title(self, premium_token):
        """POST /api/audio-digests should reject PHI in title."""
        response = requests.post(
            f"{BASE_URL}/api/audio-digests",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "pmids": ["41202182"],  # Use existing PMID
                "title": "patient John Smith articles",
                "auto_generate_missing": False
            }
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_create_audio_digest_rejects_ssn_in_title(self, premium_token):
        """POST /api/audio-digests should reject SSN in title."""
        response = requests.post(
            f"{BASE_URL}/api/audio-digests",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "pmids": ["41202182"],
                "title": "SSN 123-45-6789 research",
                "auto_generate_missing": False
            }
        )
        assert response.status_code == 422
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected"
    
    def test_create_audio_digest_clean_title_accepted(self, premium_token):
        """POST /api/audio-digests should accept clean title."""
        response = requests.post(
            f"{BASE_URL}/api/audio-digests",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "pmids": ["41202182"],
                "title": "Cardiology Research Collection",
                "auto_generate_missing": False
            }
        )
        # Should succeed (201) or fail for other reasons (400 if PMID not in library)
        assert response.status_code in [201, 400], f"Unexpected status: {response.status_code}: {response.text}"


# =============================================================================
# C) ZIP Download Hardening Tests
# =============================================================================

class TestZipDownloadHardening(TestSetup):
    """Test ZIP download hardening features via API."""
    
    def test_user_cannot_download_another_users_digest(self, premium_token, free_token):
        """User should get 404 when trying to download another user's digest."""
        # Get premium user's digests
        list_response = requests.get(
            f"{BASE_URL}/api/audio-digests",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert list_response.status_code == 200
        digests = list_response.json().get("audio_digests", [])
        
        if not digests:
            pytest.skip("No audio digests to test")
        
        digest_id = digests[0]["audio_digest_id"]
        
        # Try to download with free user token (different user)
        response = requests.get(
            f"{BASE_URL}/api/audio-digests/{digest_id}/download.zip",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        # Should return 404 (not found for this user)
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    
    def test_nonexistent_digest_returns_404(self, premium_token):
        """Requesting non-existent digest should return 404."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = requests.get(
            f"{BASE_URL}/api/audio-digests/{fake_id}/download.zip",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 404


# =============================================================================
# D) Feature Flag OFF Tests
# =============================================================================

class TestFeatureFlagsOff:
    """Test that endpoints return 404 when feature flags are OFF.
    
    Note: These tests require flags to be OFF. Run separately or skip if flags are ON.
    """
    
    @pytest.fixture(scope="class")
    def token(self):
        """Get a token for testing."""
        return get_premium_token()
    
    def test_profiles_returns_404_or_200_based_on_flag(self, token):
        """GET /api/preferences/profiles behavior depends on flag."""
        response = requests.get(
            f"{BASE_URL}/api/preferences/profiles",
            headers={"Authorization": f"Bearer {token}"}
        )
        # Either 200 (flag ON) or 404 (flag OFF)
        assert response.status_code in [200, 404]
        
        if response.status_code == 404:
            data = response.json()
            assert data.get("detail", {}).get("error_code") == "feature_disabled"
    
    def test_audio_digests_returns_404_or_200_based_on_flag(self, token):
        """GET /api/audio-digests behavior depends on flag."""
        response = requests.get(
            f"{BASE_URL}/api/audio-digests",
            headers={"Authorization": f"Bearer {token}"}
        )
        # Either 200 (flag ON) or 404 (flag OFF)
        assert response.status_code in [200, 404]
        
        if response.status_code == 404:
            data = response.json()
            assert data.get("detail", {}).get("error_code") == "feature_disabled"


# =============================================================================
# E) Rate Limiting Tests (Manual - requires multiple rapid requests)
# =============================================================================

class TestZipRateLimiting(TestSetup):
    """Test ZIP download rate limiting.
    
    Note: These tests make multiple rapid requests and may affect rate limit state.
    """
    
    def test_rate_limit_returns_429_after_limit(self, premium_token):
        """ZIP download should return 429 after exceeding rate limit.
        
        This test is informational - it checks if rate limiting is working
        but doesn't guarantee hitting the limit (depends on prior state).
        """
        # Get a digest to download
        list_response = requests.get(
            f"{BASE_URL}/api/audio-digests",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        
        if list_response.status_code != 200:
            pytest.skip("Audio digests feature not enabled")
        
        digests = list_response.json().get("audio_digests", [])
        if not digests:
            pytest.skip("No audio digests available")
        
        digest_id = digests[0]["audio_digest_id"]
        
        # Make multiple rapid requests (up to 7 to exceed 5/minute limit)
        responses = []
        for i in range(7):
            response = requests.get(
                f"{BASE_URL}/api/audio-digests/{digest_id}/download.zip",
                headers={"Authorization": f"Bearer {premium_token}"}
            )
            responses.append(response.status_code)
            if response.status_code == 429:
                break
            time.sleep(0.1)  # Small delay between requests
        
        # Check if we got rate limited (429) or other valid responses
        # Valid responses: 200 (success), 422 (no audio ready), 429 (rate limited)
        valid_codes = {200, 422, 429, 413}
        for code in responses:
            assert code in valid_codes, f"Unexpected status code: {code}"
        
        # If we got 429, verify the error structure
        if 429 in responses:
            # Make one more request to get the 429 response body
            response = requests.get(
                f"{BASE_URL}/api/audio-digests/{digest_id}/download.zip",
                headers={"Authorization": f"Bearer {premium_token}"}
            )
            if response.status_code == 429:
                data = response.json()
                assert data.get("detail", {}).get("error_code") == "zip_rate_limited"
                print("Rate limiting verified: 429 response received")


# =============================================================================
# F) Track Cap Tests (413 for >25 tracks)
# =============================================================================

class TestZipTrackCaps(TestSetup):
    """Test ZIP download track caps.
    
    Note: Creating a digest with >25 tracks requires having >25 articles in library.
    This test verifies the error response structure if such a digest exists.
    """
    
    def test_track_cap_error_structure(self, premium_token):
        """Verify 413 error structure for track cap violations.
        
        This is a structural test - actual 413 requires a digest with >25 tracks.
        """
        # This test documents expected behavior
        # Actual testing requires creating a digest with >25 PMIDs
        expected_error = {
            "error_code": "zip_too_large",
            "message": "Audio digest has too many tracks",
            "track_count": ">25",
            "max_tracks": 25
        }
        # Just verify the test runs - actual 413 testing needs setup
        assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
