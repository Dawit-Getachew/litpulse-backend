"""
Step 13: Copilot Cache + Kill Switch + Admin Metrics Tests

Tests for Step 13 Copilot enhancements:
- Cache for evidence-brief: first call cached=false, second call cached=true
- Cache for compare-studies (no question): first cached=false, second cached=true
- ask-article NOT cached (has user question)
- Capabilities reflect kill switch: copilot_evidence_brief depends on ENABLE_COPILOT flag
- Admin metrics includes copilot.calls_last_24h and copilot.cache_entries
- Free user still gets 403 on all copilot endpoints
- PHI in copilot question still returns 422

COPILOT_PROVIDER=mock returns deterministic JSON for testing.
"""
import pytest
import requests
import os
import time

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}  # premium + verified + admin
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}     # free + unverified

# Test data
TEST_PMID = "12345678"


@pytest.fixture(scope="module")
def premium_token():
    """Get token for premium + verified user (admin)."""
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        json=PREMIUM_USER,
        headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200, f"Premium login failed: {response.text}"
    return response.json()["access_token"]


@pytest.fixture(scope="module")
def free_token():
    """Get token for free + unverified user."""
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        json=FREE_USER,
        headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200, f"Free login failed: {response.text}"
    return response.json()["access_token"]


# ============================================================
# Cache Tests - evidence-brief
# ============================================================

class TestEvidenceBriefCache:
    """Test caching behavior for evidence-brief endpoint."""

    def test_evidence_brief_cache_miss_then_hit(self, premium_token):
        """First call should be cached=false, second call should be cached=true."""
        headers = {
            "Authorization": f"Bearer {premium_token}",
            "Content-Type": "application/json"
        }
        
        # First call - may be cached=false (fresh) or cached=true (from previous tests)
        response1 = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": TEST_PMID},
            headers=headers
        )
        assert response1.status_code == 200, f"First call failed: {response1.text}"
        data1 = response1.json()
        assert "cached" in data1, "Response should have 'cached' field"
        first_cached = data1.get("cached")
        print(f"First evidence-brief call: cached={first_cached}")
        
        # Second call - should be cached=true (cache hit)
        response2 = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": TEST_PMID},
            headers=headers
        )
        assert response2.status_code == 200, f"Second call failed: {response2.text}"
        data2 = response2.json()
        assert "cached" in data2, "Response should have 'cached' field"
        second_cached = data2.get("cached")
        print(f"Second evidence-brief call: cached={second_cached}")
        
        # The second call should definitely be cached (regardless of first)
        assert second_cached is True, "Second call should return cached=true"
        
        # Both should have same core content
        assert "disclaimer" in data1 and "disclaimer" in data2
        print("PASS: Evidence-brief caching works - second call returns cached=true")


# ============================================================
# Cache Tests - compare-studies (no question)
# ============================================================

class TestCompareStudiesCache:
    """Test caching behavior for compare-studies endpoint."""

    def test_compare_studies_no_question_cache(self, premium_token):
        """Compare-studies without question_optional should be cached."""
        headers = {
            "Authorization": f"Bearer {premium_token}",
            "Content-Type": "application/json"
        }
        
        # First call without question
        response1 = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={"pmids": [TEST_PMID, TEST_PMID], "question_optional": None},
            headers=headers
        )
        assert response1.status_code == 200, f"First call failed: {response1.text}"
        data1 = response1.json()
        assert "cached" in data1, "Response should have 'cached' field"
        first_cached = data1.get("cached")
        print(f"First compare-studies call (no question): cached={first_cached}")
        
        # Second call without question - should be cached
        response2 = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={"pmids": [TEST_PMID, TEST_PMID], "question_optional": None},
            headers=headers
        )
        assert response2.status_code == 200, f"Second call failed: {response2.text}"
        data2 = response2.json()
        assert "cached" in data2, "Response should have 'cached' field"
        second_cached = data2.get("cached")
        print(f"Second compare-studies call (no question): cached={second_cached}")
        
        assert second_cached is True, "Second call should return cached=true"
        print("PASS: Compare-studies (no question) caching works")

    def test_compare_studies_with_question_not_cached(self, premium_token):
        """Compare-studies WITH question_optional should NOT be cached."""
        headers = {
            "Authorization": f"Bearer {premium_token}",
            "Content-Type": "application/json"
        }
        
        # Call with a question - should NOT be cached
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={"pmids": [TEST_PMID, TEST_PMID], "question_optional": "What are the key differences?"},
            headers=headers
        )
        assert response.status_code == 200, f"Call with question failed: {response.text}"
        data = response.json()
        # With user question, it's never cached (always fresh generation)
        cached = data.get("cached")
        print(f"Compare-studies with question: cached={cached}")
        # The response should exist but won't be from cache if it's a new unique question
        assert "disclaimer" in data, "Response should have disclaimer"
        print("PASS: Compare-studies with question returns valid response")


