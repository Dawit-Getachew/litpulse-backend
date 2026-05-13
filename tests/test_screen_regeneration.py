"""
Tests for the Screen summary regeneration flow.

Covers:
  1. GET /workspace/screen/queue includes needs_ai_summary field
  2. GET /workspace/screen/queue does NOT trigger background writes (no side effects)
  3. POST /workspace/screen/regenerate-summaries accepts PMIDs and returns count
  4. POST /workspace/screen/regenerate-summaries requires auth
  5. POST /workspace/screen/regenerate-summaries caps batch size
  6. POST /workspace/screen/regenerate-summaries with empty list returns 0
  7. Admin dry-run endpoint is disabled by default (.env reverted)
"""

import os
import sys

import pytest
import requests

# Ensure backend root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE_URL = os.environ.get("TEST_BASE_URL", "https://litscreen-aggregate.preview.emergentagent.com")


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _get_auth_token(email: str = "admin@test.com", password: str = "TestPass123!") -> str:
    """Get a valid auth token for testing."""
    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
    )
    if resp.status_code != 200:
        # Try signup first
        requests.post(
            f"{BASE_URL}/api/auth/signup",
            json={"email": email, "password": password, "name": "Test User"},
        )
        resp = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": email, "password": password},
        )
    return resp.json().get("access_token", "")


# ══════════════════════════════════════════════════════════════════
# Test 1: GET /screen/queue includes needs_ai_summary field
# ══════════════════════════════════════════════════════════════════

