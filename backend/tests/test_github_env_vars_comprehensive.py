"""
Comprehensive test suite for github_env_vars.py module

This test suite provides comprehensive coverage for:
- Environment variable CRUD operations
- Free account limit enforcement
- Variable validation
- Batch operations
- Error handling
- Sync operations
- Environment management
"""
import pytest
import sys
import os
from types import SimpleNamespace
from unittest.mock import patch, Mock, AsyncMock, MagicMock
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

# Add the backend directory to the Python path
sys.path.insert(0, os.path.dirname(__file__))

from github_env_vars import (
    router,
    count_project_env_vars,
    count_project_environments,
    _validate_request_data,
    _get_auth_headers,
    _format_env_var_key,
    _check_variable_exists_in_repos,
    _validate_sync_environment_request,
    _find_existing_environments,
    _create_missing_environments,
    _get_truly_new_variables,
    _update_or_create_variable,
    _repo_url,
    _require_valid_key,
)

# The routes take the caller from the session (#2241); called directly, they
# are handed the account auth.current_account would have resolved.
CALLER = SimpleNamespace(github_user="testuser", user_id=1)


class TestEnvVarsCRUD:
    """Test environment variable CRUD operations"""
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_update_env_vars_success(self):
        """Test successful environment variable update"""
        # Mock database objects
        mock_db = MagicMock()
        
        mock_project = Mock()
        mock_project.project_code = "TEST"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_project
        
        mock_account = Mock()
        mock_account.account_type = "pro"
        
        # Mock request
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "testuser",
            "repo_names": ["test/repo1"],
            "env": [{"key": "VAR1", "value": "value1"}],
            "project_name": "TestProject"
        })
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            # Mock check variable response (doesn't exist)
            mock_check_response = Mock()
            mock_check_response.status_code = 404
            
            # Mock create response (success)
            mock_create_response = Mock()
            mock_create_response.status_code = 201
            
            mock_get = AsyncMock(return_value=mock_check_response)
            mock_post = AsyncMock(return_value=mock_create_response)
            
            mock_client.return_value.__aenter__.return_value.get = mock_get
            mock_client.return_value.__aenter__.return_value.post = mock_post
            
            from github_env_vars import update_env_vars
            result = await update_env_vars(mock_request, mock_db, CALLER)
            
            assert "message" in result
            assert result["message"] == "✅ GitHub Repository Variables updated!"
            assert "results" in result
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    @patch('github_env_vars._find_project_by_name')
    async def test_get_env_vars_success(self, mock_find):
        """Test successful retrieval of environment variables"""
        # Mock database
        mock_db = MagicMock()
        mock_project = Mock()
        mock_project.project_code = "TEST"
        mock_find.return_value = mock_project
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            # Mock list variables response (first page)
            mock_list_response1 = Mock()
            mock_list_response1.status_code = 200
            mock_list_response1.json.return_value = {
                "variables": [
                    {"name": "AM_TEST_VAR1"},
                    {"name": "AM_TEST_VAR2"},
                    {"name": "OTHER_VAR"}
                ]
            }
            
            # Mock list variables response (second page - empty)
            mock_list_response2 = Mock()
            mock_list_response2.status_code = 200
            mock_list_response2.json.return_value = {"variables": []}
            
            # Mock get variable value response
            mock_value_response = Mock()
            mock_value_response.status_code = 200
            mock_value_response.json.return_value = {"value": "test_value"}
            
            mock_get = AsyncMock(side_effect=[
                mock_list_response1,  # First page of variables
                mock_list_response2,  # Second page (empty - stops pagination)
                mock_value_response,  # Value for AM_TEST_VAR1
                mock_value_response   # Value for AM_TEST_VAR2
            ])
            mock_client.return_value.__aenter__.return_value.get = mock_get
            
            from github_env_vars import get_env_vars
            result = await get_env_vars("test/repo1", "TestProject", mock_db, CALLER)
            
            assert "env_vars" in result
            assert len(result["env_vars"]) == 2  # Only AM_TEST_* variables
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_delete_env_vars_success(self):
        """Test successful deletion of environment variables"""
        # Mock database
        mock_db = MagicMock()
        mock_project = Mock()
        mock_project.project_code = "TEST"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_project
        
        # Mock request
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "testuser",
            "repo_names": ["test/repo1"],
            "env": [{"env_key": "AM_TEST_VAR1"}],
            "project_name": "TestProject"
        })
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            mock_delete_response = Mock()
            mock_delete_response.status_code = 204
            
            mock_delete = AsyncMock(return_value=mock_delete_response)
            mock_client.return_value.__aenter__.return_value.delete = mock_delete
            
            from github_env_vars import delete_env_vars
            result = await delete_env_vars(mock_request, mock_db, CALLER)
            
            assert "message" in result
            assert "deleted" in result["message"]
            assert "results" in result


