"""A request from a project's page acts on that project, whatever its name collides with (#2240).

Project names are unique per owner only. Part 1 refused a name that matches
several owners' projects (409) rather than guess. The page knows the project's
id, and sends it as X-Project-Id; the name lookups use it to pick among the
projects that name could already reach - never to reach further.
"""
import io
import json

import pytest
from fastapi.testclient import TestClient

import github_secrets
import workflows
from main import app
from models import Account, Project, ProjectMembership, ProjectRuleset, WorkspaceMember
from tests.conftest import TestingSessionLocal

OWNER_A = "hint-owner-a"
OWNER_B = "hint-owner-b"
ADMIN = "hint-admin"
MEMBER = "hint-member"
READER = "hint-reader"
NAME = "Infra"

client = TestClient(app)


@pytest.fixture
def projects():
    """A's and B's projects share a name; the other accounts own neither."""
    db = TestingSessionLocal()
    try:
        roles = {OWNER_A: "member", OWNER_B: "member", ADMIN: "admin", MEMBER: "member", READER: "read_only"}
        accounts = {
            name: Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
            for name in roles
        }
        db.add_all(accounts.values())
        db.flush()
        for name, role in roles.items():
            db.add(WorkspaceMember(user_id=accounts[name].user_id, workspace_role=role))
        a = Project(project_name=NAME, project_code="HNTA", user_id=accounts[OWNER_A].user_id, project_type="standard")
        b = Project(project_name=NAME, project_code="HNTB", user_id=accounts[OWNER_B].user_id, project_type="standard")
        other = Project(project_name="Platform", project_code="HNTP", user_id=accounts[OWNER_A].user_id,
                        project_type="standard")
        db.add_all([a, b, other])
        db.commit()
        return {
            "a": a.project_id, "b": b.project_id, "other": other.project_id,
            **{f"{name}_id": accounts[name].user_id for name in roles},
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


def _upload(caller, hint=None):
    return client.post(
        "/api/rulesets/upload",
        data={"project_name": NAME, "github_user": caller},
        files={"file": ("policy.json", io.BytesIO(json.dumps({"name": "policy"}).encode()), "application/json")},
        headers={"X-Project-Id": str(hint)} if hint is not None else {},
    )


def _attached_to():
    db = TestingSessionLocal()
    try:
        return [link.project_id for link in db.query(ProjectRuleset)]
    finally:
        db.close()


@pytest.mark.parametrize("target", ["a", "b"])
def test_an_admin_uploads_to_the_project_the_page_names(projects, target, authenticate_client):
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = _upload(ADMIN, projects[target])

    assert resp.status_code == 200, resp.text
    assert _attached_to() == [projects[target]]


def test_a_member_granted_on_both_uploads_to_the_page_project(projects, authenticate_client):
    _grant(projects[f"{MEMBER}_id"], projects["a"])
    _grant(projects[f"{MEMBER}_id"], projects["b"])
    authenticate_client(client, MEMBER, TestingSessionLocal)

    resp = _upload(MEMBER, projects["b"])

    assert resp.status_code == 200, resp.text
    assert _attached_to() == [projects["b"]]


def test_an_id_for_a_project_of_another_name_is_ignored(projects, authenticate_client):
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = _upload(ADMIN, projects["other"])

    assert resp.status_code == 409, resp.text
    assert _attached_to() == []


@pytest.mark.parametrize("hint", ["abc", "-3", "0", "", "2147483648", "99999999999999999999"])
def test_a_malformed_id_is_ignored(projects, hint, authenticate_client):
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = _upload(ADMIN, hint)

    assert resp.status_code == 409, resp.text


def test_an_id_never_reaches_a_project_the_caller_cannot_see(projects, authenticate_client):
    """A read-only user granted A's project names B's id: the grant decides, as before."""
    _grant(projects[f"{READER}_id"], projects["a"], role="project_viewer")
    authenticate_client(client, READER, TestingSessionLocal)

    resp = client.get(
        f"/api/projects/{NAME}",
        params={"github_user": READER},
        headers={"X-GitHub-User": READER, "X-Project-Id": str(projects["b"])},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_code"] == "HNTA"


@pytest.mark.parametrize("target, code", [("a", "HNTA"), ("b", "HNTB")])
def test_the_project_page_opens_the_project_it_names(projects, target, code, authenticate_client):
    """The page asks with the viewer's own name in the URL, which settles nothing for an admin."""
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get(
        f"/api/projects/{NAME}",
        params={"github_user": ADMIN},
        headers={"X-GitHub-User": ADMIN, "X-Project-Id": str(projects[target])},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_code"] == code


def test_an_admin_with_their_own_same_named_project_opens_the_one_they_clicked(projects, authenticate_client):
    """Without the id the viewer's own "Infra" won silently."""
    db = TestingSessionLocal()
    try:
        db.add(Project(project_name=NAME, project_code="HNTO", user_id=projects[f"{ADMIN}_id"],
                       project_type="standard"))
        db.commit()
    finally:
        db.close()
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get(
        f"/api/projects/{NAME}",
        params={"github_user": ADMIN},
        headers={"X-GitHub-User": ADMIN, "X-Project-Id": str(projects["b"])},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_code"] == "HNTB"


@pytest.mark.parametrize("target, code", [("a", "HNTA"), ("b", "HNTB")])
def test_reading_secrets_uses_the_page_project(projects, target, code, authenticate_client, monkeypatch):
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"secrets": [{"name": "AM_HNTA_TOKEN"}, {"name": "AM_HNTB_TOKEN"}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(github_secrets.httpx, "AsyncClient", _Client)
    monkeypatch.setitem(github_secrets.user_tokens, ADMIN, "tok")
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get(
        "/api/get-secrets",
        params={"user": ADMIN, "repo_name": "acme/api", "project_name": NAME},
        headers={"X-Project-Id": str(projects[target])},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["secrets"] == [{"secret_key": f"AM_{code}_TOKEN", "repo": "acme/api"}]


def _admin_owns_one_too(projects):
    db = TestingSessionLocal()
    try:
        own = Project(project_name=NAME, project_code="HNTO", user_id=projects[f"{ADMIN}_id"],
                      project_type="standard")
        db.add(own)
        db.commit()
        return own.project_id
    finally:
        db.close()


def _remaining_projects():
    db = TestingSessionLocal()
    try:
        return {p.project_code for p in db.query(Project)}
    finally:
        db.close()


def _delete_from_page(hint):
    headers = {"X-Project-Id": str(hint)}
    summary = client.get(f"/api/projects/{NAME}/deletion-summary", headers=headers)
    deleted = client.request("DELETE", f"/api/projects/{NAME}/enhanced", headers=headers, json={
        "project_name": NAME, "delete_github_resources": False,
    })
    return summary, deleted


def test_deleting_from_another_owners_page_never_deletes_the_callers_own(projects, authenticate_client):
    """Deletion is owner-only, and looked up the caller's own "Infra" whichever the page showed."""
    _admin_owns_one_too(projects)
    authenticate_client(client, ADMIN, TestingSessionLocal)

    summary, deleted = _delete_from_page(projects["b"])

    assert summary.status_code == 404, summary.text
    assert deleted.status_code == 404, deleted.text
    assert _remaining_projects() == {"HNTA", "HNTB", "HNTP", "HNTO"}


def test_deleting_from_the_callers_own_page_deletes_it(projects, authenticate_client):
    own = _admin_owns_one_too(projects)
    authenticate_client(client, ADMIN, TestingSessionLocal)

    summary, deleted = _delete_from_page(own)

    assert summary.status_code == 200, summary.text
    assert summary.json()["project_code"] == "HNTO"
    assert deleted.status_code == 200, deleted.text
    assert _remaining_projects() == {"HNTA", "HNTB", "HNTP"}


def test_a_drift_refresh_by_id_does_not_ask_the_name_again(projects, authenticate_client, monkeypatch):
    """The refresh re-ran detection by the project's name, which two owners share."""
    monkeypatch.setitem(workflows.user_tokens, ADMIN, "tok")
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get(f"/api/projects/{projects['b']}/drift", params={"github_user": ADMIN, "refresh": "true"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == projects["b"]


@pytest.mark.parametrize("project_id", [0, -1, 2**31])
def test_a_pull_request_payload_refuses_an_id_no_project_can_have(project_id):
    with pytest.raises(ValueError):
        workflows.CreatePullRequestsRequest(project_name=NAME, project_id=project_id)


def test_a_pull_request_payload_names_its_project_by_id(projects, monkeypatch):
    """Routes that build a campaign for a project they hold pass its id along."""
    monkeypatch.setitem(workflows.user_tokens, ADMIN, "tok")
    db = TestingSessionLocal()
    try:
        payload = workflows.CreatePullRequestsRequest(project_name=NAME, project_id=projects["a"])
        _token, project = workflows._get_project_and_token(payload, db, github_user=ADMIN)
    finally:
        db.close()

    assert project.project_id == projects["a"]
