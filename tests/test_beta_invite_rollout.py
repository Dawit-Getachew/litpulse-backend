"""
Beta Invite-Only Rollout Tests

Tests for LitPulse single-specialty invite-only beta:
1. Signup WITHOUT invite code returns 403 with error_code=invite_required
2. Admin can create invite codes via POST /api/beta-admin/invite
3. Signup WITH valid invite code succeeds and user gets beta_status=active_beta
4. Signup with already-used invite code fails
5. Admin can activate/pause/remove beta users
6. GET /api/beta-admin/dashboard returns overview metrics
7. GET /api/beta-admin/funnel returns activation funnel
8. GET /api/beta-admin/feature-usage returns feature counts
9. GET /api/beta-admin/reliability returns system reliability info
10. GET /api/beta-admin/user/{user_id} returns user drill-down
11. GET /api/beta-admin/ai-health returns all AI provider health status
12. Copilot enabled with real Gemini provider
13. Feature flags endpoint returns correct beta settings
14. Community posting blocked for unverified beta user (403 verification_required)
15. Login tracks analytics event
"""
import pytest
import requests
import os
import uuid
import time

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "demo@litpulse.com"
ADMIN_PASSWORD = "DemoPass123!"


@pytest.fixture(scope="module")
def admin_session():
    """Create a session with admin auth token (shared across all tests in module)"""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    
    # Login as admin once
    response = session.post(f"{BASE_URL}/api/auth/login", json={
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD
    })
    
    if response.status_code == 429:
        # Wait for rate limit to clear
        time.sleep(60)
        response = session.post(f"{BASE_URL}/api/auth/login", json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        })
    
    assert response.status_code == 200, f"Admin login failed: {response.text}"
    token = response.json().get("access_token")
    admin_user_id = response.json()["user"]["user_id"]
    
    session.headers.update({"Authorization": f"Bearer {token}"})
    session.admin_user_id = admin_user_id
    
    return session


@pytest.fixture(scope="module")
def unauth_session():
    """Session without auth for testing unauthenticated endpoints"""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


# =========================================================================
# Test 1: Signup WITHOUT invite code returns 403 with error_code=invite_required
# =========================================================================
def test_01_signup_without_invite_code_blocked(unauth_session):
    """Signup without invite code should return 403 with error_code=invite_required"""
    test_email = f"test_no_invite_{uuid.uuid4().hex[:8]}@example.com"
    
    response = unauth_session.post(f"{BASE_URL}/api/auth/signup", json={
        "email": test_email,
        "password": "TestPass123!",
        "full_name": "Test No Invite"
        # No invite_code
    })
    
    assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
    data = response.json()
    assert "detail" in data
    detail = data["detail"]
    assert detail.get("error_code") == "invite_required", f"Expected error_code=invite_required, got {detail}"
    print("✓ Test 1 PASSED: Signup without invite code correctly returns 403 with invite_required")


# =========================================================================
# Test 2: Admin can create invite codes via POST /api/beta-admin/invite
# =========================================================================
def test_02_admin_create_invite_codes(admin_session):
    """Admin should be able to create invite codes"""
    response = admin_session.post(f"{BASE_URL}/api/beta-admin/invite", json={"count": 2})
    
    assert response.status_code == 200, f"Failed to create invites: {response.text}"
    data = response.json()
    assert "codes" in data
    assert len(data["codes"]) == 2
    assert data["count"] == 2
    
    print(f"✓ Test 2 PASSED: Admin created {len(data['codes'])} invite codes")


# =========================================================================
# Test 3: Signup WITH valid invite code succeeds, user gets beta_status=active_beta
# =========================================================================
def test_03_signup_with_valid_invite_code(admin_session, unauth_session):
    """Signup with valid invite code should succeed and user gets beta_status=active_beta"""
    # First create an invite code
    create_resp = admin_session.post(f"{BASE_URL}/api/beta-admin/invite", json={"count": 1})
    assert create_resp.status_code == 200, f"Failed to create invite: {create_resp.text}"
    invite_code = create_resp.json()["codes"][0]
    
    # Now signup with the invite code
    test_email = f"test_with_invite_{uuid.uuid4().hex[:8]}@example.com"
    signup_resp = unauth_session.post(f"{BASE_URL}/api/auth/signup", json={
        "email": test_email,
        "password": "TestPass123!",
        "full_name": "Test With Invite",
        "invite_code": invite_code
    })
    
    assert signup_resp.status_code == 201, f"Signup with invite failed: {signup_resp.text}"
    user_data = signup_resp.json()
    assert "user_id" in user_data
    
    # Check user detail via admin endpoint
    user_detail_resp = admin_session.get(f"{BASE_URL}/api/beta-admin/user/{user_data['user_id']}")
    assert user_detail_resp.status_code == 200, f"User detail failed: {user_detail_resp.text}"
    user_detail = user_detail_resp.json()
    assert user_detail["user"].get("beta_status") == "active_beta", f"Expected beta_status=active_beta, got {user_detail['user'].get('beta_status')}"
    
    print(f"✓ Test 3 PASSED: Signup with invite code succeeded, user has beta_status=active_beta")


