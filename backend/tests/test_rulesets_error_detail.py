"""Ruleset routes report a failure without its exception text (#2246).

#2243 stopped upload and create echoing the exception; list, apply, sync status
and delete still put it in the response, so a database error reached the
client with its SQL and bound parameters, and a network error with internal
hostnames or proxy details.
"""
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from main import app
from models import Account, Project, ProjectRepo, ProjectRuleset, Repo, Ruleset
from tests.conftest import TestingSessionLocal

OWNER = "rs-detail-owner"
PROJECT = "DetailProject"
REPO = "rs-detail-org/api"
MARKER = "secret-marker-2246"

client = TestClient(app)


def _db_error():
    return OperationalError(f"SELECT {MARKER}", {"param": MARKER}, Exception(MARKER))


def _network_error():
    return httpx.ConnectError(f"proxy {MARKER}.internal refused the connection")


def _raise(error):
    def fail(*_args, **_kwargs):
        raise error
    return fail


@pytest.fixture
def ruleset_id(authenticate_client):
    db = TestingSessionLocal()
    try:
        account = Account(github_user=OWNER, github_email=f"{OWNER}@example.com", account_type="free")
        db.add(account)
        db.flush()
        project = Project(
            project_name=PROJECT, project_code="RSDT", user_id=account.user_id, project_type="standard",
        )
        repo = Repo(repo_name=REPO)
        ruleset = Ruleset(
            ruleset_name="policy", ruleset_json=json.dumps({"name": "policy"}), user_id=account.user_id,
        )
        db.add_all([project, repo, ruleset])
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.add(ProjectRuleset(project_id=project.project_id, ruleset_id=ruleset.ruleset_id))
        db.commit()
        rid = ruleset.ruleset_id
    finally:
        db.close()
    authenticate_client(client, OWNER, TestingSessionLocal)
    return rid


@pytest.fixture(autouse=True)
def github_token():
    import rulesets as rulesets_module

    rulesets_module.user_tokens[OWNER] = "gho_test_token"
    yield
    rulesets_module.user_tokens.pop(OWNER, None)


class FailingGitHub:
    """An httpx.AsyncClient whose every request raises *error*."""

    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, *_args, **_kwargs):
        raise self.error

    async def post(self, *_args, **_kwargs):
        raise self.error


def _assert_hidden(resp, status):
    assert resp.status_code == status, resp.text
    assert MARKER not in resp.text


def _sync_status(rid):
    return client.post(f"/api/rulesets/{rid}/sync-status", json={"repo_names": [REPO]})


def _apply(rid):
    return client.post(f"/api/rulesets/{rid}/apply", json={"repo_names": [REPO]})


def test_list_hides_a_database_error(ruleset_id, monkeypatch):
    monkeypatch.setattr("rulesets._find_project_by_name", _raise(_db_error()))

    resp = client.get(f"/api/rulesets/{PROJECT}")

    _assert_hidden(resp, 500)
    assert resp.json()["detail"] == "Error fetching rulesets"


def test_apply_hides_a_server_error(ruleset_id, monkeypatch):
    monkeypatch.setattr("rulesets._github_name", _raise(_db_error()))

    resp = _apply(ruleset_id)

    _assert_hidden(resp, 500)
    assert resp.json()["detail"] == "Error applying ruleset"


@pytest.mark.parametrize("error", [_network_error(), RuntimeError(MARKER)], ids=["network", "unexpected"])
def test_apply_reports_a_github_failure_without_its_text(ruleset_id, monkeypatch, error):
    monkeypatch.setattr("rulesets.httpx.AsyncClient", lambda *a, **k: FailingGitHub(error))

    resp = _apply(ruleset_id)

    _assert_hidden(resp, 200)
    assert resp.json()["errors"] == [f"❌ Failed to apply ruleset to {REPO}"]


@pytest.mark.parametrize(
    "error, kind",
    [(_network_error(), "network error"), (RuntimeError(MARKER), "unexpected error")],
    ids=["network", "unexpected"],
)
def test_apply_names_a_repository_failure_by_kind_only(ruleset_id, monkeypatch, error, kind):
    """apply_ruleset_to_repo turns its own failures into False today; this is the
    route's guard for one that escapes it."""
    monkeypatch.setattr("rulesets.apply_ruleset_to_repo", AsyncMock(side_effect=error))

    resp = _apply(ruleset_id)

    _assert_hidden(resp, 200)
    assert resp.json()["errors"] == [f"❌ Error applying ruleset to {REPO}: {kind}"]


def test_sync_status_hides_a_server_error(ruleset_id, monkeypatch):
    monkeypatch.setattr("rulesets.httpx.AsyncClient", _raise(RuntimeError(MARKER)))

    resp = _sync_status(ruleset_id)

    _assert_hidden(resp, 500)
    assert resp.json()["detail"] == "Error checking ruleset sync status"


@pytest.mark.parametrize(
    "error, kind",
    [(_network_error(), "network error"), (RuntimeError(MARKER), "unexpected error")],
    ids=["network", "unexpected"],
)
def test_sync_status_names_a_repository_failure_by_kind_only(ruleset_id, monkeypatch, error, kind):
    monkeypatch.setattr("rulesets.httpx.AsyncClient", lambda *a, **k: FailingGitHub(error))

    resp = _sync_status(ruleset_id)

    _assert_hidden(resp, 200)
    assert resp.json()["repo_statuses"][REPO] == {
        "status": "error",
        "message": f"Error checking repository: {kind}",
    }


def test_delete_hides_a_database_error(ruleset_id, monkeypatch):
    monkeypatch.setattr("rulesets._repos_targeted_by_ruleset", _raise(_db_error()))

    resp = client.delete(f"/api/rulesets/{ruleset_id}", params={"scope": "github"})

    _assert_hidden(resp, 500)
    assert resp.json()["detail"] == "Error deleting ruleset"


def test_delete_names_a_repository_network_failure_by_kind_only(ruleset_id, monkeypatch):
    monkeypatch.setattr("rulesets.httpx.AsyncClient", lambda *a, **k: FailingGitHub(_network_error()))

    resp = client.delete(f"/api/rulesets/{ruleset_id}", params={"scope": "project_and_github"})

    _assert_hidden(resp, 502)
    assert resp.json()["detail"]["errors"] == [f"{REPO}: network error"]
