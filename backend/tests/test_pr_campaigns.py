"""
Regression tests for first-class PR campaign tracking.

Each PR campaign creation run must produce a NEW unique campaign record
(project_pr_campaigns) and attach the PR rows created during that run to it,
instead of appending new PRs to a previous campaign
(https://github.com/greywolflabs/actions-manager/issues/1360).

Verifies:
- _save_prs_and_update_status creates a new campaign per run and links PR rows
- A second campaign run never regroups or modifies the first campaign's PRs
- "pr_updated" results (commits added to an existing open PR) keep their
  original campaign and do not leave empty campaign records behind
- /api/project-pr-campaigns groups PRs by campaign_id, so two runs with the
  same workflows/branch/day render as separate campaigns
- Campaign status is derived only from that campaign's PRs
- Legacy PR rows without a campaign_id still group heuristically
- Campaign created_by/created_at come from the campaign record
"""

import hashlib
import json
import sys
import os
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (
    Base, Account, Project, ProjectPullRequest, ProjectPRCampaign, Repo, ProjectRepo, Codeowners,
    LinkedReusableWorkflow, Workflow, ProjectWorkflow, WorkflowVersion,
)
from auth import create_auth_session  # noqa: E402
from main import app
from workflows import (
    get_db, _save_prs_and_update_status, _fetch_branch_protection,
    _campaign_pr_body, _finalize_pr_result, CreatePullRequestsRequest,
    _includes_regular_workflows, _build_campaign_snapshot, _build_policy_version,
    _build_codeowners_for_delivery, _process_regular_workflows_update,
)
from deliveries import blob_sha as _git_blob_sha  # noqa: E402

TEST_USER = "campaignuser"
TEST_PROJECT = "campaign_project"

client = TestClient(app)


@pytest.fixture()
def db_session(authenticate_client):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    authenticate_client(client, TEST_USER, TestingSessionLocal)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        app.dependency_overrides.pop(get_db, None)
        Base.metadata.drop_all(bind=engine)


def _create_account_and_project(db):
    account = Account(
        github_user=TEST_USER,
        github_email="campaign@example.com",
        account_type="free",
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    project = Project(
        project_name=TEST_PROJECT,
        project_code="CMP",
        user_id=account.user_id,
        branch_option="default",
        reusable_workflows_enabled=False,
        pr_state="synced",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return account, project


def _pr_result(pr_number, branch_name, status="pr_created"):
    return {
        "status": status,
        "pr_number": pr_number,
        "pr_url": f"https://github.com/org/repo/pull/{pr_number}",
        "branch_name": branch_name,
        "pr_title": f"Update workflows #{pr_number}",
        "pr_author": TEST_USER,
        "pr_body": "Automated workflow update",
    }


def _get_campaigns(github_user=TEST_USER, project_name=TEST_PROJECT):
    with patch("workflows.user_tokens", {github_user: "fake-token"}):
        return client.get(
            "/api/project-pr-campaigns",
            params={"github_user": github_user, "project_name": project_name},
        )


class TestCampaignRecordCreation:

    def test_first_run_creates_campaign_with_linked_prs(self, db_session):
        _, project = _create_account_and_project(db_session)
        results = {
            "org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main"),
            "org/repo-b on main": _pr_result(11, "actions-manager/cmp/b/222-main"),
        }

        pr_count, _ = _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )

        assert pr_count == 2
        campaigns = db_session.query(ProjectPRCampaign).all()
        assert len(campaigns) == 1
        assert campaigns[0].created_by == TEST_USER
        assert campaigns[0].project_id == project.project_id
        prs = db_session.query(ProjectPullRequest).all()
        assert len(prs) == 2
        assert all(pr.campaign_id == campaigns[0].campaign_id for pr in prs)

    def test_second_run_creates_new_campaign_and_does_not_regroup_first(self, db_session):
        _, project = _create_account_and_project(db_session)
        first_results = {
            "org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main"),
        }
        _save_prs_and_update_status(
            first_results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )
        campaign_a = db_session.query(ProjectPRCampaign).one()
        pr_a = db_session.query(ProjectPullRequest).one()
        original_campaign_a_id = pr_a.campaign_id

        # Simulate the first campaign's PR being merged
        pr_a.pr_state = "merged"
        pr_a.merged_at = datetime.now(timezone.utc)
        db_session.commit()

        second_results = {
            "org/repo-a on main": _pr_result(14, "actions-manager/cmp/a/333-main"),
            "org/repo-b on main": _pr_result(54, "actions-manager/cmp/b/444-main"),
        }
        _save_prs_and_update_status(
            second_results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )

        campaigns = db_session.query(ProjectPRCampaign).order_by(
            ProjectPRCampaign.campaign_id
        ).all()
        assert len(campaigns) == 2
        campaign_b = campaigns[1]
        assert campaign_b.campaign_id != campaign_a.campaign_id

        prs = db_session.query(ProjectPullRequest).order_by(
            ProjectPullRequest.pr_id
        ).all()
        assert len(prs) == 3
        # Campaign A's PR was not modified or regrouped
        assert prs[0].campaign_id == original_campaign_a_id
        assert prs[0].pr_state == "merged"
        # New PRs belong only to campaign B
        assert prs[1].campaign_id == campaign_b.campaign_id
        assert prs[2].campaign_id == campaign_b.campaign_id

    def test_pr_updated_rows_keep_original_campaign_and_no_empty_campaign_remains(self, db_session):
        _, project = _create_account_and_project(db_session)
        first_results = {
            "org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main"),
        }
        _save_prs_and_update_status(
            first_results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )
        campaign_a = db_session.query(ProjectPRCampaign).one()

        # Same branch gets new commits: the PR is updated, not recreated
        update_results = {
            "org/repo-a on main": _pr_result(
                10, "actions-manager/cmp/a/111-main", status="pr_updated"
            ),
        }
        _save_prs_and_update_status(
            update_results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )

        pr = db_session.query(ProjectPullRequest).one()
        assert pr.campaign_id == campaign_a.campaign_id
        # No empty campaign record was left behind
        assert db_session.query(ProjectPRCampaign).count() == 1

    def test_legacy_row_reopened_by_pr_updated_joins_new_campaign(self, db_session):
        _, project = _create_account_and_project(db_session)
        legacy_pr = ProjectPullRequest(
            project_id=project.project_id,
            campaign_id=None,
            repo_name="org/repo-a",
            pr_number=5,
            pr_url="https://github.com/org/repo-a/pull/5",
            pr_state="open",
            branch_name="actions-manager/cmp-main",
            target_branch="main",
        )
        db_session.add(legacy_pr)
        db_session.commit()

        results = {
            "org/repo-a on main": _pr_result(
                5, "actions-manager/cmp-main", status="pr_updated"
            ),
        }
        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )

        campaign = db_session.query(ProjectPRCampaign).one()
        pr = db_session.query(ProjectPullRequest).one()
        assert pr.campaign_id == campaign.campaign_id


