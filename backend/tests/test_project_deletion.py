"""
Tests for the project_deletion module to ensure refactoring maintains functionality.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from project_deletion import (
    _get_project_and_user,
    _get_project_repositories,
    _get_project_workflows,
    _fetch_repository_secrets,
    _fetch_repository_variables,
    _fetch_environment_secrets,
    _fetch_environment_variables,
    _fetch_deployment_environments,
    _process_repository_resources,
    _validate_repository_access,
    _build_deletion_summary,
    _delete_github_resources_for_repository,
    ALREADY_DELETING,
)
import deliveries
from authorization import PROJECT_DELETING, has_project_role
from models import (
    Account, Codeowners, CustomFile, FileDelivery, LinkedReusableWorkflow, Project, ProjectPRCampaign,
    ProjectPullRequest, Repo, Workflow, ProjectRepo, ProjectWorkflow, WorkspaceMember,
)
from database import Base
from tests.campaign_harness import (
    BRANCH as HARNESS_BRANCH,
    USER as HARNESS_USER,
    FakeGitHub,
    delete_project,
    github,
    press_close,
    press_merge,
    run_campaign,
)
from tests.delivery_records import record_pr_carries


# Where project deletion's orphaned-workflow cleanup is looked up (workflows.delete_project_record).
_ORPHAN_CLEANUP = 'workflows.cleanup_orphaned_workflows'


def _make_empty_middleware_factory():
    """Return a DB factory backed by an empty in-memory SQLite database.

    The WriteProtectionMiddleware skips enforcement when the workspace_members
    table has zero rows, so pointing it at a fresh in-memory DB is sufficient to
    let test requests through without auth headers.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestProjectDeletionHelpers:
    """Test class for project deletion helper functions."""

    def test_get_project_and_user_success(self):
        """Test successful user and project retrieval."""
        # Mock database session
        mock_db = Mock(spec=Session)
        
        # Mock user
        mock_user = Mock(spec=Account)
        mock_user.user_id = 1
        mock_user.github_user = "testuser"
        
        # Mock project
        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.project_name = "Test Project"
        mock_project.user_id = 1
        
        # Configure database queries
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        # Test the function
        with patch("project_deletion.find_project_by_name", return_value=mock_project):
            user, project = _get_project_and_user("Test Project", "testuser", mock_db)
        
        assert user == mock_user
        assert project == mock_project

    def test_get_project_and_user_user_not_found(self):
        """Test user not found scenario."""
        mock_db = Mock(spec=Session)
        mock_db.query.return_value.filter.return_value.first.return_value = None
        
        with pytest.raises(HTTPException) as exc_info:
            _get_project_and_user("Test Project", "nonexistent", mock_db)
        
        assert exc_info.value.status_code == 404
        assert "GitHub user not found" in str(exc_info.value.detail)

    def test_get_project_and_user_project_not_found(self):
        """Test project not found scenario."""
        mock_db = Mock(spec=Session)
        
        # Mock user exists but project doesn't
        mock_user = Mock(spec=Account)
        mock_user.user_id = 1
        
        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        with pytest.raises(HTTPException) as exc_info, \
                patch("project_deletion.find_project_by_name", return_value=None):
            _get_project_and_user("Nonexistent Project", "testuser", mock_db)
        
        assert exc_info.value.status_code == 404
        assert "Project not found or access denied" in str(exc_info.value.detail)

    def test_get_project_repositories(self):
        """Test repository retrieval for a project."""
        mock_db = Mock(spec=Session)
        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        
        # Mock project repo associations
        mock_project_repo = Mock(spec=ProjectRepo)
        mock_project_repo.repo_id = 1
        
        # Mock repository names query
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_project_repo]
        mock_db.query.return_value.filter.return_value.scalar.return_value = "test-repo"
        
        repos = _get_project_repositories(mock_project, mock_db)
        
        assert repos == ["test-repo"]

    def test_get_project_workflows(self):
        """Test workflow categorization."""
        mock_db = Mock(spec=Session)
        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        
        # Mock workflows
        mock_regular_workflow = Mock(spec=Workflow)
        mock_regular_workflow.workflow_name = "Regular Workflow"
        mock_regular_workflow.reusable_workflow = False
        mock_regular_workflow.created_at = None
        mock_regular_workflow.updated_at = None
        
        mock_reusable_workflow = Mock(spec=Workflow)
        mock_reusable_workflow.workflow_name = "Reusable Workflow"
        mock_reusable_workflow.reusable_workflow = True
        mock_reusable_workflow.created_at = None
        mock_reusable_workflow.updated_at = None
        
        mock_db.query.return_value.join.return_value.filter.return_value.all.return_value = [
            mock_regular_workflow, mock_reusable_workflow
        ]
        
        workflows, reusable_workflows = _get_project_workflows(mock_project, mock_db)
        
        assert len(workflows) == 1
        assert len(reusable_workflows) == 1
        assert workflows[0]["name"] == "Regular Workflow"
        assert workflows[0]["is_reusable"] is False
        assert reusable_workflows[0]["name"] == "Reusable Workflow"
        assert reusable_workflows[0]["is_reusable"] is True

    @patch('project_deletion.requests.get')
    def test_validate_repository_access_success(self, mock_get):
        """Test successful repository access validation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        headers = {"Authorization": "token test-token"}
        result = _validate_repository_access("test/repo", headers)
        
        assert result is True

    @patch('project_deletion.requests.get')
    def test_validate_repository_access_failure(self, mock_get):
        """Test repository access validation failure."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response
        
        headers = {"Authorization": "token test-token"}
        result = _validate_repository_access("test/repo", headers)
        
        assert result is False

    @patch('project_deletion.requests.get')
    def test_fetch_repository_secrets(self, mock_get):
        """Test repository secrets fetching."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "secrets": [
                {"name": "AM_TEST_SECRET1", "created_at": "2024-01-01", "updated_at": "2024-01-02"},
                {"name": "OTHER_SECRET", "created_at": "2024-01-01", "updated_at": "2024-01-02"}
            ]
        }
        mock_get.return_value = mock_response
        
        headers = {"Authorization": "token test-token"}
        secrets = _fetch_repository_secrets("test/repo", headers, "AM_TEST_")
        
        assert len(secrets) == 1
        assert secrets[0]["name"] == "AM_TEST_SECRET1"
        assert secrets[0]["repository"] == "test/repo"

    @patch('project_deletion.requests.get')
    def test_fetch_repository_variables(self, mock_get):
        """Test repository variables fetching."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "variables": [
                {"name": "AM_TEST_VAR1", "value": "value1", "created_at": "2024-01-01", "updated_at": "2024-01-02"},
                {"name": "OTHER_VAR", "value": "value2", "created_at": "2024-01-01", "updated_at": "2024-01-02"}
            ]
        }
        mock_get.return_value = mock_response
        
        headers = {"Authorization": "token test-token"}
        variables = _fetch_repository_variables("test/repo", headers, "AM_TEST_")
        
        assert len(variables) == 1
        assert variables[0]["name"] == "AM_TEST_VAR1"
        assert variables[0]["repository"] == "test/repo"
        assert variables[0]["environment"] == "repository"

    @patch('project_deletion.requests.get')
    def test_fetch_deployment_environments(self, mock_get):
        """Test deployment environments fetching."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "environments": [
                {"name": "production", "created_at": "2024-01-01", "updated_at": "2024-01-02"},
                {"name": "staging", "created_at": "2024-01-01", "updated_at": "2024-01-02"}
            ]
        }
        mock_get.return_value = mock_response
        
        headers = {"Authorization": "token test-token"}
        environments, _, _ = _fetch_deployment_environments("test/repo", headers)
        
        assert len(environments) == 2
        assert environments[0]["name"] == "production"
        assert environments[1]["name"] == "staging"

    def test_log_missing_repos_debug_runs_without_error(self):
        """Diagnostic helper queries project-repo associations without raising."""
        from project_deletion import _log_missing_repos_debug

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1

        mock_db = Mock(spec=Session)
        mock_pr = Mock()
        mock_pr.repo_id = 5
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_pr]
        mock_repo = Mock()
        mock_repo.repo_name = "owner/repo"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_repo

        _log_missing_repos_debug(mock_project, mock_db)

    @patch('project_deletion.user_tokens', {})
    def test_collect_github_resources_skips_without_token(self):
        """No user_tokens entry for github_user -> returns empty lists, no API calls."""
        from project_deletion import _collect_github_resources_for_deletion_summary

        mock_project = Mock(spec=Project)
        mock_db = Mock(spec=Session)

        secrets, env_vars, environments = _collect_github_resources_for_deletion_summary(
            mock_project, "testuser", ["owner/repo"], mock_db
        )

        assert secrets == []
        assert env_vars == []
        assert environments == []

    @patch('project_deletion.user_tokens', {})
    def test_collect_github_resources_skips_without_repos(self):
        """No repo_names -> returns empty lists, no API calls, regardless of token."""
        from project_deletion import _collect_github_resources_for_deletion_summary

        mock_project = Mock(spec=Project)
        mock_db = Mock(spec=Session)

        secrets, env_vars, environments = _collect_github_resources_for_deletion_summary(
            mock_project, "testuser", [], mock_db
        )

        assert secrets == []
        assert env_vars == []
        assert environments == []

    @patch('project_deletion.user_tokens', {"testuser": "test-token"})
    @patch('project_deletion._process_repository_resources')
    def test_collect_github_resources_aggregates_across_repos(self, mock_process):
        """Aggregates secrets/env vars/environments across every repo and tolerates per-repo failure."""
        from project_deletion import _collect_github_resources_for_deletion_summary

        mock_process.side_effect = [
            ([{"name": "SECRET1"}], [{"name": "VAR1"}], [{"name": "production"}]),
            Exception("repo unreachable"),
        ]

        mock_project = Mock(spec=Project)
        mock_project.project_code = "TEST"
        mock_project.use_prefix = True
        mock_db = Mock(spec=Session)

        secrets, env_vars, environments = _collect_github_resources_for_deletion_summary(
            mock_project, "testuser", ["owner/repo1", "owner/repo2"], mock_db
        )

        assert secrets == [{"name": "SECRET1"}]
        assert env_vars == [{"name": "VAR1"}]
        assert environments == [{"name": "production"}]
        assert mock_process.call_count == 2

    def test_build_deletion_summary(self):
        """Test deletion summary construction."""
        mock_project = Mock(spec=Project)
        mock_project.project_name = "Test Project"
        mock_project.project_code = "TEST"
        
        workflows = [{"name": "workflow1", "is_reusable": False}]
        reusable_workflows = [{"name": "reusable1", "is_reusable": True}]
        secrets = [{"name": "AM_TEST_SECRET", "repository": "test/repo"}]
        env_vars = [{"name": "AM_TEST_VAR", "repository": "test/repo"}]
        environments = [{"name": "production", "repository": "test/repo"}]
        
        targets = [{"repository": "test/repo", "branch": "main", "files": []}]

        summary = _build_deletion_summary(
            mock_project, workflows, reusable_workflows, secrets, env_vars, environments, {"removal_targets": targets}
        )
        
        assert summary.removal_targets == targets
        assert summary.removal_blocked_reason is None
        assert summary.project_name == "Test Project"
        assert summary.project_code == "TEST"
        assert len(summary.workflows) == 1
        assert len(summary.reusable_workflows) == 1
        assert len(summary.secrets) == 1
        assert len(summary.environment_variables) == 1
        assert len(summary.deployment_environments) == 1

    @patch('project_deletion._validate_repository_access')
    @patch('project_deletion._fetch_repository_secrets')
    @patch('project_deletion._fetch_repository_variables')
    @patch('project_deletion._fetch_deployment_environments')
    @patch('project_deletion._process_single_environment')
    def test_process_repository_resources(self, mock_process_env, mock_fetch_envs, 
                                         mock_fetch_vars, mock_fetch_secrets, mock_validate):
        """Test complete repository resource processing."""
        # Setup mocks
        mock_validate.return_value = True
        mock_fetch_secrets.return_value = [{"name": "AM_TEST_SECRET", "repository": "test/repo"}]
        mock_fetch_vars.return_value = [{"name": "AM_TEST_VAR", "repository": "test/repo"}]
        mock_fetch_envs.return_value = ([{"name": "production"}], [], [])
        mock_process_env.return_value = ([{"name": "AM_TEST_ENV_SECRET"}], [{"name": "AM_TEST_ENV_VAR"}])
        
        headers = {"Authorization": "token test-token"}
        secrets, variables, environments = _process_repository_resources("test/repo", headers, "AM_TEST_")
        
        assert len(secrets) == 2  # repo secret + env secret
        assert len(variables) == 2  # repo variable + env variable
        assert len(environments) == 1

    @patch('project_deletion._validate_repository_access')
    def test_process_repository_resources_access_denied(self, mock_validate):
        """Test repository resource processing when access is denied."""
        mock_validate.return_value = False
        
        headers = {"Authorization": "token test-token"}
        secrets, variables, environments = _process_repository_resources("test/repo", headers, "AM_TEST_")
        
        assert secrets == []
        assert variables == []
        assert environments == []


# --------------------------------------------------------------------------- #
# Deleting a project with its files: a last PR Campaign, one pull request per #
# (repo, branch) the delivery record holds any of them on.                    #
# --------------------------------------------------------------------------- #

DOOMED = "doomed"
APP, WEB = "doomed-org/app", "doomed-org/web"
RELEASE = "release/1"
CI_PATH = ".github/workflows/AM_DMD_ci.yml"
LIB_PATH = ".github/workflows/AM_DMD_lib.yml"
SCRIPT, MOVED_SCRIPT = ".github/scripts/build.sh", ".github/scripts/ci/build.sh"
OWNERS_PATH = ".github/CODEOWNERS"
REMOVE_FILES = {"remove_files": True}
CI, LIB, OPEN = "ci", "lib", "open"
SHARED, CALLER = "doomed-org/shared", "caller"

_Session = _make_empty_middleware_factory()
_engine = _Session.kw["bind"]


@pytest.fixture
def db():
    Base.metadata.create_all(bind=_engine)
    session = _Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=_engine)


@pytest.fixture
def client(db, authenticate_client):
    from fastapi.testclient import TestClient
    from database import get_db
    from main import app

    def override_get_db():
        session = _Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    original_factory = app.state.middleware_db_factory
    app.state.middleware_db_factory = _Session
    try:
        with patch("mode_validation.validate_startup_configuration"), TestClient(app) as test_client:
            authenticate_client(test_client, HARNESS_USER, _Session)
            yield test_client
    finally:
        app.state.middleware_db_factory = original_factory
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def fake():
    return FakeGitHub()


def _add_project(db, name=DOOMED, code="DMD", project_type="standard", repos=(APP, WEB)):
    owner = db.query(Account).filter_by(github_user=HARNESS_USER).first()
    if owner is None:
        owner = Account(github_user=HARNESS_USER, github_email="doomed@example.com", account_type="free")
        db.add(owner)
        db.flush()
    project = Project(project_name=name, project_code=code, user_id=owner.user_id, project_type=project_type,
                      use_prefix=True, branch_option="default", pr_state="draft")
    db.add(project)
    db.flush()
    for repo_name in repos:
        repo = db.query(Repo).filter_by(repo_name=repo_name).first() or Repo(repo_name=repo_name)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.commit()
    return project


def _add_workflow(db, project, name, reusable=False):
    workflow = Workflow(workflow_name=name, reusable_workflow=reusable, workflow_status="new",
                        workflow_yaml="on: workflow_call\n" if reusable else "on: push\n")
    db.add(workflow)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    return workflow


def _deliver(db, fake, project, repos, **selection):
    """Deliver the selection to *repos* by a campaign whose pull requests all merged; then a clean slate of calls."""
    for pr in run_campaign(db, fake, project, repos, **selection):
        fake.merge(db, pr)
    fake.calls.clear()
    fake.deletes.clear()


def _delivered_ci(db, fake):
    """DOOMED with ``ci`` delivered to main of APP and WEB, and to APP's release branch the project no longer targets."""
    project = _add_project(db)
    _add_workflow(db, project, CI)
    _deliver(db, fake, project, [APP, WEB])
    fake.branch_config = [RELEASE]
    _deliver(db, fake, project, [APP])
    fake.branch_config = [HARNESS_BRANCH]
    return project


