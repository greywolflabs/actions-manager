"""
Tests for the refactored adopt_github_version helper functions.

Covers the cognitive complexity reduction refactoring (python:S3776).
Tests verify that individual helper functions work correctly and maintain
the same behavior as the original monolithic function.
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from workflows import (  # noqa: E402
    _validate_adopt_github_request,
    _resolve_source_repo,
    _fetch_github_content_and_affected_repos,
    _handle_create_repo_override,
    _update_project_workflow,
    _handle_adopt_local_only,
    _get_target_repos_for_sync,
    _handle_sync_direct_push,
    _direct_push_workflow_to_branch,
    _handle_sync_pr_mode,
    _handle_adopt_project_and_sync,
    AdoptGithubVersionRequest,
    DriftCheckUnavailable,
    ADOPT_PROJECT_AND_SYNC,
    ADOPT_LOCAL_ONLY,
    CREATE_REPO_OVERRIDE,
)
from models import (  # noqa: E402
    Base, Account, Project, Repo, ProjectRepo, Workflow, ProjectWorkflow,
    RepoWorkflowOverride, FileDelivery,
)
from auth import user_tokens  # noqa: E402
from tests.delivery_records import record_drift  # noqa: E402
from github_api_tracker import RateLimitExceeded  # noqa: E402


SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db_session():
    """Create a fresh database session for each test."""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def test_data(db_session):
    """Create test data with user, project, repos, and workflow."""
    user = Account(github_user="testuser", github_email="test@example.com", account_type="free")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    project = Project(
        project_name="test-project",
        project_code="TEST",
        user_id=user.user_id,
        branch_option="default",
        use_prefix=True,
        pr_state="synced",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    repo1 = Repo(repo_name="owner/repo1")
    repo2 = Repo(repo_name="owner/repo2")
    db_session.add_all([repo1, repo2])
    db_session.commit()
    db_session.refresh(repo1)
    db_session.refresh(repo2)

    db_session.add_all([
        ProjectRepo(project_id=project.project_id, repo_id=repo1.repo_id),
        ProjectRepo(project_id=project.project_id, repo_id=repo2.repo_id),
    ])
    db_session.commit()

    workflow = Workflow(
        workflow_name="ci",
        workflow_yaml="name: AM_TEST_ci\non: push",
        workflow_git_hash="sha-old",
        reusable_workflow=False,
        workflow_status="synced_with_github",
    )
    db_session.add(workflow)
    db_session.commit()
    db_session.refresh(workflow)

    db_session.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db_session.commit()

    user_tokens["testuser"] = "test-token"

    yield {
        "user": user,
        "project": project,
        "workflow": workflow,
        "repo1": repo1,
        "repo2": repo2,
    }

    user_tokens.pop("testuser", None)


class TestValidateAdoptGithubRequest:
    """Tests for _validate_adopt_github_request helper."""

    def test_unauthenticated_user_raises_401(self, db_session, test_data):
        """Test that unauthenticated users get 401."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        with pytest.raises(HTTPException) as exc_info:
            _validate_adopt_github_request(db_session, payload, "unknownuser")
        assert exc_info.value.status_code == 401

    def test_invalid_resolution_mode_raises_400(self, db_session, test_data):
        """Test that invalid resolution mode raises 400."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            resolution_mode="invalid_mode",
        )
        with pytest.raises(HTTPException) as exc_info:
            _validate_adopt_github_request(db_session, payload, "testuser")
        assert exc_info.value.status_code == 400

    @pytest.mark.parametrize("branch", [None, "", "  "])
    def test_a_missing_branch_raises_400(self, db_session, test_data, branch):
        """Adopting from the repo default would import a file the user wasn't shown."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            branch=branch,
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        with pytest.raises(HTTPException) as exc_info:
            _validate_adopt_github_request(db_session, payload, "testuser")
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "branch is required to adopt the GitHub version"

    def test_a_padded_branch_is_stripped(self, db_session, test_data):
        """The fetch and the drift clear use it, and neither matches a padded name."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            branch=" release/2.1 ",
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        _validate_adopt_github_request(db_session, payload, "testuser")
        assert payload.branch == "release/2.1"

    def test_project_not_found_raises_404(self, db_session, test_data):
        """Test that non-existent project raises 404."""
        payload = AdoptGithubVersionRequest(
            project_id=99999,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            branch="main",
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        with pytest.raises(HTTPException) as exc_info:
            _validate_adopt_github_request(db_session, payload, "testuser")
        assert exc_info.value.status_code == 404

    @patch('workflows._find_project_by_name')
    def test_valid_request_returns_entities(self, mock_find, db_session, test_data):
        """Test that valid request returns all expected entities."""
        mock_find.return_value = test_data["project"]
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            branch="main",
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        result = _validate_adopt_github_request(db_session, payload, "testuser")
        project, workflow, source_repo, token, formatted_name, owner, repo = result
        
        assert project.project_id == test_data["project"].project_id
        assert workflow.workflow_id == test_data["workflow"].workflow_id
        assert source_repo.repo_id == test_data["repo1"].repo_id
        assert token == "test-token"
        assert owner == "owner"
        assert repo == "repo1"


class TestResolveSourceRepo:
    """Tests for _resolve_source_repo helper."""

    def test_resolve_by_repo_id(self, db_session, test_data):
        """Test resolving repo by ID."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        result = _resolve_source_repo(db_session, payload, test_data["project"])
        assert result.repo_id == test_data["repo1"].repo_id

    def test_resolve_by_repo_name(self, db_session, test_data):
        """Test resolving repo by name when ID is not provided."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_name="owner/repo1",
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        result = _resolve_source_repo(db_session, payload, test_data["project"])
        assert result.repo_name == "owner/repo1"

    def test_repo_not_found_raises_404(self, db_session, test_data):
        """Test that non-existent repo raises 404."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=99999,
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        with pytest.raises(HTTPException) as exc_info:
            _resolve_source_repo(db_session, payload, test_data["project"])
        assert exc_info.value.status_code == 404

    def test_repo_not_in_project_raises_400(self, db_session, test_data):
        """Test that repo not in project raises 400."""
        other_repo = Repo(repo_name="other/repo")
        db_session.add(other_repo)
        db_session.commit()
        db_session.refresh(other_repo)

        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=other_repo.repo_id,
            resolution_mode=ADOPT_LOCAL_ONLY,
        )
        with pytest.raises(HTTPException) as exc_info:
            _resolve_source_repo(db_session, payload, test_data["project"])
        assert exc_info.value.status_code == 400


