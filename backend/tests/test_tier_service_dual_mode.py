"""
Unit tests for tier_service.py's self-hosted tier rules.

Self-hosted mode: license-based tier plus the self-hosted beta limits. The
cloud rules (Marketplace plan, admin override, per-tier limits) are tested in
tests/cloud/test_tier_service_cloud_mode.py.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from tier_service import (
    get_effective_tier,
    check_new_project,
    check_private_repo_access,
    check_repo_limit,
    secrets_limit,
    secrets_limit_message,
    normalize_tier_name,
    REPOS_PER_PROJECT,
    SELF_HOSTED_BETA_LIMITS,
)


class MockAccount:
    """Mock Account object for testing"""
    def __init__(self, **kwargs):
        self.admin_override = kwargs.get('admin_override', False)
        self.admin_override_until = kwargs.get('admin_override_until', None)
        self.account_type = kwargs.get('account_type', 'free')


class TestSelfHostedMode:
    """Test tier_service in self-hosted mode with license keys"""
    
    def test_self_hosted_free_tier_no_license(self, monkeypatch):
        """Test self-hosted mode with no license defaults to free tier"""
        # Mock license.get_installation_tier to return free
        with patch('tier_service.license.get_installation_tier', return_value='free'):
            account = MockAccount(account_type='free')
            tier = get_effective_tier(account)
            assert tier == 'free'
    
    def test_self_hosted_professional_tier(self, monkeypatch):
        """Test self-hosted mode with professional license"""
        with patch('tier_service.license.get_installation_tier', return_value='professional'):
            account = MockAccount(account_type='free')
            tier = get_effective_tier(account)
            assert tier == 'professional'
    
    def test_self_hosted_enterprise_tier(self, monkeypatch):
        """Test self-hosted mode with enterprise license"""
        with patch('tier_service.license.get_installation_tier', return_value='enterprise'):
            account = MockAccount(account_type='free')
            tier = get_effective_tier(account)
            assert tier == 'enterprise'
    
    def test_self_hosted_tier_ignores_admin_override_columns(self, monkeypatch):
        """The admin override is a cloud feature (#2137): only cloud/admin.py can
        set one, so the self-hosted tier is always the license tier."""
        with patch('tier_service.license.get_installation_tier', return_value='free'):
            account = MockAccount(
                admin_override=True,
                admin_override_until=None,
                account_type='enterprise'
            )
            tier = get_effective_tier(account)
            assert tier == 'free'
    
    def test_self_hosted_expired_admin_override_uses_license(self, monkeypatch):
        """Test that expired admin override falls back to license tier"""
        past_date = datetime.now(timezone.utc) - timedelta(days=1)
        with patch('tier_service.license.get_installation_tier', return_value='professional'):
            account = MockAccount(
                admin_override=True,
                admin_override_until=past_date,  # Expired
                account_type='enterprise'
            )
            tier = get_effective_tier(account)
            assert tier == 'professional'
    
    def test_self_hosted_project_limits(self, monkeypatch):
        """Test project limits in self-hosted beta mode.

        Each project type has its own beta limit
        (SELF_HOSTED_BETA_LIMITS['standard_projects'] / ['rwx_projects']),
        regardless of the license tier.
        """
        standard_limit = SELF_HOSTED_BETA_LIMITS["standard_projects"]

        # Below the beta limit → allowed
        with patch('tier_service.license.get_installation_tier', return_value='free'):
            account = MockAccount()
            allowed, error = check_new_project(account, standard_limit - 1, standard_limit - 1, 0, "standard")
            assert allowed is True
            assert error is None

        # At the beta limit → blocked
        with patch('tier_service.license.get_installation_tier', return_value='free'):
            account = MockAccount()
            allowed, error = check_new_project(account, standard_limit, standard_limit, 0, "standard")
            assert allowed is False
            assert "self-hosted beta" in error.lower()

        # License tier does not override beta limits in self-hosted mode
        with patch('tier_service.license.get_installation_tier', return_value='enterprise'):
            account = MockAccount()
            allowed, error = check_new_project(account, standard_limit, standard_limit, 0, "standard")
            assert allowed is False
            assert "self-hosted beta" in error.lower()
    
    def test_self_hosted_private_repo_access(self, monkeypatch):
        """Private repo access is part of the core product on every tier."""
        # Free tier: private repos are available (workflow-first model)
        with patch('tier_service.license.get_installation_tier', return_value='free'):
            account = MockAccount()
            allowed, error = check_private_repo_access(account)
            assert allowed is True
            assert error is None

        # Professional tier: also has private repo access
        with patch('tier_service.license.get_installation_tier', return_value='professional'):
            account = MockAccount()
            allowed, error = check_private_repo_access(account)
            assert allowed is True
            assert error is None

    def test_self_hosted_repo_limits(self, monkeypatch):
        """Test repo limits in self-hosted mode"""
        # Free tier: 10 repos per project
        with patch('tier_service.license.get_installation_tier', return_value='free'):
            account = MockAccount()
            allowed, error = check_repo_limit(account, 11)
            assert allowed is False
            assert "10" in error

        # Professional tier: 50 repos per project
        with patch('tier_service.license.get_installation_tier', return_value='professional'):
            account = MockAccount()
            allowed, error = check_repo_limit(account, 40)
            assert allowed is True
            assert error is None

    @pytest.mark.parametrize("tier", ["free", "professional"])
    def test_self_hosted_repo_limit_offers_no_paid_plan(self, tier):
        """#2193: the self-hosted beta sells no plan, so the denial must not offer one."""
        limit = REPOS_PER_PROJECT[tier]
        with patch('tier_service.license.get_installation_tier', return_value=tier):
            allowed, error = check_repo_limit(MockAccount(), limit + 1)

        assert allowed is False
        assert f"up to {limit} repositories per project" in error
        assert "Paid plans are not available during the self-hosted beta." in error
        for word in ("Upgrade", "Professional", "Enterprise"):
            assert word not in error

    def test_self_hosted_secrets_limits(self, monkeypatch):
        """Test secrets limits in self-hosted beta mode.

        In self-hosted beta the limit is always SELF_HOSTED_BETA_LIMITS['secrets_per_project']
        regardless of the license tier.
        """
        beta_secret_limit = SELF_HOSTED_BETA_LIMITS["secrets_per_project"]

        for license_tier in ('free', 'enterprise'):
            with patch('tier_service.license.get_installation_tier', return_value=license_tier):
                account = MockAccount()
                # Within beta limit → allowed; one more → blocked
                assert secrets_limit(account) == beta_secret_limit
                error = secrets_limit_message(account, beta_secret_limit, beta_secret_limit, 1)
                assert "self-hosted beta" in error.lower()
                assert str(beta_secret_limit) in error


class TestTierHelpers:
    """Test tier helper functions"""
    
    def test_normalize_tier_name(self):
        """Test tier name normalization"""
        assert normalize_tier_name('professional') == 'professional'
        assert normalize_tier_name('pro') == 'professional'
        assert normalize_tier_name('PRO') == 'professional'
        assert normalize_tier_name('enterprise') == 'enterprise'
        assert normalize_tier_name('ENTERPRISE') == 'enterprise'
        assert normalize_tier_name('free') == 'free'
        assert normalize_tier_name('') == 'free'
        assert normalize_tier_name(None) == 'free'
        assert normalize_tier_name('unknown') == 'free'


class TestSelfHostedIgnoresCloudTierLimits:
    """Self-hosted enforces its own fixed limits, whatever the license tier.

    The cloud half of this comparison is in
    tests/cloud/test_tier_service_cloud_mode.py.
    """

    def test_professional_license_within_beta_limits(self):
        sh_account = MockAccount(account_type='professional')
        with patch('tier_service.license.get_installation_tier', return_value='professional'):
            sh_private_check = check_private_repo_access(sh_account)
            sh_secret_limit = secrets_limit(sh_account)

        # Private repo access is always True
        assert sh_private_check == (True, None)
        # Beta secret limit is 6; exactly at the limit is allowed
        assert sh_secret_limit == 6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
