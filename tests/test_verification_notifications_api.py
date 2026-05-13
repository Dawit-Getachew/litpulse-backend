"""
Backend API Tests for LitPulse v2.1 Features
- Professional Verification (Step 2)
- In-App Inbox / Notifications (Step 3)
"""
import pytest
import requests
import os
import time
import uuid

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
LEVEL_2_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}  # subscription_level: 2, already verified
LEVEL_1_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}  # subscription_level: 1, not verified


class TestAuthSetup:
    """Test authentication and get tokens for both users"""
    
    @pytest.fixture(scope="class")
    def level_2_token(self):
        """Get auth token for Level 2 user (demo@litpulse.com)"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_2_USER)
        assert response.status_code == 200, f"Level 2 login failed: {response.text}"
        return response.json()["access_token"]
    
    @pytest.fixture(scope="class")
    def level_1_token(self):
        """Get auth token for Level 1 user (test@litpulse.com)"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_1_USER)
        assert response.status_code == 200, f"Level 1 login failed: {response.text}"
        return response.json()["access_token"]
    
    def test_level_2_login(self, level_2_token):
        """Verify Level 2 user can login"""
        assert level_2_token is not None
        assert len(level_2_token) > 0
        print(f"✓ Level 2 user login successful")
    
    def test_level_1_login(self, level_1_token):
        """Verify Level 1 user can login"""
        assert level_1_token is not None
        assert len(level_1_token) > 0
        print(f"✓ Level 1 user login successful")


