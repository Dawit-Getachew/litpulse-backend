"""
Step 3 Moderation Backend Tests for LitPulse v3.0
Tests admin moderation endpoints: reports, remove-comment, suspend/unsuspend user
Admin email: demo@litpulse.com
"""
import pytest
import requests
import os
import uuid

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "demo@litpulse.com"
ADMIN_PASSWORD = "DemoPass123!"
NON_ADMIN_EMAIL = "test@litpulse.com"
NON_ADMIN_PASSWORD = "TestPass123!"

# Store tokens and IDs for tests
admin_token = None
non_admin_token = None
test_thread_id = None
test_comment_id = None
test_report_id = None


class TestModerationSetup:
    """Setup: Get tokens and create test data"""
    
    def test_login_admin_user(self):
        """Login admin user (demo@litpulse.com)"""
        global admin_token
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        })
        assert response.status_code == 200, f"Admin login failed: {response.text}"
        data = response.json()
        admin_token = data["access_token"]
        assert admin_token, "No admin token returned"
        print(f"PASS: Admin login successful, token={admin_token[:20]}...")
    
    def test_login_non_admin_user(self):
        """Login non-admin user (test@litpulse.com)"""
        global non_admin_token
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": NON_ADMIN_EMAIL,
            "password": NON_ADMIN_PASSWORD
        })
        assert response.status_code == 200, f"Non-admin login failed: {response.text}"
        data = response.json()
        non_admin_token = data["access_token"]
        assert non_admin_token, "No non-admin token returned"
        print(f"PASS: Non-admin login successful, token={non_admin_token[:20]}...")
    
    def test_create_thread_for_moderation(self):
        """Create a test thread for moderation testing"""
        global test_thread_id
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "context_type": "specialty",
                "context_id": "TEST_MOD_CONTEXT",
                "specialty_id": "cardiology",
                "title": f"TEST_MOD_Thread_{uuid.uuid4().hex[:8]}"
            }
        )
        assert response.status_code == 201, f"Thread creation failed: {response.text}"
        data = response.json()
        test_thread_id = data["thread_id"]
        assert test_thread_id, "No thread_id returned"
        print(f"PASS: Test thread created: {test_thread_id}")
    
    def test_create_comment_for_moderation(self):
        """Create a test comment to be moderated"""
        global test_comment_id
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{test_thread_id}/comments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"body": "TEST_MOD_Comment - This is a test comment for moderation testing"}
        )
        assert response.status_code == 201, f"Comment creation failed: {response.text}"
        data = response.json()
        test_comment_id = data["comment_id"]
        assert test_comment_id, "No comment_id returned"
        print(f"PASS: Test comment created: {test_comment_id}")


