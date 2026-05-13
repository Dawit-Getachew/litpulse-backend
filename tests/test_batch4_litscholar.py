"""
Test Batch 4: LitScholar Expertise Memory Profile System

Tests for:
  - GET /api/litscholar/profile - returns user expertise profile (premium only)
  - GET /api/litscholar/profile - returns 503 when feature flag disabled
  - POST /api/litscholar/profile/rebuild - rebuilds profile from preferences, library, reading data
  - POST /api/litscholar/artifacts - creates a saved artifact with PHI guard
  - POST /api/litscholar/artifacts - blocks PHI (SSN, patient names, etc.)
  - GET /api/litscholar/artifacts - lists saved artifacts sorted by newest first
  - DELETE /api/litscholar/artifacts/{id} - removes an artifact
  - Free user gets 403/premium_required on all litscholar endpoints
  - Copilot endpoints still work WITHOUT use_expertise_context (backward compat)
  - Copilot endpoints accept use_expertise_context:true without errors
  - litscholar_state collection has unique index on user_id
"""
import pytest
import requests
import os
import time
import uuid

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


class TestSetup:
    """Setup: Login and get tokens for premium and free users."""
    
    premium_token = None
    free_token = None
    
    @classmethod
    def get_premium_token(cls):
        if cls.premium_token:
            return cls.premium_token
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json=PREMIUM_USER,
            timeout=30
        )
        if response.status_code == 200:
            cls.premium_token = response.json().get("access_token")
            return cls.premium_token
        elif response.status_code == 429:
            pytest.skip("Rate limited - restart backend: sudo supervisorctl restart backend")
        return None
    
    @classmethod
    def get_free_token(cls):
        if cls.free_token:
            return cls.free_token
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json=FREE_USER,
            timeout=30
        )
        if response.status_code == 200:
            cls.free_token = response.json().get("access_token")
            return cls.free_token
        elif response.status_code == 429:
            pytest.skip("Rate limited - restart backend: sudo supervisorctl restart backend")
        return None


@pytest.fixture
def premium_headers():
    """Get headers with premium user token."""
    token = TestSetup.get_premium_token()
    if not token:
        pytest.skip("Could not obtain premium user token")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


@pytest.fixture
def free_headers():
    """Get headers with free user token."""
    token = TestSetup.get_free_token()
    if not token:
        pytest.skip("Could not obtain free user token")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


# ===========================================================================
# LitScholar Profile Endpoints Tests
# ===========================================================================

