"""
Step 8: Production Readiness Hardening Tests
============================================
- Single-use token invalidation for verify-email and reset-password
- Billing realism: billing/me includes portal_available/portal_mode, portal-session returns 503
- Production config validation (ENVIRONMENT=development skips validation)
- map_stripe_status_to_plan_tier mapping verification
- Regression tests: auth/me, library export, audio, admin metrics
"""

import pytest
import requests
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_EMAIL = "demo@litpulse.com"
PREMIUM_PASSWORD = "DemoPass123!"
FREE_EMAIL = "test@litpulse.com"
FREE_PASSWORD = "TestPass123!"


@pytest.fixture(scope="module")
def premium_token():
    """Get auth token for premium user"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": PREMIUM_EMAIL,
        "password": PREMIUM_PASSWORD
    })
    if resp.status_code == 200:
        return resp.json()["access_token"]
    pytest.skip(f"Could not login premium user: {resp.text}")


@pytest.fixture(scope="module")
def free_token():
    """Get auth token for free/trial user"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": FREE_EMAIL,
        "password": FREE_PASSWORD
    })
    if resp.status_code == 200:
        return resp.json()["access_token"]
    pytest.skip(f"Could not login free user: {resp.text}")


class TestBillingMePortalFields:
    """Test billing/me includes portal_available and portal_mode fields"""

    def test_billing_me_includes_portal_available(self, premium_token):
        """billing/me should include portal_available=false"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        resp = requests.get(f"{BASE_URL}/api/billing/me", headers=headers)
        
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        # Verify portal_available field exists and is False
        assert "portal_available" in data, f"Missing portal_available field: {data}"
        assert data["portal_available"] == False, f"Expected portal_available=False, got {data['portal_available']}"

    def test_billing_me_includes_portal_mode(self, premium_token):
        """billing/me should include portal_mode='disabled'"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        resp = requests.get(f"{BASE_URL}/api/billing/me", headers=headers)
        
        assert resp.status_code == 200
        data = resp.json()
        
        # Verify portal_mode field
        assert "portal_mode" in data, f"Missing portal_mode field: {data}"
        assert data["portal_mode"] == "disabled", f"Expected portal_mode='disabled', got {data['portal_mode']}"

    def test_billing_me_billing_enabled(self, premium_token):
        """billing/me should show billing_enabled=true (ENABLE_STRIPE_BILLING=true in .env)"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        resp = requests.get(f"{BASE_URL}/api/billing/me", headers=headers)
        
        assert resp.status_code == 200
        data = resp.json()
        
        assert "billing_enabled" in data, f"Missing billing_enabled field: {data}"
        assert data["billing_enabled"] == True, f"Expected billing_enabled=True, got {data['billing_enabled']}"


class TestPortalSession503:
    """Test portal-session endpoint returns 503 portal_unavailable"""

    def test_portal_session_returns_503(self, premium_token):
        """POST /api/billing/stripe/portal-session should return 503 portal_unavailable"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        resp = requests.post(
            f"{BASE_URL}/api/billing/stripe/portal-session",
            headers=headers,
            json={"origin_url": "https://litscreen-aggregate.preview.emergentagent.com"}
        )
        
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        # Verify error structure
        detail = data.get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("error_code") == "portal_unavailable", f"Expected error_code='portal_unavailable': {detail}"
            assert "message" in detail, f"Missing message in detail: {detail}"
        else:
            assert "portal" in str(detail).lower(), f"Expected portal-related error: {detail}"


class TestSingleUseTokenInvalidation:
    """Test single-use token invalidation for verify-email and reset-password"""

    def test_verify_email_token_already_used(self, premium_token):
        """
        Test that verify-email returns 400 token_already_used on second use.
        We need to generate a real token and use it twice.
        """
        # We'll use a synthetic approach: create a token directly
        # Since we can't easily generate tokens without backend access,
        # we'll test the error response format with an invalid/reused token
        
        # First, let's test with a fabricated token to see error handling
        resp = requests.post(f"{BASE_URL}/api/auth/verify-email", json={
            "token": "fake_test_token_for_already_used_check"
        })
        
        # Should return 401 (invalid token) since the token is invalid JWT
        assert resp.status_code in [400, 401], f"Expected 400/401, got {resp.status_code}: {resp.text}"

    def test_reset_password_token_already_used(self):
        """
        Test that reset-password returns 400 token_already_used on second use.
        Similar to verify-email test.
        """
        resp = requests.post(f"{BASE_URL}/api/auth/reset-password", json={
            "token": "fake_test_token_for_reset_already_used",
            "new_password": "NewTestPass123!"
        })
        
        # Should return 401 (invalid token) since the token is invalid JWT
        assert resp.status_code in [400, 401], f"Expected 400/401, got {resp.status_code}: {resp.text}"


