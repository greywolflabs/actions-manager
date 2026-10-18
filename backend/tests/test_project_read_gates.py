"""Project reads resolve the project through the caller's access.

get-env-vars, env-vars-count, get-secrets and secrets-count looked the project
up with a bare global name match. A read_only member could read a project they
hold no grant on, and a caller who owns a project with the same name as
someone else's got whichever row matched first - so the screen could show one
project while the save, which already resolves the caller's own, wrote to the
other. Every test signs in as a real session so the
middleware and the route both run.
"""
import contextlib
import sys
import os
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auth
from models import Base, Account, Project, ProjectEnvVar, ProjectMembership, ProjectRepo, Repo, WorkspaceMember
from main import app
from database import get_db

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "read-owner"
MEMBER = "read-member"
READER = "read-reader"
VIEWER = "read-viewer"
PROJECT = "Payments"
REPO = "acme/payments"
OTHER_REPO = "acme/elsewhere"

ROUTES = {
    "get-env-vars": ("/api/get-env-vars", {"repo_name": REPO}),
    "env-vars-count": ("/api/env-vars-count", {"repo_names": REPO}),
    "get-secrets": ("/api/get-secrets", {"repo_name": REPO}),
    "secrets-count": ("/api/secrets-count", {"repo_names": REPO}),
}


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _account(db, name, role):
    account = Account(github_user=name, github_email=f"{name}@e.com", account_type="pro")
    db.add(account)
    db.flush()
    db.add(WorkspaceMember(user_id=account.user_id, workspace_role=role))
    return account


def _project(db, owner, code):
    project = Project(
        project_name=PROJECT, project_code=code, user_id=owner.user_id,
        project_type="standard", use_prefix=True,
    )
    db.add(project)
    repo = db.query(Repo).filter_by(repo_name=REPO).first() or Repo(repo_name=REPO)
    db.add(repo)
    db.flush()
    db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    return project


def _seed(member_owns_same_name=False):
    """The owner's project is created first, so a bare name match returns it."""
    db = TestingSessionLocal()
    try:
        owner = _account(db, OWNER, "member")
        member = _account(db, MEMBER, "member")
        _account(db, READER, "read_only")
        viewer = _account(db, VIEWER, "read_only")
        project = _project(db, owner, "PAY")
        db.add(ProjectMembership(
            user_id=viewer.user_id, project_id=project.project_id, project_role="project_viewer",
        ))
        if member_owns_same_name:
            _project(db, member, "MINE")
        db.commit()
    finally:
        db.close()


@pytest.fixture
def signed_in(authenticate_client):
    app.dependency_overrides[get_db] = override_get_db
    original = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestingSessionLocal

    def _client_as(username):
        client = TestClient(app)
        authenticate_client(client, username, TestingSessionLocal)
        return client

    with mock.patch("mode_validation.validate_startup_configuration"):
        yield _client_as
    app.state.middleware_db_factory = original
    app.dependency_overrides.pop(get_db, None)


@contextlib.contextmanager
def _stub_github(tokens):
    """Stub every GitHub-facing call the four routes make, recording the
    project code each one acted with. With *tokens* None the real credential
    store stays in place."""
    response = mock.MagicMock(status_code=200)
    response.json.return_value = {
        "secrets": [{"name": "AM_PAY_DEPLOY"}, {"name": "AM_MINE_DEPLOY"}],
    }
    client = mock.AsyncMock()
    client.get.return_value = response
    async_client = mock.MagicMock()
    async_client.return_value.__aenter__.return_value = client
    mocks = {
        "async_client": async_client,
        "env_count": mock.AsyncMock(return_value=3),
        "secret_count": mock.AsyncMock(return_value=2),
        "prefix_vars": mock.Mock(return_value=[]),
    }
    with contextlib.ExitStack() as stack:
        if tokens is not None:
            for module in ("github_env_vars", "github_secrets"):
                stack.enter_context(mock.patch(f"{module}.user_tokens", tokens))
        with mock.patch("httpx.AsyncClient", async_client), \
                mock.patch("github_env_vars._fetch_all_github_variables", mock.AsyncMock(return_value=[])), \
                mock.patch("github_env_vars._get_prefix_mode_vars", mocks["prefix_vars"]), \
                mock.patch("github_env_vars.count_project_env_vars", mocks["env_count"]), \
                mock.patch("github_secrets.count_project_secrets", mocks["secret_count"]):
            yield mocks


@pytest.fixture
def github():
    with _stub_github({name: f"{name}-token" for name in (OWNER, MEMBER, READER, VIEWER)}) as mocks:
        yield mocks


