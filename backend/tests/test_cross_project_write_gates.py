"""A signed-in user cannot act on a project they only see.

Each route here authorized one thing and acted on another: a project owner
taken from the request body, a project re-resolved by a global name match after
the access check had passed, or a removal that skipped the editor check on its
early-return path. Every test signs in as a real session so the middleware and
the route's own checks both run, and asserts on what the database still holds.
"""
import sys
import os
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (
    Base, Account, CustomFile, Project, ProjectEnvVar, ProjectRepo, ProjectWorkflow, Repo,
    Workflow, WorkspaceMember,
)
from main import app
from database import get_db

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "gate-owner"
MEMBER = "gate-member"
READER = "gate-reader"
PROJECT = "Payments"
RWX_YAML = "on: workflow_call\njobs: {secret_owner_job: {runs-on: ubuntu-latest}}\n"


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _account(db, name, role):
    account = Account(github_user=name, github_email=f"{name}@e.com", account_type="pro")
    db.add(account)
    db.flush()
    db.add(WorkspaceMember(user_id=account.user_id, workspace_role=role))
    return account


def _seed():
    """An owner's project, a full member with no grant on it, and a read_only user."""
    db = TestingSessionLocal()
    try:
        owner = _account(db, OWNER, "member")
        _account(db, MEMBER, "member")
        _account(db, READER, "read_only")
        project = Project(
            project_name=PROJECT, project_code="PAY", user_id=owner.user_id,
            project_type="standard", use_prefix=False,
        )
        db.add(project)
        db.commit()
        return project.project_id
    finally:
        db.close()


def _client_as(authenticate_client, username):
    client = TestClient(app)
    authenticate_client(client, username, TestingSessionLocal)
    return client


@pytest.fixture
def signed_in(authenticate_client):
    """Returns a factory for clients signed in as a given user."""
    app.dependency_overrides[get_db] = override_get_db
    original = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestingSessionLocal
    with mock.patch("mode_validation.validate_startup_configuration"):
        yield lambda username: _client_as(authenticate_client, username)
    app.state.middleware_db_factory = original
    app.dependency_overrides.pop(get_db, None)


def _project_exists(project_id):
    db = TestingSessionLocal()
    try:
        return db.query(Project).filter_by(project_id=project_id).first() is not None
    finally:
        db.close()


# --- DELETE /api/projects/{name}/enhanced -----------------------------------

def test_a_member_cannot_delete_another_users_project_by_naming_its_owner(signed_in):
    project_id = _seed()

    resp = signed_in(MEMBER).request("DELETE", f"/api/projects/{PROJECT}/enhanced", json={
        "github_user": OWNER, "project_name": PROJECT, "delete_github_resources": False,
    })

    # The body's name is ignored (#2241): the member's own projects hold no "Payments".
    assert resp.status_code == 404, resp.text
    assert _project_exists(project_id)


def test_naming_someone_else_in_the_query_is_refused(signed_in):
    """auth.current_account owns the legacy github_user query parameter (#2241)."""
    project_id = _seed()

    resp = signed_in(OWNER).request(
        "DELETE", f"/api/projects/{PROJECT}/enhanced", params={"github_user": MEMBER},
        json={"github_user": OWNER, "project_name": PROJECT, "delete_github_resources": False},
    )

    assert resp.status_code == 403, resp.text
    assert _project_exists(project_id)


def test_the_owner_can_still_delete_their_own_project(signed_in):
    project_id = _seed()

    resp = signed_in(OWNER).request("DELETE", f"/api/projects/{PROJECT}/enhanced", json={
        "project_name": PROJECT, "delete_github_resources": False,
    })

    assert resp.status_code == 200, resp.text
    assert not _project_exists(project_id)


def test_a_name_in_the_body_is_ignored(signed_in):
    """The caller comes from the session (#2241), not a github_user the body still carries."""
    project_id = _seed()

    resp = signed_in(OWNER).request("DELETE", f"/api/projects/{PROJECT}/enhanced", json={
        "github_user": MEMBER, "project_name": PROJECT, "delete_github_resources": False,
    })

    assert resp.status_code == 200, resp.text
    assert not _project_exists(project_id)


# --- POST /api/update-project-files ----------------------------------------------

def _update_workflow(client, repo_name="gate-member/own-repo"):
    return client.post("/api/update-project-files", json={
        "project_name": PROJECT,
        "repo_names": [repo_name],
        "workflows": [{"name": "deploy", "content": "on: push\n"}],
    })


@mock.patch("workflows.create_pull_requests", return_value={"results": {}})
def test_a_member_without_editor_cannot_update_another_projects_workflows(campaign, signed_in):
    _seed()

    with mock.patch("workflows.user_tokens", {MEMBER: "member-token"}):
        resp = _update_workflow(signed_in(MEMBER))

    assert resp.status_code == 403, resp.text
    campaign.assert_not_called()


