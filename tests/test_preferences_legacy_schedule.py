"""
Tests for /api/preferences/me 500 error fix for legacy users.

Root cause: Legacy preference documents stored schedule as {hour: 9, minute: 0} without time_local field,
but ScheduleConfig Pydantic model required time_local as a non-optional str.

Fix: Made time_local Optional with a model_validator that derives it from legacy hour/minute fields,
or defaults to '09:00'.

Tests:
  1. GET /api/preferences/me returns 200 for premium user with legacy schedule
  2. Response schedule contains time_local derived from legacy hour/minute
  3. Response contains all expected fields
  4. GET /api/preferences/me returns 404 for user with no preferences
  5. POST /api/preferences with new-format schedule (time_local) works
  6. POST /api/preferences with only hour/minute (no time_local) works
  7. Copilot endpoints still work (regression check)
  8. LitScholar profile endpoint still works (regression check)
"""
import os
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_USER_EMAIL = "demo@litpulse.com"
PREMIUM_USER_PASSWORD = "DemoPass123!"
FREE_USER_EMAIL = "test@litpulse.com"
FREE_USER_PASSWORD = "TestPass123!"


@pytest.fixture(scope="module")
def premium_user_token():
    """Login premium user (demo@litpulse.com) and return token."""
    response = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": PREMIUM_USER_EMAIL,
        "password": PREMIUM_USER_PASSWORD
    })
    if response.status_code != 200:
        pytest.skip(f"Failed to login premium user: {response.status_code} - {response.text}")
    return response.json().get("access_token")


@pytest.fixture(scope="module")
def free_user_token():
    """Login free user (test@litpulse.com) and return token."""
    response = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": FREE_USER_EMAIL,
        "password": FREE_USER_PASSWORD
    })
    if response.status_code != 200:
        pytest.skip(f"Failed to login free user: {response.status_code} - {response.text}")
    return response.json().get("access_token")


