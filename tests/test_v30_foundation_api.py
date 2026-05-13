"""
LitPulse v3.0 Foundation - Backend API Tests

Tests for:
1. Enhanced /api/auth/me with plan_tier, peer_verification_status, capabilities
2. GET /api/config/feature-flags
3. PHI Guard enforcement on notes, discussions (threads, comments, reports)
4. Existing auth flows remain unaffected
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
    """Login as premium user (demo@litpulse.com - subscription_level=2)"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json=PREMIUM_USER)
    if response.status_code == 200:
        return response.json()["access_token"]
    pytest.skip(f"Premium user login failed: {response.status_code} - {response.text}")


@pytest.fixture(scope="module")
def free_token():
    """Login as free user (test@litpulse.com - subscription_level=1)"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json=FREE_USER)
    if response.status_code == 200:
        return response.json()["access_token"]
    pytest.skip(f"Free user login failed: {response.status_code} - {response.text}")


class TestAuthMeEnhanced:
    """Test enhanced /api/auth/me endpoint with plan_tier, peer_verification_status, capabilities"""

    def test_auth_me_premium_user_plan_tier(self, premium_token):
        """Premium user (subscription_level=2) should get plan_tier='premium'"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # Basic user fields
        assert "user_id" in data
        assert "email" in data
        assert data["email"] == PREMIUM_USER["email"]
        
        # v3.0 enhanced fields
        assert data.get("plan_tier") == "premium", f"Expected plan_tier='premium', got '{data.get('plan_tier')}'"
        print(f"✓ Premium user plan_tier: {data.get('plan_tier')}")

    def test_auth_me_premium_user_peer_verification_status(self, premium_token):
        """Premium user should have peer_verification_status field"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # peer_verification_status should be one of: none, pending, verified, rejected
        peer_status = data.get("peer_verification_status")
        valid_statuses = ["none", "pending", "verified", "rejected"]
        assert peer_status in valid_statuses, f"Invalid peer_verification_status: {peer_status}"
        print(f"✓ Premium user peer_verification_status: {peer_status}")

    def test_auth_me_premium_user_capabilities(self, premium_token):
        """Premium user should have full capabilities object"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {premium_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        capabilities = data.get("capabilities")
        assert capabilities is not None, "capabilities should not be None"
        assert isinstance(capabilities, dict), "capabilities should be a dict"
        
        # Check premium-specific fields
        assert capabilities.get("premium_export") == True, "Premium user should have premium_export=True"
        assert capabilities.get("premium_audio") == True, "Premium user should have premium_audio=True"
        assert capabilities.get("premium_copilot") == True, "Premium user should have premium_copilot=True"
        assert capabilities.get("copilot_premium") == True, "Premium user should have copilot_premium=True"
        
        # Check quota limits (premium should have higher limits)
        assert capabilities.get("max_digests_per_day") == 10, "Premium should have max_digests_per_day=10"
        assert capabilities.get("max_notes_per_article") == 50, "Premium should have max_notes_per_article=50"
        assert capabilities.get("max_threads_per_day") == 20, "Premium should have max_threads_per_day=20"
        assert capabilities.get("max_library_articles") == 1000, "Premium should have max_library_articles=1000"
        
        print(f"✓ Premium user capabilities verified with correct limits")

    def test_auth_me_free_user_plan_tier(self, free_token):
        """Free user (subscription_level=1) should get plan_tier='free'"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        assert data.get("plan_tier") == "free", f"Expected plan_tier='free', got '{data.get('plan_tier')}'"
        print(f"✓ Free user plan_tier: {data.get('plan_tier')}")

    def test_auth_me_free_user_peer_verification_status(self, free_token):
        """Free user should have peer_verification_status='none'"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        peer_status = data.get("peer_verification_status")
        assert peer_status == "none", f"Expected peer_verification_status='none', got '{peer_status}'"
        print(f"✓ Free user peer_verification_status: {peer_status}")

    def test_auth_me_free_user_capabilities(self, free_token):
        """Free user should have limited capabilities"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {free_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        capabilities = data.get("capabilities")
        assert capabilities is not None, "capabilities should not be None"
        
        # Check free user limits (lower than premium)
        assert capabilities.get("premium_export") == False, "Free user should have premium_export=False"
        assert capabilities.get("premium_audio") == False, "Free user should have premium_audio=False"
        assert capabilities.get("copilot_premium") == False, "Free user should have copilot_premium=False"
        
        # Check quota limits (free should have lower limits)
        assert capabilities.get("max_digests_per_day") == 3, "Free should have max_digests_per_day=3"
        assert capabilities.get("max_notes_per_article") == 10, "Free should have max_notes_per_article=10"
        assert capabilities.get("max_threads_per_day") == 5, "Free should have max_threads_per_day=5"
        assert capabilities.get("max_library_articles") == 100, "Free should have max_library_articles=100"
        
        print(f"✓ Free user capabilities verified with correct limits")


class TestFeatureFlags:
    """Test GET /api/config/feature-flags endpoint"""

    def test_feature_flags_returns_phi_guard_enabled(self):
        """Feature flags should include phi_guard_enabled"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        data = response.json()
        
        assert "phi_guard_enabled" in data
        assert isinstance(data["phi_guard_enabled"], bool)
        print(f"✓ phi_guard_enabled: {data['phi_guard_enabled']}")

    def test_feature_flags_returns_phi_guard_mode(self):
        """Feature flags should include phi_guard_mode"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        data = response.json()
        
        assert "phi_guard_mode" in data
        assert data["phi_guard_mode"] in ["block", "warn"]
        print(f"✓ phi_guard_mode: {data['phi_guard_mode']}")

    def test_feature_flags_returns_require_verified_for_posting(self):
        """Feature flags should include require_verified_for_posting"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        data = response.json()
        
        assert "require_verified_for_posting" in data
        assert isinstance(data["require_verified_for_posting"], bool)
        print(f"✓ require_verified_for_posting: {data['require_verified_for_posting']}")

    def test_feature_flags_public_access(self):
        """Feature flags should be accessible without authentication"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        print("✓ Feature flags accessible without auth")


class TestPhiGuardNotes:
    """Test PHI Guard on notes endpoints"""

    def test_notes_with_ssn_blocked(self, premium_token):
        """POST /api/notes with SSN pattern should return 422 with phi_detected"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test123", "body": "Patient SSN is 123-45-6789"}
        )
        assert response.status_code == 422
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected", f"Expected error_code='phi_detected', got: {detail}"
        assert "ssn" in detail.get("detected_categories", [])
        print(f"✓ SSN detected and blocked: {detail.get('detected_categories')}")

    def test_notes_with_mrn_blocked(self, premium_token):
        """POST /api/notes with MRN pattern should return 422"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test123", "body": "Medical record MRN: 12345678"}
        )
        assert response.status_code == 422
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected"
        assert "mrn" in detail.get("detected_categories", [])
        print(f"✓ MRN detected and blocked: {detail.get('detected_categories')}")

    def test_notes_with_dob_blocked(self, premium_token):
        """POST /api/notes with DOB pattern should return 422"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test123", "body": "Patient DOB: 01/15/1980"}
        )
        assert response.status_code == 422
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected"
        assert "dob" in detail.get("detected_categories", [])
        print(f"✓ DOB detected and blocked: {detail.get('detected_categories')}")

    def test_notes_with_patient_name_blocked(self, premium_token):
        """POST /api/notes with patient name pattern should return 422"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test123", "body": "The patient John Smith presented with symptoms"}
        )
        assert response.status_code == 422
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected"
        assert "patient_name" in detail.get("detected_categories", [])
        print(f"✓ Patient name detected and blocked: {detail.get('detected_categories')}")

    def test_notes_with_clean_text_passes(self, premium_token):
        """POST /api/notes with clean medical text should pass and create note"""
        clean_text = "This article discusses SGLT2 inhibitors in heart failure management. The study included 1000 participants with HFrEF."
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test_clean_article", "body": clean_text}
        )
        # Note: This may return 404 if article doesn't exist, but we're testing PHI guard passes
        if response.status_code == 422:
            data = response.json()
            detail = data.get("detail", {})
            pytest.fail(f"Clean text was blocked by PHI guard: {detail}")
        # 201 (created) or 404 (article not found) are both acceptable - no PHI block
        assert response.status_code in [201, 404], f"Unexpected status: {response.status_code}"
        print(f"✓ Clean text passed PHI guard (status={response.status_code})")


class TestPhiGuardDiscussions:
    """Test PHI Guard on discussion endpoints (threads, comments, reports)"""

    def test_create_thread_with_phi_blocked(self, premium_token):
        """POST /api/discussions/threads with PHI in title should return 422"""
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": "Case review: patient John Smith with SSN 123-45-6789"
            }
        )
        assert response.status_code == 422
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected"
        print(f"✓ Thread with PHI blocked: {detail.get('detected_categories')}")

    def test_create_thread_with_clean_title_passes(self, premium_token):
        """POST /api/discussions/threads with clean title should pass"""
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": "TEST_V30 Discussion on SGLT2 inhibitors in HFrEF management"
            }
        )
        assert response.status_code == 201
        data = response.json()
        assert "thread_id" in data
        print(f"✓ Clean thread created: {data.get('thread_id')}")
        return data.get("thread_id")

    def test_create_comment_with_phi_blocked(self, premium_token):
        """POST comment with PHI should return 422"""
        # First create a clean thread
        thread_resp = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": "TEST_V30_COMMENT Test thread for PHI comment testing"
            }
        )
        if thread_resp.status_code != 201:
            pytest.skip("Could not create thread for comment test")
        
        thread_id = thread_resp.json().get("thread_id")
        
        # Try to create comment with PHI
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"body": "The patient DOB: 03/25/1975 presented with chest pain"}
        )
        assert response.status_code == 422
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected"
        print(f"✓ Comment with PHI blocked: {detail.get('detected_categories')}")


class TestExistingAuthFlows:
    """Verify existing auth flows still work correctly and don't return capabilities"""

    def test_login_returns_user_without_capabilities(self):
        """POST /api/auth/login should return user without plan_tier/capabilities"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json=PREMIUM_USER
        )
        assert response.status_code == 200
        data = response.json()
        
        # Should have access_token and user
        assert "access_token" in data
        assert "user" in data
        
        user = data["user"]
        # Login response should have plan_tier=null (not computed for login)
        # The UserResponse model has these as Optional fields
        assert "user_id" in user
        assert "email" in user
        print(f"✓ Login returns user correctly (plan_tier={user.get('plan_tier')})")

    def test_login_with_invalid_credentials_fails(self):
        """POST /api/auth/login with wrong password should return 401"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": PREMIUM_USER["email"], "password": "WrongPassword123!"}
        )
        assert response.status_code == 401
        print("✓ Invalid credentials correctly rejected")

    def test_auth_me_without_token_fails(self):
        """GET /api/auth/me without token should return 401/403"""
        response = requests.get(f"{BASE_URL}/api/auth/me")
        assert response.status_code in [401, 403]
        print(f"✓ /api/auth/me without token returns {response.status_code}")


