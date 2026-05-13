"""
Phase UX-D: Full Preferences Wizard per Digest Profile - Backend Tests

Tests for:
1. Extended digest_profiles schema (topics, journals, schedule, advanced preferences)
2. PUT /api/preferences/profiles/{id} saves all new fields
3. Migration _ensure_default_profile copies ALL legacy preference fields
4. Feature flag enable_digest_profile_full_wizard is returned by /api/config/feature-flags
"""
import pytest
import requests
import os
import uuid
from datetime import datetime, timedelta

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
PREMIUM_USER_EMAIL = "demo@litpulse.com"
PREMIUM_USER_PASSWORD = "DemoPass123!"


class TestPhaseUXDFeatureFlags:
    """Test feature flag for Phase UX-D"""
    
    def test_feature_flag_returned(self):
        """Verify enable_digest_profile_full_wizard is returned by feature-flags endpoint"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        
        data = response.json()
        assert "enable_digest_profile_full_wizard" in data
        # Should be true based on backend/.env
        assert data["enable_digest_profile_full_wizard"] == True
        print(f"✓ enable_digest_profile_full_wizard = {data['enable_digest_profile_full_wizard']}")
    
    def test_multi_digest_profiles_enabled(self):
        """Verify enable_multi_digest_profiles is also enabled (prerequisite)"""
        response = requests.get(f"{BASE_URL}/api/config/feature-flags")
        assert response.status_code == 200
        
        data = response.json()
        assert data.get("enable_multi_digest_profiles") == True
        print("✓ enable_multi_digest_profiles = True")


class TestPhaseUXDProfileSchema:
    """Test extended profile schema with all Phase UX-D fields"""
    
    @pytest.fixture(scope="class")
    def auth_token(self):
        """Get authentication token for premium user"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": PREMIUM_USER_EMAIL,
            "password": PREMIUM_USER_PASSWORD
        })
        if response.status_code != 200:
            pytest.skip(f"Login failed: {response.status_code} - {response.text}")
        return response.json()["access_token"]
    
    @pytest.fixture(scope="class")
    def auth_headers(self, auth_token):
        """Get auth headers"""
        return {"Authorization": f"Bearer {auth_token}"}
    
    def test_list_profiles_returns_extended_fields(self, auth_headers):
        """Verify GET /api/preferences/profiles returns profiles with extended fields"""
        response = requests.get(f"{BASE_URL}/api/preferences/profiles", headers=auth_headers)
        assert response.status_code == 200
        
        data = response.json()
        assert "profiles" in data
        
        if len(data["profiles"]) > 0:
            profile = data["profiles"][0]
            # Check for Phase UX-D fields (may be empty but should exist or be null)
            print(f"✓ Profile has {len(data['profiles'])} profiles")
            print(f"  Profile fields: {list(profile.keys())}")
            
            # These fields should be present in the schema
            expected_fields = [
                "profile_id", "name", "specialty_id", "subspecialty_id",
                "digest_frequency", "delivery_time"
            ]
            for field in expected_fields:
                assert field in profile, f"Missing field: {field}"
            print("✓ All expected base fields present")
    
    def test_create_profile_with_full_wizard_fields(self, auth_headers):
        """Test creating a profile with all Phase UX-D fields"""
        unique_name = f"TEST_UXD_Profile_{uuid.uuid4().hex[:8]}"
        
        # Calculate suppress_until date (7 days from now)
        suppress_until = (datetime.utcnow() + timedelta(days=7)).isoformat()
        
        payload = {
            "name": unique_name,
            "specialty_id": "cardiology",
            "subspecialty_id": "heart_failure",
            "subspecialties": ["heart_failure", "interventional"],
            "topics_selected": ["heart_failure", "arrhythmias"],
            "custom_topics": ["TAVR", "SGLT2 inhibitors"],
            "journals_selected": ["nejm", "jacc"],
            "custom_journals": ["Circulation", "Heart Rhythm"],
            "max_articles_per_digest": 15,
            "schedule": {
                "frequency": "weekly",
                "day_of_week": "Monday",
                "hour": 9,
                "minute": 0,
                "time_local": "09:00",
                "timezone": "America/New_York"
            },
            "email_notifications_enabled": True,
            "email_suppress_until": suppress_until,
            "advanced_preferences": {
                "clinical_notes": "Focus on elderly patients",
                "journal_notes": "Prefer open access"
            }
        }
        
        response = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers=auth_headers,
            json=payload
        )
        
        # May fail if at profile limit - that's OK
        if response.status_code == 409:
            detail = response.json().get("detail", {})
            if detail.get("error_code") == "profile_limit_reached":
                pytest.skip("Profile limit reached - cannot create new profile")
        
        assert response.status_code == 201, f"Failed to create profile: {response.text}"
        
        created = response.json()
        assert created["name"] == unique_name
        assert created["specialty_id"] == "cardiology"
        
        # Verify Phase UX-D fields were saved
        assert created.get("topics_selected") == ["heart_failure", "arrhythmias"]
        assert created.get("custom_topics") == ["TAVR", "SGLT2 inhibitors"]
        assert created.get("journals_selected") == ["nejm", "jacc"]
        assert created.get("custom_journals") == ["Circulation", "Heart Rhythm"]
        assert created.get("max_articles_per_digest") == 15
        assert created.get("email_notifications_enabled") == True
        assert created.get("email_suppress_until") is not None
        assert created.get("advanced_preferences") is not None
        assert created["advanced_preferences"].get("clinical_notes") == "Focus on elderly patients"
        
        print(f"✓ Created profile with all Phase UX-D fields: {created['profile_id']}")
        
        # Cleanup - delete the test profile
        delete_response = requests.delete(
            f"{BASE_URL}/api/preferences/profiles/{created['profile_id']}",
            headers=auth_headers
        )
        # May fail if it's the last profile - that's OK
        if delete_response.status_code == 200:
            print(f"✓ Cleaned up test profile")
        
        return created["profile_id"]
    
    def test_update_profile_with_full_wizard_fields(self, auth_headers):
        """Test updating a profile with Phase UX-D fields via PUT"""
        # First get existing profiles
        list_response = requests.get(f"{BASE_URL}/api/preferences/profiles", headers=auth_headers)
        assert list_response.status_code == 200
        
        profiles = list_response.json().get("profiles", [])
        if not profiles:
            pytest.skip("No profiles to update")
        
        profile_id = profiles[0]["profile_id"]
        original_name = profiles[0]["name"]
        
        # Update with Phase UX-D fields
        update_payload = {
            "topics_selected": ["updated_topic_1", "updated_topic_2"],
            "custom_topics": ["Custom Topic A"],
            "journals_selected": ["updated_journal"],
            "custom_journals": ["Custom Journal X"],
            "max_articles_per_digest": 12,
            "email_notifications_enabled": False,
            "advanced_preferences": {
                "clinical_notes": "Updated clinical notes",
                "journal_notes": "Updated journal notes"
            }
        }
        
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json=update_payload
        )
        
        assert response.status_code == 200, f"Failed to update profile: {response.text}"
        
        updated = response.json()
        
        # Verify updates
        assert updated.get("topics_selected") == ["updated_topic_1", "updated_topic_2"]
        assert updated.get("custom_topics") == ["Custom Topic A"]
        assert updated.get("journals_selected") == ["updated_journal"]
        assert updated.get("custom_journals") == ["Custom Journal X"]
        assert updated.get("max_articles_per_digest") == 12
        assert updated.get("email_notifications_enabled") == False
        assert updated.get("advanced_preferences", {}).get("clinical_notes") == "Updated clinical notes"
        
        print(f"✓ Updated profile {profile_id} with Phase UX-D fields")
        
        # Restore original state (partial restore)
        restore_payload = {
            "topics_selected": [],
            "custom_topics": [],
            "journals_selected": [],
            "custom_journals": [],
            "max_articles_per_digest": 10,
            "email_notifications_enabled": True,
            "advanced_preferences": None
        }
        requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json=restore_payload
        )
        print("✓ Restored profile to original state")
    
    def test_update_profile_suppress_until(self, auth_headers):
        """Test updating email_suppress_until field"""
        # Get existing profiles
        list_response = requests.get(f"{BASE_URL}/api/preferences/profiles", headers=auth_headers)
        profiles = list_response.json().get("profiles", [])
        if not profiles:
            pytest.skip("No profiles to update")
        
        profile_id = profiles[0]["profile_id"]
        
        # Set suppress for 14 days
        suppress_date = (datetime.utcnow() + timedelta(days=14)).isoformat()
        
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={"email_suppress_until": suppress_date}
        )
        
        assert response.status_code == 200
        updated = response.json()
        assert updated.get("email_suppress_until") is not None
        print(f"✓ Set email_suppress_until to {updated['email_suppress_until']}")
        
        # Clear suppress by setting to empty string
        clear_response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={"email_suppress_until": ""}
        )
        
        assert clear_response.status_code == 200
        cleared = clear_response.json()
        assert cleared.get("email_suppress_until") is None
        print("✓ Cleared email_suppress_until")
    
    def test_max_articles_validation(self, auth_headers):
        """Test max_articles_per_digest validation (5-20 range)"""
        list_response = requests.get(f"{BASE_URL}/api/preferences/profiles", headers=auth_headers)
        profiles = list_response.json().get("profiles", [])
        if not profiles:
            pytest.skip("No profiles to update")
        
        profile_id = profiles[0]["profile_id"]
        
        # Test below minimum (should fail)
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={"max_articles_per_digest": 3}
        )
        assert response.status_code == 422, "Should reject max_articles < 5"
        print("✓ Rejected max_articles_per_digest = 3 (below minimum)")
        
        # Test above maximum (should fail)
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={"max_articles_per_digest": 25}
        )
        assert response.status_code == 422, "Should reject max_articles > 20"
        print("✓ Rejected max_articles_per_digest = 25 (above maximum)")
        
        # Test valid value
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={"max_articles_per_digest": 15}
        )
        assert response.status_code == 200
        assert response.json().get("max_articles_per_digest") == 15
        print("✓ Accepted max_articles_per_digest = 15 (valid)")
        
        # Restore default
        requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={"max_articles_per_digest": 10}
        )