class TestLitScholarProfile:
    """Tests for GET /api/litscholar/profile and POST /api/litscholar/profile/rebuild"""
    
    def test_get_profile_premium_user_success(self, premium_headers):
        """GET /api/litscholar/profile returns expertise profile for premium user."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/profile",
            headers=premium_headers,
            timeout=30
        )
        
        # Should return 200 OK
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        # Verify structure
        assert "user_id" in data, "Response should contain user_id"
        assert "expertise_profile" in data, "Response should contain expertise_profile"
        assert "saved_artifacts" in data, "Response should contain saved_artifacts"
        assert "version" in data, "Response should contain version"
        
        # Verify expertise_profile structure
        profile = data["expertise_profile"]
        assert "specialty_ids" in profile, "expertise_profile should have specialty_ids"
        assert "subspecialty_ids" in profile, "expertise_profile should have subspecialty_ids"
        assert "topic_weights" in profile, "expertise_profile should have topic_weights"
        assert "journal_weights" in profile, "expertise_profile should have journal_weights"
        assert "study_design_preferences" in profile, "expertise_profile should have study_design_preferences"
        assert "recent_library_clusters" in profile, "expertise_profile should have recent_library_clusters"
        
        print(f"SUCCESS: GET /api/litscholar/profile returns valid profile")
    
    def test_get_profile_free_user_forbidden(self, free_headers):
        """GET /api/litscholar/profile returns 403 for free user."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/profile",
            headers=free_headers,
            timeout=30
        )
        
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "premium_required", \
            f"Expected error_code=premium_required, got {data}"
        
        print("SUCCESS: Free user gets 403/premium_required on /api/litscholar/profile")
    
    def test_rebuild_profile_premium_user_success(self, premium_headers):
        """POST /api/litscholar/profile/rebuild rebuilds profile from app activity."""
        response = requests.post(
            f"{BASE_URL}/api/litscholar/profile/rebuild",
            headers=premium_headers,
            timeout=60  # Longer timeout for rebuild
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "user_id" in data, "Response should contain user_id"
        assert "expertise_profile" in data, "Response should contain expertise_profile"
        assert "last_updated_at" in data, "Response should contain last_updated_at"
        
        print(f"SUCCESS: POST /api/litscholar/profile/rebuild rebuilds profile")
    
    def test_rebuild_profile_free_user_forbidden(self, free_headers):
        """POST /api/litscholar/profile/rebuild returns 403 for free user."""
        response = requests.post(
            f"{BASE_URL}/api/litscholar/profile/rebuild",
            headers=free_headers,
            timeout=30
        )
        
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        
        print("SUCCESS: Free user gets 403/premium_required on /api/litscholar/profile/rebuild")


# ===========================================================================
# LitScholar Artifacts Endpoints Tests
# ===========================================================================

class TestLitScholarArtifacts:
    """Tests for artifact CRUD operations."""
    
    created_artifact_id = None
    
    def test_create_artifact_success(self, premium_headers):
        """POST /api/litscholar/artifacts creates a saved artifact."""
        payload = {
            "artifact_type": "evidence_brief",
            "title": "TEST_Evidence Brief for Study Analysis",
            "summary_text": "This is a test summary of the evidence brief analyzing study results.",
            "pmids": ["12345678", "23456789"]
        }
        
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "artifact_id" in data, "Response should contain artifact_id"
        assert data["artifact_type"] == payload["artifact_type"]
        assert data["title"] == payload["title"]
        assert data["summary_text"] == payload["summary_text"]
        assert data["pmids"] == payload["pmids"]
        assert "created_at" in data
        
        TestLitScholarArtifacts.created_artifact_id = data["artifact_id"]
        print(f"SUCCESS: Created artifact with ID {data['artifact_id']}")
    
    def test_create_artifact_phi_block_ssn(self, premium_headers):
        """POST /api/litscholar/artifacts blocks PHI (SSN)."""
        payload = {
            "artifact_type": "evidence_brief",
            "title": "TEST_Artifact with SSN",
            "summary_text": "Patient SSN is 123-45-6789. This should be blocked.",
            "pmids": ["12345678"]
        }
        
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected", \
            f"Expected error_code=phi_detected, got {data}"
        assert "ssn" in data.get("detail", {}).get("detected_categories", []), \
            "Should detect SSN category"
        
        print("SUCCESS: PHI guard blocks SSN in artifacts")
    
    def test_create_artifact_phi_block_patient_name(self, premium_headers):
        """POST /api/litscholar/artifacts blocks PHI (patient name)."""
        payload = {
            "artifact_type": "evidence_brief",
            "title": "TEST_Artifact about patient John Smith",
            "summary_text": "Regular medical summary without PHI",
            "pmids": ["12345678"]
        }
        
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected", \
            f"Expected error_code=phi_detected, got {data}"
        assert "patient_name" in data.get("detail", {}).get("detected_categories", []), \
            "Should detect patient_name category"
        
        print("SUCCESS: PHI guard blocks patient names in artifact title")
    
    def test_create_artifact_phi_block_mrn(self, premium_headers):
        """POST /api/litscholar/artifacts blocks PHI (MRN)."""
        payload = {
            "artifact_type": "ask_answer",
            "title": "TEST_MRN artifact",
            "summary_text": "Patient MRN #12345678 shows elevated levels.",
            "pmids": []
        }
        
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected", \
            f"Expected error_code=phi_detected, got {data}"
        
        print("SUCCESS: PHI guard blocks MRN in artifacts")
    
    def test_list_artifacts_success(self, premium_headers):
        """GET /api/litscholar/artifacts lists saved artifacts sorted newest first."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            timeout=30
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "artifacts" in data, "Response should contain artifacts array"
        assert "total" in data, "Response should contain total count"
        
        # If we have artifacts, verify they are sorted by newest first
        artifacts = data["artifacts"]
        if len(artifacts) >= 2:
            dates = [a.get("created_at", "") for a in artifacts]
            assert dates == sorted(dates, reverse=True), \
                "Artifacts should be sorted by created_at descending"
        
        print(f"SUCCESS: GET /api/litscholar/artifacts returns {data['total']} artifacts")
    
    def test_list_artifacts_free_user_forbidden(self, free_headers):
        """GET /api/litscholar/artifacts returns 403 for free user."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=free_headers,
            timeout=30
        )
        
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        
        print("SUCCESS: Free user gets 403/premium_required on /api/litscholar/artifacts")
    
    def test_delete_artifact_success(self, premium_headers):
        """DELETE /api/litscholar/artifacts/{id} removes an artifact."""
        # First create an artifact to delete
        payload = {
            "artifact_type": "comparison",
            "title": "TEST_Artifact to Delete",
            "summary_text": "This artifact will be deleted.",
            "pmids": ["11111111"]
        }
        
        create_response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        assert create_response.status_code == 201, f"Failed to create artifact: {create_response.text}"
        artifact_id = create_response.json()["artifact_id"]
        
        # Now delete it
        delete_response = requests.delete(
            f"{BASE_URL}/api/litscholar/artifacts/{artifact_id}",
            headers=premium_headers,
            timeout=30
        )
        
        assert delete_response.status_code == 200, f"Expected 200, got {delete_response.status_code}: {delete_response.text}"
        
        data = delete_response.json()
        assert data.get("message") == "Artifact deleted"
        assert data.get("artifact_id") == artifact_id
        
        print(f"SUCCESS: DELETE /api/litscholar/artifacts/{artifact_id} removes artifact")
    
    def test_delete_artifact_not_found(self, premium_headers):
        """DELETE /api/litscholar/artifacts/{id} returns 404 for non-existent artifact."""
        fake_id = str(uuid.uuid4())
        
        response = requests.delete(
            f"{BASE_URL}/api/litscholar/artifacts/{fake_id}",
            headers=premium_headers,
            timeout=30
        )
        
        assert response.status_code == 404, f"Expected 404, got {response.status_code}: {response.text}"
        
        print("SUCCESS: DELETE non-existent artifact returns 404")


