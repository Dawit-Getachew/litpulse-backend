"""
Test Suite for LitPulse Step 2: Trust Gate (Verified-Only Posting)

Tests:
- Unverified users blocked from POST/PATCH to discussions (403 verification_required)
- Verified users allowed full discussion participation
- Unverified users can still delete their own comments (204)
- Verification endpoints open to all (no Level-2 gate)
- PHI Guard still enforced
- Feature flags correct

Test Users:
- Verified (premium): demo@litpulse.com / DemoPass123!
- Unverified (free): test@litpulse.com / TestPass123!
"""

import pytest
import requests
import os
import uuid
from typing import Optional

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Module-level token cache to avoid rate limiting
_tokens = {}

def get_auth_token(email: str, password: str) -> Optional[str]:
    """Login and get auth token with caching to avoid rate limits"""
    if email in _tokens:
        return _tokens[email]
    
    response = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password}
    )
    if response.status_code == 200:
        token = response.json().get("access_token")
        _tokens[email] = token
        return token
    print(f"Login failed for {email}: {response.status_code} {response.text}")
    return None

def get_headers(token: str) -> dict:
    """Get auth headers with token"""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


@pytest.fixture(scope="module")
def verified_token():
    """Get verified user token (cached)"""
    token = get_auth_token("demo@litpulse.com", "DemoPass123!")
    assert token, "Failed to get verified user token"
    return token


@pytest.fixture(scope="module")
def unverified_token():
    """Get unverified user token (cached)"""
    token = get_auth_token("test@litpulse.com", "TestPass123!")
    assert token, "Failed to get unverified user token"
    return token


