"""workflows.py reports failures without their exception text (#2297).

#2296 covered HTTPException details. These are the other ways the text reached
a client: the async PR-creation task status the UI polls, dicts returned from
catch-alls, and per-item messages. A message GitHub wrote about a repository
(DriftCheckUnavailable) is still shown.
"""
import uuid
from types import SimpleNamespace

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import workflows
from github_api_tracker import RateLimitExceeded
from main import app
from models import Account
from tests.conftest import TestingSessionLocal

USER = "wf-detail-user"
MARKER = "secret-marker-2297"

client = TestClient(app)


def _db_error():
    return OperationalError(f"SELECT {MARKER}", {"param": MARKER}, Exception(MARKER))


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
    workflows.user_tokens[USER] = "gho_test_token"
    authenticate_client(client, USER, TestingSessionLocal)
    yield
    workflows.user_tokens.pop(USER, None)


@pytest.fixture
def task_id():
    task_id = str(uuid.uuid4())
    workflows.pr_campaign_tasks[task_id] = {"status": "pending", "repos": {}, "results": {}, "prs_created": 0}
    yield task_id
    workflows.pr_campaign_tasks.pop(task_id, None)


def test_the_async_pr_task_hides_the_exception(task_id, monkeypatch):
    """The UI creates PRs this way, then polls the task for its status."""
    monkeypatch.setattr(workflows, "_get_project_and_token", _raise(_db_error()))
    payload = workflows.CreatePullRequestsRequest(project_name="p", async_mode=True)

    workflows._run_create_pull_requests_async(task_id, payload, USER)
    resp = client.get(f"/api/create-pull-requests/{task_id}")

    assert resp.json()["status"] == "error"
    assert resp.json()["error"] == "Error creating pull requests"
    assert MARKER not in resp.text


def test_the_async_pr_task_keeps_an_http_error_written_for_the_user(task_id, monkeypatch):
    """The synchronous path returns this detail too; the UI needs it to act."""
    gate = HTTPException(status_code=400, detail="Preflight validation must pass before creating this PR Campaign.")
    monkeypatch.setattr(workflows, "_get_project_and_token", _raise(gate))
    payload = workflows.CreatePullRequestsRequest(project_name="p", async_mode=True)

    workflows._run_create_pull_requests_async(task_id, payload, USER)
    resp = client.get(f"/api/create-pull-requests/{task_id}")

    assert resp.json()["status"] == "error"
    assert resp.json()["error"] == "Preflight validation must pass before creating this PR Campaign."


def test_a_returned_error_dict_hides_the_exception(signed_in, monkeypatch):
    monkeypatch.setattr(workflows, "_find_project_by_name", _raise(_db_error()))

    resp = client.get("/api/workflows-count", params={"project_name": "p"})

    assert resp.json() == {"error": "Error getting workflows count", "status": 500}
    assert MARKER not in resp.text


def test_a_per_item_result_names_the_failure_kind(monkeypatch):
    monkeypatch.setattr(workflows.requests, "get", _raise(requests.ConnectionError(f"proxy {MARKER}.internal")))

    result = workflows._fetch_branch_protection("o", "r", "main", {})

    assert result == {"status": "unknown", "error": "network error"}


def test_a_delivery_item_says_when_the_rate_limit_resets(monkeypatch):
    limited = RateLimitExceeded("API rate limit exceeded. Limit resets at 2026-10-08T04:00:00")
    monkeypatch.setattr(workflows, "_update_workflow_to_github", _raise(limited))

    committed, errors = workflows._commit_workflows_to_branch(
        [{"name": "ci"}], "o", "r", "P001", "am-branch", {}, "o/r", USER, None, False,
    )

    assert committed == []
    assert errors == ["ci: API rate limit exceeded. Limit resets at 2026-10-08T04:00:00"]


def test_a_failed_preflight_stores_the_failure_kind(signed_in, monkeypatch):
    """The stored result is what the preflight status endpoints return."""
    project = SimpleNamespace()
    monkeypatch.setattr(workflows, "_get_project_and_token", _raise(_db_error()))
    monkeypatch.setattr(workflows, "_find_project_by_name", lambda *_args: project)

    resp = client.post("/api/run-preflight-validation", json={"project_name": "p"})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Preflight validation failed."
    assert project.last_preflight_status == "failed"
    assert project.last_preflight_error == "Preflight validation failed: unexpected error"


@pytest.mark.parametrize(
    "error, expected",
    [
        (workflows.DriftCheckUnavailable("GitHub returned 403 resolving the default branch of o/r", 403),
         "Failed to resolve branches: GitHub returned 403 resolving the default branch of o/r"),
        (RuntimeError(MARKER), "Failed to resolve branches: unexpected error"),
    ],
    ids=["github-answer-shown", "unexpected-hidden"],
)
def test_branch_resolution_shows_githubs_answer_only(monkeypatch, error, expected):
    monkeypatch.setattr(workflows, "_resolve_branches_for_repo", _raise(error))

    branches, error_result = workflows._resolve_effective_target_branches(
        "o/r", "o", "r", None, None, "default", "", 0, {}, USER,
    )

    assert branches is None
    assert error_result == {"error": expected, "status": 500}


def test_a_github_error_reading_a_workflow_keeps_its_status(monkeypatch):
    """Raised as DriftCheckUnavailable, so callers still show GitHub's answer."""
    class Forbidden:
        status_code = 403

    monkeypatch.setattr(workflows.requests, "get", lambda *a, **k: Forbidden())

    with pytest.raises(workflows.DriftCheckUnavailable) as raised:
        workflows.get_workflow_from_github("o", "r", "ci.yml", "token", default_branch="main")

    assert raised.value.status_code == 403
    assert workflows.failure_kind(raised.value, workflows._SHOWN_GITHUB_ERRORS) == "GitHub API error: 403"