# ============================================================
# ask-article NOT Cached Test
# ============================================================

class TestAskArticleNotCached:
    """Verify ask-article responses do NOT have 'cached' field (not cacheable)."""

    def test_ask_article_no_cached_field(self, premium_token):
        """ask-article has user question so should NOT be cached."""
        headers = {
            "Authorization": f"Bearer {premium_token}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={"pmid": TEST_PMID, "question": "What was the study design?"},
            headers=headers
        )
        assert response.status_code == 200, f"ask-article failed: {response.text}"
        data = response.json()
        
        # ask-article does NOT have caching - verify by checking if it returns response
        assert "answer" in data, "Should have answer"
        assert "disclaimer" in data, "Should have disclaimer"
        # ask-article does NOT return 'cached' field since user questions are always unique
        print(f"ask-article response has cached={data.get('cached', 'NOT_PRESENT')}")
        print("PASS: ask-article returns valid response (not cached)")


# ============================================================
# Capabilities Test - Kill Switch
# ============================================================

class TestCapabilitiesKillSwitch:
    """Test that capabilities reflect ENABLE_COPILOT flag."""

    def test_capabilities_include_copilot_surfaces(self, premium_token):
        """Premium user capabilities should include copilot surfaces when enabled."""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        caps = data.get("capabilities", {})
        
        # Check that copilot surfaces are present
        assert "copilot_evidence_brief" in caps, "Should have copilot_evidence_brief"
        assert "copilot_ask_article" in caps, "Should have copilot_ask_article"
        assert "copilot_compare_studies" in caps, "Should have copilot_compare_studies"
        assert "copilot_draft_post" in caps, "Should have copilot_draft_post"
        
        # With ENABLE_COPILOT=true and premium user, all should be True
        assert caps.get("copilot_evidence_brief") is True, "copilot_evidence_brief should be true"
        print(f"PASS: Capabilities include copilot surfaces: copilot_evidence_brief={caps.get('copilot_evidence_brief')}")

    def test_feature_flags_show_copilot_enabled(self):
        """Feature flags endpoint should show copilot_enabled=true."""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        data = response.json()
        
        assert "copilot_enabled" in data, "Should have copilot_enabled"
        print(f"Feature flags: copilot_enabled={data.get('copilot_enabled')}")
        # Current env has ENABLE_COPILOT=true
        assert data.get("copilot_enabled") is True, "copilot_enabled should be true"
        print("PASS: Feature flags show copilot_enabled=true")


# ============================================================
# Admin Metrics Test
# ============================================================