# =========================================================================
# Test 4: Signup with already-used invite code fails
# =========================================================================
def test_04_signup_with_used_invite_code_fails(admin_session, unauth_session):
    """Signup with already-used invite code should fail"""
    # Create a code and use it
    create_resp = admin_session.post(f"{BASE_URL}/api/beta-admin/invite", json={"count": 1})
    assert create_resp.status_code == 200
    invite_code = create_resp.json()["codes"][0]
    
    # Use the code
    first_email = f"test_first_use_{uuid.uuid4().hex[:8]}@example.com"
    first_signup = unauth_session.post(f"{BASE_URL}/api/auth/signup", json={
        "email": first_email,
        "password": "TestPass123!",
        "full_name": "First Use",
        "invite_code": invite_code
    })
    assert first_signup.status_code == 201, f"First signup failed: {first_signup.text}"
    
    # Try to use the same code again
    second_email = f"test_second_use_{uuid.uuid4().hex[:8]}@example.com"
    second_signup = unauth_session.post(f"{BASE_URL}/api/auth/signup", json={
        "email": second_email,
        "password": "TestPass123!",
        "full_name": "Second Use",
        "invite_code": invite_code
    })
    
    assert second_signup.status_code == 403, f"Expected 403 for used code, got {second_signup.status_code}"
    data = second_signup.json()
    assert data["detail"].get("error_code") == "invalid_invite", f"Expected invalid_invite, got {data['detail']}"
    
    print("✓ Test 4 PASSED: Signup with already-used invite code correctly fails")


# =========================================================================
# Test 5: Admin can activate/pause/remove beta users
# =========================================================================
def test_05_admin_can_manage_beta_users(admin_session, unauth_session):
    """Admin should be able to activate, pause, and remove beta users"""
    # Create a test user first
    create_resp = admin_session.post(f"{BASE_URL}/api/beta-admin/invite", json={"count": 1})
    invite_code = create_resp.json()["codes"][0]
    
    test_email = f"test_manage_{uuid.uuid4().hex[:8]}@example.com"
    signup_resp = unauth_session.post(f"{BASE_URL}/api/auth/signup", json={
        "email": test_email,
        "password": "TestPass123!",
        "full_name": "Test Manage User",
        "invite_code": invite_code
    })
    assert signup_resp.status_code == 201
    user_id = signup_resp.json()["user_id"]
    
    # Test PAUSE
    pause_resp = admin_session.post(
        f"{BASE_URL}/api/beta-admin/pause",
        json={"user_id": user_id, "reason": "Testing pause"}
    )
    assert pause_resp.status_code == 200, f"Pause failed: {pause_resp.text}"
    assert pause_resp.json()["status"] == "paused"
    
    # Test ACTIVATE (re-activate)
    activate_resp = admin_session.post(
        f"{BASE_URL}/api/beta-admin/activate",
        json={"user_id": user_id}
    )
    assert activate_resp.status_code == 200, f"Activate failed: {activate_resp.text}"
    assert activate_resp.json()["status"] == "active_beta"
    
    # Test REMOVE
    remove_resp = admin_session.post(
        f"{BASE_URL}/api/beta-admin/remove",
        json={"user_id": user_id}
    )
    assert remove_resp.status_code == 200, f"Remove failed: {remove_resp.text}"
    assert remove_resp.json()["status"] == "removed"
    
    print("✓ Test 5 PASSED: Admin can activate, pause, and remove beta users")