def _delivered_every_kind(db, fake):
    """DOOMED with a workflow, a reusable workflow, a custom file and CODEOWNERS delivered to main of APP and WEB.

    The custom file has moved since, locally only.
    """
    project = _add_project(db)
    _add_workflow(db, project, CI)
    _add_workflow(db, project, LIB, reusable=True)
    script = CustomFile(project_id=project.project_id, file_path=SCRIPT, file_content="echo build\n", file_status="new")
    db.add_all([script, Codeowners(project_id=project.project_id, content="* @doomed-org/team\n", file_path=OWNERS_PATH)])
    db.commit()
    _deliver(db, fake, project, [APP, WEB], selected_workflows=[CI], selected_reusable_workflows=[LIB],
             selected_custom_file_ids=[script.id], include_codeowners=True)
    script.file_path, script.file_status = MOVED_SCRIPT, "committed_locally"
    db.commit()
    return project


def _detail(response):
    return response.json()["detail"]


def _reason(refused):
    """What a refusal says, for the user."""
    return _detail(refused)["message"]


def _deleted(response):
    return response.json()["project_deleted"]


def _file(kind, name, path):
    """One file of the removal plan, as the summary lists it."""
    return {"kind": kind, "name": name, "path": path}


def _doomed(db):
    db.expire_all()
    return db.query(Project).filter_by(project_name=DOOMED).first()