class TestCampaignEndpointGrouping:

    def _add_pr(self, db, project_id, *, pr_number, campaign_id, pr_state="open",
                workflow_names="wf222.yml", merged_at=None, closed_at=None):
        pr = ProjectPullRequest(
            project_id=project_id,
            campaign_id=campaign_id,
            repo_name="org/repo-a",
            pr_number=pr_number,
            pr_url=f"https://github.com/org/repo-a/pull/{pr_number}",
            pr_state=pr_state,
            branch_name=f"actions-manager/cmp/{pr_number}-main",
            target_branch="main",
            title=f"Update wf222 #{pr_number}",
            author=TEST_USER,
            workflow_names=workflow_names,
            merged_at=merged_at,
            closed_at=closed_at,
        )
        db.add(pr)
        db.commit()
        db.refresh(pr)
        return pr

    def _add_campaign(self, db, project_id, created_by=TEST_USER):
        campaign = ProjectPRCampaign(project_id=project_id, created_by=created_by)
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign

    def test_two_campaigns_same_day_and_workflows_render_separately(self, db_session):
        """Regression: new PRs must not be appended to a prior campaign card."""
        _, project = _create_account_and_project(db_session)
        now = datetime.now(timezone.utc)
        campaign_a = self._add_campaign(db_session, project.project_id)
        campaign_b = self._add_campaign(db_session, project.project_id)
        self._add_pr(db_session, project.project_id, pr_number=11,
                     campaign_id=campaign_a.campaign_id, pr_state="merged", merged_at=now)
        self._add_pr(db_session, project.project_id, pr_number=51,
                     campaign_id=campaign_a.campaign_id, pr_state="merged", merged_at=now)
        self._add_pr(db_session, project.project_id, pr_number=14,
                     campaign_id=campaign_b.campaign_id, pr_state="open")
        self._add_pr(db_session, project.project_id, pr_number=54,
                     campaign_id=campaign_b.campaign_id, pr_state="open")

        response = _get_campaigns()

        assert response.status_code == 200
        data = response.json()
        assert data["total_campaigns"] == 2
        assert data["active_campaigns"] == 1
        assert data["completed_campaigns"] == 1

        by_id = {campaign["campaign_id"]: campaign for campaign in data["campaigns"]}
        campaign_a_resp = by_id[f"campaign-{campaign_a.campaign_id}"]
        campaign_b_resp = by_id[f"campaign-{campaign_b.campaign_id}"]

        # Status derived only from each campaign's own PRs
        assert campaign_a_resp["campaign_status"] == "completed"
        assert campaign_a_resp["merged_count"] == 2
        assert campaign_a_resp["open_count"] == 0
        assert campaign_b_resp["campaign_status"] == "open"
        assert campaign_b_resp["open_count"] == 2
        assert campaign_b_resp["merged_count"] == 0

        # Older merged PRs are not displayed inside the new campaign
        campaign_b_numbers = {pr["pr_number"] for pr in campaign_b_resp["pull_requests"]}
        assert campaign_b_numbers == {14, 54}
        campaign_a_numbers = {pr["pr_number"] for pr in campaign_a_resp["pull_requests"]}
        assert campaign_a_numbers == {11, 51}

    def test_campaign_created_by_comes_from_campaign_record(self, db_session):
        _, project = _create_account_and_project(db_session)
        campaign = self._add_campaign(db_session, project.project_id, created_by="opener-user")
        self._add_pr(db_session, project.project_id, pr_number=14,
                     campaign_id=campaign.campaign_id)

        response = _get_campaigns()

        assert response.status_code == 200
        data = response.json()
        assert data["campaigns"][0]["created_by"] == "opener-user"

    def test_campaign_reports_the_projects_configured_branch_option(self, db_session):
        _, project = _create_account_and_project(db_session)
        campaign = self._add_campaign(db_session, project.project_id)
        self._add_pr(db_session, project.project_id, pr_number=14,
                     campaign_id=campaign.campaign_id)

        response = _get_campaigns()

        assert response.status_code == 200
        assert response.json()["campaigns"][0]["branch_option"] == "default"

    def test_legacy_branch_option_is_normalized(self, db_session):
        _, project = _create_account_and_project(db_session)
        project.branch_option = "all"
        db_session.commit()
        campaign = self._add_campaign(db_session, project.project_id)
        self._add_pr(db_session, project.project_id, pr_number=14,
                     campaign_id=campaign.campaign_id)

        response = _get_campaigns()

        assert response.status_code == 200
        assert response.json()["campaigns"][0]["branch_option"] == "default"

    def test_legacy_prs_without_campaign_id_still_grouped(self, db_session):
        _, project = _create_account_and_project(db_session)
        now = datetime.now(timezone.utc)
        # Legacy rows (no campaign) plus one tracked campaign
        self._add_pr(db_session, project.project_id, pr_number=1,
                     campaign_id=None, pr_state="merged", merged_at=now,
                     workflow_names="legacy.yml")
        self._add_pr(db_session, project.project_id, pr_number=2,
                     campaign_id=None, pr_state="merged", merged_at=now,
                     workflow_names="legacy.yml")
        campaign = self._add_campaign(db_session, project.project_id)
        self._add_pr(db_session, project.project_id, pr_number=14,
                     campaign_id=campaign.campaign_id)

        response = _get_campaigns()

        assert response.status_code == 200
        data = response.json()
        assert data["total_campaigns"] == 2
        legacy = next(
            campaign_resp for campaign_resp in data["campaigns"]
            if campaign_resp["campaign_id"] != f"campaign-{campaign.campaign_id}"
        )
        # Legacy NULL-campaign rows are isolated under a distinct "legacy-" id
        assert legacy["campaign_id"].startswith("legacy-")
        assert legacy["merged_count"] == 2
        assert legacy["campaign_status"] == "completed"
        tracked = next(
            campaign_resp for campaign_resp in data["campaigns"]
            if campaign_resp["campaign_id"] == f"campaign-{campaign.campaign_id}"
        )
        assert {pr["pr_number"] for pr in tracked["pull_requests"]} == {14}

    def test_unauthenticated_user_is_rejected(self, db_session):
        _create_account_and_project(db_session)

        with patch("workflows.user_tokens", {}):
            response = client.get(
                "/api/project-pr-campaigns",
                params={"github_user": TEST_USER, "project_name": TEST_PROJECT},
            )

        assert response.status_code == 401

    def test_merged_campaign_then_new_run_same_day_renders_two_campaigns(self, db_session):
        """End-to-end regression for the reported failure: create campaign A,
        merge its PRs, then create campaign B with the same workflows and target
        branch on the same day. The API must return two separate campaigns and
        campaign B must not include campaign A's merged PRs."""
        _, project = _create_account_and_project(db_session)

        first_results = {
            "whatsupdawg/test1 on main": _pr_result(56, "actions-manager/cmp/test1/aaa-main"),
            "whatsupdawg/test2 on main": _pr_result(57, "actions-manager/cmp/test2/bbb-main"),
        }
        _save_prs_and_update_status(
            first_results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )
        campaign_a = db_session.query(ProjectPRCampaign).one()
        now = datetime.now(timezone.utc)
        for pr in db_session.query(ProjectPullRequest).all():
            pr.pr_state = "merged"
            pr.merged_at = now
        db_session.commit()

        second_results = {
            "whatsupdawg/test1 on main": _pr_result(57, "actions-manager/cmp/test1/ccc-main"),
            "whatsupdawg/test2 on main": _pr_result(58, "actions-manager/cmp/test2/ddd-main"),
        }
        _save_prs_and_update_status(
            second_results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
        )
        campaign_b = db_session.query(ProjectPRCampaign).filter(
            ProjectPRCampaign.campaign_id != campaign_a.campaign_id
        ).one()

        # Every new PR row must carry the new campaign id — never NULL
        assert all(
            pr.campaign_id is not None
            for pr in db_session.query(ProjectPullRequest).all()
        )

        response = _get_campaigns()
        assert response.status_code == 200
        data = response.json()
        assert data["total_campaigns"] == 2

        by_id = {campaign["campaign_id"]: campaign for campaign in data["campaigns"]}
        campaign_a_resp = by_id[f"campaign-{campaign_a.campaign_id}"]
        campaign_b_resp = by_id[f"campaign-{campaign_b.campaign_id}"]

        a_prs = {(pr["repo_name"], pr["pr_number"]) for pr in campaign_a_resp["pull_requests"]}
        b_prs = {(pr["repo_name"], pr["pr_number"]) for pr in campaign_b_resp["pull_requests"]}
        assert a_prs == {("whatsupdawg/test1", 56), ("whatsupdawg/test2", 57)}
        assert b_prs == {("whatsupdawg/test1", 57), ("whatsupdawg/test2", 58)}
        assert campaign_a_resp["campaign_status"] == "completed"
        assert campaign_b_resp["campaign_status"] == "open"
        assert campaign_b_resp["merged_count"] == 0