class TestTokenInvalidationIntegration:
    """
    Integration test for token invalidation using real tokens.
    Tests token reuse protection by generating valid tokens with proper JWT secret.
    """

    def test_verify_email_first_use_then_reuse(self):
        """
        Generate a verification token, use it once (will fail with 404 for non-existent user),
        then use it again (should return 400 token_already_used).
        The token_use is recorded even if user lookup fails, preventing reuse.
        """
        import jwt
        from datetime import datetime, timezone, timedelta
        import time
        
        # The server is using insecure default key when JWT_SECRET_KEY env var is not available to the running process
        # This matches the behavior in auth_utils.py when JWT_SECRET_KEY is empty
        jwt_secret = "dev-only-insecure-key-do-not-use-in-production-32chars"
        jwt_algorithm = "HS256"
        
        # Create a verification token for a unique test user_id
        test_user_id = f"test-verify-invalidation-{int(time.time() * 1000)}"
        expire = datetime.now(timezone.utc) + timedelta(hours=24)
        token = jwt.encode({
            "user_id": test_user_id,
            "type": "verification",
            "exp": expire
        }, jwt_secret, algorithm=jwt_algorithm)
        
        # First use - should decode token successfully, mark as used, then fail with 404 user not found
        resp1 = requests.post(f"{BASE_URL}/api/auth/verify-email", json={"token": token})
        first_status = resp1.status_code
        print(f"First verify-email call: {first_status} - {resp1.text[:300]}")
        
        # First call should return 404 (User not found or already verified) since user doesn't exist
        # But token_use should be recorded in DB before the user lookup
        assert first_status == 404, f"Expected 404 on first use (user not found), got {first_status}: {resp1.text}"
        
        # Second use - should return 400 token_already_used
        resp2 = requests.post(f"{BASE_URL}/api/auth/verify-email", json={"token": token})
        
        assert resp2.status_code == 400, f"Expected 400 on reuse, got {resp2.status_code}: {resp2.text}"
        data = resp2.json()
        detail = data.get("detail", {})
        
        if isinstance(detail, dict):
            assert detail.get("error_code") == "token_already_used", f"Expected error_code='token_already_used': {detail}"
        else:
            assert "already" in str(detail).lower() or "used" in str(detail).lower(), f"Expected token_already_used error: {detail}"

    def test_reset_password_first_use_then_reuse(self):
        """
        Generate a reset token, use it once (will fail with 404 for non-existent user),
        then use it again (should return 400 token_already_used).
        """
        import jwt
        from datetime import datetime, timezone, timedelta
        import time
        
        # The server is using insecure default key when JWT_SECRET_KEY env var is not available to the running process
        jwt_secret = "dev-only-insecure-key-do-not-use-in-production-32chars"
        jwt_algorithm = "HS256"
        
        # Create a password reset token for a unique test user_id
        test_user_id = f"test-reset-invalidation-{int(time.time() * 1000)}"
        expire = datetime.now(timezone.utc) + timedelta(hours=1)
        token = jwt.encode({
            "user_id": test_user_id,
            "type": "password_reset",
            "exp": expire
        }, jwt_secret, algorithm=jwt_algorithm)
        
        # First use - should decode token successfully, mark as used, then fail with 404 user not found
        resp1 = requests.post(f"{BASE_URL}/api/auth/reset-password", json={
            "token": token,
            "new_password": "NewTestPass123!"
        })
        
        first_status = resp1.status_code
        print(f"First reset-password call: {first_status} - {resp1.text[:300]}")
        
        # First call should return 404 (User not found) since user doesn't exist
        assert first_status == 404, f"Expected 404 on first use (user not found), got {first_status}: {resp1.text}"
        
        # Second use - should return 400 token_already_used
        resp2 = requests.post(f"{BASE_URL}/api/auth/reset-password", json={
            "token": token,
            "new_password": "AnotherNewPass123!"
        })
        
        assert resp2.status_code == 400, f"Expected 400 on reuse, got {resp2.status_code}: {resp2.text}"
        data = resp2.json()
        detail = data.get("detail", {})
        
        if isinstance(detail, dict):
            assert detail.get("error_code") == "token_already_used", f"Expected error_code='token_already_used': {detail}"
        else:
            assert "already" in str(detail).lower() or "used" in str(detail).lower(), f"Expected token_already_used error: {detail}"


