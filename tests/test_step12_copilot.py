"""
Step 12: Copilot MVP Tests
Tests for 4 copilot endpoints:
- POST /api/copilot/evidence-brief (premium) -> evidence_brief + citations + disclaimer
- POST /api/copilot/ask-article (premium) -> answer + confidence + citations, PHI block
- POST /api/copilot/compare-studies (premium) -> table + synthesis + citations
- POST /api/copilot/draft-discussion-post (premium + verified) -> draft + suggested_questions

COPILOT_PROVIDER=mock returns deterministic JSON for testing.
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}  # premium + verified
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}     # free + unverified

# Test data
TEST_PMID = "12345678"


@pytest.fixture(scope="module")
def premium_token():
    """Get token for premium + verified user."""
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
# /api/auth/me - Capabilities Check
# ============================================================

class TestCopilotCapabilities:
    """Test copilot capabilities returned in /api/auth/me."""

    def test_premium_user_has_copilot_capabilities(self, premium_token):
        """Premium user should have copilot_evidence_brief=true, copilot_calls_per_24h=50."""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        caps = data.get("capabilities", {})
        
        assert caps.get("copilot_evidence_brief") is True, "copilot_evidence_brief should be true"
        assert caps.get("copilot_ask_article") is True, "copilot_ask_article should be true"
        assert caps.get("copilot_compare_studies") is True, "copilot_compare_studies should be true"
        assert caps.get("copilot_draft_post") is True, "copilot_draft_post should be true"
        assert caps.get("copilot_calls_per_24h") == 50, "copilot_calls_per_24h should be 50"
        print("PASS: Premium user has correct copilot capabilities")

    def test_free_user_no_copilot_capabilities(self, free_token):
        """Free user should have copilot_evidence_brief=false, copilot_calls_per_24h=0."""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        caps = data.get("capabilities", {})
        
        assert caps.get("copilot_evidence_brief") is False, "copilot_evidence_brief should be false"
        assert caps.get("copilot_ask_article") is False, "copilot_ask_article should be false"
        assert caps.get("copilot_calls_per_24h") == 0, "copilot_calls_per_24h should be 0"
        print("PASS: Free user has no copilot capabilities")


# ============================================================
# POST /api/copilot/evidence-brief
# ============================================================

class TestEvidenceBrief:
    """Tests for POST /api/copilot/evidence-brief endpoint."""

    def test_evidence_brief_premium_success(self, premium_token):
        """Premium user gets evidence brief with citations and disclaimer."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": TEST_PMID},
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Verify structure (mock returns evidence_brief object)
        assert "evidence_brief" in data or "title" in data, "Should have evidence_brief or title"
        assert "citations" in data, "Should have citations"
        assert "disclaimer" in data, "Should have disclaimer"
        assert isinstance(data["citations"], list), "Citations should be a list"
        print(f"PASS: Evidence brief returned with disclaimer: {data.get('disclaimer', '')[:50]}")

    def test_evidence_brief_free_user_403(self, free_token):
        """Free user should get 403 premium_required."""
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
        assert detail.get("error_code") == "premium_required", "Should return premium_required error code"
        print("PASS: Free user correctly blocked from evidence-brief")

    def test_evidence_brief_article_not_found(self, premium_token):
        """Non-existent article returns 404."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": "99999999"},
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 404, f"Expected 404, got {response.status_code}: {response.text}"
        print("PASS: Non-existent article returns 404")

    def test_evidence_brief_no_auth_401(self):
        """Unauthenticated request returns 401."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            json={"pmid": TEST_PMID},
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code in [401, 403], f"Expected 401/403, got {response.status_code}"
        print("PASS: Unauthenticated request blocked")


# ============================================================
# POST /api/copilot/ask-article
# ============================================================

