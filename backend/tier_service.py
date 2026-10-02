"""
Entitlements: what an account may do in ActionsManager, and its limits.

Core ships the self-hosted rules: the installation's license tier plus the
self-hosted beta limits. The cloud entrypoint installs its own rules
(cloud.entitlements) over them at startup with use(), so nothing here knows
which deployment it runs in. Callers use the module-level functions, which
answer from whichever rules are installed.
"""

from typing import Optional, Tuple
from models import Account
import license


# Self-hosted beta limits. These apply to every license tier.
SELF_HOSTED_BETA_LIMITS = {
    "standard_projects": 4,   # Caller Workflow Projects
    "rwx_projects": 2,        # Reusable Workflow Projects
    "secrets_per_project": 6,
    "env_vars_per_project": 6,
    "environments_per_project": 6,
    "run_history_days": 30,
}

# Repositories per project, by tier (None = unlimited). The one tier-based
# limit self-hosted enforces, by license tier; the cloud rules use it too.
REPOS_PER_PROJECT = {"free": 10, "professional": 50, "enterprise": None}


def normalize_tier_name(tier: Optional[str]) -> str:
    """
    Normalize tier name to standard values.
    
    Args:
        tier: Tier name (may be null, varied case, or "pro" vs "professional")
        
    Returns:
        Normalized tier: "free", "professional", or "enterprise"
    """
    if not tier:
        return "free"
    
    tier_lower = tier.lower()
    
    # Handle "pro" as alias for "professional"
    if tier_lower in ["pro", "professional"]:
        return "professional"
    
    # Handle enterprise
    if tier_lower == "enterprise":
        return "enterprise"
    
    # Default to free for unknown tiers
    return "free"


def check_project_type_limit(
    current_standard_count: int,
    current_rwx_count: int,
    project_type: str,
) -> Tuple[bool, Optional[str]]:
    """
    Check if a new project of the given type fits the self-hosted beta limits.

    Args:
        current_standard_count: Current number of standard (caller) projects.
        current_rwx_count: Current number of rwx (reusable workflow) projects.
        project_type: ``"standard"`` or ``"rwx"``.

    Returns:
        Tuple of (allowed: bool, error_message: Optional[str])
    """
    if project_type == "rwx":
        limit = SELF_HOSTED_BETA_LIMITS["rwx_projects"]
        if current_rwx_count >= limit:
            return False, (
                f"Self-hosted beta allows up to {limit} Reusable Workflow Project(s). "
                f"You have reached the beta limit for Reusable Workflow Projects. "
                f"Paid plans are not available during the self-hosted beta."
            )
    else:
        # "standard" and any unrecognised project type
        limit = SELF_HOSTED_BETA_LIMITS["standard_projects"]
        if current_standard_count >= limit:
            return False, (
                f"Self-hosted beta allows up to {limit} Caller Workflow Project(s). "
                f"You have reached the beta limit for Caller Workflow Projects. "
                f"Paid plans are not available during the self-hosted beta."
            )

    return True, None


class Entitlements:
    """The tier rules the functions below answer from.

    SelfHostedEntitlements is core's; cloud.entitlements.CloudEntitlements is
    the cloud's. A rule an implementation leaves out raises rather than
    quietly applying the other deployment's limit.

    A ``*_limit`` method returning ``None`` means the resource is not limited
    for that account, and the caller skips counting it.
    """

    def effective_tier(self, account: Account) -> str:
        raise NotImplementedError

    def run_history_days(self, account: Account) -> Optional[int]:
        raise NotImplementedError

    def check_new_project(
        self, account: Account, total_count: int, standard_count: int, rwx_count: int, project_type: str
    ) -> Tuple[bool, Optional[str]]:
        raise NotImplementedError

    def check_private_visibility_scope(self, account: Account) -> Tuple[bool, Optional[str]]:
        raise NotImplementedError

    def repo_limit_message(self, account: Account, limit: int) -> str:
        raise NotImplementedError

    def secrets_limit(self, account: Account) -> Optional[int]:
        raise NotImplementedError

    def secrets_limit_message(self, account: Account, limit: int, current: int, new: int) -> str:
        raise NotImplementedError

    def env_var_limit(self, account: Optional[Account]) -> Optional[int]:
        raise NotImplementedError

    def env_var_limit_message(self, limit: int, current: int, new: int) -> str:
        raise NotImplementedError

    def environment_limit(self, account: Optional[Account]) -> Optional[int]:
        raise NotImplementedError

    def environment_limit_message(self, limit: int, current: int) -> str:
        raise NotImplementedError

    def environment_sync_limit_message(self, limit: int, current: int, new: int) -> str:
        raise NotImplementedError

    def plan(self, account: Account) -> dict:
        """What the UI shows for the account's plan: label, limits and messages.

        The limits must be the ones the rules above enforce; the frontend has
        no limits of its own.
        """
        raise NotImplementedError


