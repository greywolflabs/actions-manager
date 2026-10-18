"""A ruleset stored against a project must be attached to that project (#2063).

`require_project_write_access` resolves the project through the access-aware
lookup and returns it, but upload and create then looked it up a second time,
owner-only and by exact name. A caller the gate let through - an editor or
admin on another account's project, or the owner typing the name in another
case - got `success: true` and a ruleset with no ProjectRuleset row: missing
from the project's list, and invisible to the GitHub-scoped removals.
"""
import io
import json

import pytest
from fastapi.testclient import TestClient

from main import app
from models import Account, Project, ProjectMembership, ProjectRuleset, Ruleset, WorkspaceMember
from tests.conftest import TestingSessionLocal

OWNER = "rs-upload-owner"
ADMIN = "rs-upload-admin"
EDITOR = "rs-upload-editor"
PROJECT = "UploadProject"
RULESET = {"name": "require-reviews", "target": "branch"}

client = TestClient(app)


@pytest.fixture
def project_id():
    """A project owned by OWNER, a workspace admin, and a member granted project_editor on it."""
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email=f"{OWNER}@example.com", account_type="free")
        admin = Account(github_user=ADMIN, github_email=f"{ADMIN}@example.com", account_type="free")
        editor = Account(github_user=EDITOR, github_email=f"{EDITOR}@example.com", account_type="free")
        db.add_all([owner, admin, editor])
        db.flush()
        db.add(WorkspaceMember(user_id=admin.user_id, workspace_role="admin"))
        db.add(WorkspaceMember(user_id=editor.user_id, workspace_role="member"))
        project = Project(
            project_name=PROJECT, project_code="RSUP", user_id=owner.user_id,
            project_type="standard",
        )
        db.add(project)
        db.flush()
        db.add(ProjectMembership(
            user_id=editor.user_id, project_id=project.project_id, project_role="project_editor",
        ))
        db.commit()
        return project.project_id
    finally:
        db.close()


def _upload(caller, project_name):
    return client.post(
        "/api/rulesets/upload",
        data={"project_name": project_name, "github_user": caller},
        files={"file": ("require-reviews.json", io.BytesIO(json.dumps(RULESET).encode()), "application/json")},
    )


def _create(caller, project_name):
    return client.post(
        "/api/rulesets/create",
        params={"project_name": project_name, "github_user": caller},
        json={"name": "require-reviews", "ruleset_json": RULESET},
    )


def _attached_project_ids(ruleset_id):
    db = TestingSessionLocal()
    try:
        return [link.project_id for link in db.query(ProjectRuleset).filter_by(ruleset_id=ruleset_id)]
    finally:
        db.close()


def _listed_names(caller):
    resp = client.get(f"/api/rulesets/{PROJECT}", params={"github_user": caller})
    assert resp.status_code == 200, resp.text
    return [r["ruleset_name"] for r in resp.json()["rulesets"]]


@pytest.mark.parametrize("caller", [ADMIN, EDITOR])
def test_upload_to_another_accounts_project_attaches_it(project_id, caller, authenticate_client):
    authenticate_client(client, caller, TestingSessionLocal)

    resp = _upload(caller, PROJECT)

    assert resp.status_code == 200, resp.text
    assert _attached_project_ids(resp.json()["ruleset_id"]) == [project_id]
    assert _listed_names(caller) == ["require-reviews"]


@pytest.mark.parametrize("named", [OWNER, None])
def test_upload_stores_the_ruleset_under_the_session_caller(project_id, named, authenticate_client):
    """The caller comes from the session (#2241); a name in the form is ignored."""
    authenticate_client(client, EDITOR, TestingSessionLocal)
    form = {"project_name": PROJECT, **({"github_user": named} if named else {})}

    resp = client.post(
        "/api/rulesets/upload",
        data=form,
        files={"file": ("require-reviews.json", io.BytesIO(json.dumps(RULESET).encode()), "application/json")},
    )

    assert resp.status_code == 200, resp.text
    db = TestingSessionLocal()
    try:
        stored = db.get(Ruleset, resp.json()["ruleset_id"])
        assert db.get(Account, stored.user_id).github_user == EDITOR
    finally:
        db.close()


def test_upload_by_the_owner_in_another_case_attaches_it(project_id, authenticate_client):
    authenticate_client(client, OWNER, TestingSessionLocal)

    resp = _upload(OWNER, PROJECT.lower())

    assert resp.status_code == 200, resp.text
    assert _attached_project_ids(resp.json()["ruleset_id"]) == [project_id]


def test_upload_naming_an_unknown_project_stores_nothing(project_id, authenticate_client):
    authenticate_client(client, OWNER, TestingSessionLocal)

    resp = _upload(OWNER, "NoSuchProject")

    assert resp.status_code == 404, resp.text
    db = TestingSessionLocal()
    try:
        assert db.query(Ruleset).count() == 0
    finally:
        db.close()


def test_create_on_another_accounts_project_attaches_it(project_id, authenticate_client):
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = _create(ADMIN, PROJECT)

    assert resp.status_code == 200, resp.text
    assert _attached_project_ids(resp.json()["ruleset_id"]) == [project_id]


@pytest.mark.parametrize("send", [_upload, _create])
def test_a_failed_project_link_stores_no_ruleset(project_id, send, authenticate_client, monkeypatch):
    """The ruleset and its project link are saved together, or not at all."""
    authenticate_client(client, OWNER, TestingSessionLocal)

    def refuse(**_kwargs):
        raise RuntimeError("link insert failed")

    monkeypatch.setattr("rulesets.ProjectRuleset", refuse)

    resp = send(OWNER, PROJECT)

    assert resp.status_code == 500
    db = TestingSessionLocal()
    try:
        assert db.query(Ruleset).count() == 0
    finally:
        db.close()
