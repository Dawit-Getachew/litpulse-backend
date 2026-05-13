"""
API Smoke Test Suite — Phase 0 CI Gate

Tests the 7 core API endpoints against a real (seeded) backend.
Expects environment variables:
  SMOKE_API_URL  — base URL, e.g. http://localhost:8001
  SMOKE_EMAIL    — seeded test user email (default: smoketest@litpulse.com)
  SMOKE_PASSWORD — seeded test user password (default: SmokeTest123!)
  SMOKE_PMID     — a known PMID from seeded articles (default: smoke_pmid_001)

All tests are ordered and share an authenticated session.
"""
import os
import sys
import pytest
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_URL = os.environ.get("SMOKE_API_URL", "http://localhost:8001").rstrip("/")
EMAIL = os.environ.get("SMOKE_EMAIL", "smoketest@litpulse.com")
PASSWORD = os.environ.get("SMOKE_PASSWORD", "SmokeTest123!")
PMID = os.environ.get("SMOKE_PMID", "smoke_pmid_001")


@pytest.fixture(scope="session")
def auth_token():
    """Authenticate once; share across all smoke tests."""
    resp = requests.post(
        f"{API_URL}/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, (
        f"Login failed ({resp.status_code}): {resp.text[:200]}"
    )
    token = resp.json()["access_token"]
    assert token, "access_token missing from login response"
    return token


@pytest.fixture(scope="session")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


# ---------------------------------------------------------------------------
# Smoke Tests
# ---------------------------------------------------------------------------

class TestHealthEndpoints:
    def test_api_health(self):
        """/api/health returns 200 ok."""
        resp = requests.get(f"{API_URL}/api/health", timeout=5)
        assert resp.status_code == 200
        assert resp.json().get("status") == "ok"

    def test_feature_flags_endpoint(self):
        """/api/config/feature-flags returns 200 with Phase-0 keys."""
        resp = requests.get(f"{API_URL}/api/config/feature-flags", timeout=5)
        assert resp.status_code == 200
        body = resp.json()
        phase0_keys = [
            "enable_new_landing_page",
            "enable_premium_trials",
            "enable_explore_topic_search_v2",
            "enable_multi_digest_profiles",
            "enable_community_v2",
            "enable_library_audio_digests_v2",
            "enable_multi_digest_profiles_scheduler",
            "enforce_community_digest_membership",
        ]
        for key in phase0_keys:
            assert key in body, f"Phase-0 flag '{key}' missing from /api/config/feature-flags"
            assert body[key] is False, f"Phase-0 flag '{key}' should default to false"


class TestAuthEndpoints:
    def test_auth_me(self, auth_headers):
        """/api/auth/me returns the authenticated user."""
        resp = requests.get(f"{API_URL}/api/auth/me", headers=auth_headers, timeout=5)
        assert resp.status_code == 200
        user = resp.json()
        assert "user_id" in user
        assert "email" in user
        assert user["email"] == EMAIL

    def test_auth_me_rejects_unauthenticated(self):
        """/api/auth/me returns 401 without a token."""
        resp = requests.get(f"{API_URL}/api/auth/me", timeout=5)
        assert resp.status_code == 401


class TestDigestsEndpoints:
    def test_digests_list(self, auth_headers):
        """/api/digests returns a list (may be empty for new users)."""
        resp = requests.get(f"{API_URL}/api/digests", headers=auth_headers, timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        assert "digests" in body
        assert isinstance(body["digests"], list)

    def test_digests_list_contains_seeded_digest(self, auth_headers):
        """/api/digests list must contain at least 1 seeded digest."""
        resp = requests.get(f"{API_URL}/api/digests?limit=50", headers=auth_headers, timeout=10)
        assert resp.status_code == 200
        digests = resp.json()["digests"]
        assert len(digests) >= 1, "Expected at least 1 seeded digest in /api/digests"


class TestArticlesEndpoints:
    def test_article_detail(self, auth_headers):
        """/api/articles/{pmid} returns article data for the seeded article."""
        resp = requests.get(
            f"{API_URL}/api/articles/{PMID}",
            headers=auth_headers,
            timeout=10,
        )
        # 200 if found; 404 is acceptable if pmid not in articles collection
        # (seed script must ensure it is present)
        assert resp.status_code == 200, (
            f"Article {PMID} not found. Ensure seed script ran successfully. "
            f"Response: {resp.text[:200]}"
        )
        article = resp.json()
        assert "pmid" in article or "title" in article


class TestLibraryEndpoints:
    def test_library_list(self, auth_headers):
        """/api/library returns paginated library response."""
        resp = requests.get(f"{API_URL}/api/library", headers=auth_headers, timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        assert "articles" in body
        assert isinstance(body["articles"], list)

    def test_library_has_seeded_article(self, auth_headers):
        """/api/library must contain at least 1 seeded saved article."""
        resp = requests.get(f"{API_URL}/api/library", headers=auth_headers, timeout=10)
        assert resp.status_code == 200
        articles = resp.json()["articles"]
        assert len(articles) >= 1, "Expected at least 1 seeded article in /api/library"


class TestCommunityEndpoints:
    def test_specialty_rooms(self, auth_headers):
        """/api/discussions/specialty-rooms returns a rooms list."""
        resp = requests.get(
            f"{API_URL}/api/discussions/specialty-rooms",
            headers=auth_headers,
            timeout=10,
        )
        assert resp.status_code == 200
        body = resp.json()
        # Response may be a dict with "rooms" key or a list
        assert isinstance(body, (dict, list))
