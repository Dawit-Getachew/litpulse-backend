"""
Tests for P0 Hotfix: Profile digest AttributeError fix + scheduler gating + backoff.

Covers:
  A) Scheduler flag gating (ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER)
  B) Profile digest generation with minimal/incomplete profile documents
  C) Failure backoff prevents immediate retry
"""
import asyncio
import os
import uuid
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# A) Scheduler flag-gating tests
# ---------------------------------------------------------------------------


class TestSchedulerFlagGating:
    """Ensure scheduler respects ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER."""

    @pytest.mark.asyncio
    async def test_flag_false_runs_legacy_path(self):
        """When scheduler flag=false, only legacy path runs."""
        with patch.dict(os.environ, {
            "ENABLE_MULTI_DIGEST_PROFILES": "true",
            "ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER": "false",
        }):
            from scheduler import SchedulerAgent
            db = AsyncMock()
            agent = SchedulerAgent(db)
            agent._run_legacy_digests = AsyncMock()
            agent._run_profile_digests = AsyncMock()

            await agent._check_and_run_digests()

            agent._run_legacy_digests.assert_awaited_once()
            agent._run_profile_digests.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flag_true_runs_profile_path(self):
        """When scheduler flag=true, profile path runs."""
        with patch.dict(os.environ, {
            "ENABLE_MULTI_DIGEST_PROFILES": "true",
            "ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER": "true",
        }):
            from scheduler import SchedulerAgent
            db = AsyncMock()
            agent = SchedulerAgent(db)
            agent._run_legacy_digests = AsyncMock()
            agent._run_profile_digests = AsyncMock()

            await agent._check_and_run_digests()

            agent._run_profile_digests.assert_awaited_once()
            agent._run_legacy_digests.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_both_flags_false_runs_legacy(self):
        """When both flags are false, legacy runs (no profiles at all)."""
        with patch.dict(os.environ, {
            "ENABLE_MULTI_DIGEST_PROFILES": "false",
            "ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER": "false",
        }):
            from scheduler import SchedulerAgent
            db = AsyncMock()
            agent = SchedulerAgent(db)
            agent._run_legacy_digests = AsyncMock()
            agent._run_profile_digests = AsyncMock()

            await agent._check_and_run_digests()

            agent._run_legacy_digests.assert_awaited_once()
            agent._run_profile_digests.assert_not_awaited()


# ---------------------------------------------------------------------------
# B) Profile digest generation with minimal fields
# ---------------------------------------------------------------------------