# =========================================================================
# Test 6: GET /api/beta-admin/dashboard returns overview metrics
# =========================================================================
def test_06_dashboard_returns_metrics(admin_session):
    """Dashboard endpoint should return overview metrics"""
    response = admin_session.get(f"{BASE_URL}/api/beta-admin/dashboard")
    
    assert response.status_code == 200, f"Dashboard failed: {response.text}"
    data = response.json()
    
    # Check required fields
    required_fields = [
        "total_users", "pending_invites", "active_beta", "active_capacity",
        "waitlist", "waitlist_capacity", "paused", "verified_email",
        "verified_clinician", "active_7d", "engaged_14d"
    ]
    for field in required_fields:
        assert field in data, f"Missing field: {field}"
    
    print(f"✓ Test 6 PASSED: Dashboard returns metrics - active_beta={data['active_beta']}, capacity={data['active_capacity']}")


# =========================================================================
# Test 7: GET /api/beta-admin/funnel returns activation funnel
# =========================================================================
def test_07_funnel_returns_metrics(admin_session):
    """Funnel endpoint should return activation funnel metrics"""
    response = admin_session.get(f"{BASE_URL}/api/beta-admin/funnel")
    
    assert response.status_code == 200, f"Funnel failed: {response.text}"
    data = response.json()
    
    required_fields = [
        "signup_completed", "email_verified", "work_email_verified",
        "onboarding_completed", "first_digest_generated"
    ]
    for field in required_fields:
        assert field in data, f"Missing funnel field: {field}"
    
    print(f"✓ Test 7 PASSED: Funnel returns activation metrics")


# =========================================================================
# Test 8: GET /api/beta-admin/feature-usage returns feature counts
# =========================================================================
def test_08_feature_usage_returns_counts(admin_session):
    """Feature usage endpoint should return feature counts"""
    response = admin_session.get(f"{BASE_URL}/api/beta-admin/feature-usage")
    
    assert response.status_code == 200, f"Feature usage failed: {response.text}"
    data = response.json()
    
    required_fields = [
        "digests_generated", "audio_generated", "summary_requests",
        "deepdive_sessions", "community_posts"
    ]
    for field in required_fields:
        assert field in data, f"Missing feature usage field: {field}"
    
    print(f"✓ Test 8 PASSED: Feature usage returns counts")


# =========================================================================
# Test 9: GET /api/beta-admin/reliability returns system reliability info
# =========================================================================
def test_09_reliability_returns_info(admin_session):
    """Reliability endpoint should return system reliability metrics"""
    response = admin_session.get(f"{BASE_URL}/api/beta-admin/reliability")
    
    assert response.status_code == 200, f"Reliability failed: {response.text}"
    data = response.json()
    
    assert "audio_storage_backend" in data
    assert "last_24h" in data
    
    print(f"✓ Test 9 PASSED: Reliability returns system info")


# =========================================================================
# Test 10: GET /api/beta-admin/user/{user_id} returns user drill-down
# =========================================================================
def test_10_user_drilldown_returns_detail(admin_session):
    """User drill-down endpoint should return detailed user info"""
    admin_user_id = admin_session.admin_user_id
    
    response = admin_session.get(f"{BASE_URL}/api/beta-admin/user/{admin_user_id}")
    
    assert response.status_code == 200, f"User drill-down failed: {response.text}"
    data = response.json()
    
    assert "user" in data
    assert "profile_count" in data
    assert "digest_count" in data
    assert "library_count" in data
    
    print(f"✓ Test 10 PASSED: User drill-down returns detailed info")


# =========================================================================
# Test 11: GET /api/beta-admin/ai-health returns all AI provider health status
# =========================================================================
def test_11_ai_health_returns_status(admin_session):
    """AI health endpoint should return provider health status"""
    response = admin_session.get(f"{BASE_URL}/api/beta-admin/ai-health")
    
    assert response.status_code == 200, f"AI health failed: {response.text}"
    data = response.json()
    
    # Check summary provider (gemini-2.5-flash)
    assert "summary" in data
    assert data["summary"]["provider"] == "google"
    assert data["summary"]["model"] == "gemini-2.5-flash"
    
    # Check deepdive provider (gemini-2.5-pro)
    assert "deepdive" in data
    assert data["deepdive"]["provider"] == "google"
    assert data["deepdive"]["model"] == "gemini-2.5-pro"
    
    # Check TTS provider
    assert "tts" in data
    
    print(f"✓ Test 11 PASSED: AI health returns - summary reachable={data['summary']['reachable']}, deepdive reachable={data['deepdive']['reachable']}")