def _targets(prs):
    return sorted((pr.repo_name, pr.target_branch) for pr in prs)


def _on_target_branches(fake):
    return sorted(key for key in fake.files if not key[1].startswith("actions-manager/"))


def test_remove_files_opens_one_pr_per_recorded_repo_and_branch(db, client, fake):
    _delivered_ci(db, fake)

    response, prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert response.status_code == 200, response.text
    body = response.json()
    assert _deleted(response) is False
    assert body["pr_state"] == PROJECT_DELETING
    assert body["prs_created"] == 3
    assert _targets(prs) == [(APP, HARNESS_BRANCH), (APP, RELEASE), (WEB, HARNESS_BRANCH)], \
        "every (repo, branch) the record holds, the release branch the project no longer targets included"
    assert {pr.campaign_id for pr in prs} == {body["campaign_id"]}
    campaign = db.query(ProjectPRCampaign).filter_by(campaign_id=body["campaign_id"]).one()
    assert campaign.campaign_name == f"Delete project {DOOMED}"
    assert _doomed(db).pr_state == PROJECT_DELETING


def test_each_pr_deletes_every_kind_at_its_delivered_path(db, client, fake):
    _delivered_every_kind(db, fake)

    response, prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert response.status_code == 200, response.text
    assert _targets(prs) == [(APP, HARNESS_BRANCH), (WEB, HARNESS_BRANCH)]
    every_kind = sorted([CI_PATH, LIB_PATH, SCRIPT, OWNERS_PATH])
    for pr in prs:
        removed = sorted(path for repo, branch, path in fake.deletes if (repo, branch) == (pr.repo_name, pr.branch_name))
        assert removed == every_kind, "the custom file is removed where it was delivered, not where it is now"
        assert fake.paths(pr.repo_name, pr.branch_name) == set()
        assert OWNERS_PATH in pr.file_names


def test_no_direct_commit_is_made_for_files(db, client, fake):
    _delivered_every_kind(db, fake)
    before = _on_target_branches(fake)

    response, _prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert response.status_code == 200, response.text
    assert _on_target_branches(fake) == before, "nothing leaves a target branch until its pull request merges"
    assert {branch.split("/", 1)[0] for _verb, _repo, branch, _path in fake.calls} == {"actions-manager"}
    assert {verb for verb, *_ in fake.calls} == {"DELETE"}, "the record's blob SHAs make each removal one call"


def test_open_prs_block_remove_files(db, client, fake):
    project = _add_project(db)
    _add_workflow(db, project, CI)
    [open_pr] = run_campaign(db, fake, project, [APP])
    fake.calls.clear()

    refused, opened = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)
    summary = client.get(f"/api/projects/{DOOMED}/deletion-summary")

    assert refused.status_code == 409, refused.text
    detail = _detail(refused)
    assert detail["open_prs"] == [{"repo_name": APP, "pr_number": open_pr.pr_number, "pr_url": open_pr.pr_url,
                                   "target_branch": HARNESS_BRANCH}]
    assert f"{APP}#{open_pr.pr_number}" in _reason(refused)
    assert summary.json()["removal_blocked_reason"] == _reason(refused)
    assert opened == []
    assert fake.calls == []
    assert _doomed(db).pr_state == OPEN


def test_another_projects_open_pr_carrying_a_file_blocks_remove_files(db, client, fake):
    project = _add_project(db)
    workflow = _add_workflow(db, project, CI)
    _deliver(db, fake, project, [APP])
    other = _add_project(db, name="other", code="OTH", repos=(WEB,))
    carrier = ProjectPullRequest(project_id=other.project_id, repo_name=APP, pr_number=41, pr_state=OPEN,
                                 pr_url=f"https://github.com/{APP}/pull/41", branch_name="actions-manager/oth-main",
                                 target_branch=HARNESS_BRANCH)
    db.add(carrier)
    db.commit()
    record_pr_carries(db, carrier, workflow)

    refused, _prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert refused.status_code == 409, refused.text
    assert [pr["pr_number"] for pr in _detail(refused)["open_prs"]] == [41]


def test_rwx_with_linked_callers_blocks_remove_files(db, client, fake):
    shared = _add_project(db, name=DOOMED, code="DMD", project_type="rwx", repos=(SHARED,))
    reusable = _add_workflow(db, shared, "build", reusable=True)
    _deliver(db, fake, shared, [SHARED], selected_reusable_workflows=["build"])
    caller = _add_project(db, name=CALLER, code="CLR", repos=(APP,))
    db.add(LinkedReusableWorkflow(standard_project_id=caller.project_id, rwx_project_id=shared.project_id,
                                  workflow_id=reusable.workflow_id))
    db.commit()

    refused, opened = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)
    kept, _ = delete_project(client, fake, db, DOOMED, remove_files=False)

    assert refused.status_code == 409, refused.text
    assert _detail(refused)["linked_projects"] == [CALLER]
    assert CALLER in _reason(refused)
    assert opened == []
    assert kept.status_code == 200, kept.text
    assert _deleted(kept) is True, "deleting it and keeping its files stays allowed"
    still_there = (SHARED, HARNESS_BRANCH, ".github/workflows/AM_DMD_build.yml")
    assert still_there in fake.files


def test_files_in_a_repository_no_longer_in_the_project_block_remove_files(db, client, fake):
    project = _add_project(db)
    _add_workflow(db, project, CI)
    _deliver(db, fake, project, [APP, WEB])
    web = db.query(Repo).filter_by(repo_name=WEB).one()
    db.query(ProjectRepo).filter_by(project_id=project.project_id, repo_id=web.repo_id).delete()
    db.commit()

    refused, opened = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert refused.status_code == 409, refused.text
    assert _detail(refused)["repositories"] == [WEB]
    assert opened == []


