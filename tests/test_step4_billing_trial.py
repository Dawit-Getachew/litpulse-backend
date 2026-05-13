"""
Test Step 4 Extended: 14-day Pro trial on signup + Stripe Checkout

New features tested:
- GET /api/auth/me includes trial_ends_at, trial_active, has_subscription
- POST /api/billing/checkout creates Stripe session and returns url + session_id  
- GET /api/billing/checkout/status/{session_id} returns payment status
- Non-authenticated user cannot access /api/billing/checkout (401)
- Library export still works for premium (200), blocked for free (403)
- Admin set-plan-tier still works
- Admin metrics still includes moderation section

Credentials:
- Admin/Premium: demo@litpulse.com / DemoPass123! (has subscription_level=2, has_subscription=true)
- Free user: test@litpulse.com / TestPass123! (no trial_ends_at, trial_active=false)
"""
import pytest
import requests
import os
from datetime import datetime, timezone

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


@pytest.fixture(scope="module")
def admin_token():
    """Get token for admin/premium user (demo@litpulse.com)"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json=ADMIN_PREMIUM_USER)
    assert response.status_code == 200, f"Admin login failed: {response.text}"
    return response.json()["access_token"]


@pytest.fixture(scope="module")
def free_token():
    """Get token for free user (test@litpulse.com)"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json=FREE_USER)
    assert response.status_code == 200, f"Free user login failed: {response.text}"
    return response.json()["access_token"]


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


# ==============================================================================
# /api/auth/me Tests - Trial and Subscription Fields
# ==============================================================================

class TestAuthMeTrialSubscription:
    """Test GET /api/auth/me includes trial and subscription fields"""

    def test_premium_user_has_subscription(self, admin_token):
        """BACKEND: GET /api/auth/me for premium user shows plan_tier=premium, has_subscription=true, trial_active=false"""
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(admin_token))
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Verify plan_tier
        assert data.get("plan_tier") == "premium", f"Expected plan_tier=premium, got {data.get('plan_tier')}"
        
        # Verify has_subscription (demo user has subscription_level=2 so has_subscription=true)
        assert data.get("has_subscription") == True, f"Expected has_subscription=true for premium user, got {data.get('has_subscription')}"
        
        # Verify trial_active (premium via subscription, not trial)
        assert data.get("trial_active") == False, f"Expected trial_active=false for premium via subscription, got {data.get('trial_active')}"
        
        print(f"✓ Premium user: plan_tier={data['plan_tier']}, has_subscription={data['has_subscription']}, trial_active={data['trial_active']}")

    def test_free_user_no_subscription_no_trial(self, free_token):
        """BACKEND: GET /api/auth/me for free user shows plan_tier=free, trial_active=false, has_subscription=false"""
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token))
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Verify plan_tier
        assert data.get("plan_tier") == "free", f"Expected plan_tier=free, got {data.get('plan_tier')}"
        
        # Verify has_subscription (free user has no subscription)
        assert data.get("has_subscription") == False, f"Expected has_subscription=false for free user, got {data.get('has_subscription')}"
        
        # Verify trial_active (existing user created before trial feature, no trial_ends_at)
        assert data.get("trial_active") == False, f"Expected trial_active=false for existing free user, got {data.get('trial_active')}"
        
        print(f"✓ Free user: plan_tier={data['plan_tier']}, has_subscription={data['has_subscription']}, trial_active={data['trial_active']}")


# ==============================================================================
# Billing Checkout Tests - Stripe Session Creation
# ==============================================================================

