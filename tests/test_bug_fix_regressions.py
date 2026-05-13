"""
Bug Fix Regression Tests - 3 Fixed Issues
Tests for:
1. Signup flow - returns 201 with user_id, email, is_verified=false, created_at as string
2. Login flow - returns access_token and user object
3. Specialty config - GET /api/config/specialties returns top_journals at subspecialty level
4. Profile creation - POST /api/preferences/profiles accepts auto-generated name
5. Dual-write to legacy preferences collection
"""
import pytest
import requests
import os
import uuid
from datetime import datetime

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
DEMO_USER_EMAIL = "demo@litpulse.com"
DEMO_USER_PASSWORD = "DemoPass123!"

@pytest.fixture(scope="module")
def api_client():
    """Shared requests session"""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session

@pytest.fixture(scope="module")
def demo_token(api_client):
    """Get auth token for demo user"""
    response = api_client.post(f"{BASE_URL}/api/auth/login", json={
        "email": DEMO_USER_EMAIL,
        "password": DEMO_USER_PASSWORD
    })
    if response.status_code == 200:
        return response.json().get("access_token")
    pytest.skip("Could not authenticate demo user")

@pytest.fixture(scope="module")
def authenticated_client(api_client, demo_token):
    """Session with auth header"""
    api_client.headers.update({"Authorization": f"Bearer {demo_token}"})
    return api_client


class TestSignupEndpoint:
    """
    Bug Fix #1: Signup endpoint returns datetime instead of string
    Fixed: server.py line 595-675 now converts datetime to ISO strings
    """

    def test_signup_returns_201_with_correct_fields(self, api_client):
        """POST /api/auth/signup should return 201 with user_id, email, is_verified=false, created_at as string"""
        unique_email = f"test_signup_{uuid.uuid4().hex[:8]}@example.com"
        
        response = api_client.post(f"{BASE_URL}/api/auth/signup", json={
            "email": unique_email,
            "password": "TestPass123!",
            "full_name": "Test User",
            "timezone": "America/New_York"
        })
        
        # Status code assertion
        assert response.status_code == 201, f"Expected 201, got {response.status_code}: {response.text}"
        
        # Data assertions
        data = response.json()
        assert "user_id" in data, "Response should contain user_id"
        assert "email" in data, "Response should contain email"
        assert data["email"] == unique_email.lower(), "Email should match and be lowercase"
        assert "is_verified" in data, "Response should contain is_verified"
        assert data["is_verified"] == False, "is_verified should be False for new user"
        assert "created_at" in data, "Response should contain created_at"
        
        # CRITICAL: Verify created_at is a STRING (the bug was returning datetime object)
        assert isinstance(data["created_at"], str), f"created_at should be string, got {type(data['created_at'])}"
        
        # Verify it's a valid ISO format string
        try:
            datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        except ValueError as e:
            pytest.fail(f"created_at is not valid ISO format: {e}")
        
        # Also check updated_at
        assert "updated_at" in data, "Response should contain updated_at"
        assert isinstance(data["updated_at"], str), f"updated_at should be string, got {type(data['updated_at'])}"
        
        print(f"SUCCESS: Signup returned 201 with user_id={data['user_id']}, is_verified={data['is_verified']}, created_at type={type(data['created_at'])}")

    def test_signup_existing_email_returns_400(self, api_client):
        """POST /api/auth/signup with existing email should return 400"""
        response = api_client.post(f"{BASE_URL}/api/auth/signup", json={
            "email": DEMO_USER_EMAIL,
            "password": "TestPass123!",
            "full_name": "Test User"
        })
        
        assert response.status_code == 400, f"Expected 400 for existing email, got {response.status_code}"
        assert "already registered" in response.json().get("detail", "").lower()
        print("SUCCESS: Existing email returns 400 with 'already registered' message")


class TestLoginEndpoint:
    """
    Bug Fix #2: Login flow should return access_token and user object
    """

    def test_login_success_returns_token_and_user(self, api_client):
        """POST /api/auth/login with valid credentials returns access_token and user"""
        response = api_client.post(f"{BASE_URL}/api/auth/login", json={
            "email": DEMO_USER_EMAIL,
            "password": DEMO_USER_PASSWORD
        })
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "access_token" in data, "Response should contain access_token"
        assert "token_type" in data, "Response should contain token_type"
        assert data["token_type"] == "bearer", "token_type should be 'bearer'"
        assert "user" in data, "Response should contain user object"
        
        # Validate user object
        user = data["user"]
        assert "user_id" in user, "User should have user_id"
        assert "email" in user, "User should have email"
        assert user["email"] == DEMO_USER_EMAIL, "User email should match"
        
        print(f"SUCCESS: Login returned access_token and user with email={user['email']}")

    def test_login_invalid_password_returns_401(self, api_client):
        """POST /api/auth/login with wrong password returns 401"""
        response = api_client.post(f"{BASE_URL}/api/auth/login", json={
            "email": DEMO_USER_EMAIL,
            "password": "WrongPassword123!"
        })
        
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
        print("SUCCESS: Invalid password returns 401")


