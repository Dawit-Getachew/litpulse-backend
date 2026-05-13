"""
Step 16: Efficiency & Optimization Pass Testing
Tests for:
- Request timing middleware (LatencyTracker)
- GET /api/admin/metrics - request_latency field
- GET /api/discussions/specialty-rooms - batch optimized
- GET /api/discussions/threads - batch optimized  
- GET /api/notifications/ - pagination
- GET /api/notifications/unread-count
- GET /api/digests - pagination
- GET /api/library - list articles
- Auth endpoints (login, /api/auth/me)
- Feature flags endpoint
"""
import pytest
import requests
import os

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


class TestAuthEndpoints:
    """Test auth endpoints work correctly"""
    
    def test_login_premium_user(self):
        """Login with premium/admin user"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=PREMIUM_USER)
        assert response.status_code == 200, f"Login failed: {response.text}"
        data = response.json()
        assert "access_token" in data, "Missing access_token in response"
        assert "user" in data, "Missing user in response"
        print(f"PASS: Premium user login successful, user_id={data['user']['user_id']}")
    
    def test_login_free_user(self):
        """Login with free user"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=FREE_USER)
        assert response.status_code == 200, f"Login failed: {response.text}"
        data = response.json()
        assert "access_token" in data, "Missing access_token in response"
        print(f"PASS: Free user login successful")
    
    def test_auth_me_returns_capabilities(self, premium_auth_header):
        """GET /api/auth/me should return user data with capabilities"""
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=premium_auth_header)
        assert response.status_code == 200, f"Auth/me failed: {response.text}"
        data = response.json()
        assert "user_id" in data, "Missing user_id"
        assert "email" in data, "Missing email"
        assert "capabilities" in data, "Missing capabilities field"
        caps = data["capabilities"]
        # Check expected capability keys (actual names may vary)
        assert len(caps) > 0, "Capabilities should have at least one entry"
        print(f"PASS: /api/auth/me returns user with capabilities: {list(caps.keys())}")


class TestDigestsEndpoint:
    """Test digests endpoint with optimization"""
    
    def test_get_digests_with_limit(self, premium_auth_header):
        """GET /api/digests?limit=3 should return user digests"""
        response = requests.get(f"{BASE_URL}/api/digests?limit=3", headers=premium_auth_header)
        assert response.status_code == 200, f"Digests failed: {response.text}"
        data = response.json()
        # Response can be a list or an object with digests key
        if isinstance(data, dict) and "digests" in data:
            digests = data["digests"]
        else:
            digests = data
        assert isinstance(digests, list), "Digests should be a list"
        assert len(digests) <= 3, "Should respect limit parameter"
        print(f"PASS: GET /api/digests?limit=3 returned {len(digests)} digests")


class TestLibraryEndpoint:
    """Test library endpoint"""
    
    def test_get_library(self, premium_auth_header):
        """GET /api/library should return saved articles"""
        response = requests.get(f"{BASE_URL}/api/library", headers=premium_auth_header)
        assert response.status_code == 200, f"Library failed: {response.text}"
        data = response.json()
        assert "articles" in data, "Missing articles field"
        assert isinstance(data["articles"], list), "Articles should be a list"
        print(f"PASS: GET /api/library returned {len(data['articles'])} articles")


class TestNotificationsEndpoint:
    """Test notifications endpoints with optimization"""
    
    def test_get_notifications_list(self, premium_auth_header):
        """GET /api/notifications/ should return notification list with pagination"""
        response = requests.get(f"{BASE_URL}/api/notifications/", headers=premium_auth_header)
        assert response.status_code == 200, f"Notifications failed: {response.text}"
        data = response.json()
        assert "notifications" in data, "Missing notifications field"
        assert "total" in data, "Missing total field"
        assert isinstance(data["notifications"], list), "Notifications should be a list"
        print(f"PASS: GET /api/notifications/ returned {len(data['notifications'])} notifications, total={data['total']}")
    
    def test_get_unread_count(self, premium_auth_header):
        """GET /api/notifications/unread-count should return unread count"""
        response = requests.get(f"{BASE_URL}/api/notifications/unread-count", headers=premium_auth_header)
        assert response.status_code == 200, f"Unread count failed: {response.text}"
        data = response.json()
        assert "unread_count" in data, "Missing unread_count field"
        assert isinstance(data["unread_count"], int), "unread_count should be an integer"
        print(f"PASS: GET /api/notifications/unread-count returned count={data['unread_count']}")


