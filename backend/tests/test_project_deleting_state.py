"""A project being deleted: read-only while its removal PRs are open, gone once they all merge.

A deletion that removes the project's files from GitHub opens one removal PR
per (repo, branch) the delivery record holds, and the project waits in
pr_state "deleting". Merging the last of them deletes the project; closing any
of them calls the deletion off, and the project and its files stay. These
drive the outcomes the way users do, through the merge and close buttons and
the page load, and read the result from the database.
"""
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deliveries  # noqa: E402
import drift_worker  # noqa: E402
import workflows as wf  # noqa: E402
from authorization import PROJECT_DELETING, has_project_role  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Account,
    Base,
    FileDelivery,
    LinkedReusableWorkflow,
    Project,
    ProjectPRCampaign,
    ProjectPullRequest,
    ProjectRepo,
    ProjectWorkflow,
    Repo,
    Workflow,
    WorkspaceMember,
)
from projects import get_db as projects_get_db  # noqa: E402
from tests.delivery_records import removed  # noqa: E402
from workflows import PROJECT_DELETED_DETAIL, get_db as workflows_get_db  # noqa: E402

OWNER = "doomed-owner"
VIEWER = "doomed-viewer"
PROJECT = "doomed"
REPO_A = "doomed-owner/a"
REPO_B = "doomed-owner/b"
CI_PATH = ".github/workflows/AM_DMD_ci.yml"
MAIN = "main"
WORKFLOW_NAME = "ci"
WORKFLOW_YAML = "on: push\n"
PROJECT_URL = f"/api/projects/{PROJECT}"
PROJECT_BODY = {"project_name": PROJECT}

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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


@pytest.fixture
def client(authenticate_client):
    app.dependency_overrides[projects_get_db] = override_get_db
    app.dependency_overrides[workflows_get_db] = override_get_db
    original_factory = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestingSessionLocal
    with patch("mode_validation.validate_startup_configuration"):
        with TestClient(app) as c:
            c.sign_in = lambda name: authenticate_client(c, name, TestingSessionLocal)
            c.sign_in(OWNER)
            yield c
    app.state.middleware_db_factory = original_factory
    app.dependency_overrides.pop(projects_get_db, None)
    app.dependency_overrides.pop(workflows_get_db, None)


def _seed(*, also_delivered_to=None):
    """*PROJECT*, being deleted: ``ci`` delivered to main of REPO_A and REPO_B, and
    removal PR #1 (REPO_A) and #2 (REPO_B) open, each carrying its removal.

    *also_delivered_to* is a (repo, branch) the record holds ``ci`` on that no
    removal PR covers. VIEWER is a full member, so sees the project but never edits it.
    """
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email="o@example.com", account_type="free")
        viewer = Account(github_user=VIEWER, github_email="v@example.com", account_type="free")
        db.add_all([owner, viewer])
        db.flush()
        db.add_all([WorkspaceMember(user_id=owner.user_id, workspace_role="member"),
                    WorkspaceMember(user_id=viewer.user_id, workspace_role="member")])
        project = Project(project_name=PROJECT, project_code="DMD", user_id=owner.user_id,
                          project_type="standard", pr_state=PROJECT_DELETING)
        workflow = Workflow(workflow_name=WORKFLOW_NAME, workflow_yaml=WORKFLOW_YAML,
                            workflow_status="synced_with_github")
        repos = [Repo(repo_name=REPO_A), Repo(repo_name=REPO_B)]
        db.add_all([project, workflow, *repos])
        db.flush()
        db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
        db.add_all([ProjectRepo(project_id=project.project_id, repo_id=r.repo_id) for r in repos])
        db.flush()
        for repo, branch in [(REPO_A, MAIN), (REPO_B, MAIN), *([also_delivered_to] if also_delivered_to else [])]:
            deliveries.record_delivered(db, deliveries.WORKFLOW, workflow.workflow_id, repo, branch, CI_PATH, "a" * 40)
        campaign = ProjectPRCampaign(project_id=project.project_id, campaign_name=f"Delete {PROJECT}")
        db.add(campaign)
        db.flush()
        for number, repo in ((1, REPO_A), (2, REPO_B)):
            pr = ProjectPullRequest(
                project_id=project.project_id, campaign_id=campaign.campaign_id, repo_name=repo,
                pr_number=number, pr_url=f"https://github.com/{repo}/pull/{number}", pr_state="open",
                branch_name=f"actions-manager/dmd/{number}-main", target_branch=MAIN,
                workflow_names=WORKFLOW_NAME,
            )
            db.add(pr)
            db.flush()
            deliveries.record_pending(db, pr, removed(workflow))
        db.commit()
        return project.project_id
    finally:
        db.close()


