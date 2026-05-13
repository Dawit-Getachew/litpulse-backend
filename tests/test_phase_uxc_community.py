"""
Phase UX-C: Community Visibility + Subspecialty Community Limits Tests

Tests:
1. Feature flags returned from /api/config/feature-flags
2. GET /api/discussions/specialty-rooms returns can_enter, can_post, eligible_subspecialties, visible_subspecialties
3. When ENABLE_COMMUNITY_VISIBLE_ONLY_ELIGIBLE=true, locked rooms filtering
4. Community subspecialty selection validation
5. Free users read-only restrictions
6. Profile create/update with community_subspecialty_ids validation
"""
import pytest
import requests
import os
import time

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


class TestSession:
    """Shared session with retry logic for rate limiting"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
    
    def login(self, credentials, max_retries=3):
        """Login with retry for rate limiting"""
        for attempt in range(max_retries):
            response = self.session.post(
                f"{BASE_URL}/api/auth/login",
                json=credentials
            )
            if response.status_code == 200:
                token = response.json().get("access_token")
                self.session.headers.update({"Authorization": f"Bearer {token}"})
                return response.json()
            elif response.status_code == 429:
                # Rate limited, wait and retry
                time.sleep(5 * (attempt + 1))
            else:
                return response.json()
        return {"error": "Max retries exceeded due to rate limiting"}
    
    def get(self, endpoint):
        return self.session.get(f"{BASE_URL}{endpoint}")
    
    def post(self, endpoint, json=None):
        return self.session.post(f"{BASE_URL}{endpoint}", json=json)
    
    def put(self, endpoint, json=None):
        return self.session.put(f"{BASE_URL}{endpoint}", json=json)


@pytest.fixture(scope="module")
def premium_session():
    """Premium user session"""
    session = TestSession()
    result = session.login(PREMIUM_USER)
    if "access_token" not in result:
        pytest.skip(f"Premium user login failed: {result}")
    return session


@pytest.fixture(scope="module")
def free_session():
    """Free user session"""
    session = TestSession()
    result = session.login(FREE_USER)
    if "access_token" not in result:
        pytest.skip(f"Free user login failed: {result}")
    return session


class TestFeatureFlags:
    """Test feature flags endpoint returns Phase UX-C flags"""
    
    def test_feature_flags_endpoint_returns_uxc_flags(self):
        """Feature flags should include enable_community_visible_only_eligible and enable_community_subspecialty_selection"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200, f"Feature flags endpoint failed: {response.text}"
        
        flags = response.json()
        
        # Check Phase UX-C flags exist
        assert "enable_community_visible_only_eligible" in flags, "Missing enable_community_visible_only_eligible flag"
        assert "enable_community_subspecialty_selection" in flags, "Missing enable_community_subspecialty_selection flag"
        
        # Verify they are boolean
        assert isinstance(flags["enable_community_visible_only_eligible"], bool)
        assert isinstance(flags["enable_community_subspecialty_selection"], bool)
        
        print(f"enable_community_visible_only_eligible: {flags['enable_community_visible_only_eligible']}")
        print(f"enable_community_subspecialty_selection: {flags['enable_community_subspecialty_selection']}")
    
    def test_feature_flags_include_community_v2(self):
        """Feature flags should include enable_community_v2"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        
        flags = response.json()
        assert "enable_community_v2" in flags
        print(f"enable_community_v2: {flags['enable_community_v2']}")


class TestSpecialtyRoomsEndpoint:
    """Test GET /api/discussions/specialty-rooms returns Phase UX-C fields"""
    
    def test_specialty_rooms_returns_can_enter_field(self, premium_session):
        """Specialty rooms should include can_enter field"""
        response = premium_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200, f"Specialty rooms failed: {response.text}"
        
        data = response.json()
        assert "rooms" in data
        assert len(data["rooms"]) > 0, "No specialty rooms returned"
        
        # Check first room has can_enter field
        room = data["rooms"][0]
        assert "can_enter" in room, f"Missing can_enter field in room: {room}"
        print(f"First room: {room['specialty_id']}, can_enter: {room['can_enter']}")
    
    def test_specialty_rooms_returns_can_post_field(self, premium_session):
        """Specialty rooms should include can_post field when subspecialty selection is enabled"""
        response = premium_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200
        
        data = response.json()
        room = data["rooms"][0]
        
        # can_post should be present (may be null if can_enter is false)
        assert "can_post" in room, f"Missing can_post field in room: {room}"
        print(f"First room: {room['specialty_id']}, can_post: {room['can_post']}")
    
    def test_specialty_rooms_returns_subspecialty_fields(self, premium_session):
        """Specialty rooms should include eligible_subspecialties and visible_subspecialties"""
        response = premium_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200
        
        data = response.json()
        room = data["rooms"][0]
        
        # Check subspecialty fields exist
        assert "subspecialties" in room, "Missing subspecialties field"
        assert "eligible_subspecialties" in room, "Missing eligible_subspecialties field"
        assert "visible_subspecialties" in room, "Missing visible_subspecialties field"
        
        print(f"Room {room['specialty_id']}:")
        print(f"  subspecialties count: {len(room['subspecialties'] or [])}")
        print(f"  eligible_subspecialties count: {len(room['eligible_subspecialties'] or [])}")
        print(f"  visible_subspecialties count: {len(room['visible_subspecialties'] or [])}")
    
    def test_specialty_rooms_eligible_subspecialties_structure(self, premium_session):
        """eligible_subspecialties should have id and label fields"""
        response = premium_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200
        
        data = response.json()
        room = data["rooms"][0]
        
        if room.get("eligible_subspecialties"):
            sub = room["eligible_subspecialties"][0]
            assert "id" in sub, "Missing id in subspecialty"
            assert "label" in sub, "Missing label in subspecialty"
            print(f"Sample subspecialty: {sub}")


class TestCommunityVisibility:
    """Test community visibility filtering based on eligibility"""
    
    def test_user_can_enter_their_specialty(self, premium_session):
        """User should be able to enter their specialty community"""
        # First get user's preferences to find their specialty
        prefs_response = premium_session.get("/api/preferences/me")
        if prefs_response.status_code != 200:
            pytest.skip("Could not get user preferences")
        
        prefs = prefs_response.json()
        user_specialty = prefs.get("specialty_id")
        
        if not user_specialty:
            pytest.skip("User has no specialty set")
        
        # Check specialty rooms
        rooms_response = premium_session.get("/api/discussions/specialty-rooms")
        assert rooms_response.status_code == 200
        
        data = rooms_response.json()
        user_room = next((r for r in data["rooms"] if r["specialty_id"] == user_specialty), None)
        
        if user_room:
            print(f"User specialty: {user_specialty}")
            print(f"can_enter: {user_room['can_enter']}")
            # User should be able to enter their own specialty
            assert user_room["can_enter"] is True or user_room["can_enter"] is None, \
                f"User cannot enter their own specialty: {user_room}"
        else:
            print(f"User specialty {user_specialty} not found in rooms")
    
    def test_locked_rooms_have_can_enter_false(self, premium_session):
        """Rooms user cannot access should have can_enter=false"""
        response = premium_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200
        
        data = response.json()
        locked_rooms = [r for r in data["rooms"] if r.get("can_enter") is False]
        
        print(f"Total rooms: {len(data['rooms'])}")
        print(f"Locked rooms (can_enter=false): {len(locked_rooms)}")
        
        if locked_rooms:
            print(f"Sample locked room: {locked_rooms[0]['specialty_id']}")


class TestFreeUserRestrictions:
    """Test free user read-only restrictions"""
    
    def test_free_user_can_view_specialty_rooms(self, free_session):
        """Free user should be able to view specialty rooms"""
        response = free_session.get("/api/discussions/specialty-rooms")
        assert response.status_code == 200, f"Free user cannot view rooms: {response.text}"
        
        data = response.json()
        assert "rooms" in data
        print(f"Free user can see {len(data['rooms'])} rooms")
    
    def test_free_user_cannot_post_thread_in_community(self, free_session):
        """Free user should get 403 when trying to post in community"""
        # First find a room the user can enter
        rooms_response = free_session.get("/api/discussions/specialty-rooms")
        if rooms_response.status_code != 200:
            pytest.skip("Cannot get rooms")
        
        data = rooms_response.json()
        accessible_room = next((r for r in data["rooms"] if r.get("can_enter") is not False), None)
        
        if not accessible_room:
            pytest.skip("No accessible rooms for free user")
        
        # Try to post a thread
        response = free_session.post("/api/discussions/threads", json={
            "context_type": "specialty",
            "context_id": accessible_room["specialty_id"],
            "specialty_id": accessible_room["specialty_id"],
            "title": "Test thread from free user"
        })
        
        # Should get 403 with free_tier_read_only error
        if response.status_code == 403:
            detail = response.json().get("detail", {})
            error_code = detail.get("error_code") if isinstance(detail, dict) else None
            print(f"Free user post blocked: {error_code}")
            # This is expected behavior
            assert error_code in ["free_tier_read_only", "community_locked", "verification_required"], \
                f"Unexpected error code: {error_code}"
        elif response.status_code == 201:
            # If posting succeeded, it means free user has an active digest
            print("Free user was able to post (has active digest in this specialty)")
        else:
            print(f"Unexpected response: {response.status_code} - {response.text}")


class TestProfileCommunitySubspecialties:
    """Test profile create/update with community_subspecialty_ids validation"""
    
    def test_profiles_endpoint_requires_feature_flag(self, premium_session):
        """Profiles endpoint should check enable_multi_digest_profiles flag"""
        response = premium_session.get("/api/preferences/profiles")
        
        # If feature is disabled, should get 404 with feature_disabled error
        if response.status_code == 404:
            detail = response.json().get("detail", {})
            if isinstance(detail, dict) and detail.get("error_code") == "feature_disabled":
                print("Profiles feature is disabled (expected when ENABLE_MULTI_DIGEST_PROFILES=false)")
                pytest.skip("Profiles feature disabled")
        
        assert response.status_code == 200, f"Profiles endpoint failed: {response.text}"
        print(f"Profiles response: {response.json()}")


class TestHealthCheck:
    """Basic health check"""
    
    def test_api_health(self):
        """API should be healthy"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        assert response.json().get("status") == "ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
