"""A project's owner passes the editor checks on their own project (#2337).

Owners get no ProjectMembership row: those are only created by the admin
"add project member" route. With workspace membership in use, a non-admin
member's access to any project comes from check_project_access, which knows
nothing about ownership and answers project_viewer. So every project write in
projects.py refused the member who created the project, except the rename,
which let owners through by hand. workflows.py and custom_files.py already
treated the owner as an editor.

Here the owner is a plain workspace member with no membership row. Each write
must let them through, still refuse a non-owner viewer, and still accept a
non-owner editor.
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (  # noqa: E402
    Account, Base, Project, ProjectMembership, ProjectRepo, Repo, WorkspaceMember,
)
from main import app  # noqa: E402
from projects import get_db  # noqa: E402
from auth import create_auth_session  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "own-owner"
VIEWER = "own-viewer"
EDITOR = "own-editor"
READ_ONLY_OWNER = "own-readonly"
PROJECT = "Owned"


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def ids():
    app.dependency_overrides[get_db] = override_get_db
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        owner, viewer, editor, read_only_owner = (
            Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
            for name in (OWNER, VIEWER, EDITOR, READ_ONLY_OWNER)
        )
        db.add_all([owner, viewer, editor, read_only_owner])
        db.commit()
        project = Project(project_name=PROJECT, project_code="OWN", user_id=owner.user_id,
                          branch_option="default", project_type="standard")
        read_only_project = Project(project_name="Demoted", project_code="DEM", user_id=read_only_owner.user_id,
                                    branch_option="default", project_type="standard")
        repo = Repo(repo_name=f"{OWNER}/service")
        db.add_all([project, read_only_project, repo])
        db.commit()
        db.add_all([
            WorkspaceMember(user_id=owner.user_id, workspace_role="member"),
            WorkspaceMember(user_id=viewer.user_id, workspace_role="member"),
            WorkspaceMember(user_id=editor.user_id, workspace_role="member"),
            WorkspaceMember(user_id=read_only_owner.user_id, workspace_role="read_only"),
            ProjectMembership(user_id=viewer.user_id, project_id=project.project_id, project_role="project_viewer"),
            ProjectMembership(user_id=editor.user_id, project_id=project.project_id, project_role="project_editor"),
            ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id),
        ])
        db.commit()
        yield {"project": project.project_id, "repo": repo.repo_id, "read_only_project": read_only_project.project_id}
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.pop(get_db, None)


def _as(caller):
    db = TestingSessionLocal()
    try:
        token = create_auth_session(caller, db)
    finally:
        db.close()
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def _save_payload():
    return {
        "github_user": OWNER, "project_name": PROJECT, "selected_repos": [f"{OWNER}/service"],
        "workflows": [], "branch_regex": "release/.*", "branch_option": "default",
        "branch_max_age_days": 30, "reusable_workflows_enabled": False, "use_prefix": False,
    }


# Each write: how to send it, and what it answers once the editor check lets
# the caller through. Linking, unlinking and updating a link answer 404 for the
# RWX project and the link that don't exist here, reached only past the check.
WRITES = {
    "save": (lambda c, i: c.put(f"/api/projects/{i['project']}/", json=_save_payload()), 200),
    "rename": (lambda c, i: c.patch(f"/api/projects/{i['project']}/project-name",
                                    json={"github_user": OWNER, "project_name": "Renamed"}), 200),
    "color": (lambda c, i: c.patch(f"/api/projects/{i['project']}/project-color",
                                   json={"github_user": OWNER, "project_color": "amber"}), 200),
    "drift config": (lambda c, i: c.patch(f"/api/projects/{i['project']}/drift-config",
                                          json={"github_user": OWNER, "drift_check_interval_minutes": None}), 200),
    "link reusable": (lambda c, i: c.post(f"/api/projects/{PROJECT}/linked-reusable-workflows",
                                          json={"github_user": OWNER, "workflow_id": 1, "rwx_project_id": 999}), 404),
    "unlink reusable": (lambda c, i: c.delete(f"/api/projects/{PROJECT}/linked-reusable-workflows/1",
                                              params={"github_user": OWNER}), 404),
    "update linked reusable": (lambda c, i: c.put(f"/api/projects/{PROJECT}/linked-reusable-workflows/1",
                                                  json={"github_user": OWNER, "content": "on: workflow_call\n"}), 404),
    "repo branch config": (lambda c, i: c.patch(f"/api/projects/{i['project']}/repos/{i['repo']}/branch-config",
                                                json={"branch_config_mode": "inherit"}), 200),
    "reset repo branch config": (lambda c, i: c.delete(
        f"/api/projects/{i['project']}/repos/{i['repo']}/branch-config"), 200),
    "delete": (lambda c, i: c.delete(f"/api/projects/{PROJECT}", params={"github_user": OWNER}), 200),
}


@pytest.mark.parametrize("write", WRITES)
@pytest.mark.parametrize("caller", [OWNER, EDITOR])
def test_the_owner_and_an_editor_get_past_the_editor_check(ids, write, caller):
    send, expected = WRITES[write]

    resp = send(_as(caller), ids)

    assert resp.status_code == expected, resp.text


@pytest.mark.parametrize("write", WRITES)
def test_a_viewer_who_does_not_own_the_project_is_still_refused(ids, write):
    send, _ = WRITES[write]

    resp = send(_as(VIEWER), ids)

    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("caller, role", [
    (OWNER, "project_editor"),
    (EDITOR, "project_editor"),
    (VIEWER, "project_viewer"),
])
def test_the_project_page_opens_editable_for_its_owner(ids, caller, role):
    """The page reads caller_project_role and goes read-only on project_viewer,
    so an owner reported as a viewer could not use the writes above."""
    resp = _as(caller).get(f"/api/projects/{PROJECT}", params={"github_user": OWNER})

    assert resp.status_code == 200, resp.text
    assert resp.json()["caller_project_role"] == role


def test_a_read_only_owner_sees_their_project_but_cannot_change_it(ids):
    """read_only never writes, whoever created the project. The middleware
    refuses its writes before any route, and the route's own checks refuse them
    too. Owning the project still lets them see it (authorization.project_role)."""
    client = _as(READ_ONLY_OWNER)
    project_id = ids["read_only_project"]

    save = client.put(f"/api/projects/{project_id}/", json={**_save_payload(), "github_user": READ_ONLY_OWNER,
                                                           "project_name": "Demoted", "selected_repos": []})
    read = client.get(f"/api/projects/{project_id}/repo-branch-configs")

    assert save.status_code == 403, save.text
    assert read.status_code == 200, read.text
