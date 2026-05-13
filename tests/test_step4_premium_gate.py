"""
Test Step 4: Premium Value Gate v1 + Admin Moderation Summary Widget
- Library export (CSV/RIS) for premium only
- Run-now quota enforcement behind ENFORCE_RUN_NOW_QUOTA=true flag
- Admin set-plan-tier endpoint
- GET /api/auth/me capabilities for premium vs free users
- GET /api/admin/metrics includes moderation summary widget
- GET /api/config/feature-flags includes enforce_run_now_quota

Credentials:
- Admin/Premium: demo@litpulse.com / DemoPass123!
- Free user: test@litpulse.com / TestPass123!
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
# Feature Flags Tests
# ==============================================================================

class TestFeatureFlags:
    """Test GET /api/config/feature-flags includes enforce_run_now_quota"""

    def test_feature_flags_includes_enforce_run_now_quota(self, admin_token):
        """BACKEND: GET /api/config/feature-flags includes enforce_run_now_quota=true"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags", headers=auth_header(admin_token))
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        assert "enforce_run_now_quota" in data, "enforce_run_now_quota not in feature flags"
        assert data["enforce_run_now_quota"] == True, f"Expected enforce_run_now_quota=true, got {data['enforce_run_now_quota']}"
        print(f"✓ Feature flags include enforce_run_now_quota={data['enforce_run_now_quota']}")


# ==============================================================================
# Capabilities Tests for Premium vs Free Users
# ==============================================================================

class TestUserCapabilities:
    """Test GET /api/auth/me returns proper capabilities based on plan_tier"""

    def test_premium_user_capabilities(self, admin_token):
        """BACKEND: GET /api/auth/me for premium user shows premium capabilities"""
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(admin_token))
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Check plan_tier
        assert data.get("plan_tier") == "premium", f"Expected plan_tier=premium, got {data.get('plan_tier')}"
        
        # Check capabilities
        caps = data.get("capabilities", {})
        assert caps.get("premium_export_csv") == True, f"premium_export_csv should be True for premium: {caps}"
        assert caps.get("premium_export_ris") == True, f"premium_export_ris should be True for premium: {caps}"
        assert caps.get("max_articles_per_digest") == 25, f"max_articles_per_digest should be 25 for premium: {caps.get('max_articles_per_digest')}"
        assert caps.get("run_now_per_24h") == 5, f"run_now_per_24h should be 5 for premium: {caps.get('run_now_per_24h')}"
        
        print(f"✓ Premium user has correct capabilities: export={caps.get('premium_export_csv')}, max_articles={caps.get('max_articles_per_digest')}, run_now={caps.get('run_now_per_24h')}")

    def test_free_user_capabilities(self, free_token):
        """BACKEND: GET /api/auth/me for free user shows free capabilities"""
        response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token))
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Check plan_tier (should be free or missing)
        plan_tier = data.get("plan_tier", "free")
        assert plan_tier == "free", f"Expected plan_tier=free, got {plan_tier}"
        
        # Check capabilities
        caps = data.get("capabilities", {})
        assert caps.get("premium_export_csv") == False, f"premium_export_csv should be False for free: {caps}"
        assert caps.get("premium_export_ris") == False, f"premium_export_ris should be False for free: {caps}"
        assert caps.get("max_articles_per_digest") == 10, f"max_articles_per_digest should be 10 for free: {caps.get('max_articles_per_digest')}"
        assert caps.get("run_now_per_24h") == 1, f"run_now_per_24h should be 1 for free: {caps.get('run_now_per_24h')}"
        
        print(f"✓ Free user has correct capabilities: export={caps.get('premium_export_csv')}, max_articles={caps.get('max_articles_per_digest')}, run_now={caps.get('run_now_per_24h')}")


# ==============================================================================
# Library Export Tests (Premium-Only Feature)
# ==============================================================================