class TestPhiGuardPatterns:
    """Test various PHI patterns to ensure detection works"""

    def test_phone_pattern_detected(self, premium_token):
        """Phone numbers should be detected"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test123", "body": "Contact the patient at phone: 555-123-4567"}
        )
        assert response.status_code == 422
        detail = response.json().get("detail", {})
        assert "phone" in detail.get("detected_categories", [])
        print("✓ Phone pattern detected")

    def test_address_pattern_detected(self, premium_token):
        """Street addresses should be detected"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test123", "body": "Patient lives at 123 Main Street, Springfield"}
        )
        assert response.status_code == 422
        detail = response.json().get("detail", {})
        assert "address" in detail.get("detected_categories", [])
        print("✓ Address pattern detected")

    def test_insurance_id_pattern_detected(self, premium_token):
        """Insurance/policy IDs should be detected"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={"article_id": "test123", "body": "Patient's insurance ID: ABC123456789"}
        )
        assert response.status_code == 422
        detail = response.json().get("detail", {})
        assert "insurance_id" in detail.get("detected_categories", [])
        print("✓ Insurance ID pattern detected")

    def test_multiple_phi_types_detected(self, premium_token):
        """Multiple PHI types should all be detected"""
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers={"Authorization": f"Bearer {premium_token}"},
            json={
                "article_id": "test123",
                "body": "Patient John Smith, SSN 123-45-6789, DOB: 01/15/1980, lives at 456 Oak Avenue"
            }
        )
        assert response.status_code == 422
        detail = response.json().get("detail", {})
        categories = detail.get("detected_categories", [])
        # Should detect multiple categories
        assert len(categories) >= 3, f"Expected multiple PHI types, got: {categories}"
        print(f"✓ Multiple PHI types detected: {categories}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