def test_nothing_delivered_deletes_immediately(db, client, fake):
    project = _add_project(db)
    _add_workflow(db, project, CI)

    response, opened = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert response.status_code == 200, response.text
    assert _deleted(response) is True
    assert response.json()["pr_state"] is None
    assert opened == []
    assert fake.calls == []
    assert _doomed(db) is None


def test_files_already_gone_from_github_delete_immediately(db, client, fake):
    _delivered_ci(db, fake)
    for key in [key for key in fake.files if key[2] == CI_PATH]:
        del fake.files[key]

    response, opened = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert response.status_code == 200, response.text
    assert _deleted(response) is True
    assert opened == []
    assert _doomed(db) is None


def test_keep_files_touches_no_file(db, client, fake):
    _delivered_every_kind(db, fake)
    before = dict(fake.files)

    response, opened = delete_project(client, fake, db, DOOMED, remove_files=False)

    assert response.status_code == 200, response.text
    assert _deleted(response) is True
    assert response.json()["details"]["project_deleted"] is True
    assert opened == []
    assert fake.files == before
    assert fake.calls == []
    assert _doomed(db) is None


SECRET = "AM_DMD_TOKEN"


def _listing_secrets(fake):
    fake.secrets = {APP: [SECRET], WEB: [SECRET]}
    return patch("project_deletion.requests.get", side_effect=fake.actions_settings)


@pytest.mark.parametrize("remove_files", [True, False])
def test_secrets_option_is_direct_and_independent(db, client, fake, remove_files):
    _delivered_ci(db, fake)

    with _listing_secrets(fake):
        response, opened = delete_project(client, fake, db, DOOMED, remove_files=remove_files,
                                          delete_github_resources=True, delete_deployment_environments=False)

    assert response.status_code == 200, response.text
    assert sorted(fake.secrets_deleted) == [(APP, SECRET), (WEB, SECRET)]
    assert response.json()["details"]["github_resources_deleted"] == [
        f"Repository Secret: {SECRET} from {repo}" for repo in (APP, WEB)]
    assert len(opened) == (3 if remove_files else 0), "files only ever go by pull request"


def test_without_the_secrets_option_no_secret_is_touched(db, client, fake):
    _delivered_ci(db, fake)

    with _listing_secrets(fake) as secrets_read:
        response, _prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert response.status_code == 200, response.text
    assert fake.secrets_deleted == []
    secrets_read.assert_not_called()


def test_a_deleting_project_cannot_be_deleted_again(db, client, fake):
    _delivered_ci(db, fake)
    started, _prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)
    fake.calls.clear()

    again = [delete_project(client, fake, db, DOOMED, remove_files=choice)[0] for choice in (True, False)]

    assert started.status_code == 200, started.text
    assert [response.status_code for response in again] == [409, 409]
    assert _detail(again[0]) == ALREADY_DELETING
    assert fake.calls == []
    assert _doomed(db).pr_state == PROJECT_DELETING


def test_summary_lists_the_removal_plan_without_github_calls(db, client, fake):
    project = _delivered_every_kind(db, fake)
    never_recorded = _add_workflow(db, project, "legacy")
    never_recorded.workflow_status = "synced_with_github"
    db.commit()

    with github(fake), patch("project_deletion.requests.get", return_value=Mock(status_code=404)) as github_read:
        response = client.get(f"/api/projects/{DOOMED}/deletion-summary")

    assert response.status_code == 200, response.text
    body = response.json()
    every_kind = [
        _file(deliveries.WORKFLOW, CI, CI_PATH),
        _file(deliveries.WORKFLOW, LIB, LIB_PATH),
        _file(deliveries.CUSTOM_FILE, MOVED_SCRIPT, SCRIPT),
        _file(deliveries.CODEOWNERS, OWNERS_PATH, OWNERS_PATH),
    ]
    assert body["removal_targets"] == [
        {"repository": repo, "branch": HARNESS_BRANCH, "files": every_kind} for repo in (APP, WEB)]
    assert body["unrecorded_files"] == [
        _file(deliveries.WORKFLOW, "legacy", ".github/workflows/AM_DMD_legacy.yml")]
    assert body["removal_blocked_reason"] is None
    assert fake.calls == [], "the plan is the record's alone"
    assert [read.args[0] for read in github_read.call_args_list] == [
        f"https://api.github.com/repos/{repo}" for repo in (APP, WEB)], "only the secrets listing reads GitHub"


def test_errors_opening_every_pr_leave_the_project_untouched(db, client, fake):
    project = _delivered_ci(db, fake)
    state = project.pr_state
    held = deliveries.project_delivered_repos(db, project.project_id)

    fake.pr_refusal = "Resource not accessible by integration"

    response, opened = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    assert response.status_code == 502, response.text
    assert "was not deleted" in _reason(response)
    assert opened == []
    doomed = _doomed(db)
    assert doomed.pr_state == state
    assert db.query(ProjectPRCampaign).filter_by(campaign_name=f"Delete project {DOOMED}").count() == 0
    assert deliveries.project_delivered_repos(db, doomed.project_id) == held


def test_merging_every_removal_pr_deletes_the_project(db, client, fake):
    _delivered_every_kind(db, fake)
    _started, prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)
    first, last = prs

    merged = [press_merge(client, fake, DOOMED, first), press_merge(client, fake, DOOMED, last)]

    assert [response.status_code for response in merged] == [200, 200], merged[-1].text
    assert [_deleted(response) for response in merged] == [False, True]
    assert _doomed(db) is None
    assert db.query(Workflow).count() == 0
    assert db.query(FileDelivery).count() == 0
    assert _on_target_branches(fake) == [], "every file the project delivered is gone from GitHub"


def test_closing_a_removal_pr_makes_the_project_editable_again(db, client, fake):
    _delivered_ci(db, fake)
    _started, prs = delete_project(client, fake, db, DOOMED, **REMOVE_FILES)

    closed = press_close(client, fake, DOOMED, prs[0])

    assert closed.status_code == 200, closed.text
    project = _doomed(db)
    owner = db.query(Account).filter_by(github_user=HARNESS_USER).one()
    assert project.pr_state == OPEN, "called off, its other removal PRs now ordinary open PRs"
    assert has_project_role(db, owner, project, "project_editor")
    assert _on_target_branches(fake) == sorted(
        [(APP, HARNESS_BRANCH, CI_PATH), (APP, RELEASE, CI_PATH), (WEB, HARNESS_BRANCH, CI_PATH)])


class TestDeleteDeploymentEnvironmentsOptOut:
    """Tests for the delete_deployment_environments opt-out (issue #487)."""

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_default_true_deletes_environments(self, mock_delete, mock_get):
        """Flag omitted/True: default behavior is unchanged, environments are still deleted."""
        def get_side_effect(url, headers=None, **kwargs):
            resp = Mock()
            if url.endswith("/environments"):
                resp.status_code = 200
                resp.json.return_value = {"environments": [{"name": "production"}, {"name": "staging"}]}
            else:
                resp.status_code = 404
            return resp
        mock_get.side_effect = get_side_effect
        mock_delete.return_value = Mock(status_code=204)

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_github_resources_for_repository("owner/repo", headers, "", deletion_results, None, None, True)

        deleted_envs = [d for d in deletion_results["github_resources_deleted"] if d.startswith("Deployment Environment:")]
        assert len(deleted_envs) == 2

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_flag_false_preserves_environments(self, mock_delete, mock_get):
        """delete_deployment_environments=False: environments must be preserved, not deleted."""
        def get_side_effect(url, headers=None, **kwargs):
            resp = Mock()
            if url.endswith("/environments"):
                resp.status_code = 200
                resp.json.return_value = {"environments": [{"name": "production"}, {"name": "staging"}]}
            else:
                resp.status_code = 404
            return resp
        mock_get.side_effect = get_side_effect

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_github_resources_for_repository("owner/repo", headers, "", deletion_results, None, None, False)

        assert not any(d.startswith("Deployment Environment:") for d in deletion_results["github_resources_deleted"])
        mock_delete.assert_not_called()