class SelfHostedEntitlements(Entitlements):
    """The self-hosted rules: the license tier plus SELF_HOSTED_BETA_LIMITS."""

    def effective_tier(self, account: Account) -> str:
        return license.get_installation_tier()

    def run_history_days(self, account: Account) -> Optional[int]:
        return SELF_HOSTED_BETA_LIMITS["run_history_days"]

    def check_new_project(
        self, account: Account, total_count: int, standard_count: int, rwx_count: int, project_type: str
    ) -> Tuple[bool, Optional[str]]:
        return check_project_type_limit(standard_count, rwx_count, project_type)

    def check_private_visibility_scope(self, account: Account) -> Tuple[bool, Optional[str]]:
        # Private repositories are allowed whenever the user's GitHub
        # credentials can see them, whatever the license tier.
        return True, None

    def repo_limit_message(self, account: Account, limit: int) -> str:
        return (
            f"Self-hosted beta allows up to {limit} repositories per project. "
            "Paid plans are not available during the self-hosted beta."
        )

    def secrets_limit(self, account: Account) -> Optional[int]:
        return SELF_HOSTED_BETA_LIMITS["secrets_per_project"]

    def secrets_limit_message(self, account: Account, limit: int, current: int, new: int) -> str:
        return (
            f"Self-hosted beta allows up to {limit} secrets per project. "
            f"You currently have {current} and are trying to add {new} new secret(s). "
            f"Paid plans are not available during the self-hosted beta."
        )

    def env_var_limit(self, account: Optional[Account]) -> Optional[int]:
        return SELF_HOSTED_BETA_LIMITS["env_vars_per_project"]

    def env_var_limit_message(self, limit: int, current: int, new: int) -> str:
        return (
            f"Self-hosted beta allows up to {limit} environment variables per project. "
            f"You currently have {current} and are trying to add {new} new variable(s). "
            f"Paid plans are not available during the self-hosted beta."
        )

    def environment_limit(self, account: Optional[Account]) -> Optional[int]:
        return SELF_HOSTED_BETA_LIMITS["environments_per_project"]

    def environment_limit_message(self, limit: int, current: int) -> str:
        return (
            f"Self-hosted beta allows up to {limit} GitHub environments per project. "
            f"You currently have {current} environment(s). "
            f"Paid plans are not available during the self-hosted beta."
        )

    def environment_sync_limit_message(self, limit: int, current: int, new: int) -> str:
        return (
            f"Self-hosted beta allows up to {limit} GitHub environments per project. "
            f"Syncing would create {new} new environment(s), "
            f"but you already have {current}. "
            f"Paid plans are not available during the self-hosted beta."
        )

    def plan(self, account: Account) -> dict:
        caller = SELF_HOSTED_BETA_LIMITS["standard_projects"]
        reusable = SELF_HOSTED_BETA_LIMITS["rwx_projects"]
        limits = {
            "projects": None,
            "caller_projects": caller,
            "reusable_projects": reusable,
            "secrets_per_project": self.secrets_limit(account),
            "env_vars_per_project": self.env_var_limit(account),
            "environments_per_project": self.environment_limit(account),
        }
        return {
            "tier": "self-hosted-beta",
            "label": "Self-Hosted Beta",
            "summary": "Self-hosted beta access. Paid plans are not currently available.",
            "limits": limits,
            "private_repositories": True,
            "private_repositories_help": (
                "Use this project for private GitHub repositories only. "
                "Requires GitHub credentials with private repository access."
            ),
            "limit_messages": {
                "caller_projects": f"Beta limit reached ({caller}/{caller} Caller Workflow Projects).",
                "reusable_projects": f"Beta limit reached ({reusable}/{reusable} Reusable Workflow Projects).",
                **{
                    key: (
                        f"Self-Hosted Beta: You can create up to {limits[limit_key]} {noun} per project. "
                        f"You have reached the limit."
                    )
                    for key, limit_key, noun in _PER_PROJECT_RESOURCES
                },
            },
            "upgrade": None,
        }