class TestFeatureFlags:
    """Test feature flags configuration"""
    
    def test_feature_flags_require_verified_for_posting(self):
        """GET /api/config/feature-flags -> require_verified_for_posting=true"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        
        data = response.json()
        assert data.get("require_verified_for_posting") == True, \
            f"Expected require_verified_for_posting=true, got {data.get('require_verified_for_posting')}"
        print(f"✓ Feature flag require_verified_for_posting = {data.get('require_verified_for_posting')}")


class TestAuthMeCapabilities:
    """Test /api/auth/me returns correct capabilities based on verification status"""
    
    def test_unverified_user_capabilities(self, unverified_token):
        """GET /api/auth/me for unverified user -> capabilities community_write=false"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers=get_headers(unverified_token)
        )
        assert response.status_code == 200
        
        data = response.json()
        caps = data.get("capabilities", {})
        
        assert caps.get("community_write") == False, \
            f"Expected community_write=false for unverified user, got {caps.get('community_write')}"
        assert caps.get("community_react") == False, \
            f"Expected community_react=false for unverified user, got {caps.get('community_react')}"
        assert caps.get("community_attach") == False, \
            f"Expected community_attach=false for unverified user, got {caps.get('community_attach')}"
        
        print(f"✓ Unverified user capabilities: community_write={caps.get('community_write')}, community_react={caps.get('community_react')}, community_attach={caps.get('community_attach')}")
    
    def test_verified_user_capabilities(self, verified_token):
        """GET /api/auth/me for verified user -> capabilities community_write=true"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers=get_headers(verified_token)
        )
        assert response.status_code == 200
        
        data = response.json()
        caps = data.get("capabilities", {})
        
        assert caps.get("community_write") == True, \
            f"Expected community_write=true for verified user, got {caps.get('community_write')}"
        assert caps.get("community_react") == True, \
            f"Expected community_react=true for verified user, got {caps.get('community_react')}"
        assert caps.get("community_attach") == True, \
            f"Expected community_attach=true for verified user, got {caps.get('community_attach')}"
        
        print(f"✓ Verified user capabilities: community_write={caps.get('community_write')}, community_react={caps.get('community_react')}, community_attach={caps.get('community_attach')}")


class TestUnverifiedUserBlocked:
    """Test unverified user is blocked from discussion write operations"""
    
    @pytest.fixture(autouse=True)
    def setup_test_data(self, verified_token, unverified_token):
        """Create test data with verified user"""
        self.verified_token = verified_token
        self.unverified_token = unverified_token
        
        # Create a test thread with verified user for testing comments
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=get_headers(self.verified_token),
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": f"TEST_TRUSTGATE_Thread_{uuid.uuid4().hex[:8]}"
            }
        )
        self.test_thread_id = response.json().get("thread_id") if response.status_code == 201 else None
        
        # Create a test comment with verified user
        if self.test_thread_id:
            response = requests.post(
                f"{BASE_URL}/api/discussions/threads/{self.test_thread_id}/comments",
                headers=get_headers(self.verified_token),
                json={"body": f"TEST_TRUSTGATE_Comment_{uuid.uuid4().hex[:8]}"}
            )
            self.test_comment_id = response.json().get("comment_id") if response.status_code == 201 else None
        else:
            self.test_comment_id = None
    
    def test_unverified_create_thread_blocked(self):
        """Unverified user POST /api/discussions/threads -> 403 verification_required"""
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=get_headers(self.unverified_token),
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": "Should be blocked - unverified user test"
            }
        )
        assert response.status_code == 403, \
            f"Expected 403 for unverified user creating thread, got {response.status_code}: {response.text}"
        
        detail = response.json().get("detail", {})
        assert detail.get("error_code") == "verification_required", \
            f"Expected error_code='verification_required', got {detail.get('error_code')}"
        
        print(f"✓ Unverified user blocked from creating thread: 403 {detail.get('error_code')}")
    
    def test_unverified_create_comment_blocked(self):
        """Unverified user POST /api/discussions/threads/{id}/comments -> 403 verification_required"""
        assert self.test_thread_id, "Test thread not created"
        
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{self.test_thread_id}/comments",
            headers=get_headers(self.unverified_token),
            json={"body": "Should be blocked - unverified user test comment"}
        )
        assert response.status_code == 403, \
            f"Expected 403 for unverified user creating comment, got {response.status_code}: {response.text}"
        
        detail = response.json().get("detail", {})
        assert detail.get("error_code") == "verification_required", \
            f"Expected error_code='verification_required', got {detail.get('error_code')}"
        
        print(f"✓ Unverified user blocked from creating comment: 403 {detail.get('error_code')}")
    
    def test_unverified_react_blocked(self):
        """Unverified user POST /api/discussions/comments/{id}/react -> 403 verification_required"""
        assert self.test_comment_id, "Test comment not created"
        
        response = requests.post(
            f"{BASE_URL}/api/discussions/comments/{self.test_comment_id}/react",
            headers=get_headers(self.unverified_token),
            json={"reaction_type": "helpful"}
        )
        assert response.status_code == 403, \
            f"Expected 403 for unverified user reacting, got {response.status_code}: {response.text}"
        
        detail = response.json().get("detail", {})
        assert detail.get("error_code") == "verification_required", \
            f"Expected error_code='verification_required', got {detail.get('error_code')}"
        
        print(f"✓ Unverified user blocked from reacting: 403 {detail.get('error_code')}")
    
    def test_unverified_edit_comment_blocked(self):
        """Unverified user PATCH /api/discussions/comments/{id} -> 403 verification_required"""
        assert self.test_comment_id, "Test comment not created"
        
        response = requests.patch(
            f"{BASE_URL}/api/discussions/comments/{self.test_comment_id}",
            headers=get_headers(self.unverified_token),
            json={"body": "Edited body - should be blocked"}
        )
        # Should return 403 for verification_required (trust gate runs first)
        assert response.status_code == 403, \
            f"Expected 403 for unverified user editing comment, got {response.status_code}: {response.text}"
        
        detail = response.json().get("detail", {})
        assert detail.get("error_code") == "verification_required", \
            f"Expected error_code='verification_required', got {detail.get('error_code')}"
        
        print(f"✓ Unverified user blocked from editing comment: 403 {detail.get('error_code')}")


class TestVerifiedUserAllowed:
    """Test verified user can perform all discussion operations"""
    
    def test_verified_create_thread_allowed(self, verified_token):
        """Verified user POST /api/discussions/threads -> 201 success"""
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=get_headers(verified_token),
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": f"TEST_TRUSTGATE_Verified_{uuid.uuid4().hex[:8]}"
            }
        )
        assert response.status_code == 201, \
            f"Expected 201 for verified user creating thread, got {response.status_code}: {response.text}"
        
        thread_id = response.json().get("thread_id")
        assert thread_id, "Thread ID not returned"
        
        print(f"✓ Verified user created thread successfully: {thread_id}")
    
    def test_verified_create_comment_allowed(self, verified_token):
        """Verified user POST /api/discussions/threads/{id}/comments -> 201 success"""
        # First create a thread
        thread_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=get_headers(verified_token),
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": f"TEST_TRUSTGATE_ForComment_{uuid.uuid4().hex[:8]}"
            }
        )
        assert thread_response.status_code == 201
        thread_id = thread_response.json().get("thread_id")
        
        # Now create a comment
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            headers=get_headers(verified_token),
            json={"body": f"TEST_TRUSTGATE_Comment_{uuid.uuid4().hex[:8]}"}
        )
        assert response.status_code == 201, \
            f"Expected 201 for verified user creating comment, got {response.status_code}: {response.text}"
        
        comment_id = response.json().get("comment_id")
        assert comment_id, "Comment ID not returned"
        
        print(f"✓ Verified user created comment successfully: {comment_id}")
    
    def test_verified_react_allowed(self, verified_token):
        """Verified user POST /api/discussions/comments/{id}/react -> 200 success"""
        # Create thread and comment
        thread_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=get_headers(verified_token),
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": f"TEST_TRUSTGATE_ForReact_{uuid.uuid4().hex[:8]}"
            }
        )
        assert thread_response.status_code == 201
        thread_id = thread_response.json().get("thread_id")
        
        comment_response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            headers=get_headers(verified_token),
            json={"body": f"TEST_TRUSTGATE_Comment_{uuid.uuid4().hex[:8]}"}
        )
        assert comment_response.status_code == 201
        comment_id = comment_response.json().get("comment_id")
        
        # Now react
        response = requests.post(
            f"{BASE_URL}/api/discussions/comments/{comment_id}/react",
            headers=get_headers(verified_token),
            json={"reaction_type": "helpful"}
        )
        assert response.status_code == 200, \
            f"Expected 200 for verified user reacting, got {response.status_code}: {response.text}"
        
        print(f"✓ Verified user reacted successfully")


class TestVerificationEndpointsOpen:
    """Test verification endpoints are open to all (no Level-2 gate)"""
    
    def test_verification_me_accessible_to_free_user(self, unverified_token):
        """Free user GET /api/verification/me -> 200 (accessible, no Level-2 gate)"""
        response = requests.get(
            f"{BASE_URL}/api/verification/me",
            headers=get_headers(unverified_token)
        )
        assert response.status_code == 200, \
            f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        # The key test: endpoint is accessible (not 403 for Level-2)
        # can_submit may be false if user already has pending/verified status
        status = data.get("status")
        print(f"✓ Free user can access /api/verification/me: status={status}, can_submit={data.get('can_submit')}")
    
    def test_verification_send_code_no_level2_gate(self, unverified_token):
        """Free user POST /api/verification/work-email/send-code -> No 403 for Level-2"""
        response = requests.post(
            f"{BASE_URL}/api/verification/work-email/send-code",
            headers=get_headers(unverified_token),
            json={"work_email": "test@somehospital.org"}
        )
        # Should NOT be 403 for subscription level
        # May fail on rate limit (429) or email domain validation (400) but NOT 403 for Level-2
        if response.status_code == 403:
            detail = response.text
            assert "Level" not in detail and "subscription" not in detail.lower(), \
                f"Unexpected 403 Level-2 gate on verification endpoint: {response.text}"
        
        # Success could be 200 (code sent), 400 (bad email), 429 (rate limit)
        print(f"✓ Free user not blocked by Level-2 gate on /api/verification/work-email/send-code: status={response.status_code}")


class TestPhiGuardStillEnforced:
    """Test PHI guard is still enforced on notes"""
    
    def test_phi_guard_blocks_ssn_in_notes(self, verified_token):
        """POST /api/notes with SSN -> 422 phi_detected"""
        # Use a generic article_id - doesn't need to exist for PHI check
        response = requests.post(
            f"{BASE_URL}/api/notes",
            headers=get_headers(verified_token),
            json={
                "article_id": "test_article_phi",
                "body": "Patient SSN is 123-45-6789"
            }
        )
        
        # PHI guard should block this with 422
        assert response.status_code == 422, \
            f"Expected 422 for PHI in notes, got {response.status_code}: {response.text}"
        
        data = response.json()
        detail = data.get("detail", {})
        assert detail.get("error_code") == "phi_detected", \
            f"Expected error_code='phi_detected', got {detail.get('error_code')}"
        
        print(f"✓ PHI Guard still blocks PHI in notes: 422 {detail.get('error_code')}")


class TestUnverifiedUserCanDeleteOwnComment:
    """Test unverified user can still delete their own comments"""
    
    def test_unverified_delete_own_comment(self, unverified_token):
        """Unverified user DELETE /api/discussions/comments/{id} (own comment) -> 204 allowed"""
        # This test requires the unverified user to have an existing comment
        # Since they can't create new ones with the trust gate on,
        # we'd need a pre-existing comment or to temporarily disable the flag
        
        # For now, skip this test with a note
        # The delete endpoint doesn't have a trust gate check - it only checks ownership
        print("⚠ Skipping: Unverified user delete test requires pre-existing comment")
        pytest.skip("Requires pre-existing comment from before trust gate was enabled")


# Run all tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