class TestCascadeDeletion:
    """Tests for cascade deletion behavior during project deletion."""

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_cascade_deletes_all_resource_types(self, mock_delete, mock_get):
        """Deleting a project's GitHub resources cascades to secrets, variables and environments, never its files.

        Files are removed only by pull request (workflows.start_project_removal),
        so a workflow GitHub lists is left alone even where its name matches.
        """
        def get_side_effect(url, headers=None, **kwargs):
            resp = Mock()
            if "/actions/workflows" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "workflows": [
                        {"name": "CI", "path": ".github/workflows/AM_TEST_ci.yml"}
                    ]
                }
            elif "/contents/" in url:
                resp.status_code = 200
                resp.json.return_value = {"sha": "abc123"}
            elif "/actions/secrets" in url and "/environments/" not in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "secrets": [{"name": "AM_TEST_SECRET", "created_at": "2024-01-01", "updated_at": "2024-01-01"}]
                }
            elif "/actions/variables" in url and "/environments/" not in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "variables": [{"name": "AM_TEST_VAR", "value": "v1", "created_at": "2024-01-01", "updated_at": "2024-01-01"}]
                }
            elif "/environments" in url and "/secrets" not in url and "/variables" not in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "environments": [{"name": "production"}]
                }
            elif "/environments/" in url and "/secrets" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "secrets": [{"name": "AM_TEST_ENV_SECRET", "created_at": "2024-01-01", "updated_at": "2024-01-01"}]
                }
            else:
                resp.status_code = 404
            return resp

        mock_get.side_effect = get_side_effect
        mock_delete.return_value = Mock(status_code=204)
        # Workflow file deletion returns 200
        mock_delete.side_effect = lambda url, headers=None, json=None, **kwargs: Mock(
            status_code=200 if "/contents/" in url else 204
        )

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.use_prefix = True

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_github_resources_for_repository(
            "owner/repo", headers, "AM_TEST_", deletion_results, mock_project, None, True
        )

        # Verify multiple resource types were deleted
        deleted = deletion_results["github_resources_deleted"]
        assert not any("Workflow" in d for d in deleted)
        assert mock_delete.call_count == len(deleted), "every DELETE was a secret, variable or environment"
        assert any("Repository Secret" in d for d in deleted)
        assert any("Repository Variable" in d for d in deleted)
        assert any("Deployment Environment" in d for d in deleted)
        assert len(deletion_results["errors"]) == 0

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_cascade_across_multiple_repositories(self, mock_delete, mock_get):
        """_delete_all_github_resources processes all repositories in the list."""
        call_log = []

        def get_side_effect(url, headers=None, **kwargs):
            resp = Mock()
            if "/actions/workflows" in url:
                resp.status_code = 200
                resp.json.return_value = {"workflows": []}
            elif "/actions/secrets" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "secrets": [{"name": "AM_CODE_SECRET", "created_at": "2024-01-01", "updated_at": "2024-01-01"}]
                }
            elif "/actions/variables" in url:
                resp.status_code = 200
                resp.json.return_value = {"variables": []}
            elif "/environments" in url:
                resp.status_code = 200
                resp.json.return_value = {"environments": []}
            else:
                resp.status_code = 404
            return resp

        def delete_side_effect(url, headers=None, json=None, **kwargs):
            call_log.append(url)
            return Mock(status_code=204)

        mock_get.side_effect = get_side_effect
        mock_delete.side_effect = delete_side_effect

        mock_project = Mock(spec=Project)
        mock_project.use_prefix = True

        from project_deletion import _delete_all_github_resources

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_all_github_resources(
            ["owner/repo1", "owner/repo2", "owner/repo3"],
            headers, "CODE", deletion_results, mock_project, None, True
        )

        # Secrets from each repo should be deleted
        assert len(deletion_results["github_resources_deleted"]) == 3
        assert any("repo1" in d for d in deletion_results["github_resources_deleted"])
        assert any("repo2" in d for d in deletion_results["github_resources_deleted"])
        assert any("repo3" in d for d in deletion_results["github_resources_deleted"])

    def test_cascade_prefix_derived_from_project_code(self):
        """_delete_all_github_resources builds AM_{code}_ prefix when use_prefix=True."""
        from project_deletion import _delete_all_github_resources

        mock_project = Mock(spec=Project)
        mock_project.use_prefix = True

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        with patch('project_deletion._delete_github_resources_for_repository') as mock_del:
            _delete_all_github_resources(
                ["owner/repo"], headers, "MYCODE", deletion_results, mock_project, None, True
            )
            mock_del.assert_called_once_with(
                "owner/repo", headers, "AM_MYCODE_", deletion_results, mock_project, None, True
            )

    def test_cascade_no_prefix_when_use_prefix_false(self):
        """_delete_all_github_resources passes empty prefix when use_prefix=False."""
        from project_deletion import _delete_all_github_resources

        mock_project = Mock(spec=Project)
        mock_project.use_prefix = False

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        with patch('project_deletion._delete_github_resources_for_repository') as mock_del:
            _delete_all_github_resources(
                ["owner/repo"], headers, "MYCODE", deletion_results, mock_project, None, True
            )
            mock_del.assert_called_once_with(
                "owner/repo", headers, "", deletion_results, mock_project, None, True
            )


class TestOrphanedResourceCleanup:
    """Tests for orphaned resource cleanup after project deletion."""

    @patch('project_deletion.requests.get')
    def test_fetch_secrets_no_prefix_uses_database_tracking(self, mock_get):
        """No-prefix projects use DB-tracked secret names instead of prefix matching."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "secrets": [
                {"name": "MY_SECRET", "created_at": "2024-01-01", "updated_at": "2024-01-01"},
                {"name": "OTHER_SECRET", "created_at": "2024-01-01", "updated_at": "2024-01-01"}
            ]
        }
        mock_get.return_value = mock_response

        mock_project = Mock(spec=Project)
        mock_project.project_id = 42
        mock_project.use_prefix = False

        mock_db = Mock(spec=Session)
        mock_secret = Mock()
        mock_secret.secret_name = "MY_SECRET"
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_secret]

        headers = {"Authorization": "token test-token"}
        secrets = _fetch_repository_secrets("test/repo", headers, "", mock_project, mock_db)

        assert len(secrets) == 1
        assert secrets[0]["name"] == "MY_SECRET"

    @patch('project_deletion.requests.get')
    def test_fetch_variables_no_prefix_uses_database_tracking(self, mock_get):
        """No-prefix projects use DB-tracked variable names instead of prefix matching."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "variables": [
                {"name": "MY_VAR", "value": "v1", "created_at": "2024-01-01", "updated_at": "2024-01-01"},
                {"name": "UNTRACKED_VAR", "value": "v2", "created_at": "2024-01-01", "updated_at": "2024-01-01"}
            ]
        }
        mock_get.return_value = mock_response

        mock_project = Mock(spec=Project)
        mock_project.project_id = 42
        mock_project.use_prefix = False

        mock_db = Mock(spec=Session)
        mock_var = Mock()
        mock_var.env_var_name = "MY_VAR"
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_var]

        headers = {"Authorization": "token test-token"}
        variables = _fetch_repository_variables("test/repo", headers, "", mock_project, mock_db)

        assert len(variables) == 1
        assert variables[0]["name"] == "MY_VAR"

    @patch('project_deletion.requests.get')
    def test_empty_prefix_does_not_match_all_secrets(self, mock_get):
        """Empty prefix with use_prefix=True must not match all secrets."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "secrets": [
                {"name": "SECRET1", "created_at": "2024-01-01", "updated_at": "2024-01-01"},
                {"name": "SECRET2", "created_at": "2024-01-01", "updated_at": "2024-01-01"}
            ]
        }
        mock_get.return_value = mock_response

        # project with use_prefix=True but we pass empty prefix
        mock_project = Mock(spec=Project)
        mock_project.use_prefix = True

        headers = {"Authorization": "token test-token"}
        secrets = _fetch_repository_secrets("test/repo", headers, "", mock_project, None)

        # Should return nothing since empty prefix must not match everything
        assert len(secrets) == 0

    @patch('project_deletion.requests.get')
    def test_empty_prefix_does_not_match_all_variables(self, mock_get):
        """Empty prefix with use_prefix=True must not match all variables."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "variables": [
                {"name": "VAR1", "value": "v1", "created_at": "2024-01-01", "updated_at": "2024-01-01"},
                {"name": "VAR2", "value": "v2", "created_at": "2024-01-01", "updated_at": "2024-01-01"}
            ]
        }
        mock_get.return_value = mock_response

        mock_project = Mock(spec=Project)
        mock_project.use_prefix = True

        headers = {"Authorization": "token test-token"}
        variables = _fetch_repository_variables("test/repo", headers, "", mock_project, None)

        assert len(variables) == 0

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_delete_secrets_no_prefix_only_tracked(self, mock_delete, mock_get):
        """Only secrets tracked in the database should be deleted for no-prefix projects."""
        from project_deletion import _delete_repository_secrets

        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "secrets": [
                {"name": "TRACKED_SECRET"},
                {"name": "UNTRACKED_SECRET"}
            ]
        }
        mock_get.return_value = mock_get_response
        mock_delete.return_value = Mock(status_code=204)

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.use_prefix = False

        mock_db = Mock(spec=Session)
        mock_secret = Mock()
        mock_secret.secret_name = "TRACKED_SECRET"
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_secret]

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_repository_secrets("owner/repo", headers, "", deletion_results, mock_project, mock_db)

        assert len(deletion_results["github_resources_deleted"]) == 1
        assert "TRACKED_SECRET" in deletion_results["github_resources_deleted"][0]


