"""
Tests for Batch 1: Feature flags, grounded article context service, ArticleCard CTA.

Covers:
  A) 4 new feature flags default OFF
  B) Grounded article context service — source packets, grounding level, max 5 PMIDs
  C) Copilot backward compatibility after shared service extraction
"""
import os
import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# A) Feature flag tests
# ---------------------------------------------------------------------------

class TestNewFeatureFlags:

    def test_all_new_flags_default_off(self):
        """All 4 new flags must default to False when env vars are not set."""
        # Clear any env overrides
        env_keys = [
            "ENABLE_DIGEST_ARTICLE_AUDIO_LINKS",
            "ENABLE_LIBRARY_COMBINED_AUDIO_SUMMARY",
            "ENABLE_LITSCHOLAR_V1",
            "ENABLE_LITSCHOLAR_PROFILE_MEMORY",
        ]
        clean_env = {k: v for k, v in os.environ.items() if k not in env_keys}
        with patch.dict(os.environ, clean_env, clear=True):
            from utils.feature_flags import get_feature_flags
            flags = get_feature_flags()
            assert flags["enable_digest_article_audio_links"] is False
            assert flags["enable_library_combined_audio_summary"] is False
            assert flags["enable_litscholar_v1"] is False
            assert flags["enable_litscholar_profile_memory"] is False

    def test_flags_on_when_enabled(self):
        """Flags must be True when env vars set to 'true'."""
        overrides = {
            "ENABLE_DIGEST_ARTICLE_AUDIO_LINKS": "true",
            "ENABLE_LIBRARY_COMBINED_AUDIO_SUMMARY": "true",
            "ENABLE_LITSCHOLAR_V1": "true",
            "ENABLE_LITSCHOLAR_PROFILE_MEMORY": "true",
        }
        with patch.dict(os.environ, overrides):
            from utils.feature_flags import get_feature_flags
            flags = get_feature_flags()
            assert flags["enable_digest_article_audio_links"] is True
            assert flags["enable_library_combined_audio_summary"] is True
            assert flags["enable_litscholar_v1"] is True
            assert flags["enable_litscholar_profile_memory"] is True


# ---------------------------------------------------------------------------
# B) Grounded article context service tests
# ---------------------------------------------------------------------------

