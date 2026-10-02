"""
Shared test fixtures and configuration for all admin tests.

This file ensures that all test files use the same database engine and session,
preventing issues with multiple database instances when running tests together.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Import app and dependencies
import sys
import os

# Tests are development environments by definition. Setting this before
# importing ``main`` ensures the startup validator applies the permissive
# self-hosted-development rules instead of the strict production rules.
# (See backend/mode_validation.py for the production/development split.)
os.environ.setdefault("ENVIRONMENT", "development")

# Core tests run the way core ships: self-hosted. tests/cloud/conftest.py
# switches its own tests to cloud mode. Self-hosted keeps its SQLite database
# in /app/data, which only the image has, so the tests keep theirs in
# ./test.db as before. setdefault means an explicit env var still wins.
os.environ.setdefault("INSTALLATION_MODE", "self-hosted")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pytest-xdist workers are separate processes sharing this directory, and the
# tests name their SQLite files relatively - ./test.db, ./test_project_limits.db
# and so on, several of them used by more than one test file. Two workers on
# one file collide, so each worker gets its own copy of every such file. This
# has to run before main or any test module imports create_engine.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
if _XDIST_WORKER:
    import sqlalchemy

    _create_engine = sqlalchemy.create_engine

    def _worker_create_engine(url, *args, **kwargs):
        if isinstance(url, str) and url.startswith("sqlite:///./") and url.endswith(".db"):
            url = f"{url[:-3]}_{_XDIST_WORKER}.db"
        return _create_engine(url, *args, **kwargs)

    sqlalchemy.create_engine = _worker_create_engine

from main import app
from database import Base, get_db
from auth import get_db as auth_get_db

# Create shared test database (in-memory for test isolation)
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    """Override database dependency for testing"""
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


# Override the database dependency once for all tests
app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[auth_get_db] = override_get_db


@pytest.fixture(scope="session", autouse=True)
def setup_database():
    """Create test database tables once for all tests"""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def shared_db_overrides():
    """Re-assert the shared get_db overrides before every test.

    `auth.get_db`, `codeowners.get_db` and `database.get_db` are all the same
    function object, so a module that installs "its own" override and then
    pops it in teardown deletes the process-wide override installed above.
    Every auth-dependent test that ran afterwards resolved against the real
    app database instead and got a 401 — which only stayed hidden because the
    modules that do this sort alphabetically after the tests they broke.

    Re-asserting per test is cheaper than trusting each module's teardown, and
    it keeps working when the next module does the same thing.

    setdefault, not assignment: plenty of modules deliberately point get_db at
    their own database at import time, and those must keep winning. This only
    fills the override back in when something has removed it entirely.
    """
    app.dependency_overrides.setdefault(get_db, override_get_db)
    app.dependency_overrides.setdefault(auth_get_db, override_get_db)
    yield


@pytest.fixture(autouse=True)
def cleanup_database():
    """Clean up database after each test"""
    yield
    # Clean up all data after each test
    db = TestingSessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            try:
                db.execute(table.delete())
            except Exception:
                # Table may not exist in this database instance
                pass
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def entitlements():
    """Core tests run against core's self-hosted tier rules.

    tests/cloud/conftest.py installs the cloud rules process-wide when pytest
    collects it, and overrides this fixture for the cloud tests. Pinning the
    core rules here gives a core test the same answers in this repository as
    in the public export, which has no cloud rules at all.
    """
    import tier_service
    previous = tier_service.use(tier_service.SelfHostedEntitlements())
    yield
    tier_service.use(previous)


@pytest.fixture(autouse=True)
def sign_in_rules():
    """Core tests sign in with core's rules, like the entitlements fixture."""
    import auth
    previous = auth.use_sign_in(auth.SignIn())
    yield
    auth.use_sign_in(previous)


@pytest.fixture(autouse=True)
def startup_rules(monkeypatch):
    """Core tests validate startup with core's rules only, like the fixtures above."""
    import mode_validation
    monkeypatch.setattr(mode_validation, "_VALIDATORS", {"self-hosted": mode_validation._validate_self_hosted})


@pytest.fixture(autouse=True)
def license_tier_cache():
    """Every test starts with no cached installation tier.

    license.get_installation_tier() caches its answer for the life of the
    process, and the licensing tests leave "enterprise" in it. A serial run
    only passed because a later file happened to reset it; under pytest-xdist
    a worker can run the licensing tests right before a test expecting "free".
    """
    import license
    license.reset_cache()
    yield
    license.reset_cache()


@pytest.fixture
def test_db():
    """Get test database session"""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def authenticate_client():
    """Give a TestClient a real server-issued session for a username.

    Reads are authenticated now: WriteProtectionMiddleware no longer exempts
    safe methods, and routes that take the caller as a ``github_user`` query
    parameter check it against the session. A TestClient that only passes
    ``github_user=<name>`` therefore gets 401 without this.

    Usage::

        def my_fixture(authenticate_client):
            authenticate_client(client, "alice", TestingSessionLocal)

    The Authorization header is removed automatically at teardown.
    """
    from auth import create_auth_session

    patched = []

    def _authenticate(client, username, session_factory):
        db = session_factory()
        try:
            token = create_auth_session(username, db)
        finally:
            db.close()
        client.headers["Authorization"] = f"Bearer {token}"
        patched.append(client)
        return token

    yield _authenticate

    for client in patched:
        client.headers.pop("Authorization", None)
