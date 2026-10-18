"""
Test that the refactored detect_workflow_drift function still works correctly.
This test verifies that the cognitive complexity reduction didn't break functionality.
"""
import pytest
from unittest.mock import MagicMock, patch
import deliveries
from workflows import (
    detect_workflow_drift,
    _validate_user_and_get_project,
    _get_project_workflows,
    _check_regular_workflow_in_repo,
    _process_regular_workflows,
    _process_reusable_workflows,
    _handle_deployment_variables,
    _create_drift_status
)
from models import Account, Base, Project, Workflow, ProjectWorkflow
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from tests.delivery_records import record_delivered


FILENAME = "AM_TEST_test-workflow.yml"


def _workflow_db(yaml="test content", delivered=False, git_hash="def456"):
    """A workflow, in a database of its own, optionally with the delivery
    record a merge to test/repo@main writes."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    account = Account(github_user="drift-refactor", github_email="d@example.com", account_type="free")
    db.add(account)
    db.flush()
    project = Project(project_name="drift-refactor", project_code="TEST", user_id=account.user_id)
    workflow = Workflow(workflow_name="test-workflow", workflow_yaml=yaml, workflow_git_hash=git_hash)
    db.add_all([project, workflow])
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    if delivered:
        record_delivered(db, workflow, "test/repo", "main")
    return db, workflow, project.project_id


def _check(github, **workflow):
    """The drift check of the workflow on test/repo@main, GitHub holding *github* ({content, sha}) or nothing."""
    db, wf, project_id = _workflow_db(**workflow)
    shas = {} if github is None else {FILENAME: github["sha"]}
    try:
        with patch("workflows.get_workflow_from_github", return_value=github):
            return _check_regular_workflow_in_repo(
                db, wf, "test/repo", "test", "repo", FILENAME, shas, "TEST", "token", project_id, "main",
                deliveries.targets_for(db, project_id),
            )
    finally:
        db.close()
from fastapi import HTTPException


class TestWorkflowDriftRefactor:
    """Test the refactored drift detection functions."""
    
    def test_create_drift_status(self):
        """Test the helper function to create drift status objects."""
        drift_status = _create_drift_status(
            workflow_name="test-workflow",
            has_drift=True,
            github_content="github content",
            local_content="local content",
            github_sha="abc123",
            local_sha="def456",
            message="Test message",
            drift_type="workflow"
        )
        
        assert drift_status.workflow_name == "test-workflow"
        assert drift_status.has_drift == True
        assert drift_status.github_content == "github content"
        assert drift_status.local_content == "local content"
        assert drift_status.github_sha == "abc123"
        assert drift_status.local_sha == "def456"
        assert drift_status.message == "Test message"
        assert drift_status.drift_type == "workflow"
    
    @patch('workflows.user_tokens', {'test_user': 'test_token'})
    @patch('workflows._find_project_by_name')
    def test_validate_user_and_get_project_success(self, mock_find_project):
        """Test successful user validation and project retrieval."""
        # Create mock database session
        mock_db = MagicMock()
        
        # Mock project returned by _find_project_by_name
        mock_project = MagicMock()
        mock_project.project_code = "TEST"
        mock_find_project.return_value = mock_project
        
        token, project, project_code = _validate_user_and_get_project(
            mock_db, 'test_user', 'test_project'
        )
        
        assert token == 'test_token'
        assert project == mock_project
        assert project_code == 'TEST'
        mock_find_project.assert_called_once_with(mock_db, 'test_user', 'test_project', None)
    
    @patch('workflows.user_tokens', {})
    def test_validate_user_and_get_project_auth_failure(self):
        """Test authentication failure in user validation."""
        mock_db = MagicMock()
        
        with pytest.raises(HTTPException) as exc_info:
            _validate_user_and_get_project(mock_db, 'invalid_user', 'test_project')
        
        assert exc_info.value.status_code == 401
        assert "User not authenticated" in str(exc_info.value.detail)
    
    def _split(self, project_type):
        mock_db = MagicMock()
        mock_project = MagicMock()
        mock_project.project_id = 1
        mock_project.project_type = project_type

        regular_workflow = MagicMock()
        regular_workflow.reusable_workflow = False
        reusable_workflow = MagicMock()
        reusable_workflow.reusable_workflow = True

        mock_db.query.return_value.join.return_value.filter.return_value.all.return_value = [
            regular_workflow, reusable_workflow,
        ]
        regular, reusable = _get_project_workflows(mock_db, mock_project)
        return regular, reusable, regular_workflow, reusable_workflow

    def test_get_project_workflows(self):
        """An RWX project's reusable workflows are checked in its reusable repo."""
        regular, reusable, regular_workflow, reusable_workflow = self._split("rwx")

        assert regular == [regular_workflow]
        assert reusable == [reusable_workflow]

    def test_get_project_workflows_checks_owned_reusable_in_the_projects_repos(self):
        """#2085: a standard project delivers its own reusable workflows to its
        own repositories, so drift must read them there, not the reusable repo."""
        regular, reusable, regular_workflow, reusable_workflow = self._split("standard")

        assert regular == [regular_workflow, reusable_workflow]
        assert reusable == []
    
    def test_handle_deployment_variables_disabled(self):
        """Test deployment variables handling when disabled."""
        result = _handle_deployment_variables(False)
        assert result == []
    
    def test_handle_deployment_variables_enabled(self):
        """Test deployment variables handling when enabled."""
        result = _handle_deployment_variables(True)
        assert len(result) == 1
        assert result[0].workflow_name == "deployment_variables"
        assert result[0].has_drift == True
        assert result[0].drift_type == "deployment_vars"
    
    def test_matching_content_is_no_drift(self):
        result = _check({"content": "test content", "sha": "abc123"})

        assert result.workflow_name == "test-workflow"
        assert result.has_drift is False
        assert "synchronized" in result.message

    def test_different_content_where_it_was_delivered_is_drift(self):
        result = _check({"content": "different content", "sha": "abc123"}, yaml="original content", delivered=True)

        assert result.workflow_name == "test-workflow"
        assert result.has_drift is True
        assert "differs" in result.message

    def test_a_missing_file_where_it_was_delivered_is_a_deletion(self):
        result = _check(None, delivered=True)

        assert result.workflow_name == "test-workflow"
        assert result.has_drift is True
        assert "deleted" in result.message

    def test_a_missing_file_never_delivered_is_reported_as_such(self):
        """Not drift, just not delivered yet. Reported rather than dropped, so a
        check can clear what an earlier one recorded there. (No blob SHA: one
        could be a delivery made before the record existed.)"""
        result = _check(None, git_hash=None)

        assert result.has_drift is False
        assert "not delivered" in result.message

    @patch('workflows._validate_user_and_get_project')
    @patch('workflows._get_project_workflows')
    @patch('workflows._process_regular_workflows')
    @patch('workflows._process_reusable_workflows')
    @patch('workflows._handle_deployment_variables')
    def test_detect_workflow_drift_integration(self, mock_handle_vars, mock_process_reusable,
                                             mock_process_regular, mock_get_workflows,
                                             mock_validate):
        """Test the main detect_workflow_drift function integration."""
        # Set up mocks
        mock_validate.return_value = ("token", MagicMock(), "TEST")
        mock_get_workflows.return_value = ([], [])
        mock_process_regular.return_value = [_create_drift_status("regular", False, "", "", "", "", "No drift", "workflow")]
        mock_process_reusable.return_value = [_create_drift_status("reusable", True, "", "", "", "", "Has drift", "reusable_workflow")]
        mock_handle_vars.return_value = []
        
        # Call the main function
        result = detect_workflow_drift(
            MagicMock(), "test_user", "test_project", ["test/repo"], False
        )
        
        # Verify results
        assert len(result) == 2
        assert result[0].workflow_name == "regular"
        assert result[0].has_drift == False
        assert result[1].workflow_name == "reusable"
        assert result[1].has_drift == True
        
        # Verify all helper functions were called
        mock_validate.assert_called_once()
        mock_get_workflows.assert_called_once()
        mock_process_regular.assert_called_once()
        mock_process_reusable.assert_called_once()
        mock_handle_vars.assert_called_once_with(False)