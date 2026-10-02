"""
The plan GET /api/user/{username} returns for the UI (#2140), self-hosted
rules. The frontend has no limits of its own, so every limit here has to be
the one the endpoints enforce. The cloud plans are in tests/cloud/test_tier_plan.py.
"""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import tier_service
from models import Account
from tests.conftest import TestingSessionLocal, override_get_db


def _account():
    return Account(github_user="plan-user", account_type="free")


def test_plan_limits_are_the_enforced_limits():
    account = _account()
    limits = tier_service.describe_plan(account)["limits"]

    assert limits["secrets_per_project"] == tier_service.secrets_limit(account)
    assert limits["env_vars_per_project"] == tier_service.env_var_limit(account)
    assert limits["environments_per_project"] == tier_service.environment_limit(account)
    assert limits["projects"] is None
    for project_type, key, counts in (
        ("standard", "caller_projects", lambda n: (n, 0)),
        ("rwx", "reusable_projects", lambda n: (0, n)),
    ):
        limit = limits[key]
        assert tier_service.check_new_project(account, limit - 1, *counts(limit - 1), project_type)[0] is True
        assert tier_service.check_new_project(account, limit, *counts(limit), project_type)[0] is False


@pytest.mark.parametrize("license_tier", ["free", "professional", "enterprise"])
def test_plan_is_the_beta_plan_for_every_license_tier(license_tier):
    with patch("license.get_installation_tier", return_value=license_tier):
        plan = tier_service.describe_plan(_account())

    assert plan == EXPECTED_PLAN
    assert "marketplace" not in json.dumps(plan).lower()


def test_user_details_returns_the_plan(authenticate_client):
    from database import get_db
    from main import app

    db = TestingSessionLocal()
    db.add(Account(github_user="plan-user", github_email="plan@example.com", account_type="free"))
    db.commit()
    db.close()
    client = TestClient(app, base_url="https://testserver")
    authenticate_client(client, "plan-user", TestingSessionLocal)

    # Pinned so the request reads the database the account was written to:
    # other modules point get_db at their own database when imported.
    with patch.dict(app.dependency_overrides, {get_db: override_get_db}):
        response = client.get("/api/user/plan-user")

    assert response.status_code == 200
    assert response.json()["plan"] == EXPECTED_PLAN


# The wording the frontend showed before #2140, now sent by the backend.
EXPECTED_PLAN = {
    "tier": "self-hosted-beta",
    "label": "Self-Hosted Beta",
    "summary": "Self-hosted beta access. Paid plans are not currently available.",
    "limits": {
        "projects": None,
        "caller_projects": 4,
        "reusable_projects": 2,
        "secrets_per_project": 6,
        "env_vars_per_project": 6,
        "environments_per_project": 6,
    },
    "private_repositories": True,
    "private_repositories_help": (
        "Use this project for private GitHub repositories only. "
        "Requires GitHub credentials with private repository access."
    ),
    "limit_messages": {
        "caller_projects": "Beta limit reached (4/4 Caller Workflow Projects).",
        "reusable_projects": "Beta limit reached (2/2 Reusable Workflow Projects).",
        "secrets": "Self-Hosted Beta: You can create up to 6 secrets per project. You have reached the limit.",
        "env_vars": (
            "Self-Hosted Beta: You can create up to 6 environment variables per project. "
            "You have reached the limit."
        ),
        "environments": (
            "Self-Hosted Beta: You can create up to 6 deployment environments per project. "
            "You have reached the limit."
        ),
    },
    "upgrade": None,
}
