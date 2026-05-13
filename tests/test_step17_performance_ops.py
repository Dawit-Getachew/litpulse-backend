"""
Step 17: P1 Performance Polish + Ops Visibility Tests

Tests for:
1. Library pagination with backward-compatible limit/cursor params
2. Admin metrics parallelization (asyncio.gather) — returns request_latency
3. Admin slow-query viewer with ring buffer endpoint
4. Slow-query event schema PHI-Zero compliance
"""

import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


@pytest.fixture(scope="module")
def admin_token():
    """Get admin auth token"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json=ADMIN_USER, timeout=30)
    if resp.status_code != 200:
        pytest.skip(f"Admin login failed: {resp.status_code} - {resp.text}")
    return resp.json().get("access_token")


@pytest.fixture(scope="module")
def free_token():
    """Get free user auth token"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json=FREE_USER, timeout=30)
    if resp.status_code != 200:
        pytest.skip(f"Free user login failed: {resp.status_code} - {resp.text}")
    return resp.json().get("access_token")


class TestLibraryPagination:
    """Tests for GET /api/library pagination with backward compatibility"""

    def test_library_no_params_backward_compat(self, admin_token):
        """GET /api/library (no params) — returns articles array, total count, next_cursor=null"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = requests.get(f"{BASE_URL}/api/library", headers=headers, timeout=30)
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        # Verify backward-compatible response structure
        assert "articles" in data, "Response must have 'articles' field"
        assert isinstance(data["articles"], list), "'articles' must be a list"
        assert "total" in data, "Response must have 'total' field"
        assert isinstance(data["total"], int), "'total' must be an integer"
        assert "next_cursor" in data, "Response must have 'next_cursor' field (for backward compat)"
        
        print(f"Library returned {len(data['articles'])} articles, total={data['total']}, next_cursor={data['next_cursor']}")

    def test_library_with_limit_1(self, admin_token):
        """GET /api/library?limit=1 — returns at most 1 article, correct total, next_cursor if more exist"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = requests.get(f"{BASE_URL}/api/library?limit=1", headers=headers, timeout=30)
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        assert "articles" in data
        assert len(data["articles"]) <= 1, f"Expected at most 1 article, got {len(data['articles'])}"
        assert "total" in data
        assert isinstance(data["total"], int)
        assert "next_cursor" in data
        
        # If total > 1, next_cursor should be non-null
        if data["total"] > 1 and len(data["articles"]) == 1:
            assert data["next_cursor"] is not None, "next_cursor should be set when more articles exist"
            print(f"next_cursor returned: {data['next_cursor']}")
        
        print(f"Limit=1: got {len(data['articles'])} articles, total={data['total']}")

    def test_library_pagination_no_duplicates(self, admin_token):
        """Pagination: if next_cursor returned, fetching with cursor=<next_cursor> returns next page without duplicates"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        
        # First page
        resp1 = requests.get(f"{BASE_URL}/api/library?limit=2", headers=headers, timeout=30)
        assert resp1.status_code == 200
        data1 = resp1.json()
        
        if data1["total"] <= 2 or data1["next_cursor"] is None:
            pytest.skip("Not enough articles to test pagination")
        
        page1_pmids = set(a.get("pmid") for a in data1["articles"] if a.get("pmid"))
        cursor = data1["next_cursor"]
        
        # Second page
        resp2 = requests.get(f"{BASE_URL}/api/library?limit=2&cursor={cursor}", headers=headers, timeout=30)
        assert resp2.status_code == 200
        data2 = resp2.json()
        
        page2_pmids = set(a.get("pmid") for a in data2["articles"] if a.get("pmid"))
        
        # Check no overlap
        overlap = page1_pmids.intersection(page2_pmids)
        assert len(overlap) == 0, f"Duplicate articles found across pages: {overlap}"
        
        print(f"Page 1 PMIDs: {page1_pmids}")
        print(f"Page 2 PMIDs: {page2_pmids}")
        print("No duplicates - pagination working correctly")


class TestAdminMetrics:
    """Tests for GET /api/admin/metrics (parallelized counts)"""

    def test_admin_metrics_has_request_latency(self, admin_token):
        """GET /api/admin/metrics — returns full metrics with request_latency field"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = requests.get(f"{BASE_URL}/api/admin/metrics", headers=headers, timeout=60)
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        # Verify metrics structure
        assert "users" in data, "Missing 'users' metrics"
        assert "digests_24h" in data, "Missing 'digests_24h' metrics"
        assert "articles" in data, "Missing 'articles' metrics"
        assert "feedback" in data, "Missing 'feedback' metrics"
        assert "request_latency" in data, "Missing 'request_latency' field (Step 17 feature)"
        
        # Verify request_latency structure
        latency = data["request_latency"]
        assert "total_tracked" in latency, "request_latency must have 'total_tracked'"
        assert "overall" in latency, "request_latency must have 'overall'"
        assert "by_route" in latency, "request_latency must have 'by_route'"
        
        # Overall stats
        overall = latency["overall"]
        assert "p50_ms" in overall, "overall must have 'p50_ms'"
        assert "p95_ms" in overall, "overall must have 'p95_ms'"
        
        print(f"request_latency: total_tracked={latency['total_tracked']}")
        print(f"Overall: p50={overall.get('p50_ms')}ms, p95={overall.get('p95_ms')}ms")
        print(f"Counts: users={data['users']['total']}, articles={data['articles']['total_indexed']}")

    def test_admin_metrics_all_counts_populated(self, admin_token):
        """Verify all count fields are populated (parallelized asyncio.gather)"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = requests.get(f"{BASE_URL}/api/admin/metrics", headers=headers, timeout=60)
        
        assert resp.status_code == 200
        data = resp.json()
        
        # Check all sections have integer counts
        assert isinstance(data["users"]["total"], int), "users.total must be int"
        assert isinstance(data["users"]["verified"], int), "users.verified must be int"
        assert isinstance(data["preferences"]["active"], int), "preferences.active must be int"
        assert isinstance(data["digests_24h"]["total"], int), "digests_24h.total must be int"
        assert isinstance(data["articles"]["total_indexed"], int), "articles.total_indexed must be int"
        assert isinstance(data["feedback"]["useful"], int), "feedback.useful must be int"
        
        # Audio section
        if "audio" in data:
            assert isinstance(data["audio"]["ready_count"], int)
            assert isinstance(data["audio"]["pending_count"], int)
            assert isinstance(data["audio"]["failed_count"], int)
        
        # Billing section
        if "billing" in data:
            assert isinstance(data["billing"]["premium_users_count"], int)
        
        print("All count fields populated correctly")


class TestAdminSlowQueries:
    """Tests for GET /api/admin/slow-queries endpoint"""

    def test_slow_queries_admin_only(self, admin_token):
        """GET /api/admin/slow-queries — admin-only returns queries array and count"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = requests.get(f"{BASE_URL}/api/admin/slow-queries", headers=headers, timeout=30)
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        assert "queries" in data, "Response must have 'queries' field"
        assert isinstance(data["queries"], list), "'queries' must be a list"
        assert "count" in data, "Response must have 'count' field"
        assert isinstance(data["count"], int), "'count' must be an integer"
        assert data["count"] == len(data["queries"]), "count must match queries array length"
        
        print(f"Slow queries returned: count={data['count']}")
        if data["queries"]:
            print(f"Sample query: {data['queries'][0]}")

    def test_slow_queries_403_for_non_admin(self, free_token):
        """GET /api/admin/slow-queries — returns 403 for non-admin user"""
        headers = {"Authorization": f"Bearer {free_token}"}
        resp = requests.get(f"{BASE_URL}/api/admin/slow-queries", headers=headers, timeout=30)
        
        assert resp.status_code == 403, f"Expected 403 for non-admin, got {resp.status_code}: {resp.text}"
        print("Non-admin correctly denied access to slow-queries endpoint")