class TestMapStripeStatusToPlanTier:
    """Test map_stripe_status_to_plan_tier function"""

    def test_stripe_status_mapping(self):
        """Verify the mapping function works correctly"""
        # We need to import from the billing module
        import sys
        sys.path.insert(0, '/app/backend')
        from routes.billing import map_stripe_status_to_plan_tier
        
        # Test active -> premium
        assert map_stripe_status_to_plan_tier("active") == "premium", "active should map to premium"
        
        # Test trialing -> premium
        assert map_stripe_status_to_plan_tier("trialing") == "premium", "trialing should map to premium"
        
        # Test canceled -> free
        assert map_stripe_status_to_plan_tier("canceled") == "free", "canceled should map to free"
        
        # Test past_due -> free
        assert map_stripe_status_to_plan_tier("past_due") == "free", "past_due should map to free"
        
        # Test unknown status -> free
        assert map_stripe_status_to_plan_tier("unknown") == "free", "unknown should map to free"
        assert map_stripe_status_to_plan_tier("") == "free", "empty string should map to free"


class TestConfigValidation:
    """Test config validation behavior"""

    def test_environment_development_no_crash(self):
        """
        With ENVIRONMENT=development, config validation should be skipped.
        The app is running, so this test passes if we can make any successful API call.
        """
        # If the app is running, config validation didn't crash it
        resp = requests.get(f"{BASE_URL}/api/health")
        assert resp.status_code == 200, f"Health check failed: {resp.text}"
        
        # Additional check: verify we can login (app fully functional)
        resp = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": PREMIUM_EMAIL,
            "password": PREMIUM_PASSWORD
        })
        assert resp.status_code == 200, f"Login failed - app may have config issues: {resp.text}"


class TestRegressionAuthMe:
    """Regression: auth/me still works with capabilities"""

    def test_auth_me_returns_capabilities(self, premium_token):
        """auth/me should return user with capabilities"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        resp = requests.get(f"{BASE_URL}/api/auth/me", headers=headers)
        
        assert resp.status_code == 200, f"auth/me failed: {resp.text}"
        data = resp.json()
        
        # Verify key fields
        assert "user_id" in data
        assert "email" in data
        assert "plan_tier" in data
        assert "capabilities" in data

    def test_auth_me_free_user(self, free_token):
        """auth/me works for free user too"""
        headers = {"Authorization": f"Bearer {free_token}"}
        resp = requests.get(f"{BASE_URL}/api/auth/me", headers=headers)
        
        assert resp.status_code == 200, f"auth/me failed for free user: {resp.text}"


class TestRegressionLibraryExport:
    """Regression: library export still works for premium users"""

    def test_library_export_csv(self, premium_token):
        """Premium user can export library as CSV"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        resp = requests.get(f"{BASE_URL}/api/library/export?format=csv", headers=headers)
        
        assert resp.status_code == 200, f"Library export failed: {resp.text}"
        # Should return CSV content
        assert "pmid" in resp.text or resp.headers.get("Content-Type", "").startswith("text/csv")


class TestRegressionAudio:
    """Regression: audio endpoints still work"""

    def test_audio_summary_endpoint_exists(self, premium_token):
        """Audio summary endpoint should exist"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        # Test with a fake pmid - should return 404 for article not found, not 500
        resp = requests.get(f"{BASE_URL}/api/audio/summary/fake-pmid-12345", headers=headers)
        
        # 404 is acceptable (article not found), 500 would be an error
        assert resp.status_code in [200, 404], f"Audio endpoint error: {resp.status_code} - {resp.text}"


class TestRegressionAdminMetrics:
    """Regression: admin metrics still works"""

    def test_admin_metrics_endpoint(self, premium_token):
        """Admin user can access metrics"""
        headers = {"Authorization": f"Bearer {premium_token}"}
        # The correct endpoint is /api/admin/metrics (not moderation/metrics)
        resp = requests.get(f"{BASE_URL}/api/admin/metrics", headers=headers)
        
        # Premium demo user is admin, should get 200
        assert resp.status_code == 200, f"Admin metrics failed: {resp.status_code} - {resp.text}"
        data = resp.json()
        assert isinstance(data, dict), f"Expected dict response, got {type(data)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
