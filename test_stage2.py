#!/usr/bin/env python3
"""
Stage 2 Test Script - Authentication & Email Flows
Tests all auth endpoints and email service integration
"""

import requests
import sys
import uuid

BACKEND_URL = "http://localhost:8001"

def test_signup():
    """Test user signup"""
    print("\n=== Testing POST /api/auth/signup ===")
    
    # Generate unique email
    test_email = f"testuser_{uuid.uuid4().hex[:8]}@scienthesis.ai"
    
    response = requests.post(
        f"{BACKEND_URL}/api/auth/signup",
        json={
            "email": test_email,
            "password": "TestPass123!",
            "full_name": "Test User",
            "timezone": "America/New_York"
        }
    )
    
    assert response.status_code == 201, f"Expected 201, got {response.status_code}"
    data = response.json()
    assert data["email"] == test_email.lower()
    assert data["is_verified"] == False
    assert data["is_active"] == True
    print("✓ Signup successful")
    return test_email, data["user_id"]

def test_duplicate_signup(email):
    """Test duplicate email registration"""
    print("\n=== Testing duplicate email registration ===")
    
    response = requests.post(
        f"{BACKEND_URL}/api/auth/signup",
        json={
            "email": email,
            "password": "TestPass123!",
            "full_name": "Duplicate User"
        }
    )
    
    assert response.status_code == 400, f"Expected 400, got {response.status_code}"
    data = response.json()
    assert "already registered" in data["detail"].lower()
    print("✓ Duplicate email properly rejected")

def test_password_validation():
    """Test password strength validation"""
    print("\n=== Testing password validation ===")
    
    test_cases = [
        ("Short1!", "at least 8 characters"),
        ("nouppercasepass123!", "uppercase letter"),
        ("NOLOWERCASE123!", "lowercase letter"),
        ("NoDigits!", "digit"),
        ("NoSpecial123", "special character")
    ]
    
    for password, expected_error in test_cases:
        response = requests.post(
            f"{BACKEND_URL}/api/auth/signup",
            json={
                "email": f"test_{uuid.uuid4().hex[:8]}@test.com",
                "password": password,
                "full_name": "Test"
            }
        )
        assert response.status_code == 422, f"Expected 422 for weak password, got {response.status_code}"
    
    print("✓ Password validation working")

def test_login(email):
    """Test user login"""
    print("\n=== Testing POST /api/auth/login ===")
    
    response = requests.post(
        f"{BACKEND_URL}/api/auth/login",
        json={
            "email": email,
            "password": "TestPass123!"
        }
    )
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert "user" in data
    print("✓ Login successful")
    return data["access_token"]

def test_get_me(token):
    """Test get current user"""
    print("\n=== Testing GET /api/auth/me ===")
    
    response = requests.get(
        f"{BACKEND_URL}/api/auth/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert "user_id" in data
    assert "email" in data
    print("✓ Get current user successful")

def test_verify_email(user_id):
    """Test email verification"""
    print("\n=== Testing POST /api/auth/verify-email ===")
    
    # Generate verification token
    import sys
    sys.path.append('/app/backend')
    from auth_utils import create_verification_token
    
    token = create_verification_token(user_id)
    
    response = requests.post(
        f"{BACKEND_URL}/api/auth/verify-email",
        json={"token": token}
    )
    
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert "verified successfully" in data["message"].lower()
    print("✓ Email verification successful")

def test_password_reset(user_id, email):
    """Test password reset flow"""
    print("\n=== Testing password reset flow ===")
    
    # Request password reset
    response = requests.post(
        f"{BACKEND_URL}/api/auth/request-password-reset",
        json={"email": email}
    )
    assert response.status_code == 200
    print("  ✓ Password reset requested")
    
    # Generate reset token
    import sys
    sys.path.append('/app/backend')
    from auth_utils import create_password_reset_token
    
    reset_token = create_password_reset_token(user_id)
    
    # Reset password
    new_password = "NewTestPass456!"
    response = requests.post(
        f"{BACKEND_URL}/api/auth/reset-password",
        json={
            "token": reset_token,
            "new_password": new_password
        }
    )
    assert response.status_code == 200
    print("  ✓ Password reset successful")
    
    # Test login with new password
    response = requests.post(
        f"{BACKEND_URL}/api/auth/login",
        json={
            "email": email,
            "password": new_password
        }
    )
    assert response.status_code == 200
    print("  ✓ Login with new password successful")

def test_invalid_token():
    """Test invalid token handling"""
    print("\n=== Testing invalid token handling ===")
    
    response = requests.get(
        f"{BACKEND_URL}/api/auth/me",
        headers={"Authorization": "Bearer invalid_token"}
    )
    
    assert response.status_code == 401, f"Expected 401, got {response.status_code}"
    print("✓ Invalid token properly rejected")

def test_stage1_endpoints():
    """Verify Stage 1 endpoints still work"""
    print("\n=== Verifying Stage 1 endpoints ===")
    
    response = requests.get(f"{BACKEND_URL}/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    print("  ✓ GET /health")
    
    response = requests.get(f"{BACKEND_URL}/api/health")
    assert response.status_code == 200
    print("  ✓ GET /api/health")
    
    response = requests.get(f"{BACKEND_URL}/api/")
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Scienthesis API"
    print("  ✓ GET /api/")

def main():
    print("=" * 70)
    print("STAGE 2 TEST SUITE - Authentication & Email Flows")
    print("=" * 70)
    
    try:
        # Test Stage 1 endpoints first
        test_stage1_endpoints()
        
        # Test signup and get credentials
        email, user_id = test_signup()
        
        # Test duplicate signup
        test_duplicate_signup(email)
        
        # Test password validation
        test_password_validation()
        
        # Test login
        token = test_login(email)
        
        # Test get current user
        test_get_me(token)
        
        # Test email verification
        test_verify_email(user_id)
        
        # Test password reset
        test_password_reset(user_id, email)
        
        # Test invalid token
        test_invalid_token()
        
        print("\n" + "=" * 70)
        print("✓ ALL STAGE 2 TESTS PASSED")
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