class TestCodeownersInCampaigns:
    """
    The project's CODEOWNERS rides the campaign's per-repo PR on every repo,
    as custom files do, and a commit makes its blob the file's git_hash, as
    _update_workflow_git_hash does for a workflow.
    """

    def _add_repo(self, db, project, repo_name):
        repo = Repo(repo_name=repo_name)
        db.add(repo)
        db.commit()
        db.refresh(repo)
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.commit()
        return repo

    def _add_codeowners(self, db, project, content="* @a", git_hash=None, status="committed_locally"):
        record = Codeowners(project_id=project.project_id, content=content, git_hash=git_hash, status=status)
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    def _update(self, db, project, repo_names, codeowners, existing_sha=None):
        existing = MagicMock()
        existing.status_code = 200 if existing_sha else 404
        existing.json.return_value = {"sha": existing_sha}
        put_ok = MagicMock()
        put_ok.status_code = 201
        put_ok.json.return_value = {"content": {"sha": "newsha"}}
        new_pr = {
            "number": 42, "html_url": "https://github.com/org/repo/pull/42",
            "title": "t", "user": {"login": TEST_USER}, "body": "b",
        }
        with patch("workflows._resolve_branches_for_repo", return_value=["main"]), \
             patch("workflows._create_or_get_am_branch", return_value=("actions-manager/cmp-main", True, None)), \
             patch("workflows._delete_actions_manager_branch"), \
             patch("workflows.github_get", return_value=existing), \
             patch("workflows.requests.put", return_value=put_ok) as put, \
             patch("workflows._check_existing_pr", return_value=None), \
             patch("workflows._create_pull_request", return_value=(new_pr, None)):
            results = _process_regular_workflows_update(
                repo_names=repo_names, workflows=[], project_code="CMP", branch_option="default",
                regex_pattern="", branch_max_age_days=30, headers={}, db=db, user=TEST_USER,
                project=project, codeowners=codeowners,
            )
        return results, put

    def test_the_project_file_is_delivered_only_when_the_campaign_includes_it(self, db_session):
        db = db_session
        _, project = _create_account_and_project(db)
        record = self._add_codeowners(db, project, status="synced_with_github", git_hash="sha")

        assert _build_codeowners_for_delivery(project, CreatePullRequestsRequest(project_name=TEST_PROJECT), db) is None
        # Whatever its status: a campaign can also put it back where it drifted.
        included = CreatePullRequestsRequest(project_name=TEST_PROJECT, include_codeowners=True)
        assert _build_codeowners_for_delivery(project, included, db) == {
            "id": record.id, "file_path": ".github/CODEOWNERS", "content": "* @a",
        }

    def test_nothing_to_deliver_without_a_codeowners_file(self, db_session):
        db = db_session
        _, project = _create_account_and_project(db)
        included = CreatePullRequestsRequest(project_name=TEST_PROJECT, include_codeowners=True)
        assert _build_codeowners_for_delivery(project, included, db) is None

    def test_codeowners_goes_to_every_repo_and_its_blob_becomes_the_hash(self, db_session):
        db = db_session
        _, project = _create_account_and_project(db)
        self._add_repo(db, project, "whatsupdawg/repo-a")
        self._add_repo(db, project, "whatsupdawg/repo-b")
        record = self._add_codeowners(db, project)
        codeowners = _build_codeowners_for_delivery(
            project, CreatePullRequestsRequest(project_name=TEST_PROJECT, include_codeowners=True), db
        )

        results, _ = self._update(db, project, ["whatsupdawg/repo-a", "whatsupdawg/repo-b"], codeowners)

        for repo_name in ("whatsupdawg/repo-a", "whatsupdawg/repo-b"):
            assert results[f"{repo_name} on main"]["status"] == "pr_created"
            assert results[f"{repo_name} on main"]["codeowners_committed"] == ".github/CODEOWNERS"
        db.refresh(record)
        assert record.git_hash == "newsha"
        # Locked when the campaign records its PRs, so a run failing before then locks nothing.
        assert record.status == "committed_locally"

    def test_a_branch_already_holding_the_file_gets_no_commit(self, db_session):
        db = db_session
        _, project = _create_account_and_project(db)
        self._add_repo(db, project, "whatsupdawg/repo-a")
        record = self._add_codeowners(db, project)
        codeowners = {"id": record.id, "file_path": ".github/CODEOWNERS", "content": "* @a"}

        results, put = self._update(db, project, ["whatsupdawg/repo-a"], codeowners,
                                    existing_sha=_git_blob_sha("* @a"))

        put.assert_not_called()
        assert results["whatsupdawg/repo-a on main"]["nothing_to_deliver"] is True

    def test_a_copy_saved_again_during_the_campaign_keeps_its_cleared_hash(self, db_session):
        db = db_session
        _, project = _create_account_and_project(db)
        self._add_repo(db, project, "whatsupdawg/repo-a")
        record = self._add_codeowners(db, project, content="* @newer")
        # What the campaign read before the save.
        codeowners = {"id": record.id, "file_path": ".github/CODEOWNERS", "content": "* @a"}

        self._update(db, project, ["whatsupdawg/repo-a"], codeowners)

        db.refresh(record)
        assert record.git_hash is None

    def test_recording_the_prs_locks_the_file_they_carry(self, db_session):
        db = db_session
        _, project = _create_account_and_project(db)
        record = self._add_codeowners(db, project, git_hash=_git_blob_sha("* @a"))
        result = {**_pr_result(1, "actions-manager/cmp-main"), "codeowners_committed": ".github/CODEOWNERS"}

        _save_prs_and_update_status({"org/repo on main": result}, project, [], [], db, github_user=TEST_USER)

        db.refresh(record)
        assert record.status == "under_review"
        pr = db.query(ProjectPullRequest).one()
        assert pr.file_names == ".github/CODEOWNERS"

    def test_a_copy_saved_since_its_commit_is_not_locked(self, db_session):
        db = db_session
        _, project = _create_account_and_project(db)
        record = self._add_codeowners(db, project)  # zeroed: saved after the commit
        result = {**_pr_result(1, "actions-manager/cmp-main"), "codeowners_committed": ".github/CODEOWNERS"}

        _save_prs_and_update_status({"org/repo on main": result}, project, [], [], db, github_user=TEST_USER)

        db.refresh(record)
        assert record.status == "committed_locally"


