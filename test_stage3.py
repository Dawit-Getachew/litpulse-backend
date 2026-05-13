#!/usr/bin/env python3
"""
Stage 3 Test Script - Preferences, Specialties & PubMed
Tests specialty config, preferences management, and PubMed search
"""

import requests
import sys
import uuid
import time

BACKEND_URL = "http://localhost:8001"

def setup_test_user():
    """Create and login a test user"""
    email = f"stage3_{uuid.uuid4().hex[:8]}@scienthesis.ai"
    
    # Signup
    requests.post(
        f"{BACKEND_URL}/api/auth/signup",
        json={
            "email": email,
            "password": "TestPass123!",
            "full_name": "Stage 3 Test"
        }
    )
    
    # Login
    response = requests.post(
        f"{BACKEND_URL}/api/auth/login",
        json={"email": email, "password": "TestPass123!"}
    )
    
    token = response.json()["access_token"]
    return email, token

def test_specialty_config():
    """Test GET /api/config/specialties"""
    print("\n=== Testing GET /api/config/specialties ===")
    
    response = requests.get(f"{BACKEND_URL}/api/config/specialties")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    
    data = response.json()
    assert "specialties" in data
    assert len(data["specialties"]) >= 10, f"Expected at least 10 specialties"
    
    # Check structure
    spec = data["specialties"][0]
    assert "id" in spec
    assert "label" in spec
    assert "subspecialties" in spec
    
    if spec["subspecialties"]:
        subspec = spec["subspecialties"][0]
        assert "id" in subspec
        assert "label" in subspec
        assert "topics" in subspec
        assert "journals" in subspec
    
    print(f"  ✓ {len(data['specialties'])} specialties loaded")
    print(f"  ✓ Configuration structure valid")

def test_create_preferences(token):
    """Test POST /api/preferences"""
    print("\n=== Testing POST /api/preferences ===")
    
    response = requests.post(
        f"{BACKEND_URL}/api/preferences",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "specialty_id": "internal_medicine",
            "subspecialty_id": "im_cardiology",
            "topics_selected": ["Acute myocardial infarction", "Heart failure"],
            "custom_topics": ["STEMI management"],
            "journals_selected": ["Circulation", "JAMA Cardiology"],
            "custom_journals": [],
            "max_articles_per_digest": 12,
            "schedule": {
                "frequency": "daily",
                "time_local": "08:00",
                "timezone": "America/New_York"
            }
        }
    )
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    
    assert data["specialty_id"] == "internal_medicine"
    assert data["subspecialty_id"] == "im_cardiology"
    assert len(data["topics_selected"]) == 2
    assert len(data["custom_topics"]) == 1
    assert data["max_articles_per_digest"] == 12
    assert data["schedule"]["frequency"] == "daily"
    assert data["next_run_timestamp"] is not None
    assert data["last_run_timestamp"] is None
    assert data["is_active"] == True
    
    print("  ✓ Preferences created successfully")
    print(f"  ✓ Next run: {data['next_run_timestamp']}")

def test_get_preferences(token):
    """Test GET /api/preferences/me"""
    print("\n=== Testing GET /api/preferences/me ===")
    
    response = requests.get(
        f"{BACKEND_URL}/api/preferences/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    
    assert data["specialty_id"] == "internal_medicine"
    assert data["subspecialty_id"] == "im_cardiology"
    
    print("  ✓ Preferences retrieved successfully")

def test_update_preferences(token):
    """Test updating existing preferences"""
    print("\n=== Testing preference updates ===")
    
    response = requests.post(
        f"{BACKEND_URL}/api/preferences",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "specialty_id": "internal_medicine",
            "subspecialty_id": "im_nephrology",
            "topics_selected": ["Chronic kidney disease"],
            "custom_topics": [],
            "journals_selected": ["Kidney International"],
            "custom_journals": [],
            "max_articles_per_digest": 10,
            "schedule": {
                "frequency": "weekly",
                "time_local": "09:00",
                "timezone": "UTC",
                "day_of_week": "Mon"
            }
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["subspecialty_id"] == "im_nephrology"
    assert data["schedule"]["frequency"] == "weekly"
    
    print("  ✓ Preferences updated successfully")

def test_preferences_not_found():
    """Test 404 when preferences don't exist"""
    print("\n=== Testing preferences not found ===")
    
    # Create new user
    email, token = setup_test_user()
    
    response = requests.get(
        f"{BACKEND_URL}/api/preferences/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    
    assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    data = response.json()
    assert "not found" in data["detail"].lower()
    
    print("  ✓ 404 returned for missing preferences")

def test_pubmed_search(token):
    """Test POST /api/articles/test-search"""
    print("\n=== Testing PubMed search ===")
    
    # First create preferences
    requests.post(
        f"{BACKEND_URL}/api/preferences",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "specialty_id": "internal_medicine",
            "subspecialty_id": "im_infectious",
            "topics_selected": ["COVID-19", "Sepsis"],
            "custom_topics": [],
            "journals_selected": ["Clinical Infectious Diseases"],
            "custom_journals": [],
            "max_articles_per_digest": 10,
            "schedule": {
                "frequency": "daily",
                "time_local": "09:00",
                "timezone": "UTC"
            }
        }
    )
    
    # Run search
    print("  Querying PubMed (this may take a few seconds)...")
    response = requests.post(
        f"{BACKEND_URL}/api/articles/test-search",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "days_back": 30,
            "max_results": 5
        },
        timeout=30
    )
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    
    assert "query_plan" in data
    assert "search_window" in data
    assert "articles_found" in data
    assert "articles" in data
    
    print(f"  ✓ Query planned: {data['query_plan']['topics_count']} topics")
    print(f"  ✓ Search window: {data['search_window']['days_back']} days")
    print(f"  ✓ Articles found: {data['articles_found']}")
    
    if data["articles"]:
        article = data["articles"][0]
        assert "pmid" in article
        assert "title" in article
        assert "journal" in article
        assert "url" in article
        print(f"  ✓ Article structure valid (PMID: {article['pmid']})")

def test_pubmed_without_preferences():
    """Test search without preferences"""
    print("\n=== Testing search without preferences ===")
    
    # Create new user without preferences
    email, token = setup_test_user()
    
    response = requests.post(
        f"{BACKEND_URL}/api/articles/test-search",
        headers={"Authorization": f"Bearer {token}"},
        json={"days_back": 7, "max_results": 5}
    )
    
    assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    print("  ✓ 404 returned when preferences missing")

def main():
    print("=" * 70)
    print("STAGE 3 TEST SUITE - Preferences, Specialties & PubMed")
    print("=" * 70)
    
    try:
        # Test specialty configuration
        test_specialty_config()
        
        # Setup test user
        email, token = setup_test_user()
        print(f"\n✓ Test user created: {email}")
        
        # Test preferences endpoints
        test_create_preferences(token)
        test_get_preferences(token)
        test_update_preferences(token)
        test_preferences_not_found()
        
        # Test PubMed search
        email2, token2 = setup_test_user()
        test_pubmed_search(token2)
        test_pubmed_without_preferences()
        
        print("\n" + "=" * 70)
        print("✓ ALL STAGE 3 TESTS PASSED")
        print("=" * 70)
        return 0
        
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
