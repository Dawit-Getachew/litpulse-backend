#!/usr/bin/env python3
"""
Stage 1 Health Check Script
Tests all Stage 1 requirements for the Scienthesis backend skeleton
"""

import requests
import sys

BACKEND_URL = "http://localhost:8001"

def test_health_endpoint():
    """Test GET /health endpoint"""
    print("Testing GET /health...")
    try:
        response = requests.get(f"{BACKEND_URL}/health")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data.get("status") == "ok", f"Expected status='ok', got {data}"
        print("✓ GET /health passed")
        return True
    except Exception as e:
        print(f"✗ GET /health failed: {e}")
        return False

def test_api_health_endpoint():
    """Test GET /api/health endpoint"""
    print("Testing GET /api/health...")
    try:
        response = requests.get(f"{BACKEND_URL}/api/health")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert data.get("status") == "ok", f"Expected status='ok', got {data}"
        print("✓ GET /api/health passed")
        return True
    except Exception as e:
        print(f"✗ GET /api/health failed: {e}")
        return False

def test_api_root():
    """Test GET /api/ endpoint"""
    print("Testing GET /api/...")
    try:
        response = requests.get(f"{BACKEND_URL}/api/")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert "message" in data, f"Missing 'message' field in {data}"
        assert "version" in data, f"Missing 'version' field in {data}"
        assert data["message"] == "Scienthesis API", f"Unexpected message: {data['message']}"
        assert data["version"] == "1.0.0", f"Unexpected version: {data['version']}"
        print("✓ GET /api/ passed")
        return True
    except Exception as e:
        print(f"✗ GET /api/ failed: {e}")
        return False

def main():
    print("=" * 60)
    print("STAGE 1 HEALTH CHECK - Scienthesis Backend Skeleton")
    print("=" * 60)
    print()
    
    results = []
    results.append(test_health_endpoint())
    results.append(test_api_health_endpoint())
    results.append(test_api_root())
    
    print()
    print("=" * 60)
    if all(results):
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        return 0
    else:
        print("✗ SOME TESTS FAILED")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    sys.exit(main())