class TestVerificationAPI:
    """Tests for Professional Verification endpoints (Step 2)"""
    
    @pytest.fixture(scope="class")
    def level_2_token(self):
        """Get auth token for Level 2 user"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_2_USER)
        if response.status_code != 200:
            pytest.skip(f"Level 2 login failed: {response.text}")
        return response.json()["access_token"]
    
    @pytest.fixture(scope="class")
    def level_1_token(self):
        """Get auth token for Level 1 user"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_1_USER)
        if response.status_code != 200:
            pytest.skip(f"Level 1 login failed: {response.text}")
        return response.json()["access_token"]
    
    # ============================================================
    # GET /api/verification/me - Verification Status
    # ============================================================
    
    def test_get_verification_status_level_2(self, level_2_token):
        """GET /api/verification/me - Level 2 user should see verified status"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        response = requests.get(f"{BASE_URL}/api/verification/me", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Verify response structure
        assert "status" in data
        assert "can_submit" in data
        
        # Demo user should be verified
        assert data["status"] == "verified", f"Expected 'verified', got '{data['status']}'"
        print(f"✓ Level 2 user verification status: {data['status']}")
    
    def test_get_verification_status_level_1(self, level_1_token):
        """GET /api/verification/me - Level 1 user should see not_submitted or appropriate status"""
        headers = {"Authorization": f"Bearer {level_1_token}"}
        response = requests.get(f"{BASE_URL}/api/verification/me", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Verify response structure
        assert "status" in data
        assert "can_submit" in data
        
        # Level 1 user should NOT be able to submit (can_submit should be False)
        assert data["can_submit"] == False, f"Level 1 user should not be able to submit verification"
        print(f"✓ Level 1 user verification status: {data['status']}, can_submit: {data['can_submit']}")
    
    def test_get_verification_status_unauthenticated(self):
        """GET /api/verification/me - Should return 401 without auth"""
        response = requests.get(f"{BASE_URL}/api/verification/me")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print("✓ Unauthenticated request correctly rejected")
    
    # ============================================================
    # GET /api/verification/subscription-level
    # ============================================================
    
    def test_get_subscription_level_level_2(self, level_2_token):
        """GET /api/verification/subscription-level - Level 2 user"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        response = requests.get(f"{BASE_URL}/api/verification/subscription-level", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        assert "subscription_level" in data
        assert "is_level_2" in data
        assert data["subscription_level"] >= 2, f"Expected level >= 2, got {data['subscription_level']}"
        assert data["is_level_2"] == True
        print(f"✓ Level 2 user subscription level: {data['subscription_level']}, is_level_2: {data['is_level_2']}")
    
    def test_get_subscription_level_level_1(self, level_1_token):
        """GET /api/verification/subscription-level - Level 1 user"""
        headers = {"Authorization": f"Bearer {level_1_token}"}
        response = requests.get(f"{BASE_URL}/api/verification/subscription-level", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        assert "subscription_level" in data
        assert "is_level_2" in data
        assert data["subscription_level"] == 1, f"Expected level 1, got {data['subscription_level']}"
        assert data["is_level_2"] == False
        print(f"✓ Level 1 user subscription level: {data['subscription_level']}, is_level_2: {data['is_level_2']}")
    
    # ============================================================
    # POST /api/verification/work-email/send-code
    # ============================================================
    
    def test_send_code_rejects_personal_email(self, level_2_token):
        """POST /api/verification/work-email/send-code - Should reject personal emails like gmail.com"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        # Test with Gmail (should be rejected)
        response = requests.post(
            f"{BASE_URL}/api/verification/work-email/send-code",
            headers=headers,
            json={"work_email": "test@gmail.com"}
        )
        
        assert response.status_code == 400, f"Expected 400 for personal email, got {response.status_code}"
        data = response.json()
        assert "detail" in data
        assert "institutional" in data["detail"].lower() or "work" in data["detail"].lower() or "personal" in data["detail"].lower()
        print(f"✓ Personal email (gmail.com) correctly rejected: {data['detail']}")
    
    def test_send_code_rejects_yahoo_email(self, level_2_token):
        """POST /api/verification/work-email/send-code - Should reject yahoo.com"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        response = requests.post(
            f"{BASE_URL}/api/verification/work-email/send-code",
            headers=headers,
            json={"work_email": "test@yahoo.com"}
        )
        
        assert response.status_code == 400, f"Expected 400 for yahoo email, got {response.status_code}"
        print("✓ Personal email (yahoo.com) correctly rejected")
    
    def test_send_code_level_1_forbidden(self, level_1_token):
        """POST /api/verification/work-email/send-code - Level 1 user should be forbidden"""
        headers = {"Authorization": f"Bearer {level_1_token}"}
        
        response = requests.post(
            f"{BASE_URL}/api/verification/work-email/send-code",
            headers=headers,
            json={"work_email": "test@university.edu"}
        )
        
        assert response.status_code == 403, f"Expected 403 for Level 1 user, got {response.status_code}"
        data = response.json()
        assert "detail" in data
        assert "level 2" in data["detail"].lower() or "subscriber" in data["detail"].lower()
        print(f"✓ Level 1 user correctly forbidden: {data['detail']}")
    
    # ============================================================
    # POST /api/verification/work-email/confirm
    # ============================================================
    
    def test_confirm_code_no_pending(self, level_1_token):
        """POST /api/verification/work-email/confirm - Should fail if no pending verification"""
        headers = {"Authorization": f"Bearer {level_1_token}"}
        
        response = requests.post(
            f"{BASE_URL}/api/verification/work-email/confirm",
            headers=headers,
            json={"code": "123456"}
        )
        
        # Should return 400 (no pending verification) or similar error
        assert response.status_code in [400, 404], f"Expected 400/404, got {response.status_code}"
        print(f"✓ Confirm code without pending verification correctly rejected")
    
    def test_confirm_code_invalid_format(self, level_2_token):
        """POST /api/verification/work-email/confirm - Should validate code format"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        # Test with too short code
        response = requests.post(
            f"{BASE_URL}/api/verification/work-email/confirm",
            headers=headers,
            json={"code": "123"}  # Too short
        )
        
        # Should return 422 (validation error) or 400
        assert response.status_code in [400, 422], f"Expected 400/422 for invalid code, got {response.status_code}"
        print("✓ Invalid code format correctly rejected")


class TestNotificationsAPI:
    """Tests for In-App Inbox / Notifications endpoints (Step 3)"""
    
    @pytest.fixture(scope="class")
    def level_2_token(self):
        """Get auth token for Level 2 user"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_2_USER)
        if response.status_code != 200:
            pytest.skip(f"Level 2 login failed: {response.text}")
        return response.json()["access_token"]
    
    @pytest.fixture(scope="class")
    def level_1_token(self):
        """Get auth token for Level 1 user"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_1_USER)
        if response.status_code != 200:
            pytest.skip(f"Level 1 login failed: {response.text}")
        return response.json()["access_token"]
    
    # ============================================================
    # GET /api/notifications/unread-count
    # ============================================================
    
    def test_get_unread_count(self, level_2_token):
        """GET /api/notifications/unread-count - Should return unread count"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        response = requests.get(f"{BASE_URL}/api/notifications/unread-count", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        assert "unread_count" in data
        assert isinstance(data["unread_count"], int)
        assert data["unread_count"] >= 0
        print(f"✓ Unread count for Level 2 user: {data['unread_count']}")
    
    def test_get_unread_count_level_1(self, level_1_token):
        """GET /api/notifications/unread-count - Level 1 user should also work"""
        headers = {"Authorization": f"Bearer {level_1_token}"}
        response = requests.get(f"{BASE_URL}/api/notifications/unread-count", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        assert "unread_count" in data
        print(f"✓ Unread count for Level 1 user: {data['unread_count']}")
    
    def test_get_unread_count_unauthenticated(self):
        """GET /api/notifications/unread-count - Should return 401 without auth"""
        response = requests.get(f"{BASE_URL}/api/notifications/unread-count")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print("✓ Unauthenticated request correctly rejected")
    
    # ============================================================
    # GET /api/notifications/
    # ============================================================
    
    def test_get_notifications_list(self, level_2_token):
        """GET /api/notifications/ - Should return list of notifications"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        response = requests.get(f"{BASE_URL}/api/notifications/", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Verify response structure
        assert "notifications" in data
        assert "total" in data
        assert "has_more" in data
        assert isinstance(data["notifications"], list)
        
        print(f"✓ Notifications list returned: {len(data['notifications'])} items, total: {data['total']}")
        
        # If there are notifications, verify structure
        if data["notifications"]:
            notif = data["notifications"][0]
            assert "notification_id" in notif
            assert "type" in notif
            assert "thread_id" in notif
            assert "summary_text" in notif
            assert "created_at" in notif
            assert "is_read" in notif
            print(f"✓ Notification structure verified: type={notif['type']}, is_read={notif['is_read']}")
    
    def test_get_notifications_with_limit(self, level_2_token):
        """GET /api/notifications/?limit=5 - Should respect limit parameter"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        response = requests.get(f"{BASE_URL}/api/notifications/?limit=5", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        assert len(data["notifications"]) <= 5
        print(f"✓ Limit parameter respected: {len(data['notifications'])} items returned")
    
    def test_get_notifications_unread_only(self, level_2_token):
        """GET /api/notifications/?unread_only=true - Should filter unread only"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        response = requests.get(f"{BASE_URL}/api/notifications/?unread_only=true", headers=headers)
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # All returned notifications should be unread
        for notif in data["notifications"]:
            assert notif["is_read"] == False, f"Found read notification in unread_only filter"
        
        print(f"✓ Unread only filter working: {len(data['notifications'])} unread notifications")
    
    # ============================================================
    # POST /api/notifications/mark-read
    # ============================================================
    
    def test_mark_all_read(self, level_2_token):
        """POST /api/notifications/mark-read - Mark all as read"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        response = requests.post(
            f"{BASE_URL}/api/notifications/mark-read",
            headers=headers,
            json={"mark_all": True}
        )
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        assert "message" in data
        assert "marked_count" in data
        assert isinstance(data["marked_count"], int)
        print(f"✓ Mark all read: {data['marked_count']} notifications marked")
    
    def test_mark_specific_read(self, level_2_token):
        """POST /api/notifications/mark-read - Mark specific notifications as read"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        # First get notifications to find IDs
        get_response = requests.get(f"{BASE_URL}/api/notifications/", headers=headers)
        notifications = get_response.json().get("notifications", [])
        
        if not notifications:
            pytest.skip("No notifications to mark as read")
        
        # Mark first notification as read
        notif_id = notifications[0]["notification_id"]
        response = requests.post(
            f"{BASE_URL}/api/notifications/mark-read",
            headers=headers,
            json={"notification_ids": [notif_id]}
        )
        
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        assert "message" in data
        assert "marked_count" in data
        print(f"✓ Mark specific notification read: {data['marked_count']} marked")


class TestNotificationCreation:
    """Test that notifications are created when replying to comments"""
    
    @pytest.fixture(scope="class")
    def level_2_token(self):
        """Get auth token for Level 2 user (demo)"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_2_USER)
        if response.status_code != 200:
            pytest.skip(f"Level 2 login failed: {response.text}")
        return response.json()["access_token"]
    
    @pytest.fixture(scope="class")
    def level_1_token(self):
        """Get auth token for Level 1 user (test)"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_1_USER)
        if response.status_code != 200:
            pytest.skip(f"Level 1 login failed: {response.text}")
        return response.json()["access_token"]
    
    def test_notification_created_on_reply(self, level_2_token, level_1_token):
        """
        Test that when Level 1 user replies to Level 2 user's comment,
        Level 2 user gets a notification
        """
        headers_l2 = {"Authorization": f"Bearer {level_2_token}"}
        headers_l1 = {"Authorization": f"Bearer {level_1_token}"}
        
        # Step 1: Level 2 user creates a thread
        thread_data = {
            "context_type": "specialty",
            "context_id": "cardiology",
            "specialty_id": "cardiology",
            "title": f"TEST_Notification_Thread_{uuid.uuid4().hex[:8]}"
        }
        thread_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=headers_l2,
            json=thread_data
        )
        assert thread_response.status_code == 201, f"Thread creation failed: {thread_response.text}"
        thread_id = thread_response.json()["thread_id"]
        print(f"✓ Thread created: {thread_id}")
        
        # Step 2: Level 2 user creates a comment
        comment_data = {"body": "TEST_Comment from Level 2 user for notification test"}
        comment_response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            headers=headers_l2,
            json=comment_data
        )
        assert comment_response.status_code == 201, f"Comment creation failed: {comment_response.text}"
        comment_id = comment_response.json()["comment_id"]
        print(f"✓ Comment created by Level 2 user: {comment_id}")
        
        # Step 3: Get Level 2 user's unread count before reply
        unread_before = requests.get(
            f"{BASE_URL}/api/notifications/unread-count",
            headers=headers_l2
        ).json()["unread_count"]
        print(f"✓ Level 2 unread count before reply: {unread_before}")
        
        # Step 4: Level 1 user replies to Level 2's comment
        reply_data = {
            "body": "TEST_Reply from Level 1 user - should trigger notification",
            "parent_comment_id": comment_id
        }
        reply_response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            headers=headers_l1,
            json=reply_data
        )
        assert reply_response.status_code == 201, f"Reply creation failed: {reply_response.text}"
        print(f"✓ Reply created by Level 1 user")
        
        # Step 5: Check Level 2 user's unread count after reply
        time.sleep(0.5)  # Small delay for notification to be created
        unread_after = requests.get(
            f"{BASE_URL}/api/notifications/unread-count",
            headers=headers_l2
        ).json()["unread_count"]
        print(f"✓ Level 2 unread count after reply: {unread_after}")
        
        # Verify notification was created
        assert unread_after > unread_before, f"Expected unread count to increase. Before: {unread_before}, After: {unread_after}"
        print(f"✓ Notification created successfully! Unread count increased from {unread_before} to {unread_after}")
        
        # Step 6: Verify notification content
        notifications_response = requests.get(
            f"{BASE_URL}/api/notifications/?limit=5",
            headers=headers_l2
        )
        notifications = notifications_response.json()["notifications"]
        
        # Find the notification for this thread
        thread_notifications = [n for n in notifications if n["thread_id"] == thread_id]
        assert len(thread_notifications) > 0, "No notification found for the thread"
        
        notif = thread_notifications[0]
        assert notif["type"] == "reply"
        assert "replied" in notif["summary_text"].lower()
        print(f"✓ Notification content verified: {notif['summary_text']}")


class TestVerifiedBadgeInDiscussions:
    """Test that verified badge appears in discussion responses"""
    
    @pytest.fixture(scope="class")
    def level_2_token(self):
        """Get auth token for Level 2 user (verified)"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json=LEVEL_2_USER)
        if response.status_code != 200:
            pytest.skip(f"Level 2 login failed: {response.text}")
        return response.json()["access_token"]
    
    def test_thread_creator_verified_badge(self, level_2_token):
        """Test that thread response includes creator_is_verified field"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        # Create a thread
        thread_data = {
            "context_type": "specialty",
            "context_id": "cardiology",
            "specialty_id": "cardiology",
            "title": f"TEST_VerifiedBadge_Thread_{uuid.uuid4().hex[:8]}"
        }
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=headers,
            json=thread_data
        )
        assert response.status_code == 201, f"Thread creation failed: {response.text}"
        data = response.json()
        
        # Verify creator_is_verified field exists
        assert "creator_is_verified" in data, "creator_is_verified field missing from thread response"
        assert data["creator_is_verified"] == True, f"Expected creator_is_verified=True for verified user"
        print(f"✓ Thread creator_is_verified: {data['creator_is_verified']}")
    
    def test_comment_author_verified_badge(self, level_2_token):
        """Test that comment response includes author_is_verified field"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        # First create a thread
        thread_data = {
            "context_type": "specialty",
            "context_id": "cardiology",
            "specialty_id": "cardiology",
            "title": f"TEST_CommentVerified_Thread_{uuid.uuid4().hex[:8]}"
        }
        thread_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            headers=headers,
            json=thread_data
        )
        thread_id = thread_response.json()["thread_id"]
        
        # Create a comment
        comment_data = {"body": "TEST_Comment to verify author_is_verified field"}
        comment_response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            headers=headers,
            json=comment_data
        )
        assert comment_response.status_code == 201, f"Comment creation failed: {comment_response.text}"
        data = comment_response.json()
        
        # Verify author_is_verified field exists
        assert "author_is_verified" in data, "author_is_verified field missing from comment response"
        assert data["author_is_verified"] == True, f"Expected author_is_verified=True for verified user"
        print(f"✓ Comment author_is_verified: {data['author_is_verified']}")
    
    def test_thread_detail_includes_verified_badges(self, level_2_token):
        """Test that thread detail response includes verified badges for all comments"""
        headers = {"Authorization": f"Bearer {level_2_token}"}
        
        # Get specialty threads
        response = requests.get(
            f"{BASE_URL}/api/discussions/specialties/cardiology",
            headers=headers
        )
        assert response.status_code == 200, f"Failed to get specialty threads: {response.text}"
        threads = response.json()["threads"]
        
        if not threads:
            pytest.skip("No threads in cardiology specialty")
        
        # Get thread detail
        thread_id = threads[0]["thread_id"]
        detail_response = requests.get(
            f"{BASE_URL}/api/discussions/threads/{thread_id}",
            headers=headers
        )
        assert detail_response.status_code == 200, f"Failed to get thread detail: {detail_response.text}"
        data = detail_response.json()
        
        # Verify creator_is_verified in thread
        assert "creator_is_verified" in data, "creator_is_verified missing from thread detail"
        print(f"✓ Thread detail creator_is_verified: {data['creator_is_verified']}")
        
        # Verify author_is_verified in comments
        for comment in data.get("comments", []):
            assert "author_is_verified" in comment, f"author_is_verified missing from comment {comment.get('comment_id')}"
        
        print(f"✓ All {len(data.get('comments', []))} comments have author_is_verified field")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