class TestSchemaSafety:

    def test_missing_campaign_column_raises_backend_error(self, db_session):
        """If the campaign migration has not been applied to the runtime
        database, saving a new PR must raise a loud backend error instead of
        silently writing rows that fall back to heuristic grouping."""
        from fastapi import HTTPException
        from sqlalchemy import text

        _, project = _create_account_and_project(db_session)
        # Recreate the table in its pre-migration shape (no campaign_id column)
        db_session.execute(text("DROP TABLE project_pull_requests"))
        db_session.execute(text(
            "CREATE TABLE project_pull_requests ("
            "pr_id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, "
            "repo_name VARCHAR(255) NOT NULL, pr_number INTEGER NOT NULL, "
            "pr_url VARCHAR(500) NOT NULL, pr_state VARCHAR(20) NOT NULL, "
            "branch_name VARCHAR(255) NOT NULL, target_branch VARCHAR(255) NOT NULL, "
            "title VARCHAR(500), author VARCHAR(255), body TEXT, "
            "merged_at TIMESTAMP, closed_at TIMESTAMP, workflow_names TEXT, "
            "created_at TIMESTAMP, updated_at TIMESTAMP)"
        ))
        db_session.commit()

        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        with pytest.raises(HTTPException) as exc_info:
            _save_prs_and_update_status(
                results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
            )
        assert exc_info.value.status_code == 500
        assert "could not be recorded" in exc_info.value.detail

    def test_unrecorded_pr_keeps_the_remedy_but_not_the_database_error(self, db_session, monkeypatch):
        """The SQL and its parameters go to the server log, not the response (#2295)."""
        from fastapi import HTTPException
        from sqlalchemy.exc import OperationalError
        import workflows

        marker = "secret-marker-2295"

        def fail(**_kwargs):
            raise OperationalError(f"INSERT {marker}", {"param": marker}, Exception(marker))

        monkeypatch.setattr(workflows, "_save_pr_to_database", fail)
        _, project = _create_account_and_project(db_session)

        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        with pytest.raises(HTTPException) as exc_info:
            _save_prs_and_update_status(
                results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER
            )
        assert exc_info.value.status_code == 500
        assert "could not be recorded" in exc_info.value.detail
        assert "run_migrations.py" in exc_info.value.detail
        assert marker not in exc_info.value.detail

    def test_sqlite_migration_adds_column_to_runtime_database(self, tmp_path, monkeypatch):
        """The SQLite migration must target the application's resolved database
        (e.g. /app/data/actions_manager.db in self-hosted mode), not a
        hard-coded file next to the migration script."""
        import sqlite3
        import migrate_add_pr_campaigns as migration

        db_file = tmp_path / "actions_manager.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE projects (project_id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE project_pull_requests ("
            "pr_id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, "
            "repo_name VARCHAR(255) NOT NULL, pr_number INTEGER NOT NULL, "
            "pr_url VARCHAR(500) NOT NULL, pr_state VARCHAR(50), "
            "branch_name VARCHAR(255), target_branch VARCHAR(255))"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(migration, "APP_DATABASE_URL", f"sqlite:///{db_file}")
        migration.run_sqlite_migration()

        conn = sqlite3.connect(str(db_file))
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(project_pull_requests)")}
            assert "campaign_id" in columns
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "project_pr_campaigns" in tables
            # Idempotent: running again must not fail
            migration.run_sqlite_migration()
        finally:
            conn.close()

    def test_other_user_cannot_access_campaigns(self, db_session):
        _create_account_and_project(db_session)
        other = Account(
            github_user="intruder",
            github_email="intruder@example.com",
            account_type="free",
        )
        db_session.add(other)
        db_session.commit()

        # Actually sign in as the intruder: the point of this test is that a
        # real session still cannot reach a project it does not own.
        saved = client.headers.get("Authorization")
        client.headers["Authorization"] = f"Bearer {create_auth_session('intruder', db_session)}"
        try:
            response = _get_campaigns(github_user="intruder")
        finally:
            client.headers["Authorization"] = saved

        assert response.status_code == 404


class TestPerPRWorkflowNames:
    """Regression tests for per-PR workflow_names scoping (issue #1508).

    Each PR row must record only the workflows actually committed to that
    specific repo/branch, not the union of all workflows selected across the
    entire PR-creation run.
    """

    def test_per_pr_workflow_names_uses_workflows_committed_not_all_selected(self, db_session):
        """Caller workflows must not bleed onto the RWX repo's PR record."""
        _, project = _create_account_and_project(db_session)

        # Simulate a run that includes two caller-repo PRs (regular workflows)
        # and one RWX-repo PR (reusable workflow only).
        caller_wf1 = "wwff11.yml"
        caller_wf2 = "test.yml"
        rwx_wf = "rwx1.yml"

        results = {
            "org/caller-repo-a on main": {
                "status": "pr_created",
                "pr_number": 10,
                "pr_url": "https://github.com/org/caller-repo-a/pull/10",
                "branch_name": "actions-manager/cmp/a/111-main",
                "pr_title": "Update workflows",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": [caller_wf1, caller_wf2],
            },
            "org/rwx-repo on main": {
                "status": "pr_created",
                "pr_number": 50,
                "pr_url": "https://github.com/org/rwx-repo/pull/50",
                "branch_name": "actions-manager/cmp/rwx/2ce531e-main",
                "pr_title": "Update reusable workflow",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": [rwx_wf],
            },
        }

        _save_prs_and_update_status(
            results,
            project,
            [caller_wf1, caller_wf2],
            [rwx_wf],
            db_session,
            github_user=TEST_USER,
        )

        prs = {
            pr.repo_name: pr
            for pr in db_session.query(ProjectPullRequest).all()
        }

        # Caller repo PR must list only its own workflows
        caller_pr = prs["org/caller-repo-a"]
        assert caller_pr.workflow_names == f"{caller_wf1}, {caller_wf2}"

        # RWX repo PR must list only the reusable workflow — not the caller workflows
        rwx_pr = prs["org/rwx-repo"]
        assert rwx_pr.workflow_names == rwx_wf
        assert caller_wf1 not in (rwx_pr.workflow_names or "")
        assert caller_wf2 not in (rwx_pr.workflow_names or "")

    def test_custom_file_only_pr_has_null_workflow_names(self, db_session):
        """A PR that commits only custom files (no workflows) must have
        workflow_names=None so the UI renders 'No workflows recorded'."""
        _, project = _create_account_and_project(db_session)

        results = {
            "org/repo-a on main": {
                "status": "pr_created",
                "pr_number": 20,
                "pr_url": "https://github.com/org/repo-a/pull/20",
                "branch_name": "actions-manager/cmp/a/111-main",
                "pr_title": "Add custom file",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": [],
            },
        }

        _save_prs_and_update_status(
            results, project, [], [], db_session, github_user=TEST_USER
        )

        pr = db_session.query(ProjectPullRequest).one()
        assert pr.workflow_names is None