@mock.patch("workflows.create_pull_requests", return_value={"results": {}})
def test_the_owner_still_reaches_the_workflow_update(campaign, signed_in):
    project_id = _seed()
    # A repository of the project's own: save-and-push writes nowhere else (#2316).
    db = TestingSessionLocal()
    try:
        repo = Repo(repo_name="gate-owner/app")
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project_id, repo_id=repo.repo_id))
        db.commit()
    finally:
        db.close()

    with mock.patch("workflows.user_tokens", {OWNER: "owner-token"}):
        resp = _update_workflow(signed_in(OWNER), "gate-owner/app")

    assert resp.status_code == 200, resp.text
    campaign.assert_called_once()


# --- DELETE /api/projects/{id}/custom-files/{file_id}?scope=project ---------

@mock.patch("custom_files.user_tokens", {MEMBER: "member-token"})
def test_a_member_without_editor_cannot_hard_delete_a_custom_file(signed_in):
    project_id = _seed()
    db = TestingSessionLocal()
    try:
        cf = CustomFile(project_id=project_id, file_path=".github/CODEOWNERS", file_content="* @owner")
        db.add(cf)
        db.commit()
        file_id = cf.id
    finally:
        db.close()

    resp = signed_in(MEMBER).delete(
        f"/api/projects/{project_id}/custom-files/{file_id}", params={"scope": "project"},
    )

    assert resp.status_code == 403, resp.text
    db = TestingSessionLocal()
    try:
        assert db.query(CustomFile).filter_by(id=file_id).first() is not None
    finally:
        db.close()


# --- GET /api/rwx-workflows -------------------------------------------------

def _seed_owner_rwx_project():
    db = TestingSessionLocal()
    try:
        owner = db.query(Account).filter_by(github_user=OWNER).one()
        rwx = Project(
            project_name="Shared RWX", project_code="RWX", user_id=owner.user_id,
            project_type="rwx",
        )
        workflow = Workflow(workflow_name="build", workflow_yaml=RWX_YAML, reusable_workflow=True)
        db.add_all([rwx, workflow])
        db.flush()
        db.add(ProjectWorkflow(project_id=rwx.project_id, workflow_id=workflow.workflow_id))
        db.commit()
    finally:
        db.close()


def test_read_only_cannot_list_another_users_reusable_workflows(signed_in):
    _seed()
    _seed_owner_rwx_project()

    resp = signed_in(READER).get("/api/rwx-workflows")
    named = signed_in(READER).get("/api/rwx-workflows", params={"github_user": OWNER})

    assert resp.status_code == 200, resp.text
    assert "secret_owner_job" not in resp.text
    # Naming the owner does not borrow their view (#2241).
    assert named.status_code == 403, named.text
    assert "secret_owner_job" not in named.text


def test_the_owner_still_lists_their_own_reusable_workflows(signed_in):
    _seed()
    _seed_owner_rwx_project()

    resp = signed_in(OWNER).get("/api/rwx-workflows", params={"github_user": OWNER})

    assert resp.status_code == 200, resp.text
    assert "secret_owner_job" in resp.text


# --- env-var routes: authorize project A, then act on same-named project B ---

def _seed_same_named_projects():
    """The victim's project is older, so a bare name match returns it first."""
    victim_project_id = _seed()
    db = TestingSessionLocal()
    try:
        member = db.query(Account).filter_by(github_user=MEMBER).one()
        own = Project(
            project_name=PROJECT, project_code="MINE", user_id=member.user_id,
            project_type="standard", use_prefix=False,
        )
        db.add(own)
        db.add(ProjectEnvVar(project_id=victim_project_id, env_var_name="DEPLOY_ROLE"))
        db.commit()
        return victim_project_id, own.project_id
    finally:
        db.close()


def _env_var_names(project_id):
    db = TestingSessionLocal()
    try:
        return {
            row.env_var_name
            for row in db.query(ProjectEnvVar).filter_by(project_id=project_id).all()
        }
    finally:
        db.close()


@mock.patch("github_env_vars.user_tokens", {MEMBER: "member-token"})
def test_update_env_vars_writes_to_the_project_that_was_authorized(signed_in):
    victim_id, own_id = _seed_same_named_projects()

    resp = signed_in(MEMBER).post("/api/update-env-vars", json={
        "user": MEMBER, "project_name": PROJECT, "repo_names": [],
        "env": [{"key": "PLANTED", "value": "x"}],
    })

    assert resp.status_code == 200, resp.text
    assert _env_var_names(victim_id) == {"DEPLOY_ROLE"}
    assert "PLANTED" in _env_var_names(own_id)


@mock.patch("github_env_vars.user_tokens", {MEMBER: "member-token"})
def test_delete_env_vars_cannot_reach_a_same_named_project(signed_in):
    victim_id, _own_id = _seed_same_named_projects()

    resp = signed_in(MEMBER).request("DELETE", "/api/delete-env-vars", json={
        "user": MEMBER, "project_name": PROJECT, "repo_names": [],
        "env": [{"env_key": "DEPLOY_ROLE"}],
    })

    assert resp.status_code == 200, resp.text
    assert _env_var_names(victim_id) == {"DEPLOY_ROLE"}
