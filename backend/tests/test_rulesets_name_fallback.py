"""Apply must use the same ruleset name as sync status and removal (#2238).

A ruleset uploaded without a `name` is listed under its filename, and sync status
and the GitHub-side removal look it up on GitHub by that filename. Apply sent
"Imported Ruleset" instead, so the copy it created was never found again: the
panel reported it missing forever and a GitHub removal deleted nothing. A null
or blank `name` in the JSON is just as nameless.
"""
import io
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from models import Account, Project, ProjectRepo, Repo
from tests.conftest import TestingSessionLocal

OWNER = "rs-name-owner"
PROJECT = "NameFallbackProject"
REPO = "acme/api"

client = TestClient(app)


@pytest.fixture
def owner():
    db = TestingSessionLocal()
    try:
        account = Account(github_user=OWNER, github_email=f"{OWNER}@example.com", account_type="free")
        db.add(account)
        db.flush()
        project = Project(project_name=PROJECT, project_code="RSNF", user_id=account.user_id, project_type="standard")
        repo = Repo(repo_name=REPO)
        db.add_all([project, repo])
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.commit()
    finally:
        db.close()


def _upload_and_apply(ruleset_json, filename, authenticate_client):
    authenticate_client(client, OWNER, TestingSessionLocal)
    upload = client.post(
        "/api/rulesets/upload",
        data={"project_name": PROJECT, "github_user": OWNER},
        files={"file": (filename, io.BytesIO(json.dumps(ruleset_json).encode()), "application/json")},
    )
    assert upload.status_code == 200, upload.text

    with patch("rulesets.apply_ruleset_to_repo", new=AsyncMock(return_value=True)) as apply:
        resp = client.post(
            f"/api/rulesets/{upload.json()['ruleset_id']}/apply",
            json={"repo_names": [REPO], "github_user": OWNER},
        )
    assert resp.status_code == 200, resp.text
    return apply.await_args.args[2]


def test_a_nameless_ruleset_is_applied_under_its_listed_name(owner, authenticate_client):
    sent = _upload_and_apply({"target": "branch", "rules": []}, "branch-protect.json", authenticate_client)

    assert sent.get("name") == "branch-protect"


def test_a_named_ruleset_keeps_its_own_name(owner, authenticate_client):
    sent = _upload_and_apply({"name": "require-reviews", "target": "branch"}, "anything.json", authenticate_client)

    assert sent.get("name") == "require-reviews"


@pytest.mark.parametrize("json_name", [None, ""])
def test_a_null_or_blank_json_name_counts_as_none(owner, json_name, authenticate_client):
    authenticate_client(client, OWNER, TestingSessionLocal)
    created = client.post(
        "/api/rulesets/create",
        params={"project_name": PROJECT, "github_user": OWNER},
        json={"name": "branch-protect", "ruleset_json": {"name": json_name, "target": "branch"}},
    )
    assert created.status_code == 200, created.text

    with patch("rulesets.apply_ruleset_to_repo", new=AsyncMock(return_value=True)) as apply:
        resp = client.post(
            f"/api/rulesets/{created.json()['ruleset_id']}/apply",
            json={"repo_names": [REPO], "github_user": OWNER},
        )

    assert resp.status_code == 200, resp.text
    assert apply.await_args.args[2]["name"] == "branch-protect"


class FakeGitHub:
    """Keeps the rulesets POSTed to it and lists them back, like a repository would."""

    def __init__(self):
        self.created = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, json=None, **_):
        self.created.append(json["name"])
        return FakeResponse(201, {"id": len(self.created)})

    async def get(self, url, **_):
        return FakeResponse(200, [{"id": i + 1, "name": n} for i, n in enumerate(self.created)])


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def test_sync_status_finds_the_copy_apply_created(owner, authenticate_client, monkeypatch):
    """Through the real GitHub request bodies, not just the dict handed to them."""
    import rulesets as rulesets_module

    github = FakeGitHub()
    monkeypatch.setattr(rulesets_module.httpx, "AsyncClient", lambda *a, **k: github)
    monkeypatch.setitem(rulesets_module.user_tokens, OWNER, "tok")
    authenticate_client(client, OWNER, TestingSessionLocal)
    upload = client.post(
        "/api/rulesets/upload",
        data={"project_name": PROJECT, "github_user": OWNER},
        files={"file": ("branch-protect.json", io.BytesIO(b'{"target": "branch"}'), "application/json")},
    )
    ruleset_id = upload.json()["ruleset_id"]
    body = {"repo_names": [REPO], "github_user": OWNER}

    assert client.post(f"/api/rulesets/{ruleset_id}/apply", json=body).json()["applied_count"] == 1
    status = client.post(f"/api/rulesets/{ruleset_id}/sync-status", json=body).json()

    assert github.created == ["branch-protect"]
    assert status["is_synced"] is True