class TestLibraryExport:
    """Test library export endpoints - premium only"""

    def test_free_user_export_csv_blocked(self, free_token):
        """BACKEND: Free user GET /api/library/export?format=csv -> 403 premium_required"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=csv",
            headers=auth_header(free_token)
        )
        assert response.status_code == 403, f"Expected 403 for free user export, got {response.status_code}"
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "premium_required", f"Expected error_code=premium_required: {data}"
        print("✓ Free user blocked from CSV export with 403 premium_required")

    def test_free_user_export_ris_blocked(self, free_token):
        """BACKEND: Free user GET /api/library/export?format=ris -> 403 premium_required"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=ris",
            headers=auth_header(free_token)
        )
        assert response.status_code == 403, f"Expected 403 for free user export, got {response.status_code}"
        data = response.json()
        assert data.get("detail", {}).get("error_code") == "premium_required", f"Expected error_code=premium_required: {data}"
        print("✓ Free user blocked from RIS export with 403 premium_required")

    def test_premium_user_export_csv(self, admin_token):
        """BACKEND: Premium user GET /api/library/export?format=csv -> 200 with CSV content"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=csv",
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Expected 200 for premium CSV export, got {response.status_code}: {response.text}"
        
        # Check content-disposition header
        content_disp = response.headers.get("content-disposition", "")
        assert "litpulse_library_" in content_disp and ".csv" in content_disp, f"Expected CSV filename in Content-Disposition: {content_disp}"
        
        # Check content-type
        content_type = response.headers.get("content-type", "")
        assert "text/csv" in content_type, f"Expected text/csv content-type: {content_type}"
        
        # Check CSV headers in content
        content = response.text
        assert "pmid" in content and "title" in content, f"CSV should have headers pmid,title. Got: {content[:200]}"
        
        print(f"✓ Premium user can export CSV. Content-Disposition: {content_disp}")

    def test_premium_user_export_ris(self, admin_token):
        """BACKEND: Premium user GET /api/library/export?format=ris -> 200 with RIS content"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=ris",
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Expected 200 for premium RIS export, got {response.status_code}: {response.text}"
        
        # Check content-disposition header
        content_disp = response.headers.get("content-disposition", "")
        assert "litpulse_library_" in content_disp and ".ris" in content_disp, f"Expected RIS filename in Content-Disposition: {content_disp}"
        
        # Check content-type
        content_type = response.headers.get("content-type", "")
        assert "application/x-research-info-systems" in content_type, f"Expected RIS content-type: {content_type}"
        
        print(f"✓ Premium user can export RIS. Content-Disposition: {content_disp}")


# ==============================================================================
# Admin Set Plan Tier Tests
# ==============================================================================

class TestAdminSetPlanTier:
    """Test POST /api/admin/users/set-plan-tier endpoint"""

    def test_non_admin_set_plan_tier_blocked(self, free_token):
        """BACKEND: Non-admin POST /api/admin/users/set-plan-tier -> 403"""
        response = requests.post(
            f"{BASE_URL}/api/admin/users/set-plan-tier",
            json={"email": "test@litpulse.com", "plan_tier": "premium"},
            headers=auth_header(free_token)
        )
        assert response.status_code == 403, f"Expected 403 for non-admin, got {response.status_code}: {response.text}"
        print("✓ Non-admin blocked from set-plan-tier with 403")

    def test_admin_set_plan_tier_to_free(self, admin_token, free_token):
        """BACKEND: Admin POST /api/admin/users/set-plan-tier -> changes user plan_tier"""
        # First, verify current plan_tier
        me_response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token))
        original_tier = me_response.json().get("plan_tier", "free")
        
        # Set to premium
        response = requests.post(
            f"{BASE_URL}/api/admin/users/set-plan-tier",
            json={"email": "test@litpulse.com", "plan_tier": "premium"},
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["plan_tier"] == "premium", f"Expected plan_tier=premium in response: {data}"
        
        # Verify the user now has premium capabilities
        me_response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token))
        assert me_response.json().get("plan_tier") == "premium", "User should now have premium plan_tier"
        
        # Verify premium capabilities
        caps = me_response.json().get("capabilities", {})
        assert caps.get("premium_export_csv") == True, "User should now have premium_export_csv=True"
        
        print("✓ Admin successfully set user to premium tier")
        
        # Restore to free
        response = requests.post(
            f"{BASE_URL}/api/admin/users/set-plan-tier",
            json={"email": "test@litpulse.com", "plan_tier": "free"},
            headers=auth_header(admin_token)
        )
        assert response.status_code == 200, f"Expected 200 restoring to free: {response.text}"
        
        # Verify restored
        me_response = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token))
        assert me_response.json().get("plan_tier") == "free", "User should be restored to free tier"
        caps = me_response.json().get("capabilities", {})
        assert caps.get("premium_export_csv") == False, "User should have premium_export_csv=False after restore"
        
        print("✓ Admin successfully restored user to free tier")


# ==============================================================================
# Admin Metrics Moderation Summary Tests
# ==============================================================================

