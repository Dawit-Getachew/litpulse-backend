"""
Test suite for LitScreen UI/UX Redesign - Phase UX-D
Tests the new DigestChipsBar, screen-stats endpoint, and set-primary functionality.

Features tested:
1. GET /preferences/profiles/screen-stats - Returns profile stats for LitScreen
2. POST /preferences/profiles/{id}/set-primary - Sets a profile as primary
3. Profile stats include: specialty labels, last digest date, unscreened count, saved %
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
TEST_EMAIL = "sectest@example.com"
TEST_PASSWORD = "SecureTest123!"


class TestScreenRedesignAPIs:
    """Test the new screen redesign API endpoints."""
    
    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup test fixtures - login and get token."""
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        
        # Login to get token
        response = self.session.post(f"{BASE_URL}/api/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD
        })
        
        if response.status_code == 200:
            data = response.json()
            self.token = data.get("access_token")
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
            self.user = data.get("user", {})
        else:
            pytest.skip(f"Login failed: {response.status_code} - {response.text}")
    
    # =========================================================================
    # GET /preferences/profiles/screen-stats Tests
    # =========================================================================
    
    def test_get_profiles_screen_stats_returns_200(self):
        """Test that screen-stats endpoint returns 200."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    
    def test_get_profiles_screen_stats_structure(self):
        """Test that screen-stats returns correct structure."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 200
        
        data = response.json()
        
        # Check top-level structure
        assert "profiles" in data, "Response should have 'profiles' key"
        assert "total_unscreened" in data, "Response should have 'total_unscreened' key"
        assert isinstance(data["profiles"], list), "profiles should be a list"
        assert isinstance(data["total_unscreened"], int), "total_unscreened should be an integer"
    
    def test_get_profiles_screen_stats_profile_fields(self):
        """Test that each profile has required fields."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        profile = profiles[0]
        
        # Required fields for DigestChipsBar
        required_fields = [
            "profile_id",
            "name",
            "specialty_id",
            "specialty_label",
            "subspecialty_id",
            "subspecialty_label",
            "is_primary",
            "last_digest_date",
            "last_digest_id",
            "unscreened_count",
            "total_articles",
            "saved_percent",
        ]
        
        for field in required_fields:
            assert field in profile, f"Profile should have '{field}' field"
    
    def test_get_profiles_screen_stats_primary_profile_first(self):
        """Test that primary profile is sorted first."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        # First profile should be primary (if any is primary)
        has_primary = any(p.get("is_primary") for p in profiles)
        if has_primary:
            assert profiles[0].get("is_primary") == True, "Primary profile should be first"
    
    def test_get_profiles_screen_stats_specialty_labels(self):
        """Test that specialty labels are human-readable."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        profile = profiles[0]
        
        # Labels should be human-readable (not snake_case IDs)
        specialty_label = profile.get("specialty_label", "")
        assert specialty_label, "specialty_label should not be empty"
        # Should be title case or proper name
        assert specialty_label[0].isupper(), "specialty_label should start with uppercase"
    
    def test_get_profiles_screen_stats_saved_percent_range(self):
        """Test that saved_percent is in valid range (0-100)."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        for profile in profiles:
            saved_percent = profile.get("saved_percent", 0)
            assert 0 <= saved_percent <= 100, f"saved_percent should be 0-100, got {saved_percent}"
    
    # =========================================================================
    # POST /preferences/profiles/{id}/set-primary Tests
    # =========================================================================
    
    def test_set_primary_profile_returns_200(self):
        """Test that set-primary endpoint returns 200."""
        # First get profiles to find a profile ID
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        profile_id = profiles[0].get("profile_id")
        
        # Set as primary
        response = self.session.post(f"{BASE_URL}/api/preferences/profiles/{profile_id}/set-primary")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    
    def test_set_primary_profile_response_structure(self):
        """Test that set-primary returns correct response structure."""
        # First get profiles
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        profile_id = profiles[0].get("profile_id")
        
        # Set as primary
        response = self.session.post(f"{BASE_URL}/api/preferences/profiles/{profile_id}/set-primary")
        assert response.status_code == 200
        
        data = response.json()
        assert "message" in data, "Response should have 'message' key"
        assert "profile_id" in data, "Response should have 'profile_id' key"
        assert data["profile_id"] == profile_id, "Response profile_id should match request"
    
    def test_set_primary_profile_already_primary(self):
        """Test that setting already-primary profile returns appropriate message."""
        # First get profiles
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        # Find primary profile
        primary_profile = next((p for p in profiles if p.get("is_primary")), profiles[0])
        profile_id = primary_profile.get("profile_id")
        
        # Set as primary (should work even if already primary)
        response = self.session.post(f"{BASE_URL}/api/preferences/profiles/{profile_id}/set-primary")
        assert response.status_code == 200
        
        data = response.json()
        # Should indicate it's already primary or successfully set
        assert "message" in data
    
    def test_set_primary_profile_invalid_id_returns_404(self):
        """Test that invalid profile ID returns 404."""
        response = self.session.post(f"{BASE_URL}/api/preferences/profiles/invalid-uuid-12345/set-primary")
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    
    def test_set_primary_profile_updates_screen_stats(self):
        """Test that setting primary updates screen-stats response."""
        # Get profiles
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        profile_id = profiles[0].get("profile_id")
        
        # Set as primary
        response = self.session.post(f"{BASE_URL}/api/preferences/profiles/{profile_id}/set-primary")
        assert response.status_code == 200
        
        # Verify in screen-stats
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 200
        
        data = response.json()
        stats_profiles = data.get("profiles", [])
        
        # Find the profile we set as primary
        target_profile = next((p for p in stats_profiles if p.get("profile_id") == profile_id), None)
        assert target_profile is not None, "Profile should be in screen-stats"
        assert target_profile.get("is_primary") == True, "Profile should be marked as primary"
    
    # =========================================================================
    # GET /preferences/profiles Tests (existing endpoint verification)
    # =========================================================================
    
    def test_get_profiles_returns_200(self):
        """Test that profiles endpoint returns 200."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    
    def test_get_profiles_has_is_primary_field(self):
        """Test that profiles include is_primary field."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        if len(profiles) == 0:
            pytest.skip("No profiles found for user")
        
        for profile in profiles:
            assert "is_primary" in profile, "Profile should have 'is_primary' field"
            assert isinstance(profile["is_primary"], bool), "is_primary should be boolean"
    
    def test_only_one_primary_profile(self):
        """Test that only one profile is marked as primary."""
        response = self.session.get(f"{BASE_URL}/api/preferences/profiles")
        assert response.status_code == 200
        
        data = response.json()
        profiles = data.get("profiles", [])
        
        primary_count = sum(1 for p in profiles if p.get("is_primary"))
        assert primary_count <= 1, f"Should have at most 1 primary profile, found {primary_count}"


class TestScreenRedesignUnauthorized:
    """Test unauthorized access to screen redesign endpoints."""
    
    def test_screen_stats_requires_auth(self):
        """Test that screen-stats requires authentication."""
        response = requests.get(f"{BASE_URL}/api/preferences/profiles/screen-stats")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"
    
    def test_set_primary_requires_auth(self):
        """Test that set-primary requires authentication."""
        response = requests.post(f"{BASE_URL}/api/preferences/profiles/some-id/set-primary")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