def _response(status, payload):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


def _signed_in_to_github():
    return patch("workflows.user_tokens", {OWNER: "tok", VIEWER: "tok"})


def _merge(client, repo, number, branch_deleter=None):
    deleter = branch_deleter or MagicMock(return_value=(True, None))
    with _signed_in_to_github(), \
            patch("workflows._merge_pull_request_with_fallback",
                  return_value=(_response(200, {"sha": "m"}), "merge", None)), \
            patch("workflows._delete_actions_manager_branch", deleter):
        return client.put("/api/merge-pull-request", json={**PROJECT_BODY, "repo_name": repo, "pr_number": number})


def _close(client, repo, number):
    with _signed_in_to_github(), \
            patch("workflows.github_patch", return_value=_response(200, {"state": "closed"})):
        return client.patch("/api/close-pull-request", json={**PROJECT_BODY, "repo_name": repo, "pr_number": number})


def _says_deleted(response) -> bool:
    return response.json()["project_deleted"]


def _project(db):
    return db.query(Project).filter_by(project_name=PROJECT).first()


def _workflow(db):
    return db.query(Workflow).filter_by(workflow_name=WORKFLOW_NAME).first()


def _delivered_on(db, repo, branch=MAIN):
    return deliveries.delivered_here(db, deliveries.WORKFLOW, _workflow(db).workflow_id, repo, branch)


def _pr_state(db, number):
    return db.query(ProjectPullRequest).filter_by(pr_number=number).one().pr_state


def test_a_closed_removal_pr_calls_off_while_others_are_open(client):
    _seed()

    assert _close(client, REPO_A, 1).status_code == 200

    db = TestingSessionLocal()
    try:
        project = _project(db)
        owner = db.query(Account).filter_by(github_user=OWNER).one()
        assert project.pr_state == "open", "called off, with removal PR #2 still open as an ordinary PR"
        assert _pr_state(db, 2) == "open"
        assert has_project_role(db, owner, project, "project_editor"), "the project is editable again"
        assert _delivered_on(db, REPO_A), "a closed removal leaves the file where it was"
    finally:
        db.close()

    assert _says_deleted(_merge(client, REPO_B, 2)) is False

    db = TestingSessionLocal()
    try:
        assert _project(db).pr_state == "draft", "a deletion once called off never resumes"
    finally:
        db.close()


def test_a_partial_deletion_leaves_removed_files_as_local_changes(client):
    _seed()

    assert _says_deleted(_merge(client, REPO_A, 1)) is False
    db = TestingSessionLocal()
    try:
        assert _project(db).pr_state == PROJECT_DELETING, "removal PR #2 is still open"
        assert not _delivered_on(db, REPO_A)
    finally:
        db.close()

    assert _close(client, REPO_B, 2).status_code == 200

    db = TestingSessionLocal()
    try:
        assert _project(db).pr_state == "draft"
        assert _workflow(db).workflow_status == "committed_locally", \
            "ci is gone from REPO_A, so it is not synced: it is there to deliver again"
        assert _delivered_on(db, REPO_B)
    finally:
        db.close()


def test_files_still_delivered_keep_the_project(client):
    _seed(also_delivered_to=(REPO_A, "release"))

    assert _says_deleted(_merge(client, REPO_A, 1)) is False
    assert _says_deleted(_merge(client, REPO_B, 2)) is False

    db = TestingSessionLocal()
    try:
        project = _project(db)
        assert project is not None, "the record still holds ci on REPO_A's release branch"
        assert project.pr_state == "draft"
        assert _delivered_on(db, REPO_A, "release")
        assert _workflow(db).workflow_status == "committed_locally"
    finally:
        db.close()


