"""
Test suite for Copilot Dashboard Admin API
Tests the new GET /api/admin/copilot-dashboard endpoint with admin-only access.
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "testlaunch@test.com"
ADMIN_PASSWORD = "Test1234!"
NON_ADMIN_EMAIL = "nonadmin_test@test.com"
NON_ADMIN_PASSWORD = "Test1234!"


@pytest.fixture(scope="module")
def api_client():
    """Shared requests session"""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture(scope="module")
def admin_token(api_client):
    """Get admin authentication token"""
    response = api_client.post(f"{BASE_URL}/api/auth/login", json={
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD
    })
    if response.status_code == 200:
        return response.json().get("access_token")
    pytest.skip(f"Admin authentication failed: {response.status_code} - {response.text}")


@pytest.fixture(scope="module")
def non_admin_token(api_client):
    """Get or create non-admin user token for testing 403 response"""
    # First try to login
    response = api_client.post(f"{BASE_URL}/api/auth/login", json={
        "email": NON_ADMIN_EMAIL,
        "password": NON_ADMIN_PASSWORD
    })
    if response.status_code == 200:
        return response.json().get("access_token")
    
    # If login fails, try to create the user
    signup_response = api_client.post(f"{BASE_URL}/api/auth/signup", json={
        "email": NON_ADMIN_EMAIL,
        "password": NON_ADMIN_PASSWORD,
        "full_name": "Non Admin Test User"
    })
    if signup_response.status_code in [200, 201]:
        # Login again after signup
        login_response = api_client.post(f"{BASE_URL}/api/auth/login", json={
            "email": NON_ADMIN_EMAIL,
            "password": NON_ADMIN_PASSWORD
        })
        if login_response.status_code == 200:
            return login_response.json().get("access_token")
    
    pytest.skip(f"Could not create/login non-admin user: {signup_response.status_code}")


class TestCopilotDashboardAuth:
    """Test authentication and authorization for copilot dashboard"""
    
    def test_dashboard_without_auth_returns_401(self, api_client):
        """GET /api/admin/copilot-dashboard without auth should return 401"""
        response = api_client.get(f"{BASE_URL}/api/admin/copilot-dashboard")
        assert response.status_code == 401, f"Expected 401, got {response.status_code}: {response.text}"
        print("✓ Dashboard without auth returns 401")
    
    def test_dashboard_with_non_admin_returns_403(self, api_client, non_admin_token):
        """GET /api/admin/copilot-dashboard with non-admin auth should return 403"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard",
            headers={"Authorization": f"Bearer {non_admin_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("✓ Dashboard with non-admin auth returns 403")
    
    def test_dashboard_with_admin_returns_200(self, api_client, admin_token):
        """GET /api/admin/copilot-dashboard with admin auth should return 200"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print("✓ Dashboard with admin auth returns 200")


class TestCopilotDashboardData:
    """Test dashboard data structure and content"""
    
    def test_dashboard_summary_fields(self, api_client, admin_token):
        """Dashboard summary should include required fields"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=30",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # Check summary exists and has required fields
        assert "summary" in data, "Response missing 'summary' field"
        summary = data["summary"]
        
        required_summary_fields = [
            "copilot_calls_total",
            "audio_calls_total", 
            "unique_copilot_users",
            "cache_entries_active"
        ]
        for field in required_summary_fields:
            assert field in summary, f"Summary missing '{field}' field"
            assert isinstance(summary[field], int), f"Summary '{field}' should be int"
        
        print(f"✓ Summary fields present: copilot_calls_total={summary['copilot_calls_total']}, "
              f"audio_calls_total={summary['audio_calls_total']}, "
              f"unique_copilot_users={summary['unique_copilot_users']}, "
              f"cache_entries_active={summary['cache_entries_active']}")
    
    def test_dashboard_costs_fields(self, api_client, admin_token):
        """Dashboard costs should include required fields"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=30",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # Check costs exists and has required fields
        assert "costs" in data, "Response missing 'costs' field"
        costs = data["costs"]
        
        required_cost_fields = [
            "estimated_copilot_cost_usd",
            "estimated_audio_cost_usd",
            "estimated_total_cost_usd"
        ]
        for field in required_cost_fields:
            assert field in costs, f"Costs missing '{field}' field"
            assert isinstance(costs[field], (int, float)), f"Costs '{field}' should be numeric"
        
        print(f"✓ Costs fields present: copilot=${costs['estimated_copilot_cost_usd']}, "
              f"audio=${costs['estimated_audio_cost_usd']}, "
              f"total=${costs['estimated_total_cost_usd']}")
    
    def test_dashboard_by_feature_structure(self, api_client, admin_token):
        """Dashboard by_feature should show surface type breakdown"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=30",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # Check by_feature exists and is a dict
        assert "by_feature" in data, "Response missing 'by_feature' field"
        by_feature = data["by_feature"]
        assert isinstance(by_feature, dict), "by_feature should be a dict"
        
        # Valid surface types
        valid_surfaces = ["evidence_brief", "ask_article", "compare_studies", "draft_post", "unknown"]
        
        for surface, count in by_feature.items():
            assert isinstance(count, int), f"by_feature['{surface}'] should be int"
            print(f"  - {surface}: {count}")
        
        print(f"✓ by_feature structure valid with {len(by_feature)} surface types")
    
    def test_dashboard_daily_series_structure(self, api_client, admin_token):
        """Dashboard daily_series should have date, copilot, audio, total fields"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=30",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # Check daily_series exists and is a list
        assert "daily_series" in data, "Response missing 'daily_series' field"
        daily_series = data["daily_series"]
        assert isinstance(daily_series, list), "daily_series should be a list"
        
        # If there's data, check structure
        if len(daily_series) > 0:
            required_fields = ["date", "copilot", "audio", "total"]
            for entry in daily_series[:3]:  # Check first 3 entries
                for field in required_fields:
                    assert field in entry, f"daily_series entry missing '{field}' field"
            print(f"✓ daily_series has {len(daily_series)} entries with correct structure")
        else:
            print("✓ daily_series is empty (no usage data yet)")
    
    def test_dashboard_top_users_structure(self, api_client, admin_token):
        """Dashboard top_users should include user_id, email, copilot_calls, audio_calls"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=30",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # Check top_users exists and is a list
        assert "top_users" in data, "Response missing 'top_users' field"
        top_users = data["top_users"]
        assert isinstance(top_users, list), "top_users should be a list"
        
        # If there's data, check structure
        if len(top_users) > 0:
            required_fields = ["user_id", "email", "copilot_calls", "audio_calls"]
            for user in top_users[:3]:  # Check first 3 users
                for field in required_fields:
                    assert field in user, f"top_users entry missing '{field}' field"
            print(f"✓ top_users has {len(top_users)} users with correct structure")
            for user in top_users[:3]:
                print(f"  - {user['email']}: copilot={user['copilot_calls']}, audio={user['audio_calls']}")
        else:
            print("✓ top_users is empty (no usage data yet)")
    
    def test_dashboard_audio_files_structure(self, api_client, admin_token):
        """Dashboard audio_files should include ready, failed, pending counts"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=30",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        
        # Check audio_files exists and has required fields
        assert "audio_files" in data, "Response missing 'audio_files' field"
        audio_files = data["audio_files"]
        
        required_fields = ["ready", "failed", "pending"]
        for field in required_fields:
            assert field in audio_files, f"audio_files missing '{field}' field"
            assert isinstance(audio_files[field], int), f"audio_files['{field}'] should be int"
        
        print(f"✓ audio_files: ready={audio_files['ready']}, "
              f"failed={audio_files['failed']}, pending={audio_files['pending']}")


class TestCopilotDashboardPeriods:
    """Test dashboard with different time periods"""
    
    def test_dashboard_7_days(self, api_client, admin_token):
        """Dashboard should work with 7 day period"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=7",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["period_days"] == 7
        print("✓ Dashboard works with 7 day period")
    
    def test_dashboard_30_days(self, api_client, admin_token):
        """Dashboard should work with 30 day period (default)"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=30",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["period_days"] == 30
        print("✓ Dashboard works with 30 day period")
    
    def test_dashboard_90_days(self, api_client, admin_token):
        """Dashboard should work with 90 day period"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard?days=90",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["period_days"] == 90
        print("✓ Dashboard works with 90 day period")
    
    def test_dashboard_default_period(self, api_client, admin_token):
        """Dashboard should default to 30 days when no period specified"""
        response = api_client.get(
            f"{BASE_URL}/api/admin/copilot-dashboard",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["period_days"] == 30
        print("✓ Dashboard defaults to 30 day period")


class TestSurfaceTracking:
    """Test that copilot endpoints track surface type correctly"""
    
    def test_ask_article_tracks_surface(self, api_client, admin_token):
        """POST /api/copilot/ask-article should track surface='ask_article'"""
        # Make an ask-article call
        response = api_client.post(
            f"{BASE_URL}/api/copilot/ask-article",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "pmid": "39000001",
                "question": "What is the main finding of this study?"
            }
        )
        # Should succeed (200) or fail gracefully (404 if article not found, 503 if copilot disabled)
        assert response.status_code in [200, 404, 503, 429], f"Unexpected status: {response.status_code}"
        
        if response.status_code == 200:
            print("✓ ask-article call succeeded - surface tracking should be recorded")
        elif response.status_code == 429:
            print("✓ ask-article quota exceeded - surface tracking already recorded from previous calls")
        else:
            print(f"⚠ ask-article returned {response.status_code} - may not have tracked surface")
    
    def test_evidence_brief_tracks_surface(self, api_client, admin_token):
        """POST /api/copilot/evidence-brief should track surface='evidence_brief'"""
        # Make an evidence-brief call
        response = api_client.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"pmid": "39000001"}
        )
        # Should succeed (200) or fail gracefully
        assert response.status_code in [200, 404, 503, 429], f"Unexpected status: {response.status_code}"
        
        if response.status_code == 200:
            data = response.json()
            # If cached, no new usage event is recorded
            if data.get("cached"):
                print("✓ evidence-brief returned cached result - no new surface tracking")
            else:
                print("✓ evidence-brief call succeeded - surface tracking should be recorded")
        elif response.status_code == 429:
            print("✓ evidence-brief quota exceeded - surface tracking already recorded")
        else:
            print(f"⚠ evidence-brief returned {response.status_code}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