def _get(signed_in, username, route):
    path, params = ROUTES[route]
    resp = signed_in(username).get(path, params={"user": username, "project_name": PROJECT, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _code_acted_on(route, mocks, body):
    if route == "get-env-vars":
        return mocks["prefix_vars"].call_args.args[2]
    if route == "env-vars-count":
        return mocks["env_count"].await_args.args[1]
    if route == "get-secrets":
        (secret,) = body["secrets"]
        return secret["secret_key"].split("_")[1]
    return mocks["secret_count"].await_args.args[1]


@pytest.mark.parametrize("route", ROUTES)
def test_read_only_without_a_grant_cannot_read_the_project(signed_in, github, route):
    _seed()

    body = _get(signed_in, READER, route)

    assert body.get("status") == 404, body
    github["async_client"].assert_not_called()
    github["env_count"].assert_not_awaited()
    github["secret_count"].assert_not_awaited()


@pytest.mark.parametrize("route", ROUTES)
def test_read_only_with_a_viewer_grant_still_reads_the_project(signed_in, github, route):
    _seed()

    body = _get(signed_in, VIEWER, route)

    assert _code_acted_on(route, github, body) == "PAY"


@pytest.mark.parametrize("route", ROUTES)
def test_a_same_named_project_resolves_to_the_callers_own(signed_in, github, route):
    _seed(member_owns_same_name=True)

    body = _get(signed_in, MEMBER, route)

    assert _code_acted_on(route, github, body) == "MINE"


# Every route takes the caller from the session and ignores `user` (#2241),
# so the reader acts as themselves and cannot see the project.
@pytest.mark.parametrize("route", ROUTES)
def test_naming_nobody_reads_as_the_session_caller(signed_in, github, route):
    _seed()
    path, params = ROUTES[route]

    resp = signed_in(VIEWER).get(path, params={"project_name": PROJECT, **params})

    assert resp.status_code == 200, resp.text
    assert _code_acted_on(route, github, resp.json()) == "PAY"


@pytest.mark.parametrize("route", ROUTES)
def test_naming_another_user_does_not_borrow_their_access(signed_in, route):
    """The `user` parameter used to pick whose access and token the route used.
    It is ignored now (#2241), so a read_only caller naming the owner still acts
    as themselves and gets nothing."""
    _seed()
    auth.user_tokens[OWNER] = "owner-token"
    auth.user_tokens[READER] = "reader-token"
    try:
        with _stub_github(None) as mocks:
            path, params = ROUTES[route]
            resp = signed_in(READER).get(
                path, params={"user": OWNER, "project_name": PROJECT, **params},
            )
    finally:
        auth.user_tokens.pop(OWNER, None)
        auth.user_tokens.pop(READER, None)

    assert "not found" in resp.text
    mocks["async_client"].assert_not_called()
    mocks["env_count"].assert_not_awaited()
    mocks["secret_count"].assert_not_awaited()


def test_env_var_reads_leave_out_a_repository_outside_the_project(signed_in, github):
    """get-env-vars answers with an error body, as for an unsaved project, so
    the panel lists nothing for the repository; the count leaves it out."""
    _seed()

    listed = signed_in(OWNER).get("/api/get-env-vars", params={"project_name": PROJECT, "repo_name": OTHER_REPO})
    signed_in(OWNER).get("/api/env-vars-count", params={"project_name": PROJECT, "repo_names": f"{REPO},{OTHER_REPO}"})

    assert "not part of project" in listed.json()["error"]
    github["async_client"].assert_not_called()
    assert github["env_count"].await_args.args[2] == [REPO]


def test_a_no_prefix_project_is_counted_by_the_names_it_stores(signed_in):
    """Its variables carry no prefix, so matching AM_<CODE>_ on GitHub found none
    and the limit badge read 0."""
    _seed()
    db = TestingSessionLocal()
    try:
        project = db.query(Project).filter_by(project_code="PAY").one()
        project.use_prefix = False
        db.add_all([ProjectEnvVar(project_id=project.project_id, env_var_name=name) for name in ("API_URL", "DB_HOST")])
        db.commit()
    finally:
        db.close()

    with mock.patch.dict("github_env_vars.user_tokens", {OWNER: "owner-token"}), \
            mock.patch("github_env_vars.httpx.AsyncClient") as client:
        resp = signed_in(OWNER).get("/api/env-vars-count", params={"project_name": PROJECT, "repo_names": REPO})

    assert resp.json() == {"count": 2}
    client.assert_not_called()


@pytest.mark.parametrize("username, repo, allowed", [
    (READER, REPO, False),
    (VIEWER, REPO, True),
    (MEMBER, "acme/not-saved-yet", True),
])
def test_environment_reads_follow_the_callers_projects(signed_in, github, username, repo, allowed):
    """A read_only member sees only the projects they were granted. A full
    member may start a project from any repository, so one ticked before the
    project is saved still shows its environments."""
    _seed()

    listed = signed_in(username).get("/api/get-environments", params={"repo_name": repo})
    counted = signed_in(username).get("/api/environments-count", params={"repo_names": repo})

    if allowed:
        assert listed.json() == {"environments": []}
        assert counted.json() == {"count": 0}
    else:
        assert listed.status_code == counted.status_code == 403
        github["async_client"].assert_not_called()
