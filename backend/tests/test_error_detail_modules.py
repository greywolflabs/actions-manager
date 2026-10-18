"""Env vars, project deletion and actions projects hide the exception text (#2297).

These returned dicts, per-repository messages and _ApiError details carried
str(e) to the client. A marker in the exception must not appear in what the
caller gets back.
"""
import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import actions_projects
import github_env_vars
import project_deletion
from main import app
from models import Account
from tests.conftest import TestingSessionLocal

USER = "mod-detail-user"
MARKER = "secret-marker-2297"

client = TestClient(app)


def _raise(error):
    def fail(*_args, **_kwargs):
        raise error
    return fail


@pytest.fixture
def signed_in(authenticate_client):
    db = TestingSessionLocal()
    try:
        db.add(Account(github_user=USER, github_email=f"{USER}@example.com", account_type="free"))
        db.commit()
    finally:
        db.close()
    github_env_vars.user_tokens[USER] = "gho_test_token"
    authenticate_client(client, USER, TestingSessionLocal)
    yield
    github_env_vars.user_tokens.pop(USER, None)


def test_an_env_vars_route_returns_a_fixed_message(signed_in, monkeypatch):
    db_error = OperationalError(f"SELECT {MARKER}", {"param": MARKER}, Exception(MARKER))
    monkeypatch.setattr(github_env_vars, "_find_project_by_name", _raise(db_error))

    resp = client.get("/api/env-vars-count", params={"project_name": "p", "repo_names": "o/r"})

    assert resp.json() == {"error": "Error getting env vars count", "status": 500}
    assert MARKER not in resp.text


def test_a_project_deletion_error_names_the_failure_kind(monkeypatch):
    monkeypatch.setattr(project_deletion.requests, "get", _raise(requests.ConnectionError(f"proxy {MARKER}.internal")))
    results = {"errors": []}

    project_deletion._delete_repository_secrets("o/r", {}, "P1", results)

    assert results["errors"] == ["Error deleting repository secrets from o/r: network error"]


def test_an_actions_project_fetch_failure_hides_the_exception(monkeypatch):
    monkeypatch.setattr(actions_projects.requests, "get", _raise(requests.ConnectionError(f"proxy {MARKER}.internal")))

    with pytest.raises(actions_projects._ApiError) as raised:
        actions_projects._fetch_repo_file("o", "r", "main", "action.yml", "token")

    assert raised.value.status_code == 502
    assert raised.value.detail == "GitHub API request failed: network error"