class TestPerPRFileNames:
    """Regression tests for per-PR file_names storage (custom files + CODEOWNERS).

    Each PR row must record only the custom files and CODEOWNERS paths actually
    committed to that specific repo/branch — not files from other repos in the run.
    """

    def test_custom_files_stored_per_pr(self, db_session):
        """custom_files_committed paths must be recorded in file_names on the PR row."""
        _, project = _create_account_and_project(db_session)

        results = {
            "org/repo-a on main": {
                "status": "pr_created",
                "pr_number": 10,
                "pr_url": "https://github.com/org/repo-a/pull/10",
                "branch_name": "actions-manager/cmp/a/111-main",
                "pr_title": "Deploy files",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": ["deploy.yml"],
                "custom_files_committed": [".github/myfiles/test.txt", ".github/scripts/run.sh"],
                "codeowners_committed": "",
            },
        }

        _save_prs_and_update_status(results, project, ["deploy.yml"], [], db_session, github_user=TEST_USER)

        pr = db_session.query(ProjectPullRequest).one()
        assert pr.file_names == ".github/myfiles/test.txt, .github/scripts/run.sh"

    def test_codeowners_path_stored_per_pr(self, db_session):
        """codeowners_committed path must be appended to file_names on the PR row."""
        _, project = _create_account_and_project(db_session)

        results = {
            "org/repo-b on main": {
                "status": "pr_created",
                "pr_number": 20,
                "pr_url": "https://github.com/org/repo-b/pull/20",
                "branch_name": "actions-manager/cmp/b/111-main",
                "pr_title": "Add CODEOWNERS",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": ["ci.yml"],
                "custom_files_committed": [],
                "codeowners_committed": ".github/CODEOWNERS",
            },
        }

        _save_prs_and_update_status(results, project, ["ci.yml"], [], db_session, github_user=TEST_USER)

        pr = db_session.query(ProjectPullRequest).one()
        assert pr.file_names == ".github/CODEOWNERS"

    def test_mixed_workflows_custom_files_codeowners_per_pr(self, db_session):
        """All three file types are stored correctly and scoped per repo."""
        _, project = _create_account_and_project(db_session)

        results = {
            "org/repo-a on main": {
                "status": "pr_created",
                "pr_number": 30,
                "pr_url": "https://github.com/org/repo-a/pull/30",
                "branch_name": "actions-manager/cmp/a/111-main",
                "pr_title": "Mixed PR",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": ["build.yml"],
                "custom_files_committed": [".github/scripts/setup.sh"],
                "codeowners_committed": ".github/CODEOWNERS",
            },
            "org/repo-b on main": {
                "status": "pr_created",
                "pr_number": 31,
                "pr_url": "https://github.com/org/repo-b/pull/31",
                "branch_name": "actions-manager/cmp/b/111-main",
                "pr_title": "Workflow only PR",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": ["build.yml"],
                "custom_files_committed": [],
                "codeowners_committed": "",
            },
        }

        _save_prs_and_update_status(results, project, ["build.yml"], [], db_session, github_user=TEST_USER)

        prs = {pr.repo_name: pr for pr in db_session.query(ProjectPullRequest).all()}

        # repo-a gets both custom file and CODEOWNERS in file_names
        assert prs["org/repo-a"].file_names == ".github/scripts/setup.sh, .github/CODEOWNERS"
        # repo-b has no custom files or CODEOWNERS
        assert prs["org/repo-b"].file_names is None

    def test_no_custom_files_or_codeowners_gives_null_file_names(self, db_session):
        """A PR with only workflow commits must have file_names=None."""
        _, project = _create_account_and_project(db_session)

        results = {
            "org/repo-a on main": {
                "status": "pr_created",
                "pr_number": 40,
                "pr_url": "https://github.com/org/repo-a/pull/40",
                "branch_name": "actions-manager/cmp/a/111-main",
                "pr_title": "Workflow only",
                "pr_author": TEST_USER,
                "pr_body": "body",
                "workflows_committed": ["release.yml"],
                "custom_files_committed": [],
                "codeowners_committed": "",
            },
        }

        _save_prs_and_update_status(results, project, ["release.yml"], [], db_session, github_user=TEST_USER)

        pr = db_session.query(ProjectPullRequest).one()
        assert pr.file_names is None


class TestReusableWorkflowPRHighlighting:
    """
    /api/project-pr-campaigns must mark PR rows as ``is_reusable_workflow_pr``
    when the committed workflow is a linked reusable workflow, so the frontend
    can render the row purple — regardless of whether the PR happens to also
    carry a ``source_project_name`` (https://github.com/greywolflabs/actions-manager
    PR feedback: the row stayed blue for real reusable-workflow PRs).

    A reusable workflow rollout to a caller ("standard") project creates its PR
    row with ``project_id`` set to the *caller* project, not the RWX project —
    so ``is_reusable_workflow_pr`` cannot be derived from ``pr.project_id``
    alone. It must be derived from whether the PR's committed workflow name
    matches a workflow actually linked via ``LinkedReusableWorkflow``.
    """

    def _create_linked_setup(self, db):
        account = Account(
            github_user=TEST_USER,
            github_email="campaign@example.com",
            account_type="free",
        )
        db.add(account)
        db.commit()
        db.refresh(account)

        std_proj = Project(
            project_name=TEST_PROJECT,
            project_code="CMP",
            user_id=account.user_id,
            branch_option="default",
            reusable_workflows_enabled=True,
            pr_state="synced",
            project_type="standard",
        )
        db.add(std_proj)
        db.commit()
        db.refresh(std_proj)

        rwx_proj = Project(
            project_name="rwx_source_project",
            project_code="RWX",
            user_id=account.user_id,
            branch_option="default",
            reusable_workflows_enabled=True,
            pr_state="synced",
            project_type="rwx",
        )
        db.add(rwx_proj)
        db.commit()
        db.refresh(rwx_proj)

        wf = Workflow(
            workflow_name="reusable",
            workflow_yaml="name: reusable",
            reusable_workflow=True,
        )
        db.add(wf)
        db.commit()
        db.refresh(wf)
        db.add(ProjectWorkflow(project_id=rwx_proj.project_id, workflow_id=wf.workflow_id))
        db.add(LinkedReusableWorkflow(
            standard_project_id=std_proj.project_id,
            rwx_project_id=rwx_proj.project_id,
            workflow_id=wf.workflow_id,
        ))
        db.commit()

        return std_proj, rwx_proj

    def test_reusable_workflow_pr_committed_on_caller_project_is_flagged(self, db_session):
        """A reusable-workflow rollout PR row lives on the caller project_id
        (not the RWX project) but must still be flagged as reusable."""
        std_proj, _ = self._create_linked_setup(db_session)

        results = {
            "org/repo-a on main": _pr_result(50, "actions-manager/cmp/a/rwx-main"),
        }
        results["org/repo-a on main"]["workflows_committed"] = ["AM_RWX_reusable.yml"]

        _save_prs_and_update_status(
            results, std_proj, [], ["reusable.yml"], db_session, github_user=TEST_USER
        )

        response = _get_campaigns()
        assert response.status_code == 200
        prs = response.json()["pull_requests"]
        assert len(prs) == 1
        assert prs[0]["is_reusable_workflow_pr"] is True

    def test_regular_caller_workflow_pr_is_not_flagged(self, db_session):
        """A PR for the caller's own (non-reusable) workflow stays unflagged/blue."""
        std_proj, _ = self._create_linked_setup(db_session)

        results = {
            "org/repo-a on main": _pr_result(51, "actions-manager/cmp/a/build-main"),
        }
        results["org/repo-a on main"]["workflows_committed"] = ["build.yml"]

        _save_prs_and_update_status(
            results, std_proj, ["build.yml"], [], db_session, github_user=TEST_USER
        )

        response = _get_campaigns()
        assert response.status_code == 200
        prs = response.json()["pull_requests"]
        assert len(prs) == 1
        assert prs[0]["is_reusable_workflow_pr"] is False