class TestFreeAccountLimits:
    """Test free account limit enforcement"""
    
    
    


class TestVariableValidation:
    """Test variable validation"""
    
    def test_format_env_var_key_simple(self):
        """Test formatting simple environment variable key"""
        formatted = _format_env_var_key("MY_VAR", "TEST")
        assert formatted == "AM_TEST_MY_VAR"
    
    def test_format_env_var_key_with_prefix(self):
        """Test formatting key that already has prefix"""
        formatted = _format_env_var_key("AM_TEST_MY_VAR", "TEST")
        assert formatted == "AM_TEST_MY_VAR"
    
    def test_format_env_var_key_lowercase(self):
        """Test formatting with lowercase input"""
        formatted = _format_env_var_key("my_var", "test")
        assert formatted == "AM_TEST_MY_VAR"
    
    def test_format_env_var_key_mixed_case(self):
        """Test formatting with mixed case input"""
        formatted = _format_env_var_key("My_Var", "TeSt")
        assert formatted == "AM_TEST_MY_VAR"
    
    def test_validate_request_data_complete(self):
        """Test request data validation with complete data"""
        data = {
            "user": "testuser",
            "repo_names": ["owner/repo1", "owner/repo2"],
            "env": [{"key": "VAR1", "value": "value1"}],
            "project_name": "  TEST_PROJECT  "
        }
        
        repo_names, env_vars, project_name = _validate_request_data(data)
        
        assert repo_names == ["owner/repo1", "owner/repo2"]
        assert len(env_vars) == 1
        assert project_name == "TEST_PROJECT"
    
    def test_validate_request_data_missing_env(self):
        """Test request data validation with missing env"""
        data = {
            "user": "testuser",
            "repo_names": ["owner/repo1"],
            "project_name": "TEST"
        }
        
        repo_names, env_vars, project_name = _validate_request_data(data)
        assert env_vars == []

    @pytest.mark.parametrize("repo_names", [None, "owner/repo", [123], ["no-slash"]])
    def test_validate_request_data_refuses_malformed_repo_names(self, repo_names):
        """Malformed names reached the loops: None and a number were 500s, and a
        string was walked letter by letter."""
        with pytest.raises(HTTPException) as refused:
            _validate_request_data({"repo_names": repo_names, "project_name": "P"})
        assert refused.value.status_code == 400

    def test_valid_key_accepts_ascii_letters_digits_and_underscores(self):
        assert _require_valid_key(" Api_Key_9 ") == "Api_Key_9"

    @pytest.mark.parametrize("key", ["ÄPI_KEY", "KEY_٣", "KEY-1", "KEY.1", "", None])
    def test_valid_key_refuses_anything_else(self, key):
        """GitHub variable names are ASCII. A bare \\w would also let Unicode
        letters and digits through, which GitHub then refuses."""
        with pytest.raises(HTTPException) as refused:
            _require_valid_key(key)
        assert refused.value.status_code == 400


