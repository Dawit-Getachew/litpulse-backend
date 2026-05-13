"""
Centralized Feature Flags for LitPulse.
Read from environment variables with safe defaults.

Phase-0 safety flags: all new flags default OFF so existing behavior
is reproduced exactly when no env vars are set.
"""
import os
from typing import Dict, Any


def _bool_flag(key: str, default: bool = False) -> bool:
    """Read an env var as a boolean. Missing / unset → default (False = safe)."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


def get_feature_flags() -> Dict[str, Any]:
    """Return current feature flag values. Called per-request (cheap, no caching needed)."""
    return {
        # ----------------------------------------------------------------
        # Existing operational flags
        # ----------------------------------------------------------------
        "require_verified_for_posting": _bool_flag("REQUIRE_VERIFIED_FOR_POSTING"),
        "enable_phi_guard": _bool_flag("ENABLE_PHI_GUARD", default=True),
        "phi_guard_mode": os.environ.get("PHI_GUARD_MODE", "block"),
        "enforce_run_now_quota": _bool_flag("ENFORCE_RUN_NOW_QUOTA"),
        "enable_audio_takeaway": _bool_flag("ENABLE_AUDIO_TAKEAWAY"),
        "enforce_audio_quota": _bool_flag("ENFORCE_AUDIO_QUOTA"),
        "audio_autogenerate_on_save": _bool_flag("AUDIO_AUTOGENERATE_ON_SAVE"),
        "enable_copilot": _bool_flag("ENABLE_COPILOT"),
        "enforce_copilot_quota": _bool_flag("ENFORCE_COPILOT_QUOTA"),
        "enable_messaging": False,
        "enable_premium_billing": False,
        "admin_email": os.environ.get("ADMIN_EMAIL", ""),

        # ----------------------------------------------------------------
        # Phase-0 core feature flags — all default OFF
        # ----------------------------------------------------------------
        # New landing page variant (replaces LandingPage.js when ON)
        "enable_new_landing_page": _bool_flag("ENABLE_NEW_LANDING_PAGE"),
        # 30-day premium trial flow (stub in Phase 0; real logic in Phase 2)
        "enable_premium_trials": _bool_flag("ENABLE_PREMIUM_TRIALS"),
        # Explore / topic search V2 (enhanced search UX)
        "enable_explore_topic_search_v2": _bool_flag("ENABLE_EXPLORE_TOPIC_SEARCH_V2"),
        # Multi-digest profile creation in preferences
        "enable_multi_digest_profiles": _bool_flag("ENABLE_MULTI_DIGEST_PROFILES"),
        # Community V2 enhancements (threaded improvements, gating)
        "enable_community_v2": _bool_flag("ENABLE_COMMUNITY_V2"),
        # Library audio digest tab V2 (enhanced audio UX)
        "enable_library_audio_digests_v2": _bool_flag("ENABLE_LIBRARY_AUDIO_DIGESTS_V2"),

        # ----------------------------------------------------------------
        # Phase-0 split flags — separate UI rollout from enforcement
        # ----------------------------------------------------------------
        # Scheduler-side multi-digest support (enable AFTER UI flag stable)
        "enable_multi_digest_profiles_scheduler": _bool_flag("ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER"),
        # Enforce community membership gating (enable AFTER community_v2 stable)
        "enforce_community_digest_membership": _bool_flag("ENFORCE_COMMUNITY_DIGEST_MEMBERSHIP"),

        # ----------------------------------------------------------------
        # Phase UX-A: App Shell UI Refresh
        # ----------------------------------------------------------------
        # New app shell with dark nav, tab layout, badges (enable for UI refresh)
        "enable_app_shell_ui_v2": _bool_flag("ENABLE_APP_SHELL_UI_V2"),

        # ----------------------------------------------------------------
        # Phase SEC-A: Email Verification Requirement
        # ----------------------------------------------------------------
        # Require email verification before app access (anti-bot)
        "require_email_verified_for_app_access": _bool_flag("REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS"),

        # ----------------------------------------------------------------
        # Phase UX-B: Explore Simple PubMed Search
        # ----------------------------------------------------------------
        # Minimal PubMed search UI without topic suggestions or date filters
        "enable_explore_simple_pubmed_ui": _bool_flag("ENABLE_EXPLORE_SIMPLE_PUBMED_UI"),

        # ----------------------------------------------------------------
        # Phase UX-C: Community Visibility + Subspecialty Limits
        # ----------------------------------------------------------------
        # Show only communities user is eligible to access (hide locked)
        "enable_community_visible_only_eligible": _bool_flag("ENABLE_COMMUNITY_VISIBLE_ONLY_ELIGIBLE"),
        # Allow subspecialty community selection (max 3 per specialty)
        "enable_community_subspecialty_selection": _bool_flag("ENABLE_COMMUNITY_SUBSPECIALTY_SELECTION"),

        # ----------------------------------------------------------------
        # Phase UX-D: Full Preferences Wizard per Digest Profile
        # ----------------------------------------------------------------
        # Enable full wizard (topics/journals/schedule/advanced) per digest profile
        "enable_digest_profile_full_wizard": _bool_flag("ENABLE_DIGEST_PROFILE_FULL_WIZARD"),

        # ----------------------------------------------------------------
        # Phase UX-E: Onboarding + Preferences Wizard V2
        # ----------------------------------------------------------------
        # New signup onboarding wizard flow
        "enable_onboarding_wizard_v2": _bool_flag("ENABLE_ONBOARDING_WIZARD_V2"),
        # Unified preferences/profile wizard for editing
        "enable_preferences_wizard_v2": _bool_flag("ENABLE_PREFERENCES_WIZARD_V2"),
        # Dual-write profile -> legacy preferences (for scheduler compatibility)
        "enable_preferences_dual_write": _bool_flag("ENABLE_PREFERENCES_DUAL_WRITE"),

        # ----------------------------------------------------------------
        # Audio + LitScholar Enhancement Flags
        # ----------------------------------------------------------------
        # Show "Audio Summary" CTA on digest article cards
        "enable_digest_article_audio_links": _bool_flag("ENABLE_DIGEST_ARTICLE_AUDIO_LINKS"),
        # Enable combined multi-article audio summary in Library
        "enable_library_combined_audio_summary": _bool_flag("ENABLE_LIBRARY_COMBINED_AUDIO_SUMMARY"),
        # Enable LitScholar (rebranded Copilot) UI and preset flows
        "enable_litscholar_v1": _bool_flag("ENABLE_LITSCHOLAR_V1"),
        # Enable LitScholar structured expertise profile memory
        "enable_litscholar_profile_memory": _bool_flag("ENABLE_LITSCHOLAR_PROFILE_MEMORY"),
        "enable_litscholar_langgraph_spike": _bool_flag("ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE"),
        "enable_pricing_page": _bool_flag("ENABLE_PRICING_PAGE"),
        "enable_starter_packs": _bool_flag("ENABLE_STARTER_PACKS"),
        "enable_saved_views": _bool_flag("ENABLE_SAVED_VIEWS"),
        "enable_article_notes": _bool_flag("ENABLE_ARTICLE_NOTES"),
        "enable_reading_goals": _bool_flag("ENABLE_READING_GOALS"),
        "enable_notification_prefs": _bool_flag("ENABLE_NOTIFICATION_PREFS"),
        "enable_navigation_v2": _bool_flag("ENABLE_NAVIGATION_V2"),

        # ----------------------------------------------------------------
        # Homepage V3 UI Refresh
        # ----------------------------------------------------------------
        "enable_home_ui_v3": _bool_flag("ENABLE_HOME_UI_V3"),

        # ----------------------------------------------------------------
        # NPI Verification
        # ----------------------------------------------------------------
        # Allow NPI self-attestation (provisional verification)
        "allow_npi_self_attestation": _bool_flag("ALLOW_NPI_SELF_ATTESTATION"),

        # Auto-start 30-day trial for existing users on login (one-time)
        "auto_start_trial_for_existing_users": _bool_flag("AUTO_START_TRIAL_FOR_EXISTING_USERS"),

        # ----------------------------------------------------------------
        # Beta Rollout
        # ----------------------------------------------------------------
        "enable_invite_only_beta": _bool_flag("ENABLE_INVITE_ONLY_BETA"),
        "beta_specialty_id": os.environ.get("BETA_SPECIALTY_ID", ""),

        # ----------------------------------------------------------------
        # Workspace Shell V1 — Task-first authenticated workspace
        # ----------------------------------------------------------------
        "enable_workspace_shell_v1": _bool_flag("ENABLE_WORKSPACE_SHELL_V1"),

        # ----------------------------------------------------------------
        # TEMPORARY — Stage 1A Migration Dry-Run Admin Endpoint
        # Remove after migration is complete and validated.
        # ----------------------------------------------------------------
        "enable_admin_migration_dryrun": _bool_flag("ENABLE_ADMIN_MIGRATION_DRYRUN"),
    }