class TestCampaignCreationSnapshot:
    """A campaign must freeze what it was aimed at, at creation time.

    Everything else a campaign displays is derived live from its surviving PR
    rows, so without this a campaign silently re-reads against today's repo
    list, branch heads and workflow content, and a target that produced no PR
    leaves no trace at all.
    """

    def _workflow(self, db, project, name, yaml_content, versions=0):
        workflow = Workflow(workflow_name=name, workflow_yaml=yaml_content)
        db.add(workflow)
        db.commit()
        db.refresh(workflow)
        db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
        for number in range(1, versions + 1):
            db.add(WorkflowVersion(
                workflow_id=workflow.workflow_id,
                version_number=number,
                content=yaml_content,
            ))
        db.commit()
        return workflow

    def test_snapshot_persists_targets_base_commits_and_policy_version(self, db_session):
        _, project = _create_account_and_project(db_session)
        self._workflow(db_session, project, "wf222.yml", "name: ci", versions=3)

        results = {
            "org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main"),
            "org/repo-b on main": _pr_result(11, "actions-manager/cmp/b/222-main"),
        }
        results["org/repo-a on main"]["base_sha"] = "a" * 40
        results["org/repo-b on main"]["base_sha"] = "b" * 40

        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a", "org/repo-b"],
        )

        campaign = db_session.query(ProjectPRCampaign).one()
        assert json.loads(campaign.target_repos) == ["org/repo-a", "org/repo-b"]
        assert json.loads(campaign.base_commits) == {
            "org/repo-a on main": "a" * 40,
            "org/repo-b on main": "b" * 40,
        }
        policy = json.loads(campaign.policy_version)
        assert policy["wf222.yml"]["version"] == 3
        assert policy["wf222.yml"]["sha256"] == hashlib.sha256(b"name: ci").hexdigest()

    def test_never_versioned_workflow_is_still_pinned_by_hash(self, db_session):
        _, project = _create_account_and_project(db_session)
        self._workflow(db_session, project, "wf222.yml", "name: ci", versions=0)

        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a"],
        )

        policy = json.loads(db_session.query(ProjectPRCampaign).one().policy_version)
        assert policy["wf222.yml"]["version"] is None
        assert policy["wf222.yml"]["sha256"] == hashlib.sha256(b"name: ci").hexdigest()

    def test_target_that_produced_no_pr_is_still_snapshotted(self, db_session):
        _, project = _create_account_and_project(db_session)

        results = {
            "org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main"),
            "org/repo-b on main": {"status": "error", "error": "boom", "base_sha": None},
        }
        results["org/repo-a on main"]["base_sha"] = "a" * 40

        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a", "org/repo-b"],
        )

        campaign = db_session.query(ProjectPRCampaign).one()
        assert json.loads(campaign.target_repos) == ["org/repo-a", "org/repo-b"]
        assert json.loads(campaign.base_commits)["org/repo-b on main"] is None
        # Only repo-a actually opened a PR.
        assert db_session.query(ProjectPullRequest).count() == 1

    def test_snapshot_fields_cannot_be_changed_after_creation(self, db_session):
        _, project = _create_account_and_project(db_session)
        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a"],
        )

        campaign = db_session.query(ProjectPRCampaign).one()
        for field in ("target_repos", "base_commits", "policy_version"):
            with pytest.raises(ValueError):
                setattr(campaign, field, json.dumps(["org/repo-tampered"]))

    def test_snapshot_survives_the_project_changing_afterwards(self, db_session):
        """Adding/removing repos and editing the workflow after the fact must
        not rewrite what the campaign says it went out with."""
        _, project = _create_account_and_project(db_session)
        workflow = self._workflow(db_session, project, "wf222.yml", "name: ci", versions=1)

        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        results["org/repo-a on main"]["base_sha"] = "a" * 40
        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a"],
        )

        workflow.workflow_yaml = "name: ci-edited"
        db_session.add(WorkflowVersion(
            workflow_id=workflow.workflow_id, version_number=2, content="name: ci-edited",
        ))
        db_session.commit()

        response = _get_campaigns()
        assert response.status_code == 200
        campaign = response.json()["campaigns"][0]
        assert campaign["target_repos"] == ["org/repo-a"]
        assert campaign["base_commits"] == {"org/repo-a on main": "a" * 40}
        assert campaign["policy_version"]["wf222.yml"]["version"] == 1
        assert campaign["policy_version"]["wf222.yml"]["sha256"] == hashlib.sha256(b"name: ci").hexdigest()

    def test_legacy_campaign_without_snapshot_reads_as_empty(self, db_session):
        _, project = _create_account_and_project(db_session)
        campaign = ProjectPRCampaign(project_id=project.project_id, created_by=TEST_USER)
        db_session.add(campaign)
        db_session.commit()
        db_session.refresh(campaign)
        db_session.add(ProjectPullRequest(
            project_id=project.project_id, campaign_id=campaign.campaign_id,
            repo_name="org/repo-a", pr_number=1, pr_url="https://github.com/org/repo-a/pull/1",
            pr_state="open", branch_name="actions-manager/cmp/a/1-main", target_branch="main",
        ))
        db_session.commit()

        response = _get_campaigns()
        assert response.status_code == 200
        item = response.json()["campaigns"][0]
        assert item["target_repos"] == []
        assert item["base_commits"] == {}
        assert item["policy_version"] == {}


class TestBranchProtectionSnapshot:
    """Protection rules are captured at the same moment as the base commit.

    "no protection configured" and "could not read protection" are different
    facts about a rollout; collapsing them into one null would make the
    snapshot lie about what the branch looked like.
    """

    def _response(self, status_code, payload=None, text=""):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = payload or {}
        response.text = text
        return response

    def test_protected_branch_summary(self):
        payload = {
            "required_pull_request_reviews": {"required_approving_review_count": 2},
            "required_status_checks": {"strict": True, "contexts": ["ci/test", "lint"]},
            "enforce_admins": {"enabled": True},
        }
        with patch("workflows.requests.get", return_value=self._response(200, payload)):
            summary = _fetch_branch_protection("org", "repo-a", "main", {})

        assert summary == {
            "status": "protected",
            "required_reviews": 2,
            "required_status_checks": ["ci/test", "lint"],
            "enforce_admins": True,
        }

    def test_protected_branch_reads_newer_checks_list(self):
        """GitHub deprecated `contexts` in favour of `checks`."""
        payload = {
            "required_status_checks": {"contexts": [], "checks": [{"context": "build"}]},
            "enforce_admins": {"enabled": False},
        }
        with patch("workflows.requests.get", return_value=self._response(200, payload)):
            summary = _fetch_branch_protection("org", "repo-a", "main", {})

        assert summary["required_status_checks"] == ["build"]
        assert summary["required_reviews"] is None
        assert summary["enforce_admins"] is False

    def test_unprotected_branch_is_none_not_unknown(self):
        with patch("workflows.requests.get", return_value=self._response(404, text="Branch not protected")):
            assert _fetch_branch_protection("org", "repo-a", "main", {}) == {"status": "none"}

    def test_permission_denied_is_unknown_with_reason(self):
        with patch("workflows.requests.get",
                   return_value=self._response(403, text="Resource not accessible by integration")):
            summary = _fetch_branch_protection("org", "repo-a", "main", {})

        assert summary["status"] == "unknown"
        assert "403" in summary["error"]

    def test_fetch_failure_never_breaks_campaign_creation(self):
        with patch("workflows.requests.get", side_effect=RuntimeError("socket exploded")):
            summary = _fetch_branch_protection("org", "repo-a", "main", {})

        assert summary["status"] == "unknown"
        # Named by kind: the exception's own text stays in the server log (#2297).
        assert summary["error"] == "unexpected error"