class TestGetTargetReposForSync:
    """Tests for _get_target_repos_for_sync helper."""

    def test_returns_all_when_no_filter(self, test_data):
        """Test that all affected repos are returned when no filter is specified."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            resolution_mode=ADOPT_PROJECT_AND_SYNC,
            target_repo_ids=None,
        )
        affected_repos = [test_data["repo1"], test_data["repo2"]]
        result = _get_target_repos_for_sync(payload, affected_repos)
        assert len(result) == 2

    def test_filters_by_target_repo_ids(self, test_data):
        """Test that repos are filtered by target_repo_ids."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            resolution_mode=ADOPT_PROJECT_AND_SYNC,
            target_repo_ids=[test_data["repo1"].repo_id],
        )
        affected_repos = [test_data["repo1"], test_data["repo2"]]
        result = _get_target_repos_for_sync(payload, affected_repos)
        assert len(result) == 1
        assert result[0].repo_id == test_data["repo1"].repo_id


class TestUpdateProjectWorkflow:
    """Tests for _update_project_workflow helper."""

    def test_updates_workflow_fields(self, db_session, test_data):
        """Test that workflow fields are properly updated."""
        workflow = test_data["workflow"]
        _update_project_workflow(
            db_session, workflow, "new content", "new-sha", "testuser"
        )
        db_session.refresh(workflow)
        
        assert workflow.workflow_yaml == "new content"
        assert workflow.workflow_git_hash == "new-sha"
        assert workflow.last_modified_by == "testuser"
        assert workflow.workflow_status == "synced_with_github"


