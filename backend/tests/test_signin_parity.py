"""
Parity of both sign-in flows, self-hosted half (#2138).

Drives /auth/callback (OAuth) and /auth/token (PAT) against a recording
stand-in for GitHub, and records the ordered GitHub requests, the response
status, and the account row the sign-in stored. The table was recorded before
the Marketplace sign-in step moved into backend/cloud/, and must keep passing
unchanged. The cloud half, tests/cloud/test_signin_parity.py, reuses the
drivers below with its own table.
"""

from unittest.mock import Mock, patch

import pytest
import requests
from fastapi.testclient import TestClient

import auth
from github_permissions import PermissionStatus
from models import Account

PAT = "ghp_test1234567890123456789012345678"

PURCHASE_WITH_ACCOUNT = {"plan": {"name": "professional"}, "account": {"login": "octo-org", "type": "Organization"}}
PURCHASE_WITHOUT_ACCOUNT = {"plan": {"name": "enterprise"}}
INSTALLATION = {"account": {"login": "inst-org", "type": "Organization"}}

# name -> (Marketplace status, Marketplace body, installations body)
GITHUB_STATES = {
    "purchase_with_account": (200, [PURCHASE_WITH_ACCOUNT], {"installations": [INSTALLATION]}),
    "purchase_without_account": (200, [PURCHASE_WITHOUT_ACCOUNT], {"installations": [INSTALLATION]}),
    "no_purchases": (200, [], {"installations": [INSTALLATION]}),
    "marketplace_error": (500, {"message": "boom"}, {"installations": [INSTALLATION]}),
    "no_installation": (200, [], {"installations": []}),
}


def _response(status, body):
    response = Mock(status_code=status)
    response.json.return_value = body
    return response


def _fake_github(state, calls):
    marketplace_status, marketplace_body, installations_body = GITHUB_STATES[state]

    def post(url, *args, **kwargs):
        calls.append(("POST", url))
        return _response(200, {"access_token": "gho_oauthtoken"})

    def get(url, *args, **kwargs):
        calls.append(("GET", url))
        if url.endswith("/user/marketplace_purchases"):
            return _response(marketplace_status, marketplace_body)
        if url.endswith("/user/installations"):
            return _response(200, installations_body)
        if url.endswith("/user"):
            return _response(200, {
                "login": "octo", "email": "octo@example.com", "avatar_url": "https://a/octo", "type": "User",
            })
        return _response(404, {})

    return get, post


def _stored_account(db):
    db.expire_all()
    account = db.query(Account).filter(Account.github_user == "octo").first()
    if account is None:
        return None
    return (account.account_type, account.connected_github_account, account.connected_github_account_type)


def sign_in(flow, state, db, mock_responses=False):
    """Run one sign-in; return (status, JSON body, GitHub requests in order, stored account)."""
    calls = []
    get, post = _fake_github(state, calls)
    validation = {
        "status": PermissionStatus.VALID,
        "valid": True,
        "details": {"auth_type": "personal_access_token", "token_type": "classic_pat"},
    }
    from database import get_db
    from main import app
    from tests.conftest import override_get_db
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    # Pinned so the sign-in writes where `db` reads: another module's override
    # would send the account, and its first-member admin row, to a different
    # database, and switch session enforcement on for every later test.
    with patch.dict(app.dependency_overrides, {get_db: override_get_db}), \
            patch.object(requests, "get", get), patch.object(requests, "post", post), \
            patch("auth._enforce_auth_rate_limit"), \
            patch("auth._validate_github_token", return_value=validation), \
            patch("auth._encrypt_saved_token", return_value="encrypted"), \
            patch("auth.USE_MOCK_RESPONSES", mock_responses):
        if flow == "oauth":
            response = client.get("/auth/callback", params={"code": "c0de", "state": auth.oauth_states.create()})
        else:
            response = client.post("/auth/token", json={"token": PAT})
    body = response.json() if response.headers.get("content-type") == "application/json" else None
    return response.status_code, body, calls, _stored_account(db)


CASES = [(flow, state) for flow in ("oauth", "pat") for state in GITHUB_STATES]