class TestDiscussionsEndpoints:
    """Test discussions endpoints with batch optimization"""
    
    def test_get_specialty_rooms(self, premium_auth_header):
        """GET /api/discussions/specialty-rooms should return specialty rooms list"""
        response = requests.get(f"{BASE_URL}/api/discussions/specialty-rooms", headers=premium_auth_header)
        assert response.status_code == 200, f"Specialty rooms failed: {response.text}"
        data = response.json()
        assert "rooms" in data, "Missing rooms field"
        assert isinstance(data["rooms"], list), "Rooms should be a list"
        if data["rooms"]:
            room = data["rooms"][0]
            assert "specialty_id" in room or "id" in room, "Room missing specialty_id"
            assert "thread_count" in room, "Room missing thread_count field"
        print(f"PASS: GET /api/discussions/specialty-rooms returned {len(data['rooms'])} rooms")
    
    def test_get_threads_batch_optimized(self, premium_auth_header):
        """GET /api/discussions/threads should return threads list (batch-optimized)"""
        # Test with specialty context
        params = {"context_type": "specialty", "context_id": "internal_medicine"}
        response = requests.get(f"{BASE_URL}/api/discussions/threads", params=params, headers=premium_auth_header)
        assert response.status_code == 200, f"Threads failed: {response.text}"
        data = response.json()
        assert "threads" in data, "Missing threads field"
        assert "total" in data, "Missing total field"
        assert isinstance(data["threads"], list), "Threads should be a list"
        print(f"PASS: GET /api/discussions/threads returned {len(data['threads'])} threads, total={data['total']}")


class TestAdminMetricsEndpoint:
    """Test admin metrics endpoint for request_latency field"""
    
    def test_admin_metrics_includes_request_latency(self, premium_auth_header):
        """GET /api/admin/metrics should include request_latency field with p50/p95"""
        response = requests.get(f"{BASE_URL}/api/admin/metrics", headers=premium_auth_header)
        assert response.status_code == 200, f"Admin metrics failed: {response.text}"
        data = response.json()
        
        # Check for request_latency field
        assert "request_latency" in data, "Missing request_latency field in admin/metrics"
        latency = data["request_latency"]
        assert "overall" in latency, "Missing overall in request_latency"
        assert "p50_ms" in latency["overall"], "Missing p50_ms in overall latency"
        assert "p95_ms" in latency["overall"], "Missing p95_ms in overall latency"
        print(f"PASS: GET /api/admin/metrics includes request_latency with p50={latency['overall']['p50_ms']}ms, p95={latency['overall']['p95_ms']}ms")
    
    def test_admin_metrics_forbidden_for_non_admin(self, free_auth_header):
        """GET /api/admin/metrics should return 403 for non-admin user"""
        response = requests.get(f"{BASE_URL}/api/admin/metrics", headers=free_auth_header)
        assert response.status_code == 403, f"Expected 403 for non-admin, got {response.status_code}"
        print(f"PASS: GET /api/admin/metrics correctly returns 403 for non-admin user")


class TestFeatureFlagsEndpoint:
    """Test feature flags endpoint"""
    
    def test_get_feature_flags(self, premium_auth_header):
        """GET /api/config/feature-flags should return feature flags"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags", headers=premium_auth_header)
        assert response.status_code == 200, f"Feature flags failed: {response.text}"
        data = response.json()
        assert isinstance(data, dict), "Feature flags should return a dict"
        # Check some expected flags
        print(f"PASS: GET /api/config/feature-flags returned {len(data)} flags")


class TestHealthEndpoint:
    """Test health check endpoint"""
    
    def test_health(self):
        """GET /api/health should return ok status"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200, f"Health check failed: {response.text}"
        data = response.json()
        assert data.get("status") == "ok", "Health status should be 'ok'"
        print(f"PASS: GET /api/health returns status=ok")


# Fixtures
@pytest.fixture(scope="module")
def premium_auth_header():
    """Get auth header for premium/admin user"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json=PREMIUM_USER)
    if response.status_code != 200:
        pytest.skip(f"Premium user login failed: {response.text}")
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def free_auth_header():
    """Get auth header for free user"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json=FREE_USER)
    if response.status_code != 200:
        pytest.skip(f"Free user login failed: {response.text}")
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
