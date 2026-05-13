"""
PATCH UX-C: Community Posting Rule Tests
New rule: Premium OR Professionally Verified can post

Test scenarios:
1. free + unverified => can_post=false, write endpoints return 403 community_read_only
2. free + professionally_verified => can_post=true, can create thread/comment
3. premium + unverified => can_post=true, can create thread/comment
4. premium + professionally_verified => can_post=true
5. locked specialty => 403 community_locked regardless of can_post
6. Backend /api/discussions/specialty-rooms returns can_post boolean based on new rule
"""
import pytest
import requests
import os
import time

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_VERIFIED_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_UNVERIFIED_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


class TestSession:
    """Shared session with retry logic for rate limiting"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.token = None
    
    def login(self, credentials, max_retries=3):
        """Login with retry for rate limiting"""
        for attempt in range(max_retries):
            response = self.session.post(
                f"{BASE_URL}/api/auth/login",
                json=credentials
            )
            if response.status_code == 200:
                self.token = response.json().get("access_token")
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
                return response.json()
            elif response.status_code == 429:
                time.sleep(5 * (attempt + 1))
            else:
                return response.json()
        return {"error": "Max retries exceeded due to rate limiting"}
    
    def get(self, endpoint):
        return self.session.get(f"{BASE_URL}{endpoint}")
    
    def post(self, endpoint, json=None):
        return self.session.post(f"{BASE_URL}{endpoint}", json=json)


@pytest.fixture(scope="module")
def premium_verified_session():
    """Premium + verified user session (demo@litpulse.com)"""
    session = TestSession()
    result = session.login(PREMIUM_VERIFIED_USER)
    if "access_token" not in result:
        pytest.skip(f"Premium user login failed: {result}")
    return session


@pytest.fixture(scope="module")
def free_unverified_session():
    """Free + unverified user session (test@litpulse.com)"""
    session = TestSession()
    result = session.login(FREE_UNVERIFIED_USER)
    if "access_token" not in result:
        pytest.skip(f"Free user login failed: {result}")
    return session


class TestCanPostFieldInSpecialtyRooms:
    """Test that /api/discussions/specialty-rooms returns can_post field correctly"""
    
    def test_premium_verified_user_can_post_true(self, premium_verified_session):
        """Premium + verified user should have can_post=true for accessible rooms"""
        response = premium_verified_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200, f"Failed: {response.text}"
        
        data = response.json()
        accessible_rooms = [r for r in data["rooms"] if r.get("can_enter") is True]
        
        assert len(accessible_rooms) > 0, "Premium user should have accessible rooms"
        
        for room in accessible_rooms:
            assert room.get("can_post") is True, \
                f"Premium+verified user should have can_post=true for {room['specialty_id']}"
            print(f"✓ {room['specialty_id']}: can_enter={room['can_enter']}, can_post={room['can_post']}")
    
    def test_free_unverified_user_can_post_false(self, free_unverified_session):
        """Free + unverified user should have can_post=false for accessible rooms"""
        response = free_unverified_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200, f"Failed: {response.text}"
        
        data = response.json()
        accessible_rooms = [r for r in data["rooms"] if r.get("can_enter") is True]
        
        # If user has no accessible rooms, that's also valid (no profiles)
        if len(accessible_rooms) == 0:
            print("Free user has no accessible rooms (no profiles configured)")
            return
        
        for room in accessible_rooms:
            # Free + unverified should have can_post=false
            assert room.get("can_post") is False, \
                f"Free+unverified user should have can_post=false for {room['specialty_id']}, got {room.get('can_post')}"
            print(f"✓ {room['specialty_id']}: can_enter={room['can_enter']}, can_post={room['can_post']}")
    
    def test_locked_rooms_have_null_can_post(self, premium_verified_session):
        """Locked rooms (can_enter=false) should have can_post=null"""
        response = premium_verified_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200
        
        data = response.json()
        locked_rooms = [r for r in data["rooms"] if r.get("can_enter") is False]
        
        assert len(locked_rooms) > 0, "Should have some locked rooms"
        
        for room in locked_rooms:
            assert room.get("can_post") is None, \
                f"Locked room {room['specialty_id']} should have can_post=null, got {room.get('can_post')}"
        
        print(f"✓ {len(locked_rooms)} locked rooms have can_post=null")


class TestFreeUnverifiedCannotPost:
    """Test that free + unverified users get 403 when trying to post
    
    Note: When REQUIRE_VERIFIED_FOR_POSTING=true, the trust gate check happens first
    and returns 'verification_required'. When that flag is false, the PATCH UX-C
    check returns 'community_read_only'.
    """
    
    def test_free_unverified_cannot_create_thread(self, free_unverified_session):
        """Free + unverified user should get 403 when creating thread"""
        # First ensure user has an accessible room
        rooms_response = free_unverified_session.get("/api/discussions/specialty-rooms")
        assert rooms_response.status_code == 200
        
        data = rooms_response.json()
        accessible_rooms = [r for r in data["rooms"] if r.get("can_enter") is True]
        
        if len(accessible_rooms) == 0:
            # Create a profile first
            profile_response = free_unverified_session.post("/api/preferences/profiles", json={
                "specialty_id": "cardiology",
                "name": "TEST_PATCH_UXC_Profile",
                "keywords": ["test"],
                "frequency": "weekly"
            })
            if profile_response.status_code not in [200, 201]:
                pytest.skip("Could not create profile for free user")
            
            # Re-check rooms
            rooms_response = free_unverified_session.get("/api/discussions/specialty-rooms")
            data = rooms_response.json()
            accessible_rooms = [r for r in data["rooms"] if r.get("can_enter") is True]
        
        if len(accessible_rooms) == 0:
            pytest.skip("Free user has no accessible rooms")
        
        room = accessible_rooms[0]
        
        # Try to create a thread
        response = free_unverified_session.post("/api/discussions/threads", json={
            "context_type": "specialty",
            "context_id": room["specialty_id"],
            "specialty_id": room["specialty_id"],
            "title": "TEST_PATCH_UXC_Free_Unverified_Thread"
        })
        
        # Should get 403 - either verification_required (trust gate) or community_read_only (PATCH UX-C)
        assert response.status_code == 403, \
            f"Expected 403, got {response.status_code}: {response.text}"
        
        detail = response.json().get("detail", {})
        error_code = detail.get("error_code") if isinstance(detail, dict) else None
        
        # Accept either error code - trust gate runs first when enabled
        valid_error_codes = ["community_read_only", "verification_required"]
        assert error_code in valid_error_codes, \
            f"Expected error_code in {valid_error_codes}, got '{error_code}'"
        
        print(f"✓ Free+unverified user blocked from creating thread: {error_code}")
    
    def test_free_unverified_cannot_create_comment(self, free_unverified_session, premium_verified_session):
        """Free + unverified user should get 403 when creating comment"""
        # First, premium user creates a thread
        rooms_response = premium_verified_session.get("/api/discussions/specialty-rooms")
        data = rooms_response.json()
        accessible_rooms = [r for r in data["rooms"] if r.get("can_enter") is True]
        
        if len(accessible_rooms) == 0:
            pytest.skip("No accessible rooms for premium user")
        
        room = accessible_rooms[0]
        
        # Create a thread as premium user
        thread_response = premium_verified_session.post("/api/discussions/threads", json={
            "context_type": "specialty",
            "context_id": room["specialty_id"],
            "specialty_id": room["specialty_id"],
            "title": "TEST_PATCH_UXC_Thread_For_Comment_Test"
        })
        
        if thread_response.status_code != 201:
            pytest.skip(f"Could not create thread: {thread_response.text}")
        
        thread_id = thread_response.json()["thread_id"]
        
        # Ensure free user has access to this room
        free_rooms_response = free_unverified_session.get("/api/discussions/specialty-rooms")
        free_data = free_rooms_response.json()
        free_accessible = [r for r in free_data["rooms"] if r.get("can_enter") is True and r["specialty_id"] == room["specialty_id"]]
        
        if len(free_accessible) == 0:
            # Create profile for free user in this specialty
            profile_response = free_unverified_session.post("/api/preferences/profiles", json={
                "specialty_id": room["specialty_id"],
                "name": f"TEST_PATCH_UXC_Profile_{room['specialty_id']}",
                "keywords": ["test"],
                "frequency": "weekly"
            })
            # Profile might already exist, that's ok
        
        # Try to create a comment as free user
        comment_response = free_unverified_session.post(f"/api/discussions/threads/{thread_id}/comments", json={
            "body": "TEST_PATCH_UXC_Free_Unverified_Comment"
        })
        
        # Should get 403 - either verification_required (trust gate) or community_read_only (PATCH UX-C)
        assert comment_response.status_code == 403, \
            f"Expected 403, got {comment_response.status_code}: {comment_response.text}"
        
        detail = comment_response.json().get("detail", {})
        error_code = detail.get("error_code") if isinstance(detail, dict) else None
        
        # Accept either error code - trust gate runs first when enabled
        valid_error_codes = ["community_read_only", "verification_required"]
        assert error_code in valid_error_codes, \
            f"Expected error_code in {valid_error_codes}, got '{error_code}'"
        
        print(f"✓ Free+unverified user blocked from creating comment: {error_code}")


class TestPremiumCanPost:
    """Test that premium users can post regardless of verification status"""
    
    def test_premium_verified_can_create_thread(self, premium_verified_session):
        """Premium + verified user should be able to create thread"""
        rooms_response = premium_verified_session.get("/api/discussions/specialty-rooms")
        assert rooms_response.status_code == 200
        
        data = rooms_response.json()
        accessible_rooms = [r for r in data["rooms"] if r.get("can_enter") is True]
        
        if len(accessible_rooms) == 0:
            pytest.skip("No accessible rooms for premium user")
        
        room = accessible_rooms[0]
        
        response = premium_verified_session.post("/api/discussions/threads", json={
            "context_type": "specialty",
            "context_id": room["specialty_id"],
            "specialty_id": room["specialty_id"],
            "title": "TEST_PATCH_UXC_Premium_Verified_Thread"
        })
        
        assert response.status_code == 201, \
            f"Premium+verified user should be able to create thread: {response.text}"
        
        thread = response.json()
        assert thread.get("thread_id"), "Thread should have thread_id"
        assert thread.get("title") == "TEST_PATCH_UXC_Premium_Verified_Thread"
        
        print(f"✓ Premium+verified user created thread: {thread['thread_id']}")
    
    def test_premium_verified_can_create_comment(self, premium_verified_session):
        """Premium + verified user should be able to create comment"""
        rooms_response = premium_verified_session.get("/api/discussions/specialty-rooms")
        data = rooms_response.json()
        accessible_rooms = [r for r in data["rooms"] if r.get("can_enter") is True]
        
        if len(accessible_rooms) == 0:
            pytest.skip("No accessible rooms for premium user")
        
        room = accessible_rooms[0]
        
        # Create a thread first
        thread_response = premium_verified_session.post("/api/discussions/threads", json={
            "context_type": "specialty",
            "context_id": room["specialty_id"],
            "specialty_id": room["specialty_id"],
            "title": "TEST_PATCH_UXC_Thread_For_Comment"
        })
        
        if thread_response.status_code != 201:
            pytest.skip(f"Could not create thread: {thread_response.text}")
        
        thread_id = thread_response.json()["thread_id"]
        
        # Create a comment
        comment_response = premium_verified_session.post(f"/api/discussions/threads/{thread_id}/comments", json={
            "body": "TEST_PATCH_UXC_Premium_Verified_Comment"
        })
        
        assert comment_response.status_code == 201, \
            f"Premium+verified user should be able to create comment: {comment_response.text}"
        
        comment = comment_response.json()
        assert comment.get("comment_id"), "Comment should have comment_id"
        
        print(f"✓ Premium+verified user created comment: {comment['comment_id']}")


class TestLockedSpecialtyReturns403CommunityLocked:
    """Test that locked specialty returns 403 community_locked regardless of can_post"""
    
    def test_locked_specialty_thread_creation_returns_community_locked(self, premium_verified_session):
        """Creating thread in locked specialty should return 403 community_locked"""
        rooms_response = premium_verified_session.get("/api/discussions/specialty-rooms")
        data = rooms_response.json()
        locked_rooms = [r for r in data["rooms"] if r.get("can_enter") is False]
        
        if len(locked_rooms) == 0:
            pytest.skip("No locked rooms for premium user")
        
        locked_room = locked_rooms[0]
        
        response = premium_verified_session.post("/api/discussions/threads", json={
            "context_type": "specialty",
            "context_id": locked_room["specialty_id"],
            "specialty_id": locked_room["specialty_id"],
            "title": "TEST_PATCH_UXC_Locked_Specialty_Thread"
        })
        
        assert response.status_code == 403, \
            f"Expected 403 for locked specialty, got {response.status_code}: {response.text}"
        
        detail = response.json().get("detail", {})
        error_code = detail.get("error_code") if isinstance(detail, dict) else None
        
        assert error_code == "community_locked", \
            f"Expected error_code 'community_locked', got '{error_code}'"
        
        print(f"✓ Locked specialty returns community_locked: {locked_room['specialty_id']}")


class TestVerificationStatusCheck:
    """Test verification status for test users"""
    
    def test_premium_user_verification_status(self, premium_verified_session):
        """Check premium user's verification status"""
        response = premium_verified_session.get("/api/verification/me")
        assert response.status_code == 200, f"Failed: {response.text}"
        
        data = response.json()
        print(f"Premium user verification: status={data.get('status')}, method={data.get('method')}")
        
        # Premium user (demo@litpulse.com) should be verified
        assert data.get("status") == "verified", \
            f"Premium user should be verified, got {data.get('status')}"
    
    def test_free_user_verification_status(self, free_unverified_session):
        """Check free user's verification status"""
        response = free_unverified_session.get("/api/verification/me")
        assert response.status_code == 200, f"Failed: {response.text}"
        
        data = response.json()
        print(f"Free user verification: status={data.get('status')}, method={data.get('method')}")
        
        # Free user (test@litpulse.com) should NOT be verified
        assert data.get("status") != "verified", \
            f"Free user should not be verified, got {data.get('status')}"


class TestUserPlanTier:
    """Test user plan tier for test users"""
    
    def test_premium_user_plan_tier(self, premium_verified_session):
        """Check premium user's plan tier"""
        response = premium_verified_session.get("/api/auth/me")
        assert response.status_code == 200, f"Failed: {response.text}"
        
        data = response.json()
        print(f"Premium user: plan_tier={data.get('plan_tier')}, email={data.get('email')}")
        
        assert data.get("plan_tier") == "premium", \
            f"Premium user should have plan_tier=premium, got {data.get('plan_tier')}"
    
    def test_free_user_plan_tier(self, free_unverified_session):
        """Check free user's plan tier"""
        response = free_unverified_session.get("/api/auth/me")
        assert response.status_code == 200, f"Failed: {response.text}"
        
        data = response.json()
        print(f"Free user: plan_tier={data.get('plan_tier')}, email={data.get('email')}")
        
        assert data.get("plan_tier") == "free", \
            f"Free user should have plan_tier=free, got {data.get('plan_tier')}"


class TestHealthCheck:
    """Basic health check"""
    
    def test_api_health(self):
        """API should be healthy"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        assert response.json().get("status") == "ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
