"""
Iteration 4: Test digest screening cards redesign and regression tests.

Tests:
1. GET /api/workspace/screen/queue - returns articles with correct fields
2. POST /api/workspace/screen/decide - save/skip/defer actions
3. Regression: GET /api/copilot/health - copilot still works
4. Regression: GET /api/audio/my-summaries - audio endpoints still work
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
TEST_EMAIL = "testlaunch@test.com"
TEST_PASSWORD = "Test1234!"


class TestAuth:
    """Authentication helper tests"""
    
    @pytest.fixture(scope="class")
    def auth_token(self):
        """Get authentication token for test user"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD}
        )
        assert response.status_code == 200, f"Login failed: {response.text}"
        data = response.json()
        assert "access_token" in data, "No access_token in response"
        return data["access_token"]
    
    @pytest.fixture(scope="class")
    def auth_headers(self, auth_token):
        """Get auth headers"""
        return {"Authorization": f"Bearer {auth_token}"}


class TestScreenQueueEndpoint(TestAuth):
    """Test GET /api/workspace/screen/queue endpoint"""
    
    def test_screen_queue_returns_200(self, auth_headers):
        """Screen queue endpoint returns 200 with auth"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue",
            headers=auth_headers
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print("✓ Screen queue returns 200")
    
    def test_screen_queue_response_structure(self, auth_headers):
        """Screen queue returns correct response structure"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        
        # Check top-level fields
        assert "articles" in data, "Missing 'articles' field"
        assert "progress" in data, "Missing 'progress' field"
        assert "filter_status" in data, "Missing 'filter_status' field"
        
        # Check progress structure
        progress = data["progress"]
        assert "total" in progress, "Missing 'total' in progress"
        assert "saved" in progress, "Missing 'saved' in progress"
        assert "deferred" in progress, "Missing 'deferred' in progress"
        assert "skipped" in progress, "Missing 'skipped' in progress"
        assert "remaining" in progress, "Missing 'remaining' in progress"
        
        print(f"✓ Screen queue response structure correct. Progress: {progress}")
    
    def test_screen_queue_article_fields(self, auth_headers):
        """Screen queue articles have required fields for card display"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        
        articles = data.get("articles", [])
        if len(articles) == 0:
            pytest.skip("No articles in queue to test field structure")
        
        # Check first article has required fields
        article = articles[0]
        required_fields = ["pmid", "title", "abstract", "ai_summary", "screening_status"]
        optional_fields = ["design_tags", "topic_tags", "journal", "pub_date", "is_in_library"]
        
        for field in required_fields:
            assert field in article, f"Missing required field '{field}' in article"
        
        print(f"✓ Article has required fields. PMID: {article.get('pmid')}")
        print(f"  - Title: {article.get('title', '')[:50]}...")
        print(f"  - Has abstract: {bool(article.get('abstract'))}")
        print(f"  - Has AI summary: {bool(article.get('ai_summary'))}")
        print(f"  - Design tags: {article.get('design_tags', [])}")
        print(f"  - Topic tags: {article.get('topic_tags', [])}")
    
    def test_screen_queue_filter_unscreened(self, auth_headers):
        """Screen queue filters by unscreened status"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue?status=unscreened",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("filter_status") == "unscreened"
        print(f"✓ Unscreened filter works. Articles: {len(data.get('articles', []))}")
    
    def test_screen_queue_filter_saved(self, auth_headers):
        """Screen queue filters by saved status"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue?status=saved",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("filter_status") == "saved"
        print(f"✓ Saved filter works. Articles: {len(data.get('articles', []))}")
    
    def test_screen_queue_filter_all(self, auth_headers):
        """Screen queue filters by all status"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue?status=all",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("filter_status") == "all"
        print(f"✓ All filter works. Articles: {len(data.get('articles', []))}")