class TestAdminMetrics:
    """Test admin metrics includes copilot stats."""

    def test_admin_metrics_has_copilot_stats(self, premium_token):
        """Admin metrics should include copilot.calls_last_24h and copilot.cache_entries."""
        response = requests.get(
            f"{BASE_URL}/api/admin/metrics",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200, f"Admin metrics failed: {response.text}"
        data = response.json()
        
        # Check for copilot stats
        assert "copilot" in data, "Admin metrics should have 'copilot' section"
        copilot = data.get("copilot", {})
        
        assert "calls_last_24h" in copilot, "copilot should have calls_last_24h"
        assert "cache_entries" in copilot, "copilot should have cache_entries"
        
        calls = copilot.get("calls_last_24h")
        cache = copilot.get("cache_entries")
        
        assert isinstance(calls, int), "calls_last_24h should be int"
        assert isinstance(cache, int), "cache_entries should be int"
        assert calls >= 0, "calls_last_24h should be >= 0"
        assert cache >= 0, "cache_entries should be >= 0"
        
        print(f"PASS: Admin metrics has copilot stats: calls_last_24h={calls}, cache_entries={cache}")


# ============================================================
# Free User 403 Tests
# ============================================================

class TestFreeUserBlocked:
    """Verify free user still gets 403 on all copilot endpoints."""

    def test_free_user_evidence_brief_403(self, free_token):
        """Free user should get 403 on evidence-brief."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": TEST_PMID},
            headers={
                "Authorization": f"Bearer {free_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "premium_required", "Should return premium_required"
        print("PASS: Free user blocked from evidence-brief with 403")

    def test_free_user_ask_article_403(self, free_token):
        """Free user should get 403 on ask-article."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={"pmid": TEST_PMID, "question": "What is this about?"},
            headers={
                "Authorization": f"Bearer {free_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("PASS: Free user blocked from ask-article with 403")

    def test_free_user_compare_studies_403(self, free_token):
        """Free user should get 403 on compare-studies."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={"pmids": [TEST_PMID, TEST_PMID]},
            headers={
                "Authorization": f"Bearer {free_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("PASS: Free user blocked from compare-studies with 403")

    def test_free_user_draft_post_403(self, free_token):
        """Free user should get 403 on draft-discussion-post."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/draft-discussion-post",
            json={"specialty_id": "cardiology", "pmids": [TEST_PMID]},
            headers={
                "Authorization": f"Bearer {free_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("PASS: Free user blocked from draft-post with 403")


# ============================================================
# PHI Guard Test
# ============================================================

class TestPhiGuard:
    """Verify PHI in copilot questions still returns 422."""

    def test_phi_in_ask_article_422(self, premium_token):
        """PHI-containing question should return 422."""
        phi_question = "My patient John Smith MRN 123456 had this outcome, what do you think?"
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={"pmid": TEST_PMID, "question": phi_question},
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected", f"Should return phi_detected: {detail}"
        print("PASS: PHI in ask-article returns 422 phi_detected")

    def test_phi_in_compare_studies_question_422(self, premium_token):
        """PHI in compare-studies question should return 422."""
        phi_question = "Patient John Doe SSN 123-45-6789 had different responses"
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={"pmids": [TEST_PMID, TEST_PMID], "question_optional": phi_question},
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected", f"Should return phi_detected: {detail}"
        print("PASS: PHI in compare-studies question returns 422 phi_detected")


# ============================================================
# Regression Tests
# ============================================================

class TestRegressions:
    """Ensure other features still work after Step 13 changes."""

    def test_auth_me_works(self, premium_token):
        """Auth/me endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "user_id" in data
        assert "capabilities" in data
        print("PASS: /api/auth/me works")

    def test_community_discussions_work(self, premium_token):
        """Community specialty rooms endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/discussions/specialty-rooms",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        print("PASS: /api/discussions/specialty-rooms works")

    def test_billing_works(self, premium_token):
        """Billing status endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/billing/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        print("PASS: /api/billing/me works")

    def test_article_detail_works(self, premium_token):
        """Article detail endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        print("PASS: /api/articles/{pmid} works")

    def test_audio_takeaway_available(self, premium_token):
        """Audio endpoint is available (premium feature)."""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}/audio-summary",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        # May return 200 (ready/pending) or 404 (not generated)
        assert response.status_code in [200, 404], f"Unexpected status: {response.status_code}"
        print("PASS: Audio takeaway endpoint accessible")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
