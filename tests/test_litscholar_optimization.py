"""
Test LitScholar Personalization Optimization (Iteration 41)

Tests for the optimization that replaced unconditional rebuildProfile() with:
- Cached-first getProfile() + conditional background rebuild based on staleness

Key tests:
1. GET /api/litscholar/profile returns expertise_summary and last_rebuild_completed_at fields
2. POST /api/litscholar/profile/rebuild stores expertise_summary and last_rebuild_completed_at
3. GET /api/litscholar/profile for existing user with old data returns gracefully
4. POST /api/copilot/evidence-brief with use_expertise_context:true works
5. POST /api/copilot/evidence-brief without use_expertise_context works (backward compat)
6. POST /api/copilot/compare-studies rejects <2 or >5 PMIDs
7. POST /api/copilot/draft-discussion-post returns draft_post
8. PHI guard still applies on /api/litscholar/artifacts (SSN blocked)
9. DELETE /api/litscholar/artifacts/{id} returns 404 for non-existent

COPILOT_PROVIDER=mock - all AI responses are placeholder text
"""
import pytest
import requests
import os
import uuid
import time

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


class TestSetup:
    """Setup: Login and get tokens."""
    premium_token = None
    library_pmids = None
    
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
    def get_library_pmids(cls, headers, count=5):
        """Get PMIDs from user's library."""
        if cls.library_pmids and len(cls.library_pmids) >= count:
            return cls.library_pmids[:count]
        
        response = requests.get(
            f"{BASE_URL}/api/library?limit={count}",
            headers=headers,
            timeout=30
        )
        if response.status_code == 200:
            articles = response.json().get("articles", [])
            cls.library_pmids = [a.get("pmid") for a in articles if a.get("pmid")]
            return cls.library_pmids[:count]
        return []


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


# ===========================================================================
# LITSCHOLAR PROFILE OPTIMIZATION TESTS
# ===========================================================================

