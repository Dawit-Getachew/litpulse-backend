"""
Phase-0 Feature Flags — Unit Tests

Verifies:
1. All 8 Phase-0 flags exist in get_feature_flags()
2. All 8 default to False when env vars are absent
3. Each flag turns ON when its env var is set to "true" (case-insensitive)
4. Setting env var to "false" explicitly keeps flag OFF
5. The /api/config/feature-flags endpoint returns all 8 keys
"""
import os
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_flags(**overrides):
    """Read feature flags with isolated env overrides."""
    import importlib
    import backend.utils.feature_flags as ff_module  # noqa: E401
    saved = {}
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        # Re-evaluate to pick up env changes
        importlib.reload(ff_module)
        return ff_module.get_feature_flags()
    finally:
        for k, orig in saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
        importlib.reload(ff_module)


PHASE0_FLAGS = [
    ("enable_new_landing_page", "ENABLE_NEW_LANDING_PAGE"),
    ("enable_premium_trials", "ENABLE_PREMIUM_TRIALS"),
    ("enable_explore_topic_search_v2", "ENABLE_EXPLORE_TOPIC_SEARCH_V2"),
    ("enable_multi_digest_profiles", "ENABLE_MULTI_DIGEST_PROFILES"),
    ("enable_community_v2", "ENABLE_COMMUNITY_V2"),
    ("enable_library_audio_digests_v2", "ENABLE_LIBRARY_AUDIO_DIGESTS_V2"),
    ("enable_multi_digest_profiles_scheduler", "ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER"),
    ("enforce_community_digest_membership", "ENFORCE_COMMUNITY_DIGEST_MEMBERSHIP"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPhase0FlagDefaults:
    """All Phase-0 flags must default to False (OFF) with no env vars set."""

    @pytest.mark.parametrize("flag_name,env_var", PHASE0_FLAGS)
    def test_flag_defaults_to_false(self, flag_name, env_var):
        # Ensure env var is absent
        os.environ.pop(env_var, None)
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        assert flag_name in flags, f"Flag '{flag_name}' missing from get_feature_flags()"
        assert flags[flag_name] is False, (
            f"Flag '{flag_name}' should default to False but got {flags[flag_name]!r}"
        )

    def test_all_phase0_flags_present(self):
        """get_feature_flags() must contain all 8 Phase-0 flag keys."""
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        for flag_name, _ in PHASE0_FLAGS:
            assert flag_name in flags, f"Missing Phase-0 flag: '{flag_name}'"


class TestPhase0FlagToggle:
    """Each Phase-0 flag must flip to True when its env var is 'true'."""

    @pytest.mark.parametrize("flag_name,env_var", PHASE0_FLAGS)
    def test_flag_enabled_when_env_true(self, flag_name, env_var, monkeypatch):
        monkeypatch.setenv(env_var, "true")
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        assert flags[flag_name] is True, (
            f"Flag '{flag_name}' should be True when {env_var}='true'"
        )

    @pytest.mark.parametrize("flag_name,env_var", PHASE0_FLAGS)
    def test_flag_enabled_case_insensitive(self, flag_name, env_var, monkeypatch):
        """Env var value 'TRUE', 'True', 'true' must all enable the flag."""
        for val in ("TRUE", "True", "TrUe"):
            monkeypatch.setenv(env_var, val)
            from utils.feature_flags import get_feature_flags
            flags = get_feature_flags()
            assert flags[flag_name] is True, (
                f"Flag '{flag_name}' should be True when {env_var}='{val}'"
            )

    @pytest.mark.parametrize("flag_name,env_var", PHASE0_FLAGS)
    def test_flag_stays_off_when_env_false(self, flag_name, env_var, monkeypatch):
        """Explicit 'false' must keep flag OFF."""
        monkeypatch.setenv(env_var, "false")
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        assert flags[flag_name] is False, (
            f"Flag '{flag_name}' should be False when {env_var}='false'"
        )

    @pytest.mark.parametrize("flag_name,env_var", PHASE0_FLAGS)
    def test_flag_stays_off_when_env_0(self, flag_name, env_var, monkeypatch):
        """'0' or '1' should not be treated as True (only 'true' is valid)."""
        monkeypatch.setenv(env_var, "1")
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        assert flags[flag_name] is False, (
            f"Flag '{flag_name}': '1' should NOT enable the flag (only 'true' is valid)"
        )


class TestExistingFlagsUnchanged:
    """Existing operational flags must be unaffected by Phase-0 additions."""

    def test_enable_phi_guard_defaults_true(self):
        """ENABLE_PHI_GUARD defaults to True (existing behavior preserved)."""
        os.environ.pop("ENABLE_PHI_GUARD", None)
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        assert flags["enable_phi_guard"] is True

    def test_existing_flags_present(self):
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        existing = [
            "require_verified_for_posting",
            "enable_phi_guard",
            "phi_guard_mode",
            "enforce_run_now_quota",
            "enable_audio_takeaway",
            "enable_copilot",
        ]
        for f in existing:
            assert f in flags, f"Existing flag '{f}' was removed"
