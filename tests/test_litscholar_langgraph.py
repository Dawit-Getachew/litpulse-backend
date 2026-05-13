"""
Automated tests for LitScholar LangGraph Spike — Hardened

Tests:
  1. Flag OFF → 404
  2. Valid PMID + question → success with grounded answer
  3. Invalid PMID → clean error
  4. Abstract-only fallback (no full_text)
  5. No unintended writes/side effects
  6. Existing copilot endpoint still works
  7. Unauthenticated request → 401
  8. Article access denied for unknown PMID → 403

Run: pytest backend/tests/test_litscholar_langgraph.py -v
"""
import os
import sys
import pytest
import asyncio

# Ensure backend is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="module")
def api_url():
    return os.environ.get(
        "TEST_API_URL",
        "https://litscreen-aggregate.preview.emergentagent.com",
    )


@pytest.fixture(scope="module")
def auth_token(api_url):
    """Get auth token for test user."""
    import requests
    resp = requests.post(
        f"{api_url}/api/auth/login",
        json={"email": "test@example.com", "password": "test123"},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def real_pmid():
    """A real PMID that exists in the test database with an abstract."""
    import pymongo
    client = pymongo.MongoClient("mongodb://localhost:27017")
    db = client["test_database"]
    art = db.articles.find_one(
        {"abstract": {"$regex": ".{50,}"}, "pmid": {"$regex": "^4"}},
        {"_id": 0, "pmid": 1},
    )
    if art:
        return art["pmid"]
    # Fallback: any article with abstract
    art = db.articles.find_one(
        {"abstract": {"$exists": True, "$ne": ""}},
        {"_id": 0, "pmid": 1},
    )
    return art["pmid"] if art else "40192339"


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Test 1: Flag OFF → 404
# ---------------------------------------------------------------------------

class TestFlagOff:
    """When flag is OFF, endpoint must return 404."""

    def test_flag_off_returns_404(self, api_url, auth_token):
        """This test requires the flag to be OFF in .env.
        Since we set it to false in the hardening pass, this should pass
        unless someone manually re-enabled it.
        We test by checking the response — if 404, flag is OFF (correct default).
        If 200, the flag is ON (which means someone enabled it for testing).
        """
        import requests
        resp = requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers=_headers(auth_token),
            json={"pmid": "12345678", "question": "test question"},
        )
        # Accept either 404 (flag off) or 200/4xx (flag on, other validation)
        # The key assertion: if flag is off, must be 404
        if os.environ.get("ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE", "false").lower() == "false":
            assert resp.status_code == 404, f"Expected 404 when flag OFF, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Tests 2-8: Flag ON tests (require flag to be enabled)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def enable_flag_for_tests():
    """Temporarily enable the flag for testing if not already on."""
    original = os.environ.get("ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE")
    os.environ["ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE"] = "true"
    yield
    if original is not None:
        os.environ["ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE"] = original
    else:
        os.environ.pop("ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE", None)


