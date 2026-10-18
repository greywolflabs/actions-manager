"""A local edit resolves only the drift it adopts.

Drift is GitHub's copy on a (repo, branch) differing from what ActionsManager
delivered there, so a local edit, which changes neither, leaves it reported:
the workflow's status shows the edit. The one exception is an edit that makes
ActionsManager's copy exactly GitHub's, the blob the last check saw there:
GitHub then holds what ActionsManager would deliver, so that drift is resolved
at once and the blob recorded as delivered. The same for every path that
edits a workflow: Commit Locally (and the project save and import, which share
its code), restoring a version, and editing a linked reusable workflow.

Editing used to zero the hash and resolve every drift of the workflow, and a
drift check then read any difference from GitHub as the edit pending sync,
hiding a delivered file someone changed on GitHub.
"""

import os
import sys
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import app  # noqa: E402
from projects import get_db as projects_get_db  # noqa: E402
from workflows import get_db as workflows_get_db  # noqa: E402
from models import (  # noqa: E402
    Base, Account, FileDelivery, LinkedReusableWorkflow, Project, ProjectRepo, ProjectWorkflow,
    Repo, RepoWorkflowOverride, Workflow, WorkflowVersion,
)
from auth import user_tokens  # noqa: E402
from deliveries import blob_sha  # noqa: E402
from tests.delivery_records import record_delivered, record_drift  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
client = TestClient(app)

SYNCED_HASH = "a" * 40
ORIGINAL = "name: ci\non: push"
EDITED = "name: ci\non: [push, pull_request]"


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_workflow(db, project, name, yaml=ORIGINAL, reusable=False):
    wf = Workflow(workflow_name=name, workflow_yaml=yaml, workflow_git_hash=SYNCED_HASH,
                  reusable_workflow=reusable, workflow_status="synced_with_github")
    db.add(wf)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=wf.workflow_id))
    return wf


def _add_repo(db, project, name):
    repo = Repo(repo_name=name)
    db.add(repo)
    db.flush()
    db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    return repo


@pytest.fixture
def state(authenticate_client):
    app.dependency_overrides[workflows_get_db] = _override_get_db
    app.dependency_overrides[projects_get_db] = _override_get_db
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        user = Account(github_user="alice", github_email="a@e.com", account_type="pro")
        db.add(user)
        db.flush()
        project = Project(project_name="proj", project_code="P001", user_id=user.user_id,
                          use_prefix=False, branch_option="default", project_type="standard")
        db.add(project)
        db.flush()
        api = _add_repo(db, project, "acme/api")
        web = _add_repo(db, project, "acme/web")
        ci = _add_workflow(db, project, "ci")
        lint = _add_workflow(db, project, "lint")
        db.commit()
        user_tokens["alice"] = "tok"
        authenticate_client(client, "alice", TestingSessionLocal)
        yield {"db": db, "project": project, "ci": ci, "lint": lint, "api": api, "web": web}
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        user_tokens.clear()
        app.dependency_overrides.pop(workflows_get_db, None)
        app.dependency_overrides.pop(projects_get_db, None)


def _drifted(db, workflow, repo, branch="main", github_content=EDITED):
    """A delivery to (repo, branch), and the check that found GitHub holding *github_content* there."""
    record_delivered(db, workflow, repo.repo_name, branch, sha=SYNCED_HASH)
    record_drift(db, workflow, repo.repo_name, branch, github_sha=blob_sha(github_content))


def _drift(project_id):
    """What the page renders: the stored drift, read without calling GitHub."""
    resp = client.get(f"/api/projects/{project_id}/drift")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _drifted_pairs(project_id):
    return sorted(
        (d["workflow_name"], d["repo"], d["branch"])
        for d in _drift(project_id)["drifted_workflows"]
    )


def _commit_locally(name, content, project_name="proj"):
    resp = client.post("/api/save-workflows", json={
        "project_name": project_name,
        "workflows": [{"name": name, "content": content}],
    })
    assert resp.status_code == 200, resp.text


def _project_summary(project_id):
    db = TestingSessionLocal()
    try:
        project = db.query(Project).filter_by(project_id=project_id).one()
        return project.drift_status, project.drift_count
    finally:
        db.close()