class TestGroundedArticleContextService:

    def _sample_article(self, pmid="11111", has_full_text=False):
        doc = {
            "pmid": pmid,
            "title": "Efficacy of Drug X in Heart Failure",
            "journal": "NEJM",
            "pub_date": "2026-01-15",
            "authors": "Smith J, Doe A",
            "abstract": "Background: Drug X is a novel agent. Methods: RCT of 500 patients. Results: Significant reduction in mortality.",
            "ai_summary": "Drug X reduces mortality in heart failure patients.",
            "key_findings": ["Drug X reduced mortality by 30%", "No major adverse effects"],
            "design_tags": ["randomized controlled trial"],
            "mesh_terms": ["Heart Failure", "Drug Therapy"],
            "doi": "10.1000/test",
        }
        if has_full_text:
            doc["full_text"] = "This is the full text of the article with much more detail..."
        return doc

    @pytest.mark.asyncio
    async def test_single_article_source_packet(self):
        from services.grounded_article_context_service import build_grounded_context
        db = AsyncMock()
        db.articles.find_one = AsyncMock(return_value=self._sample_article())

        result = await build_grounded_context(db, ["11111"])

        assert result["article_count"] == 1
        assert len(result["source_packets"]) == 1
        assert result["missing_pmids"] == []
        assert result["overall_grounding_level"] == "abstract_only"

        packet = result["source_packets"][0]
        assert packet["pmid"] == "11111"
        assert packet["title"] == "Efficacy of Drug X in Heart Failure"
        assert packet["grounding_level"] == "abstract_only"
        assert len(packet["key_findings"]) == 2
        assert len(packet["evidence_anchors"]) == 2  # 2 key_findings
        assert packet["citation_metadata"]["journal"] == "NEJM"

    @pytest.mark.asyncio
    async def test_full_text_grounding_level(self):
        from services.grounded_article_context_service import build_grounded_context
        db = AsyncMock()
        db.articles.find_one = AsyncMock(return_value=self._sample_article(has_full_text=True))

        result = await build_grounded_context(db, ["11111"])
        assert result["overall_grounding_level"] == "full_text_available"
        assert result["source_packets"][0]["grounding_level"] == "full_text_available"

    @pytest.mark.asyncio
    async def test_missing_pmid_tracked(self):
        from services.grounded_article_context_service import build_grounded_context
        db = AsyncMock()
        db.articles.find_one = AsyncMock(return_value=None)

        result = await build_grounded_context(db, ["99999"])
        assert result["article_count"] == 0
        assert result["missing_pmids"] == ["99999"]

    @pytest.mark.asyncio
    async def test_max_5_pmids_enforced(self):
        from services.grounded_article_context_service import build_grounded_context
        db = AsyncMock()

        with pytest.raises(ValueError, match="Maximum 5"):
            await build_grounded_context(db, ["1", "2", "3", "4", "5", "6"])

    @pytest.mark.asyncio
    async def test_empty_pmids_rejected(self):
        from services.grounded_article_context_service import build_grounded_context
        db = AsyncMock()

        with pytest.raises(ValueError, match="At least 1"):
            await build_grounded_context(db, [])

    @pytest.mark.asyncio
    async def test_multiple_articles_mixed_grounding(self):
        from services.grounded_article_context_service import build_grounded_context
        db = AsyncMock()

        call_count = 0
        async def mock_find(query, projection):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._sample_article("111", has_full_text=True)
            return self._sample_article("222", has_full_text=False)

        db.articles.find_one = mock_find

        result = await build_grounded_context(db, ["111", "222"])
        assert result["article_count"] == 2
        assert result["overall_grounding_level"] == "mixed"

    @pytest.mark.asyncio
    async def test_insufficient_info_for_missing_fields(self):
        from services.grounded_article_context_service import build_source_packet
        # Article with no key_findings and no limitations
        art = {"pmid": "000", "title": "T", "abstract": "A"}
        packet = build_source_packet(art)
        assert packet["key_findings"] == ["Insufficient information in available article text."]
        assert packet["limitations"] == ["Insufficient information in available article text."]

    @pytest.mark.asyncio
    async def test_article_text_combined_output(self):
        from services.grounded_article_context_service import build_grounded_context
        db = AsyncMock()

        async def mock_find(query, projection):
            pmid = query["pmid"]
            return self._sample_article(pmid)

        db.articles.find_one = mock_find

        result = await build_grounded_context(db, ["111", "222"])
        assert "---" in result["article_texts"]  # separator between articles
        assert "PMID: 111" in result["article_texts"]
        assert "PMID: 222" in result["article_texts"]


# ---------------------------------------------------------------------------
# C) Copilot backward compatibility after refactor
# ---------------------------------------------------------------------------

class TestCopilotBackwardCompat:

    @pytest.mark.asyncio
    async def test_fetch_article_uses_shared_service(self):
        """_fetch_article wrapper must call shared get_article_context."""
        from routes.copilot import _fetch_article
        import routes.copilot as copilot_mod

        mock_db = AsyncMock()
        mock_db.articles.find_one = AsyncMock(return_value={"pmid": "123", "title": "Test"})
        copilot_mod.db = mock_db

        result = await _fetch_article("123")
        assert result is not None
        assert result["pmid"] == "123"
        mock_db.articles.find_one.assert_awaited_once()

    def test_build_article_text_shared(self):
        """_build_article_text imported from shared service produces text."""
        from routes.copilot import _build_article_text
        art = {"pmid": "123", "title": "Test Article", "abstract": "Some abstract"}
        text = _build_article_text(art)
        assert "PMID: 123" in text
        assert "Test Article" in text
        assert "Some abstract" in text

    def test_make_citation_shared(self):
        """_make_citation imported from shared service produces citation dict."""
        from routes.copilot import _make_citation
        art = {"pmid": "123", "title": "T", "journal": "J", "pub_date": "2026"}
        cit = _make_citation(art)
        assert cit["pmid"] == "123"
        assert cit["journal"] == "J"