def test_the_merge_response_says_deleted_and_the_am_branch_is_still_cleaned(client):
    _seed()
    deleter = MagicMock(return_value=(True, None))

    first = _merge(client, REPO_A, 1, deleter)
    last = _merge(client, REPO_B, 2, deleter)

    assert _says_deleted(first) is False
    assert last.status_code == 200, last.text
    assert _says_deleted(last) is True
    assert last.json()["branch_deleted"] is True
    assert deleter.call_args.kwargs["branch_name"] == "actions-manager/dmd/2-main"
    assert deleter.call_args.kwargs["target_branch"] == MAIN
    db = TestingSessionLocal()
    try:
        assert _project(db) is None
        assert db.query(ProjectPullRequest).count() == 0
        assert db.query(FileDelivery).count() == 0
        assert _workflow(db) is None, "a workflow no other project holds goes with it"
    finally:
        db.close()


def test_the_merge_that_loses_a_race_to_the_webhook_still_says_deleted(client):
    """The merge button and GitHub's webhook for that merge settle the last
    removal PR at once: the slower finds the project gone and must not fail."""
    _seed()
    _merge(client, REPO_A, 1, MagicMock(return_value=(True, None)))
    slower, faster = TestingSessionLocal(), TestingSessionLocal()
    try:
        stale = slower.query(ProjectPullRequest).filter_by(pr_number=2).one()
        assert wf.apply_pr_outcome(faster, faster.query(ProjectPullRequest).filter_by(pr_number=2).one(),
                                   "merged") is True

        assert wf.apply_pr_outcome(slower, stale, "merged") is True
        assert _project(slower) is None
    finally:
        slower.close()
        faster.close()


def test_page_load_of_a_deleted_project_is_404(client):
    _seed()
    db = TestingSessionLocal()
    try:
        # Both merges reached the record by a path that did not settle the project.
        for pr in db.query(ProjectPullRequest).all():
            pr.pr_state, pr.merged_at = "merged", datetime.now(timezone.utc)
            deliveries.record_pr_outcome(db, pr, "merged")
        db.commit()
    finally:
        db.close()

    settled = client.get(PROJECT_URL, params={"github_user": OWNER})
    again = client.get(PROJECT_URL, params={"github_user": OWNER})

    assert settled.status_code == 404
    assert settled.json()["detail"] == PROJECT_DELETED_DETAIL
    assert again.status_code == 404
    assert again.json()["detail"] != PROJECT_DELETED_DETAIL, "it can no longer tell why"


def test_a_refresh_that_finds_the_last_removal_merged_says_deleted(client):
    _seed()
    assert _merge(client, REPO_A, 1).status_code == 200
    merged = _response(200, {"number": 2, "state": "closed", "merged": True, "merged_at": "2026-10-01T12:00:00Z"})

    with _signed_in_to_github(), patch("workflows.github_get", return_value=merged):
        response = client.get("/api/project-pr-status", params={**PROJECT_BODY, "refresh_from_github": "true"})

    assert response.status_code == 200, response.text
    assert response.json()["project_state"] == "deleted"
    db = TestingSessionLocal()
    try:
        assert _project(db) is None
    finally:
        db.close()