class TestAskArticle:
    """Tests for POST /api/copilot/ask-article endpoint."""

    def test_ask_article_clean_question_success(self, premium_token):
        """Premium user can ask clean question about article."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={
                "pmid": TEST_PMID,
                "question": "What was the primary endpoint?"
            },
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        assert "answer" in data, "Should have answer"
        assert "confidence" in data, "Should have confidence"
        assert "citations" in data, "Should have citations"
        assert "disclaimer" in data, "Should have disclaimer"
        print(f"PASS: Ask-article returned answer with confidence: {data.get('confidence')}")

    def test_ask_article_phi_question_422(self, premium_token):
        """PHI-containing question should be blocked with 422."""
        # Common PHI patterns: patient names, medical record numbers, specific dates with patient info
        phi_question = "My patient John Smith MRN 123456 had this medication, what do you think?"
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={
                "pmid": TEST_PMID,
                "question": phi_question
            },
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 422, f"Expected 422 for PHI, got {response.status_code}: {response.text}"
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected", f"Should return phi_detected error code: {detail}"
        print("PASS: PHI question correctly blocked with 422")

    def test_ask_article_free_user_403(self, free_token):
        """Free user should get 403 premium_required."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            json={
                "pmid": TEST_PMID,
                "question": "What was the primary endpoint?"
            },
            headers={
                "Authorization": f"Bearer {free_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("PASS: Free user correctly blocked from ask-article")


# ============================================================
# POST /api/copilot/compare-studies
# ============================================================

class TestCompareStudies:
    """Tests for POST /api/copilot/compare-studies endpoint."""

    def test_compare_studies_two_pmids_success(self, premium_token):
        """Premium user can compare 2 articles."""
        # Need to find two valid PMIDs - try with TEST_PMID and another
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={
                "pmids": [TEST_PMID, TEST_PMID]  # Using same PMID twice for test
            },
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Mock returns table, synthesis, citations
        assert "table" in data or "comparison_title" in data, "Should have table or comparison_title"
        assert "citations" in data, "Should have citations"
        assert "disclaimer" in data, "Should have disclaimer"
        print("PASS: Compare-studies returned comparison result")

    def test_compare_studies_one_pmid_400(self, premium_token):
        """Sending only 1 PMID should return 400 (need at least 2)."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={
                "pmids": [TEST_PMID]  # Only 1 PMID
            },
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        # This should fail validation (min_length=2 on pmids field)
        assert response.status_code in [400, 422], f"Expected 400/422, got {response.status_code}: {response.text}"
        print("PASS: Single PMID correctly rejected")

    def test_compare_studies_free_user_403(self, free_token):
        """Free user should get 403 premium_required."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            json={
                "pmids": [TEST_PMID, TEST_PMID]
            },
            headers={
                "Authorization": f"Bearer {free_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("PASS: Free user correctly blocked from compare-studies")


# ============================================================
# POST /api/copilot/draft-discussion-post
# ============================================================

class TestDraftDiscussionPost:
    """Tests for POST /api/copilot/draft-discussion-post endpoint."""

    def test_draft_post_premium_verified_success(self, premium_token):
        """Premium + verified user can draft discussion post."""
        response = requests.post(
            f"{BASE_URL}/api/copilot/draft-discussion-post",
            json={
                "specialty_id": "cardiology",
                "pmids": [TEST_PMID],
                "tone": "neutral"
            },
            headers={
                "Authorization": f"Bearer {premium_token}",
                "Content-Type": "application/json"
            }
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Mock returns draft_post, suggested_questions, citations
        assert "draft_post" in data, "Should have draft_post"
        assert "suggested_questions" in data, "Should have suggested_questions"
        assert "citations" in data, "Should have citations"
        assert "disclaimer" in data, "Should have disclaimer"
        print(f"PASS: Draft post returned with {len(data.get('suggested_questions', []))} suggested questions")

    def test_draft_post_unverified_403(self, free_token):
        """Unverified user (even if premium) should get 403 verification_required."""
        # free_token user is free + pending verification
        response = requests.post(
            f"{BASE_URL}/api/copilot/draft-discussion-post",
            json={
                "specialty_id": "cardiology",
                "pmids": [TEST_PMID],
                "tone": "neutral"
            },
            headers={
                "Authorization": f"Bearer {free_token}",
                "Content-Type": "application/json"
            }
        )
        # Should get 403 - either premium_required (checked first) or verification_required
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        data = response.json()
        detail = data.get("detail", {})
        # Free user will hit premium_required first
        assert detail.get("error_code") in ["premium_required", "verification_required"], \
            f"Expected premium_required or verification_required: {detail}"
        print(f"PASS: Unverified/free user blocked with {detail.get('error_code')}")


# ============================================================
# Regression Tests
# ============================================================

class TestRegressions:
    """Ensure other features still work."""

    def test_auth_me_works(self, premium_token):
        """Auth/me endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        print("PASS: /api/auth/me works")

    def test_articles_detail_works(self, premium_token):
        """Articles detail endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/articles/{TEST_PMID}",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        print("PASS: /api/articles/{pmid} works")

    def test_discussions_specialty_rooms(self, premium_token):
        """Community specialty rooms endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/discussions/specialty-rooms",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        print("PASS: /api/discussions/specialty-rooms works")

    def test_billing_me_works(self, premium_token):
        """Billing status endpoint works."""
        response = requests.get(
            f"{BASE_URL}/api/billing/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        print("PASS: /api/billing/me works")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