class TestGitHubUrls:
    """Every GitHub URL the module calls is built by _repo_url."""

    def test_each_part_stays_in_its_segment(self):
        """'?' and '#' ended the path, so the rest of it never reached GitHub."""
        assert _repo_url("o/r", "environments", "qa?x#y") == "https://api.github.com/repos/o/r/environments/qa%3Fx%23y"
        assert _repo_url("o/r", "environments", "team/prod") == "https://api.github.com/repos/o/r/environments/team%2Fprod"

    @pytest.mark.parametrize("repo_name, segments", [
        ("o/r", ("environments", "..")),
        ("o/r", ("environments", "../../other")),
        ("o/r", ("environments", "prod/")),
        ("o/..", ("environments",)),
        ("a/b/../../../user", ("environments",)),
        ("o", ("environments",)),
        ("o/r", ("actions", "variables", "")),
    ])
    def test_refuses_a_part_that_would_leave_its_segment(self, repo_name, segments):
        """httpx resolves '..': DELETE .../environments/.. was DELETE /repos/o/r,
        which deletes the repository."""
        with pytest.raises(HTTPException) as refused:
            _repo_url(repo_name, *segments)
        assert refused.value.status_code == 400


class TestBatchOperations:
    """Test batch operations"""
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_update_multiple_vars_multiple_repos(self):
        """Test updating multiple variables across multiple repositories"""
        # Mock database
        mock_db = MagicMock()
        
        mock_project = Mock()
        mock_project.project_code = "TEST"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_project
        
        mock_account = Mock()
        mock_account.account_type = "pro"
        
        # Mock request with multiple vars and repos
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "testuser",
            "repo_names": ["test/repo1", "test/repo2"],
            "env": [
                {"key": "VAR1", "value": "value1"},
                {"key": "VAR2", "value": "value2"}
            ],
            "project_name": "TestProject"
        })
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            mock_check_response = Mock()
            mock_check_response.status_code = 404
            
            mock_create_response = Mock()
            mock_create_response.status_code = 201
            
            mock_get = AsyncMock(return_value=mock_check_response)
            mock_post = AsyncMock(return_value=mock_create_response)
            
            mock_client.return_value.__aenter__.return_value.get = mock_get
            mock_client.return_value.__aenter__.return_value.post = mock_post
            
            from github_env_vars import update_env_vars
            result = await update_env_vars(mock_request, mock_db, CALLER)
            
            assert "results" in result
            # Should have 4 results (2 vars × 2 repos)
            assert len(result["results"]) == 4
    
    @pytest.mark.asyncio
    @patch('httpx.AsyncClient')
    async def test_get_truly_new_variables(self, mock_client):
        """Test identifying new variables"""
        env_vars = [
            {"key": "VAR1", "value": "val1"},
            {"key": "VAR2", "value": "val2"},
            {"key": "VAR3", "value": "val3"}
        ]
        
        # Mock: VAR1 exists, VAR2 and VAR3 don't
        def mock_get_side_effect(url, headers):
            mock_response = Mock()
            if "AM_TEST_VAR1" in url:
                mock_response.status_code = 200
            else:
                mock_response.status_code = 404
            return mock_response
        
        mock_get = AsyncMock(side_effect=mock_get_side_effect)
        mock_client.return_value.__aenter__.return_value.get = mock_get
        
        headers = {"Authorization": "token fake_token"}
        new_vars = await _get_truly_new_variables(env_vars, "TEST", ["test/repo"], headers)
        
        # Should return only VAR2 and VAR3
        assert len(new_vars) == 2


