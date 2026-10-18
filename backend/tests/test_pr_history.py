"""
Tests for the PR History view, served by GET /api/project-pr-campaigns.
"""

import sys
import os
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import Base, Account, Project, ProjectPullRequest, LinkedReusableWorkflow, Workflow, ProjectWorkflow
from main import app
from workflows import get_db

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_pr_history.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


# TestClient is created once; dependency override is applied/removed per test.
client = TestClient(app)

TEST_USER = "historyuser"
TEST_PROJECT = "history_project"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_account_and_project(db):
    account = Account(
        github_user=TEST_USER,
        github_email="history@example.com",
        account_type="free",
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    project = Project(
        project_name=TEST_PROJECT,
        project_code="HPS",
        user_id=account.user_id,
        branch_option="default",
        reusable_workflows_enabled=False,
        pr_state="synced",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return account, project


def _add_pr(db, project_id, *, repo_name, pr_number, pr_state, title=None,
            author=None, workflow_names=None, merged_at=None, closed_at=None):
    # Use pr_number in the branch name to ensure uniqueness per-PR in tests
    pr = ProjectPullRequest(
        project_id=project_id,
        repo_name=repo_name,
        pr_number=pr_number,
        pr_url=f"https://github.com/{repo_name}/pull/{pr_number}",
        pr_state=pr_state,
        branch_name=f"actions-manager/hps-main-{pr_number}",
        target_branch="main",
        title=title,
        author=author,
        workflow_names=workflow_names,
        merged_at=merged_at,
        closed_at=closed_at,
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    return pr


def _add_workflow(db, project_id, *, name="campaign.yml", status="under_review"):
    workflow = Workflow(
        workflow_name=name,
        workflow_yaml="name: Campaign\non: push",
        reusable_workflow=False,
        workflow_status=status,
    )
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    db.add(ProjectWorkflow(project_id=project_id, workflow_id=workflow.workflow_id))
    db.commit()
    return workflow


# ---------------------------------------------------------------------------
# Cross-project PR history helpers
# ---------------------------------------------------------------------------

CROSS_USER = "crossuser"
STD_PROJECT = "std_project"
RWX_PROJECT = "rwx_project"


def _create_cross_project_setup(db):
    """
    Create an account, a Standard project, and an RWX project, link them via a
    LinkedReusableWorkflow row, and return (account, std_proj, rwx_proj).
    """
    account = Account(
        github_user=CROSS_USER,
        github_email="cross@example.com",
        account_type="free",
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    std_proj = Project(
        project_name=STD_PROJECT,
        project_code="STD",
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
        project_name=RWX_PROJECT,
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

    # A workflow owned by the RWX project, so the link row has a valid workflow_id.
    wf = Workflow(
        workflow_name="reusable.yml",
        workflow_yaml="name: reusable",
        reusable_workflow=True,
    )
    db.add(wf)
    db.commit()
    db.refresh(wf)

    db.add(ProjectWorkflow(project_id=rwx_proj.project_id, workflow_id=wf.workflow_id))
    db.commit()

    db.add(LinkedReusableWorkflow(
        standard_project_id=std_proj.project_id,
        rwx_project_id=rwx_proj.project_id,
        workflow_id=wf.workflow_id,
    ))
    db.commit()

    return account, std_proj, rwx_proj


def _add_pr_for_project(db, project_id, pr_number):
    """Add a merged PR belonging to ``project_id``."""
    pr = ProjectPullRequest(
        project_id=project_id,
        repo_name="org/repo",
        pr_number=pr_number,
        pr_url=f"https://github.com/org/repo/pull/{pr_number}",
        pr_state="merged",
        branch_name=f"actions-manager/branch-{project_id}-{pr_number}",
        target_branch="main",
        title=f"PR #{pr_number} from project {project_id}",
        merged_at=datetime.now(timezone.utc),
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    return pr


# ---------------------------------------------------------------------------
# Cross-project PR history tests
# ---------------------------------------------------------------------------

class TestCrossProjectPRHistory:
    """
    PR History is shared both ways across a linked Standard ↔ RWX project pair,
    through the visibility map /api/project-pr-campaigns builds.
    """

    @pytest.fixture(autouse=True)
    def setup_db(self, authenticate_client):
        app.dependency_overrides[get_db] = override_get_db
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        authenticate_client(client, CROSS_USER, TestingSessionLocal)
        yield
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.pop(get_db, None)

    def _history(self, project_name):
        with patch("workflows.user_tokens", {CROSS_USER: "fake-token"}):
            response = client.get("/api/project-pr-campaigns", params={"project_name": project_name})
        assert response.status_code == 200, response.text
        return response.json()["pull_requests"]

    def test_standard_project_includes_rwx_prs(self):
        _, std_proj, rwx_proj = _create_cross_project_setup(self.db)
        _add_pr_for_project(self.db, std_proj.project_id, pr_number=1)
        _add_pr_for_project(self.db, rwx_proj.project_id, pr_number=2)

        assert {pr["pr_number"] for pr in self._history(STD_PROJECT)} == {1, 2}

    def test_rwx_pr_carries_source_project_name_in_standard_query(self):
        _, _, rwx_proj = _create_cross_project_setup(self.db)
        _add_pr_for_project(self.db, rwx_proj.project_id, pr_number=10)

        prs = self._history(STD_PROJECT)
        assert len(prs) == 1
        assert prs[0]["source_project_name"] == RWX_PROJECT

    def test_own_prs_have_no_source_project_name(self):
        _, std_proj, _ = _create_cross_project_setup(self.db)
        _add_pr_for_project(self.db, std_proj.project_id, pr_number=5)

        prs = self._history(STD_PROJECT)
        assert len(prs) == 1
        assert prs[0]["source_project_name"] is None

    def test_rwx_project_includes_standard_prs(self):
        _, std_proj, rwx_proj = _create_cross_project_setup(self.db)
        _add_pr_for_project(self.db, std_proj.project_id, pr_number=3)
        _add_pr_for_project(self.db, rwx_proj.project_id, pr_number=4)

        assert {pr["pr_number"] for pr in self._history(RWX_PROJECT)} == {3, 4}

    def test_standard_pr_carries_source_project_name_in_rwx_query(self):
        _, std_proj, _ = _create_cross_project_setup(self.db)
        _add_pr_for_project(self.db, std_proj.project_id, pr_number=20)

        prs = self._history(RWX_PROJECT)
        assert len(prs) == 1
        assert prs[0]["source_project_name"] == STD_PROJECT

    def test_unlinked_project_does_not_see_other_project_prs(self):
        _, std_proj, rwx_proj = _create_cross_project_setup(self.db)
        unlinked = Project(
            project_name="unlinked_project",
            project_code="UNL",
            user_id=std_proj.user_id,
            branch_option="default",
            reusable_workflows_enabled=False,
            pr_state="synced",
            project_type="standard",
        )
        self.db.add(unlinked)
        self.db.commit()
        _add_pr_for_project(self.db, rwx_proj.project_id, pr_number=99)

        assert self._history("unlinked_project") == []


# ---------------------------------------------------------------------------
# PR Campaigns tests
# ---------------------------------------------------------------------------

class TestPRCampaigns:

    @pytest.fixture(autouse=True)
    def setup_db(self, authenticate_client):
        app.dependency_overrides[get_db] = override_get_db
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        authenticate_client(client, TEST_USER, TestingSessionLocal)
        yield
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.pop(get_db, None)

    def test_lists_active_campaign_from_open_prs(self):
        _, project = _create_account_and_project(self.db)
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-a",
            pr_number=10,
            pr_state="open",
            workflow_names="okok.yml",
            author="aireland1010",
        )

        with patch("workflows.user_tokens", {TEST_USER: "fake-token"}):
            response = client.get(
                "/api/project-pr-campaigns",
                params={"github_user": TEST_USER, "project_name": TEST_PROJECT},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["active_campaigns"] == 1
        assert data["completed_campaigns"] == 0
        assert data["open_prs"] == 1
        campaign = data["campaigns"][0]
        assert campaign["campaign_name"] == "Update okok.yml"
        assert campaign["campaign_status"] == "open"
        assert campaign["open_count"] == 1
        assert campaign["created_by"] == "aireland1010"
        assert campaign["completion_percentage"] == 0

    def test_lists_completed_cancelled_and_mixed_campaign_states(self):
        _, project = _create_account_and_project(self.db)
        now = datetime.now(timezone.utc)
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-a",
            pr_number=20,
            pr_state="merged",
            workflow_names="merged.yml",
            merged_at=now,
        )
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-b",
            pr_number=21,
            pr_state="closed",
            workflow_names="closed.yml",
            closed_at=now,
        )
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-c",
            pr_number=22,
            pr_state="merged",
            workflow_names="mixed.yml",
            merged_at=now,
        )
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-d",
            pr_number=23,
            pr_state="closed",
            workflow_names="mixed.yml",
            closed_at=now,
        )

        with patch("workflows.user_tokens", {TEST_USER: "fake-token"}):
            response = client.get(
                "/api/project-pr-campaigns",
                params={"github_user": TEST_USER, "project_name": TEST_PROJECT},
            )

        assert response.status_code == 200
        data = response.json()
        statuses = {campaign["campaign_name"]: campaign["campaign_status"] for campaign in data["campaigns"]}
        assert statuses["Update merged.yml"] == "completed"
        assert statuses["Update closed.yml"] == "cancelled"
        assert statuses["Update mixed.yml"] == "partially_completed"
        assert data["completed_campaigns"] == 3
        assert data["merged_prs"] == 2
        assert data["closed_prs"] == 2
        mixed = next(campaign for campaign in data["campaigns"] if campaign["campaign_name"] == "Update mixed.yml")
        assert mixed["completion_percentage"] == 100

    def test_campaigns_reject_another_users_project(self, authenticate_client):
        _, _ = _create_account_and_project(self.db)
        other = Account(
            github_user="otheruser",
            github_email="other@example.com",
            account_type="free",
        )
        self.db.add(other)
        self.db.commit()

        # Actually sign in as otheruser — this test is about project ownership,
        # not about spoofing someone else's name in the query string.
        authenticate_client(client, "otheruser", TestingSessionLocal)

        with patch("workflows.user_tokens", {"otheruser": "fake-token"}):
            response = client.get(
                "/api/project-pr-campaigns",
                params={"github_user": "otheruser", "project_name": TEST_PROJECT},
            )

        assert response.status_code == 404

    def test_merge_and_close_update_campaign_and_project_state(self):
        _, project = _create_account_and_project(self.db)
        project.pr_state = "open"
        self.db.commit()
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-a",
            pr_number=30,
            pr_state="open",
            workflow_names="rollout.yml",
        )
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-b",
            pr_number=31,
            pr_state="open",
            workflow_names="rollout.yml",
        )
        merge_response = Mock()
        merge_response.status_code = 200
        merge_response.json.return_value = {"sha": "merged-sha"}
        close_response = Mock()
        close_response.status_code = 200
        close_response.json.return_value = {"state": "closed"}

        with patch("workflows.user_tokens", {TEST_USER: "fake-token"}), \
             patch("workflows.github_put", return_value=merge_response), \
             patch("workflows.github_patch", return_value=close_response), \
             patch("workflows._delete_actions_manager_branch", return_value=(True, None)):
            merge = client.put(
                "/api/merge-pull-request",
                json={
                    "github_user": TEST_USER,
                    "project_name": TEST_PROJECT,
                    "repo_name": "org/repo-a",
                    "pr_number": 30,
                },
            )
            close = client.patch(
                "/api/close-pull-request",
                json={
                    "github_user": TEST_USER,
                    "project_name": TEST_PROJECT,
                    "repo_name": "org/repo-b",
                    "pr_number": 31,
                },
            )
            campaigns = client.get(
                "/api/project-pr-campaigns",
                params={"github_user": TEST_USER, "project_name": TEST_PROJECT},
            )

        assert merge.status_code == 200
        assert close.status_code == 200
        assert campaigns.status_code == 200
        campaign = campaigns.json()["campaigns"][0]
        assert campaign["campaign_status"] == "partially_completed"
        assert campaign["merged_count"] == 1
        assert campaign["closed_count"] == 1

        self.db.expire_all()
        updated_project = self.db.query(Project).filter_by(project_id=project.project_id).first()
        assert updated_project.pr_state == "draft"

    def test_final_merge_updates_project_workflow_and_campaign_state(self):
        _, project = _create_account_and_project(self.db)
        project.pr_state = "open"
        self.db.commit()
        workflow = _add_workflow(self.db, project.project_id, name="synced.yml")
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-a",
            pr_number=40,
            pr_state="open",
            workflow_names="synced.yml",
        )
        merge_response = Mock()
        merge_response.status_code = 200
        merge_response.json.return_value = {"sha": "merged-sha"}

        with patch("workflows.user_tokens", {TEST_USER: "fake-token"}), \
             patch("workflows.github_put", return_value=merge_response), \
             patch("workflows._delete_actions_manager_branch", return_value=(True, None)):
            merge = client.put(
                "/api/merge-pull-request",
                json={
                    "github_user": TEST_USER,
                    "project_name": TEST_PROJECT,
                    "repo_name": "org/repo-a",
                    "pr_number": 40,
                },
            )
            campaigns = client.get(
                "/api/project-pr-campaigns",
                params={"github_user": TEST_USER, "project_name": TEST_PROJECT},
            )

        assert merge.status_code == 200
        assert campaigns.status_code == 200
        data = campaigns.json()
        assert data["active_campaigns"] == 0
        assert data["completed_campaigns"] == 1
        assert data["campaigns"][0]["campaign_status"] == "completed"

        self.db.expire_all()
        updated_project = self.db.query(Project).filter_by(project_id=project.project_id).first()
        updated_workflow = self.db.query(Workflow).filter_by(workflow_id=workflow.workflow_id).first()
        assert updated_project.pr_state == "synced"
        assert updated_workflow.workflow_status == "synced_with_github"

    def test_final_close_updates_project_workflow_and_campaign_state(self):
        _, project = _create_account_and_project(self.db)
        project.pr_state = "open"
        self.db.commit()
        workflow = _add_workflow(self.db, project.project_id, name="closed.yml")
        _add_pr(
            self.db,
            project.project_id,
            repo_name="org/repo-a",
            pr_number=41,
            pr_state="open",
            workflow_names="closed.yml",
        )
        close_response = Mock()
        close_response.status_code = 200
        close_response.json.return_value = {"state": "closed"}

        with patch("workflows.user_tokens", {TEST_USER: "fake-token"}), \
             patch("workflows.github_patch", return_value=close_response):
            close = client.patch(
                "/api/close-pull-request",
                json={
                    "github_user": TEST_USER,
                    "project_name": TEST_PROJECT,
                    "repo_name": "org/repo-a",
                    "pr_number": 41,
                },
            )
            campaigns = client.get(
                "/api/project-pr-campaigns",
                params={"github_user": TEST_USER, "project_name": TEST_PROJECT},
            )

        assert close.status_code == 200
        assert campaigns.status_code == 200
        data = campaigns.json()
        assert data["active_campaigns"] == 0
        assert data["completed_campaigns"] == 1
        assert data["campaigns"][0]["campaign_status"] == "cancelled"

        self.db.expire_all()
        updated_project = self.db.query(Project).filter_by(project_id=project.project_id).first()
        updated_workflow = self.db.query(Workflow).filter_by(workflow_id=workflow.workflow_id).first()
        assert updated_project.pr_state == "draft"
        assert updated_workflow.workflow_status == "committed_locally"
