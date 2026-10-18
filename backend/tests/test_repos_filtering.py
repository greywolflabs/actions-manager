"""
Test cases for repository filtering based on account type.
The self-hosted beta sees private repositories on every tier (#2142). The
cloud cases (Free accounts only see public repositories) are in
tests/cloud/test_repos_filtering.py, which reuses the helpers below.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base
from models import Account
from repos import _should_restrict_to_public_repos, get_repos, get_rwx_repos
from fastapi import HTTPException
from fastapi.testclient import TestClient


# Test database setup
TEST_DATABASE_URL = "sqlite:///./test_repos_filtering.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def setup_test_db():
    """Create test database tables"""
    Base.metadata.create_all(bind=engine)


def teardown_test_db():
    """Clean up test database"""
    Base.metadata.drop_all(bind=engine)


def get_test_db():
    """Get test database session"""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_owner_scoped_rwx_search(user, db, items):
    """Call get_rwx_repos with an explicit owner, with GitHub returning ``items``
    from the org search (the owner-scoped path never lists /user/repos)."""
    search_response = Mock(status_code=200)
    search_response.json.return_value = {"items": items}
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get = AsyncMock(return_value=search_response)
    owner_type = Mock(status_code=200)
    owner_type.json.return_value = {"type": "Organization"}
    membership = Mock(status_code=200)
    membership.json.return_value = {"state": "active"}
    with patch('repos.httpx.AsyncClient', return_value=client), \
         patch('repos.github_get', side_effect=[owner_type, membership]), \
         patch('repos.user_tokens', {user: "fake_token"}):
        return asyncio.run(get_rwx_repos(db, Mock(github_user=user), "my-org"))


_OWNER_SCOPED_RWX_ITEMS = [
    {"id": 1, "name": "public-flows", "full_name": "my-org/public-flows", "private": False,
     "owner": {"login": "my-org", "type": "Organization"}},
    {"id": 2, "name": "private-flows", "full_name": "my-org/private-flows", "private": True,
     "owner": {"login": "my-org", "type": "Organization"}},
]


class TestShouldRestrictToPublicRepos:
    """Test the helper function that determines if a user should be restricted"""
    
    def setup_method(self):
        setup_test_db()
        self.db = next(get_test_db())
        # Reset license cache to avoid interference from other tests
        import license
        license.reset_cache()

    def teardown_method(self):
        self.db.close()
        teardown_test_db()


    def test_missing_user_is_restricted(self):
        """Users not in database should be restricted by default for safety"""
        result = _should_restrict_to_public_repos("nonexistent_user", self.db)
        assert result is True


class TestGetReposFiltering:
    """Test the main get_repos endpoint filtering behavior"""

    def setup_method(self):
        setup_test_db()
        self.db = next(get_test_db())
        import license
        license.reset_cache()

    def teardown_method(self):
        self.db.close()
        teardown_test_db()


    @patch('repos.github_get')
    @patch('repos.user_tokens')
    @patch('repos.get_github_api_endpoints')
    def test_repos_pagination_collects_all_pages(self, mock_endpoints, mock_tokens, mock_github_get):
        """get_repos should paginate through all pages of the GitHub API"""
        mock_tokens.__contains__ = Mock(return_value=True)
        mock_tokens.__getitem__ = Mock(return_value="fake_token")
        mock_endpoints.return_value = {"repos_list": "https://api.github.com/user/repos"}

        # Build two pages of 100 repos each + a partial third page
        def _repo(i):
            return {"id": i, "name": f"repo-{i}", "full_name": f"user/repo-{i}", "private": False,
                    "owner": {"login": "paginated_user", "type": "User"}}

        page1 = [_repo(i) for i in range(100)]
        page2 = [_repo(i) for i in range(100, 200)]
        page3 = [_repo(200)]

        responses = iter([page1, page2, page3])

        def mock_get_side_effect(*args, **kwargs):
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = next(responses)
            return resp

        mock_github_get.side_effect = mock_get_side_effect

        # No account = restricted to public repos (which all are)
        account = Account(
            github_user="paginated_user",
            github_email="page@example.com",
            account_type="free",
        )
        self.db.add(account)
        self.db.commit()

        result = get_repos(self.db, Mock(github_user="paginated_user"))

        assert len(result) == 201
        # Ensure github_get was called 3 times (one per page)
        assert mock_github_get.call_count == 3


class TestSelfHostedPrivateRepos:
    """Issue #2142: an unlicensed self-hosted install resolves to the "free"
    tier, but the self-hosted beta allows private repositories on every tier
    (see check_private_visibility_scope), so the pickers must list them."""

    def setup_method(self):
        setup_test_db()
        self.db = next(get_test_db())
        self._patches = [
            patch.dict('os.environ', {'LICENSE_KEY': ''}),
        ]
        for p in self._patches:
            p.start()
        import license
        license.reset_cache()
        self.db.add(Account(
            github_user="sh_user",
            github_email="sh_user@example.com",
            account_type="free"
        ))
        self.db.commit()

    def teardown_method(self):
        for p in reversed(self._patches):
            p.stop()
        import license
        license.reset_cache()
        self.db.close()
        teardown_test_db()

    def test_unlicensed_account_is_not_restricted(self):
        assert _should_restrict_to_public_repos("sh_user", self.db) is False

    @patch('repos.github_get')
    @patch('repos.user_tokens', {"sh_user": "fake_token"})
    @patch('repos.get_github_api_endpoints')
    def test_repo_picker_includes_private_repos(self, mock_endpoints, mock_github_get):
        mock_endpoints.return_value = {"repos_list": "https://api.github.com/user/repos"}
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": 1, "name": "public-repo", "full_name": "sh_user/public-repo", "private": False},
            {"id": 2, "name": "private-repo", "full_name": "sh_user/private-repo", "private": True},
        ]
        mock_github_get.return_value = mock_response

        result = get_repos(self.db, Mock(github_user="sh_user"))

        assert [r["name"] for r in result] == ["public-repo", "private-repo"]

    @patch('repos.github_get')
    @patch('repos.user_tokens', {"sh_user": "fake_token"})
    @patch('repos.get_github_api_endpoints')
    def test_rwx_discovery_includes_private_repos(self, mock_endpoints, mock_github_get):
        mock_endpoints.return_value = {"repos_list": "https://api.github.com/user/repos"}
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": 1, "name": "shared-workflows", "full_name": "sh_user/shared-workflows",
             "private": True, "topics": ["am-rwx"], "owner": {"login": "sh_user", "type": "User"}},
            {"id": 2, "name": "app", "full_name": "sh_user/app",
             "private": True, "topics": [], "owner": {"login": "sh_user", "type": "User"}},
        ]
        mock_github_get.return_value = mock_response

        result = asyncio.run(get_rwx_repos(self.db, Mock(github_user="sh_user"), None))

        assert [r["full_name"] for r in result] == ["sh_user/shared-workflows"]


    def test_owner_scoped_rwx_search_includes_private_repos(self):
        result = _run_owner_scoped_rwx_search("sh_user", self.db, _OWNER_SCOPED_RWX_ITEMS)

        assert [r["full_name"] for r in result] == ["my-org/public-flows", "my-org/private-flows"]


if __name__ == "__main__":
    pytest.main([__file__])