class TestErrorHandling:
    """Test error handling"""
    
    @pytest.mark.asyncio
    async def test_update_env_vars_no_auth(self):
        """Test update fails without authentication"""
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "nonexistent_user",
            "repo_names": ["test/repo1"],
            "env": [{"key": "VAR1", "value": "value1"}],
            "project_name": "TestProject"
        })
        
        mock_db = MagicMock()
        
        from github_env_vars import update_env_vars
        result = await update_env_vars(mock_request, mock_db, SimpleNamespace(github_user="nonexistent_user"))
        
        assert "error" in result
        assert result["status"] == 401
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    @patch('github_env_vars._find_project_by_name')
    async def test_get_env_vars_project_not_found(self, mock_find):
        """Test get env vars fails with invalid project"""
        mock_db = MagicMock()
        mock_find.return_value = None
        
        from github_env_vars import get_env_vars
        result = await get_env_vars("test/repo1", "NonexistentProject", mock_db, CALLER)
        
        assert "error" in result
        assert result["status"] == 404
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_get_env_vars_api_error(self):
        """Test get env vars handles API errors"""
        mock_db = MagicMock()
        mock_project = Mock()
        mock_project.project_code = "TEST"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_project
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            mock_error_response = Mock()
            mock_error_response.status_code = 500
            
            mock_get = AsyncMock(return_value=mock_error_response)
            mock_client.return_value.__aenter__.return_value.get = mock_get
            
            from github_env_vars import get_env_vars
            result = await get_env_vars("test/repo1", "TestProject", mock_db, CALLER)
            
            assert "error" in result


class TestSyncOperations:
    """Test sync operations"""
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_sync_env_var_success(self):
        """Test successful sync of environment variable"""
        mock_db = MagicMock()
        mock_project = Mock()
        mock_project.project_code = "TEST"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_project
        
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "testuser",
            "project_name": "TestProject",
            "repo_names": ["test/repo1", "test/repo2"],
            "env_key": "VAR1"
        })
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            # Mock: repo1 has the variable, repo2 doesn't
            def mock_get_side_effect(url, headers):
                mock_response = Mock()
                if "repo1" in url:
                    mock_response.status_code = 200
                    mock_response.json.return_value = {"value": "test_value"}
                else:
                    mock_response.status_code = 404
                return mock_response
            
            mock_create_response = Mock()
            mock_create_response.status_code = 201
            
            mock_get = AsyncMock(side_effect=mock_get_side_effect)
            mock_post = AsyncMock(return_value=mock_create_response)
            
            mock_client.return_value.__aenter__.return_value.get = mock_get
            mock_client.return_value.__aenter__.return_value.post = mock_post
            
            from github_env_vars import sync_env_var
            result = await sync_env_var(mock_request, mock_db, CALLER)
            
            assert "message" in result
            assert "synced" in result["message"]


