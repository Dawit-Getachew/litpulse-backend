"""
Step 19: Server-side Library Filtering/Sorting Tests

Tests:
- GET /api/library (no params) — backward compat returns {articles, total, next_cursor}
- GET /api/library?limit=50 — returns paginated results
- GET /api/library?search=eGFR — server-side search matches title/journal
- GET /api/library?search=zzzznotfound — returns {articles: [], total: 0, next_cursor: null}
- GET /api/library?sort_by=title&sort_dir=asc — sorts by title ascending
- GET /api/library?sort_by=saved_at&sort_dir=desc — default sort behavior
- GET /api/library?design_type=Meta-Analysis — filters by design tag
- GET /api/library?saved_after=2025-01-01T00:00:00 — date range filter works
- Compound cursor format testing
- Old cursor format (bare string) backward compatibility
"""

import pytest
import requests
import os
import json
from datetime import datetime, timezone, timedelta

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test users
PREMIUM_USER = {"email": "demo@litpulse.com", "password": "DemoPass123!"}
FREE_USER = {"email": "test@litpulse.com", "password": "TestPass123!"}


def login(email, password):
    """Helper to log in and get auth token."""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password})
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def premium_token():
    """Get auth token for premium/admin user."""
    return login(PREMIUM_USER["email"], PREMIUM_USER["password"])


@pytest.fixture(scope="module")
def free_token():
    """Get auth token for free user."""
    return login(FREE_USER["email"], FREE_USER["password"])


@pytest.fixture(scope="module")
def auth_headers(premium_token):
    """Auth headers for premium user."""
    return {"Authorization": f"Bearer {premium_token}"}