# =========================================================================
# Test 12: Copilot enabled with real Gemini provider
# =========================================================================
def test_12_copilot_enabled_with_gemini(admin_session):
    """Copilot should be enabled with real Gemini provider (COPILOT_PROVIDER=google)"""
    # Test copilot health endpoint to verify configuration
    health_resp = admin_session.get(f"{BASE_URL}/api/copilot/health")
    
    assert health_resp.status_code == 200, f"Copilot health failed: {health_resp.text}"
    health_data = health_resp.json()
    
    # Verify copilot is enabled with google provider
    assert health_data.get("copilot_enabled") == True, f"Copilot should be enabled, got {health_data}"
    assert health_data.get("provider") == "google", f"Copilot provider should be google, got {health_data.get('provider')}"
    
    print(f"✓ Test 12 PASSED: Copilot is enabled with Google Gemini (provider={health_data.get('provider')}, model={health_data.get('model')})")


# =========================================================================
# Test 13: Feature flags endpoint returns correct beta settings
# =========================================================================
def test_13_feature_flags_beta_settings(unauth_session):
    """Feature flags should return correct beta settings"""
    response = unauth_session.get(f"{BASE_URL}/api/config/feature-flags")
    
    assert response.status_code == 200, f"Feature flags failed: {response.text}"
    data = response.json()
    
    # Check beta-related flags
    assert data.get("copilot_enabled") == True, f"copilot_enabled should be true"
    assert data.get("enable_invite_only_beta") == True, f"enable_invite_only_beta should be true"
    assert data.get("allow_npi_self_attestation") == False, f"allow_npi_self_attestation should be false"
    assert data.get("require_verified_for_posting") == True, f"require_verified_for_posting should be true"
    
    print(f"✓ Test 13 PASSED: Feature flags return correct beta settings")


# =========================================================================
# Test 14: Community posting blocked for unverified beta user
# =========================================================================
def test_14_community_posting_blocked_for_unverified(admin_session, unauth_session):
    """Community posting should be blocked for unverified beta user"""
    # Create a new beta user (unverified)
    create_resp = admin_session.post(f"{BASE_URL}/api/beta-admin/invite", json={"count": 1})
    invite_code = create_resp.json()["codes"][0]
    
    test_email = f"test_unverified_{uuid.uuid4().hex[:8]}@example.com"
    signup_resp = unauth_session.post(f"{BASE_URL}/api/auth/signup", json={
        "email": test_email,
        "password": "TestPass123!",
        "full_name": "Unverified User",
        "invite_code": invite_code
    })
    assert signup_resp.status_code == 201
    
    # Login as unverified user
    login_resp = unauth_session.post(f"{BASE_URL}/api/auth/login", json={
        "email": test_email,
        "password": "TestPass123!"
    })
    assert login_resp.status_code == 200
    unverified_token = login_resp.json()["access_token"]
    
    # Try to create a community thread with unverified user token
    # ThreadCreate requires: context_type, context_id, title
    thread_resp = requests.post(
        f"{BASE_URL}/api/discussions/threads",
        json={
            "context_type": "specialty",
            "context_id": "internal_medicine",
            "specialty_id": "internal_medicine",
            "title": "Test thread for unverified user"
        },
        headers={
            "Authorization": f"Bearer {unverified_token}",
            "Content-Type": "application/json"
        }
    )
    
    # Should be blocked with 403
    assert thread_resp.status_code == 403, f"Expected 403, got {thread_resp.status_code}: {thread_resp.text}"
    data = thread_resp.json()
    # Check for verification_required or beta_access_required
    error_code = data.get("detail", {}).get("error_code", "")
    assert error_code in ["verification_required", "beta_access_required"], f"Expected verification_required or beta_access_required, got {error_code}"
    
    print(f"✓ Test 14 PASSED: Community posting blocked for unverified user (error_code={error_code})")


# =========================================================================
# Test 15: Login tracks analytics event (verified via dashboard)
# =========================================================================
def test_15_login_tracks_analytics_event(admin_session):
    """Login should track analytics event (verify via dashboard active_7d count)"""
    # Check dashboard for active_7d count (should include the login we did)
    dashboard_resp = admin_session.get(f"{BASE_URL}/api/beta-admin/dashboard")
    assert dashboard_resp.status_code == 200
    data = dashboard_resp.json()
    
    # The login should have been tracked (active_7d >= 1 because admin logged in)
    assert data.get("active_7d", 0) >= 1, f"Expected active_7d >= 1, got {data.get('active_7d')}"
    
    print(f"✓ Test 15 PASSED: Login analytics tracked (active_7d={data.get('active_7d')})")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
