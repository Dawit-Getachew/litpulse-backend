"""
Test Step 11: Go-Live Readiness Dashboard
- GET /api/admin/go-live-status (admin) -> 200 with environment, integrations, operations
- GET /api/admin/go-live-status (non-admin) -> 403
- POST /api/admin/go-live-status/run-live-checks (admin) -> 200 with per-check status
- POST /api/admin/go-live-status/run-live-checks (non-admin) -> 403
- go-live-status does NOT return any secret values (no API keys, only booleans)
- run-live-checks returns stripe/s3/openai_tts/sendgrid/scheduler with ok/failed/skipped status
- Regression: auth/me, billing/me, audio, library export
"""
import pytest
import requests
import os
import re

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "demo@litpulse.com"
ADMIN_PASSWORD = "DemoPass123!"
NON_ADMIN_EMAIL = "test@litpulse.com"
NON_ADMIN_PASSWORD = "TestPass123!"


@pytest.fixture(scope="module")
def admin_token():
    """Get admin token for testing"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD
    })
    if response.status_code != 200:
        pytest.skip(f"Admin login failed: {response.status_code}")
    return response.json().get("access_token")


@pytest.fixture(scope="module")
def non_admin_token():
    """Get non-admin token for testing"""
    response = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": NON_ADMIN_EMAIL,
        "password": NON_ADMIN_PASSWORD
    })
    if response.status_code != 200:
        pytest.skip(f"Non-admin login failed: {response.status_code}")
    return response.json().get("access_token")


class TestGoLiveStatusEndpoint:
    """Test GET /api/admin/go-live-status"""
    
    def test_go_live_status_admin_success(self, admin_token):
        """Admin can access go-live-status and receives structured response"""
        response = requests.get(
            f"{BASE_URL}/api/admin/go-live-status",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        
        # Verify top-level keys
        assert "environment" in data, "Missing 'environment' key"
        assert "integrations" in data, "Missing 'integrations' key"
        assert "operations" in data, "Missing 'operations' key"
        
        # Verify environment structure
        env = data["environment"]
        assert "name" in env, "Missing environment name"
        assert "config_validation_ok" in env, "Missing config_validation_ok"
        assert "cors_is_wildcard" in env, "Missing cors_is_wildcard"
        assert "app_base_url_set" in env, "Missing app_base_url_set"
        assert "jwt_secret_set" in env, "Missing jwt_secret_set"
        
        # Verify integrations structure
        integ = data["integrations"]
        assert "sendgrid" in integ, "Missing sendgrid integration"
        assert "stripe" in integ, "Missing stripe integration"
        assert "audio" in integ, "Missing audio integration"
        
        # Verify sendgrid structure
        assert "configured" in integ["sendgrid"], "Missing sendgrid.configured"
        
        # Verify stripe structure
        stripe = integ["stripe"]
        assert "billing_enabled" in stripe, "Missing stripe.billing_enabled"
        assert "secret_key_configured" in stripe, "Missing stripe.secret_key_configured"
        assert "webhook_secret_configured" in stripe, "Missing stripe.webhook_secret_configured"
        assert "price_id_configured" in stripe, "Missing stripe.price_id_configured"
        
        # Verify audio structure
        audio = integ["audio"]
        assert "enabled_flag" in audio, "Missing audio.enabled_flag"
        assert "tts_provider" in audio, "Missing audio.tts_provider"
        assert "openai_key_configured" in audio, "Missing audio.openai_key_configured"
        assert "storage_backend" in audio, "Missing audio.storage_backend"
        
        # Verify operations structure
        ops = data["operations"]
        assert "scheduler" in ops, "Missing scheduler operations"
        assert "audio" in ops, "Missing audio operations"
        assert "billing" in ops, "Missing billing operations"
        
        print(f"Go-live status response validated: environment={env['name']}, "
              f"billing_enabled={stripe['billing_enabled']}, audio_enabled={audio['enabled_flag']}")
    
    def test_go_live_status_non_admin_forbidden(self, non_admin_token):
        """Non-admin user receives 403 Forbidden"""
        response = requests.get(
            f"{BASE_URL}/api/admin/go-live-status",
            headers={"Authorization": f"Bearer {non_admin_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("Non-admin correctly denied access to go-live-status")
    
    def test_go_live_status_no_secrets_exposed(self, admin_token):
        """Verify no secret values are exposed in the response"""
        response = requests.get(
            f"{BASE_URL}/api/admin/go-live-status",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        
        data = response.json()
        response_str = str(data)
        
        # Check that no API keys or secrets are in the response
        # API keys patterns to check
        api_key_patterns = [
            r'sk_test_',
            r'sk_live_',
            r'SG\.',
            r'sk-emergent-',
            r'AKIA[A-Z0-9]{16}',  # AWS access key pattern
        ]
        
        for pattern in api_key_patterns:
            matches = re.findall(pattern, response_str)
            assert not matches, f"Found potential secret pattern '{pattern}' in response: {matches}"
        
        # Verify only booleans for sensitive fields
        integ = data["integrations"]
        assert isinstance(integ["sendgrid"]["configured"], bool), "sendgrid.configured should be boolean"
        assert isinstance(integ["stripe"]["secret_key_configured"], bool), "stripe.secret_key_configured should be boolean"
        assert isinstance(integ["stripe"]["webhook_secret_configured"], bool), "stripe.webhook_secret_configured should be boolean"
        assert isinstance(integ["audio"]["openai_key_configured"], bool), "audio.openai_key_configured should be boolean"
        
        print("Verified: no secrets exposed in go-live-status response")


class TestRunLiveChecksEndpoint:
    """Test POST /api/admin/go-live-status/run-live-checks"""
    
    def test_run_live_checks_admin_success(self, admin_token):
        """Admin can run live checks and receives per-check status"""
        response = requests.post(
            f"{BASE_URL}/api/admin/go-live-status/run-live-checks",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        data = response.json()
        
        # Verify structure
        assert "checks" in data, "Missing 'checks' key"
        assert "timestamp" in data, "Missing 'timestamp' key"
        
        checks = data["checks"]
        
        # Verify expected check keys
        expected_checks = ["stripe", "s3", "openai_tts", "sendgrid", "scheduler"]
        for check_name in expected_checks:
            assert check_name in checks, f"Missing check: {check_name}"
            check = checks[check_name]
            assert "status" in check, f"Missing status for {check_name}"
            assert check["status"] in ["ok", "failed", "skipped"], f"Invalid status for {check_name}: {check['status']}"
        
        # Report results
        for name, result in checks.items():
            print(f"  {name}: {result['status']}" + (f" - {result.get('message', '')}" if result.get('message') else ""))
        
        print(f"Live checks completed at {data['timestamp']}")
    
    def test_run_live_checks_non_admin_forbidden(self, non_admin_token):
        """Non-admin user receives 403 Forbidden"""
        response = requests.post(
            f"{BASE_URL}/api/admin/go-live-status/run-live-checks",
            headers={"Authorization": f"Bearer {non_admin_token}"}
        )
        assert response.status_code == 403, f"Expected 403, got {response.status_code}: {response.text}"
        print("Non-admin correctly denied access to run-live-checks")
    
    def test_run_live_checks_expected_statuses(self, admin_token):
        """Verify expected statuses based on current config"""
        response = requests.post(
            f"{BASE_URL}/api/admin/go-live-status/run-live-checks",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200
        
        checks = response.json()["checks"]
        
        # S3 should be skipped when AUDIO_STORAGE_BACKEND=local
        s3_check = checks.get("s3", {})
        if s3_check.get("status") == "skipped":
            assert "local" in s3_check.get("message", "").lower(), "S3 skip should mention local storage"
            print("S3 correctly skipped (local storage mode)")
        
        # OpenAI TTS should be ok if key is configured
        openai_check = checks.get("openai_tts", {})
        if openai_check.get("status") == "ok":
            assert "key present" in openai_check.get("message", "").lower() or "api key" in openai_check.get("message", "").lower(), \
                "OpenAI OK should mention key present"
            print("OpenAI TTS check: OK")
        elif openai_check.get("status") == "skipped":
            print("OpenAI TTS check: Skipped (provider not openai)")
        
        # SendGrid should be ok if key is configured
        sendgrid_check = checks.get("sendgrid", {})
        if sendgrid_check.get("status") == "ok":
            print("SendGrid check: OK")
        
        # Scheduler should be ok if running
        scheduler_check = checks.get("scheduler", {})
        print(f"Scheduler check: {scheduler_check.get('status')}")
        
        # Stripe may fail with test key - that's expected
        stripe_check = checks.get("stripe", {})
        print(f"Stripe check: {stripe_check.get('status')}" + 
              (f" - {stripe_check.get('message', '')}" if stripe_check.get('message') else ""))


class TestRegressionEndpoints:
    """Regression tests for existing endpoints"""
    
    def test_auth_me(self, admin_token):
        """GET /api/auth/me returns user with capabilities"""
        response = requests.get(
            f"{BASE_URL}/api/auth/me",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        
        data = response.json()
        assert "user_id" in data, "Missing user_id"
        assert "email" in data, "Missing email"
        assert "capabilities" in data or "plan_tier" in data, "Missing capabilities/plan_tier"
        print(f"auth/me: OK - user={data['email']}")
    
    def test_billing_me(self, admin_token):
        """GET /api/billing/me returns billing status"""
        response = requests.get(
            f"{BASE_URL}/api/billing/me",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        
        data = response.json()
        assert "billing_enabled" in data or "plan_tier" in data, "Missing billing fields"
        print(f"billing/me: OK")
    
    def test_audio_summary_endpoint(self, admin_token):
        """GET /api/articles/{pmid}/audio-summary endpoint exists"""
        # Use a test PMID
        pmid = "12345678"
        response = requests.get(
            f"{BASE_URL}/api/articles/{pmid}/audio-summary",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        # 200 or 404 are both acceptable (depends on whether article exists)
        assert response.status_code in [200, 404], f"Expected 200 or 404, got {response.status_code}"
        print(f"audio-summary endpoint: OK (status={response.status_code})")
    
    def test_library_export_endpoint(self, admin_token):
        """GET /api/library/export endpoint works"""
        response = requests.get(
            f"{BASE_URL}/api/library/export?format=csv",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        # 200 or 403 (if premium check fails)
        assert response.status_code in [200, 403], f"Expected 200 or 403, got {response.status_code}"
        print(f"library/export: OK (status={response.status_code})")


class TestUnauthorizedAccess:
    """Test endpoints without auth"""
    
    def test_go_live_status_no_auth(self):
        """Go-live-status requires auth"""
        response = requests.get(f"{BASE_URL}/api/admin/go-live-status")
        assert response.status_code in [401, 403], f"Expected 401/403, got {response.status_code}"
        print("go-live-status correctly requires auth")
    
    def test_run_live_checks_no_auth(self):
        """Run-live-checks requires auth"""
        response = requests.post(f"{BASE_URL}/api/admin/go-live-status/run-live-checks")
        assert response.status_code in [401, 403], f"Expected 401/403, got {response.status_code}"
        print("run-live-checks correctly requires auth")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