class TestSlowQueryEventSchema:
    """Tests for slow-query event schema (PHI-Zero compliance)"""

    def test_slow_query_event_keys_phi_zero(self):
        """Slow-query event schema: only has {timestamp, duration_ms, collection, operation, route}"""
        import sys
        sys.path.insert(0, '/app/backend')
        from utils.instrumentation import SLOW_QUERY_EVENT_KEYS
        
        expected_keys = {"timestamp", "duration_ms", "collection", "operation", "route"}
        assert SLOW_QUERY_EVENT_KEYS == expected_keys, (
            f"SLOW_QUERY_EVENT_KEYS mismatch: expected {expected_keys}, got {SLOW_QUERY_EVENT_KEYS}"
        )
        print(f"SLOW_QUERY_EVENT_KEYS verified: {SLOW_QUERY_EVENT_KEYS}")
        print("No query bodies or filters stored (PHI-Zero compliance)")

    def test_slow_query_events_have_only_allowed_keys(self, admin_token):
        """Each slow-query event only has allowed keys (no query bodies)"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        resp = requests.get(f"{BASE_URL}/api/admin/slow-queries?limit=50", headers=headers, timeout=30)
        
        assert resp.status_code == 200
        data = resp.json()
        
        allowed_keys = {"timestamp", "duration_ms", "collection", "operation", "route"}
        
        for i, event in enumerate(data["queries"]):
            event_keys = set(event.keys())
            extra_keys = event_keys - allowed_keys
            assert len(extra_keys) == 0, f"Event {i} has disallowed keys: {extra_keys}"
        
        if data["queries"]:
            print(f"Validated {len(data['queries'])} events - all have only PHI-Zero safe keys")
        else:
            print("No slow queries recorded (ring buffer empty) - this is expected if no queries exceeded 200ms")


class TestHealthCheck:
    """Basic health check"""

    def test_health_endpoint(self):
        """Health check endpoint works"""
        resp = requests.get(f"{BASE_URL}/api/health", timeout=10)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"
        print("Health check passed")
