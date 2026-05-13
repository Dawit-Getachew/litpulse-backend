"""
Step 18: Library Pagination UI Adoption + Frontend Error Boundaries
Tests backend APIs used by the frontend pagination adoption.
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


@pytest.fixture(scope="module")
def premium_auth_header():
    """Get auth header for premium admin user - module scoped to avoid rate limits"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json=PREMIUM_USER)
    if resp.status_code != 200:
        pytest.skip("Premium user login failed")
    token = resp.json().get("access_token")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def free_auth_header():
    """Get auth header for free user"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json=FREE_USER)
    if resp.status_code != 200:
        pytest.skip("Free user login failed")
    token = resp.json().get("access_token")
    return {"Authorization": f"Bearer {token}"}


class TestLibraryPaginationAPI:
    """Test GET /api/library with pagination params (Step 18 frontend adoption)"""
    
    def test_library_with_limit_50(self, premium_auth_header):
        """Library API with limit=50 (the frontend page size)"""
        resp = requests.get(f"{BASE_URL}/api/library", params={"limit": 50}, headers=premium_auth_header)
        assert resp.status_code == 200
        
        data = resp.json()
        # Verify response structure matches LibraryResponse type
        assert "articles" in data, "Response should have 'articles' array"
        assert "total" in data, "Response should have 'total' count"
        assert "next_cursor" in data, "Response should have 'next_cursor' (can be null)"
        
        # Verify types
        assert isinstance(data["articles"], list), "articles should be a list"
        assert isinstance(data["total"], int), "total should be an integer"
        assert data["next_cursor"] is None or isinstance(data["next_cursor"], str), "next_cursor should be null or string"
        
        print(f"Library response: {len(data['articles'])} articles, total={data['total']}, next_cursor={data['next_cursor']}")
    
    def test_library_backward_compat_no_params(self, premium_auth_header):
        """Library API without params (backward compatibility)"""
        resp = requests.get(f"{BASE_URL}/api/library", headers=premium_auth_header)
        assert resp.status_code == 200
        
        data = resp.json()
        # Should still have same structure
        assert "articles" in data
        assert "total" in data
        assert "next_cursor" in data
        
        print(f"Backward compat: {len(data['articles'])} articles, total={data['total']}")
    
    def test_library_with_cursor_param(self, premium_auth_header):
        """Library API with cursor param (if next_cursor exists)"""
        # First get initial page
        resp = requests.get(f"{BASE_URL}/api/library", params={"limit": 1}, headers=premium_auth_header)
        assert resp.status_code == 200
        data = resp.json()
        
        cursor = data.get("next_cursor")
        if cursor:
            # Fetch next page
            resp2 = requests.get(f"{BASE_URL}/api/library", params={"limit": 1, "cursor": cursor}, headers=premium_auth_header)
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert "articles" in data2
            print(f"Cursor-based pagination working: {len(data2['articles'])} articles on page 2")
        else:
            print("No cursor returned (all articles fit in first page - expected behavior)")
    
    def test_library_returns_article_structure(self, premium_auth_header):
        """Verify article objects have expected fields"""
        resp = requests.get(f"{BASE_URL}/api/library", params={"limit": 50}, headers=premium_auth_header)
        assert resp.status_code == 200
        data = resp.json()
        
        if data["articles"]:
            article = data["articles"][0]
            # Check common fields that LibraryPage.js uses
            assert "pmid" in article or "article_id" in article, "Article should have identifier"
            assert "title" in article, "Article should have title"
            # Optional fields
            for field in ["journal", "pub_date", "design_tags", "saved_at"]:
                if field in article:
                    print(f"Field '{field}' present in article")
            print(f"Article structure validated: title='{article.get('title', '')[:50]}...'")
        else:
            print("No articles in library - empty state expected")


class TestAdminPageAPI:
    """Test admin APIs that AdminPage uses (wrapped in ErrorBoundary)"""
    
    def test_admin_metrics_returns_data(self, premium_auth_header):
        """Admin metrics endpoint still works (AdminPage is wrapped in ErrorBoundary)"""
        resp = requests.get(f"{BASE_URL}/api/admin/metrics", headers=premium_auth_header)
        assert resp.status_code == 200
        
        data = resp.json()
        # Check for expected sections in the metrics response
        assert "users" in data or "articles" in data, "Metrics should have 'users' or 'articles' section"
        print(f"Admin metrics sections: {list(data.keys())}")
    
    def test_admin_slow_queries(self, premium_auth_header):
        """Slow queries endpoint works (AdminPage Slow Queries panel)"""
        resp = requests.get(f"{BASE_URL}/api/admin/slow-queries", headers=premium_auth_header)
        assert resp.status_code == 200
        
        data = resp.json()
        assert "queries" in data
        print(f"Slow queries: {data.get('count', 0)} queries")


class TestPlanPageAPI:
    """Test billing APIs that PlanPage uses (wrapped in ErrorBoundary)"""
    
    def test_billing_me_endpoint(self, premium_auth_header):
        """Billing status endpoint works (PlanPage is wrapped in ErrorBoundary)"""
        resp = requests.get(f"{BASE_URL}/api/billing/me", headers=premium_auth_header)
        assert resp.status_code == 200
        
        data = resp.json()
        # Check it has expected fields
        assert "plan_tier" in data or "status" in data or "trial_active" in data
        print(f"Billing status: {data}")


class TestHealthCheck:
    """Basic health check"""
    
    def test_health_endpoint(self):
        """Health endpoint returns OK"""
        resp = requests.get(f"{BASE_URL}/api/health")
        assert resp.status_code == 200
        print("Health check passed")