class TestDeleteEnvironmentSecrets:
    """Tests for _delete_environment_secrets (no prior direct coverage)."""

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_delete_environment_secrets_no_prefix_only_tracked(self, mock_delete, mock_get):
        """Only tracked secrets are deleted from each environment for no-prefix projects."""
        from project_deletion import _delete_environment_secrets

        environments_response = Mock(status_code=200)
        environments_response.json.return_value = {"environments": [{"name": "production"}]}
        env_secrets_response = Mock(status_code=200)
        env_secrets_response.json.return_value = {
            "secrets": [{"name": "TRACKED_SECRET"}, {"name": "UNTRACKED_SECRET"}]
        }
        mock_get.side_effect = [environments_response, env_secrets_response]
        mock_delete.return_value = Mock(status_code=204)

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.use_prefix = False

        mock_db = Mock(spec=Session)
        mock_secret = Mock()
        mock_secret.secret_name = "TRACKED_SECRET"
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_secret]

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_environment_secrets("owner/repo", headers, "", deletion_results, mock_project, mock_db)

        assert mock_delete.call_count == 1
        assert len(deletion_results["github_resources_deleted"]) == 1
        assert "TRACKED_SECRET" in deletion_results["github_resources_deleted"][0]
        assert "production" in deletion_results["github_resources_deleted"][0]

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_delete_environment_secrets_prefix_match_across_environments(self, mock_delete, mock_get):
        """Prefix-matching secrets are deleted across multiple environments."""
        from project_deletion import _delete_environment_secrets

        environments_response = Mock(status_code=200)
        environments_response.json.return_value = {
            "environments": [{"name": "staging"}, {"name": "production"}]
        }
        staging_secrets = Mock(status_code=200)
        staging_secrets.json.return_value = {"secrets": [{"name": "AM_TEST_SECRET"}, {"name": "OTHER"}]}
        production_secrets = Mock(status_code=200)
        production_secrets.json.return_value = {"secrets": [{"name": "AM_TEST_SECRET"}]}
        mock_get.side_effect = [environments_response, staging_secrets, production_secrets]
        mock_delete.return_value = Mock(status_code=204)

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_environment_secrets("owner/repo", headers, "AM_TEST_", deletion_results)

        assert mock_delete.call_count == 2
        assert len(deletion_results["github_resources_deleted"]) == 2

    @patch('project_deletion.requests.get')
    def test_delete_environment_secrets_empty_prefix_matches_nothing(self, mock_get):
        """Empty prefix with use_prefix=True must not match/delete anything."""
        from project_deletion import _delete_environment_secrets

        environments_response = Mock(status_code=200)
        environments_response.json.return_value = {"environments": [{"name": "production"}]}
        env_secrets_response = Mock(status_code=200)
        env_secrets_response.json.return_value = {"secrets": [{"name": "SOME_SECRET"}]}
        mock_get.side_effect = [environments_response, env_secrets_response]

        mock_project = Mock(spec=Project)
        mock_project.use_prefix = True

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_environment_secrets("owner/repo", headers, "", deletion_results, mock_project, None)

        assert len(deletion_results["github_resources_deleted"]) == 0


class TestPartialDeletionHandling:
    """Tests for partial deletion when some resources fail to delete."""

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_partial_secret_deletion_failure(self, mock_delete, mock_get):
        """Some secrets fail to delete; errors are recorded but deletion continues."""
        from project_deletion import _delete_repository_secrets

        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "secrets": [
                {"name": "AM_TEST_SECRET1"},
                {"name": "AM_TEST_SECRET2"},
                {"name": "AM_TEST_SECRET3"}
            ]
        }
        mock_get.return_value = mock_get_response

        # First delete succeeds, second fails, third succeeds
        mock_delete.side_effect = [
            Mock(status_code=204),
            Mock(status_code=403),
            Mock(status_code=204),
        ]

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_repository_secrets("owner/repo", headers, "AM_TEST_", deletion_results)

        assert len(deletion_results["github_resources_deleted"]) == 2
        assert len(deletion_results["errors"]) == 1
        assert "AM_TEST_SECRET2" in deletion_results["errors"][0]

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_partial_variable_deletion_failure(self, mock_delete, mock_get):
        """Some variables fail to delete; errors are recorded but deletion continues."""
        from project_deletion import _delete_repository_variables

        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "variables": [
                {"name": "AM_TEST_VAR1", "value": "v1"},
                {"name": "AM_TEST_VAR2", "value": "v2"}
            ]
        }
        mock_get.return_value = mock_get_response

        mock_delete.side_effect = [
            Mock(status_code=204),
            Mock(status_code=500),
        ]

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_repository_variables("owner/repo", headers, "AM_TEST_", deletion_results)

        assert len(deletion_results["github_resources_deleted"]) == 1
        assert len(deletion_results["errors"]) == 1
        assert "AM_TEST_VAR2" in deletion_results["errors"][0]

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_partial_environment_deletion_failure(self, mock_delete, mock_get):
        """Some environments fail to delete; errors are recorded but deletion continues."""
        from project_deletion import _delete_deployment_environments

        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "environments": [
                {"name": "production"},
                {"name": "staging"},
                {"name": "dev"}
            ]
        }
        mock_get.return_value = mock_get_response

        mock_delete.side_effect = [
            Mock(status_code=204),
            Mock(status_code=422),
            Mock(status_code=204),
        ]

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_deployment_environments("owner/repo", headers, deletion_results)

        assert len(deletion_results["github_resources_deleted"]) == 2
        assert len(deletion_results["errors"]) == 1
        assert "staging" in deletion_results["errors"][0]


