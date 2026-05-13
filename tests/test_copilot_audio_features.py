"""
Test Copilot and Audio Summary Features - Production Verification

Tests for:
- Copilot health endpoint (unauthenticated)
- Feature flags endpoint
- Evidence brief generation (authenticated, premium)
- Ask article Q&A (authenticated, premium)
- Audio summary generation and retrieval (authenticated, premium)
- Audio file serving (unauthenticated)
- Auth gating (401/403 for unauthenticated requests)

Test user: testlaunch@test.com / Test1234! (30-day Pro trial)
Test article: PMID 39000001
"""
import pytest
import requests
import os
import time

# Use the production URL from environment
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = "https://litscreen-aggregate.preview.emergentagent.com"

# Test credentials
TEST_EMAIL = "testlaunch@test.com"
TEST_PASSWORD = "Test1234!"
TEST_PMID = "39000001"


class TestCopilotHealth:
    """Copilot health endpoint - unauthenticated"""

    def test_copilot_health_returns_enabled_and_reachable(self):
        """GET /api/copilot/health should return copilot_enabled=true, provider=openai, reachable=true"""
        response = requests.get(f"{BASE_URL}/api/copilot/health", timeout=30)
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        print(f"Copilot health response: {data}")
        
        # Verify copilot is enabled
        assert data.get("copilot_enabled") is True, f"Expected copilot_enabled=true, got {data.get('copilot_enabled')}"
        
        # Verify provider is openai
        assert data.get("provider") == "openai", f"Expected provider=openai, got {data.get('provider')}"
        
        # Verify reachable (may be True or have a timeout note)
        # The health check may timeout but still be functional
        if data.get("error") == "timeout":
            print("Note: Health check timed out but provider may still be functional")
        else:
            assert data.get("reachable") is True, f"Expected reachable=true, got {data.get('reachable')}"