class TestScreenDecisionEndpoint(TestAuth):
    """Test POST /api/workspace/screening/decision endpoint"""
    
    def test_screening_decision_endpoint_exists(self, auth_headers):
        """Screening decision endpoint exists and requires valid decision"""
        # Test with invalid decision
        response = requests.post(
            f"{BASE_URL}/api/workspace/screening/decision",
            headers=auth_headers,
            json={"article_id": "test123", "decision": "invalid"}
        )
        # Should return 400 for invalid decision, not 404
        assert response.status_code in [400, 422], f"Expected 400/422, got {response.status_code}"
        print("✓ Screening decision endpoint exists and validates input")
    
    def test_screening_decision_requires_auth(self):
        """Screening decision requires authentication"""
        response = requests.post(
            f"{BASE_URL}/api/workspace/screening/decision",
            json={"article_id": "test123", "decision": "keep"}
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print("✓ Screening decision requires auth")


class TestScreenDigestsEndpoint(TestAuth):
    """Test GET /api/workspace/screen/digests endpoint"""
    
    def test_screen_digests_returns_200(self, auth_headers):
        """Screen digests endpoint returns 200"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/digests",
            headers=auth_headers
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print("✓ Screen digests returns 200")
    
    def test_screen_digests_response_structure(self, auth_headers):
        """Screen digests returns correct structure"""
        response = requests.get(
            f"{BASE_URL}/api/workspace/screen/digests",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        
        assert "digests" in data, "Missing 'digests' field"
        assert isinstance(data["digests"], list), "'digests' should be a list"
        
        if len(data["digests"]) > 0:
            digest = data["digests"][0]
            assert "digest_id" in digest, "Missing 'digest_id' in digest"
            assert "article_count" in digest, "Missing 'article_count' in digest"
            print(f"✓ Screen digests structure correct. Found {len(data['digests'])} digests")
        else:
            print("✓ Screen digests structure correct. No digests found.")


class TestRegressionCopilot(TestAuth):
    """Regression tests for Copilot feature"""
    
    def test_copilot_health_endpoint(self):
        """Copilot health endpoint still works (no auth required)"""
        response = requests.get(f"{BASE_URL}/api/copilot/health")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        
        assert "copilot_enabled" in data, "Missing 'copilot_enabled'"
        assert "reachable" in data, "Missing 'reachable'"
        assert data["reachable"] == True, "Copilot should be reachable"
        
        print(f"✓ Copilot health: enabled={data.get('copilot_enabled')}, reachable={data.get('reachable')}")


class TestRegressionAudio(TestAuth):
    """Regression tests for Audio feature"""
    
    def test_audio_my_summaries_endpoint(self, auth_headers):
        """Audio my-summaries endpoint still works"""
        response = requests.get(
            f"{BASE_URL}/api/audio/my-summaries",
            headers=auth_headers
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # API returns 'audio_summaries' field
        assert "audio_summaries" in data, "Missing 'audio_summaries' field"
        print(f"✓ Audio my-summaries works. Found {len(data.get('audio_summaries', []))} summaries")
    
    def test_audio_my_summaries_requires_auth(self):
        """Audio my-summaries requires authentication"""
        response = requests.get(f"{BASE_URL}/api/audio/my-summaries")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print("✓ Audio my-summaries requires auth")


class TestRegressionAdminDashboard(TestAuth):
    """Regression tests for Admin Copilot Dashboard"""
    
    def test_admin_copilot_dashboard_requires_auth(self):
        """Admin copilot dashboard requires authentication"""
        response = requests.get(f"{BASE_URL}/api/admin/copilot-dashboard")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print("✓ Admin copilot dashboard requires auth")
    
    def test_admin_copilot_dashboard_requires_admin(self, auth_headers):
        """Admin copilot dashboard requires admin role (test user is not admin)"""
        # Note: testlaunch@test.com is NOT the admin (admin is info@scienthesis.ai)
        # So this should return 403
        response = requests.get(
            f"{BASE_URL}/api/admin/copilot-dashboard",
            headers=auth_headers
        )
        # Test user is not admin, so expect 403
        assert response.status_code == 403, f"Expected 403 for non-admin, got {response.status_code}"
        print("✓ Admin copilot dashboard correctly returns 403 for non-admin user")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