class TestEnvironmentManagement:
    """Test deployment environment management"""
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_create_environment_success(self):
        """Test successful environment creation"""
        mock_db = MagicMock()
        mock_account = Mock()
        mock_account.account_type = "pro"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_account
        
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "testuser",
            "repo_name": "test/repo1",
            "environment_name": "production"
        })
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            # Environment does not exist yet
            mock_check_response = Mock()
            mock_check_response.status_code = 404

            mock_response = Mock()
            mock_response.status_code = 201
            
            mock_get = AsyncMock(return_value=mock_check_response)
            mock_put = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value.get = mock_get
            mock_client.return_value.__aenter__.return_value.put = mock_put
            
            from github_env_vars import create_environment
            result = await create_environment(mock_request, mock_db, CALLER)
            
            assert "message" in result
            assert "created successfully" in result["message"]
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_get_environments_success(self):
        """Test successful retrieval of environments"""
        mock_db = MagicMock()
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "environments": [
                    {"name": "production"},
                    {"name": "staging"}
                ]
            }
            
            mock_get = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value.get = mock_get
            
            from github_env_vars import get_environments
            result = await get_environments("test/repo1", mock_db, CALLER)
            
            assert "environments" in result
            assert len(result["environments"]) == 2

    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_get_environments_passes_githubs_status_through(self):
        """A missing repository and a token without access stay distinguishable."""
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=Mock(status_code=404))

            from github_env_vars import get_environments
            result = await get_environments("test/gone", MagicMock(), CALLER)

        assert result["status"] == 404
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_delete_environment_success(self):
        """Test successful environment deletion"""
        mock_db = MagicMock()
        
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "testuser",
            "repo_names": ["test/repo1"],
            "environment_name": "production"
        })
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            mock_response = Mock()
            mock_response.status_code = 204
            mock_response.text = ""
            
            mock_delete = AsyncMock(return_value=mock_response)
            mock_client.return_value.__aenter__.return_value.delete = mock_delete
            
            from github_env_vars import delete_environment
            result = await delete_environment(mock_request, mock_db, CALLER)
            
            assert "message" in result
            assert "deleted" in result["message"]
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    async def test_sync_environment_success(self):
        """Test successful environment sync"""
        mock_db = MagicMock()
        mock_account = Mock()
        mock_account.account_type = "pro"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_account
        
        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "user": "testuser",
            "project_name": "TestProject",
            "repo_names": ["test/repo1", "test/repo2"],
            "environment_name": "staging"
        })
        
        with patch('github_env_vars.httpx.AsyncClient') as mock_client:
            # Mock: repo1 has the environment, repo2 doesn't
            def mock_get_side_effect(url, headers):
                mock_response = Mock()
                if "repo1" in url and "environments" in url:
                    mock_response.status_code = 200
                    mock_response.json.return_value = {
                        "environments": [{"name": "staging"}]
                    }
                elif "repo2" in url and "environments" in url:
                    mock_response.status_code = 200
                    mock_response.json.return_value = {"environments": []}
                else:
                    mock_response.status_code = 404
                return mock_response
            
            mock_create_response = Mock()
            mock_create_response.status_code = 201
            
            mock_get = AsyncMock(side_effect=mock_get_side_effect)
            mock_put = AsyncMock(return_value=mock_create_response)
            
            mock_client.return_value.__aenter__.return_value.get = mock_get
            mock_client.return_value.__aenter__.return_value.put = mock_put
            
            from github_env_vars import sync_environment
            result = await sync_environment(mock_request, mock_db, CALLER)
            
            assert "message" in result
            assert "synced" in result["message"]
    