class TestCampaignPRUrlAndProtectionSnapshot:

    def test_snapshot_records_pr_url_and_protection_per_target(self, db_session):
        _, project = _create_account_and_project(db_session)
        protected = {
            "status": "protected", "required_reviews": 1,
            "required_status_checks": ["ci"], "enforce_admins": True,
        }

        results = {
            "org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main"),
            "org/repo-b on main": {"status": "error", "error": "boom"},
        }
        results["org/repo-a on main"]["branch_protection"] = protected
        results["org/repo-b on main"]["branch_protection"] = {"status": "none"}

        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a", "org/repo-b"],
        )

        campaign = db_session.query(ProjectPRCampaign).one()
        assert json.loads(campaign.target_pr_urls) == {
            "org/repo-a on main": "https://github.com/org/repo/pull/10",
        }
        assert json.loads(campaign.branch_protection) == {
            "org/repo-a on main": protected,
            "org/repo-b on main": {"status": "none"},
        }

    def test_pr_url_and_protection_reach_the_campaigns_endpoint(self, db_session):
        _, project = _create_account_and_project(db_session)
        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        results["org/repo-a on main"]["branch_protection"] = {"status": "none"}

        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a"],
        )

        response = _get_campaigns()
        assert response.status_code == 200
        campaign = response.json()["campaigns"][0]
        assert campaign["target_pr_urls"] == {"org/repo-a on main": "https://github.com/org/repo/pull/10"}
        assert campaign["branch_protection"] == {"org/repo-a on main": {"status": "none"}}

    def test_pr_url_slot_can_be_filled_once_and_never_changed(self, db_session):
        """The CODEOWNERS deploy path opens its PR after the campaign row exists."""
        _, project = _create_account_and_project(db_session)
        campaign = ProjectPRCampaign(
            project_id=project.project_id, created_by=TEST_USER,
            target_repos=json.dumps(["org/repo-a"]),
        )
        db_session.add(campaign)
        db_session.commit()
        db_session.refresh(campaign)

        campaign.record_pr_url("org/repo-a on main", "https://github.com/org/repo-a/pull/7")
        db_session.commit()
        assert json.loads(campaign.target_pr_urls) == {
            "org/repo-a on main": "https://github.com/org/repo-a/pull/7",
        }

        with pytest.raises(ValueError):
            campaign.record_pr_url("org/repo-a on main", "https://github.com/org/repo-a/pull/8")

        # A different target may still be filled in.
        campaign.record_pr_url("org/repo-b on main", "https://github.com/org/repo-b/pull/9")
        db_session.commit()
        assert len(json.loads(campaign.target_pr_urls)) == 2

    def test_direct_assignment_and_frozen_fields_still_rejected(self, db_session):
        _, project = _create_account_and_project(db_session)
        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a"],
        )
        campaign = db_session.query(ProjectPRCampaign).one()

        with pytest.raises(ValueError):
            campaign.target_pr_urls = json.dumps({"org/repo-a on main": "https://tampered"})
        for field in ("target_repos", "base_commits", "policy_version", "branch_protection"):
            with pytest.raises(ValueError):
                setattr(campaign, field, json.dumps({"tampered": True}))


class TestCampaignPRBody:
    """The PR itself should say what it is delivering.

    A bare list of workflow names does not tell a reviewer *which* version of
    each workflow went out, so a PR read months later cannot be checked against
    what the campaign intended.
    """

    def _project_with_workflows(self, db):
        _, project = _create_account_and_project(db)
        for name, yaml_content, versions in [("build-and-test", "name: CI", 12), ("never-saved", "name: X", 0)]:
            workflow = Workflow(workflow_name=name, workflow_yaml=yaml_content)
            db.add(workflow)
            db.commit()
            db.refresh(workflow)
            db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
            for number in range(1, versions + 1):
                db.add(WorkflowVersion(
                    workflow_id=workflow.workflow_id, version_number=number, content=yaml_content,
                ))
        db.commit()
        return project

    def test_body_carries_versions_hashes_base_commit_and_files(self, db_session):
        self._project_with_workflows(db_session)

        with patch("workflows.auth_module.FRONTEND_URL", "https://am.example.com"):
            body = _campaign_pr_body(
                db_session, "CMP", TEST_USER, ["build-and-test", "never-saved"], "main",
                "4f2a91c3d5e7b90a1c2d3e4f5a6b7c8d9e0f1a2b",
                {"custom_files_committed": ["scripts/deploy.sh"],
                 "codeowners_committed": ".github/CODEOWNERS"},
            )

        assert "| build-and-test | v12 |" in body
        # Never versioned, so there is no version to show — the hash still pins it.
        assert "| never-saved | — |" in body
        assert f"`{hashlib.sha256(b'name: CI').hexdigest()[:16]}`" in body
        assert "**Target branch:** `main`" in body
        assert "**Base commit:** `4f2a91c`" in body
        assert "`scripts/deploy.sh`, `.github/CODEOWNERS`" in body
        assert f"https://am.example.com/project/{TEST_USER}/{TEST_PROJECT}" in body

    def test_link_is_omitted_when_no_app_url_is_configured(self, db_session):
        """The frontend auto-detects its own URL; the backend cannot guess it,
        and a wrong link is worse than none."""
        self._project_with_workflows(db_session)

        with patch("workflows.auth_module.FRONTEND_URL", ""):
            body = _campaign_pr_body(db_session, "CMP", TEST_USER, ["build-and-test"], "main", None, None)

        assert "**Campaign:**" not in body
        assert "http" not in body
        # The rest of the detail is still there.
        assert "| build-and-test | v12 |" in body
        assert "**Base commit:**" not in body

    def test_body_reaches_the_created_pull_request(self, db_session):
        self._project_with_workflows(db_session)
        created = MagicMock()
        created.status_code = 201
        created.json.return_value = {"number": 7, "html_url": "https://github.com/org/repo-a/pull/7"}

        with patch("workflows.auth_module.FRONTEND_URL", "https://am.example.com"), \
             patch("workflows._check_existing_pr", return_value=None), \
             patch("workflows._get_authenticated_headers", return_value={"Authorization": "token x"}), \
             patch("workflows.requests.post", return_value=created) as mock_post:
            _finalize_pr_result(
                "org", "repo-a", "actions-manager/cmp/a/1-main", "main", {}, TEST_USER, db_session,
                "CMP", None, "org/repo-a on main",
                delivery={
                    "workflows_committed": ["build-and-test"],
                    "base_sha": "4f2a91c3d5e7b90a1c2d3e4f5a6b7c8d9e0f1a2b",
                },
            )

        body = mock_post.call_args.kwargs["json"]["body"]
        assert "| build-and-test | v12 |" in body
        assert "**Base commit:** `4f2a91c`" in body
        assert "https://am.example.com/project/" in body

    def test_unknown_project_code_still_produces_a_usable_body(self, db_session):
        """Never raise out of PR creation just because the snapshot lookup missed."""
        body = _campaign_pr_body(db_session, "NOPE", TEST_USER, ["build-and-test"], "main", None, None)

        assert "**NOPE**" in body
        assert "| build-and-test | — | — |" in body


