"""The workflow save, delete, count and version routes act as the session's
caller (#2241).

The counts and version history took the caller from a `user` query parameter,
restore-version from a `github_user` body field, and the delete and update
routes fell back to a `user` in the body whenever
X-GitHub-User was absent, which it is on an install with no workspace members
(the middleware only pins it while membership is enforced). That is the setup
here: bob is signed in and names alice, who owns the project.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from database import get_db
from models import Base, Account, Project, ProjectWorkflow, Workflow, WorkflowVersion

engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "alice"
OTHER = "bob"
PROJECT = "payments"
ORIGINAL = "name: ci\non: push\n"
OLDER = "name: ci\non: pull_request\n"


def _override():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def signed_in(authenticate_client):
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    owner = Account(github_user=OWNER, github_email="a@example.com", account_type="free")
    other = Account(github_user=OTHER, github_email="b@example.com", account_type="free")
    db.add_all([owner, other])
    db.commit()
    project = Project(project_name=PROJECT, project_code="PAY", user_id=owner.user_id,
                      branch_option="default", use_prefix=True, pr_state="synced")
    workflow = Workflow(workflow_name="ci", workflow_yaml=ORIGINAL, reusable_workflow=False,
                        workflow_git_hash="a" * 40, workflow_status="synced_with_github")
    db.add_all([project, workflow])
    db.commit()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    version = WorkflowVersion(workflow_id=workflow.workflow_id, version_number=1, content=OLDER)
    db.add(version)
    db.commit()
    ids = {"workflow": workflow.workflow_id, "version": version.version_id}
    db.close()

    app.dependency_overrides[get_db] = _override
    original = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestingSessionLocal

    def _as(user):
        client = TestClient(app)
        authenticate_client(client, user, TestingSessionLocal)
        return client

    tokens = {OWNER: "alice-token", OTHER: "bob-token"}
    with patch.dict("auth.user_tokens", tokens):
        yield _as, ids
    app.state.middleware_db_factory = original
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)


def _workflow_yaml(workflow_id):
    db = TestingSessionLocal()
    try:
        workflow = db.get(Workflow, workflow_id)
        return workflow.workflow_yaml if workflow else None
    finally:
        db.close()


def test_a_user_query_no_longer_reads_someone_elses_history(signed_in):
    as_user, _ids = signed_in

    resp = as_user(OTHER).get(
        "/api/workflows/ci/versions", params={"user": OWNER, "project_name": PROJECT},
    )

    assert resp.status_code == 404, resp.text
    assert OLDER not in resp.text


def test_naming_another_user_in_the_body_does_not_restore_as_them(signed_in):
    as_user, ids = signed_in

    resp = as_user(OTHER).post("/api/workflows/restore-version", json={
        "github_user": OWNER, "project_name": PROJECT,
        "workflow_name": "ci", "version_id": ids["version"],
    })

    assert resp.status_code == 404, resp.text
    assert _workflow_yaml(ids["workflow"]) == ORIGINAL


def test_naming_another_user_in_the_body_does_not_delete_as_them(signed_in):
    as_user, ids = signed_in

    resp = as_user(OTHER).request("DELETE", "/api/delete-db-workflow", json={
        "user": OWNER, "project_name": PROJECT, "workflow_name": "ci",
    })

    assert resp.status_code == 404, resp.text
    assert _workflow_yaml(ids["workflow"]) == ORIGINAL
