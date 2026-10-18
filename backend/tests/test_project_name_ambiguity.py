"""A bare project name matching several owners' projects is refused, never guessed (#2240).

Project names are unique per owner, not across the workspace. When an admin or
member named a project they neither own nor were granted, `_find_project_by_name`
fell back to a workspace-wide match and took `.first()` - whichever project the
database returned. Working on B's "Infra" could write to A's.
"""
import io
import json

import pytest
from fastapi.testclient import TestClient

import github_secrets
from main import app
from models import Account, Project, ProjectMembership, ProjectRuleset, Ruleset, WorkspaceMember
from tests.conftest import TestingSessionLocal

OWNER_A = "amb-owner-a"
OWNER_B = "amb-owner-b"
ADMIN = "amb-admin"
MEMBER = "amb-member"
NAME = "Infra"

client = TestClient(app)


@pytest.fixture
def projects():
    """A's and B's projects share a name; ADMIN and MEMBER own neither."""
    db = TestingSessionLocal()
    try:
        accounts = {
            name: Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
            for name in (OWNER_A, OWNER_B, ADMIN, MEMBER)
        }
        db.add_all(accounts.values())
        db.flush()
        for name, role in ((OWNER_A, "member"), (OWNER_B, "member"), (ADMIN, "admin"), (MEMBER, "member")):
            db.add(WorkspaceMember(user_id=accounts[name].user_id, workspace_role=role))
        a = Project(project_name=NAME, project_code="AMBA", user_id=accounts[OWNER_A].user_id, project_type="standard")
        b = Project(project_name=NAME, project_code="AMBB", user_id=accounts[OWNER_B].user_id, project_type="standard")
        db.add_all([a, b])
        db.commit()
        return {
            "a": a.project_id, "b": b.project_id,
            "member_id": accounts[MEMBER].user_id, "owner_b_id": accounts[OWNER_B].user_id,
        }
    finally:
        db.close()


def _grant(user_id, project_id, role="project_editor"):
    db = TestingSessionLocal()
    try:
        db.add(ProjectMembership(user_id=user_id, project_id=project_id, project_role=role))
        db.commit()
    finally:
        db.close()


def _upload(caller, project_name=NAME):
    return client.post(
        "/api/rulesets/upload",
        data={"project_name": project_name, "github_user": caller},
        files={"file": ("policy.json", io.BytesIO(json.dumps({"name": "policy"}).encode()), "application/json")},
    )


def _attached_to():
    db = TestingSessionLocal()
    try:
        return [link.project_id for link in db.query(ProjectRuleset)]
    finally:
        db.close()


def _rulesets():
    db = TestingSessionLocal()
    try:
        return db.query(Ruleset).count()
    finally:
        db.close()


@pytest.mark.parametrize("caller", [ADMIN, MEMBER])
def test_a_name_two_owners_share_is_refused(projects, caller, authenticate_client):
    authenticate_client(client, caller, TestingSessionLocal)

    resp = _upload(caller)

    assert resp.status_code == 409, resp.text
    assert _rulesets() == 0


def test_an_owner_still_gets_their_own_project(projects, authenticate_client):
    authenticate_client(client, OWNER_B, TestingSessionLocal)

    resp = _upload(OWNER_B, NAME.lower())

    assert resp.status_code == 200, resp.text
    assert _attached_to() == [projects["b"]]


def test_a_single_grant_picks_that_project(projects, authenticate_client):
    _grant(projects["member_id"], projects["b"])
    authenticate_client(client, MEMBER, TestingSessionLocal)

    resp = _upload(MEMBER)

    assert resp.status_code == 200, resp.text
    assert _attached_to() == [projects["b"]]


def test_grants_on_both_are_refused(projects, authenticate_client):
    _grant(projects["member_id"], projects["a"])
    _grant(projects["member_id"], projects["b"])
    authenticate_client(client, MEMBER, TestingSessionLocal)

    resp = _upload(MEMBER)

    assert resp.status_code == 409, resp.text
    assert _rulesets() == 0


def test_a_unique_name_still_resolves_workspace_wide(projects, authenticate_client):
    db = TestingSessionLocal()
    try:
        db.query(Project).filter(Project.project_id == projects["a"]).update({"project_name": "Platform"})
        db.commit()
    finally:
        db.close()
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = _upload(ADMIN)

    assert resp.status_code == 200, resp.text
    assert _attached_to() == [projects["b"]]


def test_creating_secrets_under_a_shared_name_is_refused(projects, authenticate_client, monkeypatch):
    def no_github(*_args, **_kwargs):
        raise AssertionError("reached GitHub")

    monkeypatch.setattr(github_secrets.httpx, "AsyncClient", no_github)
    monkeypatch.setitem(github_secrets.user_tokens, ADMIN, "tok")
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.post("/api/create-secrets", json={
        "user": ADMIN,
        "project_name": NAME,
        "repo_names": ["acme/api"],
        "secrets": [{"secret_key": "TOKEN", "secret_value": "v"}],
    })

    assert resp.status_code == 409, resp.text


def test_an_owner_with_a_grant_on_the_other_project_still_gets_their_own(projects, authenticate_client):
    """The caller's own project resolves before a grant on someone else's of the same name."""
    _grant(projects["owner_b_id"], projects["a"])
    authenticate_client(client, OWNER_B, TestingSessionLocal)

    resp = _upload(OWNER_B, NAME.lower())

    assert resp.status_code == 200, resp.text
    assert _attached_to() == [projects["b"]]


def test_a_route_holding_the_project_id_does_not_ask_the_name_again(projects, authenticate_client, monkeypatch):
    import workflows

    monkeypatch.setitem(workflows.user_tokens, ADMIN, "tok")
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get(f"/api/projects/{projects['b']}/drift", params={"github_user": ADMIN})

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == projects["b"]


def test_a_count_route_reports_the_conflict_rather_than_a_server_error(projects, authenticate_client, monkeypatch):
    import workflows

    monkeypatch.setitem(workflows.user_tokens, ADMIN, "tok")
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get("/api/workflows-count", params={"user": ADMIN, "project_name": NAME})

    assert resp.status_code == 409, resp.text


def test_deleting_a_project_by_a_shared_name_is_refused(projects, authenticate_client):
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.delete(f"/api/projects/{NAME}", params={"github_user": ADMIN}, headers={"X-GitHub-User": ADMIN})

    assert resp.status_code == 409, resp.text
    db = TestingSessionLocal()
    try:
        assert db.query(Project).filter(Project.project_name == NAME).count() == 2
    finally:
        db.close()


def test_the_project_page_still_opens_the_named_owners_project(projects, authenticate_client):
    """The page passes the owner, which settles a shared name before any fallback."""
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get(f"/api/projects/{NAME}", params={"github_user": OWNER_B}, headers={"X-GitHub-User": ADMIN})

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_code"] == "AMBB"