def test_a_linked_projects_deletion_leaves_the_reusable_workflow_project_standing(client):
    """An RWX project lists the PRs of the caller projects linking it, so its refresh
    can be the one that finds a caller's last removal PR merged. That deletion is
    the caller's: the RWX project's own status comes back."""
    _seed()
    db = TestingSessionLocal()
    try:
        owner = db.query(Account).filter_by(github_user=OWNER).one()
        shared = Project(project_name="shared", project_code="SHR", user_id=owner.user_id,
                         project_type="rwx", pr_state="synced")
        reusable = Workflow(workflow_name="build", workflow_yaml="on: workflow_call\n", reusable_workflow=True)
        db.add_all([shared, reusable])
        db.flush()
        db.add(ProjectWorkflow(project_id=shared.project_id, workflow_id=reusable.workflow_id))
        db.add(LinkedReusableWorkflow(standard_project_id=_project(db).project_id,
                                      rwx_project_id=shared.project_id, workflow_id=reusable.workflow_id))
        db.commit()
    finally:
        db.close()
    assert _merge(client, REPO_A, 1).status_code == 200
    merged = _response(200, {"number": 2, "state": "closed", "merged": True, "merged_at": "2026-10-01T12:00:00Z"})

    with _signed_in_to_github(), patch("workflows.github_get", return_value=merged):
        response = client.get("/api/project-pr-status", params={"project_name": "shared", "refresh_from_github": "true"})

    assert response.status_code == 200, response.text
    assert response.json()["project_state"] == "synced"
    assert response.json()["total_prs"] == 0, "the caller's PRs went with it"
    db = TestingSessionLocal()
    try:
        assert _project(db) is None
    finally:
        db.close()


def test_drift_sweep_skips_deleting_projects():
    _seed()
    db = TestingSessionLocal()
    try:
        owner = db.query(Account).filter_by(github_user=OWNER).one()
        db.add(Project(project_name="kept", project_code="KPT", user_id=owner.user_id, pr_state="draft"))
        db.commit()

        due = drift_worker._projects_to_check(db, datetime.now(timezone.utc), [owner.user_id], 10)

        assert [p.project_name for p in due] == ["kept"]
    finally:
        db.close()


def _edit_routes(project_id):
    by_id = f"/api/projects/{project_id}"
    return {
        "rename": ("PATCH", f"{by_id}/project-name", {"project_name": "renamed"}),
        "drift schedule": ("PATCH", f"{by_id}/drift-config", {"drift_check_interval_minutes": 0}),
        "save workflows": ("POST", "/api/save-workflows", {
            **PROJECT_BODY, "workflows": [{"name": WORKFLOW_NAME, "content": "on: pull_request\n"}]}),
        "open a campaign": ("POST", "/api/create-pull-requests", PROJECT_BODY),
        "delete": ("DELETE", f"{PROJECT_URL}?github_user={OWNER}", None),
        "delete with its GitHub resources": ("DELETE", f"{PROJECT_URL}/enhanced", {
            **PROJECT_BODY, "delete_github_resources": False}),
    }


# Refused as a deletion already under way rather than as read-only.
_REFUSED_AS_UNDER_WAY = {"delete with its GitHub resources": 409}


@pytest.mark.parametrize("route", list(_edit_routes(0)))
def test_every_edit_route_refuses_a_deleting_project(client, route):
    method, url, body = _edit_routes(_seed())[route]

    with _signed_in_to_github():
        response = client.request(method, url, json=body)

    assert response.status_code == _REFUSED_AS_UNDER_WAY.get(route, 403), response.text
    db = TestingSessionLocal()
    try:
        project = _project(db)
        assert project is not None
        assert project.pr_state == PROJECT_DELETING
        assert project.drift_check_interval_minutes is None
        assert _workflow(db).workflow_yaml == WORKFLOW_YAML
        assert db.query(ProjectPRCampaign).count() == 1
    finally:
        db.close()


def test_editors_can_still_merge_and_close_removal_prs(client):
    _seed()

    client.sign_in(VIEWER)
    refused = [_merge(client, REPO_A, 1).status_code, _close(client, REPO_A, 1).status_code]
    client.sign_in(OWNER)
    merged = _merge(client, REPO_A, 1)
    closed = _close(client, REPO_B, 2)

    assert refused == [403, 403], "a viewer still cannot"
    assert merged.status_code == 200, merged.text
    assert closed.status_code == 200, closed.text
    db = TestingSessionLocal()
    try:
        assert [_pr_state(db, 1), _pr_state(db, 2)] == ["merged", "closed"]
        assert _project(db).pr_state == "draft"
    finally:
        db.close()