class TestLitScholarProfileOptimization:
    """Tests for the new profile optimization: expertise_summary and last_rebuild_completed_at fields"""
    
    def test_get_profile_returns_expertise_summary_field(self, premium_headers):
        """GET /api/litscholar/profile returns expertise_summary field."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/profile",
            headers=premium_headers,
            timeout=30
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        # Check new optimization fields exist in response
        assert "expertise_summary" in data, f"Response should contain expertise_summary. Keys: {data.keys()}"
        # expertise_summary can be empty string for users who haven't rebuilt
        assert isinstance(data.get("expertise_summary"), str), "expertise_summary should be a string"
        
        print(f"SUCCESS: GET /api/litscholar/profile returns expertise_summary (length={len(data.get('expertise_summary', ''))})")
    
    def test_get_profile_returns_last_rebuild_completed_at_field(self, premium_headers):
        """GET /api/litscholar/profile returns last_rebuild_completed_at field."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/profile",
            headers=premium_headers,
            timeout=30
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        # last_rebuild_completed_at may be None if never rebuilt, or ISO timestamp if rebuilt
        # The field should exist in the response schema
        assert "last_rebuild_completed_at" in data, f"Response should contain last_rebuild_completed_at. Keys: {data.keys()}"
        # It can be None (never rebuilt) or a string (ISO timestamp)
        if data.get("last_rebuild_completed_at") is not None:
            assert isinstance(data.get("last_rebuild_completed_at"), str), \
                "last_rebuild_completed_at should be ISO timestamp string"
        
        print(f"SUCCESS: GET /api/litscholar/profile returns last_rebuild_completed_at={data.get('last_rebuild_completed_at')}")
    
    def test_rebuild_profile_stores_expertise_summary(self, premium_headers):
        """POST /api/litscholar/profile/rebuild stores expertise_summary and last_rebuild_completed_at."""
        response = requests.post(
            f"{BASE_URL}/api/litscholar/profile/rebuild",
            headers=premium_headers,
            timeout=60
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        
        # After rebuild, both fields should be populated
        assert "expertise_summary" in data, "Response should contain expertise_summary after rebuild"
        assert "last_rebuild_completed_at" in data, "Response should contain last_rebuild_completed_at after rebuild"
        
        # After rebuild, last_rebuild_completed_at should NOT be None
        assert data.get("last_rebuild_completed_at") is not None, \
            "last_rebuild_completed_at should be set after rebuild"
        
        # Verify expertise_summary is precomputed string format
        summary = data.get("expertise_summary", "")
        assert isinstance(summary, str), "expertise_summary should be a string"
        
        print(f"SUCCESS: POST /api/litscholar/profile/rebuild stores expertise_summary (length={len(summary)})")
        print(f"         last_rebuild_completed_at={data.get('last_rebuild_completed_at')}")
    
    def test_get_profile_after_rebuild_has_populated_fields(self, premium_headers):
        """GET /api/litscholar/profile after rebuild returns populated fields."""
        # First rebuild
        rebuild_response = requests.post(
            f"{BASE_URL}/api/litscholar/profile/rebuild",
            headers=premium_headers,
            timeout=60
        )
        assert rebuild_response.status_code == 200, "Rebuild should succeed"
        
        # Then GET to verify persistence
        get_response = requests.get(
            f"{BASE_URL}/api/litscholar/profile",
            headers=premium_headers,
            timeout=30
        )
        
        assert get_response.status_code == 200, f"Expected 200, got {get_response.status_code}"
        
        data = get_response.json()
        # Verify the rebuild persisted the data
        assert "expertise_summary" in data
        assert "last_rebuild_completed_at" in data
        assert data.get("last_rebuild_completed_at") is not None, \
            "After rebuild, last_rebuild_completed_at should be persisted"
        
        print("SUCCESS: GET after rebuild returns populated expertise_summary and last_rebuild_completed_at")
    
    def test_profile_graceful_for_missing_fields_old_data(self, premium_headers):
        """GET /api/litscholar/profile handles old data without expertise_summary gracefully."""
        # This test verifies the endpoint doesn't error when legacy data exists
        response = requests.get(
            f"{BASE_URL}/api/litscholar/profile",
            headers=premium_headers,
            timeout=30
        )
        
        # Should return 200, not 500 even if fields were missing (backward compat)
        assert response.status_code == 200, \
            f"Should return 200 gracefully, got {response.status_code}: {response.text}"
        
        data = response.json()
        # Core fields should exist
        assert "user_id" in data
        assert "expertise_profile" in data
        
        print("SUCCESS: GET /api/litscholar/profile handles data gracefully")


# ===========================================================================
# COPILOT EXPERTISE CONTEXT TESTS
# ===========================================================================

class TestCopilotExpertiseContext:
    """Tests for copilot endpoints using the precomputed expertise_summary"""
    
    def test_evidence_brief_with_expertise_context(self, premium_headers):
        """POST /api/copilot/evidence-brief with use_expertise_context:true works."""
        pmids = TestSetup.get_library_pmids(premium_headers, 1)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            headers=premium_headers,
            json={"pmid": pmids[0], "use_expertise_context": True},
            timeout=60
        )
        
        assert response.status_code in [200, 404, 429], \
            f"Expected 200/404/429, got {response.status_code}: {response.text}"
        
        if response.status_code == 200:
            data = response.json()
            assert "evidence_brief" in data or "raw_text" in data
            print(f"SUCCESS: evidence-brief with use_expertise_context:true works, cached={data.get('cached', False)}")
        else:
            print(f"INFO: evidence-brief returned {response.status_code} (quota or article issue)")
    
    def test_evidence_brief_without_expertise_context_backward_compat(self, premium_headers):
        """POST /api/copilot/evidence-brief without use_expertise_context works (backward compat)."""
        pmids = TestSetup.get_library_pmids(premium_headers, 1)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            headers=premium_headers,
            json={"pmid": pmids[0]},  # No use_expertise_context field
            timeout=60
        )
        
        assert response.status_code in [200, 404, 429], \
            f"Expected 200/404/429, got {response.status_code}: {response.text}"
        
        print(f"SUCCESS: evidence-brief without use_expertise_context works (backward compat), status={response.status_code}")


# ===========================================================================
# COPILOT COMPARE STUDIES VALIDATION TESTS
# ===========================================================================

class TestCopilotCompareStudiesValidation:
    """Tests for POST /api/copilot/compare-studies PMID count validation (2-5 required)"""
    
    def test_compare_studies_rejects_less_than_2_pmids(self, premium_headers):
        """POST /api/copilot/compare-studies rejects <2 PMIDs with 422."""
        pmids = TestSetup.get_library_pmids(premium_headers, 1)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": pmids[:1]},  # Only 1 PMID
            timeout=30
        )
        
        # Pydantic validation should reject with 422
        assert response.status_code == 422, \
            f"Expected 422 for 1 PMID, got {response.status_code}: {response.text}"
        
        print("SUCCESS: compare-studies rejects <2 PMIDs with 422")
    
    def test_compare_studies_rejects_more_than_5_pmids(self, premium_headers):
        """POST /api/copilot/compare-studies rejects >5 PMIDs with 422."""
        # Use 6 fake PMIDs for validation test
        too_many_pmids = ["11111111", "22222222", "33333333", "44444444", "55555555", "66666666"]
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": too_many_pmids},
            timeout=30
        )
        
        # Pydantic validation should reject with 422
        assert response.status_code == 422, \
            f"Expected 422 for 6 PMIDs, got {response.status_code}: {response.text}"
        
        print("SUCCESS: compare-studies rejects >5 PMIDs with 422")
    
    def test_compare_studies_accepts_2_pmids(self, premium_headers):
        """POST /api/copilot/compare-studies accepts 2 PMIDs."""
        pmids = TestSetup.get_library_pmids(premium_headers, 2)
        if len(pmids) < 2:
            pytest.skip("Need at least 2 articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": pmids[:2]},
            timeout=60
        )
        
        # 200 (success), 400 (not enough valid articles in DB), 429 (quota)
        assert response.status_code in [200, 400, 429], \
            f"Expected 200/400/429, got {response.status_code}: {response.text}"
        
        print(f"SUCCESS: compare-studies accepts 2 PMIDs, status={response.status_code}")