class TestUserNamedCampaigns:
    """A campaign the user named must keep that name.

    Before this, campaign_name was derived from the PR rows on every read, so
    every run against the same workflows read identically and the campaign list
    was impossible to scan.
    """

    def test_name_and_description_persist_and_come_back(self, db_session):
        _, project = _create_account_and_project(db_session)
        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        results["org/repo-a on main"]["workflows_committed"] = ["wf222.yml"]

        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a"],
            campaign_name="Q3 security rollout",
            campaign_description="Pinning actions to commit SHAs.",
        )

        campaign = db_session.query(ProjectPRCampaign).one()
        assert campaign.campaign_name == "Q3 security rollout"
        assert campaign.campaign_description == "Pinning actions to commit SHAs."

        response = _get_campaigns()
        assert response.status_code == 200
        item = response.json()["campaigns"][0]
        assert item["campaign_name"] == "Q3 security rollout"
        assert item["campaign_description"] == "Pinning actions to commit SHAs."

    def test_unnamed_campaign_still_uses_the_derived_name(self, db_session):
        _, project = _create_account_and_project(db_session)
        results = {"org/repo-a on main": _pr_result(10, "actions-manager/cmp/a/111-main")}
        results["org/repo-a on main"]["workflows_committed"] = ["wf222.yml"]

        _save_prs_and_update_status(
            results, project, ["wf222.yml"], [], db_session, github_user=TEST_USER,
            repo_names=["org/repo-a"],
        )

        item = _get_campaigns().json()["campaigns"][0]
        assert item["campaign_name"] == "Update wf222.yml"
        assert item["campaign_description"] is None

    def test_whitespace_only_name_is_not_stored(self):
        """A blank title would render as an empty campaign card."""
        payload = CreatePullRequestsRequest(
            project_name=TEST_PROJECT, campaign_name="   ", campaign_description="\n\t ",
        )
        assert payload.campaign_name is None
        assert payload.campaign_description is None

    def test_name_is_trimmed(self):
        payload = CreatePullRequestsRequest(project_name=TEST_PROJECT, campaign_name="  Q3 rollout  ")
        assert payload.campaign_name == "Q3 rollout"

    def test_over_long_name_is_rejected_not_truncated(self):
        with pytest.raises(ValidationError):
            CreatePullRequestsRequest(project_name=TEST_PROJECT, campaign_name="x" * 201)
        with pytest.raises(ValidationError):
            CreatePullRequestsRequest(project_name=TEST_PROJECT, campaign_description="x" * 2001)

    def test_pr_body_leads_with_the_name_and_description(self, db_session):
        _create_account_and_project(db_session)
        body = _campaign_pr_body(
            db_session, "CMP", TEST_USER, [], "main", None, None,
            {"name": "Q3 security rollout", "description": "Pinning actions to commit SHAs."},
        )

        assert body.startswith("## Q3 security rollout\n")
        assert "Pinning actions to commit SHAs." in body
        assert "Delivered by ActionsManager for project **CMP**." in body

    def test_pr_body_is_unchanged_when_nothing_was_named(self, db_session):
        _create_account_and_project(db_session)
        without = _campaign_pr_body(db_session, "CMP", TEST_USER, [], "main", None, None)
        explicit_none = _campaign_pr_body(db_session, "CMP", TEST_USER, [], "main", None, None, None)

        assert without == explicit_none
        assert without.startswith("This PR updates ActionsManager workflows for project **CMP**.")
        assert "##" not in without


class TestSnapshotTruthfulness:
    """Regressions for review findings on PR #1907.

    Every one of these was the snapshot asserting something that did not happen.
    """

    def test_reusable_only_run_does_not_claim_the_caller_repos(self):
        """A reusable-only campaign never touches the project's caller repos,
        so recording them as targets would render a 'no PR opened' row for each."""
        reusable_only = CreatePullRequestsRequest(
            project_name=TEST_PROJECT, selected_reusable_workflows=["shared.yml"],
        )
        assert _includes_regular_workflows(reusable_only) is False

        # The backward-compatible default, and an explicit caller selection, both target them.
        assert _includes_regular_workflows(CreatePullRequestsRequest(project_name=TEST_PROJECT)) is True
        assert _includes_regular_workflows(CreatePullRequestsRequest(
            project_name=TEST_PROJECT, selected_workflows=["ci.yml"],
            selected_reusable_workflows=["shared.yml"],
        )) is True

    def test_unreached_target_is_unknown_protection_not_unprotected(self, db_session):
        """A 404 from a repo the campaign never reached is 'we could not tell',
        not 'this branch has no protection'."""
        _, project = _create_account_and_project(db_session)
        results = {"org/gone on main": {
            "status": "error", "error": "Failed to get target branch 'main': 404",
            "branch_protection": {"status": "unknown", "error": "Failed to get target branch 'main': 404"},
        }}

        _save_prs_and_update_status(
            results, project, [], [], db_session, github_user=TEST_USER, repo_names=["org/gone"],
        )
        # No PRs, so no campaign — assert on the snapshot builder directly instead.
        snapshot = _build_campaign_snapshot(db_session, project, ["org/gone"], results, [])
        assert json.loads(snapshot["branch_protection"])["org/gone on main"]["status"] == "unknown"

    def test_version_is_dropped_when_it_does_not_match_the_shipped_content(self, db_session):
        """Reporting 'v7' for YAML that is not v7 is the exact misreading the
        snapshot exists to prevent."""
        _, project = _create_account_and_project(db_session)
        workflow = Workflow(workflow_name="ci.yml", workflow_yaml="name: edited-directly")
        db_session.add(workflow)
        db_session.commit()
        db_session.refresh(workflow)
        db_session.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
        db_session.add(WorkflowVersion(
            workflow_id=workflow.workflow_id, version_number=7, content="name: whatever-v7-was",
        ))
        db_session.commit()

        applied = _build_policy_version(db_session, project.project_id, ["ci.yml"])
        assert applied["ci.yml"]["version"] is None
        assert applied["ci.yml"]["sha256"] == hashlib.sha256(b"name: edited-directly").hexdigest()

    def test_version_is_reported_when_it_does_match(self, db_session):
        _, project = _create_account_and_project(db_session)
        workflow = Workflow(workflow_name="ci.yml", workflow_yaml="name: CI")
        db_session.add(workflow)
        db_session.commit()
        db_session.refresh(workflow)
        db_session.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
        for number, content in [(6, "name: old"), (7, "name: CI")]:
            db_session.add(WorkflowVersion(
                workflow_id=workflow.workflow_id, version_number=number, content=content,
            ))
        db_session.commit()

        assert _build_policy_version(db_session, project.project_id, ["ci.yml"])["ci.yml"]["version"] == 7

    def test_codeowners_pr_url_is_recorded_into_a_supplied_campaign(self, db_session):
        """The modal's normal flow creates the campaign first and passes its id;
        that PR's URL must still reach the snapshot."""
        _, project = _create_account_and_project(db_session)
        campaign = ProjectPRCampaign(project_id=project.project_id, created_by=TEST_USER)
        db_session.add(campaign)
        db_session.commit()
        db_session.refresh(campaign)

        campaign.record_pr_url("org/repo-a on main", "https://github.com/org/repo-a/pull/7")
        db_session.commit()

        assert json.loads(campaign.target_pr_urls) == {
            "org/repo-a on main": "https://github.com/org/repo-a/pull/7",
        }
