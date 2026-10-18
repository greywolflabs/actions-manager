"""Project names match literally, ignoring case - never as LIKE patterns.

The project lookups matched names with ilike, where `_` and `%` are
wildcards, so a request for "pay_ments" resolved "payXments" and "%" resolved
any project at all. Saving a project could also update a different project
whose name the new one happened to match. The secret write routes then looked
the project up a second time, owner-only, so an editor granted on someone
else's project was refused after passing the write check, and the tier limit
counted whichever same-named project came first.
"""
import sys
import os
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import projects
import workflows
from models import (
    Base, Account, Project, ProjectMembership, ProjectSecret, WorkspaceMember,
)
from main import app
from database import get_db

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

ADMIN = "name-admin"
OWNER = "name-owner"
GRANTED = "name-granted"
MEMBER = "name-member"
CALLERS = (ADMIN, OWNER, GRANTED, MEMBER)


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


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _account(db, name, role):
    account = Account(github_user=name, github_email=f"{name}@e.com", account_type="pro")
    db.add(account)
    db.flush()
    db.add(WorkspaceMember(user_id=account.user_id, workspace_role=role))
    return account


def _project(db, owner, name, code, use_prefix=True):
    project = Project(
        project_name=name, project_code=code, user_id=owner.user_id,
        project_type="standard", use_prefix=use_prefix,
    )
    db.add(project)
    db.flush()
    return project


def _grant(db, account, project, role):
    db.add(ProjectMembership(user_id=account.user_id, project_id=project.project_id, project_role=role))


def _seed_lookup(db):
    """One project, "payXments", reachable by every caller through a
    different branch of the lookup: admin, owner, explicit grant, membership."""
    _account(db, ADMIN, "admin")
    owner = _account(db, OWNER, "member")
    granted = _account(db, GRANTED, "read_only")
    _account(db, MEMBER, "member")
    project = _project(db, owner, "payXments", "PAYX")
    _grant(db, granted, project, "project_viewer")
    db.commit()


def _member(db, username):
    account = db.query(Account).filter_by(github_user=username).one()
    return db.query(WorkspaceMember).filter_by(user_id=account.user_id).one()


def _find_both(db, caller, name):
    return (
        workflows._find_project_by_name(db, caller, name),
        projects._find_project_by_name(db, name, _member(db, caller), caller),
    )


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("pattern", ["pay_ments", "%", "pay%"])
def test_like_wildcards_match_nothing(db, caller, pattern):
    _seed_lookup(db)

    assert _find_both(db, caller, pattern) == (None, None)


@pytest.mark.parametrize("caller", CALLERS)
def test_a_name_still_matches_in_any_case(db, caller):
    _seed_lookup(db)

    found = _find_both(db, caller, "  PAYXMENTS ")

    assert [p.project_code for p in found] == ["PAYX", "PAYX"]


def test_a_non_ascii_name_matches_itself(db):
    """SQLite's lower() folds ASCII only, so lowering one side in Python and
    the other in SQL would miss "Ünit"; both sides are lowered by the database."""
    owner = _account(db, OWNER, "member")
    _project(db, owner, "Ünit", "UNIT")
    db.commit()

    found = projects._find_project_by_name(db, "Ünit", _member(db, OWNER), OWNER)

    assert found is not None
    assert found.project_code == "UNIT"


@pytest.mark.parametrize("name", ["my_app", "my%"])
def test_creating_a_project_ignores_one_its_name_only_matches_as_a_pattern(db, name):
    """The create refuses a name its owner already uses (#2338): a name that
    only matches as a LIKE pattern is not one, so it is neither refused nor
    allowed to touch the other project."""
    owner = _account(db, OWNER, "member")
    other = _project(db, owner, "myxapp", "MYX", use_prefix=True)
    db.commit()
    schema = projects.ProjectSchema(project_name=name, selected_repos=[], workflows=[], use_prefix=False)

    projects._validate_create_request(db, schema, OWNER)
    saved = projects._create_project_row(db, owner.user_id, schema)

    assert saved.project_id != other.project_id
    assert saved.project_name == name
    db.refresh(other)
    assert other.use_prefix is True


# --- routes -------------------------------------------------------------------

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


def test_the_project_page_does_not_open_a_pattern_match(signed_in, db):
    _seed_lookup(db)
    client = signed_in(OWNER)

    assert client.get("/api/projects/pay_ments", params={"github_user": OWNER}).status_code == 404
    resp = client.get("/api/projects/PAYXMENTS", params={"github_user": OWNER})
    assert resp.status_code == 200, resp.text


EDITOR = "name-editor"
SECRET = [{"secret_key": "deploy", "secret_value": "s3cret"}]


def _seed_secrets(db, editor_owns_same_name=False):
    """OWNER's "Payments" is older, so a bare name match returns it first."""
    owner = _account(db, OWNER, "member")
    editor = _account(db, EDITOR, "member")
    project = _project(db, owner, "Payments", "PAY", use_prefix=False)
    if editor_owns_same_name:
        _project(db, editor, "Payments", "MINE", use_prefix=False)
    else:
        _grant(db, editor, project, "project_editor")
    db.commit()
    return project.project_id


@pytest.fixture
def secrets_github():
    client = mock.AsyncMock()
    client.delete.return_value = mock.MagicMock(status_code=204)
    async_client = mock.MagicMock()
    async_client.return_value.__aenter__.return_value = client
    count = mock.AsyncMock(return_value=0)
    with mock.patch("github_secrets.user_tokens", {EDITOR: "editor-token"}), \
            mock.patch("httpx.AsyncClient", async_client), \
            mock.patch("github_secrets.count_project_secrets", count):
        yield count


def _stored_secret_names(project_id):
    db = TestingSessionLocal()
    try:
        return {s.secret_name for s in db.query(ProjectSecret).filter_by(project_id=project_id)}
    finally:
        db.close()


def test_an_editor_creates_secrets_on_the_project_they_were_granted(signed_in, db, secrets_github):
    project_id = _seed_secrets(db)

    resp = signed_in(EDITOR).post("/api/create-secrets", json={
        "user": EDITOR, "project_name": "Payments", "repo_names": [], "secrets": SECRET,
    })

    assert resp.status_code == 200, resp.text
    assert "error" not in resp.json(), resp.json()
    assert _stored_secret_names(project_id) == {"DEPLOY"}


def test_an_editor_deletes_secrets_on_the_project_they_were_granted(signed_in, db, secrets_github):
    _seed_secrets(db)

    resp = signed_in(EDITOR).request("DELETE", "/api/delete-secrets", json={
        "user": EDITOR, "project_name": "Payments", "repo_names": ["acme/payments"],
        "secret_name": "deploy",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json().get("results") == {"acme/payments/secret/AM_PAY_DEPLOY": 204}


def test_the_secret_limit_counts_the_project_being_written(signed_in, db, secrets_github):
    _seed_secrets(db, editor_owns_same_name=True)

    resp = signed_in(EDITOR).post("/api/create-secrets", json={
        "user": EDITOR, "project_name": "Payments", "repo_names": [], "secrets": SECRET,
    })

    assert resp.status_code == 200, resp.text
    assert secrets_github.await_args.args[1] == "MINE"
