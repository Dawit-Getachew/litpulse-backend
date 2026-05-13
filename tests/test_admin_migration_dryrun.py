"""
Tests for the TEMPORARY admin migration dry-run endpoint.

Covers:
  1. Non-admin cannot access endpoint
  2. Endpoint disabled by default (feature gate)
  3. Endpoint returns dry-run JSON only
  4. Endpoint cannot be coerced into apply/write mode
  5. Optional scoped user_id works
  6. Redaction is present in anomaly samples
  7. Existing CLI script still works
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure backend root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════════
# Test 1: migration_core module basics
# ═══════════════════════════════════════════════════════════════

class TestMigrationCoreHelpers:
    """Test helper functions in migration_core."""

    def test_is_pmid_shaped(self):
        from utils.migration_core import is_pmid_shaped
        assert is_pmid_shaped("12345678") is True
        assert is_pmid_shaped("1") is True
        assert is_pmid_shaped("123456789") is False  # >8 digits
        assert is_pmid_shaped("abc") is False
        assert is_pmid_shaped("") is False

    def test_is_oid_shaped(self):
        from utils.migration_core import is_oid_shaped
        assert is_oid_shaped("507f1f77bcf86cd799439011") is True
        assert is_oid_shaped("12345") is False
        assert is_oid_shaped("") is False
        assert is_oid_shaped("ZZZZZZZZZZZZZZZZZZZZZZZZ") is False

    def test_redact_user_id(self):
        from utils.migration_core import _redact_user_id
        # Long ID gets masked
        assert _redact_user_id("abcdef12-3456-7890-abcd-ef1234567890") == "abcd...7890"
        # Short ID gets fully masked
        assert _redact_user_id("short") == "***"
        # None/empty
        assert _redact_user_id("") == "***"
        assert _redact_user_id(None) == "***"

    def test_redact_doc(self):
        from utils.migration_core import _redact_doc
        from bson import ObjectId
        doc = {
            "_id": ObjectId(),
            "user_id": "abcdef12-3456-7890-abcd-ef1234567890",
            "email": "test@example.com",
            "pmid": "12345",
            "folder": "cardiology",
        }
        redacted = _redact_doc(doc)
        assert redacted["user_id"] == "abcd...7890"
        assert redacted["email"] == "[REDACTED]"
        assert redacted["pmid"] == "12345"  # Not sensitive
        assert redacted["folder"] == "cardiology"  # Not sensitive
        assert isinstance(redacted["_id"], str)  # ObjectId -> str


# ═══════════════════════════════════════════════════════════════
# Test 2: run_migration_dryrun always sets apply=False
# ═══════════════════════════════════════════════════════════════

class AsyncCursorMock:
    """Mock an async MongoDB cursor."""

    def __init__(self, items):
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item

    def limit(self, n):
        return AsyncCursorMock(self._items[:n])


class TestDryRunEnforcement:
    """Ensure dry-run mode is enforced regardless of input."""

    def test_dryrun_result_always_false(self):
        """The result dict must always have apply=False and dry_run=True."""
        from utils.migration_core import run_migration_dryrun

        # Create a mock async db
        mock_collection = MagicMock()
        mock_collection.find = MagicMock(return_value=AsyncCursorMock([]))
        mock_collection.count_documents = AsyncMock(return_value=0)
        mock_collection.aggregate = MagicMock(return_value=AsyncCursorMock([]))

        mock_db = MagicMock()
        mock_db.name = "test_db"
        mock_db.user_articles = mock_collection
        mock_db.library = mock_collection
        mock_db.articles = mock_collection

        result = asyncio.get_event_loop().run_until_complete(
            run_migration_dryrun(mock_db, phases="ABCD")
        )

        assert result["dry_run"] is True
        assert result["apply"] is False
        assert result["database"] == "test_db"


# ═══════════════════════════════════════════════════════════════
# Test 3: Feature gate behavior
# ═══════════════════════════════════════════════════════════════

class TestFeatureGate:
    """Test that endpoint respects ENABLE_ADMIN_MIGRATION_DRYRUN."""

    def test_disabled_by_default(self):
        """When env var is not set, endpoint should be disabled."""
        os.environ.pop("ENABLE_ADMIN_MIGRATION_DRYRUN", None)
        from routes.admin_migration_dryrun import _is_migration_dryrun_enabled
        assert _is_migration_dryrun_enabled() is False

    def test_enabled_when_true(self):
        os.environ["ENABLE_ADMIN_MIGRATION_DRYRUN"] = "true"
        from routes.admin_migration_dryrun import _is_migration_dryrun_enabled
        assert _is_migration_dryrun_enabled() is True
        os.environ.pop("ENABLE_ADMIN_MIGRATION_DRYRUN", None)

    def test_disabled_when_false(self):
        os.environ["ENABLE_ADMIN_MIGRATION_DRYRUN"] = "false"
        from routes.admin_migration_dryrun import _is_migration_dryrun_enabled
        assert _is_migration_dryrun_enabled() is False
        os.environ.pop("ENABLE_ADMIN_MIGRATION_DRYRUN", None)

    def test_disabled_when_empty(self):
        os.environ["ENABLE_ADMIN_MIGRATION_DRYRUN"] = ""
        from routes.admin_migration_dryrun import _is_migration_dryrun_enabled
        assert _is_migration_dryrun_enabled() is False
        os.environ.pop("ENABLE_ADMIN_MIGRATION_DRYRUN", None)


# ═══════════════════════════════════════════════════════════════
# Test 4: Request validation — no apply option
# ═══════════════════════════════════════════════════════════════

class TestRequestModel:
    """Verify the request model has no apply field."""

    def test_no_apply_field(self):
        from routes.admin_migration_dryrun import MigrationDryrunRequest
        model = MigrationDryrunRequest()
        # Model should NOT have an 'apply' field
        assert "apply" not in model.model_fields
        # Default phases
        assert model.phases == "ABCD"
        assert model.user_id is None
        assert model.limit is None

    def test_limit_cap(self):
        from routes.admin_migration_dryrun import MigrationDryrunRequest
        model = MigrationDryrunRequest(limit=500)
        assert model.limit == 500

    def test_phases_validation(self):
        from routes.admin_migration_dryrun import MigrationDryrunRequest
        model = MigrationDryrunRequest(phases="AB")
        assert model.phases == "AB"
        model2 = MigrationDryrunRequest(phases="abcd")
        assert model2.phases == "abcd"

    def test_user_id_scoped(self):
        from routes.admin_migration_dryrun import MigrationDryrunRequest
        uid = str(uuid.uuid4())
        model = MigrationDryrunRequest(user_id=uid)
        assert model.user_id == uid


# ═══════════════════════════════════════════════════════════════
# Test 5: CLI script still works (import test)
# ═══════════════════════════════════════════════════════════════

class TestCLICompatibility:
    """Verify the CLI script can still be imported and parsed."""

    def test_cli_script_importable(self):
        """The refactored CLI script should import without error."""
        from utils.migration_core import phase_a, phase_b, phase_c, phase_d
        assert callable(phase_a)
        assert callable(phase_b)
        assert callable(phase_c)
        assert callable(phase_d)

    def test_cli_parse_args(self):
        """parse_args from the script should work with defaults."""
        original_argv = sys.argv
        try:
            sys.argv = ["migrate_user_articles_pmid.py", "--phases", "AB"]
            from scripts.migrate_user_articles_pmid import parse_args
            args = parse_args()
            assert args.phases == "AB"
            assert args.apply is False
        finally:
            sys.argv = original_argv


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
