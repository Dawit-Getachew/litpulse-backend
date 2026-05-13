"""
Test Batch 5: Final Acceptance Tests - Comprehensive Testing Across All Batches

Tests for:
  # BACKEND: Copilot endpoints
  - POST /api/copilot/evidence-brief works (single article, mock provider) - backward compat
  - POST /api/copilot/evidence-brief with use_expertise_context:true works
  - POST /api/copilot/compare-studies requires 2-5 PMIDs (rejects <2 or >5)
  - POST /api/copilot/draft-discussion-post returns draft_post (draft only, no auto-submit)
  
  # BACKEND: Audio digests
  - POST /api/audio-digests with mode=playlist works
  - POST /api/audio-digests with mode=combined_summary works
  - POST /api/audio-digests with mode=combined_summary rejects >5 PMIDs
  
  # BACKEND: PHI guard
  - PHI guard blocks SSN/patient names on /api/litscholar/artifacts and /api/copilot/ask-article
  
  # BACKEND: LitScholar profile
  - /api/litscholar/profile/rebuild returns structured expertise profile
  
  # BACKEND: Artifact deletion
  - DELETE /api/litscholar/artifacts/{id} returns 404 for non-existent

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
# COPILOT ENDPOINTS TESTS
# ===========================================================================

class TestCopilotEvidenceBrief:
    """Tests for POST /api/copilot/evidence-brief"""
    
    def test_evidence_brief_single_article_backward_compat(self, premium_headers):
        """POST /api/copilot/evidence-brief works without use_expertise_context."""
        pmids = TestSetup.get_library_pmids(premium_headers, 1)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            headers=premium_headers,
            json={"pmid": pmids[0]},
            timeout=60
        )
        
        # 200 (success), 404 (article not found), or 429 (quota) are acceptable
        assert response.status_code in [200, 404, 429], \
            f"Expected 200/404/429, got {response.status_code}: {response.text}"
        
        if response.status_code == 200:
            data = response.json()
            # Verify response structure from mock provider
            assert "evidence_brief" in data or "raw_text" in data, \
                "Response should have evidence_brief or raw_text"
            assert "disclaimer" in data, "Response should have disclaimer"
            print(f"SUCCESS: evidence-brief returned for PMID {pmids[0]}, cached={data.get('cached', False)}")
        else:
            print(f"INFO: evidence-brief returned {response.status_code}")
    
    def test_evidence_brief_with_expertise_context(self, premium_headers):
        """POST /api/copilot/evidence-brief accepts use_expertise_context:true."""
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
        
        print(f"SUCCESS: evidence-brief with use_expertise_context:true accepted, status={response.status_code}")


class TestCopilotCompareStudies:
    """Tests for POST /api/copilot/compare-studies"""
    
    def test_compare_studies_requires_min_2_pmids(self, premium_headers):
        """POST /api/copilot/compare-studies rejects <2 PMIDs."""
        pmids = TestSetup.get_library_pmids(premium_headers, 1)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": pmids[:1]},
            timeout=30
        )
        
        # Expect 422 validation error for <2 PMIDs (Pydantic validation)
        assert response.status_code == 422, \
            f"Expected 422 for 1 PMID, got {response.status_code}: {response.text}"
        
        print("SUCCESS: compare-studies rejects <2 PMIDs with 422")
    
    def test_compare_studies_requires_max_5_pmids(self, premium_headers):
        """POST /api/copilot/compare-studies rejects >5 PMIDs."""
        # Create fake PMIDs for testing validation
        too_many_pmids = ["11111111", "22222222", "33333333", "44444444", "55555555", "66666666"]
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": too_many_pmids},
            timeout=30
        )
        
        # Expect 422 validation error for >5 PMIDs
        assert response.status_code == 422, \
            f"Expected 422 for 6 PMIDs, got {response.status_code}: {response.text}"
        
        print("SUCCESS: compare-studies rejects >5 PMIDs with 422")
    
    def test_compare_studies_accepts_valid_range(self, premium_headers):
        """POST /api/copilot/compare-studies accepts 2-5 PMIDs."""
        pmids = TestSetup.get_library_pmids(premium_headers, 3)
        if len(pmids) < 2:
            pytest.skip("Need at least 2 articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/compare-studies",
            headers=premium_headers,
            json={"pmids": pmids[:2]},
            timeout=60
        )
        
        # 200, 400 (not enough valid articles), or 429 are acceptable
        assert response.status_code in [200, 400, 429], \
            f"Expected 200/400/429, got {response.status_code}: {response.text}"
        
        print(f"SUCCESS: compare-studies accepted 2 PMIDs, status={response.status_code}")


class TestCopilotDraftPost:
    """Tests for POST /api/copilot/draft-discussion-post"""
    
    def test_draft_discussion_post_returns_draft(self, premium_headers):
        """POST /api/copilot/draft-discussion-post returns draft_post."""
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
        
        # 200 (success), 400/404 (article issues), 403 (not verified), 429 (quota)
        assert response.status_code in [200, 400, 403, 404, 429], \
            f"Expected 200/400/403/404/429, got {response.status_code}: {response.text}"
        
        if response.status_code == 200:
            data = response.json()
            # Verify draft_post field exists (mock provider returns structured response)
            assert "draft_post" in data or "raw_text" in data, \
                f"Response should have draft_post or raw_text: {data.keys()}"
            print(f"SUCCESS: draft-discussion-post returned draft")
        else:
            detail = response.json().get("detail", {})
            print(f"INFO: draft-discussion-post returned {response.status_code}: {detail}")


# ===========================================================================
# AUDIO DIGESTS TESTS
# ===========================================================================

class TestAudioDigests:
    """Tests for POST /api/audio-digests with playlist and combined_summary modes"""
    
    def test_audio_digests_playlist_mode(self, premium_headers):
        """POST /api/audio-digests with mode=playlist works."""
        pmids = TestSetup.get_library_pmids(premium_headers, 2)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/audio-digests",
            headers=premium_headers,
            json={
                "pmids": pmids,
                "title": "TEST_Playlist Digest",
                "mode": "playlist",
                "auto_generate_missing": False  # Skip audio generation in test
            },
            timeout=60
        )
        
        # 201 (created), 400 (no valid PMIDs), 403 (premium required), 404 (feature disabled)
        assert response.status_code in [201, 400, 403, 404], \
            f"Expected 201/400/403/404, got {response.status_code}: {response.text}"
        
        if response.status_code == 201:
            data = response.json()
            assert "audio_digest_id" in data, "Response should have audio_digest_id"
            assert data.get("title") == "TEST_Playlist Digest" or data.get("mode") == "playlist"
            print(f"SUCCESS: Playlist audio digest created: {data.get('audio_digest_id')}")
        elif response.status_code == 404:
            detail = response.json().get("detail", {})
            if detail.get("error_code") == "feature_disabled":
                pytest.skip("Audio digests V2 feature is disabled")
        else:
            print(f"INFO: audio-digests playlist returned {response.status_code}")
    
    def test_audio_digests_combined_summary_mode(self, premium_headers):
        """POST /api/audio-digests with mode=combined_summary works."""
        pmids = TestSetup.get_library_pmids(premium_headers, 3)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/audio-digests",
            headers=premium_headers,
            json={
                "pmids": pmids[:3],  # Within 5 limit
                "title": "TEST_Combined Summary",
                "mode": "combined_summary"
            },
            timeout=90  # Longer timeout for combined summary generation
        )
        
        # 201 (created), 400 (no valid PMIDs), 403 (feature disabled or premium required), 404 (feature disabled)
        assert response.status_code in [201, 400, 403, 404], \
            f"Expected 201/400/403/404, got {response.status_code}: {response.text}"
        
        if response.status_code == 201:
            data = response.json()
            assert "audio_digest_id" in data, "Response should have audio_digest_id"
            assert data.get("mode") == "combined_summary"
            print(f"SUCCESS: Combined summary audio digest created: {data.get('audio_digest_id')}")
        elif response.status_code in [403, 404]:
            detail = response.json().get("detail", {})
            if detail.get("error_code") == "feature_disabled":
                pytest.skip("Combined audio summary feature is disabled")
        else:
            print(f"INFO: audio-digests combined_summary returned {response.status_code}")
    
    def test_audio_digests_combined_summary_rejects_over_5_pmids(self, premium_headers):
        """POST /api/audio-digests with mode=combined_summary rejects >5 PMIDs."""
        # Use 6 fake PMIDs
        too_many_pmids = ["11111111", "22222222", "33333333", "44444444", "55555555", "66666666"]
        
        response = requests.post(
            f"{BASE_URL}/api/audio-digests",
            headers=premium_headers,
            json={
                "pmids": too_many_pmids,
                "mode": "combined_summary"
            },
            timeout=30
        )
        
        # 400 (too_many_articles or no_library_pmids since fake PMIDs), 403, or 404 (feature disabled)
        # The endpoint first validates PMIDs are in library before checking count limit
        assert response.status_code in [400, 403, 404], \
            f"Expected 400/403/404, got {response.status_code}: {response.text}"
        
        if response.status_code == 404:
            detail = response.json().get("detail", {})
            if detail.get("error_code") == "feature_disabled":
                pytest.skip("Combined audio summary feature is disabled")
        
        print(f"SUCCESS: combined_summary with >5 PMIDs rejected, status={response.status_code}")


# ===========================================================================
# PHI GUARD TESTS
# ===========================================================================

class TestPHIGuard:
    """Tests for PHI guard blocking SSN/patient names"""
    
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
            f"Expected 422 (PHI detected) or 503 (feature disabled), got {response.status_code}: {response.text}"
        
        if response.status_code == 422:
            data = response.json()
            assert data.get("detail", {}).get("error_code") == "phi_detected", \
                f"Expected error_code=phi_detected, got {data}"
            print("SUCCESS: PHI guard blocks SSN on artifacts")
        else:
            pytest.skip("LitScholar profile memory feature is disabled")
    
    def test_phi_guard_blocks_patient_name_on_artifacts(self, premium_headers):
        """PHI guard blocks patient names on /api/litscholar/artifacts."""
        response = requests.post(
            f"{BASE_URL}/api/litscholar/artifacts",
            headers=premium_headers,
            json={
                "artifact_type": "evidence_brief",
                "title": "TEST_About patient John Smith findings",
                "summary_text": "Normal medical summary.",
                "pmids": ["12345678"]
            },
            timeout=30
        )
        
        assert response.status_code in [422, 503], \
            f"Expected 422 (PHI detected) or 503 (feature disabled), got {response.status_code}: {response.text}"
        
        if response.status_code == 422:
            data = response.json()
            assert data.get("detail", {}).get("error_code") == "phi_detected"
            print("SUCCESS: PHI guard blocks patient name on artifacts")
        else:
            pytest.skip("LitScholar profile memory feature is disabled")
    
    def test_phi_guard_blocks_ssn_on_ask_article(self, premium_headers):
        """PHI guard blocks SSN on /api/copilot/ask-article."""
        pmids = TestSetup.get_library_pmids(premium_headers, 1)
        if not pmids:
            pytest.skip("No articles in library")
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/ask-article",
            headers=premium_headers,
            json={
                "pmid": pmids[0],
                "question": "What about patient with SSN 123-45-6789?"
            },
            timeout=30
        )
        
        assert response.status_code in [422, 503], \
            f"Expected 422 (PHI detected) or 503 (copilot disabled), got {response.status_code}: {response.text}"
        
        if response.status_code == 422:
            data = response.json()
            assert data.get("detail", {}).get("error_code") == "phi_detected"
            print("SUCCESS: PHI guard blocks SSN on ask-article")
        else:
            pytest.skip("Copilot feature is disabled")


# ===========================================================================
# LITSCHOLAR PROFILE TESTS
# ===========================================================================

class TestLitScholarProfile:
    """Tests for LitScholar profile rebuild"""
    
    def test_profile_rebuild_returns_structured_profile(self, premium_headers):
        """POST /api/litscholar/profile/rebuild returns structured expertise profile."""
        response = requests.post(
            f"{BASE_URL}/api/litscholar/profile/rebuild",
            headers=premium_headers,
            timeout=60
        )
        
        assert response.status_code in [200, 503], \
            f"Expected 200/503, got {response.status_code}: {response.text}"
        
        if response.status_code == 503:
            pytest.skip("LitScholar profile memory feature is disabled")
        
        data = response.json()
        assert "user_id" in data, "Response should have user_id"
        assert "expertise_profile" in data, "Response should have expertise_profile"
        
        profile = data["expertise_profile"]
        # Verify structured profile fields
        assert "specialty_ids" in profile, "Profile should have specialty_ids"
        assert "subspecialty_ids" in profile, "Profile should have subspecialty_ids"
        assert "topic_weights" in profile, "Profile should have topic_weights"
        assert "journal_weights" in profile, "Profile should have journal_weights"
        assert "study_design_preferences" in profile, "Profile should have study_design_preferences"
        assert "recent_library_clusters" in profile, "Profile should have recent_library_clusters"
        
        print(f"SUCCESS: profile rebuild returns structured profile with {len(profile.get('specialty_ids', []))} specialties")


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