@pytest.fixture(autouse=True)
def _self_hosted(monkeypatch):
    monkeypatch.setattr(auth.config, "INSTALLATION_MODE", "self-hosted")
    monkeypatch.setattr(auth.license, "get_installation_tier", lambda: "professional")


@pytest.mark.parametrize("flow, state", CASES)
def test_self_hosted_sign_in_parity(flow, state, test_db):
    assert sign_in(flow, state, test_db) == SELF_HOSTED_EXPECTED[(flow, state)]


@pytest.mark.parametrize("flow", ("oauth", "pat"))
def test_self_hosted_sign_in_with_mock_responses_parity(flow, test_db):
    assert sign_in(flow, "purchase_with_account", test_db, mock_responses=True) == SELF_HOSTED_MOCK_EXPECTED[flow]


SELF_HOSTED_EXPECTED = {('oauth', 'marketplace_error'): (307,
                                  None,
                                  [('POST', 'https://github.com/login/oauth/access_token'),
                                   ('GET', 'https://api.github.com/user'),
                                   ('GET', 'https://api.github.com/user/installations')],
                                  ('professional', 'inst-org', 'Organization')),
 ('oauth', 'no_installation'): (307,
                                None,
                                [('POST', 'https://github.com/login/oauth/access_token'),
                                 ('GET', 'https://api.github.com/user'),
                                 ('GET', 'https://api.github.com/user/installations')],
                                ('professional', 'octo', 'User')),
 ('oauth', 'no_purchases'): (307,
                             None,
                             [('POST', 'https://github.com/login/oauth/access_token'),
                              ('GET', 'https://api.github.com/user'),
                              ('GET', 'https://api.github.com/user/installations')],
                             ('professional', 'inst-org', 'Organization')),
 ('oauth', 'purchase_with_account'): (307,
                                      None,
                                      [('POST', 'https://github.com/login/oauth/access_token'),
                                       ('GET', 'https://api.github.com/user'),
                                       ('GET', 'https://api.github.com/user/installations')],
                                      ('professional', 'inst-org', 'Organization')),
 ('oauth', 'purchase_without_account'): (307,
                                         None,
                                         [('POST', 'https://github.com/login/oauth/access_token'),
                                          ('GET', 'https://api.github.com/user'),
                                          ('GET', 'https://api.github.com/user/installations')],
                                         ('professional', 'inst-org', 'Organization')),
 ('pat', 'marketplace_error'): (200,
                                {'user': 'octo'},
                                [('GET', 'https://api.github.com/user'),
                                 ('GET', 'https://api.github.com/user/installations')],
                                ('professional', 'inst-org', 'Organization')),
 ('pat', 'no_installation'): (200,
                              {'user': 'octo'},
                              [('GET', 'https://api.github.com/user'),
                               ('GET', 'https://api.github.com/user/installations')],
                              ('professional', 'octo', 'User')),
 ('pat', 'no_purchases'): (200,
                           {'user': 'octo'},
                           [('GET', 'https://api.github.com/user'),
                            ('GET', 'https://api.github.com/user/installations')],
                           ('professional', 'inst-org', 'Organization')),
 ('pat', 'purchase_with_account'): (200,
                                    {'user': 'octo'},
                                    [('GET', 'https://api.github.com/user'),
                                     ('GET', 'https://api.github.com/user/installations')],
                                    ('professional', 'inst-org', 'Organization')),
 ('pat', 'purchase_without_account'): (200,
                                       {'user': 'octo'},
                                       [('GET', 'https://api.github.com/user'),
                                        ('GET', 'https://api.github.com/user/installations')],
                                       ('professional', 'inst-org', 'Organization'))}

SELF_HOSTED_MOCK_EXPECTED = {'oauth': (307,
           None,
           [('POST', 'https://github.com/login/oauth/access_token'), ('GET', 'https://api.github.com/user')],
           ('professional', 'octo', 'User')),
 'pat': (200, {'user': 'octo'}, [('GET', 'https://api.github.com/user')], ('professional', 'octo', 'User'))}
