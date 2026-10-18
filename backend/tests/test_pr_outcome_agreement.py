"""A pull request's outcome has the same effect however ActionsManager learns it.

Five paths learn that a campaign PR merged or closed: the merge and close
buttons, the GitHub webhook, a refresh from GitHub, and the project page load
(which settles from PR rows already stored). Each used to carry its own copy of
what follows, and they disagreed. The one users hit: closed PR rows are never
removed, and "all merged" was judged over the project's whole PR history, so a
single campaign closed long ago made every later merge leave files at
"Committed locally" (or, through the webhook, never sync them at all).

Every path now calls workflows.apply_pr_outcome / settle_project_prs, and this
asserts they agree, so a path that grows its own copy again fails here.
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

from main import app  # noqa: E402
from models import (  # noqa: E402
    Account,
    Base,
    NotificationEvent,
    Project,
    ProjectPRCampaign,
    ProjectPullRequest,
    ProjectRepo,
    ProjectWorkflow,
    Repo,
    FileDelivery,
    Workflow,
    WorkflowVersion,
    WorkspaceMember,
)
import deliveries  # noqa: E402
import workflows  # noqa: E402
from authorization import PROJECT_DELETING  # noqa: E402
from projects import get_db as projects_get_db  # noqa: E402
from tests.delivery_records import record_delivered, record_drift, record_pr_carries, removed  # noqa: E402
from workflows import get_db as workflows_get_db  # noqa: E402

OWNER = "outcome-owner"
PROJECT = "outcome-project"
REPO = "outcome-owner/service"
MERGED_AT = "2026-10-01T12:00:00Z"
MAIN = "main"
RELEASE = "release"
SYNCED = "synced_with_github"

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
)
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
            authenticate_client(c, OWNER, TestingSessionLocal)
            yield c
    app.state.middleware_db_factory = original_factory
    app.dependency_overrides.pop(projects_get_db, None)
    app.dependency_overrides.pop(workflows_get_db, None)


def _seed(campaigns):
    """One project with workflow ``ci`` under review, drifted on (REPO, main).

    *campaigns* is a list of campaigns, oldest first, each a list of
    (pr_number, pr_state) pairs.
    """
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email="o@example.com", account_type="free")
        db.add(owner)
        db.flush()
        db.add(WorkspaceMember(user_id=owner.user_id, workspace_role="admin"))
        project = Project(project_name=PROJECT, project_code="OUT", user_id=owner.user_id,
                          project_type="standard", pr_state="open")
        repo = Repo(repo_name=REPO)
        workflow = Workflow(workflow_name="ci", workflow_yaml="on: push\n",
                            workflow_git_hash="a" * 40, workflow_status="under_review")
        db.add_all([project, repo, workflow])
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
        record_drift(db, workflow, REPO, "main")
        for prs in campaigns:
            campaign = ProjectPRCampaign(project_id=project.project_id)
            db.add(campaign)
            db.flush()
            for number, state in prs:
                pr = ProjectPullRequest(
                    project_id=project.project_id, campaign_id=campaign.campaign_id,
                    repo_name=REPO, pr_number=number, pr_url=f"https://github.com/{REPO}/pull/{number}",
                    pr_state=state, branch_name=f"actions-manager/out/{number}", target_branch="main",
                    workflow_names="ci",
                )
                db.add(pr)
                if state == "open":
                    record_pr_carries(db, pr, workflow)
        db.commit()
    finally:
        db.close()


def _response(status, payload):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


def _merge_button(client, number, outcome):
    assert outcome == "merged"
    with patch("workflows.user_tokens", {OWNER: "tok"}), \
            patch("workflows._merge_pull_request_with_fallback",
                  return_value=(_response(200, {"sha": "m"}), "merge", None)), \
            patch("workflows._delete_actions_manager_branch", return_value=(True, None)):
        response = client.put("/api/merge-pull-request", json={
            "project_name": PROJECT, "repo_name": REPO, "pr_number": number})
    assert response.status_code == 200, response.text


def _close_button(client, number, outcome):
    assert outcome == "closed"
    with patch("workflows.user_tokens", {OWNER: "tok"}), \
            patch("workflows.github_patch", return_value=_response(200, {"state": "closed"})):
        response = client.patch("/api/close-pull-request", json={
            "project_name": PROJECT, "repo_name": REPO, "pr_number": number})
    assert response.status_code == 200, response.text


def _webhook(client, number, outcome):
    merged = outcome == "merged"
    payload = {
        "action": "closed",
        "pull_request": {"number": number, "merged": merged,
                         "merged_at": MERGED_AT if merged else None, "closed_at": MERGED_AT},
        "repository": {"full_name": REPO},
    }
    with patch("workflows._verify_pr_webhook_signature", return_value=True):
        response = client.post("/webhooks/github", content=json.dumps(payload), headers={
            "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=test"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "processed"


def _refresh(client, number, outcome):
    merged = outcome == "merged"

    def github_get(url, *args, **kwargs):
        if url.endswith(f"/pulls/{number}"):
            return _response(200, {"number": number, "state": "closed", "merged": merged,
                                   "merged_at": MERGED_AT if merged else None, "closed_at": MERGED_AT})
        return _response(200, {"state": "open"})

    with patch("workflows.user_tokens", {OWNER: "tok"}), patch("workflows.github_get", side_effect=github_get):
        response = client.get("/api/project-pr-status",
                              params={"project_name": PROJECT, "refresh_from_github": "true"})
    assert response.status_code == 200, response.text


def _load_project(client):
    return client.get(f"/api/projects/{PROJECT}", params={"github_user": OWNER})


def _page_load(client, number, outcome):
    """The outcome reached the PR row by some path that did not settle the project."""
    db = TestingSessionLocal()
    try:
        db.query(ProjectPullRequest).filter_by(pr_number=number).update({"pr_state": outcome})
        db.commit()
    finally:
        db.close()
    response = _load_project(client)
    assert response.status_code == 200, response.text


SETTLING_PATHS = {
    "merge button": _merge_button,
    "close button": _close_button,
    "webhook": _webhook,
    "refresh": _refresh,
    "page load": _page_load,
}
OUTCOME_PATHS = {name: SETTLING_PATHS[name] for name in ("merge button", "webhook", "refresh")}


def _state():
    db = TestingSessionLocal()
    try:
        workflow = db.query(Workflow).filter_by(workflow_name="ci").one()
        project = db.query(Project).filter_by(project_name=PROJECT).one()
        return workflow.workflow_status, project.pr_state
    finally:
        db.close()


# (earlier campaigns, latest campaign, PR resolved, outcome) -> (workflow status, project pr_state)
SCENARIOS = {
    "an old closed campaign does not block a later merge": (
        [[(1, "closed")]], [(2, "open")], 2, "merged", ("synced_with_github", "synced")),
    "a PR closed in the latest campaign sends the files back": (
        [], [(3, "merged"), (4, "open")], 4, "closed", ("committed_locally", "draft")),
    "nothing settles while a PR is still open": (
        [], [(5, "open"), (6, "open")], 6, "merged", ("under_review", "open")),
    "an old merged campaign does not hide a close": (
        [[(7, "merged")]], [(8, "open")], 8, "closed", ("committed_locally", "draft")),
}


@pytest.mark.parametrize("path", SETTLING_PATHS)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_path_settles_the_same_way(client, path, scenario):
    earlier, latest, number, outcome, expected = SCENARIOS[scenario]
    if (path == "merge button" and outcome != "merged") or (path == "close button" and outcome != "closed"):
        pytest.skip("that button cannot produce this outcome")
    _seed(earlier + [latest])

    SETTLING_PATHS[path](client, number, outcome)

    assert _state() == expected


@pytest.mark.parametrize("path", OUTCOME_PATHS)
def test_a_merge_has_the_same_effects_on_every_path(client, path):
    _seed([[(1, "open")]])

    OUTCOME_PATHS[path](client, 1, "merged")

    db = TestingSessionLocal()
    try:
        pr = db.query(ProjectPullRequest).filter_by(pr_number=1).one()
        drift = db.query(FileDelivery).one()
        versions = [json.loads(v.version_metadata or "{}") for v in db.query(WorkflowVersion).all()]
        events = [e.event_type for e in db.query(NotificationEvent).all()]
        assert pr.pr_state == "merged"
        assert pr.merged_at is not None
        assert drift.has_drift is False, "a merge proves the PR's files now match GitHub"
        assert drift.delivered_at is not None
        assert [v.get("action") for v in versions] == ["pr_merged"]
        assert "campaign_pr.merged" in events
    finally:
        db.close()


def test_the_webhook_records_githubs_merge_time(client):
    _seed([[(1, "open")]])

    _webhook(client, 1, "merged")

    db = TestingSessionLocal()
    try:
        merged_at = db.query(ProjectPullRequest).filter_by(pr_number=1).one().merged_at
        assert merged_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    finally:
        db.close()


def test_a_page_load_keeps_a_draft_made_after_the_last_campaign(client):
    """Nothing is under review, so there is nothing to settle: the edit's draft stays."""
    _seed([[(1, "merged")]])
    db = TestingSessionLocal()
    try:
        db.query(Workflow).update({"workflow_status": "committed_locally"})
        db.query(Project).update({"pr_state": "draft"})
        db.commit()
    finally:
        db.close()

    assert _load_project(client).status_code == 200

    assert _state() == ("committed_locally", "draft")