class TestTransactionRollback:
    """Tests for transaction rollback behavior on errors."""

    @patch(_ORPHAN_CLEANUP)
    @patch('project_deletion.user_tokens', {"testuser": "test-token"})
    def test_enhanced_delete_rolls_back_on_db_commit_failure(self, mock_cleanup):
        """If db.commit() fails, transaction is rolled back and 500 is raised."""
        from fastapi.testclient import TestClient
        from main import app

        mock_db = Mock(spec=Session)
        mock_user = Mock(spec=Account)
        mock_user.user_id = 1
        mock_user.github_user = "testuser"

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.project_name = "Test Project"
        mock_project.project_code = "TST"
        mock_project.user_id = 1
        mock_project.use_prefix = True

        mock_db.query.return_value.filter.return_value.first.side_effect = [mock_user, mock_project]
        mock_db.delete.return_value = None
        mock_db.commit.side_effect = Exception("Database write failure")
        mock_db.rollback.return_value = None
        mock_db.close.return_value = None

        from auth import current_account
        from database import get_db

        def override_get_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[current_account] = lambda: mock_user
        original_middleware_factory = app.state.middleware_db_factory
        app.state.middleware_db_factory = _make_empty_middleware_factory()
        try:
            client = TestClient(app)
            response = client.request(
                "DELETE",
                "/api/projects/Test%20Project/enhanced",
                json={
                    "project_name": "Test Project",
                    "delete_github_resources": False,
                    "delete_deployment_environments": True
                }
            )

            assert response.status_code == 500
            assert "Error deleting project" in _detail(response)
            mock_db.rollback.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(current_account, None)
            app.state.middleware_db_factory = original_middleware_factory

    @patch(_ORPHAN_CLEANUP)
    @patch('project_deletion.user_tokens', {"testuser": "test-token"})
    def test_enhanced_delete_closes_db_on_success(self, mock_cleanup):
        """Database session is closed even on successful deletion."""
        from fastapi.testclient import TestClient
        from main import app

        mock_db = Mock(spec=Session)
        mock_user = Mock(spec=Account)
        mock_user.user_id = 1
        mock_user.github_user = "testuser"

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.project_name = "Test Project"
        mock_project.project_code = "TST"
        mock_project.user_id = 1
        mock_project.use_prefix = True

        # The caller's account, then their workspace row: the owner edits their own project.
        mock_member = Mock(spec=WorkspaceMember)
        mock_member.workspace_role = "member"
        mock_db.query.return_value.filter.return_value.first.side_effect = [mock_user, mock_member]
        mock_db.delete.return_value = None
        mock_db.commit.return_value = None
        mock_db.close.return_value = None

        from auth import current_account
        from database import get_db

        def override_get_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[current_account] = lambda: mock_user
        original_middleware_factory = app.state.middleware_db_factory
        app.state.middleware_db_factory = _make_empty_middleware_factory()
        try:
            client = TestClient(app)
            with patch("project_deletion.find_project_by_name", return_value=mock_project):
                response = client.request(
                    "DELETE",
                    "/api/projects/Test%20Project/enhanced",
                    json={
                        "project_name": "Test Project",
                        "delete_github_resources": False,
                        "delete_deployment_environments": True
                    }
                )

            assert response.status_code == 200
            mock_db.close.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(current_account, None)
            app.state.middleware_db_factory = original_middleware_factory

    @patch(_ORPHAN_CLEANUP)
    @patch('project_deletion.user_tokens', {"testuser": "test-token"})
    def test_enhanced_delete_closes_db_on_failure(self, mock_cleanup):
        """Database session is closed even when deletion fails."""
        from fastapi.testclient import TestClient
        from main import app

        mock_db = Mock(spec=Session)
        mock_user = Mock(spec=Account)
        mock_user.user_id = 1
        mock_user.github_user = "testuser"

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.project_name = "Test Project"
        mock_project.project_code = "TST"
        mock_project.user_id = 1
        mock_project.use_prefix = True

        mock_db.query.return_value.filter.return_value.first.side_effect = [mock_user, mock_project]
        mock_db.delete.side_effect = Exception("Deletion failed")
        mock_db.rollback.return_value = None
        mock_db.close.return_value = None

        from auth import current_account
        from database import get_db

        def override_get_db():
            yield mock_db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[current_account] = lambda: mock_user
        original_middleware_factory = app.state.middleware_db_factory
        app.state.middleware_db_factory = _make_empty_middleware_factory()
        try:
            client = TestClient(app)
            response = client.request(
                "DELETE",
                "/api/projects/Test%20Project/enhanced",
                json={
                    "project_name": "Test Project",
                    "delete_github_resources": False,
                    "delete_deployment_environments": True
                }
            )

            assert response.status_code == 500
            mock_db.close.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(current_account, None)
            app.state.middleware_db_factory = original_middleware_factory


class TestErrorRecovery:
    """Tests for error recovery during GitHub resource deletion."""

    @patch('project_deletion.requests.get')
    def test_github_api_network_error_handled_gracefully(self, mock_get):
        """Network errors fetching secrets are caught without crashing."""
        mock_get.side_effect = Exception("Connection refused")

        headers = {"Authorization": "token test-token"}
        secrets = _fetch_repository_secrets("test/repo", headers, "AM_TEST_")

        # Returns empty list instead of raising
        assert secrets == []

    @patch('project_deletion.requests.get')
    def test_github_api_network_error_variables(self, mock_get):
        """Network errors fetching variables are caught without crashing."""
        mock_get.side_effect = Exception("Timeout")

        headers = {"Authorization": "token test-token"}
        variables = _fetch_repository_variables("test/repo", headers, "AM_TEST_")

        assert variables == []

    @patch('project_deletion.requests.get')
    def test_github_api_network_error_environments(self, mock_get):
        """Network errors fetching environments are caught without crashing."""
        mock_get.side_effect = Exception("DNS resolution failed")

        headers = {"Authorization": "token test-token"}
        environments, secrets, variables = _fetch_deployment_environments("test/repo", headers)

        assert environments == []
        assert secrets == []
        assert variables == []

    @patch('project_deletion.requests.get')
    def test_validate_repository_access_network_error(self, mock_get):
        """Network errors during repository access validation return False."""
        mock_get.side_effect = Exception("Connection reset")

        headers = {"Authorization": "token test-token"}
        result = _validate_repository_access("test/repo", headers)

        assert result is False

    @patch('project_deletion.requests.get')
    def test_validate_repository_access_auth_failure(self, mock_get):
        """401 during repository access validation returns False."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        headers = {"Authorization": "token invalid-token"}
        result = _validate_repository_access("test/repo", headers)

        assert result is False

    @patch('project_deletion.requests.get')
    def test_validate_repository_access_forbidden(self, mock_get):
        """403 during repository access validation returns False."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        headers = {"Authorization": "token test-token"}
        result = _validate_repository_access("test/repo", headers)

        assert result is False

    @patch('project_deletion.requests.get')
    @patch('project_deletion.requests.delete')
    def test_error_in_one_repo_does_not_stop_other_repos(self, mock_delete, mock_get):
        """Errors processing one repository don't prevent processing other repos."""
        from project_deletion import _delete_all_github_resources

        call_count = {"get": 0}

        def get_side_effect(url, headers=None, **kwargs):
            call_count["get"] += 1
            resp = Mock()
            if "repo1" in url and "/actions/secrets" in url:
                raise Exception("repo1 network error")
            elif "/actions/secrets" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "secrets": [{"name": "AM_X_SECRET"}]
                }
            elif "/actions/variables" in url:
                resp.status_code = 200
                resp.json.return_value = {"variables": []}
            elif "/environments" in url:
                resp.status_code = 200
                resp.json.return_value = {"environments": []}
            else:
                resp.status_code = 404
            return resp

        mock_get.side_effect = get_side_effect
        mock_delete.return_value = Mock(status_code=204)

        mock_project = Mock(spec=Project)
        mock_project.use_prefix = True

        deletion_results = {"github_resources_deleted": [], "errors": []}
        headers = {"Authorization": "token test-token"}

        _delete_all_github_resources(
            ["owner/repo1", "owner/repo2"],
            headers, "X", deletion_results, mock_project, None, True
        )

        # repo1 should have an error, but repo2 should still be processed
        assert len(deletion_results["errors"]) >= 1
        assert any("repo1" in e for e in deletion_results["errors"])
        # repo2 should have processed successfully
        assert any("repo2" in d for d in deletion_results["github_resources_deleted"])

    @patch('project_deletion.requests.get')
    def test_environment_secrets_fetch_api_error_handled(self, mock_get):
        """Errors fetching environment secrets are handled gracefully."""
        mock_get.side_effect = Exception("API rate limited")

        headers = {"Authorization": "token test-token"}
        secrets = _fetch_environment_secrets("test/repo", "production", headers, "AM_TEST_")

        assert secrets == []

    @patch('project_deletion.requests.get')
    def test_environment_variables_fetch_api_error_handled(self, mock_get):
        """Errors fetching environment variables are handled gracefully."""
        mock_get.side_effect = Exception("API unavailable")

        headers = {"Authorization": "token test-token"}
        variables = _fetch_environment_variables("test/repo", "production", headers, "AM_TEST_")

        assert variables == []

    def test_get_project_and_user_strips_whitespace(self):
        """Whitespace in github_user is stripped before lookup."""
        mock_db = Mock(spec=Session)
        mock_user = Mock(spec=Account)
        mock_user.user_id = 1

        mock_project = Mock(spec=Project)
        mock_project.project_id = 1
        mock_project.user_id = 1

        mock_db.query.return_value.filter.return_value.first.return_value = mock_user

        with patch("project_deletion.find_project_by_name", return_value=mock_project):
            user, project = _get_project_and_user("Test Project", "  testuser  ", mock_db)

        assert user == mock_user
        assert project == mock_project

    @patch('project_deletion.requests.get')
    def test_fetch_secrets_api_non_200_returns_empty(self, mock_get):
        """Non-200 response from secrets API returns empty list."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        headers = {"Authorization": "token test-token"}
        secrets = _fetch_repository_secrets("test/repo", headers, "AM_TEST_")

        assert secrets == []

    @patch('project_deletion.requests.get')
    def test_fetch_variables_api_non_200_returns_empty(self, mock_get):
        """Non-200 response from variables API returns empty list."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        headers = {"Authorization": "token test-token"}
        variables = _fetch_repository_variables("test/repo", headers, "AM_TEST_")

        assert variables == []