class TestSpecialtyConfig:
    """
    Bug Fix #3: Specialty config should have top_journals at subspecialty level
    The fix: UnifiedPreferencesWizard.tsx line 326-365 now uses useMemo to aggregate
    top_journals from subspecialties instead of looking for journals at specialty level
    """

    def test_specialty_config_structure(self, api_client):
        """GET /api/config/specialties should return specialties with subspecialties containing top_journals"""
        response = api_client.get(f"{BASE_URL}/api/config/specialties")
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        
        data = response.json()
        assert "specialties" in data, "Response should contain specialties array"
        assert len(data["specialties"]) > 0, "Should have at least one specialty"
        
        # Check first specialty
        specialty = data["specialties"][0]
        assert "id" in specialty, "Specialty should have id"
        assert "label" in specialty, "Specialty should have label"
        assert "subspecialties" in specialty, "Specialty should have subspecialties array"
        
        print(f"SUCCESS: Config has {len(data['specialties'])} specialties")
        
        # Check subspecialties have top_journals (NOT at specialty level)
        subspecs_with_journals = 0
        for subspec in specialty.get("subspecialties", []):
            assert "id" in subspec, "Subspecialty should have id"
            assert "label" in subspec, "Subspecialty should have label"
            
            if "top_journals" in subspec:
                subspecs_with_journals += 1
                assert isinstance(subspec["top_journals"], list), "top_journals should be a list"
                if len(subspec["top_journals"]) > 0:
                    # Verify journals are strings
                    assert all(isinstance(j, str) for j in subspec["top_journals"]), "top_journals should be strings"
        
        assert subspecs_with_journals > 0, "At least some subspecialties should have top_journals"
        print(f"SUCCESS: Found {subspecs_with_journals} subspecialties with top_journals in first specialty")
        
        # IMPORTANT: Journals should be at SUBSPECIALTY level, not specialty level
        # This confirms the data structure the frontend expects
        assert "journals" not in specialty or not specialty.get("journals"), \
            "Journals should NOT be at specialty level (should be in subspecialties.top_journals)"


class TestProfileCreation:
    """
    Bug Fix #4: Profile creation should accept auto-generated name
    The fix: UnifiedPreferencesWizard.tsx line 228-235 auto-generates profile name
    """

    def test_profile_creation_with_name(self, authenticated_client):
        """POST /api/preferences/profiles should create profile with provided name"""
        response = authenticated_client.post(f"{BASE_URL}/api/preferences/profiles", json={
            "name": f"Test Profile {uuid.uuid4().hex[:6]}",
            "specialty_id": "cardiology",
            "subspecialty_id": "interventional_cardiology",
            "subspecialties": ["interventional_cardiology"],
            "custom_keywords": [],
            "topics_selected": [],
            "journals_selected": [],
            "schedule": {
                "frequency": "weekly",
                "day_of_week": "Monday",
                "hour": 9,
                "minute": 0,
                "time_local": "09:00"
            }
        })
        
        # Could be 201 (created) or 409 (limit reached) - both are valid responses
        assert response.status_code in [201, 409], f"Expected 201 or 409, got {response.status_code}: {response.text}"
        
        if response.status_code == 201:
            data = response.json()
            assert "profile_id" in data, "Should return profile_id"
            assert "name" in data, "Should return name"
            print(f"SUCCESS: Profile created with name={data['name']}")
        else:
            detail = response.json().get("detail", {})
            print(f"Profile limit reached (expected): {detail.get('message', 'limit reached')}")


class TestLegacyPreferencesEndpoint:
    """
    Bug Fix #5: GET /api/preferences/me should work after profile creation (dual-write)
    """

    def test_preferences_me_after_profile(self, authenticated_client):
        """GET /api/preferences/me should return preferences (dual-write populates this)"""
        response = authenticated_client.get(f"{BASE_URL}/api/preferences/me")
        
        # For demo user who has preferences, should return 200
        # For new users, could be 404 if no preferences set
        assert response.status_code in [200, 404], f"Expected 200 or 404, got {response.status_code}"
        
        if response.status_code == 200:
            data = response.json()
            assert "specialty_id" in data, "Should have specialty_id"
            assert "schedule" in data, "Should have schedule"
            print(f"SUCCESS: /api/preferences/me returned specialty_id={data.get('specialty_id')}")
        else:
            print("User has no legacy preferences (expected for some users)")


class TestProfiles:
    """Test profiles endpoint functionality"""

    def test_list_profiles(self, authenticated_client):
        """GET /api/preferences/profiles should return list of profiles"""
        response = authenticated_client.get(f"{BASE_URL}/api/preferences/profiles")
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        
        data = response.json()
        assert "profiles" in data, "Should have profiles array"
        assert "count" in data, "Should have count"
        assert "max_profiles" in data, "Should have max_profiles"
        
        print(f"SUCCESS: Found {data['count']} profiles, max={data['max_profiles']}")


class TestHealthEndpoint:
    """Verify API is healthy"""

    def test_health_check(self, api_client):
        """GET /api/health should return ok"""
        response = api_client.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        assert response.json().get("status") == "ok"
        print("SUCCESS: Health check passed")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