# --------------------------------------------------------------------------- #
# A project being deleted                                                     #
# --------------------------------------------------------------------------- #


def _seed_deletion(prs):
    """PROJECT being deleted: ``ci`` delivered to main and RELEASE of REPO, and a
    removal PR for each, #1 to main and #2 to RELEASE, open or merged as *prs*
    says ({pr_number: state}). A merged one merged the way the merge button does.
    """
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email="o@example.com", account_type="free")
        db.add(owner)
        db.flush()
        db.add(WorkspaceMember(user_id=owner.user_id, workspace_role="admin"))
        project = Project(project_name=PROJECT, project_code="OUT", user_id=owner.user_id,
                          project_type="standard", pr_state=PROJECT_DELETING)
        repo = Repo(repo_name=REPO)
        workflow = Workflow(workflow_name="ci", workflow_yaml="on: push\n", workflow_status=SYNCED)
        db.add_all([project, repo, workflow])
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
        campaign = ProjectPRCampaign(project_id=project.project_id)
        db.add(campaign)
        db.flush()
        rows = []
        for number, branch in enumerate((MAIN, RELEASE), start=1):
            record_delivered(db, workflow, REPO, branch, sha="a" * 40)
            pr = ProjectPullRequest(
                project_id=project.project_id, campaign_id=campaign.campaign_id, repo_name=REPO,
                pr_number=number, pr_url=f"https://github.com/{REPO}/pull/{number}", pr_state="open",
                branch_name=f"actions-manager/out/{number}", target_branch=branch, workflow_names="ci",
            )
            db.add(pr)
            db.flush()
            deliveries.record_pending(db, pr, removed(workflow))
            rows.append(pr)
        db.commit()
        for pr in rows:
            if prs[pr.pr_number] == "merged":
                workflows.apply_pr_outcome(db, pr, "merged")
    finally:
        db.close()