class TestDeletionSummaryEndpointAuth:
    """The deletion-summary endpoint is session-authenticated.

    Like its sibling /enhanced, this route depends on auth.current_account, so a
    request that carries no session token is rejected before any project lookup.
    A frontend caller that does not send credentials therefore never sees the
    summary, and the delete dialog can only offer database-only deletion.
    """

    def test_returns_401_without_a_session(self):
        """No session token - the summary is refused."""
        from fastapi.testclient import TestClient
        from main import app

        original_middleware_factory = app.state.middleware_db_factory
        app.state.middleware_db_factory = _make_empty_middleware_factory()
        try:
            client = TestClient(app)
            response = client.get(
                "/api/projects/Demo-Project2/deletion-summary",
                params={"github_user": "testuser"},
            )

            assert response.status_code == 401
            assert response.json()["detail"] == "Authentication required"
        finally:
            app.state.middleware_db_factory = original_middleware_factory

    @patch("project_deletion.user_tokens", {})
    def test_returns_the_summary_once_the_session_is_supplied(self, db, client):
        """Same request, authenticated - the endpoint itself works fine."""
        _add_project(db, name="Demo-Project2", code="DP2")

        response = client.get("/api/projects/Demo-Project2/deletion-summary")

        assert response.status_code == 200
        body = response.json()
        assert body["project_name"] == "Demo-Project2"
        assert body["project_code"] == "DP2"
        assert body["workflows"] == []


def _make_db_with_projects(project_names: list[str]):
    """Real SQLite session holding one account and the given projects.

    A Mock(spec=Session) cannot exercise this - the bug being pinned is in how
    SQL matches the name, so the query has to reach an actual database.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    account = Account(
        github_user="testuser",
        github_email="testuser@example.com",
        account_type="free",
    )
    db.add(account)
    db.commit()

    for index, name in enumerate(project_names):
        db.add(Project(project_name=name, project_code=f"P{index}", user_id=account.user_id))
    db.commit()
    return db


class TestProjectLookupIsNotAWildcardMatch:
    """_get_project_and_user must match the name exactly, case-insensitively.

    It used to use ilike(), where `_` matches any character and `%` matches any
    run of them. The same helper backs DELETE /projects/{name}/enhanced, so a
    lookup that drifted onto a sibling project deleted that sibling - including
    its GitHub secrets and environments when "delete everything" was chosen.
    """

    def test_underscore_does_not_match_an_arbitrary_character(self):
        db = _make_db_with_projects(["webXapp", "web_app"])
        try:
            _, project = _get_project_and_user("web_app", "testuser", db)
            assert project.project_name == "web_app"
        finally:
            db.close()

    def test_underscore_name_is_not_reachable_by_a_sibling(self):
        """Looking up webXapp must never return web_app either."""
        db = _make_db_with_projects(["web_app", "webXapp"])
        try:
            _, project = _get_project_and_user("webXapp", "testuser", db)
            assert project.project_name == "webXapp"
        finally:
            db.close()

    def test_percent_matches_nothing_rather_than_everything(self):
        db = _make_db_with_projects(["alpha", "beta"])
        try:
            with pytest.raises(HTTPException) as exc:
                _get_project_and_user("%", "testuser", db)
            assert exc.value.status_code == 404
        finally:
            db.close()

    def test_still_matches_case_insensitively(self):
        db = _make_db_with_projects(["Demo-Project2"])
        try:
            _, project = _get_project_and_user("demo-project2", "testuser", db)
            assert project.project_name == "Demo-Project2"
        finally:
            db.close()


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}


def _requests_calls(source):
    """Yield every `requests.<method>(...)` Call node in a module's source."""
    import ast

    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
            and node.func.attr in _HTTP_METHODS
        ):
            yield node


def _backend_modules_calling_requests():
    """Backend modules with at least one requests.* call, discovered at collection.

    Discovered rather than listed so a new module is covered the day it is
    written, instead of the day someone remembers to add it here.
    """
    backend = Path(__file__).resolve().parents[1]
    return [
        path.name
        for path in sorted(backend.glob("*.py"))
        if any(_requests_calls(path.read_text()))
    ]


class TestEveryGitHubCallHasATimeout:
    """requests has no default timeout, so a hung socket pins a worker forever.

    Covers the whole backend. The AST walk matters over a grep: workflows.py and
    project_deletion.py both carry multi-line calls whose `requests.get(` and
    `timeout=` sit on different lines, which a line-based check reads as a
    violation and a missing timeout as clean.
    """

    @pytest.mark.parametrize("module_name", _backend_modules_calling_requests())
    def test_module_passes_timeout_to_every_requests_call(self, module_name):
        source = (Path(__file__).resolve().parents[1] / module_name).read_text()
        untimed = [
            node.lineno
            for node in _requests_calls(source)
            if "timeout" not in {kw.arg for kw in node.keywords}
        ]

        assert untimed == [], f"{module_name} has requests calls without timeout at lines {untimed}"

    def test_discovery_actually_finds_the_modules(self):
        """A typo in the glob would make every test above vacuously pass."""
        discovered = _backend_modules_calling_requests()

        assert "auth.py" in discovered
        assert "workflows.py" in discovered
        assert "github_api_tracker.py" in discovered
        assert len(discovered) >= 9