class TestBillingCheckout:
    """Test POST /api/billing/checkout and GET /api/billing/checkout/status/{session_id}"""

    def test_checkout_requires_authentication(self):
        """BACKEND: Non-authenticated user cannot access /api/billing/checkout (401)"""
        response = requests.post(
            f"{BASE_URL}/api/billing/checkout",
            json={"origin_url": "https://litscreen-aggregate.preview.emergentagent.com"}
        )
        assert response.status_code == 401 or response.status_code == 422, f"Expected 401/422 without auth, got {response.status_code}: {response.text}"
        print("✓ Non-authenticated user blocked from billing checkout")

    def test_checkout_creates_stripe_session(self, admin_token):
        """BACKEND: POST /api/billing/checkout creates Stripe session and returns url + session_id"""
        response = requests.post(
            f"{BASE_URL}/api/billing/checkout",
            json={"origin_url": "https://litscreen-aggregate.preview.emergentagent.com"},
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Failed to create checkout session: {response.status_code}: {response.text}"
        data = response.json()
        
        # Verify response has url and session_id
        assert "url" in data, f"Response missing 'url': {data}"
        assert "session_id" in data, f"Response missing 'session_id': {data}"
        
        # Verify URL is a real Stripe checkout URL
        assert data["url"].startswith("https://checkout.stripe.com"), f"Expected Stripe checkout URL, got: {data['url'][:50]}..."
        
        # Verify session_id format (starts with cs_test_ for test mode)
        assert data["session_id"].startswith("cs_test_"), f"Expected test session ID starting with cs_test_, got: {data['session_id'][:20]}..."
        
        print(f"✓ Checkout session created: session_id={data['session_id'][:30]}..., url starts with https://checkout.stripe.com")
        return data["session_id"]

    def test_checkout_status_returns_payment_info(self, admin_token):
        """BACKEND: GET /api/billing/checkout/status/{session_id} returns status"""
        # First create a session
        create_response = requests.post(
            f"{BASE_URL}/api/billing/checkout",
            json={"origin_url": "https://litscreen-aggregate.preview.emergentagent.com"},
            headers=auth_header(admin_token)
        )
        assert create_response.status_code == 200, f"Failed to create session: {create_response.text}"
        session_id = create_response.json()["session_id"]
        
        # Get status
        status_response = requests.get(
            f"{BASE_URL}/api/billing/checkout/status/{session_id}",
            headers=auth_header(admin_token)
        )
        assert status_response.status_code == 200, f"Failed to get status: {status_response.status_code}: {status_response.text}"
        data = status_response.json()
        
        # Verify response has expected fields
        assert "status" in data, f"Response missing 'status': {data}"
        assert "payment_status" in data, f"Response missing 'payment_status': {data}"
        assert "amount_total" in data, f"Response missing 'amount_total': {data}"
        assert "currency" in data, f"Response missing 'currency': {data}"
        
        # New session should be open/unpaid
        assert data["status"] in ["open", "complete", "expired"], f"Unexpected status: {data['status']}"
        assert data["payment_status"] in ["unpaid", "paid", "no_payment_required"], f"Unexpected payment_status: {data['payment_status']}"
        assert data["currency"] == "usd", f"Expected currency=usd, got {data['currency']}"
        
        print(f"✓ Checkout status: status={data['status']}, payment_status={data['payment_status']}, amount={data['amount_total']}, currency={data['currency']}")


# ==============================================================================
# Library Export Tests (Premium-Only Feature) - Still Works
# ==============================================================================

class TestLibraryExportStillWorks:
    """Verify library export still works for premium, blocked for free"""

    def test_premium_can_export_csv(self, admin_token):
        """BACKEND: Library export still works for premium (200 CSV)"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=csv",
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Expected 200 for premium CSV export, got {response.status_code}: {response.text}"
        
        # Verify content-type
        content_type = response.headers.get("content-type", "")
        assert "text/csv" in content_type, f"Expected text/csv, got {content_type}"
        
        # Verify CSV has headers
        content = response.text
        assert "pmid" in content.lower() or "title" in content.lower(), f"CSV should have headers"
        
        print("✓ Premium user can export CSV")

    def test_free_user_blocked_from_export(self, free_token):
        """BACKEND: Library export blocked for free (403)"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=csv",
            headers=auth_header(free_token)
        )
        assert response.status_code == 403, f"Expected 403 for free user, got {response.status_code}: {response.text}"
        
        # Verify error code
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "premium_required", f"Expected premium_required error: {data}"
        
        print("✓ Free user blocked from export with 403 premium_required")


# ==============================================================================
# Admin Tests - Set Plan Tier and Metrics Still Work
# ==============================================================================

class TestAdminEndpointsStillWork:
    """Verify admin endpoints still work"""

    def test_admin_metrics_includes_moderation(self, admin_token):
        """BACKEND: Admin metrics still includes moderation section"""
        response = requests.get(f"{BASE_URL}/api/admin/metrics", headers=auth_header(admin_token))
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Check moderation section exists
        assert "moderation" in data, f"moderation section missing: {data.keys()}"
        mod = data["moderation"]
        
        # Check required fields
        assert "pending_reports_total" in mod, f"pending_reports_total missing"
        assert "pending_reports_phi" in mod, f"pending_reports_phi missing"
        assert "avg_resolution_time_hours_last_30d" in mod, f"avg_resolution_time missing"
        assert "phi_reports_timeseries_last_14d" in mod, f"phi_timeseries missing"
        
        print(f"✓ Admin metrics includes moderation: pending={mod['pending_reports_total']}, phi={mod['pending_reports_phi']}")

    def test_admin_set_plan_tier_still_works(self, admin_token, free_token):
        """BACKEND: Admin set-plan-tier still works"""
        # Set to premium
        response = requests.post(
            f"{BASE_URL}/api/admin/users/set-plan-tier",
            json={"email": "test@litpulse.com", "plan_tier": "premium"},
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Failed to set plan tier: {response.text}"
        
        # Verify change
        me_response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token))
        assert me_response.json().get("plan_tier") == "premium", "Plan tier should be premium"
        
        # Restore to free
        response = requests.post(
            f"{BASE_URL}/api/admin/users/set-plan-tier",
            json={"email": "test@litpulse.com", "plan_tier": "free"},
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Failed to restore: {response.text}"
        
        # Verify restored
        me_response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token))
        assert me_response.json().get("plan_tier") == "free", "Plan tier should be restored to free"
        
        print("✓ Admin set-plan-tier works correctly")


# ==============================================================================
# Non-Admin Access Control Tests
# ==============================================================================

class TestNonAdminAccessBlocked:
    """Verify non-admin users cannot access admin endpoints"""

    def test_non_admin_blocked_from_metrics(self, free_token):
        """Non-admin cannot access /api/admin/metrics"""
        response = requests.get(f"{BASE_URL}/api/admin/metrics", headers=auth_header(free_token))
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        print("✓ Non-admin blocked from metrics")

    def test_non_admin_blocked_from_reports(self, free_token):
        """Non-admin cannot access /api/admin/reports"""
        response = requests.get(f"{BASE_URL}/api/admin/reports", headers=auth_header(free_token))
        assert response.status_code == 403, f"Expected 403, got {response.status_code}"
        print("✓ Non-admin blocked from reports")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