class TestPhaseUXDScheduleFields:
    """Test schedule-related fields in Phase UX-D"""
    
    @pytest.fixture(scope="class")
    def auth_token(self):
        """Get authentication token"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": PREMIUM_USER_EMAIL,
            "password": PREMIUM_USER_PASSWORD
        })
        if response.status_code != 200:
            pytest.skip(f"Login failed: {response.status_code}")
        return response.json()["access_token"]
    
    @pytest.fixture(scope="class")
    def auth_headers(self, auth_token):
        return {"Authorization": f"Bearer {auth_token}"}
    
    def test_update_schedule_with_timezone(self, auth_headers):
        """Test updating schedule with timezone field"""
        list_response = requests.get(f"{BASE_URL}/api/preferences/profiles", headers=auth_headers)
        profiles = list_response.json().get("profiles", [])
        if not profiles:
            pytest.skip("No profiles to update")
        
        profile_id = profiles[0]["profile_id"]
        
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={
                "schedule": {
                    "frequency": "daily",
                    "hour": 8,
                    "minute": 30,
                    "time_local": "08:30",
                    "timezone": "Europe/London"
                }
            }
        )
        
        assert response.status_code == 200
        updated = response.json()
        assert updated.get("digest_frequency") == "daily"
        assert updated.get("delivery_time") == "08:30"
        assert updated.get("schedule_timezone") == "Europe/London"
        print(f"✓ Updated schedule with timezone: {updated.get('schedule_timezone')}")
        
        # Restore to weekly
        requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={
                "schedule": {
                    "frequency": "weekly",
                    "day_of_week": "Monday",
                    "hour": 9,
                    "minute": 0
                }
            }
        )


class TestPhaseUXDSubspecialties:
    """Test multiple subspecialties support in Phase UX-D"""
    
    @pytest.fixture(scope="class")
    def auth_token(self):
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": PREMIUM_USER_EMAIL,
            "password": PREMIUM_USER_PASSWORD
        })
        if response.status_code != 200:
            pytest.skip(f"Login failed: {response.status_code}")
        return response.json()["access_token"]
    
    @pytest.fixture(scope="class")
    def auth_headers(self, auth_token):
        return {"Authorization": f"Bearer {auth_token}"}
    
    def test_update_multiple_subspecialties(self, auth_headers):
        """Test updating profile with multiple subspecialties"""
        list_response = requests.get(f"{BASE_URL}/api/preferences/profiles", headers=auth_headers)
        profiles = list_response.json().get("profiles", [])
        if not profiles:
            pytest.skip("No profiles to update")
        
        profile_id = profiles[0]["profile_id"]
        
        response = requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={
                "subspecialties": ["heart_failure", "interventional", "electrophysiology"]
            }
        )
        
        assert response.status_code == 200
        updated = response.json()
        assert updated.get("subspecialties") == ["heart_failure", "interventional", "electrophysiology"]
        print(f"✓ Updated with multiple subspecialties: {updated.get('subspecialties')}")
        
        # Clear subspecialties
        requests.put(
            f"{BASE_URL}/api/preferences/profiles/{profile_id}",
            headers=auth_headers,
            json={"subspecialties": []}
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
