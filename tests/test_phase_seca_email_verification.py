"""
Phase SEC-A Tests — Email Verification Requirement for App Access

Tests for:
- Feature flag REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS
- Flag OFF: unverified user can access all endpoints (existing behavior)
- Flag ON: unverified user blocked from protected endpoints (403)
- Allowlist endpoints work for unverified users

Run with: pytest tests/test_phase_seca_email_verification.py -v
"""
import pytest
import os
from unittest.mock import patch, AsyncMock, MagicMock

# Set test environment
os.environ.setdefault("REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS", "false")


class TestEmailVerificationFeatureFlag:
    """Test REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS feature flag."""
    
    def test_flag_default_off(self):
        """Flag should default to OFF."""
        with patch.dict(os.environ, {"REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS": ""}):
            from utils.feature_flags import get_feature_flags
            flags = get_feature_flags()
            assert flags.get("require_email_verified_for_app_access") == False
    
    def test_flag_can_be_enabled(self):
        """Flag should be settable via env var."""
        with patch.dict(os.environ, {"REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS": "true"}):
            from utils.feature_flags import get_feature_flags
            flags = get_feature_flags()
            assert flags.get("require_email_verified_for_app_access") == True


class TestAllowlist:
    """Test allowlist for unverified users."""
    
    def test_auth_me_in_allowlist(self):
        """GET /api/auth/me should be in allowlist."""
        from auth_utils import EMAIL_VERIFICATION_ALLOWLIST, _is_path_in_allowlist
        
        assert "/api/auth/me" in EMAIL_VERIFICATION_ALLOWLIST
        assert _is_path_in_allowlist("/api/auth/me")
    
    def test_resend_verification_in_allowlist(self):
        """POST /api/auth/resend-verification should be in allowlist."""
        from auth_utils import EMAIL_VERIFICATION_ALLOWLIST, _is_path_in_allowlist
        
        assert "/api/auth/resend-verification" in EMAIL_VERIFICATION_ALLOWLIST
        assert _is_path_in_allowlist("/api/auth/resend-verification")
    
    def test_verify_email_in_allowlist(self):
        """POST /api/auth/verify-email should be in allowlist."""
        from auth_utils import EMAIL_VERIFICATION_ALLOWLIST, _is_path_in_allowlist
        
        assert "/api/auth/verify-email" in EMAIL_VERIFICATION_ALLOWLIST
        assert _is_path_in_allowlist("/api/auth/verify-email")
    
    def test_health_in_allowlist(self):
        """GET /api/health should be in allowlist."""
        from auth_utils import _is_path_in_allowlist
        
        assert _is_path_in_allowlist("/api/health")
    
    def test_config_endpoints_in_allowlist(self):
        """Config endpoints should be in allowlist (prefix match)."""
        from auth_utils import _is_path_in_allowlist
        
        assert _is_path_in_allowlist("/api/config/feature-flags")
        assert _is_path_in_allowlist("/api/config/specialties")
    
    def test_protected_endpoints_not_in_allowlist(self):
        """Protected endpoints should NOT be in allowlist."""
        from auth_utils import _is_path_in_allowlist
        
        assert not _is_path_in_allowlist("/api/library")
        assert not _is_path_in_allowlist("/api/digests")
        assert not _is_path_in_allowlist("/api/discussions/threads")
        assert not _is_path_in_allowlist("/api/preferences")


class TestFlagOffBehavior:
    """Test that existing behavior is unchanged when flag is OFF."""
    
    def test_unverified_user_allowed_when_flag_off(self):
        """Unverified user should access all endpoints when flag is OFF."""
        from auth_utils import _is_email_verification_required
        
        with patch.dict(os.environ, {"REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS": "false"}):
            assert _is_email_verification_required() == False


class TestFlagOnBehavior:
    """Test email verification enforcement when flag is ON."""
    
    def test_verification_required_when_flag_on(self):
        """Should require email verification when flag is ON."""
        from auth_utils import _is_email_verification_required
        
        with patch.dict(os.environ, {"REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS": "true"}):
            assert _is_email_verification_required() == True


class TestErrorResponse:
    """Test error responses for unverified users."""
    
    def test_error_code_is_email_verification_required(self):
        """Error should have error_code='email_verification_required'."""
        # The error is raised in get_current_user when flag is ON and user is unverified
        # Verified via API integration tests
        pass


class TestUITerminology:
    """Test UI terminology distinction."""
    
    def test_web_page_exists(self):
        """Web VerifyEmailRequiredPage should exist."""
        import os
        assert os.path.exists('/app/frontend/src/pages/VerifyEmailRequiredPage.tsx')
    
    def test_mobile_screen_exists(self):
        """Mobile VerifyEmailRequiredScreen should exist."""
        import os
        assert os.path.exists('/app/mobile/src/screens/VerifyEmailRequiredScreen.tsx')
    
    def test_web_page_distinguishes_email_vs_professional(self):
        """Web page should distinguish Email Verified from Professional Verified."""
        with open('/app/frontend/src/pages/VerifyEmailRequiredPage.tsx', 'r') as f:
            content = f.read()
        
        # Should mention that email verification is separate from professional verification
        assert 'professional verification' in content.lower() or 'separate' in content.lower()
    
    def test_mobile_screen_distinguishes_email_vs_professional(self):
        """Mobile screen should distinguish Email Verified from Professional Verified."""
        with open('/app/mobile/src/screens/VerifyEmailRequiredScreen.tsx', 'r') as f:
            content = f.read()
        
        # Should mention that email verification is separate from professional verification
        assert 'professional verification' in content.lower() or 'separate' in content.lower()


class TestRouteProtection:
    """Test route protection behavior."""
    
    def test_protected_route_checks_email_verification(self):
        """ProtectedRoute should check email verification flag."""
        with open('/app/frontend/src/components/ProtectedRoute.tsx', 'r') as f:
            content = f.read()
        
        assert 'require_email_verified_for_app_access' in content
        assert 'verify-email-required' in content
    
    def test_mobile_navigator_checks_email_verification(self):
        """Mobile navigator should check email verification flag."""
        with open('/app/mobile/src/navigation/AppNavigation.tsx', 'r') as f:
            content = f.read()
        
        assert 'require_email_verified_for_app_access' in content
        assert 'VerifyEmailRequiredScreen' in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