def _page_load_of_a_recorded_outcome(client, number, outcome):
    """The outcome reached the PR row and the record, by some path that did not settle the project."""
    db = TestingSessionLocal()
    try:
        pr = db.query(ProjectPullRequest).filter_by(pr_number=number).one()
        deliveries.record_pr_outcome(db, pr, outcome)
        pr.pr_state = outcome
        db.commit()
    finally:
        db.close()
    return _load_project(client)


# The settling paths, with the page load finding an outcome the record has as
# well: a removal leaves nothing delivered only once its merge is recorded.
_SETTLING_A_RECORDED_OUTCOME = {
    name: _page_load_of_a_recorded_outcome if path is _page_load else path for name, path in SETTLING_PATHS.items()
}
DELETION_PATHS = {name: path for name, path in _SETTLING_A_RECORDED_OUTCOME.items() if path is not _close_button}
CALL_OFF_PATHS = {name: path for name, path in _SETTLING_A_RECORDED_OUTCOME.items() if path is not _merge_button}


def _what_is_left():
    db = TestingSessionLocal()
    try:
        return {
            "projects": db.query(Project).count(),
            "pull requests": db.query(ProjectPullRequest).count(),
            "delivery rows": db.query(FileDelivery).count(),
            "workflows": db.query(Workflow).count(),
        }
    finally:
        db.close()


@pytest.mark.parametrize("deletion_path", DELETION_PATHS)
def test_every_path_completes_a_deletion_the_same_way(client, deletion_path):
    _seed_deletion({1: "merged", 2: "open"})

    DELETION_PATHS[deletion_path](client, 2, "merged")

    assert _what_is_left() == {"projects": 0, "pull requests": 0, "delivery rows": 0, "workflows": 0}


# (removal PRs before, PR resolved) -> (ci's status, project pr_state, PR #2's state)
CALL_OFFS = {
    "a close calls it off with the other removal still open": (
        {1: "open", 2: "open"}, 1, (SYNCED, "open", "open")),
    "a close calls it off after the other removal merged": (
        {1: "merged", 2: "open"}, 2, ("committed_locally", "draft", "closed")),
}


@pytest.mark.parametrize("call_off_path", CALL_OFF_PATHS)
@pytest.mark.parametrize("call_off", CALL_OFFS)
def test_every_path_calls_off_a_deletion_the_same_way(client, call_off_path, call_off):
    prs, number, expected = CALL_OFFS[call_off]
    _seed_deletion(prs)

    CALL_OFF_PATHS[call_off_path](client, number, "closed")

    db = TestingSessionLocal()
    try:
        workflow = db.query(Workflow).filter_by(workflow_name="ci").one()
        project = db.query(Project).filter_by(project_name=PROJECT).one()
        second = db.query(ProjectPullRequest).filter_by(pr_number=2).one()
        actual = (workflow.workflow_status, project.pr_state, second.pr_state)
        assert actual == expected
    finally:
        db.close()