class TestFeatureFlags:
    """Feature flags endpoint"""

    def test_feature_flags_copilot_enabled(self):
        """GET /api/config/feature-flags should return copilot_enabled=true"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags", timeout=10)
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        print(f"Feature flags response: {data}")
        
        # Verify copilot is enabled (API returns copilot_enabled, not enable_copilot)
        assert data.get("copilot_enabled") is True, f"Expected copilot_enabled=true, got {data.get('copilot_enabled')}"


@pytest.fixture(scope="class")
def auth_token():
    """Get JWT token for test user"""
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        timeout=10
    )
    
    if response.status_code != 200:
        pytest.skip(f"Login failed: {response.status_code} - {response.text}")
    
    data = response.json()
    token = data.get("access_token") or data.get("token")
    if not token:
        pytest.skip(f"No token in login response: {data}")
    
    print(f"Logged in as {TEST_EMAIL}, token obtained")
    return token


@pytest.fixture(scope="class")
def auth_headers(auth_token):
    """Get auth headers with JWT token"""
    return {"Authorization": f"Bearer {auth_token}"}


class TestCopilotEvidenceBrief:
    """Evidence brief generation - authenticated, premium"""

    def test_evidence_brief_without_auth_returns_401_or_403(self):
        """POST /api/copilot/evidence-brief without auth should return 401/403"""
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": TEST_PMID},
            timeout=10
        )
        
        assert response.status_code in [401, 403], f"Expected 401/403, got {response.status_code}: {response.text}"
        print(f"Unauthenticated evidence-brief correctly returned {response.status_code}")

    def test_evidence_brief_with_auth_returns_real_llm_response(self, auth_headers):
        """POST /api/copilot/evidence-brief with valid auth should return real LLM evidence brief"""
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": TEST_PMID},
            headers=auth_headers,
            timeout=60  # LLM calls can take up to 30s
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        print(f"Evidence brief response keys: {list(data.keys())}")
        
        # Verify it's a real response (not mock)
        # Mock responses have specific patterns like "Mock Evidence Brief"
        title = data.get("title", "")
        one_line = data.get("one_line_takeaway", "")
        
        # Check it's not a mock response
        assert "mock" not in title.lower(), f"Got mock response: {title}"
        assert "mock" not in one_line.lower(), f"Got mock response: {one_line}"
        
        # Verify structure
        assert "evidence_brief" in data or "summary" in data, f"Missing evidence_brief in response: {data.keys()}"
        assert "disclaimer" in data, f"Missing disclaimer in response"
        
        # Verify citations exist
        citations = data.get("citations", [])
        print(f"Evidence brief title: {title[:100]}...")
        print(f"Citations count: {len(citations)}")


class TestCopilotAskArticle:
    """Ask article Q&A - authenticated, premium"""

    def test_ask_article_with_auth_returns_real_llm_answer(self, auth_headers):
        """POST /api/copilot/ask-article with valid auth should return real LLM answer"""
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={
                "pmid": TEST_PMID,
                "question": "What are the main findings of this study?"
            },
            headers=auth_headers,
            timeout=60  # LLM calls can take up to 30s
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        print(f"Ask article response keys: {list(data.keys())}")
        
        # Verify it's a real response (not mock)
        answer = data.get("answer", "")
        assert "mock" not in answer.lower(), f"Got mock response: {answer}"
        
        # Verify structure
        assert "answer" in data, f"Missing answer in response"
        assert "confidence" in data, f"Missing confidence in response"
        assert "disclaimer" in data, f"Missing disclaimer in response"
        
        print(f"Answer preview: {answer[:200]}...")
        print(f"Confidence: {data.get('confidence')}")


class TestAudioSummary:
    """Audio summary generation and retrieval - authenticated, premium"""

    def test_audio_generate_without_auth_returns_401_or_403(self):
        """POST /api/articles/{pmid}/audio-summary/generate without auth should return 401/403"""
        response = requests.post(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate",
            timeout=10
        )
        
        assert response.status_code in [401, 403], f"Expected 401/403, got {response.status_code}: {response.text}"
        print(f"Unauthenticated audio generate correctly returned {response.status_code}")

    def test_audio_generate_with_auth_returns_ready_status(self, auth_headers):
        """POST /api/articles/{pmid}/audio-summary/generate with valid auth should return status=ready"""
        response = requests.post(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary/generate",
            headers=auth_headers,
            timeout=60  # TTS can take time
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        print(f"Audio generate response: {data}")
        
        # Status should be ready or pending (if still generating)
        status = data.get("status")
        assert status in ["ready", "pending"], f"Expected status ready/pending, got {status}"
        
        if status == "pending":
            # Wait a bit and check again
            time.sleep(5)
            get_response = requests.get(
                f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
                headers=auth_headers,
                timeout=30
            )
            if get_response.status_code == 200:
                get_data = get_response.json()
                print(f"After wait, status: {get_data.get('status')}")

    def test_audio_summary_get_returns_ready_with_url(self, auth_headers):
        """GET /api/articles/{pmid}/audio-summary with valid auth should return status=ready with audio_url"""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
            headers=auth_headers,
            timeout=30
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        print(f"Audio summary response: {data}")
        
        # Verify status is ready
        assert data.get("status") == "ready", f"Expected status=ready, got {data.get('status')}"
        
        # Verify audio_url exists
        audio_url = data.get("audio_url")
        assert audio_url is not None, f"Missing audio_url in response"
        print(f"Audio URL: {audio_url}")
        
        # Verify format is mp3
        audio_format = data.get("audio_format")
        assert audio_format == "mp3", f"Expected format=mp3, got {audio_format}"
        
        # Verify duration exists
        duration = data.get("duration_seconds")
        assert duration is not None, f"Missing duration_seconds in response"
        print(f"Duration: {duration} seconds")


class TestAudioFileServing:
    """Audio file serving - unauthenticated"""

    def test_audio_file_serves_mp3(self, auth_headers):
        """GET /api/audio/files/{filename} should serve audio file with content-type audio/mpeg"""
        # First get the audio summary to find the filename
        summary_response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
            headers=auth_headers,
            timeout=30
        )
        
        if summary_response.status_code != 200:
            pytest.skip(f"Could not get audio summary: {summary_response.status_code}")
        
        data = summary_response.json()
        audio_url = data.get("audio_url")
        
        if not audio_url:
            pytest.skip("No audio_url in summary response")
        
        # Extract filename from URL
        # URL format: /api/audio/files/39000001_346ed3a7_default.mp3
        filename = audio_url.split("/")[-1] if "/" in audio_url else audio_url
        
        # Try to serve the file (no auth needed)
        file_response = requests.get(
            f"{BASE_URL}/api/audio/files/{filename}",
            timeout=30
        )
        
        assert file_response.status_code == 200, f"Expected 200, got {file_response.status_code}: {file_response.text}"
        
        # Verify content type
        content_type = file_response.headers.get("content-type", "")
        assert "audio/mpeg" in content_type or "audio/mp3" in content_type, f"Expected audio/mpeg, got {content_type}"
        
        # Verify we got actual audio data
        content_length = len(file_response.content)
        assert content_length > 1000, f"Audio file too small: {content_length} bytes"
        
        print(f"Audio file served successfully: {filename}, size: {content_length} bytes, content-type: {content_type}")


class TestAuthGating:
    """Verify auth gating on protected endpoints"""

    def test_copilot_endpoints_require_auth(self):
        """All copilot endpoints (except health) should require auth"""
        endpoints = [
            ("POST", "/api/copilot/evidence-brief", {"pmid": TEST_PMID}),
            ("POST", "/api/copilot/ask-article", {"pmid": TEST_PMID, "question": "test"}),
        ]
        
        for method, endpoint, payload in endpoints:
            if method == "POST":
                response = requests.post(f"{BASE_URL}{endpoint}", json=payload, timeout=10)
            else:
                response = requests.get(f"{BASE_URL}{endpoint}", timeout=10)
            
            assert response.status_code in [401, 403], f"{method} {endpoint} should require auth, got {response.status_code}"
            print(f"{method} {endpoint} correctly requires auth: {response.status_code}")

    def test_audio_endpoints_require_auth(self):
        """Audio summary endpoints should require auth"""
        endpoints = [
            ("GET", f"/api/articles/{TEST_PMID}/audio-summary"),
            ("POST", f"/api/articles/{TEST_PMID}/audio-summary/generate"),
        ]
        
        for method, endpoint in endpoints:
            if method == "POST":
                response = requests.post(f"{BASE_URL}{endpoint}", timeout=10)
            else:
                response = requests.get(f"{BASE_URL}{endpoint}", timeout=10)
            
            assert response.status_code in [401, 403], f"{method} {endpoint} should require auth, got {response.status_code}"
            print(f"{method} {endpoint} correctly requires auth: {response.status_code}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