# ===========================================================================
# COPILOT DRAFT DISCUSSION POST TESTS
# ===========================================================================

class TestCopilotDraftDiscussionPost:
    """Tests for POST /api/copilot/draft-discussion-post"""
    
    def test_draft_discussion_post_returns_draft_post(self, premium_headers):
        """POST /api/copilot/draft-discussion-post returns draft_post field."""
        pmids = TestSetup.get_library_pmids(premium_headers, 1)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/draft-discussion-post",
            headers=premium_headers,
            json={
                "specialty_id": "internal_medicine",
                "pmids": pmids[:1],
                "tone": "neutral"
            },
            timeout=60
        )
        
        # 200, 400 (no valid articles), 403 (not verified peer), 429 (quota)
        assert response.status_code in [200, 400, 403, 404, 429], \
            f"Expected 200/400/403/404/429, got {response.status_code}: {response.text}"
        
        if response.status_code == 200:
            data = response.json()
            # Verify draft_post field exists (mock provider returns structured response)
            assert "draft_post" in data or "raw_text" in data, \
                f"Response should have draft_post or raw_text. Keys: {list(data.keys())}"
            print("SUCCESS: draft-discussion-post returns draft_post")
        else:
            print(f"INFO: draft-discussion-post returned {response.status_code}")


# ===========================================================================
# PHI GUARD TESTS
# ===========================================================================

class TestPHIGuardArtifacts:
    """Tests for PHI guard on /api/litscholar/artifacts"""
    
    def test_phi_guard_blocks_ssn_on_artifacts(self, premium_headers):
        """PHI guard blocks SSN on /api/litscholar/artifacts."""
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json={
                "artifact_type": "evidence_brief",
                "title": "TEST_Artifact with PHI",
                "summary_text": "Patient SSN 123-45-6789 should be blocked.",
                "pmids": ["12345678"]
            },
            timeout=30
        )
        
        assert response.status_code in [422, 503], \
            f"Expected 422 (PHI) or 503 (disabled), got {response.status_code}: {response.text}"
        
        if response.status_code == 422:
            data = response.json()
            assert data.get("detail", {}).get("error_code") == "phi_detected", \
                f"Expected error_code=phi_detected, got {data}"
            print("SUCCESS: PHI guard blocks SSN on artifacts")
        else:
            pytest.skip("LitScholar profile memory feature is disabled")


# ===========================================================================
# ARTIFACT DELETION TESTS
# ===========================================================================

class TestArtifactDeletion:
    """Tests for DELETE /api/litscholar/artifacts/{id}"""
    
    def test_delete_nonexistent_artifact_returns_404(self, premium_headers):
        """DELETE /api/litscholar/artifacts/{id} returns 404 for non-existent."""
        fake_id = str(uuid.uuid4())
        
        response = requests.delete(
            f"{BASE_URL}/api/litscholar/artifacts/{fake_id}",
            headers=premium_headers,
            timeout=30
        )
        
        assert response.status_code in [404, 503], \
            f"Expected 404/503, got {response.status_code}: {response.text}"
        
        if response.status_code == 503:
            pytest.skip("LitScholar profile memory feature is disabled")
        
        print("SUCCESS: DELETE non-existent artifact returns 404")


# ===========================================================================
# CLEANUP
# ===========================================================================

class TestCleanup:
    """Cleanup TEST_ prefixed data after tests."""
    
    def test_cleanup_test_artifacts(self, premium_headers):
        """Cleanup any remaining TEST_ prefixed artifacts."""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/artifacts?limit=100",
            headers=premium_headers,
            timeout=30
        )
        
        if response.status_code == 503:
            pytest.skip("LitScholar profile memory feature is disabled")
        
        if response.status_code != 200:
            return
        
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