class TestScreenQueueResponse:
    """Verify GET /screen/queue response shape includes needs_ai_summary."""

    def test_queue_response_is_200(self):
        token = _get_auth_token()
        resp = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "articles" in data
        assert "progress" in data

    def test_articles_have_needs_ai_summary_field(self):
        """If articles are returned, they should include needs_ai_summary boolean."""
        token = _get_auth_token()
        resp = requests.get(
            f"{BASE_URL}/api/workspace/screen/queue",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        # Even if empty queue, verify shape is correct
        for article in data.get("articles", []):
            assert "needs_ai_summary" in article, \
                f"Article {article.get('pmid')} missing needs_ai_summary field"
            assert isinstance(article["needs_ai_summary"], bool)


# ══════════════════════════════════════════════════════════════════
# Test 2: GET /screen/queue has no side effects
# ══════════════════════════════════════════════════════════════════

class TestGetScreenQueueSideEffectFree:
    """Verify GET handler does not trigger background writes."""

    def test_no_asyncio_create_task_in_get_handler(self):
        """Static analysis: the GET handler should not contain asyncio.create_task."""
        import inspect
        from routes.workspace import get_screen_queue
        source = inspect.getsource(get_screen_queue)
        assert "asyncio.create_task" not in source, \
            "GET /screen/queue still contains asyncio.create_task — side effect not removed!"
        assert "create_task" not in source, \
            "GET /screen/queue still contains create_task — side effect not removed!"


# ══════════════════════════════════════════════════════════════════
# Test 3: POST /screen/regenerate-summaries basic behavior
# ══════════════════════════════════════════════════════════════════

class TestRegenerateSummariesEndpoint:
    """Test POST /workspace/screen/regenerate-summaries."""

    def test_requires_auth(self):
        """Endpoint requires authentication."""
        resp = requests.post(
            f"{BASE_URL}/api/workspace/screen/regenerate-summaries",
            json={"pmids": ["12345"]},
        )
        assert resp.status_code == 401

    def test_empty_pmids_returns_zero(self):
        """Empty PMID list returns regenerating: 0."""
        token = _get_auth_token()
        resp = requests.post(
            f"{BASE_URL}/api/workspace/screen/regenerate-summaries",
            headers={"Authorization": f"Bearer {token}"},
            json={"pmids": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["regenerating"] == 0

    def test_accepts_pmids_and_returns_count(self):
        """Endpoint accepts PMIDs and returns regenerating count."""
        token = _get_auth_token()
        pmids = ["11111111", "22222222", "33333333"]
        resp = requests.post(
            f"{BASE_URL}/api/workspace/screen/regenerate-summaries",
            headers={"Authorization": f"Bearer {token}"},
            json={"pmids": pmids},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["regenerating"] == len(pmids)

    def test_caps_batch_at_50(self):
        """Endpoint caps PMIDs at 50 even if more are sent."""
        token = _get_auth_token()
        pmids = [str(i) for i in range(100)]  # Send 100
        resp = requests.post(
            f"{BASE_URL}/api/workspace/screen/regenerate-summaries",
            headers={"Authorization": f"Bearer {token}"},
            json={"pmids": pmids},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["regenerating"] <= 50


# ══════════════════════════════════════════════════════════════════
# Test 4: Admin dry-run endpoint disabled by default
# ══════════════════════════════════════════════════════════════════

class TestAdminDryrunDisabledByDefault:
    """Verify the .env default was reverted."""

    def test_env_file_has_false_default(self):
        """The .env file should have ENABLE_ADMIN_MIGRATION_DRYRUN=false."""
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                content = f.read()
            # Should be false or not present
            if "ENABLE_ADMIN_MIGRATION_DRYRUN" in content:
                assert "ENABLE_ADMIN_MIGRATION_DRYRUN=false" in content, \
                    "ENABLE_ADMIN_MIGRATION_DRYRUN must default to false in .env"

    def test_endpoint_returns_404_when_disabled(self):
        """Admin dry-run endpoint should return 404 when feature gate is off."""
        token = _get_auth_token()
        resp = requests.post(
            f"{BASE_URL}/api/admin/migration-dryrun",
            headers={"Authorization": f"Bearer {token}"},
            json={"phases": "A"},
        )
        assert resp.status_code == 404, \
            f"Expected 404 but got {resp.status_code}. Feature gate may be enabled."


# ══════════════════════════════════════════════════════════════════
# Test 5: No infinite request loop (structural verification)
# ══════════════════════════════════════════════════════════════════

class TestNoInfiniteLoop:
    """Structural verification that the frontend wiring prevents loops."""

    def test_regeneration_ref_prevents_duplicate_requests(self):
        """The ScreenWorkspacePage uses a ref to prevent re-requesting same PMIDs."""
        screen_page_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "frontend", "src",
            "pages", "workspace", "ScreenWorkspacePage.tsx"
        )
        if not os.path.exists(screen_page_path):
            pytest.skip("Frontend source not available in this environment")
        
        with open(screen_page_path) as f:
            source = f.read()
        
        # Verify dedup ref exists
        assert "regenerationRequestedRef" in source, \
            "Missing regenerationRequestedRef — loop prevention mechanism required"
        assert "regenerationInFlightRef" in source, \
            "Missing regenerationInFlightRef — concurrent request prevention required"
        # Verify it checks before requesting
        assert "regenerationRequestedRef.current.has" in source, \
            "Must check regenerationRequestedRef before sending requests"
        # Verify it doesn't call regenerateSummaries in a way that triggers re-renders
        assert "fire-and-forget" in source.lower() or "non-blocking" in source.lower() or "Fire-and-forget" in source or "Non-blocking" in source, \
            "Comment indicating fire-and-forget pattern should be present"

    def test_queue_renders_before_regeneration(self):
        """The queue should render immediately; regeneration runs after load."""
        screen_page_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "frontend", "src",
            "pages", "workspace", "ScreenWorkspacePage.tsx"
        )
        if not os.path.exists(screen_page_path):
            pytest.skip("Frontend source not available in this environment")
        
        with open(screen_page_path) as f:
            source = f.read()
        
        # The regeneration effect should depend on [articles, queueLoading]
        # and should only fire when queueLoading is false (meaning queue has rendered)
        assert "queueLoading" in source
        assert "if (queueLoading" in source, \
            "Regeneration effect must check queueLoading to avoid firing during load"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