class TestCountingFunctions:
    """Test counting helper functions"""
    
    @pytest.mark.asyncio
    async def test_count_project_env_vars_no_auth(self):
        """Test count returns 0 when user not authenticated"""
        count = await count_project_env_vars("nonexistent_user", "TEST", ["test/repo"])
        assert count == 0
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    @patch('httpx.AsyncClient')
    async def test_count_project_env_vars_success(self, mock_client):
        """Test count function with mocked GitHub API response"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "variables": [
                {"name": "AM_TEST_VAR1"},
                {"name": "AM_TEST_VAR2"},
                {"name": "OTHER_VAR"},
                {"name": "AM_OTHER_PROJECT_VAR"}
            ]
        }
        mock_get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__.return_value.get = mock_get
        
        count = await count_project_env_vars("testuser", "TEST", ["test/repo"])
        assert count == 2
    
    @pytest.mark.asyncio
    async def test_count_project_environments_no_auth(self):
        """Test count returns 0 when user not authenticated"""
        count = await count_project_environments("nonexistent_user", ["test/repo"])
        assert count == 0
    
    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    @patch('httpx.AsyncClient')
    async def test_count_project_environments_success(self, mock_client):
        """Test environment count function"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "environments": [
                {"name": "production"},
                {"name": "staging"}
            ]
        }
        mock_get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__.return_value.get = mock_get
        
        count = await count_project_environments("testuser", ["test/repo"])
        assert count == 2

    @pytest.mark.asyncio
    @patch('github_env_vars.user_tokens', {'testuser': 'fake_token'})
    @patch('httpx.AsyncClient')
    async def test_counts_read_every_page(self, mock_client):
        """Counts page until total_count. The variables listing caps a page at 30
        whatever per_page asks, so a short page is not the last one: 45
        variables were counted as 30, and 140 environments as GitHub's default
        page of 30."""
        def page(key, prefix, start, size, total):
            response = Mock(status_code=200)
            response.json.return_value = {
                "total_count": total,
                key: [{"name": f"{prefix}{i}"} for i in range(start, start + size)],
            }
            return response

        client = mock_client.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=[
            page("variables", "AM_TEST_V", 0, 30, 45), page("variables", "AM_TEST_V", 30, 15, 45),
        ])
        assert await count_project_env_vars("testuser", "TEST", ["test/repo"]) == 45
        assert client.get.await_args.args[0].endswith("/actions/variables?per_page=30&page=2")

        client.get = AsyncMock(side_effect=[
            page("environments", "env", 0, 100, 140), page("environments", "env", 100, 40, 140),
        ])
        assert await count_project_environments("testuser", ["test/repo"]) == 140
        assert client.get.await_args.args[0].endswith("/environments?per_page=100&page=2")

    @pytest.mark.asyncio
    async def test_sync_asks_for_the_environment_by_name(self):
        """One page of the listing missed an environment past it, which was then
        PUT again with an empty configuration, clearing its reviewers and
        branch policy. Asking by name needs no paging."""
        client = AsyncMock()
        client.get.side_effect = [Mock(status_code=200), Mock(status_code=404)]

        assert await _find_existing_environments(["o/a", "o/b"], "qa?x", client, {}) == {"o/a": True}
        assert client.get.await_args_list[0].args[0].endswith("/repos/o/a/environments/qa%3Fx")

    @pytest.mark.asyncio
    async def test_sync_leaves_a_repository_github_cannot_answer_for(self):
        """A failed answer is not "missing": creating it would overwrite one that
        is there. That repository is left alone, and the rest still sync."""
        client = AsyncMock()
        client.get.side_effect = [Mock(status_code=200), Mock(status_code=502), Mock(status_code=404)]
        client.put.return_value = Mock(status_code=201)
        repos = ["o/has", "o/unreachable", "o/missing"]

        existing = await _find_existing_environments(repos, "production", client, {})
        results, created = await _create_missing_environments(repos, "production", existing, client, {})

        assert results == {"o/has": "already exists", "o/unreachable": "could not check (502)", "o/missing": "created"}
        assert created == 1
        client.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_encodes_the_environment_name(self):
        """'qa?x' was cut at the '?', so the PUT created 'qa'."""
        client = AsyncMock()
        client.put.return_value = Mock(status_code=201)

        await _create_missing_environments(["o/r"], "qa?x", {}, client, {})

        assert client.put.await_args.args[0].endswith("/environments/qa%3Fx")


if __name__ == "__main__":
    # Run tests using pytest
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# These tests call the route functions directly with a mock request object.
#
# The write routes now prove the caller's identity against the session and
# require project_editor on the project, neither of which a mock request can
# satisfy - and neither of which is what these tests cover: they exercise the
# CRUD behaviour of the routes. Authorization has its own tests, which drive
# the real application rather than calling the functions.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _bypass_write_authorization():
    import contextlib
    from unittest.mock import patch as _patch
    import github_env_vars as _mod

    from models import Project as _Project

    def _authorized_project(db, *_args, **_kwargs):
        # The routes act on the project the check returns, as the real one does;
        # hand back whatever the test's mocked session resolves for Project.
        return db.query(_Project).filter().first()

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            _patch.object(_mod, "require_project_write_access", side_effect=_authorized_project)
        )
        stack.enter_context(_patch.object(_mod, "require_repo_write_access", return_value=None))
        stack.enter_context(
            _patch.object(_mod.auth_module, "assert_session_owns_user", return_value=None)
        )
        yield
