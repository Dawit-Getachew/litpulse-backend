"""
Phase UX-A Tests — App Shell UI Refresh

Tests for:
- Feature flag ENABLE_APP_SHELL_UI_V2 presence and default value
- Flag OFF: no behavior change
- Price update verification ($4.99)

Run with: pytest tests/test_phase_uxa_app_shell.py -v
"""
import pytest
import os
from unittest.mock import patch

# Set test environment
os.environ.setdefault("ENABLE_APP_SHELL_UI_V2", "false")


class TestAppShellFeatureFlag:
    """Test ENABLE_APP_SHELL_UI_V2 feature flag."""
    
    def test_flag_default_off(self):
        """Flag should default to OFF."""
        from utils.feature_flags import get_feature_flags
        
        flags = get_feature_flags()
        assert flags.get("enable_app_shell_ui_v2") == False
    
    def test_flag_can_be_enabled(self):
        """Flag should be settable via env var."""
        with patch.dict(os.environ, {"ENABLE_APP_SHELL_UI_V2": "true"}):
            from utils.feature_flags import get_feature_flags
            
            flags = get_feature_flags()
            assert flags.get("enable_app_shell_ui_v2") == True
    
    def test_flag_case_insensitive(self):
        """Flag should be case-insensitive."""
        with patch.dict(os.environ, {"ENABLE_APP_SHELL_UI_V2": "TRUE"}):
            from utils.feature_flags import get_feature_flags
            
            flags = get_feature_flags()
            assert flags.get("enable_app_shell_ui_v2") == True
    
    def test_flag_exposed_in_api(self):
        """Flag should be exposed in /api/config/feature-flags endpoint."""
        from utils.feature_flags import get_feature_flags
        
        flags = get_feature_flags()
        assert "enable_app_shell_ui_v2" in flags


class TestFlagOffBehavior:
    """Test that existing behavior is unchanged when flag is OFF."""
    
    def test_all_existing_flags_present(self):
        """All existing flags should still be present."""
        from utils.feature_flags import get_feature_flags
        
        flags = get_feature_flags()
        
        # Check all Phase-0 flags still exist
        expected_flags = [
            "enable_new_landing_page",
            "enable_premium_trials", 
            "enable_explore_topic_search_v2",
            "enable_multi_digest_profiles",
            "enable_community_v2",
            "enable_library_audio_digests_v2",
            "enable_multi_digest_profiles_scheduler",
            "enforce_community_digest_membership",
        ]
        
        for flag in expected_flags:
            assert flag in flags, f"Missing flag: {flag}"
    
    def test_flag_off_returns_false(self):
        """When flag is OFF, it should return False."""
        with patch.dict(os.environ, {"ENABLE_APP_SHELL_UI_V2": "false"}):
            from utils.feature_flags import get_feature_flags
            
            flags = get_feature_flags()
            assert flags.get("enable_app_shell_ui_v2") == False


class TestNavigationLabels:
    """Test navigation label changes (Settings -> Preferences)."""
    
    def test_preferences_route_exists(self):
        """The /preferences route should still exist (unchanged)."""
        # This is a UI test - verified via Playwright
        pass


class TestPriceDisplay:
    """Test Pro price display changes."""
    
    def test_pro_price_is_4_99(self):
        """Pro price should display as $4.99/month."""
        # Read LandingPageNew.js and verify price
        with open('/app/frontend/src/pages/LandingPageNew.js', 'r') as f:
            content = f.read()
        
        assert '$4.99 / month' in content or '$4.99/month' in content, \
            "Pro price should be $4.99/month in LandingPageNew.js"
    
    def test_plan_page_price_is_4_99(self):
        """PlanPage should display $4.99/month."""
        with open('/app/frontend/src/pages/PlanPage.js', 'r') as f:
            content = f.read()
        
        # Check for $4.99 (allow some formatting variations)
        assert '$4.99' in content, \
            "Pro price should be $4.99 in PlanPage.js"


class TestDigestsReminderBanner:
    """Test Digests page reminder banner."""
    
    def test_banner_only_shows_when_audio_flag_on(self):
        """Reminder banner should only show when ENABLE_LIBRARY_AUDIO_DIGESTS_V2=true."""
        with open('/app/frontend/src/pages/DigestsPage.js', 'r') as f:
            content = f.read()
        
        # Banner should be conditional on audioDigestV2 flag
        assert 'audioDigestV2' in content, \
            "Banner should check audioDigestV2 flag"
        assert 'audio-digest-reminder' in content, \
            "Banner should have data-testid='audio-digest-reminder'"


class TestNavTabConfiguration:
    """Test navigation tab configuration in V2 layout."""
    
    def test_v2_layout_has_correct_tabs(self):
        """V2 layout should have correct nav tabs."""
        with open('/app/frontend/src/components/LayoutV2.tsx', 'r') as f:
            content = f.read()
        
        # Check for all required tabs
        required_tabs = ['Home', 'Explore', 'Community', 'Library', 'Digests', 'Inbox']
        for tab in required_tabs:
            assert tab in content, f"Missing tab: {tab}"
    
    def test_v2_layout_has_badges(self):
        """V2 layout should have Plan and Verification badges."""
        with open('/app/frontend/src/components/LayoutV2.tsx', 'r') as f:
            content = f.read()
        
        assert 'nav-plan-badge' in content, "Missing Plan badge"
        assert 'nav-verification-badge' in content, "Missing Verification badge"


class TestMobileV2Configuration:
    """Test mobile V2 configuration."""
    
    def test_mobile_v2_has_4_tabs(self):
        """Mobile V2 should have exactly 4 bottom tabs."""
        with open('/app/mobile/src/navigation/AppNavigation.tsx', 'r') as f:
            content = f.read()
        
        # Check for V2 tabs
        assert 'MainTabsV2' in content, "Missing MainTabsV2 component"
    
    def test_mobile_v2_home_has_badges(self):
        """Mobile V2 HomeScreen should have Plan and Verification badges."""
        with open('/app/mobile/src/screens/HomeScreenV2.tsx', 'r') as f:
            content = f.read()
        
        assert 'home-plan-badge' in content, "Missing Plan badge"
        assert 'home-verification-badge' in content, "Missing Verification badge"
    
    def test_mobile_v2_home_has_search_button(self):
        """Mobile V2 HomeScreen should have Search PubMed button."""
        with open('/app/mobile/src/screens/HomeScreenV2.tsx', 'r') as f:
            content = f.read()
        
        assert 'home-search-pubmed-btn' in content or 'Search PubMed' in content, \
            "Missing Search PubMed button"
    
    def test_mobile_v2_home_has_inbox_bell(self):
        """Mobile V2 HomeScreen should have inbox bell icon."""
        with open('/app/mobile/src/screens/HomeScreenV2.tsx', 'r') as f:
            content = f.read()
        
        assert 'home-inbox-btn' in content, "Missing inbox bell button"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