class TestHandleAdoptLocalOnly:
    """Tests for _handle_adopt_local_only helper."""

    def test_returns_correct_response_with_affected_repos(self, db_session, test_data):
        """Test response when there are affected repos."""
        result = _handle_adopt_local_only(
            db_session,
            test_data["project"],
            test_data["repo1"],
            [test_data["repo2"]],
        )
        assert result["success"] is True
        assert result["resolution_mode"] == ADOPT_LOCAL_ONLY
        assert result["updated_project_workflow"] is True
        assert "owner/repo2" in result["affected_repos"]
        assert result["new_drift_status"] == "drifted_on_other_repos"

    def test_returns_synced_when_no_affected_repos(self, db_session, test_data):
        """Test response when there are no affected repos."""
        result = _handle_adopt_local_only(
            db_session,
            test_data["project"],
            test_data["repo1"],
            [],
        )
        assert result["new_drift_status"] == "synced"

    def test_updates_pr_state_to_draft(self, db_session, test_data):
        """Test that pr_state is updated to draft when there are affected repos."""
        project = test_data["project"]
        project.pr_state = "synced"
        db_session.commit()
        
        _handle_adopt_local_only(
            db_session,
            project,
            test_data["repo1"],
            [test_data["repo2"]],
        )
        db_session.refresh(project)
        assert project.pr_state == "draft"


class TestHandleCreateRepoOverride:
    """Tests for _handle_create_repo_override helper."""

    def test_creates_new_override(self, db_session, test_data):
        """Test creating a new repo override."""
        result = _handle_create_repo_override(
            db_session,
            test_data["project"],
            test_data["workflow"],
            test_data["repo1"],
            "new content",
            "new-sha",
            "testuser",
            [],
        )
        assert result["success"] is True
        assert result["resolution_mode"] == CREATE_REPO_OVERRIDE
        assert result["updated_project_workflow"] is False
        assert result["created_or_updated_override"] is not None
        assert "created" in result["message"]

    def test_updates_existing_override(self, db_session, test_data):
        """Test updating an existing repo override."""
        # Create an existing override
        existing = RepoWorkflowOverride(
            project_id=test_data["project"].project_id,
            repo_id=test_data["repo1"].repo_id,
            workflow_id=test_data["workflow"].workflow_id,
            workflow_name="ci",
            workflow_yaml="old content",
            workflow_git_hash="old-sha",
            source_repo_name="owner/repo1",
        )
        db_session.add(existing)
        db_session.commit()

        result = _handle_create_repo_override(
            db_session,
            test_data["project"],
            test_data["workflow"],
            test_data["repo1"],
            "new content",
            "new-sha",
            "testuser",
            [],
        )
        assert result["success"] is True
        assert "updated" in result["message"]