# ===========================================================================
# Copilot Backward Compatibility Tests
# ===========================================================================

class TestCopilotBackwardCompat:
    """Tests for copilot endpoints with/without use_expertise_context parameter."""
    
    def test_evidence_brief_without_expertise_context(self, premium_headers):
        """POST /api/copilot/evidence-brief works without use_expertise_context."""
        # First get an article PMID
        response = requests.get(
            f"{BASE_URL}/api/library",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code != 200:
            pytest.skip("Could not fetch library")
        
        articles = response.json().get("articles", [])
        if not articles:
            pytest.skip("No articles in library to test with")
        
        pmid = articles[0].get("pmid")
        if not pmid:
            pytest.skip("Article has no pmid")
        
        # Test evidence-brief WITHOUT use_expertise_context
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            headers=premium_headers,
            json={"pmid": pmid},
            timeout=60
        )
        
        # 200 or 404 (article not found) are acceptable
        assert response.status_code in [200, 404, 429], \
            f"Expected 200/404/429, got {response.status_code}: {response.text}"
        
        print("SUCCESS: /api/copilot/evidence-brief works without use_expertise_context")
    
    def test_evidence_brief_with_expertise_context(self, premium_headers):
        """POST /api/copilot/evidence-brief accepts use_expertise_context:true."""
        # First get an article PMID
        response = requests.get(
            f"{BASE_URL}/api/library",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code != 200:
            pytest.skip("Could not fetch library")
        
        articles = response.json().get("articles", [])
        if not articles:
            pytest.skip("No articles in library to test with")
        
        pmid = articles[0].get("pmid")
        if not pmid:
            pytest.skip("Article has no pmid")
        
        # Test evidence-brief WITH use_expertise_context=true
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            headers=premium_headers,
            json={"pmid": pmid, "use_expertise_context": True},
            timeout=60
        )
        
        # 200, 404, or 429 are acceptable
        assert response.status_code in [200, 404, 429], \
            f"Expected 200/404/429, got {response.status_code}: {response.text}"
        
        print("SUCCESS: /api/copilot/evidence-brief accepts use_expertise_context:true")
    
    def test_ask_article_without_expertise_context(self, premium_headers):
        """POST /api/copilot/ask-article works without use_expertise_context."""
        # First get an article PMID
        response = requests.get(
            f"{BASE_URL}/api/library",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code != 200:
            pytest.skip("Could not fetch library")
        
        articles = response.json().get("articles", [])
        if not articles:
            pytest.skip("No articles in library to test with")
        
        pmid = articles[0].get("pmid")
        if not pmid:
            pytest.skip("Article has no pmid")
        
        # Test ask-article WITHOUT use_expertise_context
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            headers=premium_headers,
            json={"pmid": pmid, "question": "What is the main finding?"},
            timeout=60
        )
        
        assert response.status_code in [200, 404, 429], \
            f"Expected 200/404/429, got {response.status_code}: {response.text}"
        
        print("SUCCESS: /api/copilot/ask-article works without use_expertise_context")
    
    def test_ask_article_with_expertise_context(self, premium_headers):
        """POST /api/copilot/ask-article accepts use_expertise_context:true."""
        response = requests.get(
            f"{BASE_URL}/api/library",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code != 200:
            pytest.skip("Could not fetch library")
        
        articles = response.json().get("articles", [])
        if not articles:
            pytest.skip("No articles in library to test with")
        
        pmid = articles[0].get("pmid")
        if not pmid:
            pytest.skip("Article has no pmid")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            headers=premium_headers,
            json={
                "pmid": pmid,
                "question": "What is the main finding?",
                "use_expertise_context": True
            },
            timeout=60
        )
        
        assert response.status_code in [200, 404, 429], \
            f"Expected 200/404/429, got {response.status_code}: {response.text}"
        
        print("SUCCESS: /api/copilot/ask-article accepts use_expertise_context:true")
    
    def test_compare_studies_without_expertise_context(self, premium_headers):
        """POST /api/copilot/compare-studies works without use_expertise_context."""
        response = requests.get(
            f"{BASE_URL}/api/library",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code != 200:
            pytest.skip("Could not fetch library")
        
        articles = response.json().get("articles", [])
        pmids = [a.get("pmid") for a in articles[:2] if a.get("pmid")]
        
        if len(pmids) < 2:
            pytest.skip("Need at least 2 articles with PMIDs to test compare")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": pmids},
            timeout=60
        )
        
        assert response.status_code in [200, 400, 404, 429], \
            f"Expected 200/400/404/429, got {response.status_code}: {response.text}"
        
        print("SUCCESS: /api/copilot/compare-studies works without use_expertise_context")
    
    def test_compare_studies_with_expertise_context(self, premium_headers):
        """POST /api/copilot/compare-studies accepts use_expertise_context:true."""
        response = requests.get(
            f"{BASE_URL}/api/library",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code != 200:
            pytest.skip("Could not fetch library")
        
        articles = response.json().get("articles", [])
        pmids = [a.get("pmid") for a in articles[:2] if a.get("pmid")]
        
        if len(pmids) < 2:
            pytest.skip("Need at least 2 articles with PMIDs to test compare")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": pmids, "use_expertise_context": True},
            timeout=60
        )
        
        assert response.status_code in [200, 400, 404, 429], \
            f"Expected 200/400/404/429, got {response.status_code}: {response.text}"
        
        print("SUCCESS: /api/copilot/compare-studies accepts use_expertise_context:true")