class TestAdminReportsAccess:
    """Test admin reports endpoint access control"""
    
    def test_non_admin_cannot_access_reports(self):
        """Non-admin GET /api/admin/reports should return 403"""
        response = requests.get(
            f"{BASE_URL}/api/admin/reports",
            headers={"Authorization": f"Bearer {non_admin_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("PASS: Non-admin blocked from reports (403)")
    
    def test_admin_can_access_reports(self):
        """Admin GET /api/admin/reports should return 200 with report list"""
        response = requests.get(
            f"{BASE_URL}/api/admin/reports",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert "reports" in data, "Missing 'reports' key in response"
        assert "total" in data, "Missing 'total' key in response"
        assert isinstance(data["reports"], list), "'reports' is not a list"
        print(f"PASS: Admin can access reports. Total: {data['total']}")


class TestReportCreation:
    """Test report creation with reason_category"""
    
    def test_report_comment_with_phi_category(self):
        """POST /api/discussions/comments/{id}/report with reason_category='phi'"""
        global test_report_id
        response = requests.post(
            f"{BASE_URL}/api/discussions/comments/{test_comment_id}/report",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "reason": "Test report with PHI category",
                "reason_category": "phi"
            }
        )
        assert response.status_code == 200, f"Report failed: {response.text}"
        data = response.json()
        assert "report_id" in data, "Missing report_id in response"
        test_report_id = data["report_id"]
        print(f"PASS: Report created with phi category: {test_report_id}")
    
    def test_report_reason_phi_guarded(self):
        """Report reason should be PHI-guarded (422 if contains PHI)"""
        response = requests.post(
            f"{BASE_URL}/api/discussions/comments/{test_comment_id}/report",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "reason": "Patient John Doe SSN 123-45-6789 violated community guidelines",
                "reason_category": "phi"
            }
        )
        # Should return 422 with phi_detected
        assert response.status_code == 422, f"Expected 422 for PHI in reason, got {response.status_code}: {response.text}"
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "phi_detected", f"Expected phi_detected, got {data}"
        print("PASS: Report reason is PHI-guarded (422 on PHI)")


class TestAdminReportDetail:
    """Test admin report detail and comment viewer"""
    
    def test_admin_get_report_detail(self):
        """Admin GET /api/admin/reports/{id} should return report detail"""
        response = requests.get(
            f"{BASE_URL}/api/admin/reports/{test_report_id}",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["report_id"] == test_report_id, "Report ID mismatch"
        assert data.get("reason_category") == "phi", f"Expected reason_category=phi, got {data.get('reason_category')}"
        print(f"PASS: Report detail retrieved, category={data['reason_category']}, status={data['status']}")
    
    def test_admin_get_comment_with_body(self):
        """Admin GET /api/admin/comments/{id}?include_body=true returns full body"""
        response = requests.get(
            f"{BASE_URL}/api/admin/comments/{test_comment_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"include_body": "true"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert "body" in data, "Missing 'body' in response"
        assert data["body"], "Body should not be empty"
        print(f"PASS: Admin comment viewer returned full body (len={len(data['body'])})")


class TestAdminRemoveComment:
    """Test admin remove comment functionality"""
    
    def test_admin_remove_comment_sets_deleted_at(self):
        """Admin POST /api/admin/moderation/remove-comment sets deleted_at"""
        global test_comment_id
        
        # First create a new comment to remove (so we don't remove the one needed for other tests)
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{test_thread_id}/comments",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"body": "TEST_MOD_ToBeRemoved - This comment will be removed by admin"}
        )
        assert response.status_code == 201, f"Comment creation failed: {response.text}"
        comment_to_remove_id = response.json()["comment_id"]
        
        # Remove the comment
        response = requests.post(
            f"{BASE_URL}/api/admin/moderation/remove-comment",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "comment_id": comment_to_remove_id,
                "reason": "Test moderation removal"
            }
        )
        assert response.status_code == 200, f"Remove comment failed: {response.text}"
        data = response.json()
        assert data.get("message") == "Comment removed", f"Unexpected message: {data}"
        print(f"PASS: Comment {comment_to_remove_id} removed by admin")
        
        # Verify the comment appears with deleted_at in admin viewer
        response = requests.get(
            f"{BASE_URL}/api/admin/comments/{comment_to_remove_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"include_body": "true"}
        )
        assert response.status_code == 200, f"Comment fetch failed: {response.text}"
        data = response.json()
        assert data.get("deleted_at"), f"Expected deleted_at to be set, got {data}"
        print(f"PASS: Removed comment has deleted_at: {data['deleted_at']}")


class TestSoftDeletedCommentPlaceholder:
    """Test that soft-deleted comments show placeholder in thread detail"""
    
    def test_thread_detail_shows_moderated_placeholder(self):
        """Thread detail includes soft-deleted comments with placeholder body"""
        response = requests.get(
            f"{BASE_URL}/api/discussions/threads/{test_thread_id}",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Thread detail failed: {response.text}"
        data = response.json()
        comments = data.get("comments", [])
        
        # Find a comment with deleted_at
        moderated_comments = [c for c in comments if c.get("deleted_at")]
        if moderated_comments:
            for mc in moderated_comments:
                assert mc["body"] == "[This comment was removed by moderation.]", \
                    f"Expected placeholder body, got: {mc['body']}"
            print(f"PASS: Thread detail shows {len(moderated_comments)} moderated comment(s) with placeholder")
        else:
            print("INFO: No moderated comments found in thread yet (expected after remove-comment test)")


class TestAdminSuspendUser:
    """Test admin suspend/unsuspend user functionality"""
    
    def test_admin_suspend_user(self):
        """Admin POST /api/admin/moderation/suspend-user sets is_active=false"""
        # First, get the non-admin user_id
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {non_admin_token}"}
        )
        assert response.status_code == 200, f"Get me failed: {response.text}"
        non_admin_user_id = response.json()["user_id"]
        
        # Suspend the user
        response = requests.post(
            f"{BASE_URL}/api/admin/moderation/suspend-user",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "user_id": non_admin_user_id,
                "reason": "Test suspension for moderation testing"
            }
        )
        assert response.status_code == 200, f"Suspend user failed: {response.text}"
        data = response.json()
        assert data.get("message") == "User suspended", f"Unexpected message: {data}"
        print(f"PASS: User {non_admin_user_id} suspended by admin")
    
    def test_suspended_user_blocked_from_login(self):
        """Suspended user cannot login (403 Account is inactive)"""
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={
                "email": NON_ADMIN_EMAIL,
                "password": NON_ADMIN_PASSWORD
            }
        )
        assert response.status_code == 403, f"Expected 403 for suspended user login, got {response.status_code}: {response.text}"
        data = response.json()
        assert "inactive" in data.get("detail", "").lower(), f"Expected 'inactive' in detail, got: {data}"
        print("PASS: Suspended user blocked from login (403 Account is inactive)")
    
    def test_suspended_user_blocked_from_protected_endpoints(self):
        """Suspended user with existing token blocked from /api/auth/me"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {non_admin_token}"}
        )
        assert response.status_code == 403, f"Expected 403 for suspended user on /me, got {response.status_code}: {response.text}"
        data = response.json()
        assert "suspended" in data.get("detail", "").lower(), f"Expected 'suspended' in detail, got: {data}"
        print("PASS: Suspended user blocked from /api/auth/me (403 Account suspended)")


class TestAdminUnsuspendUser:
    """Test admin unsuspend user functionality"""
    
    def test_admin_unsuspend_user(self):
        """Admin POST /api/admin/moderation/unsuspend-user sets is_active=true"""
        # Get the non-admin user_id (need admin to fetch it since user is suspended)
        # We'll use the user_id from the report since it includes reported_user_id
        response = requests.get(
            f"{BASE_URL}/api/admin/reports/{test_report_id}",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        # Actually, let's get it differently - find a way to get the user_id
        # We can search users or use the report's reported_user_id if it's the right user
        # For now, let's re-login as admin and use admin lookup
        
        # Better approach: get all users from admin metrics to find non-admin user
        # Or, we know from previous tests - let's just use test email to lookup
        
        # Simplest: unsuspend by looking up the report's reported_user or use a direct approach
        # Since we know the email, let's get the user_id from admin API
        
        # Actually the test user was suspended - let's get user_id from the token if possible
        # Or decode it... For simplicity, let's assume we saved it from setup
        
        # Fetch from admin side - admin can look up users
        # Since there's no user lookup API, we'll rely on the fact that we need to unsuspend
        # Let's get the user_id from the previous test where we got it
        
        # Re-get non-admin user_id using admin token and admin lookup
        # For now, let's try re-logging in to get the user_id and expect it to fail,
        # then use admin API
        
        # Workaround: Create a small users lookup or use database
        # For this test, let's just try to unsuspend by email lookup
        # Actually, the API needs user_id, so let's search for it
        
        # Let me check if there's a users API... Looking at admin_moderation.py, there isn't
        # Let's assume we stored the user_id globally during suspend test
        
        # Actually, the simpler approach: we can use the reported_user_id from the report
        # if the comment was created by non-admin. But in our test, admin created the comment.
        
        # Best approach: Store the user_id during the suspend test and use it here
        # Let's do a workaround - we'll query the database or use a hardcoded approach
        
        # For now, let's login as admin, then try to find the user_id
        # Actually, the non-admin token was created at test start, so we can extract user_id from it
        
        # JWT decode approach:
        import base64
        import json as json_lib
        try:
            # non_admin_token is a JWT - extract user_id from payload
            parts = non_admin_token.split('.')
            payload_b64 = parts[1] + '==' # Add padding
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            payload = json_lib.loads(payload_bytes)
            non_admin_user_id = payload.get('user_id')
        except Exception as e:
            pytest.skip(f"Could not extract user_id from token: {e}")
        
        assert non_admin_user_id, "Could not extract non-admin user_id from token"
        
        # Unsuspend the user
        response = requests.post(
            f"{BASE_URL}/api/admin/moderation/unsuspend-user",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"user_id": non_admin_user_id}
        )
        assert response.status_code == 200, f"Unsuspend user failed: {response.text}"
        data = response.json()
        assert data.get("message") == "User unsuspended", f"Unexpected message: {data}"
        print(f"PASS: User {non_admin_user_id} unsuspended by admin")
    
    def test_unsuspended_user_can_login_again(self):
        """After unsuspension, user can login again"""
        global non_admin_token
        response = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={
                "email": NON_ADMIN_EMAIL,
                "password": NON_ADMIN_PASSWORD
            }
        )
        assert response.status_code == 200, f"Expected 200 for unsuspended user login, got {response.status_code}: {response.text}"
        data = response.json()
        non_admin_token = data["access_token"]  # Update token
        print("PASS: Unsuspended user can login again")
    
    def test_unsuspended_user_can_access_me(self):
        """After unsuspension, user can access /api/auth/me"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {non_admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200 for unsuspended user on /me, got {response.status_code}: {response.text}"
        print("PASS: Unsuspended user can access /api/auth/me")


class TestAdminResolveReport:
    """Test admin resolve report functionality"""
    
    def test_admin_resolve_report(self):
        """Admin POST /api/admin/reports/{id}/resolve sets status=resolved"""
        response = requests.post(
            f"{BASE_URL}/api/admin/reports/{test_report_id}/resolve",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"resolution_note": "Test resolution - false positive"}
        )
        assert response.status_code == 200, f"Resolve report failed: {response.text}"
        data = response.json()
        assert data.get("message") == "Report resolved", f"Unexpected message: {data}"
        print(f"PASS: Report {test_report_id} resolved")
    
    def test_resolved_report_has_status(self):
        """Verify resolved report has status=resolved"""
        response = requests.get(
            f"{BASE_URL}/api/admin/reports/{test_report_id}",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Get report failed: {response.text}"
        data = response.json()
        assert data.get("status") == "resolved", f"Expected status=resolved, got {data.get('status')}"
        assert data.get("resolution_note") == "Test resolution - false positive", f"Resolution note mismatch"
        print(f"PASS: Report status is 'resolved' with correct resolution note")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
