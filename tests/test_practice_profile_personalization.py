"""
Test Practice Profile Personalization & Analytics Features
- GET/PUT /api/practice-profile endpoints
- GET /api/beta-admin/demographics 
- GET /api/beta-admin/user/{id} with practice_profile, personalization_signals
- Digest generation with/without practice profile
"""
import pytest
import requests
import os
import uuid

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

@pytest.fixture(scope="module")
def admin_token():
    """Login as admin user (demo@litpulse.com)"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": "demo@litpulse.com",
        "password": "DemoPass123!"
    })
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


class TestPracticeProfileGETPUT:
    """Test GET/PUT /api/practice-profile endpoints"""
    
    def test_01_get_practice_profile_returns_existing_data(self, admin_headers):
        """GET /api/practice-profile returns practice_profile for user with profile"""
        resp = requests.get(f"{BASE_URL}/api/practice-profile", headers=admin_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "practice_profile" in data, "Response should contain 'practice_profile' key"
        # Demo user should have a profile based on agent context
        profile = data["practice_profile"]
        if profile:
            # Validate it has expected structure (based on context: 9 fields for demo user)
            print(f"Practice profile found with fields: {list(profile.keys())}")
            assert isinstance(profile, dict), "practice_profile should be a dict"

    def test_02_put_practice_profile_partial_update(self, admin_headers):
        """PUT /api/practice-profile with partial data saves correctly"""
        partial_data = {
            "practice_profile": {
                "primary_specialty": "Cardiology",
                "current_stage": "attending"
            }
        }
        resp = requests.put(f"{BASE_URL}/api/practice-profile", 
                           headers=admin_headers, 
                           json=partial_data)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("practice_profile") is not None, "practice_profile should be returned"
        assert data["practice_profile"].get("primary_specialty") == "Cardiology"
        assert data["practice_profile"].get("current_stage") == "attending"
        assert "message" in data, "Should have success message"

    def test_03_get_practice_profile_after_save(self, admin_headers):
        """GET /api/practice-profile after save returns saved data"""
        resp = requests.get(f"{BASE_URL}/api/practice-profile", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        profile = data.get("practice_profile")
        assert profile is not None, "Profile should exist after save"
        assert profile.get("primary_specialty") == "Cardiology", "Should have saved specialty"

    def test_04_put_practice_profile_with_null_clears(self, admin_headers):
        """PUT /api/practice-profile with null clears profile"""
        resp = requests.put(f"{BASE_URL}/api/practice-profile", 
                           headers=admin_headers, 
                           json={"practice_profile": None})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("practice_profile") is None, "Profile should be cleared"
        assert "cleared" in data.get("message", "").lower(), "Message should indicate cleared"

    def test_05_get_practice_profile_after_clear_returns_null(self, admin_headers):
        """GET /api/practice-profile after clear returns null"""
        resp = requests.get(f"{BASE_URL}/api/practice-profile", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("practice_profile") is None, "Profile should be null after clear"

    def test_06_restore_demo_user_profile(self, admin_headers):
        """Restore demo user's practice profile for other tests"""
        # Restore demo user's profile (9 fields as per context)
        full_profile = {
            "practice_profile": {
                "primary_specialty": "Internal Medicine",
                "specialty_2": "Cardiology",
                "subspecialties": ["Heart Failure", "Interventional"],
                "current_stage": "attending",
                "years_in_practice": "6_10",
                "country": "United States",
                "state_province": "California",
                "practice_setting": "academic",
                "clinical_environment": "mixed"
            }
        }
        resp = requests.put(f"{BASE_URL}/api/practice-profile",
                           headers=admin_headers,
                           json=full_profile)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("practice_profile") is not None
        assert len(data["practice_profile"]) >= 9, f"Should have 9+ fields, got {len(data['practice_profile'])}"

    def test_07_put_empty_profile_clears(self, admin_headers):
        """PUT /api/practice-profile with empty dict clears profile"""
        resp = requests.put(f"{BASE_URL}/api/practice-profile",
                           headers=admin_headers,
                           json={"practice_profile": {}})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("practice_profile") is None, "Empty dict should clear profile"

    def test_08_restore_again_for_subsequent_tests(self, admin_headers):
        """Restore demo user profile again"""
        full_profile = {
            "practice_profile": {
                "primary_specialty": "Internal Medicine",
                "specialty_2": "Cardiology",
                "subspecialties": ["Heart Failure", "Interventional"],
                "current_stage": "attending",
                "years_in_practice": "6_10",
                "country": "United States",
                "state_province": "California",
                "practice_setting": "academic",
                "clinical_environment": "mixed"
            }
        }
        resp = requests.put(f"{BASE_URL}/api/practice-profile",
                           headers=admin_headers,
                           json=full_profile)
        assert resp.status_code == 200


