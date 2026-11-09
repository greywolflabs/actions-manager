"""merge/close-pull-request must prove the caller, and prove the target (#2071).

Both endpoints took ``repo_name`` and ``pr_number`` straight from the request
and checked neither against the project: the only validation was that the repo
string contained a "/". They also resolved the caller with
``_find_project_by_name``, which never reads ``ProjectMembership.project_role``,
so a project_viewer could merge a campaign's pull requests.

The caller's own GitHub token bounds the blast radius — GitHub still refuses a
repository the caller cannot write to — but ActionsManager's own role model was
bypassed, and ``_handle_merged_pull_request`` runs its "no open PRs left"
transitions even when the merged PR has no record here, so an unrelated merge
could move project and workflow state.
"""
import sys
import os
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (
    Base,
    Account,
    LinkedReusableWorkflow,
    Project,
    ProjectMembership,
    ProjectPullRequest,
    ProjectRepo,
    ProjectWorkflow,
    Repo,
    Workflow,
    WorkspaceMember,
)
from github_api_tracker import RateLimitExceeded
from main import app
from projects import get_db as projects_get_db
from workflows import get_db as workflows_get_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_merge_close_pull_request_scope.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "owner-user"
MEMBER = "member-user"
PROJECT = "caller-project"
REPO = "owner-user/service"
OUTSIDE_REPO = "owner-user/not-in-project"
RECORDED_PR = 7


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_and_teardown_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _seed(project_role):
    """A project owned by OWNER with one recorded open PR, and MEMBER holding
    *project_role* on it as a full workspace member."""
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email="owner@example.com", account_type="pro")
        member = Account(github_user=MEMBER, github_email="member@example.com", account_type="pro")
        db.add_all([owner, member])
        db.flush()
        db.add(WorkspaceMember(user_id=member.user_id, workspace_role="member"))

        project = Project(
            project_name=PROJECT, project_code="CALL", user_id=owner.user_id,
            project_type="standard", pr_state="open",
        )
        db.add(project)
        db.flush()
        db.add(ProjectMembership(
            user_id=member.user_id, project_id=project.project_id, project_role=project_role,
        ))

        repo = Repo(repo_name=REPO)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))

        db.add(ProjectPullRequest(
            project_id=project.project_id, repo_name=REPO, pr_number=RECORDED_PR,
            pr_url=f"https://github.com/{REPO}/pull/{RECORDED_PR}", pr_state="open",
            branch_name=f"actions-manager/campaign-{RECORDED_PR}", target_branch="main",
        ))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def member_client(authenticate_client):
    app.dependency_overrides[projects_get_db] = override_get_db
    app.dependency_overrides[workflows_get_db] = override_get_db
    original_factory = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestingSessionLocal
    with mock.patch("mode_validation.validate_startup_configuration"):
        with TestClient(app) as client:
            authenticate_client(client, MEMBER, TestingSessionLocal)
            yield client
    app.state.middleware_db_factory = original_factory
    app.dependency_overrides.pop(projects_get_db, None)
    app.dependency_overrides.pop(workflows_get_db, None)


def _merge(client, repo_name=REPO, pr_number=RECORDED_PR):
    return client.put("/api/merge-pull-request", json={
        "github_user": MEMBER, "project_name": PROJECT,
        "repo_name": repo_name, "pr_number": pr_number,
    })


def _close(client, repo_name=REPO, pr_number=RECORDED_PR):
    return client.patch("/api/close-pull-request", json={
        "github_user": MEMBER, "project_name": PROJECT,
        "repo_name": repo_name, "pr_number": pr_number,
    })


@mock.patch("workflows._merge_pull_request_with_fallback")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_project_viewer_cannot_merge(mock_tokens, mock_merge, member_client):
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    response = _merge(member_client)

    assert response.status_code == 403, response.text
    assert "project_editor" in response.json()["detail"]
    mock_merge.assert_not_called()


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_project_viewer_cannot_close(mock_tokens, mock_patch, member_client):
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    response = _close(member_client)

    assert response.status_code == 403, response.text
    assert "project_editor" in response.json()["detail"]
    mock_patch.assert_not_called()