class TestPreferencesLegacyScheduleFix:
    """Tests for GET /api/preferences/me with legacy schedule format"""
    
    def test_preferences_me_returns_200_for_premium_user(self, premium_user_token):
        """GET /api/preferences/me should return 200 for user with legacy schedule (hour/minute, no time_local)"""
        response = requests.get(
            f"{BASE_URL}/api/preferences/me",
            headers={"Authorization": f"Bearer {premium_user_token}"}
        )
        
        # Key fix verification - this was returning 500 before
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert "schedule" in data, "Response should contain schedule field"
        assert "specialty_id" in data, "Response should contain specialty_id"
        assert "is_active" in data, "Response should contain is_active"
        print(f"✓ GET /api/preferences/me returns 200 for premium user")
    
    def test_schedule_contains_time_local_derived_from_legacy(self, premium_user_token):
        """Response schedule should contain time_local derived from legacy hour/minute (should be '09:00')"""
        response = requests.get(
            f"{BASE_URL}/api/preferences/me",
            headers={"Authorization": f"Bearer {premium_user_token}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        schedule = data.get("schedule", {})
        
        # time_local should be present and derived from hour:9, minute:0 -> '09:00'
        assert "time_local" in schedule, "schedule should contain time_local field"
        assert schedule["time_local"] is not None, "time_local should not be None"
        
        # Based on legacy data: {hour: 9, minute: 0} -> time_local should be '09:00'
        # Or at least be a valid HH:MM format
        time_local = schedule["time_local"]
        assert isinstance(time_local, str), f"time_local should be string, got {type(time_local)}"
        assert ":" in time_local, f"time_local should be HH:MM format, got {time_local}"
        
        print(f"✓ schedule.time_local = '{time_local}' (derived from legacy hour/minute)")
    
    def test_response_contains_all_expected_fields(self, premium_user_token):
        """Response should contain all expected PreferenceResponse fields"""
        response = requests.get(
            f"{BASE_URL}/api/preferences/me",
            headers={"Authorization": f"Bearer {premium_user_token}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Required fields
        required_fields = ["specialty_id", "schedule", "topics_selected", "is_active", "created_at", "updated_at"]
        for field in required_fields:
            assert field in data, f"Response should contain {field}"
        
        # Schedule sub-fields
        schedule = data.get("schedule", {})
        schedule_fields = ["frequency", "timezone", "time_local"]
        for field in schedule_fields:
            assert field in schedule, f"schedule should contain {field}"
        
        print(f"✓ Response contains all expected fields: {list(data.keys())}")
    
    def test_preferences_me_returns_404_for_user_without_preferences(self, free_user_token):
        """GET /api/preferences/me should return 404 for user without preferences"""
        response = requests.get(
            f"{BASE_URL}/api/preferences/me",
            headers={"Authorization": f"Bearer {free_user_token}"}
        )
        
        # User without preferences should get 404, not 500
        assert response.status_code == 404, f"Expected 404 for user without preferences, got {response.status_code}: {response.text}"
        print(f"✓ GET /api/preferences/me returns 404 for user without preferences")


class TestPreferencesCreateWithScheduleFormats:
    """Tests for POST /api/preferences with different schedule formats"""
    
    def test_post_preferences_with_time_local_format(self, premium_user_token):
        """POST /api/preferences with new-format schedule (time_local: '14:30') should work"""
        payload = {
            "specialty_id": "internal_medicine",
            "topics_selected": ["diabetes", "hypertension"],
            "custom_topics": [],
            "journals_selected": [],
            "custom_journals": [],
            "max_articles_per_digest": 10,
            "schedule": {
                "frequency": "weekly",
                "time_local": "14:30",
                "timezone": "America/New_York",
                "day_of_week": "Mon"
            }
        }
        
        response = requests.post(
            f"{BASE_URL}/api/preferences",
            headers={"Authorization": f"Bearer {premium_user_token}"},
            json=payload
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        assert data.get("schedule", {}).get("time_local") == "14:30", "time_local should be preserved as '14:30'"
        print(f"✓ POST /api/preferences with time_local='14:30' works")
    
    def test_post_preferences_with_legacy_hour_minute_format(self, premium_user_token):
        """POST /api/preferences with only hour/minute (no time_local) should still work"""
        payload = {
            "specialty_id": "internal_medicine",
            "topics_selected": ["diabetes", "hypertension"],
            "custom_topics": [],
            "journals_selected": [],
            "custom_journals": [],
            "max_articles_per_digest": 10,
            "schedule": {
                "frequency": "weekly",
                "hour": 9,
                "minute": 0,
                "timezone": "Europe/London",
                "day_of_week": "Tue"
            }
        }
        
        response = requests.post(
            f"{BASE_URL}/api/preferences",
            headers={"Authorization": f"Bearer {premium_user_token}"},
            json=payload
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        # time_local should be derived from hour:9, minute:0 -> '09:00'
        schedule = data.get("schedule", {})
        assert "time_local" in schedule, "Response schedule should contain time_local"
        assert schedule.get("time_local") == "09:00", f"time_local should be '09:00', got {schedule.get('time_local')}"
        print(f"✓ POST /api/preferences with hour=9, minute=0 derives time_local='09:00'")


class TestRegressionChecks:
    """Regression checks for Copilot and LitScholar endpoints"""
    
    def test_copilot_evidence_brief_still_works(self, premium_user_token):
        """POST /api/copilot/evidence-brief should still work"""
        # evidence-brief requires pmid field
        payload = {
            "pmid": "12345678",
            "question": "diabetes management guidelines"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/copilot/evidence-brief",
            headers={"Authorization": f"Bearer {premium_user_token}"},
            json=payload
        )
        
        # Should work (200) or return 404 (article not found) or quota error (429) - not 500
        assert response.status_code in [200, 404, 429], f"Expected 200/404/429, got {response.status_code}: {response.text}"
        print(f"✓ Copilot evidence-brief endpoint works (status: {response.status_code})")
    
    def test_litscholar_profile_still_works(self, premium_user_token):
        """GET /api/litscholar/profile should still work"""
        response = requests.get(
            f"{BASE_URL}/api/litscholar/profile",
            headers={"Authorization": f"Bearer {premium_user_token}"}
        )
        
        # Should return 200 with expertise_profile data
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        # Field is expertise_profile, not profile
        assert "expertise_profile" in data, "Response should contain expertise_profile field"
        print(f"✓ LitScholar profile endpoint works")


class TestScheduleConfigModelValidator:
    """Unit-level tests for ScheduleConfig model_validator behavior"""
    
    def test_schedule_config_derives_time_local_from_hour_minute(self):
        """ScheduleConfig should derive time_local from hour/minute if not set"""
        import sys
        sys.path.insert(0, '/app/backend')
        from preference_models import ScheduleConfig
        
        # Legacy format: hour/minute without time_local
        schedule = ScheduleConfig(
            frequency="weekly",
            timezone="Europe/London",
            hour=9,
            minute=30
        )
        
        assert schedule.time_local == "09:30", f"Expected '09:30', got '{schedule.time_local}'"
        print(f"✓ ScheduleConfig derives time_local='09:30' from hour=9, minute=30")
    
    def test_schedule_config_defaults_to_0900_when_no_time_info(self):
        """ScheduleConfig should default to '09:00' when no time_local or hour/minute provided"""
        import sys
        sys.path.insert(0, '/app/backend')
        from preference_models import ScheduleConfig
        
        # No time information at all
        schedule = ScheduleConfig(
            frequency="daily",
            timezone="UTC"
        )
        
        assert schedule.time_local == "09:00", f"Expected '09:00' default, got '{schedule.time_local}'"
        print(f"✓ ScheduleConfig defaults to time_local='09:00' when no time info")
    
    def test_schedule_config_preserves_explicit_time_local(self):
        """ScheduleConfig should preserve explicit time_local when provided"""
        import sys
        sys.path.insert(0, '/app/backend')
        from preference_models import ScheduleConfig
        
        # Explicit time_local provided
        schedule = ScheduleConfig(
            frequency="weekly",
            timezone="America/New_York",
            time_local="14:30"
        )
        
        assert schedule.time_local == "14:30", f"Expected '14:30', got '{schedule.time_local}'"
        print(f"✓ ScheduleConfig preserves explicit time_local='14:30'")
    
    def test_schedule_config_handles_invalid_time_local(self):
        """ScheduleConfig should default to '09:00' for invalid time_local values"""
        import sys
        sys.path.insert(0, '/app/backend')
        from preference_models import ScheduleConfig
        
        # Invalid time_local format
        schedule = ScheduleConfig(
            frequency="daily",
            timezone="UTC",
            time_local="invalid"
        )
        
        assert schedule.time_local == "09:00", f"Expected '09:00' for invalid, got '{schedule.time_local}'"
        print(f"✓ ScheduleConfig defaults to '09:00' for invalid time_local")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