class TestBetaAdminDemographics:
    """Test GET /api/beta-admin/demographics endpoint"""

    def test_01_demographics_endpoint_returns_distributions(self, admin_headers):
        """GET /api/beta-admin/demographics returns specialty/stage/country distributions"""
        resp = requests.get(f"{BASE_URL}/api/beta-admin/demographics", headers=admin_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        # Check required fields
        assert "total_users" in data, "Should have total_users"
        assert "profiles_completed" in data, "Should have profiles_completed"
        assert "completion_rate_pct" in data, "Should have completion_rate_pct"
        
        # Distribution arrays
        assert "specialty_distribution" in data, "Should have specialty_distribution"
        assert "stage_distribution" in data, "Should have stage_distribution"
        assert "country_distribution" in data, "Should have country_distribution"
        assert "top_subspecialties" in data, "Should have top_subspecialties"
        assert "years_distribution" in data, "Should have years_distribution"
        assert "setting_distribution" in data, "Should have setting_distribution"
        assert "environment_distribution" in data, "Should have environment_distribution"
        
        # Verify distributions are arrays
        assert isinstance(data["specialty_distribution"], list)
        assert isinstance(data["stage_distribution"], list)
        assert isinstance(data["country_distribution"], list)
        
        print(f"Demographics: {data['total_users']} users, {data['profiles_completed']} profiles, {data['completion_rate_pct']}% completion")

    def test_02_demographics_distribution_item_format(self, admin_headers):
        """Distribution items have value and count fields"""
        resp = requests.get(f"{BASE_URL}/api/beta-admin/demographics", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        
        # Check specialty distribution format
        spec_dist = data.get("specialty_distribution", [])
        if len(spec_dist) > 0:
            item = spec_dist[0]
            assert "value" in item, "Distribution item should have 'value'"
            assert "count" in item, "Distribution item should have 'count'"
            assert isinstance(item["count"], int), "count should be integer"
            print(f"Top specialty: {item['value']} ({item['count']} users)")

    def test_03_demographics_engagement_by_specialty(self, admin_headers):
        """Demographics includes engagement_by_specialty"""
        resp = requests.get(f"{BASE_URL}/api/beta-admin/demographics", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        
        assert "engagement_by_specialty" in data, "Should have engagement_by_specialty"
        engagement = data["engagement_by_specialty"]
        assert isinstance(engagement, list), "engagement_by_specialty should be list"
        
        if len(engagement) > 0:
            item = engagement[0]
            assert "specialty" in item, "Should have specialty"
            assert "users" in item, "Should have users count"
            assert "total_events" in item, "Should have total_events"


class TestBetaAdminUserDrilldown:
    """Test GET /api/beta-admin/user/{id} includes practice_profile and personalization_signals"""

    def test_01_get_user_detail_includes_practice_profile(self, admin_headers, admin_token):
        """User drill-down includes practice_profile"""
        # Get admin user_id from token decode or auth/me
        me_resp = requests.get(f"{BASE_URL}/api/auth/me", headers=admin_headers)
        assert me_resp.status_code == 200
        admin_user_id = me_resp.json()["user_id"]
        
        resp = requests.get(f"{BASE_URL}/api/beta-admin/user/{admin_user_id}", headers=admin_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        
        # Check practice profile fields
        assert "practice_profile" in data, "Should include practice_profile"
        assert "practice_profile_complete" in data, "Should include practice_profile_complete"
        assert "practice_field_count" in data, "Should include practice_field_count"
        
        print(f"User drill-down: practice_profile_complete={data['practice_profile_complete']}, field_count={data['practice_field_count']}")

    def test_02_user_detail_includes_personalization_signals(self, admin_headers):
        """User drill-down includes personalization_signals"""
        me_resp = requests.get(f"{BASE_URL}/api/auth/me", headers=admin_headers)
        admin_user_id = me_resp.json()["user_id"]
        
        resp = requests.get(f"{BASE_URL}/api/beta-admin/user/{admin_user_id}", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        
        assert "personalization_signals" in data, "Should include personalization_signals"
        signals = data["personalization_signals"]
        assert "active" in signals, "personalization_signals should have 'active' boolean"
        assert "signals" in signals, "personalization_signals should have 'signals' list"
        
        if signals["active"]:
            assert len(signals["signals"]) > 0, "If active, should have at least one signal"
            signal = signals["signals"][0]
            assert "field" in signal, "Signal should have 'field'"
            assert "weight" in signal, "Signal should have 'weight' (moderate/secondary/light)"
            assert "value" in signal, "Signal should have 'value'"
            print(f"Active personalization signals: {[s['field'] for s in signals['signals']]}")


class TestDigestGenerationWithProfile:
    """Test digest generation works with and without practice profile"""

    def test_01_digest_run_now_with_profile(self, admin_headers):
        """POST /api/digests/run-now works for user WITH practice profile"""
        # Demo user has practice profile
        resp = requests.post(f"{BASE_URL}/api/digests/run-now", 
                            headers=admin_headers,
                            json={"send_email": False})
        # Could be 200 (success) or 404 (no preferences) or 429 (quota)
        # We just verify it doesn't crash with 500
        assert resp.status_code in [200, 404, 429], f"Expected 200/404/429, got {resp.status_code}: {resp.text}"
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"Digest generated: {data.get('digest_id')}, articles: {data.get('article_count')}")
        elif resp.status_code == 404:
            print("No preferences set - expected for user without digest profile")
        elif resp.status_code == 429:
            print("Quota exceeded - expected in rate-limited scenario")

    def test_02_verify_digest_endpoint_no_500_errors(self, admin_headers):
        """Digest endpoints don't return 500 errors"""
        # Get digests list
        resp = requests.get(f"{BASE_URL}/api/digests/me?limit=5", headers=admin_headers)
        assert resp.status_code in [200, 404], f"Expected 200/404, got {resp.status_code}: {resp.text}"


class TestPracticeProfileEdgeCases:
    """Test edge cases for practice profile"""

    def test_01_put_profile_with_empty_arrays_clears(self, admin_headers):
        """Empty arrays in subspecialties should be cleaned"""
        resp = requests.put(f"{BASE_URL}/api/practice-profile",
                           headers=admin_headers,
                           json={"practice_profile": {
                               "primary_specialty": "Test",
                               "subspecialties": ["", " ", "   "]
                           }})
        assert resp.status_code == 200
        data = resp.json()
        profile = data.get("practice_profile")
        # Empty strings should be filtered out
        if profile and "subspecialties" in profile:
            for sub in profile["subspecialties"]:
                assert sub.strip(), "Empty subspecialties should be filtered"

    def test_02_put_profile_strips_whitespace(self, admin_headers):
        """Whitespace-only values should be stripped"""
        resp = requests.put(f"{BASE_URL}/api/practice-profile",
                           headers=admin_headers,
                           json={"practice_profile": {
                               "primary_specialty": "  Cardiology  ",
                               "city": "   "
                           }})
        assert resp.status_code == 200
        data = resp.json()
        profile = data.get("practice_profile")
        # Whitespace-only city should be removed
        assert "city" not in profile or profile.get("city", "").strip(), "Whitespace-only fields should be removed"

    def test_03_final_restore_profile(self, admin_headers):
        """Final cleanup: restore demo user profile"""
        full_profile = {
            "practice_profile": {
                "primary_specialty": "Internal Medicine",
                "specialty_2": "Cardiology",
                "subspecialties": ["Heart Failure", "Interventional"],
                "current_stage": "attending",
                "years_in_practice": "6_10",
                "country": "United States",
                "state_province": "California",
                "practice_setting": "academic",
                "clinical_environment": "mixed"
            }
        }
        resp = requests.put(f"{BASE_URL}/api/practice-profile",
                           headers=admin_headers,
                           json=full_profile)
        assert resp.status_code == 200
        print("Demo user profile restored successfully")