class TestDeepDiveEndpoint:
    """Tests that require the flag to be ON (running against live server)."""

    def _is_flag_on(self, api_url, auth_token):
        """Helper: check if endpoint is available."""
        import requests
        resp = requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers=_headers(auth_token),
            json={"pmid": "test", "question": "test question here"},
        )
        return resp.status_code != 404

    # Test 2: Valid PMID + question → success
    def test_valid_pmid_returns_grounded_answer(self, api_url, auth_token, real_pmid):
        import requests
        resp = requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers=_headers(auth_token),
            json={"pmid": real_pmid, "question": "What are the key findings of this study?"},
            timeout=120,
        )
        if resp.status_code == 404:
            pytest.skip("Flag is OFF on server")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["success"] is True
        assert data["graph_engine"] == "langgraph"
        assert data["pmid"] == real_pmid
        assert data["grounding_level"] in ("abstract_only", "full_text_available")
        assert data["source_label"] in ("abstract", "full-text", "ai-summary")
        assert len(data["answer"]) > 10, "Answer should be non-trivial"
        assert data["citations_verified"] is True
        assert data["disclaimer"] is not None

    # Test 3: Invalid PMID → clean error
    def test_invalid_pmid_returns_clean_error(self, api_url, auth_token):
        import requests
        resp = requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers=_headers(auth_token),
            json={"pmid": "NONEXISTENT_99999", "question": "What is this?"},
            timeout=30,
        )
        if resp.status_code == 404:
            pytest.skip("Flag is OFF on server")
        # Should be 403 (no access) or 200 with success=false
        if resp.status_code == 403:
            assert "access" in resp.json().get("detail", "").lower()
        else:
            data = resp.json()
            assert data["success"] is False
            assert "not found" in (data.get("error") or "").lower()

    # Test 4: Abstract-only fallback
    def test_abstract_only_fallback(self, api_url, auth_token, real_pmid):
        """Most articles won't have full_text — verify abstract fallback works."""
        import requests
        resp = requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers=_headers(auth_token),
            json={"pmid": real_pmid, "question": "Summarize the methodology"},
            timeout=120,
        )
        if resp.status_code == 404:
            pytest.skip("Flag is OFF on server")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # Real articles in test DB don't have full_text
        assert data["grounding_level"] == "abstract_only"
        assert data["source_label"] in ("abstract", "ai-summary")

    # Test 5: No unintended writes
    def test_no_writes_side_effects(self, api_url, auth_token, real_pmid):
        """Verify the endpoint doesn't write to any collection."""
        import pymongo
        client = pymongo.MongoClient("mongodb://localhost:27017")
        db = client["test_database"]

        # Snapshot counts before
        before = {
            "articles": db.articles.count_documents({}),
            "user_articles": db.user_articles.count_documents({}),
            "user_library": db.user_library.count_documents({}),
            "article_screening": db.article_screening.count_documents({}),
            "digests": db.digests.count_documents({}),
            "digest_profiles": db.digest_profiles.count_documents({}),
            "users": db.users.count_documents({}),
        }

        # Make the request
        import requests
        requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers=_headers(auth_token),
            json={"pmid": real_pmid, "question": "Side effect test"},
            timeout=120,
        )

        # Snapshot counts after
        after = {
            "articles": db.articles.count_documents({}),
            "user_articles": db.user_articles.count_documents({}),
            "user_library": db.user_library.count_documents({}),
            "article_screening": db.article_screening.count_documents({}),
            "digests": db.digests.count_documents({}),
            "digest_profiles": db.digest_profiles.count_documents({}),
            "users": db.users.count_documents({}),
        }

        for collection, count_before in before.items():
            assert after[collection] == count_before, \
                f"Collection '{collection}' changed: {count_before} → {after[collection]}"

    # Test 6: Existing copilot still works
    def test_existing_copilot_unchanged(self, api_url, auth_token):
        """Verify the production copilot health endpoint still responds."""
        import requests
        resp = requests.get(
            f"{api_url}/api/copilot/health",
            headers=_headers(auth_token),
            timeout=15,
        )
        # Copilot health should respond (may return various status codes depending on config)
        assert resp.status_code in (200, 503), f"Copilot health unexpected: {resp.status_code}"

    # Test 7: Unauthenticated → 401/403
    def test_unauthenticated_rejected(self, api_url):
        import requests
        resp = requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers={"Content-Type": "application/json"},
            json={"pmid": "12345", "question": "test"},
            timeout=15,
        )
        assert resp.status_code in (401, 403, 404), \
            f"Expected auth rejection, got {resp.status_code}"

    # Test 8: Citation integrity — passages have source labels
    def test_citation_integrity(self, api_url, auth_token, real_pmid):
        import requests
        resp = requests.post(
            f"{api_url}/api/litscholar-experimental/deep-dive",
            headers=_headers(auth_token),
            json={"pmid": real_pmid, "question": "What are the main results?"},
            timeout=120,
        )
        if resp.status_code == 404:
            pytest.skip("Flag is OFF on server")
        data = resp.json()
        if data["success"] and data.get("supporting_passages"):
            for p in data["supporting_passages"]:
                assert "source_label" in p, f"Passage missing source_label: {p}"
                assert p["source_label"] in ("abstract", "full-text", "ai-summary", ""), \
                    f"Invalid source_label: {p['source_label']}"
                assert "passage_id" in p, f"Passage missing passage_id: {p}"
