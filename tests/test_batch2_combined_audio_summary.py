"""
Tests for Batch 2: Library Combined Audio Summary.

Covers:
  A) AudioDigestCreate model accepts mode field
  B) Combined summary creation with grounded context
  C) Max 5 PMIDs enforcement
  D) Playlist mode backward compatibility
  E) Detail endpoint handles both modes
  F) Feature flag gating
"""
import os
import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# A) Model validation
# ---------------------------------------------------------------------------

class TestAudioDigestModel:

    def test_mode_defaults_to_playlist(self):
        from routes.audio_digests import AudioDigestCreate
        model = AudioDigestCreate(pmids=["123"])
        assert model.mode == "playlist"

    def test_mode_combined_summary_accepted(self):
        from routes.audio_digests import AudioDigestCreate
        model = AudioDigestCreate(pmids=["123"], mode="combined_summary")
        assert model.mode == "combined_summary"

    def test_mode_invalid_rejected(self):
        from routes.audio_digests import AudioDigestCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AudioDigestCreate(pmids=["123"], mode="invalid_mode")


# ---------------------------------------------------------------------------
# B) Combined summary script generation
# ---------------------------------------------------------------------------

class TestCombinedScriptGeneration:

    @pytest.mark.asyncio
    async def test_template_script_with_single_article(self):
        """When LLM is unavailable, template fallback must produce valid script."""
        from routes.audio_digests import _generate_combined_script

        context = {
            "source_packets": [{
                "pmid": "111",
                "title": "Test Article",
                "citation_metadata": {"journal": "NEJM", "pub_date": "2026"},
                "study_type": "RCT",
                "key_findings": ["Finding A", "Finding B"],
                "limitations": ["Small sample size"],
                "evidence_anchors": [],
                "grounding_level": "abstract_only",
            }],
            "overall_grounding_level": "abstract_only",
            "article_texts": "PMID: 111\nTitle: Test Article",
        }

        # Mock provider to raise so we get the template fallback
        with patch("utils.copilot_provider.create_copilot_provider", side_effect=ImportError("mock")):
            script = await _generate_combined_script(context)

        assert "grounded only in the article abstracts" in script
        assert "Test Article" in script
        assert "Finding A" in script
        assert "Small sample size" in script

    @pytest.mark.asyncio
    async def test_template_script_multiple_articles(self):
        from routes.audio_digests import _generate_combined_script

        context = {
            "source_packets": [
                {
                    "pmid": "111", "title": "Article A",
                    "citation_metadata": {"journal": "J1", "pub_date": "2026"},
                    "study_type": "RCT",
                    "key_findings": ["Finding 1"],
                    "limitations": ["Lim 1"],
                    "evidence_anchors": [],
                    "grounding_level": "abstract_only",
                },
                {
                    "pmid": "222", "title": "Article B",
                    "citation_metadata": {"journal": "J2", "pub_date": "2025"},
                    "study_type": "Cohort",
                    "key_findings": ["Finding 2"],
                    "limitations": ["Lim 2"],
                    "evidence_anchors": [],
                    "grounding_level": "abstract_only",
                },
            ],
            "overall_grounding_level": "abstract_only",
            "article_texts": "...",
        }

        with patch("utils.copilot_provider.create_copilot_provider", side_effect=ImportError("mock")):
            script = await _generate_combined_script(context)

        assert "2 articles" in script
        assert "Article A" in script
        assert "Article B" in script


# ---------------------------------------------------------------------------
# C) Max 5 PMIDs enforcement
# ---------------------------------------------------------------------------

class TestMaxPmidsEnforcement:

    @pytest.mark.asyncio
    async def test_more_than_5_rejected(self):
        """_create_combined_summary must reject > 5 PMIDs."""
        from routes.audio_digests import _create_combined_summary
        from fastapi import HTTPException
        import routes.audio_digests as mod

        mod.db = AsyncMock()

        with patch.dict(os.environ, {"ENABLE_LIBRARY_COMBINED_AUDIO_SUMMARY": "true"}):
            with pytest.raises(HTTPException) as exc_info:
                await _create_combined_summary(
                    user_id="u1",
                    valid_pmids=["1", "2", "3", "4", "5", "6"],
                    title=None,
                    flags={},
                )
            assert exc_info.value.status_code == 400
            assert "too_many_articles" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# D) Playlist backward compatibility
# ---------------------------------------------------------------------------

class TestPlaylistBackwardCompat:

    def test_default_mode_is_playlist(self):
        """Existing callers that don't send mode get playlist."""
        from routes.audio_digests import AudioDigestCreate
        model = AudioDigestCreate(pmids=["123", "456"])
        assert model.mode == "playlist"
        assert model.auto_generate_missing is True

    def test_existing_fields_unchanged(self):
        from routes.audio_digests import AudioDigestCreate
        model = AudioDigestCreate(
            pmids=["a", "b"],
            title="Test",
            auto_generate_missing=False,
        )
        assert model.pmids == ["a", "b"]
        assert model.title == "Test"
        assert model.auto_generate_missing is False
        assert model.mode == "playlist"


# ---------------------------------------------------------------------------
# E) Feature flag gating
# ---------------------------------------------------------------------------

class TestCombinedSummaryFeatureFlag:

    @pytest.mark.asyncio
    async def test_flag_off_rejects(self):
        from routes.audio_digests import _create_combined_summary
        from fastapi import HTTPException
        import routes.audio_digests as mod
        mod.db = AsyncMock()

        with patch.dict(os.environ, {"ENABLE_LIBRARY_COMBINED_AUDIO_SUMMARY": "false"}):
            with pytest.raises(HTTPException) as exc_info:
                await _create_combined_summary(
                    user_id="u1",
                    valid_pmids=["1"],
                    title=None,
                    flags={},
                )
            assert exc_info.value.status_code == 403
            assert "feature_disabled" in str(exc_info.value.detail)
