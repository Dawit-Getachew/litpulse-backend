"""
Backend API Tests for LitPulse v2 Discussion/Community Features
Tests: Specialty Rooms, Threads, Comments, Reactions
"""
import pytest
import requests
import os
import time
import uuid

# Get BASE_URL from environment
BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')
if not BASE_URL:
    BASE_URL = "https://litscreen-aggregate.preview.emergentagent.com"

# Test credentials
TEST_EMAIL = "demo@litpulse.com"
TEST_PASSWORD = "DemoPass123!"


class TestDiscussionsAPI:
    """Test suite for Discussion/Community API endpoints"""
    
    @pytest.fixture(scope="class")
    def auth_token(self):
        """Get authentication token for tests"""
        # Try login with retry for rate limiting
        for attempt in range(3):
            response = requests.post(
                f"{BASE_URL}/api/auth/login",
                json={"email": TEST_EMAIL, "password": TEST_PASSWORD}
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("access_token")
            elif response.status_code == 429:
                print(f"Rate limited, waiting 60 seconds... (attempt {attempt + 1})")
                time.sleep(60)
            else:
                print(f"Login failed with status {response.status_code}: {response.text}")
                break
        
        pytest.skip("Could not authenticate - rate limited or invalid credentials")
    
    @pytest.fixture(scope="class")
    def auth_headers(self, auth_token):
        """Get headers with auth token"""
        return {"Authorization": f"Bearer {auth_token}"}
    
    # ============================================================
    # SPECIALTY ROOMS TESTS
    # ============================================================
    
    def test_get_specialty_rooms_returns_16_rooms(self, auth_headers):
        """GET /api/discussions/specialty-rooms should return 16 specialty rooms"""
        response = requests.get(
            f"{BASE_URL}/api/discussions/specialty-rooms",
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "rooms" in data, "Response should contain 'rooms' key"
        
        rooms = data["rooms"]
        assert len(rooms) == 16, f"Expected 16 specialty rooms, got {len(rooms)}"
        
        # Verify room structure
        for room in rooms:
            assert "specialty_id" in room, "Room should have specialty_id"
            assert "specialty_name" in room, "Room should have specialty_name"
            assert "thread_count" in room, "Room should have thread_count"
            assert "member_count" in room, "Room should have member_count"
        
        print(f"✅ PASS: GET /api/discussions/specialty-rooms returned {len(rooms)} rooms")
    
    def test_specialty_rooms_unauthenticated_fails(self):
        """GET /api/discussions/specialty-rooms without auth should fail"""
        response = requests.get(f"{BASE_URL}/api/discussions/specialty-rooms")
        
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print("✅ PASS: Unauthenticated request correctly rejected")
    
    # ============================================================
    # THREAD TESTS
    # ============================================================
    
    def test_create_thread(self, auth_headers):
        """POST /api/discussions/threads should create a new thread"""
        unique_title = f"TEST_Thread_{uuid.uuid4().hex[:8]}"
        
        payload = {
            "context_type": "specialty",
            "context_id": "cardiology",
            "specialty_id": "cardiology",
            "title": unique_title
        }
        
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json=payload,
            headers=auth_headers
        )
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "thread_id" in data, "Response should contain thread_id"
        assert data["title"] == unique_title, "Title should match"
        assert data["context_type"] == "specialty", "Context type should match"
        assert data["specialty_id"] == "cardiology", "Specialty ID should match"
        assert "created_by" in data, "Should have created_by"
        assert "created_at" in data, "Should have created_at"
        
        # Store thread_id for later tests
        self.__class__.created_thread_id = data["thread_id"]
        
        print(f"✅ PASS: POST /api/discussions/threads created thread {data['thread_id']}")
        return data["thread_id"]
    
    def test_get_threads_by_context(self, auth_headers):
        """GET /api/discussions/threads?context_type=specialty&context_id=cardiology should list threads"""
        response = requests.get(
            f"{BASE_URL}/api/discussions/threads",
            params={"context_type": "specialty", "context_id": "cardiology"},
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "threads" in data, "Response should contain 'threads' key"
        assert "total" in data, "Response should contain 'total' key"
        
        threads = data["threads"]
        assert isinstance(threads, list), "Threads should be a list"
        
        # Verify thread structure if any exist
        if threads:
            thread = threads[0]
            assert "thread_id" in thread, "Thread should have thread_id"
            assert "title" in thread, "Thread should have title"
            assert "context_type" in thread, "Thread should have context_type"
            assert "comment_count" in thread, "Thread should have comment_count"
        
        print(f"✅ PASS: GET /api/discussions/threads returned {len(threads)} threads")
    
    def test_get_thread_detail(self, auth_headers):
        """GET /api/discussions/threads/{thread_id} should return thread with comments"""
        # First create a thread to get its ID
        unique_title = f"TEST_DetailThread_{uuid.uuid4().hex[:8]}"
        create_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": unique_title
            },
            headers=auth_headers
        )
        
        assert create_response.status_code == 201, f"Failed to create thread: {create_response.text}"
        thread_id = create_response.json()["thread_id"]
        
        # Now get thread detail
        response = requests.get(
            f"{BASE_URL}/api/discussions/threads/{thread_id}",
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data["thread_id"] == thread_id, "Thread ID should match"
        assert data["title"] == unique_title, "Title should match"
        assert "comments" in data, "Response should contain comments"
        assert isinstance(data["comments"], list), "Comments should be a list"
        assert "comment_count" in data, "Should have comment_count"
        
        print(f"✅ PASS: GET /api/discussions/threads/{thread_id} returned thread detail")
    
    def test_get_nonexistent_thread_returns_404(self, auth_headers):
        """GET /api/discussions/threads/{invalid_id} should return 404"""
        fake_id = str(uuid.uuid4())
        response = requests.get(
            f"{BASE_URL}/api/discussions/threads/{fake_id}",
            headers=auth_headers
        )
        
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print("✅ PASS: Nonexistent thread correctly returns 404")
    
    # ============================================================
    # COMMENT TESTS
    # ============================================================
    
    def test_create_comment(self, auth_headers):
        """POST /api/discussions/threads/{thread_id}/comments should add comment"""
        # First create a thread
        unique_title = f"TEST_CommentThread_{uuid.uuid4().hex[:8]}"
        create_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": unique_title
            },
            headers=auth_headers
        )
        
        assert create_response.status_code == 201
        thread_id = create_response.json()["thread_id"]
        
        # Create comment
        comment_body = f"TEST_Comment_{uuid.uuid4().hex[:8]}: This is a test comment for the discussion."
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            json={"body": comment_body},
            headers=auth_headers
        )
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "comment_id" in data, "Response should contain comment_id"
        assert data["body"] == comment_body, "Comment body should match"
        assert data["thread_id"] == thread_id, "Thread ID should match"
        assert "user_id" in data, "Should have user_id"
        assert "created_at" in data, "Should have created_at"
        
        # Store for later tests
        self.__class__.created_comment_id = data["comment_id"]
        self.__class__.comment_thread_id = thread_id
        
        print(f"✅ PASS: POST /api/discussions/threads/{thread_id}/comments created comment")
        return data["comment_id"], thread_id
    
    def test_create_reply_comment(self, auth_headers):
        """POST /api/discussions/threads/{thread_id}/comments with parent_comment_id should create reply"""
        # First create a thread and comment
        unique_title = f"TEST_ReplyThread_{uuid.uuid4().hex[:8]}"
        create_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": unique_title
            },
            headers=auth_headers
        )
        thread_id = create_response.json()["thread_id"]
        
        # Create parent comment
        parent_response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            json={"body": "Parent comment for reply test"},
            headers=auth_headers
        )
        parent_comment_id = parent_response.json()["comment_id"]
        
        # Create reply
        reply_body = f"TEST_Reply_{uuid.uuid4().hex[:8]}: This is a reply to the parent comment."
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            json={
                "body": reply_body,
                "parent_comment_id": parent_comment_id
            },
            headers=auth_headers
        )
        
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data["parent_comment_id"] == parent_comment_id, "Parent comment ID should match"
        
        print(f"✅ PASS: Created reply comment with parent_comment_id")
    
    def test_comment_on_nonexistent_thread_returns_404(self, auth_headers):
        """POST /api/discussions/threads/{invalid_id}/comments should return 404"""
        fake_id = str(uuid.uuid4())
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{fake_id}/comments",
            json={"body": "Test comment"},
            headers=auth_headers
        )
        
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print("✅ PASS: Comment on nonexistent thread correctly returns 404")
    
    # ============================================================
    # REACTION TESTS
    # ============================================================
    
    def test_toggle_reaction(self, auth_headers):
        """POST /api/discussions/comments/{comment_id}/react should toggle reaction"""
        # First create a thread and comment
        unique_title = f"TEST_ReactionThread_{uuid.uuid4().hex[:8]}"
        create_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": unique_title
            },
            headers=auth_headers
        )
        thread_id = create_response.json()["thread_id"]
        
        # Create comment
        comment_response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            json={"body": "Comment for reaction test"},
            headers=auth_headers
        )
        comment_id = comment_response.json()["comment_id"]
        
        # Add reaction - helpful
        response = requests.post(
            f"{BASE_URL}/api/discussions/comments/{comment_id}/react",
            json={"reaction_type": "helpful"},
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "reactions" in data, "Response should contain reactions"
        
        # Verify reaction was added
        reactions = data.get("reactions", {})
        helpful_reactions = reactions.get("helpful", [])
        assert len(helpful_reactions) > 0, "Should have at least one helpful reaction"
        
        print(f"✅ PASS: POST /api/discussions/comments/{comment_id}/react added reaction")
        
        # Toggle off (remove reaction)
        response2 = requests.post(
            f"{BASE_URL}/api/discussions/comments/{comment_id}/react",
            json={"reaction_type": "helpful"},
            headers=auth_headers
        )
        
        assert response2.status_code == 200
        data2 = response2.json()
        reactions2 = data2.get("reactions", {})
        helpful_reactions2 = reactions2.get("helpful", [])
        
        # Should be removed (toggled off)
        assert len(helpful_reactions2) == 0, "Reaction should be toggled off"
        
        print("✅ PASS: Reaction toggle off works correctly")
    
    def test_reaction_types(self, auth_headers):
        """Test all three reaction types: helpful, insightful, question"""
        # Create thread and comment
        unique_title = f"TEST_ReactionTypesThread_{uuid.uuid4().hex[:8]}"
        create_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": unique_title
            },
            headers=auth_headers
        )
        thread_id = create_response.json()["thread_id"]
        
        comment_response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            json={"body": "Comment for reaction types test"},
            headers=auth_headers
        )
        comment_id = comment_response.json()["comment_id"]
        
        # Test each reaction type
        for reaction_type in ["helpful", "insightful", "question"]:
            response = requests.post(
                f"{BASE_URL}/api/discussions/comments/{comment_id}/react",
                json={"reaction_type": reaction_type},
                headers=auth_headers
            )
            
            assert response.status_code == 200, f"Failed for reaction type {reaction_type}: {response.text}"
            print(f"✅ PASS: Reaction type '{reaction_type}' works")
    
    def test_reaction_on_nonexistent_comment_returns_404(self, auth_headers):
        """POST /api/discussions/comments/{invalid_id}/react should return 404"""
        fake_id = str(uuid.uuid4())
        response = requests.post(
            f"{BASE_URL}/api/discussions/comments/{fake_id}/react",
            json={"reaction_type": "helpful"},
            headers=auth_headers
        )
        
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print("✅ PASS: Reaction on nonexistent comment correctly returns 404")
    
    # ============================================================
    # SPECIALTY THREADS TESTS
    # ============================================================
    
    def test_get_specialty_threads(self, auth_headers):
        """GET /api/discussions/specialties/{specialty_id} should return threads for specialty"""
        response = requests.get(
            f"{BASE_URL}/api/discussions/specialties/cardiology",
            headers=auth_headers
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "threads" in data, "Response should contain 'threads' key"
        assert "total" in data, "Response should contain 'total' key"
        
        threads = data["threads"]
        assert isinstance(threads, list), "Threads should be a list"
        
        # All threads should be for cardiology specialty
        for thread in threads:
            assert thread.get("specialty_id") == "cardiology", f"Thread specialty should be cardiology, got {thread.get('specialty_id')}"
        
        print(f"✅ PASS: GET /api/discussions/specialties/cardiology returned {len(threads)} threads")
    
    # ============================================================
    # VALIDATION TESTS
    # ============================================================
    
    def test_create_thread_empty_title_fails(self, auth_headers):
        """POST /api/discussions/threads with empty title should fail"""
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": ""
            },
            headers=auth_headers
        )
        
        assert response.status_code == 422, f"Expected 422 validation error, got {response.status_code}"
        print("✅ PASS: Empty title correctly rejected with 422")
    
    def test_create_comment_empty_body_fails(self, auth_headers):
        """POST /api/discussions/threads/{id}/comments with empty body should fail"""
        # First create a thread
        create_response = requests.post(
            f"{BASE_URL}/api/discussions/threads",
            json={
                "context_type": "specialty",
                "context_id": "cardiology",
                "specialty_id": "cardiology",
                "title": f"TEST_ValidationThread_{uuid.uuid4().hex[:8]}"
            },
            headers=auth_headers
        )
        thread_id = create_response.json()["thread_id"]
        
        # Try to create comment with empty body
        response = requests.post(
            f"{BASE_URL}/api/discussions/threads/{thread_id}/comments",
            json={"body": ""},
            headers=auth_headers
        )
        
        assert response.status_code == 422, f"Expected 422 validation error, got {response.status_code}"
        print("✅ PASS: Empty comment body correctly rejected with 422")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
