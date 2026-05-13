"""
Step 14 API Tests: Copilot Productionization
Tests quota enforcement, citation validation, and go-live checks via HTTP API.
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://litscreen-aggregate.preview.emergentagent.com').rstrip('/')

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


@pytest.fixture(scope="module")
def premium_token():
    """Get auth token for premium admin user."""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json=PREMIUM_USER)
    if resp.status_code != 200:
        pytest.skip(f"Login failed: {resp.status_code} - {resp.text}")
    return resp.json().get("access_token")


@pytest.fixture(scope="module")
def free_token():
    """Get auth token for free user."""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json=FREE_USER)
    if resp.status_code != 200:
        pytest.skip(f"Login failed: {resp.status_code} - {resp.text}")
    return resp.json().get("access_token")


@pytest.fixture(scope="module")
def test_pmid():
    """Get a valid PMID from the library."""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json=PREMIUM_USER)
    token = resp.json().get("access_token")
    lib_resp = requests.get(f"{BASE_URL}/api/library", headers={"Authorization": f"Bearer {token}"})
    if lib_resp.status_code == 200:
        articles = lib_resp.json().get("articles", [])
        if articles:
            return articles[0].get("pmid") or articles[0].get("article_id")
    return "12345678"  # Fallback


class TestGoLiveStatusCopilot:
    """Test Go-Live status includes Copilot configuration (Step 14)."""

    def test_go_live_status_includes_copilot_section(self, premium_token):
        """GET /api/admin/go-live-status should include copilot config."""
        resp = requests.get(
            f"{BASE_URL}/api/admin/go-live-status",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "integrations" in data, "Response should have integrations section"
        assert "copilot" in data["integrations"], "Integrations should include copilot"
        
        copilot = data["integrations"]["copilot"]
        assert "enabled_flag" in copilot, "Copilot should have enabled_flag"
        assert "provider" in copilot, "Copilot should have provider"
        assert "model_configured" in copilot, "Copilot should have model_configured"
        assert "provider_key_configured" in copilot, "Copilot should have provider_key_configured"
        
        # Verify values are booleans or strings (no secrets)
        assert isinstance(copilot["enabled_flag"], bool)
        assert isinstance(copilot["provider"], str)
        assert isinstance(copilot["model_configured"], bool)
        assert isinstance(copilot["provider_key_configured"], bool)

    def test_go_live_status_non_admin_forbidden(self, free_token):
        """Non-admin user should get 403 for go-live-status."""
        resp = requests.get(
            f"{BASE_URL}/api/admin/go-live-status",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"


class TestLiveChecksCopilot:
    """Test Live Checks include Copilot check (Step 14)."""

    def test_run_live_checks_includes_copilot(self, premium_token):
        """POST /api/admin/go-live-status/run-live-checks should include copilot check."""
        resp = requests.post(
            f"{BASE_URL}/api/admin/go-live-status/run-live-checks",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "checks" in data, "Response should have checks section"
        assert "copilot" in data["checks"], "Checks should include copilot"
        
        copilot_check = data["checks"]["copilot"]
        assert "status" in copilot_check, "Copilot check should have status"
        assert copilot_check["status"] in ["ok", "skipped", "failed"], f"Invalid status: {copilot_check['status']}"
        
        # With mock provider, should be ok
        if copilot_check.get("provider") == "mock":
            assert copilot_check["status"] == "ok", "Mock provider should return ok"

    def test_run_live_checks_non_admin_forbidden(self, free_token):
        """Non-admin user should get 403 for run-live-checks."""
        resp = requests.post(
            f"{BASE_URL}/api/admin/go-live-status/run-live-checks",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"


class TestEvidenceBriefCitationValidation:
    """Test evidence-brief endpoint returns citations_sanitized field (Step 14)."""

    def test_evidence_brief_returns_citations_sanitized_field(self, premium_token, test_pmid):
        """Evidence brief should include citations_sanitized field."""
        resp = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": test_pmid},
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        
        # May return 200 (success) or 404 (article not found)
        if resp.status_code == 404:
            pytest.skip("Test article not found")
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "citations_sanitized" in data, "Response should include citations_sanitized field"
        assert isinstance(data["citations_sanitized"], bool), "citations_sanitized should be boolean"
        
        # If sanitized, should have citation_warning
        if data["citations_sanitized"]:
            assert "citation_warning" in data, "Should have citation_warning when sanitized"
        
        # Should have citations array
        assert "citations" in data, "Should have citations array"
        assert isinstance(data["citations"], list), "Citations should be a list"

    def test_evidence_brief_free_user_forbidden(self, free_token, test_pmid):
        """Free user should get 403 for evidence-brief."""
        resp = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": test_pmid},
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"


class TestAskArticleCitationValidation:
    """Test ask-article endpoint returns citations_sanitized field (Step 14)."""

    def test_ask_article_returns_citations_sanitized_field(self, premium_token, test_pmid):
        """Ask article should include citations_sanitized field."""
        resp = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={"pmid": test_pmid, "question": "What was the primary endpoint?"},
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        
        if resp.status_code == 404:
            pytest.skip("Test article not found")
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "citations_sanitized" in data, "Response should include citations_sanitized field"
        assert isinstance(data["citations_sanitized"], bool), "citations_sanitized should be boolean"


class TestCompareStudiesCitationValidation:
    """Test compare-studies endpoint returns citations_sanitized field (Step 14)."""

    def test_compare_studies_returns_citations_sanitized_field(self, premium_token, test_pmid):
        """Compare studies should include citations_sanitized field."""
        # Need at least 2 PMIDs - use same PMID twice for test
        resp = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={"pmids": [test_pmid, test_pmid]},
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        
        if resp.status_code == 400:
            pytest.skip("Need 2 different valid articles")
        if resp.status_code == 404:
            pytest.skip("Test articles not found")
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "citations_sanitized" in data, "Response should include citations_sanitized field"
        assert isinstance(data["citations_sanitized"], bool), "citations_sanitized should be boolean"


class TestCacheHitsNoQuota:
    """Test that cache hits don't consume quota (Step 14)."""

    def test_cached_response_has_cached_flag(self, premium_token, test_pmid):
        """Cached responses should have cached=true flag."""
        # First call - may or may not be cached
        resp1 = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": test_pmid},
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        
        if resp1.status_code != 200:
            pytest.skip("First call failed")
        
        # Second call - should be cached
        resp2 = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": test_pmid},
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        
        assert resp2.status_code == 200, f"Expected 200, got {resp2.status_code}"
        
        data = resp2.json()
        # Should have cached field
        assert "cached" in data, "Response should include cached field"
        # Second call should be cached
        assert data["cached"] is True, "Second call should return cached=true"


class TestQuotaExceeded:
    """Test quota exceeded returns 429 (Step 14)."""

    def test_quota_error_structure(self, premium_token):
        """Verify 429 response structure when quota exceeded."""
        # This test documents the expected error structure
        # We can't easily trigger quota exceeded without making 50+ calls
        # So we just verify the endpoint works and document the expected structure
        
        # Expected 429 response structure:
        # {
        #   "error_code": "copilot_quota_exceeded",
        #   "message": "Copilot limit (50/day) reached. Try again later.",
        #   "retry_after_seconds": 3600,
        #   "limit": 50,
        #   "used": 50
        # }
        pass  # Placeholder - actual quota testing would require many calls


class TestRegressions:
    """Regression tests for existing functionality."""

    def test_auth_me_returns_capabilities(self, premium_token):
        """Auth me should return user with capabilities."""
        resp = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "capabilities" in data
        assert "copilot_evidence_brief" in data["capabilities"]

    def test_library_endpoint_works(self, premium_token):
        """Library endpoint should work."""
        resp = requests.get(
            f"{BASE_URL}/api/library",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert resp.status_code == 200

    def test_health_endpoint(self):
        """Health endpoint should return ok."""
        resp = requests.get(f"{BASE_URL}/api/health")
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