@mock.patch("workflows._merge_pull_request_with_fallback")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_repository_outside_the_project_is_refused(mock_tokens, mock_merge, member_client):
    """The merge target was caller-chosen: any owner/repo string was accepted."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"

    response = _merge(member_client, repo_name=OUTSIDE_REPO)

    assert response.status_code == 404, response.text
    assert "No pull request" in response.json()["detail"]
    mock_merge.assert_not_called()


@mock.patch("workflows._merge_pull_request_with_fallback")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_number_this_project_never_opened_is_refused(mock_tokens, mock_merge, member_client):
    """A repo in the project is not enough — the PR number was free too."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"

    response = _merge(member_client, pr_number=4242)

    assert response.status_code == 404, response.text
    assert "No pull request" in response.json()["detail"]
    mock_merge.assert_not_called()


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_close_refuses_an_unrecorded_pull_request(mock_tokens, mock_patch, member_client):
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"

    response = _close(member_client, pr_number=4242)

    assert response.status_code == 404, response.text
    assert "No pull request" in response.json()["detail"]
    mock_patch.assert_not_called()


@mock.patch("workflows._merge_pull_request_with_fallback")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_an_editor_merging_a_recorded_pr_reaches_github(mock_tokens, mock_merge, member_client):
    """Both new checks must let the real case through.

    The merge is mocked as refused by GitHub (405), so this asserts the call was
    attempted rather than exercising the whole post-merge bookkeeping.
    """
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_merge.return_value = (
        mock.MagicMock(status_code=405), "merge", "Pull request is not mergeable",
    )

    response = _merge(member_client)

    assert response.status_code == 400, response.text
    mock_merge.assert_called_once()


RWX_PROJECT = "shared-rwx"
RWX_REPO = "owner-user/my-rwx-workflow"
REUSABLE_PR = 61


def _seed_linked(caller_role, rwx_role="project_editor"):
    """A caller project that opened PR #61 in the reusable-workflow repo of the Reusable
    Workflow Project it links, as a caller's campaign does. MEMBER is *rwx_role* on the
    Reusable Workflow Project and *caller_role* on the caller."""
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email="owner@example.com", account_type="pro")
        member = Account(github_user=MEMBER, github_email="member@example.com", account_type="pro")
        db.add_all([owner, member])
        db.flush()
        db.add(WorkspaceMember(user_id=member.user_id, workspace_role="member"))
        caller = Project(project_name=PROJECT, project_code="CALL", user_id=owner.user_id,
                         project_type="standard", pr_state="open")
        rwx = Project(project_name=RWX_PROJECT, project_code="RWX", user_id=owner.user_id,
                      project_type="rwx", pr_state="open")
        repo = Repo(repo_name=RWX_REPO)
        deploy = Workflow(workflow_name="deploy", workflow_yaml="on: workflow_call\n", reusable_workflow=True,
                          workflow_status="under_review")
        db.add_all([caller, rwx, repo, deploy])
        db.flush()
        db.add_all([
            ProjectMembership(user_id=member.user_id, project_id=caller.project_id, project_role=caller_role),
            ProjectMembership(user_id=member.user_id, project_id=rwx.project_id, project_role=rwx_role),
            ProjectRepo(project_id=rwx.project_id, repo_id=repo.repo_id),
            ProjectWorkflow(project_id=rwx.project_id, workflow_id=deploy.workflow_id),
            LinkedReusableWorkflow(standard_project_id=caller.project_id, rwx_project_id=rwx.project_id,
                                   workflow_id=deploy.workflow_id),
            ProjectPullRequest(
                project_id=caller.project_id, repo_name=RWX_REPO, pr_number=REUSABLE_PR,
                pr_url=f"https://github.com/{RWX_REPO}/pull/{REUSABLE_PR}", pr_state="open",
                branch_name="actions-manager/shared/61-main", target_branch="main",
            ),
        ])
        db.commit()
    finally:
        db.close()


def _close_from(client, project_name, repo_name=RWX_REPO, pr_number=REUSABLE_PR):
    return client.patch("/api/close-pull-request", json={
        "github_user": MEMBER, "project_name": project_name, "repo_name": repo_name, "pr_number": pr_number,
    })


def _merge_from(client, project_name, repo_name=RWX_REPO, pr_number=REUSABLE_PR):
    return client.put("/api/merge-pull-request", json={
        "github_user": MEMBER, "project_name": project_name, "repo_name": repo_name, "pr_number": pr_number,
    })