class TestCommitLocally:
    def test_an_edit_to_github_s_copy_resolves_its_drift_everywhere_at_once(self, state):
        pid = state["project"].project_id
        for repo in (state["api"], state["web"]):
            _drifted(state["db"], state["ci"], repo)
        _drifted(state["db"], state["ci"], state["api"], branch="release/1.0")
        assert len(_drifted_pairs(pid)) == 3

        _commit_locally("ci", EDITED)

        assert _drift(pid)["drift_count"] == 0
        assert _project_summary(pid) == ("clean", 0)

    def test_any_other_edit_leaves_drift_reported(self, state):
        """A local edit used to resolve every drift of the workflow, and the
        zeroed hash then read any later difference as the edit pending sync, even
        a delivered file someone changed on GitHub. GitHub still holds a copy
        ActionsManager never delivered: still drift."""
        pid = state["project"].project_id
        _drifted(state["db"], state["ci"], state["api"], github_content="name: ci\non: [push]\n# hotfix")

        _commit_locally("ci", EDITED)

        assert _drifted_pairs(pid) == [("ci", "acme/api", "main")]
        assert _drift(pid)["drift_count"] == 1

    def test_adopting_github_s_copy_records_it_as_delivered(self, state):
        _drifted(state["db"], state["ci"], state["api"])

        _commit_locally("ci", EDITED)

        db = TestingSessionLocal()
        try:
            row = db.query(FileDelivery).filter_by(workflow_id=state["ci"].workflow_id).one()
            assert (row.delivered_sha, row.has_drift) == (blob_sha(EDITED), False)
        finally:
            db.close()

    def test_adopting_github_s_copy_ending_in_a_newline_resolves_its_drift(self, state):
        """GitHub's editor ends a file with a newline and a save strips it, so
        Load GitHub version then Commit Locally never saves GitHub's exact blob."""
        pid = state["project"].project_id
        _drifted(state["db"], state["ci"], state["api"], github_content=EDITED + "\n")

        _commit_locally("ci", EDITED + "\n")

        assert _drift(pid)["drift_count"] == 0
        db = TestingSessionLocal()
        try:
            row = db.query(FileDelivery).filter_by(workflow_id=state["ci"].workflow_id).one()
            assert row.delivered_sha == blob_sha(EDITED + "\n")
        finally:
            db.close()

    def test_leaves_the_project_s_other_workflows_drifted(self, state):
        pid = state["project"].project_id
        _drifted(state["db"], state["ci"], state["api"])
        _drifted(state["db"], state["lint"], state["api"])

        _commit_locally("ci", EDITED)

        assert _drifted_pairs(pid) == [("lint", "acme/api", "main")]
        assert _project_summary(pid) == ("drifted", 1)

    def test_a_save_that_changes_nothing_is_not_a_local_edit(self, state):
        """A project save posts every workflow; an unchanged one resolves nothing."""
        pid = state["project"].project_id
        _drifted(state["db"], state["ci"], state["api"], github_content=ORIGINAL)

        _commit_locally("ci", ORIGINAL)

        assert _drifted_pairs(pid) == [("ci", "acme/api", "main")]

    def test_a_repo_with_its_own_override_keeps_its_drift(self, state):
        """Drift there is judged against the override, which editing the workflow does not change."""
        db, project = state["db"], state["project"]
        db.add(RepoWorkflowOverride(
            project_id=project.project_id, workflow_id=state["ci"].workflow_id,
            repo_id=state["web"].repo_id, workflow_name="ci",
            workflow_yaml="name: ci\non: workflow_dispatch",
            workflow_git_hash="c" * 40,
        ))
        db.commit()
        _drifted(db, state["ci"], state["api"])
        _drifted(db, state["ci"], state["web"])

        _commit_locally("ci", EDITED)

        assert _drifted_pairs(project.project_id) == [("ci", "acme/web", "main")]

    def test_the_resolution_is_notified_once(self, state):
        from models import NotificationEvent

        _drifted(state["db"], state["ci"], state["api"])

        _commit_locally("ci", EDITED)
        _commit_locally("ci", EDITED + "\n# again")

        db = TestingSessionLocal()
        try:
            resolved = db.query(NotificationEvent).filter_by(event_type="drift.resolved").all()
        finally:
            db.close()
        assert len(resolved) == 1


def _restore(content, state):
    db = state["db"]
    db.add(WorkflowVersion(workflow_id=state["ci"].workflow_id, version_number=1, content=content))
    db.commit()
    version_id = db.query(WorkflowVersion.version_id).scalar()
    resp = client.post("/api/workflows/restore-version", json={
        "project_name": "proj", "workflow_name": "ci", "version_id": version_id,
    })
    assert resp.status_code == 200, resp.text


def test_restoring_github_s_copy_resolves_its_drift(state):
    _drifted(state["db"], state["ci"], state["api"])

    _restore(EDITED, state)

    assert _drift(state["project"].project_id)["drift_count"] == 0


def test_restoring_another_version_leaves_drift_reported(state):
    _drifted(state["db"], state["ci"], state["api"])

    _restore("name: ci\non: workflow_dispatch", state)

    assert _drift(state["project"].project_id)["drift_count"] == 1


def test_editing_a_linked_reusable_workflow_resolves_drift_in_its_own_project(state):
    """The workflow, and its drift, belong to the Reusable Workflow Project, not the caller."""
    db = state["db"]
    rwx = Project(project_name="shared", project_code="RWX1", user_id=state["project"].user_id,
                  use_prefix=False, branch_option="default", project_type="rwx")
    db.add(rwx)
    db.flush()
    rwx_repo = _add_repo(db, rwx, "acme/shared-workflows")
    build = _add_workflow(db, rwx, "build", yaml="on: workflow_call", reusable=True)
    db.add(LinkedReusableWorkflow(standard_project_id=state["project"].project_id,
                                  rwx_project_id=rwx.project_id, workflow_id=build.workflow_id))
    db.commit()
    adopted = "on:\n  workflow_call:\n    inputs: {}"
    _drifted(db, build, rwx_repo, github_content=adopted)

    resp = client.put(f"/api/projects/proj/linked-reusable-workflows/{build.workflow_id}",
                      json={"content": adopted})

    assert resp.status_code == 200, resp.text
    assert _drift(rwx.project_id)["drift_count"] == 0
    assert _project_summary(rwx.project_id) == ("clean", 0)


def test_the_saved_workflow_is_committed_locally_with_no_hash(state):
    """No blob SHA for content GitHub has not been given; the status shows the edit."""
    _drifted(state["db"], state["ci"], state["api"])

    _commit_locally("ci", EDITED)

    db = TestingSessionLocal()
    try:
        wf = db.query(Workflow).filter_by(workflow_id=state["ci"].workflow_id).one()
        assert (wf.workflow_git_hash, wf.workflow_status) == (None, "committed_locally")
    finally:
        db.close()
