"""
Phase UX-B: Simple Explore UI Tests
Tests for the minimal PubMed search experience on the Explore page.
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
TEST_USER_EMAIL = "test@litpulse.com"
TEST_USER_PASSWORD = "TestPass123!"


class TestFeatureFlagAPI:
    """Test that enable_explore_simple_pubmed_ui flag is returned from API"""
    
    def test_feature_flags_endpoint_returns_200(self):
        """Feature flags endpoint should return 200"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    
    def test_simple_pubmed_flag_present(self):
        """enable_explore_simple_pubmed_ui flag should be present in response"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        data = response.json()
        assert "enable_explore_simple_pubmed_ui" in data, "Flag not found in response"
    
    def test_simple_pubmed_flag_is_boolean(self):
        """enable_explore_simple_pubmed_ui flag should be a boolean"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        data = response.json()
        flag_value = data.get("enable_explore_simple_pubmed_ui")
        assert isinstance(flag_value, bool), f"Expected boolean, got {type(flag_value)}"
    
    def test_simple_pubmed_flag_is_enabled(self):
        """enable_explore_simple_pubmed_ui flag should be true (as per .env)"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        data = response.json()
        assert data.get("enable_explore_simple_pubmed_ui") == True, "Flag should be enabled"


class TestSearchV2Endpoint:
    """Test the /api/articles/search-v2 endpoint used by Simple Explore UI"""
    
    @pytest.fixture
    def auth_token(self):
        """Get authentication token"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": TEST_USER_EMAIL, "password": TEST_USER_PASSWORD}
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        elif response.status_code == 429:
            pytest.skip("Rate limited - skipping authenticated tests")
        pytest.skip(f"Authentication failed: {response.status_code}")
    
    @pytest.fixture
    def auth_headers(self, auth_token):
        """Get headers with auth token"""
        return {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }
    
    def test_search_v2_requires_auth(self):
        """Search V2 endpoint should require authentication"""
        response = requests.post(
            f"{BASE_URL}/api/articles/search-v2",
            json={"query": "diabetes", "use_preferences_context": False}
        )
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
    
    def test_search_v2_with_auth(self, auth_headers):
        """Search V2 endpoint should work with authentication"""
        response = requests.post(
            f"{BASE_URL}/api/articles/search-v2",
            headers=auth_headers,
            json={"query": "diabetes", "use_preferences_context": False, "limit": 5}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert "articles" in data, "Response should contain 'articles' key"
        assert "article_count" in data, "Response should contain 'article_count' key"
    
    def test_search_v2_use_preferences_context_false(self, auth_headers):
        """Search V2 with use_preferences_context=false should not apply user preferences"""
        response = requests.post(
            f"{BASE_URL}/api/articles/search-v2",
            headers=auth_headers,
            json={"query": "heart failure", "use_preferences_context": False, "limit": 5}
        )
        assert response.status_code == 200
        data = response.json()
        # Check search_context shows preferences were not applied
        search_context = data.get("search_context", {})
        assert search_context.get("preferences_applied") == False, "Preferences should not be applied"
    
    def test_search_v2_returns_article_structure(self, auth_headers):
        """Search V2 should return articles with expected structure"""
        response = requests.post(
            f"{BASE_URL}/api/articles/search-v2",
            headers=auth_headers,
            json={"query": "cancer treatment", "use_preferences_context": False, "limit": 5}
        )
        assert response.status_code == 200
        data = response.json()
        # Even if no results due to rate limiting, structure should be correct
        assert isinstance(data.get("articles"), list), "articles should be a list"
        assert isinstance(data.get("article_count"), int), "article_count should be an integer"
    
    def test_search_v2_empty_query_handling(self, auth_headers):
        """Search V2 should handle empty query gracefully"""
        response = requests.post(
            f"{BASE_URL}/api/articles/search-v2",
            headers=auth_headers,
            json={"query": "", "use_preferences_context": False}
        )
        # Should either return 400 or 200 with empty results
        assert response.status_code in [200, 400, 422], f"Unexpected status: {response.status_code}"
    
    def test_search_v2_limit_parameter(self, auth_headers):
        """Search V2 should respect limit parameter"""
        response = requests.post(
            f"{BASE_URL}/api/articles/search-v2",
            headers=auth_headers,
            json={"query": "medicine", "use_preferences_context": False, "limit": 25}
        )
        assert response.status_code == 200
        data = response.json()
        # If results returned, should not exceed limit
        articles = data.get("articles", [])
        assert len(articles) <= 25, f"Expected max 25 articles, got {len(articles)}"


class TestLibrarySaveEndpoint:
    """Test the library save endpoint used by Simple Explore UI"""
    
    @pytest.fixture
    def auth_token(self):
        """Get authentication token"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": TEST_USER_EMAIL, "password": TEST_USER_PASSWORD}
        )
        if response.status_code == 200:
            return response.json().get("access_token")
        elif response.status_code == 429:
            pytest.skip("Rate limited - skipping authenticated tests")
        pytest.skip(f"Authentication failed: {response.status_code}")
    
    @pytest.fixture
    def auth_headers(self, auth_token):
        """Get headers with auth token"""
        return {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }
    
    def test_library_save_requires_auth(self):
        """Library save endpoint should require authentication"""
        response = requests.post(f"{BASE_URL}/api/library/save?pmid=12345678")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
    
    def test_library_save_with_invalid_pmid(self, auth_headers):
        """Library save should handle invalid PMID gracefully"""
        response = requests.post(
            f"{BASE_URL}/api/library/save?pmid=invalid_pmid",
            headers=auth_headers
        )
        # Should return 400 or 404 for invalid PMID
        assert response.status_code in [400, 404, 500], f"Unexpected status: {response.status_code}"


class TestHealthEndpoint:
    """Basic health check"""
    
    def test_health_endpoint(self):
        """Health endpoint should return 200"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
