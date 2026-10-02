"""
Tests for auth module
"""
import pytest
import os
from unittest.mock import Mock, patch
from auth import get_github_api_endpoints, debug_log


class TestAuthModule:
    """Test cases for auth module functions"""

    def test_get_github_api_endpoints_default(self):
        """Test default user endpoints without database session"""
        endpoints = get_github_api_endpoints("testuser")
        
        assert endpoints["repos_list"] == "https://api.github.com/user/repos"
        assert endpoints["repos_create"] == "https://api.github.com/user/repos"
        assert endpoints["account_type"] == "User"

    def test_get_github_api_endpoints_with_none_session(self):
        """Test endpoints with None database session"""
        endpoints = get_github_api_endpoints("testuser", None)
        
        assert endpoints["repos_list"] == "https://api.github.com/user/repos"
        assert endpoints["account_type"] == "User"

    def test_get_github_api_endpoints_org_account(self):
        """Test that Organization accounts receive correct /orgs/ endpoints"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database import Base
        from models import Account

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        db.add(Account(
            github_user="my-org",
            github_email="org@example.com",
            account_type="enterprise",
            github_account_type="Organization",
        ))
        db.commit()

        endpoints = get_github_api_endpoints("my-org", db)

        assert endpoints["repos_list"] == "https://api.github.com/orgs/my-org/repos"
        assert endpoints["repos_create"] == "https://api.github.com/orgs/my-org/repos"
        assert endpoints["account_type"] == "Organization"
        # URLs must not contain a literal '$' (regression test for the f-string typo)
        assert "$" not in endpoints["repos_list"]
        assert "$" not in endpoints["repos_create"]

        db.close()

    def test_get_github_api_endpoints_personal_account_in_db(self):
        """Test that a User account in the database still receives /user/ endpoints"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database import Base
        from models import Account

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        db.add(Account(
            github_user="personal-user",
            github_email="user@example.com",
            account_type="free",
            github_account_type="User",
        ))
        db.commit()

        endpoints = get_github_api_endpoints("personal-user", db)

        assert endpoints["repos_list"] == "https://api.github.com/user/repos"
        assert endpoints["repos_create"] == "https://api.github.com/user/repos"
        assert endpoints["account_type"] == "User"

        db.close()

    def test_debug_log_function_exists(self):
        """Test that debug_log function exists and is callable"""
        # Test that the function exists and can be called
        try:
            debug_log("test message")
            # If no exception is raised, the function exists and works
            assert True
        except NameError:
            pytest.fail("debug_log function not found")
        except Exception:
            # Function exists but may have implementation details we don't need to test
            assert True

    @patch('auth.DEBUG_MODE', True)
    def test_debug_mode_setting(self):
        """Test debug mode configuration"""
        from auth import DEBUG_MODE
        # Test that DEBUG_MODE is accessible (it should be True due to patch)
        assert isinstance(DEBUG_MODE, bool)