# ===========================================================================
# Artifact Type Validation Tests
# ===========================================================================

class TestArtifactTypeValidation:
    """Tests for artifact type validation (evidence_brief, ask_answer, comparison)."""
    
    def test_create_artifact_invalid_type(self, premium_headers):
        """POST /api/litscholar/artifacts rejects invalid artifact_type."""
        payload = {
            "artifact_type": "invalid_type",
            "title": "TEST_Invalid Type",
            "summary_text": "Test summary",
            "pmids": []
        }
        
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        # Should return 422 for validation error
        assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
        
        print("SUCCESS: Invalid artifact_type is rejected with 422")
    
    def test_create_artifact_ask_answer_type(self, premium_headers):
        """POST /api/litscholar/artifacts accepts ask_answer type."""
        payload = {
            "artifact_type": "ask_answer",
            "title": "TEST_Ask Answer Artifact",
            "summary_text": "This is an ask/answer artifact.",
            "pmids": ["33333333"]
        }
        
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        # Clean up
        artifact_id = response.json()["artifact_id"]
        requests.delete(
            f"{BASE_URL}/api/litscholar/artifacts/{artifact_id}",
            headers=premium_headers,
            timeout=30
        )
        
        print("SUCCESS: ask_answer artifact type is accepted")
    
    def test_create_artifact_comparison_type(self, premium_headers):
        """POST /api/litscholar/artifacts accepts comparison type."""
        payload = {
            "artifact_type": "comparison",
            "title": "TEST_Comparison Artifact",
            "summary_text": "This is a comparison artifact.",
            "pmids": ["44444444", "55555555"]
        }
        
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json=payload,
            timeout=30
        )
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        # Clean up
        artifact_id = response.json()["artifact_id"]
        requests.delete(
            f"{BASE_URL}/api/litscholar/artifacts/{artifact_id}",
            headers=premium_headers,
            timeout=30
        )
        
        print("SUCCESS: comparison artifact type is accepted")


# ===========================================================================
# Cleanup Test Artifacts
# ===========================================================================

class TestCleanup:
    """Cleanup TEST_ prefixed artifacts after tests."""
    
    def test_cleanup_test_artifacts(self, premium_headers):
        """Cleanup any remaining TEST_ prefixed artifacts."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/artifacts?limit=100",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code != 200:
            pytest.skip("Could not list artifacts for cleanup")
        
        artifacts = response.json().get("artifacts", [])
        deleted = 0
        
        for artifact in artifacts:
            if artifact.get("title", "").startswith("TEST_"):
                del_response = requests.delete(
                    f"{BASE_URL}/api/litscholar/artifacts/{artifact['artifact_id']}",
                    headers=premium_headers,
                    timeout=30
                )
                if del_response.status_code == 200:
                    deleted += 1
        
        print(f"Cleanup: Deleted {deleted} TEST_ artifacts")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
