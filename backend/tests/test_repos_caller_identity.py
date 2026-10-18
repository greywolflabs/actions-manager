"""The repos routes act as the signed-in caller, never a name the request carries (#2241).

/api/create-repo and POST /api/rwx-repos used to take the caller from a "user"
field in the body without checking it against the session, so a signed-in user
could have a repository created with someone else's stored GitHub token.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from database import Base, get_db
from models import Account

engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

CALLER = "alice"
OTHER = "bob"
TOKENS = {CALLER: "alice-token", OTHER: "bob-token"}


def _override():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(authenticate_client):
    Base.metadata.create_all(bind=engine)
    db = TestSession()
    for name in (CALLER, OTHER):
        db.add(Account(github_user=name, github_email=f"{name}@example.com", account_type="free"))
    db.commit()
    db.close()
    app.dependency_overrides[get_db] = _override
    original = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestSession
    test_client = TestClient(app)
    authenticate_client(test_client, CALLER, TestSession)
    with patch.dict("auth.user_tokens", TOKENS):
        yield test_client
    app.state.middleware_db_factory = original
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)


def _github_client(post_status=201, post_json=None):
    """An httpx.AsyncClient stand-in recording the token each GitHub call used."""
    github = AsyncMock()
    github.__aenter__.return_value = github
    response = MagicMock(status_code=post_status)
    response.json.return_value = post_json or {"html_url": "https://github.com/x/y", "full_name": f"{CALLER}/flows"}
    github.post = AsyncMock(return_value=response)
    github.put = AsyncMock(return_value=MagicMock(status_code=200))
    return github


def _token_used(github):
    return github.post.call_args.kwargs["headers"]["Authorization"]


@pytest.mark.parametrize("path, body", [
    ("/api/create-repo", {"user": OTHER}),
    ("/api/rwx-repos", {"user": OTHER, "repo_name": "flows"}),
])
def test_a_name_in_the_body_never_borrows_its_token(client, path, body):
    github = _github_client()
    with patch("repos.httpx.AsyncClient", return_value=github):
        resp = client.post(path, json=body)

    assert resp.status_code == 200, resp.text
    assert _token_used(github) == f"token {TOKENS[CALLER]}"
    assert github.post.call_args.args[0] == "https://api.github.com/user/repos"


def test_naming_someone_else_in_the_query_is_refused(client):
    """auth.current_account owns the legacy github_user query parameter."""
    with patch("repos.github_get", side_effect=AssertionError("no GitHub call expected")):
        # The "user" parameter names the caller, as the frontend still sends it.
        resp = client.get("/api/repos", params={"user": CALLER, "github_user": OTHER})

    assert resp.status_code == 403, resp.text
