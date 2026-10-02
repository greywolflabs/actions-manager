"""
Tests for self-hosted beta enforcement limits.

Validates that:
- Caller Workflow (standard) projects are capped at 4
- Reusable Workflow (rwx) projects are capped at 2
- Secrets per project are capped at 6
- Environment variables per project are capped at 6
- GitHub deployment environments per project are capped at 6
- Error messages identify the specific beta limit reached

The cloud tier rules are tested under tests/cloud/.
"""

from tier_service import (
    check_project_type_limit,
    environment_limit,
    environment_limit_message,
    env_var_limit,
    env_var_limit_message,
    secrets_limit,
    secrets_limit_message,
    SELF_HOSTED_BETA_LIMITS,
)


class MockAccount:
    def __init__(self, **kwargs):
        self.account_type = kwargs.get("account_type", "free")


# ---------------------------------------------------------------------------
# check_project_type_limit
# ---------------------------------------------------------------------------

class TestCheckProjectTypeLimit:
    """Tests for per-type project limits in self-hosted beta mode."""

    def test_standard_project_within_limit(self):
        """Caller Workflow Project is allowed when below the limit."""
        allowed, msg = check_project_type_limit(
            current_standard_count=3,
            current_rwx_count=0,
            project_type="standard",
        )
        assert allowed is True
        assert msg is None

    def test_standard_project_at_limit(self):
        """Caller Workflow Project is blocked when at the limit (4)."""
        limit = SELF_HOSTED_BETA_LIMITS["standard_projects"]
        allowed, msg = check_project_type_limit(
            current_standard_count=limit,
            current_rwx_count=0,
            project_type="standard",
        )
        assert allowed is False
        assert "Caller Workflow Project" in msg
        assert str(limit) in msg
        assert "self-hosted beta" in msg.lower()

    def test_rwx_project_within_limit(self):
        """Reusable Workflow Project is allowed when below the limit."""
        allowed, msg = check_project_type_limit(
            current_standard_count=0,
            current_rwx_count=1,
            project_type="rwx",
        )
        assert allowed is True
        assert msg is None

    def test_rwx_project_at_limit(self):
        """Reusable Workflow Project is blocked when at the limit (2)."""
        limit = SELF_HOSTED_BETA_LIMITS["rwx_projects"]
        allowed, msg = check_project_type_limit(
            current_standard_count=0,
            current_rwx_count=limit,
            project_type="rwx",
        )
        assert allowed is False
        assert "Reusable Workflow Project" in msg
        assert str(limit) in msg
        assert "self-hosted beta" in msg.lower()

    def test_independent_type_counts(self):
        """Standard and rwx limits are enforced independently."""
        # Standard at limit, rwx still has room
        allowed_std, _ = check_project_type_limit(
            current_standard_count=SELF_HOSTED_BETA_LIMITS["standard_projects"],
            current_rwx_count=0,
            project_type="standard",
        )
        assert allowed_std is False

        allowed_rwx, _ = check_project_type_limit(
            current_standard_count=SELF_HOSTED_BETA_LIMITS["standard_projects"],
            current_rwx_count=0,
            project_type="rwx",
        )
        assert allowed_rwx is True


# ---------------------------------------------------------------------------
# secrets_limit
# ---------------------------------------------------------------------------

class TestSecretsLimitBeta:
    """Tests for the self-hosted beta secrets limit (6 per project)."""

    def test_beta_secret_limit_applies_to_every_account(self):
        limit = SELF_HOSTED_BETA_LIMITS["secrets_per_project"]
        for account_type in ("free", "professional", "enterprise"):
            assert secrets_limit(MockAccount(account_type=account_type)) == limit

    def test_exceeds_beta_secret_limit_message(self):
        account = MockAccount()
        limit = SELF_HOSTED_BETA_LIMITS["secrets_per_project"]
        msg = secrets_limit_message(account, limit, limit, 1)
        assert str(limit) in msg
        assert "self-hosted beta" in msg.lower()


# ---------------------------------------------------------------------------
# env_var_limit
# ---------------------------------------------------------------------------

class TestEnvVarLimit:
    """Tests for the self-hosted beta env var limit (6 per project)."""

    def test_beta_env_var_limit_applies_without_an_account(self):
        # Counted for every caller, even when the account row is missing.
        assert env_var_limit(None) == SELF_HOSTED_BETA_LIMITS["env_vars_per_project"]
        assert env_var_limit(MockAccount(account_type="enterprise")) == SELF_HOSTED_BETA_LIMITS["env_vars_per_project"]

    def test_exceeds_beta_env_var_limit_message(self):
        limit = SELF_HOSTED_BETA_LIMITS["env_vars_per_project"]
        msg = env_var_limit_message(limit, limit, 1)
        assert str(limit) in msg
        assert "self-hosted beta" in msg.lower()


# ---------------------------------------------------------------------------
# environment_limit
# ---------------------------------------------------------------------------

class TestEnvironmentLimit:
    """Tests for the self-hosted beta GitHub environments limit (6 per project)."""

    def test_beta_environment_limit_applies_without_an_account(self):
        assert environment_limit(None) == SELF_HOSTED_BETA_LIMITS["environments_per_project"]
        assert environment_limit(MockAccount(account_type="enterprise")) == SELF_HOSTED_BETA_LIMITS["environments_per_project"]

    def test_exceeds_beta_environment_limit_message(self):
        limit = SELF_HOSTED_BETA_LIMITS["environments_per_project"]
        msg = environment_limit_message(limit, limit)
        assert str(limit) in msg
        assert "self-hosted beta" in msg.lower()