class TestLibraryBackwardCompatibility:
    """Test GET /api/library maintains backward compat."""

    def test_library_no_params_returns_expected_structure(self, auth_headers):
        """GET /api/library (no params) returns {articles, total, next_cursor}."""
        resp = requests.get(f"{BASE_URL}/api/library", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert "articles" in data, "Response missing 'articles' key"
        assert "total" in data, "Response missing 'total' key"
        assert "next_cursor" in data, "Response missing 'next_cursor' key"
        assert isinstance(data["articles"], list), "'articles' should be a list"
        assert isinstance(data["total"], int), "'total' should be an integer"
        print(f"PASS: GET /api/library returns correct structure - {len(data['articles'])} articles, total={data['total']}")

    def test_library_with_limit_50(self, auth_headers):
        """GET /api/library?limit=50 returns paginated results."""
        resp = requests.get(f"{BASE_URL}/api/library?limit=50", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert len(data["articles"]) <= 50, f"Should return at most 50 articles, got {len(data['articles'])}"
        print(f"PASS: GET /api/library?limit=50 returns {len(data['articles'])} articles (max 50)")


class TestLibraryServerSideSearch:
    """Test server-side search on title/journal."""

    def test_search_egfr_finds_matching_article(self, auth_headers):
        """GET /api/library?search=eGFR should find articles matching title/journal."""
        resp = requests.get(f"{BASE_URL}/api/library?search=eGFR", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        # The test dataset has 1 saved article with 'eGFR' in title
        print(f"Search 'eGFR': {len(data['articles'])} articles found, total={data['total']}")
        
        # Verify search is actually filtering (if articles exist with eGFR)
        if data["total"] > 0:
            # Check that at least one article contains 'egfr' in title or journal
            found_match = False
            for article in data["articles"]:
                title_lower = (article.get("title") or "").lower()
                journal_lower = (article.get("journal") or "").lower()
                if "egfr" in title_lower or "egfr" in journal_lower:
                    found_match = True
                    break
            assert found_match, "Search 'eGFR' should return articles with 'eGFR' in title/journal"
            print(f"PASS: search=eGFR returned matching articles")
        else:
            print(f"INFO: No articles with 'eGFR' found (may need seed data)")

    def test_search_nonexistent_returns_empty(self, auth_headers):
        """GET /api/library?search=zzzznotfound should return empty."""
        resp = requests.get(f"{BASE_URL}/api/library?search=zzzznotfound", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert data["articles"] == [], f"Expected empty articles, got {len(data['articles'])}"
        assert data["total"] == 0, f"Expected total=0, got {data['total']}"
        assert data["next_cursor"] is None, f"Expected next_cursor=null, got {data['next_cursor']}"
        print("PASS: search=zzzznotfound returns {articles: [], total: 0, next_cursor: null}")


class TestLibraryServerSideSorting:
    """Test server-side sorting by title and saved_at."""

    def test_sort_by_title_asc(self, auth_headers):
        """GET /api/library?sort_by=title&sort_dir=asc sorts by title ascending."""
        resp = requests.get(f"{BASE_URL}/api/library?sort_by=title&sort_dir=asc", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        articles = data["articles"]
        
        if len(articles) >= 2:
            # Verify ascending order
            titles = [a.get("title", "").lower() for a in articles]
            assert titles == sorted(titles), "Articles should be sorted by title ascending"
            print(f"PASS: sort_by=title&sort_dir=asc - sorted correctly ({len(articles)} articles)")
        else:
            print(f"INFO: Only {len(articles)} article(s), cannot verify sort order")

    def test_sort_by_saved_at_desc(self, auth_headers):
        """GET /api/library?sort_by=saved_at&sort_dir=desc (default behavior)."""
        resp = requests.get(f"{BASE_URL}/api/library?sort_by=saved_at&sort_dir=desc", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        articles = data["articles"]
        
        if len(articles) >= 2:
            # Verify descending order by saved_at
            saved_dates = [a.get("saved_at", "") for a in articles]
            assert saved_dates == sorted(saved_dates, reverse=True), "Articles should be sorted by saved_at descending"
            print(f"PASS: sort_by=saved_at&sort_dir=desc - sorted correctly")
        else:
            print(f"INFO: Only {len(articles)} article(s), cannot verify sort order")


class TestLibraryServerSideFiltering:
    """Test server-side filtering by design_type and saved_after."""

    def test_filter_by_design_type_meta_analysis(self, auth_headers):
        """GET /api/library?design_type=Meta-Analysis filters by design tag."""
        resp = requests.get(f"{BASE_URL}/api/library?design_type=Meta-Analysis", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        articles = data["articles"]
        
        # Verify all returned articles have Meta-Analysis in design_tags
        for article in articles:
            design_tags = article.get("design_tags", [])
            tags_lower = [t.lower() for t in design_tags]
            assert any("meta-analysis" in t for t in tags_lower), \
                f"Article should have 'Meta-Analysis' in design_tags: {design_tags}"
        
        print(f"PASS: design_type=Meta-Analysis returned {len(articles)} matching articles")

    def test_filter_by_design_type_nonexistent(self, auth_headers):
        """GET /api/library?design_type=NonexistentType returns empty."""
        resp = requests.get(f"{BASE_URL}/api/library?design_type=NonexistentDesignType123", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert data["total"] == 0, f"Expected total=0, got {data['total']}"
        print("PASS: design_type=NonexistentType returns empty")

    def test_filter_by_saved_after_date(self, auth_headers):
        """GET /api/library?saved_after=2025-01-01T00:00:00 filters by date."""
        saved_after = "2025-01-01T00:00:00"
        resp = requests.get(f"{BASE_URL}/api/library?saved_after={saved_after}", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        articles = data["articles"]
        
        # Verify all returned articles were saved after the filter date
        cutoff_str = saved_after  # Compare as strings (ISO format is lexicographically sortable)
        for article in articles:
            saved_at = article.get("saved_at")
            if saved_at:
                # Normalize to compare strings - strip timezone info for comparison
                saved_at_base = saved_at.split("+")[0].split("Z")[0]
                assert saved_at_base >= cutoff_str, f"Article saved_at {saved_at} should be >= {saved_after}"
        
        print(f"PASS: saved_after={saved_after} returned {len(articles)} articles (all after cutoff)")

    def test_filter_by_future_date_returns_empty(self, auth_headers):
        """GET /api/library?saved_after=2030-01-01T00:00:00 returns empty (future date)."""
        saved_after = "2030-01-01T00:00:00"
        resp = requests.get(f"{BASE_URL}/api/library?saved_after={saved_after}", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        assert data["total"] == 0, f"Expected total=0 for future date filter, got {data['total']}"
        print("PASS: saved_after=2030-01-01 (future) returns empty")


class TestLibraryCompoundCursor:
    """Test compound cursor format '<sort_value>|<article_id>'."""

    def test_compound_cursor_format_accepted(self, auth_headers):
        """Compound cursor format '<sort_value>|<article_id>' should be accepted."""
        # Construct a fake compound cursor
        fake_cursor = "2025-12-03T16:29:13.632616+00:00|someid123"
        resp = requests.get(f"{BASE_URL}/api/library?cursor={fake_cursor}", headers=auth_headers)
        
        # Should not error (even if it returns empty results due to cursor past end)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "articles" in data, "Response should have 'articles' key"
        print(f"PASS: Compound cursor format accepted (returned {len(data['articles'])} articles)")

    def test_old_cursor_format_backward_compat(self, auth_headers):
        """Old cursor format (bare string) should still be accepted."""
        # Construct an old-style bare cursor (just saved_at value)
        old_cursor = "2025-12-03T16:29:13.632616+00:00"
        resp = requests.get(f"{BASE_URL}/api/library?cursor={old_cursor}", headers=auth_headers)
        
        # Should not error
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert "articles" in data, "Response should have 'articles' key"
        print(f"PASS: Old cursor format (bare string) accepted (backward compat)")

    def test_next_cursor_contains_pipe_for_compound(self, auth_headers):
        """If next_cursor returned, it should use compound format '<sort_value>|<article_id>'."""
        resp = requests.get(f"{BASE_URL}/api/library?limit=1", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        next_cursor = data.get("next_cursor")
        
        if next_cursor is not None:
            # Compound cursor should contain '|'
            assert "|" in next_cursor, f"Compound cursor should contain '|': {next_cursor}"
            print(f"PASS: next_cursor uses compound format: {next_cursor}")
        else:
            # If only 1 article exists, next_cursor will be null (expected)
            print(f"INFO: next_cursor is null (all articles fit in limit=1, or library empty)")


class TestLibraryCombinedFilters:
    """Test combining multiple filters."""

    def test_search_and_design_type_combined(self, auth_headers):
        """GET /api/library?search=eGFR&design_type=Meta-Analysis combines filters."""
        resp = requests.get(f"{BASE_URL}/api/library?search=eGFR&design_type=Meta-Analysis", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        articles = data["articles"]
        
        # Verify all returned articles match BOTH criteria
        for article in articles:
            title_lower = (article.get("title") or "").lower()
            journal_lower = (article.get("journal") or "").lower()
            design_tags = [t.lower() for t in article.get("design_tags", [])]
            
            assert "egfr" in title_lower or "egfr" in journal_lower, \
                f"Article should match search 'eGFR'"
            assert any("meta-analysis" in t for t in design_tags), \
                f"Article should have 'Meta-Analysis' design tag"
        
        print(f"PASS: Combined search + design_type filter returned {len(articles)} matching articles")

    def test_all_filters_combined(self, auth_headers):
        """Test all filters: search, design_type, saved_after, sort_by, sort_dir."""
        params = {
            "search": "eGFR",
            "design_type": "Meta-Analysis",
            "saved_after": "2025-01-01T00:00:00",
            "sort_by": "title",
            "sort_dir": "asc",
            "limit": 50
        }
        resp = requests.get(f"{BASE_URL}/api/library", params=params, headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        print(f"PASS: All filters combined - {len(data['articles'])} articles, total={data['total']}")


class TestLibraryArticleStructure:
    """Verify article structure in library response."""

    def test_article_has_expected_fields(self, auth_headers):
        """Articles should have pmid, title, journal, saved_at, etc."""
        resp = requests.get(f"{BASE_URL}/api/library?limit=10", headers=auth_headers)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        
        data = resp.json()
        articles = data["articles"]
        
        if len(articles) > 0:
            article = articles[0]
            # Check expected fields
            assert "pmid" in article or "title" in article, "Article should have pmid or title"
            
            # saved_at should be present from user_articles merge
            if "saved_at" in article:
                print(f"Article has saved_at: {article['saved_at']}")
            
            # Should NOT have internal _sort_article_id field
            assert "_sort_article_id" not in article, "Internal field _sort_article_id should be removed"
            
            print(f"PASS: Article structure valid - keys: {list(article.keys())[:10]}...")
        else:
            print("INFO: No articles in library to check structure")


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