class TestAdminMetricsModeration:
    """Test GET /api/admin/metrics includes moderation summary widget"""

    def test_admin_metrics_includes_moderation_section(self, admin_token):
        """BACKEND: GET /api/admin/metrics includes moderation with pending_reports, avg_resolution_time, phi_timeseries"""
        response = requests.get(f"{BASE_URL}/api/admin/metrics", headers=auth_header(admin_token))
        assert response.status_code == 200, f"Failed: {response.text}"
        data = response.json()
        
        # Check moderation section exists
        assert "moderation" in data, f"moderation section missing from metrics: {data.keys()}"
        mod = data["moderation"]
        
        # Check required fields
        assert "pending_reports_total" in mod, f"pending_reports_total missing: {mod.keys()}"
        assert "pending_reports_phi" in mod, f"pending_reports_phi missing: {mod.keys()}"
        assert "avg_resolution_time_hours_last_30d" in mod, f"avg_resolution_time missing: {mod.keys()}"
        assert "phi_reports_timeseries_last_14d" in mod, f"phi_reports_timeseries_last_14d missing: {mod.keys()}"
        
        # Check phi_timeseries structure
        phi_ts = mod["phi_reports_timeseries_last_14d"]
        assert isinstance(phi_ts, list), f"phi_timeseries should be a list: {type(phi_ts)}"
        assert len(phi_ts) == 14, f"phi_timeseries should have 14 entries: {len(phi_ts)}"
        
        # Check each entry has date and count
        for entry in phi_ts:
            assert "date" in entry, f"Entry missing date: {entry}"
            assert "count" in entry, f"Entry missing count: {entry}"
        
        print(f"✓ Admin metrics includes moderation summary: pending={mod['pending_reports_total']}, phi={mod['pending_reports_phi']}, timeseries_len={len(phi_ts)}")


# ==============================================================================
# Run-Now Quota Enforcement Tests
# ==============================================================================

class TestRunNowQuota:
    """Test POST /api/digests/run-now quota enforcement"""

    def test_run_now_quota_info_in_capabilities(self, admin_token, free_token):
        """Verify run_now_per_24h capability differs for premium vs free"""
        # Premium user
        premium_me = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(admin_token)).json()
        assert premium_me.get("capabilities", {}).get("run_now_per_24h") == 5, "Premium should have 5 run_now_per_24h"
        
        # Free user
        free_me = requests.get(f"{BASE_URL}/api/auth/me", headers=auth_header(free_token)).json()
        assert free_me.get("capabilities", {}).get("run_now_per_24h") == 1, "Free should have 1 run_now_per_24h"
        
        print("✓ run_now_per_24h capability correctly set: premium=5, free=1")

    def test_run_now_endpoint_exists(self, admin_token):
        """Verify run-now endpoint accepts requests (may return 400 if no preferences)"""
        # This test just verifies the endpoint exists and quota check runs
        response = requests.post(
            f"{BASE_URL}/api/digests/run-now",
            json={"send_email": False},
            headers=auth_header(admin_token)
        )
        # Either 200 success, 400 (no preferences), or 429 (quota exceeded)
        assert response.status_code in [200, 400, 429], f"Unexpected status: {response.status_code}: {response.text}"
        
        if response.status_code == 429:
            data = response.json()
            assert data.get("detail", {}).get("error_code") == "run_now_quota_exceeded", f"Expected run_now_quota_exceeded: {data}"
            print("✓ Run-now endpoint returns 429 with run_now_quota_exceeded when quota exceeded")
        elif response.status_code == 400:
            print("✓ Run-now endpoint returns 400 (no preferences set) - endpoint working")
        else:
            print("✓ Run-now endpoint returned 200 - digest generated successfully")


# ==============================================================================
# Non-Admin Cannot Access Admin Endpoints
# ==============================================================================

class TestAdminAccessControl:
    """Verify non-admin users cannot access admin endpoints"""

    def test_non_admin_cannot_get_metrics(self, free_token):
        """Non-admin user cannot access GET /api/admin/metrics"""
        response = requests.get(f"{BASE_URL}/api/admin/metrics", headers=auth_header(free_token))
        assert response.status_code == 403, f"Expected 403 for non-admin metrics access: {response.status_code}"
        print("✓ Non-admin blocked from /api/admin/metrics with 403")

    def test_non_admin_cannot_get_reports(self, free_token):
        """Non-admin user cannot access GET /api/admin/reports"""
        response = requests.get(f"{BASE_URL}/api/admin/reports", headers=auth_header(free_token))
        assert response.status_code == 403, f"Expected 403 for non-admin reports access: {response.status_code}"
        print("✓ Non-admin blocked from /api/admin/reports with 403")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