class TestHandleAdoptProjectAndSync:
    """Tests for _handle_adopt_project_and_sync helper."""

    def test_returns_synced_when_no_target_repos(self, db_session, test_data):
        """Test that synced status is returned when no repos need syncing."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            resolution_mode=ADOPT_PROJECT_AND_SYNC,
            target_repo_ids=[],
        )
        result = _handle_adopt_project_and_sync(
            db_session, payload, test_data["project"], test_data["workflow"],
            test_data["repo1"], [], "AM_TEST_ci.yml", "content", "token", "testuser",
        )
        assert result["success"] is True
        assert result["new_drift_status"] == "synced"
        assert result["affected_repos"] == []

    def test_invalid_delivery_mode_raises_400(self, db_session, test_data):
        """Test that invalid delivery mode raises 400."""
        payload = AdoptGithubVersionRequest(
            project_id=test_data["project"].project_id,
            workflow_id=test_data["workflow"].workflow_id,
            repo_id=test_data["repo1"].repo_id,
            resolution_mode=ADOPT_PROJECT_AND_SYNC,
            delivery_mode="invalid",
        )
        with pytest.raises(HTTPException) as exc_info:
            _handle_adopt_project_and_sync(
                db_session, payload, test_data["project"], test_data["workflow"],
                test_data["repo1"], [test_data["repo2"]], "AM_TEST_ci.yml",
                "content", "token", "testuser",
            )
        assert exc_info.value.status_code == 400


class TestHandleSyncDirectPush:
    """Direct-mode adopt-and-sync delivers where the project delivers (#2301, #2302)."""

    def _add_repo(self, db, project, name):
        repo = Repo(repo_name=name)
        db.add(repo)
        db.commit()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.commit()
        return repo

    def _sync(self, db, data, targets, push, clear=None):
        pushed, cleared = [], []

        def fake_push(owner, repo, branch, *_args, **_kwargs):
            pushed.append((f"{owner}/{repo}", branch))
            return push(f"{owner}/{repo}", branch)

        def fake_clear(_db, _project, _kind, _workflow_id, repo, branch):
            cleared.append((repo, branch))
            if clear:
                clear(repo, branch)

        with patch("workflows._direct_push_workflow_to_branch", side_effect=fake_push), \
             patch("workflows.clear_drift", side_effect=fake_clear):
            result = _handle_sync_direct_push(
                db, data["project"], data["workflow"], data["repo1"], targets,
                "AM_TEST_ci.yml", "name: ci", "token", "testuser",
            )
        return result, pushed, cleared

    def test_pushes_to_every_branch_the_project_delivers_to(self, db_session, test_data):
        project = test_data["project"]
        project.branch_option = "pattern"
        project.branch_regex = "release/.*"
        project.pr_state = "open"
        db_session.commit()

        def branches(_owner, _repo, option, regex, *_args, **_kwargs):
            assert (option, regex) == ("pattern", "release/.*")
            return ["release/1.x", "release/2.x"]

        with patch("workflows._resolve_branches_for_repo", side_effect=branches), \
             patch("workflows.get_default_branch", side_effect=AssertionError("read the default branch")):
            result, pushed, cleared = self._sync(
                db_session, test_data, [test_data["repo2"]],
                lambda _repo, _branch: {"status_code": 201, "sha": "sha-new"},
            )

        expected = [("owner/repo2", "release/1.x"), ("owner/repo2", "release/2.x")]
        assert pushed == expected
        assert cleared == expected
        assert result["success"] is True
        assert [(e["repo"], e["branch"], e["status"]) for e in result["sync_results"]["repos"]] == [
            ("owner/repo2", "release/1.x", "synced"), ("owner/repo2", "release/2.x", "synced"),
        ]
        assert project.pr_state == "synced"

    def test_a_default_branch_project_still_pushes_to_its_default_branch(self, db_session, test_data):
        with patch("workflows.get_default_branch", return_value="trunk"):
            result, pushed, cleared = self._sync(
                db_session, test_data, [test_data["repo2"]],
                lambda _repo, _branch: {"status_code": 200, "sha": "sha-new"},
            )

        assert pushed == cleared == [("owner/repo2", "trunk")]
        assert result["success"] is True

    def test_a_later_failure_keeps_what_earlier_targets_did(self, db_session, test_data):
        """#2302: repo2 is written and its drift cleared, then repo3 hits the rate limit."""
        project, workflow, repo2 = test_data["project"], test_data["workflow"], test_data["repo2"]
        repo3 = self._add_repo(db_session, project, "owner/repo3")
        record_drift(db_session, workflow, repo2.repo_name, "main")

        def push(repo, _branch):
            if repo == "owner/repo3":
                raise RateLimitExceeded("API rate limit exceeded. Limit resets at 2026-10-08T07:00:00")
            return {"status_code": 201, "sha": "sha-new"}

        with patch("workflows.get_default_branch", return_value="main"), \
             patch("workflows._direct_push_workflow_to_branch",
                   side_effect=lambda owner, repo, *_a, **_k: push(f"{owner}/{repo}", None)):
            result = _handle_sync_direct_push(
                db_session, project, workflow, test_data["repo1"], [repo2, repo3],
                "AM_TEST_ci.yml", "name: ci", "token", "testuser",
            )

        assert result["success"] is False
        by_repo = {e["repo"]: e for e in result["sync_results"]["repos"]}
        assert by_repo["owner/repo2"]["status"] == "synced"
        assert by_repo["owner/repo3"]["status"] == "failed"
        assert by_repo["owner/repo3"]["error"] == "API rate limit exceeded. Limit resets at 2026-10-08T07:00:00"
        # The UI shows only the message, so it has to say which target failed and why.
        assert "owner/repo3@main: API rate limit exceeded. Limit resets at 2026-10-08T07:00:00" in result["message"]
        assert project.pr_state == "draft"
        db_session.expire_all()
        state = db_session.query(FileDelivery).filter_by(repo_id=repo2.repo_id).one()
        assert state.has_drift is False  # repo3's rollback did not undo repo2's clear

    def test_a_drift_clear_failing_does_not_stop_the_sync(self, db_session, test_data):
        repo3 = self._add_repo(db_session, test_data["project"], "owner/repo3")

        def clear(repo, _branch):
            if repo == "owner/repo2":
                raise RuntimeError("database is locked")

        with patch("workflows.get_default_branch", return_value="main"):
            result, pushed, _cleared = self._sync(
                db_session, test_data, [test_data["repo2"], repo3],
                lambda _repo, _branch: {"status_code": 201, "sha": "sha-new"}, clear=clear,
            )

        assert pushed == [("owner/repo2", "main"), ("owner/repo3", "main")]
        assert result["success"] is True
        assert [e["status"] for e in result["sync_results"]["repos"]] == ["synced", "synced"]

    def test_a_repository_override_decides_its_branches(self, db_session, test_data):
        """The point of resolving through the delivery path: a repo's own branch config wins."""
        assoc = db_session.query(ProjectRepo).filter_by(repo_id=test_data["repo2"].repo_id).one()
        assoc.branch_config_mode = "override"
        assoc.branch_option = "pattern"
        assoc.branch_regex = "hotfix/.*"
        db_session.commit()

        def branches(_owner, _repo, option, regex, *_args, **_kwargs):
            assert (option, regex) == ("pattern", "hotfix/.*")
            return ["hotfix/42"]

        with patch("workflows._resolve_branches_for_repo", side_effect=branches):
            _result, pushed, _cleared = self._sync(
                db_session, test_data, [test_data["repo2"]],
                lambda _repo, _branch: {"status_code": 201, "sha": "sha-new"},
            )

        assert pushed == [("owner/repo2", "hotfix/42")]

    def test_the_branch_is_encoded_when_reading_the_existing_file(self):
        """A pattern can match a branch like release/1.0+hotfix; unencoded, '+' reads as a space."""
        with patch("workflows._check_existing_workflow_content", return_value=(None, False)) as check, \
             patch("workflows.github_put") as put:
            put.return_value.status_code = 201
            put.return_value.json.return_value = {"content": {"sha": "sha-new"}}
            _direct_push_workflow_to_branch(
                "owner", "repo2", "release/1.0+hotfix", ".github/workflows/ci.yml", "name: ci",
                "msg", {}, "testuser", None,
            )

        assert check.call_args[0][0].endswith("?ref=release%2F1.0%2Bhotfix")
        assert put.call_args.kwargs["json"]["branch"] == "release/1.0+hotfix"

    def test_a_repo_whose_branches_cannot_be_resolved_is_reported(self, db_session, test_data):
        repo3 = self._add_repo(db_session, test_data["project"], "owner/repo3")

        def default_branch(owner, repo, *_args, **_kwargs):
            if repo == "repo2":
                raise DriftCheckUnavailable("GitHub returned 403 resolving the default branch of owner/repo2", 403)
            return "main"

        with patch("workflows.get_default_branch", side_effect=default_branch):
            result, pushed, _cleared = self._sync(
                db_session, test_data, [test_data["repo2"], repo3],
                lambda _repo, _branch: {"status_code": 201, "sha": "sha-new"},
            )

        assert pushed == [("owner/repo3", "main")]
        assert result["sync_results"]["repos"][0] == {
            "repo": "owner/repo2", "branch": None, "status": "failed", "github_sha": None,
            "error": "Failed to resolve branches: GitHub returned 403 resolving the default branch of owner/repo2",
        }
        assert result["success"] is False


class TestFetchGithubContentAndAffectedRepos:
    """Tests for _fetch_github_content_and_affected_repos helper."""

    @patch('workflows.get_workflow_from_github')
    def test_raises_404_when_workflow_not_found(self, mock_get, db_session, test_data):
        """Test that 404 is raised when workflow not found on GitHub."""
        mock_get.return_value = None
        with pytest.raises(HTTPException) as exc_info:
            _fetch_github_content_and_affected_repos(
                db_session, test_data["project"], test_data["workflow"],
                test_data["repo1"], "AM_TEST_ci.yml", "owner", "repo1", "token", "main",
            )
        assert exc_info.value.status_code == 404

    @patch('workflows.get_workflow_from_github')
    def test_returns_content_and_affected_repos(self, mock_get, db_session, test_data):
        """Test that GitHub content and affected repos are returned."""
        mock_get.return_value = {"content": "new content", "sha": "new-sha"}
        content, sha, affected = _fetch_github_content_and_affected_repos(
            db_session, test_data["project"], test_data["workflow"],
            test_data["repo1"], "AM_TEST_ci.yml", "owner", "repo1", "token", "main",
        )
        assert content == "new content"
        assert sha == "new-sha"
        assert len(affected) == 1  # repo2 is affected, repo1 is the source
        assert affected[0].repo_name == "owner/repo2"