def _pr_state() -> str:
    db = TestingSessionLocal()
    try:
        return db.query(ProjectPullRequest).filter_by(pr_number=REUSABLE_PR).one().pr_state
    finally:
        db.close()


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_reusable_workflow_project_can_close_the_pr_a_caller_opened_in_its_repository(mock_tokens, mock_patch, member_client):
    """The campaigns tab of the Reusable Workflow Project lists PR #61, which the caller's campaign
    recorded under the caller. Closing it said it was not recorded for this project, so the campaign
    waiting on it stayed open for good."""
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_patch.return_value = mock.MagicMock(status_code=200, json=lambda: {"state": "closed"})

    response = _close_from(member_client, RWX_PROJECT)

    assert response.status_code == 200, response.text
    assert response.json()["closed"] is True
    assert _pr_state() == "closed"


@mock.patch("workflows._delete_actions_manager_branch", return_value=(True, None))
@mock.patch("workflows._merge_pull_request_with_fallback")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_reusable_workflow_project_can_merge_the_pr_a_caller_opened_in_its_repository(mock_tokens, mock_merge, _branch, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_merge.return_value = (mock.MagicMock(status_code=200, json=lambda: {"sha": "abc"}), "merge", None)

    response = _merge_from(member_client, RWX_PROJECT)

    assert response.status_code == 200, response.text
    assert response.json()["merged"] is True
    assert _pr_state() == "merged"


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_caller_can_close_a_pr_its_linked_project_recorded(mock_tokens, mock_patch, member_client):
    _seed_linked("project_editor")
    db = TestingSessionLocal()
    try:
        row = db.query(ProjectPullRequest).filter_by(pr_number=REUSABLE_PR).one()
        row.project_id = db.query(Project).filter_by(project_name=RWX_PROJECT).one().project_id
        db.commit()
    finally:
        db.close()
    mock_tokens[MEMBER] = "fake_token"
    mock_patch.return_value = mock.MagicMock(status_code=200, json=lambda: {"state": "closed"})

    response = _close_from(member_client, PROJECT)

    assert response.status_code == 200, response.text
    assert _pr_state() == "closed"


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_closing_a_linked_projects_pr_needs_an_editor_on_the_project_that_recorded_it(mock_tokens, mock_patch, member_client):
    _seed_linked("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    response = _close_from(member_client, RWX_PROJECT)

    assert response.status_code == 403, response.text
    mock_patch.assert_not_called()
    assert _pr_state() == "open"


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_of_a_project_that_is_not_linked_is_still_refused(mock_tokens, mock_patch, member_client):
    _seed_linked("project_editor")
    db = TestingSessionLocal()
    try:
        stranger = Project(project_name="stranger", project_code="STR", user_id=1, project_type="standard", pr_state="open")
        db.add(stranger)
        db.flush()
        db.query(ProjectPullRequest).filter_by(pr_number=REUSABLE_PR).update({"project_id": stranger.project_id})
        db.commit()
    finally:
        db.close()
    mock_tokens[MEMBER] = "fake_token"

    response = _close_from(member_client, RWX_PROJECT)

    assert response.status_code == 404, response.text
    assert "No pull request" in response.json()["detail"]
    mock_patch.assert_not_called()


def _dismiss_from(client, project_name, repo_name=RWX_REPO, pr_number=REUSABLE_PR):
    return client.post("/api/dismiss-pull-request", json={
        "project_name": project_name, "repo_name": repo_name, "pr_number": pr_number,
    })


def _github_answers(status):
    return mock.MagicMock(status_code=status, json=lambda: {})


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_github_no_longer_has_can_be_removed_from_its_campaign(mock_tokens, mock_get, member_client):
    """The repository, or the pull request, was deleted: GitHub answers 404 to a close, so the
    campaign waiting on it stayed open for good. Removing it settles it as closed."""
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.return_value = _github_answers(404)

    response = _dismiss_from(member_client, RWX_PROJECT)

    assert response.status_code == 200, response.text
    assert response.json()["dismissed"] is True
    assert _pr_state() == "closed"


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_github_still_has_is_never_removed(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.return_value = _github_answers(200)

    response = _dismiss_from(member_client, RWX_PROJECT)

    assert response.status_code == 409, response.text
    assert "Close it instead" in response.json()["detail"]
    assert _pr_state() == "open"


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_is_not_removed_when_github_cannot_say_whether_it_exists(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.return_value = _github_answers(500)

    response = _dismiss_from(member_client, RWX_PROJECT)

    assert response.status_code == 502, response.text
    assert _pr_state() == "open"


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_is_not_removed_when_the_rate_limit_stops_the_check(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.side_effect = RateLimitExceeded("over")

    response = _dismiss_from(member_client, RWX_PROJECT)

    assert response.status_code == 429, response.text
    assert _pr_state() == "open"


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_removing_a_pr_needs_an_editor_and_asks_github_nothing_first(mock_tokens, mock_get, member_client):
    _seed_linked("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    response = _dismiss_from(member_client, RWX_PROJECT)

    assert response.status_code == 403, response.text
    mock_get.assert_not_called()
    assert _pr_state() == "open"


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_nobody_recorded_cannot_be_removed(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"

    response = _dismiss_from(member_client, RWX_PROJECT, pr_number=4242)

    assert response.status_code == 404, response.text
    mock_get.assert_not_called()


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_that_is_already_closed_is_not_removed_again(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    db = TestingSessionLocal()
    try:
        db.query(ProjectPullRequest).filter_by(pr_number=REUSABLE_PR).update({"pr_state": "closed"})
        db.commit()
    finally:
        db.close()

    response = _dismiss_from(member_client, RWX_PROJECT)

    assert response.status_code == 409, response.text
    mock_get.assert_not_called()


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_refresh_flags_a_pr_github_no_longer_has_so_the_page_can_offer_to_remove_it(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.return_value = _github_answers(404)

    response = member_client.get(
        "/api/project-pr-campaigns", params={"project_name": RWX_PROJECT, "refresh_from_github": "true"},
    )

    assert response.status_code == 200, response.text
    items = [pr for campaign in response.json()["campaigns"] for pr in campaign["pull_requests"]]
    assert [(pr["pr_number"], pr["pr_state"], pr["not_found_on_github"], pr["can_close"]) for pr in items] == [
        (REUSABLE_PR, "open", True, False),
    ]


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_pr_github_still_has_is_not_flagged(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.return_value = mock.MagicMock(
        status_code=200, json=lambda: {"state": "open", "mergeable": True, "mergeable_state": "clean"},
    )

    response = member_client.get(
        "/api/project-pr-campaigns", params={"project_name": RWX_PROJECT, "refresh_from_github": "true"},
    )

    items = [pr for campaign in response.json()["campaigns"] for pr in campaign["pull_requests"]]
    assert [(pr["not_found_on_github"], pr["can_close"]) for pr in items] == [(False, True)]


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_closing_a_pr_github_does_not_have_points_at_removing_it(mock_tokens, mock_patch, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_patch.return_value = _github_answers(404)

    response = _close_from(member_client, RWX_PROJECT)

    assert response.status_code == 404, response.text
    assert "remove it from the campaign" in response.json()["detail"]
    assert _pr_state() == "open"


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_removing_the_last_open_pr_lets_its_campaign_finish(mock_tokens, mock_get, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.return_value = _github_answers(404)

    before = member_client.get("/api/project-pr-campaigns", params={"project_name": RWX_PROJECT}).json()
    assert [(c["open_count"], c["campaign_status"]) for c in before["campaigns"]] == [(1, "open")]

    assert _dismiss_from(member_client, RWX_PROJECT).status_code == 200
    after = member_client.get("/api/project-pr-campaigns", params={"project_name": RWX_PROJECT}).json()

    assert [(c["open_count"], c["closed_count"], c["campaign_status"]) for c in after["campaigns"]] != [(1, 0, "open")]
    assert all(c["open_count"] == 0 and c["campaign_status"] != "open" for c in after["campaigns"])


@mock.patch("workflows.github_get")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_github_is_asked_about_the_recorded_repository_not_the_requests_spelling(mock_tokens, mock_get, member_client):
    """The record is matched with the request's whitespace stripped, so the name sent on to GitHub
    has to be the stored one, or a padded name could read a live pull request as gone."""
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_get.return_value = _github_answers(404)

    response = _dismiss_from(member_client, RWX_PROJECT, repo_name=f"  {RWX_REPO} ")

    assert response.status_code == 200, response.text
    assert response.json()["repo_name"] == RWX_REPO
    assert mock_get.call_args.args[0] == f"https://api.github.com/repos/{RWX_REPO}/pulls/{REUSABLE_PR}"


@mock.patch("workflows.github_patch")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_close_reaches_the_recorded_repository_not_the_requests_spelling(mock_tokens, mock_patch, member_client):
    _seed_linked("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_patch.return_value = mock.MagicMock(status_code=200, json=lambda: {"state": "closed"})

    response = _close_from(member_client, RWX_PROJECT, repo_name=f"{RWX_REPO} ")

    assert response.status_code == 200, response.text
    assert mock_patch.call_args.args[0] == f"https://api.github.com/repos/{RWX_REPO}/pulls/{REUSABLE_PR}"
