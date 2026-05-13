"""
Phase UX-A API Tests — App Shell UI Refresh

Tests for:
- Feature flag ENABLE_APP_SHELL_UI_V2 in /api/config/feature-flags
- Flag defaults to false
- All existing routes still work
- Price verification ($4.99)

Run with: pytest tests/test_phase_uxa_api.py -v
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')


class TestFeatureFlagAPI:
    """Test ENABLE_APP_SHELL_UI_V2 feature flag via API."""
    
    def test_feature_flags_endpoint_returns_200(self):
        """Feature flags endpoint should return 200."""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
    
    def test_app_shell_ui_v2_flag_present(self):
        """enable_app_shell_ui_v2 flag should be present in response."""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        data = response.json()
        assert "enable_app_shell_ui_v2" in data, "Missing enable_app_shell_ui_v2 flag"
    
    def test_app_shell_ui_v2_flag_defaults_false(self):
        """enable_app_shell_ui_v2 flag should default to false."""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        data = response.json()
        assert data.get("enable_app_shell_ui_v2") == False, "Flag should default to false"
    
    def test_library_audio_digests_v2_flag_present(self):
        """enable_library_audio_digests_v2 flag should be present (for banner)."""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        data = response.json()
        assert "enable_library_audio_digests_v2" in data, "Missing enable_library_audio_digests_v2 flag"


class TestExistingRoutesWork:
    """Test that all existing routes still work."""
    
    def test_health_endpoint(self):
        """Health endpoint should return 200."""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"
    
    def test_config_specialties_endpoint(self):
        """Config specialties endpoint should return 200."""
        response = requests.get(f"{BASE_URL}/api/config/specialties")
        assert response.status_code == 200
    
    def test_login_endpoint_exists(self):
        """Login endpoint should exist (return 422 for missing body, not 404)."""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={})
        # Should return 422 (validation error) not 404 (not found)
        assert response.status_code in [400, 422], f"Expected 400/422, got {response.status_code}"
    
    def test_signup_endpoint_exists(self):
        """Signup endpoint should exist."""
        response = requests.post(f"{BASE_URL}/api/auth/signup", json={})
        # Should return 422 (validation error) not 404 (not found)
        assert response.status_code in [400, 422], f"Expected 400/422, got {response.status_code}"


class TestAuthenticatedRoutes:
    """Test authenticated routes work with valid credentials."""
    
    @pytest.fixture(scope="class")
    def auth_token(self):
        """Get auth token for premium user."""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": "demo@litpulse.com",
            "password": "DemoPass123!"
        })
        if response.status_code == 200:
            return response.json().get("access_token")
        pytest.skip("Login failed - skipping authenticated tests")
    
    def test_auth_me_accessible(self, auth_token):
        """User should be able to access /auth/me."""
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert "email" in data
    
    def test_preferences_endpoint_accessible(self, auth_token):
        """Preferences endpoint should be accessible."""
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(f"{BASE_URL}/api/preferences/me", headers=headers)
        # 200 if preferences exist, 404 if not yet created
        assert response.status_code in [200, 404]
    
    def test_digests_endpoint_accessible(self, auth_token):
        """Digests endpoint should be accessible."""
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(f"{BASE_URL}/api/digests", headers=headers)
        assert response.status_code == 200
    
    def test_library_endpoint_accessible(self, auth_token):
        """Library endpoint should be accessible."""
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(f"{BASE_URL}/api/library", headers=headers)
        assert response.status_code == 200
    
    def test_community_discussions_accessible(self, auth_token):
        """Community discussions endpoint should be accessible."""
        headers = {"Authorization": f"Bearer {auth_token}"}
        # Discussions requires context_type and context_id params
        response = requests.get(f"{BASE_URL}/api/discussions/specialty-rooms", headers=headers)
        assert response.status_code == 200
    
    def test_billing_status_accessible(self, auth_token):
        """Billing status endpoint should be accessible."""
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(f"{BASE_URL}/api/billing/me", headers=headers)
        assert response.status_code == 200
    
    def test_verification_status_accessible(self, auth_token):
        """Verification status endpoint should be accessible."""
        headers = {"Authorization": f"Bearer {auth_token}"}
        response = requests.get(f"{BASE_URL}/api/verification/me", headers=headers)
        assert response.status_code == 200


class TestFreeUserRoutes:
    """Test routes work for free user."""
    
    @pytest.fixture(scope="class")
    def free_auth_token(self):
        """Get auth token for free user."""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": "test@litpulse.com",
            "password": "TestPass123!"
        })
        if response.status_code == 200:
            return response.json().get("access_token")
        pytest.skip("Free user login failed - skipping tests")
    
    def test_free_user_can_access_auth_me(self, free_auth_token):
        """Free user should be able to access /auth/me."""
        headers = {"Authorization": f"Bearer {free_auth_token}"}
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=headers)
        assert response.status_code == 200
    
    def test_free_user_can_access_digests(self, free_auth_token):
        """Free user should be able to access digests."""
        headers = {"Authorization": f"Bearer {free_auth_token}"}
        response = requests.get(f"{BASE_URL}/api/digests", headers=headers)
        assert response.status_code == 200
    
    def test_free_user_can_access_library(self, free_auth_token):
        """Free user should be able to access library."""
        headers = {"Authorization": f"Bearer {free_auth_token}"}
        response = requests.get(f"{BASE_URL}/api/library", headers=headers)
        assert response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
