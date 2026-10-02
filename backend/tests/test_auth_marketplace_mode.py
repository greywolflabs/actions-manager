"""
Marketplace purchases are cloud billing data. Self-hosted sign-in must not
call GET /user/marketplace_purchases (issue #2123). The cloud half, which
still takes the tier and the connected account from it, is in
tests/cloud/test_auth_marketplace_mode.py.
"""

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

import auth
from github_permissions import PermissionStatus
from main import app
from models import Account, AuthSession, WorkspaceMember


client = TestClient(app, base_url="https://testserver")

USERNAME = "marketplace-mode-user"
MARKETPLACE_URL = "https://api.github.com/user/marketplace_purchases"

GITHUB_RESPONSES = {
    "https://api.github.com/user": {
        "login": USERNAME,
        "type": "User",
        "email": "mm@example.com",
        "avatar_url": "https://example.com/avatar.png",
    },
    MARKETPLACE_URL: [
        {"plan": {"name": "professional"}, "account": {"login": "acme-billing", "type": "Organization"}},
    ],
    "https://api.github.com/user/installations": {
        "installations": [{"account": {"login": "acme-app", "type": "Organization"}}],
    },
}


def _fake_github_get(url, **_kwargs):
    response = Mock()
    response.status_code = 200
    response.json.return_value = GITHUB_RESPONSES[url]
    return response


@pytest.fixture(autouse=True)
def isolation(test_db):
    auth.auth_rate_limiter._hits.clear()
    auth.oauth_states._states.clear()
    auth.user_tokens.clear()
    yield
    auth.user_tokens.clear()
    user = test_db.query(Account).filter(Account.github_user == USERNAME).first()
    if user:
        test_db.query(AuthSession).filter(AuthSession.github_user == USERNAME).delete()
        test_db.query(WorkspaceMember).filter(WorkspaceMember.user_id == user.user_id).delete()
        test_db.delete(user)
        test_db.commit()


@pytest.fixture
def github_get():
    with patch("auth.USE_MOCK_RESPONSES", False), \
         patch("auth.license.get_installation_tier", return_value="enterprise"), \
         patch("auth._encrypt_saved_token", return_value="encrypted-token"), \
         patch("auth.requests.get", side_effect=_fake_github_get) as mock_get:
        yield mock_get


def _oauth_sign_in():
    state = auth.oauth_states.create()
    with patch("auth._exchange_code_for_token", return_value="gho_oauth_token"):
        response = client.get(
            "/auth/callback", params={"code": "code", "state": state}, follow_redirects=False,
        )
    assert response.status_code == 307


def _pat_sign_in():
    validation = {
        "status": PermissionStatus.VALID,
        "valid": True,
        "details": {"auth_type": "personal_access_token", "token_type": "classic_pat"},
    }
    with patch("auth._validate_github_token", return_value=validation):
        response = client.post("/auth/token", json={"token": "ghp_1234567890"})
    assert response.status_code == 200


def _requested_urls(mock_get):
    return [call.args[0] for call in mock_get.call_args_list]


def _signed_in_user(test_db):
    test_db.expire_all()
    return test_db.query(Account).filter(Account.github_user == USERNAME).one()


@pytest.mark.parametrize("sign_in", [_oauth_sign_in, _pat_sign_in], ids=["oauth", "pat"])
def test_self_hosted_sign_in_skips_marketplace(sign_in, github_get, test_db):
    with patch("auth.config.INSTALLATION_MODE", "self-hosted"):
        sign_in()

    assert MARKETPLACE_URL not in _requested_urls(github_get)
    user = _signed_in_user(test_db)
    assert user.account_type == "enterprise"
    assert user.connected_github_account == "acme-app"
    assert user.connected_github_account_type == "Organization"