# (limit_messages key, limits key, noun) for the per-project limits.
_PER_PROJECT_RESOURCES = (
    ("secrets", "secrets_per_project", "secrets"),
    ("env_vars", "env_vars_per_project", "environment variables"),
    ("environments", "environments_per_project", "deployment environments"),
)


_entitlements: Entitlements = SelfHostedEntitlements()


def use(entitlements: Entitlements) -> Entitlements:
    """Install the rules the functions below answer from; returns the previous ones."""
    global _entitlements
    previous, _entitlements = _entitlements, entitlements
    return previous


def get_effective_tier(account: Account) -> str:
    """The account's tier: "free", "professional" or "enterprise"."""
    return _entitlements.effective_tier(account)


def get_run_history_days(account: Account) -> Optional[int]:
    """How many days of workflow-run history this account can see.

    Returns a value instead of the usual ``(allowed, message)`` pair because the
    requested window is clamped rather than rejected: asking for a year of
    history on a tier that keeps a month should show the month, not an error.

    ``None`` means unlimited.
    """
    return _entitlements.run_history_days(account)


def check_new_project(
    account: Account, total_count: int, standard_count: int, rwx_count: int, project_type: str
) -> Tuple[bool, Optional[str]]:
    """Check if the account can create another project of ``project_type``.

    ``total_count`` counts every project the account owns; the other two count
    its Caller Workflow and Reusable Workflow Projects.
    """
    return _entitlements.check_new_project(account, total_count, standard_count, rwx_count, project_type)


def check_private_visibility_scope(account: Account) -> Tuple[bool, Optional[str]]:
    """Check if the account can create a project whose
    ``repository_visibility_scope`` is ``"private"``."""
    return _entitlements.check_private_visibility_scope(account)


def secrets_limit(account: Account) -> Optional[int]:
    """Secrets allowed per project, or ``None`` for unlimited."""
    return _entitlements.secrets_limit(account)


def secrets_limit_message(account: Account, limit: int, current: int, new: int) -> str:
    return _entitlements.secrets_limit_message(account, limit, current, new)


def env_var_limit(account: Optional[Account]) -> Optional[int]:
    """Environment variables allowed per project, or ``None`` when not limited."""
    return _entitlements.env_var_limit(account)


def env_var_limit_message(limit: int, current: int, new: int) -> str:
    return _entitlements.env_var_limit_message(limit, current, new)


def environment_limit(account: Optional[Account]) -> Optional[int]:
    """GitHub deployment environments allowed per project, or ``None`` when not limited."""
    return _entitlements.environment_limit(account)


def environment_limit_message(limit: int, current: int) -> str:
    return _entitlements.environment_limit_message(limit, current)


def environment_sync_limit_message(limit: int, current: int, new: int) -> str:
    return _entitlements.environment_sync_limit_message(limit, current, new)


def describe_plan(account: Account) -> dict:
    """The account's plan as GET /api/user/{username} returns it for the UI."""
    return _entitlements.plan(account)


def check_private_repo_access(account: Account) -> Tuple[bool, Optional[str]]:
    """
    Check if account can access private repositories.

    Private repositories are part of the core product and are available on
    every tier. This function is retained for backwards compatibility but
    always returns ``(True, None)``.

    Args:
        account: Account object

    Returns:
        Tuple of (allowed: bool, error_message: Optional[str])
    """
    return True, None


def check_repo_limit(account: Account, repo_count: int) -> Tuple[bool, Optional[str]]:
    """
    Check if account can add repos to a project.
    
    Args:
        account: Account object
        repo_count: Number of repos being added
        
    Returns:
        Tuple of (allowed: bool, error_message: Optional[str])
    """
    tier = get_effective_tier(account)
    max_repos = REPOS_PER_PROJECT[normalize_tier_name(tier)]
    
    # Unlimited
    if max_repos is None:
        return True, None
    
    # Check limit
    if repo_count > max_repos:
        return False, _entitlements.repo_limit_message(account, max_repos)

    return True, None
