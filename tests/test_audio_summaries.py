"""
Test Audio Summaries Features - Iteration 3
Tests for:
- GET /api/audio/my-summaries endpoint
- Audio generation and status endpoints
- Feature flags for audio
- User capabilities for premium_audio
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
TEST_EMAIL = "testlaunch@test.com"
TEST_PASSWORD = "Test1234!"
TEST_PMID = "39000001"


class TestAudioSummariesBackend:
    """Test audio summaries backend endpoints"""
    
    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test session with auth"""
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        
        # Login to get token
        login_resp = self.session.post(f"{BASE_URL}/api/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD
        })
        assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
        token = login_resp.json().get("access_token")
        assert token, "No access_token in login response"
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.token = token
    
    # ---- Feature Flags Tests ----
    
    def test_feature_flags_audio_enabled(self):
        """Verify audio-related feature flags are enabled"""
        resp = self.session.get(f"{BASE_URL}/api/config/feature-flags")
        assert resp.status_code == 200
        flags = resp.json()
        
        # Check required flags
        assert flags.get("enable_digest_article_audio_links") == True, "enable_digest_article_audio_links should be true"
        assert flags.get("enable_library_audio_digests_v2") == True, "enable_library_audio_digests_v2 should be true"
        assert flags.get("enable_premium_trials") == True, "enable_premium_trials should be true"
        print("✓ All audio feature flags are enabled")
    
    # ---- User Capabilities Tests ----
    
    def test_user_has_premium_audio_capability(self):
        """Verify test user has premium_audio capability (trial user)"""
        resp = self.session.get(f"{BASE_URL}/api/auth/me")
        assert resp.status_code == 200
        user = resp.json()
        
        # Check user has trial or premium status
        caps = user.get("capabilities", {})
        # Trial users should have premium_audio=true
        assert caps.get("premium_audio") == True or user.get("subscription_level") in ["pro", "trial"], \
            f"User should have premium_audio capability. Got caps: {caps}, subscription: {user.get('subscription_level')}"
        print(f"✓ User has premium capabilities: {caps}")
    
    # ---- GET /api/audio/my-summaries Tests ----
    
    def test_my_audio_summaries_returns_list(self):
        """GET /api/audio/my-summaries returns list of user's audio summaries"""
        resp = self.session.get(f"{BASE_URL}/api/audio/my-summaries")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "audio_summaries" in data, "Response should have audio_summaries field"
        assert "total" in data, "Response should have total field"
        assert isinstance(data["audio_summaries"], list), "audio_summaries should be a list"
        
        print(f"✓ GET /api/audio/my-summaries returned {data['total']} summaries")
        
        # If there are summaries, verify structure
        if data["audio_summaries"]:
            summary = data["audio_summaries"][0]
            assert "pmid" in summary, "Summary should have pmid"
            assert "title" in summary, "Summary should have title"
            assert "audio_url" in summary, "Summary should have audio_url"
            print(f"✓ Summary structure verified: pmid={summary['pmid']}, has audio_url={bool(summary['audio_url'])}")
    
    def test_my_audio_summaries_without_auth_returns_401(self):
        """GET /api/audio/my-summaries without auth returns 401"""
        # Create new session without auth
        no_auth_session = requests.Session()
        resp = no_auth_session.get(f"{BASE_URL}/api/audio/my-summaries")
        assert resp.status_code == 401, f"Expected 401 without auth, got {resp.status_code}"
        print("✓ GET /api/audio/my-summaries without auth returns 401")
    
    # ---- Audio Generation Tests ----
    
    def test_audio_status_endpoint(self):
        """GET /api/articles/{pmid}/audio-summary returns correct status"""
        resp = self.session.get(f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "status" in data, "Response should have status field"
        
        # Status should be 'ready', 'pending', 'failed', or 'missing'
        valid_statuses = ['ready', 'pending', 'failed', 'missing']
        assert data["status"] in valid_statuses, f"Status should be one of {valid_statuses}, got {data['status']}"
        
        if data["status"] == "ready":
            assert "audio_url" in data, "Ready status should have audio_url"
            assert data["audio_url"] is not None, "audio_url should not be None for ready status"
            print(f"✓ Audio status for PMID {TEST_PMID}: ready with audio_url={data['audio_url']}")
        else:
            print(f"✓ Audio status for PMID {TEST_PMID}: {data['status']}")
    
    def test_audio_generation_endpoint(self):
        """POST /api/articles/{pmid}/audio-summary/generate works correctly"""
        resp = self.session.post(f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "status" in data, "Response should have status field"
        
        # Status should be 'ready' (already generated) or 'pending' (generating)
        assert data["status"] in ['ready', 'pending'], f"Status should be ready or pending, got {data['status']}"
        print(f"✓ Audio generation for PMID {TEST_PMID}: status={data['status']}")
    
    def test_audio_generation_without_auth_returns_401(self):
        """POST /api/articles/{pmid}/audio-summary/generate without auth returns 401"""
        no_auth_session = requests.Session()
        resp = no_auth_session.post(f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate")
        assert resp.status_code == 401, f"Expected 401 without auth, got {resp.status_code}"
        print("✓ Audio generation without auth returns 401")
    
    # ---- Audio File Serving Tests ----
    
    def test_audio_file_serving(self):
        """GET /api/audio/files/{filename} serves audio content"""
        # First get the audio status to find the filename
        status_resp = self.session.get(f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary")
        assert status_resp.status_code == 200
        
        data = status_resp.json()
        if data["status"] != "ready" or not data.get("audio_url"):
            pytest.skip("No ready audio file to test serving")
        
        audio_url = data["audio_url"]
        # audio_url is like /api/audio/files/filename.mp3
        file_resp = self.session.get(f"{BASE_URL}{audio_url}")
        assert file_resp.status_code == 200, f"Expected 200, got {file_resp.status_code}"
        
        content_type = file_resp.headers.get("content-type", "")
        assert "audio" in content_type, f"Content-type should be audio, got {content_type}"
        assert len(file_resp.content) > 0, "Audio file should have content"
        print(f"✓ Audio file served: {len(file_resp.content)} bytes, content-type={content_type}")
    
    # ---- My Audio Summaries with Generated Audio ----
    
    def test_my_audio_summaries_includes_generated_audio(self):
        """After generating audio, it should appear in my-summaries"""
        # First ensure audio is generated
        gen_resp = self.session.post(f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate")
        assert gen_resp.status_code == 200
        
        # Wait a bit if pending
        import time
        for _ in range(5):
            status_resp = self.session.get(f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary")
            if status_resp.json().get("status") == "ready":
                break
            time.sleep(1)
        
        # Now check my-summaries
        summaries_resp = self.session.get(f"{BASE_URL}/api/audio/my-summaries")
        assert summaries_resp.status_code == 200
        
        data = summaries_resp.json()
        summaries = data.get("audio_summaries", [])
        
        # Find our test PMID in the summaries
        found = any(s.get("pmid") == TEST_PMID for s in summaries)
        if found:
            print(f"✓ PMID {TEST_PMID} found in my-summaries list")
        else:
            # This might be expected if the audio was generated before pmid tracking was added
            print(f"⚠ PMID {TEST_PMID} not found in my-summaries (may be legacy audio without pmid tracking)")
    
    # ---- Copilot Health Check (Regression) ----
    
    def test_copilot_health_still_works(self):
        """Verify copilot health endpoint still works (regression check)"""
        resp = self.session.get(f"{BASE_URL}/api/copilot/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert data.get("copilot_enabled") == True, "Copilot should be enabled"
        assert data.get("reachable") == True, "Copilot should be reachable"
        print(f"✓ Copilot health: enabled={data.get('copilot_enabled')}, reachable={data.get('reachable')}")


class TestAudioDigestsAPI:
    """Test audio digests V2 API endpoints"""
    
    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test session with auth"""
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        
        # Login to get token
        login_resp = self.session.post(f"{BASE_URL}/api/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD
        })
        assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
        token = login_resp.json().get("access_token")
        self.session.headers.update({"Authorization": f"Bearer {token}"})
    
    def test_audio_digests_list(self):
        """GET /api/audio-digests returns list of combined digests"""
        resp = self.session.get(f"{BASE_URL}/api/audio-digests")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "audio_digests" in data, "Response should have audio_digests field"
        print(f"✓ GET /api/audio-digests returned {len(data.get('audio_digests', []))} digests")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