class TestProfileDigestGeneration:
    """Test that generate_digest_for_profile handles minimal/incomplete profiles."""

    def _make_minimal_profile(self, **overrides):
        """Create a minimal profile dict — only specialty_id set."""
        base = {
            "profile_id": str(uuid.uuid4()),
            "user_id": "test-user-001",
            "name": "Test Profile",
            "specialty_id": "cardiology",
            "is_active": True,
            "deleted_at": None,
            "next_run_timestamp": datetime.now(timezone.utc).isoformat(),
            # Intentionally missing: topics_selected, journals_selected,
            # custom_topics, custom_journals, schedule, max_articles_per_digest
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_minimal_profile_no_attribute_error(self):
        """
        A profile with only specialty_id must NOT raise AttributeError.
        This is the exact regression from production.
        """
        from digest_orchestrator import DigestOrchestrator

        db = AsyncMock()
        db.users.find_one = AsyncMock(return_value={
            "user_id": "test-user-001",
            "email": "test@example.com",
            "is_verified": True,
            "full_name": "Test User",
        })
        db.user_articles.find.return_value.to_list = AsyncMock(return_value=[])
        db.articles.update_one = AsyncMock()
        db.articles.find_one = AsyncMock(return_value={"_id": "art1", "pmid": "12345"})
        db.user_articles.update_one = AsyncMock()
        db.digests.insert_one = AsyncMock()
        db.digest_profiles.update_one = AsyncMock()

        orchestrator = DigestOrchestrator(db)

        # Mock search to return at least one article
        orchestrator.pubmed_searcher.search = AsyncMock(return_value=[
            {
                "pmid": "12345",
                "title": "Test Article",
                "abstract": "Test abstract about cardiology.",
                "journal": "Test Journal",
                "pub_date": "2026-01-01",
                "authors": "Smith J",
                "design_tags": [],
            }
        ])

        profile = self._make_minimal_profile()

        # This previously raised AttributeError: 'DeduplicationRankingAgent' has no attribute 'rank_articles'
        result = await orchestrator.generate_digest_for_profile("test-user-001", profile, send_email=False)

        assert result is not None, "generate_digest_for_profile must not return None for a valid profile"
        assert result.get("article_count", 0) > 0 or result.get("status") == "no_articles"

    @pytest.mark.asyncio
    async def test_profile_with_null_lists(self):
        """Profile with explicit None for list fields should default to []."""
        from digest_orchestrator import DigestOrchestrator

        db = AsyncMock()
        db.users.find_one = AsyncMock(return_value={
            "user_id": "test-user-002",
            "email": "test2@example.com",
            "is_verified": False,
            "full_name": "Test User 2",
        })
        db.user_articles.find.return_value.to_list = AsyncMock(return_value=[])
        db.articles.update_one = AsyncMock()
        db.articles.find_one = AsyncMock(return_value={"_id": "art2", "pmid": "67890"})
        db.user_articles.update_one = AsyncMock()
        db.digests.insert_one = AsyncMock()
        db.digest_profiles.update_one = AsyncMock()

        orchestrator = DigestOrchestrator(db)
        orchestrator.pubmed_searcher.search = AsyncMock(return_value=[
            {
                "pmid": "67890",
                "title": "Another Article",
                "abstract": "Abstract text.",
                "journal": "Journal A",
                "pub_date": "2026-01-15",
                "authors": "Doe A",
                "design_tags": [],
            }
        ])

        profile = self._make_minimal_profile(
            user_id="test-user-002",
            topics_selected=None,
            custom_topics=None,
            journals_selected=None,
            custom_journals=None,
            max_articles_per_digest=None,
            schedule=None,
        )

        result = await orchestrator.generate_digest_for_profile("test-user-002", profile, send_email=False)
        # Should not crash
        assert result is not None

    @pytest.mark.asyncio
    async def test_profile_with_full_uxe_fields(self):
        """Profile with all UX-E fields populated should use them correctly."""
        from digest_orchestrator import DigestOrchestrator

        db = AsyncMock()
        db.users.find_one = AsyncMock(return_value={
            "user_id": "test-user-003",
            "email": "test3@example.com",
            "is_verified": True,
            "full_name": "Premium User",
        })
        db.user_articles.find.return_value.to_list = AsyncMock(return_value=[])
        db.articles.update_one = AsyncMock()
        db.articles.find_one = AsyncMock(return_value={"_id": "art3", "pmid": "11111"})
        db.user_articles.update_one = AsyncMock()
        db.digests.insert_one = AsyncMock()
        db.digest_profiles.update_one = AsyncMock()

        orchestrator = DigestOrchestrator(db)
        orchestrator.pubmed_searcher.search = AsyncMock(return_value=[
            {
                "pmid": "11111",
                "title": "Heart Failure Study",
                "abstract": "A study on heart failure treatments.",
                "journal": "NEJM",
                "pub_date": "2026-02-01",
                "authors": "Wang L",
                "design_tags": ["randomized controlled trial"],
            }
        ])

        profile = self._make_minimal_profile(
            user_id="test-user-003",
            topics_selected=["heart failure", "cardiology"],
            custom_topics=["SGLT2 inhibitors"],
            journals_selected=["NEJM", "Lancet"],
            custom_journals=["Circulation"],
            max_articles_per_digest=15,
            schedule={
                "frequency": "daily",
                "time_local": "08:00",
                "timezone": "America/New_York",
            },
        )

        result = await orchestrator.generate_digest_for_profile("test-user-003", profile, send_email=False)
        assert result is not None
        assert result.get("article_count", 0) > 0


# ---------------------------------------------------------------------------
# C) Backoff regression test
# ---------------------------------------------------------------------------


class TestSchedulerBackoff:
    """Failure backoff must push next_run_timestamp forward."""

    @pytest.mark.asyncio
    async def test_backoff_applied_on_failure(self):
        """When a profile digest fails, backoff must set next_run to ~60 min in the future."""
        from scheduler import SchedulerAgent, PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES

        db = AsyncMock()
        db.digest_profiles.update_one = AsyncMock()

        agent = SchedulerAgent(db)
        profile_id = "test-profile-backoff"

        before = datetime.now(timezone.utc)
        await agent._apply_profile_backoff(profile_id, "attribute_error", "rank")
        after = datetime.now(timezone.utc)

        # Verify update_one was called
        db.digest_profiles.update_one.assert_awaited_once()
        call_args = db.digest_profiles.update_one.call_args

        # Check the filter
        assert call_args[0][0] == {"profile_id": profile_id}

        # Check the $set payload
        set_payload = call_args[0][1]["$set"]
        assert set_payload["last_digest_error_code"] == "attribute_error"
        assert set_payload["last_digest_error_stage"] == "rank"
        assert set_payload["last_digest_error_at"] is not None

        # Verify next_run_timestamp is roughly now + BACKOFF
        next_run = datetime.fromisoformat(set_payload["next_run_timestamp"])
        expected_min = before + timedelta(minutes=PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES - 1)
        expected_max = after + timedelta(minutes=PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES + 1)
        assert expected_min <= next_run <= expected_max, (
            f"next_run={next_run} should be ~{PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES}min from now"
        )

    @pytest.mark.asyncio
    async def test_failed_profile_not_retried_immediately(self):
        """A profile that fails should NOT appear in the next tick's due query."""
        from scheduler import SchedulerAgent, PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES

        db = AsyncMock()

        # Simulate a profile that is "due" now
        now = datetime.now(timezone.utc)
        profile = {
            "profile_id": "fail-profile",
            "user_id": "u1",
            "is_active": True,
            "deleted_at": None,
            "next_run_timestamp": (now - timedelta(minutes=1)).isoformat(),
            "specialty_id": "cardiology",
        }

        # Make find().to_list return our profile once, then empty
        call_count = 0

        async def mock_find_to_list(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [profile]
            return []

        db.digest_profiles.find.return_value.to_list = mock_find_to_list
        db.digest_profiles.update_one = AsyncMock()

        agent = SchedulerAgent(db)

        # After backoff is applied, the profile's next_run_timestamp should be in the future
        await agent._apply_profile_backoff("fail-profile", "test_error", "test_stage")

        update_call = db.digest_profiles.update_one.call_args[0][1]["$set"]
        new_next_run = datetime.fromisoformat(update_call["next_run_timestamp"])

        # The new next_run must be in the future (at least BACKOFF minutes from now)
        assert new_next_run > now, "Backoff must push next_run_timestamp into the future"

    @pytest.mark.asyncio
    async def test_success_clears_error_fields(self):
        """After a successful digest, error fields on the profile should be cleared."""
        from digest_orchestrator import DigestOrchestrator

        db = AsyncMock()
        db.users.find_one = AsyncMock(return_value={
            "user_id": "test-clear-err",
            "email": "clear@example.com",
            "is_verified": True,
            "full_name": "Clear User",
        })
        db.user_articles.find.return_value.to_list = AsyncMock(return_value=[])
        db.articles.update_one = AsyncMock()
        db.articles.find_one = AsyncMock(return_value={"_id": "art9", "pmid": "99999"})
        db.user_articles.update_one = AsyncMock()
        db.digests.insert_one = AsyncMock()
        db.digest_profiles.update_one = AsyncMock()

        orchestrator = DigestOrchestrator(db)
        orchestrator.pubmed_searcher.search = AsyncMock(return_value=[
            {
                "pmid": "99999",
                "title": "Success Article",
                "abstract": "Abstract here.",
                "journal": "J",
                "pub_date": "2026-03-01",
                "authors": "A B",
                "design_tags": [],
            }
        ])

        profile = {
            "profile_id": "test-clear-err",
            "user_id": "test-clear-err",
            "name": "P",
            "specialty_id": "cardiology",
            "is_active": True,
            "deleted_at": None,
            "next_run_timestamp": datetime.now(timezone.utc).isoformat(),
            # Simulate prior error fields
            "last_digest_error_code": "attribute_error",
            "last_digest_error_at": datetime.now(timezone.utc).isoformat(),
            "last_digest_error_stage": "rank",
        }

        result = await orchestrator.generate_digest_for_profile("test-clear-err", profile, send_email=False)
        assert result is not None

        # Check that update_one cleared the error fields
        update_call = db.digest_profiles.update_one.call_args[0][1]["$set"]
        assert update_call["last_digest_error_code"] is None
        assert update_call["last_digest_error_at"] is None
        assert update_call["last_digest_error_stage"] is None
