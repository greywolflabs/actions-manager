"""The project list and create routes act as the session's caller (#2241).

The list decided what a caller could see from X-GitHub-User, and with no header
it fell through to the admin branch, so a read_only member saw every project.
Create took the owner from X-GitHub-User, falling back to a github_user in the
body, which on an install with no workspace members (the middleware only pins
the header while membership is enforced) let anyone open a project in someone
else's name.
"""
import pytest
from fastapi.testclient import TestClient

from main import app
from models import Account, Project, ProjectMembership, WorkspaceMember
from tests.conftest import TestingSessionLocal

ADMIN = "pci-admin"
READER = "pci-reader"
OTHER = "pci-other"

client = TestClient(app)


def _account(db, name):
    account = Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
    db.add(account)
    db.commit()
    return account


def _project(db, owner, name, code=None):
    project = Project(project_name=name, project_code=code or name[:3].upper(), user_id=owner.user_id,
                      branch_option="default", use_prefix=True)
    db.add(project)
    db.commit()
    return project


@pytest.fixture
def workspace():
    """An admin owning two projects, and a read_only member granted one."""
    db = TestingSessionLocal()
    try:
        admin = _account(db, ADMIN)
        reader = _account(db, READER)
        db.add_all([
            WorkspaceMember(user_id=admin.user_id, workspace_role="admin"),
            WorkspaceMember(user_id=reader.user_id, workspace_role="read_only"),
        ])
        alpha = _project(db, admin, "alpha")
        _project(db, admin, "beta")
        db.add(ProjectMembership(user_id=reader.user_id, project_id=alpha.project_id,
                                 project_role="project_viewer"))
        db.commit()
    finally:
        db.close()


def test_a_read_only_member_without_the_header_sees_only_their_projects(workspace, authenticate_client):
    authenticate_client(client, READER, TestingSessionLocal)

    resp = client.get("/api/projects/", params={"github_user": READER})

    assert resp.status_code == 200, resp.text
    assert [p["project_name"] for p in resp.json()] == ["alpha"]


def test_naming_someone_else_in_the_query_is_refused(workspace, authenticate_client):
    authenticate_client(client, READER, TestingSessionLocal)

    resp = client.get("/api/projects/", params={"github_user": ADMIN})

    assert resp.status_code == 403, resp.text


def test_a_name_in_the_body_does_not_pick_the_new_projects_owner(authenticate_client):
    db = TestingSessionLocal()
    try:
        _account(db, ADMIN)
        _account(db, OTHER)
    finally:
        db.close()
    authenticate_client(client, OTHER, TestingSessionLocal)

    resp = client.post("/api/projects/", json={
        "project_name": "gamma", "github_user": ADMIN,
        "selected_repos": [], "workflows": [],
    })

    assert resp.status_code == 200, resp.text
    db = TestingSessionLocal()
    try:
        owner = (db.query(Account.github_user)
                 .join(Project, Project.user_id == Account.user_id)
                 .filter(Project.project_name == "gamma").scalar())
    finally:
        db.close()
    assert owner == OTHER


# The routes whose github_user names a project owner (#2241). They trusted
# X-GitHub-User for the caller and fell back to the named owner's projects, so
# with no workspace members, where the middleware checks nothing, naming an
# owner was enough to read, change or delete their project without a session.

@pytest.fixture
def two_accounts():
    """ADMIN's "delta", whose id this returns, and OTHER's own "delta"."""
    db = TestingSessionLocal()
    try:
        owner = _account(db, ADMIN)
        other = _account(db, OTHER)
        _project(db, other, "delta", code="ODE")
        return _project(db, owner, "delta").project_id
    finally:
        db.close()


def _project_exists(project_id):
    db = TestingSessionLocal()
    try:
        return db.get(Project, project_id) is not None
    finally:
        db.close()


def test_without_a_session_naming_the_owner_deletes_nothing(two_accounts):
    resp = client.delete("/api/projects/delta", params={"github_user": ADMIN})

    assert resp.status_code == 401, resp.text
    assert _project_exists(two_accounts)


def test_without_a_session_the_owner_header_exports_nothing(two_accounts):
    resp = client.get(f"/api/projects/{two_accounts}/backup-export", headers={"X-GitHub-User": ADMIN})

    assert resp.status_code == 401, resp.text


def _owner_routes(pid):
    """Every route whose github_user names a project owner, naming ADMIN."""
    named = {"github_user": ADMIN}
    return {
        "get": ("GET", "/api/projects/delta", {"params": named}),
        "delete": ("DELETE", "/api/projects/delta", {"params": named}),
        "update": ("PUT", f"/api/projects/{pid}/",
                   {"json": {**named, "project_name": "taken", "selected_repos": [], "workflows": []}}),
        "rename": ("PATCH", f"/api/projects/{pid}/project-name", {"json": {**named, "project_name": "taken"}}),
        "color": ("PATCH", f"/api/projects/{pid}/project-color", {"json": {**named, "project_color": "rose"}}),
        "drift-config": ("PATCH", f"/api/projects/{pid}/drift-config",
                         {"json": {**named, "drift_check_interval_minutes": None}}),
        "link": ("POST", "/api/projects/delta/linked-reusable-workflows",
                 {"json": {**named, "workflow_id": 1, "rwx_project_id": 1}}),
        "unlink": ("DELETE", "/api/projects/delta/linked-reusable-workflows/1", {"params": named}),
        "update-linked": ("PUT", "/api/projects/delta/linked-reusable-workflows/1",
                          {"json": {**named, "content": "name: x"}}),
    }


@pytest.mark.parametrize("route", sorted(_owner_routes(0)))
def test_without_members_naming_another_owner_is_refused(two_accounts, authenticate_client, route):
    """Refused, not quietly read as the caller's own project of that name:
    OTHER owns a "delta" too, and neither may change."""
    authenticate_client(client, OTHER, TestingSessionLocal)
    method, path, kwargs = _owner_routes(two_accounts)[route]

    resp = client.request(method, path, **kwargs)

    assert resp.status_code == 403, resp.text
    db = TestingSessionLocal()
    try:
        rows = db.query(Project.project_code, Project.project_name, Project.project_color,
                        Project.reusable_workflows_enabled).order_by(Project.project_code).all()
        assert [tuple(r) for r in rows] == [("DEL", "delta", None, False), ("ODE", "delta", None, False)]
    finally:
        db.close()


def test_an_admin_can_still_name_the_owner(workspace, authenticate_client):
    """An admin's github_user still picks between same-named projects."""
    db = TestingSessionLocal()
    try:
        reader = db.query(Account).filter(Account.github_user == READER).one()
        readers_alpha = _project(db, reader, "alpha", code="RAL").project_id
    finally:
        db.close()
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = client.get("/api/projects/alpha", params={"github_user": READER})

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == readers_alpha
