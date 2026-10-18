"""The pull-request and preflight routes act as the session's caller (#2241).

/api/create-pull-requests took a ``github_user`` query parameter so internal
callers could name the user to act for, and any HTTP client could set it too.
The merge, close and preflight routes fell back to a ``github_user`` in the body
whenever X-GitHub-User was absent, which it is on an install with no workspace
members (the middleware only pins it while membership is enforced). That is the
setup here.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from main import app
from database import get_db
from models import Base, Account, Project, ProjectPullRequest, ProjectRepo, Repo

engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "alice"
OTHER = "bob"
PROJECT = "payments"


def _override():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def signed_in(authenticate_client):
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    owner = Account(github_user=OWNER, github_email="a@example.com", account_type="free")
    other = Account(github_user=OTHER, github_email="b@example.com", account_type="free")
    db.add_all([owner, other])
    db.commit()
    project = Project(project_name=PROJECT, project_code="PAY", user_id=owner.user_id,
                      branch_option="default", use_prefix=True, pr_state="open")
    db.add(project)
    db.commit()
    repo = Repo(repo_name=f"{OWNER}/repo1")
    db.add(repo)
    db.commit()
    db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.add(ProjectPullRequest(
        project_id=project.project_id, repo_name=f"{OWNER}/repo1", pr_number=7,
        pr_url=f"https://github.com/{OWNER}/repo1/pull/7", pr_state="open",
        branch_name="actions-manager/pay-main", target_branch="main",
    ))
    db.commit()
    db.close()

    app.dependency_overrides[get_db] = _override
    original = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestingSessionLocal

    def _as(user):
        client = TestClient(app)
        authenticate_client(client, user, TestingSessionLocal)
        return client

    tokens = {OWNER: "alice-token", OTHER: "bob-token"}
    with patch.dict("auth.user_tokens", tokens):
        yield _as
    app.state.middleware_db_factory = original
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)


def test_a_github_user_query_no_longer_picks_who_opens_the_campaign(signed_in):
    with patch("workflows._ensure_preflight_allows_campaign",
               side_effect=AssertionError("no campaign work expected")):
        resp = signed_in(OWNER).post(
            "/api/create-pull-requests",
            params={"github_user": OTHER},
            json={"project_name": PROJECT},
        )

    assert resp.status_code == 403, resp.text


def test_naming_another_user_in_the_body_does_not_act_as_them(signed_in):
    """bob names alice: the merge is looked up among bob's own projects and
    alice's token is never used."""
    with patch("workflows.github_put", side_effect=AssertionError("no merge expected")):
        resp = signed_in(OTHER).put(
            "/api/merge-pull-request",
            json={"github_user": OWNER, "project_name": PROJECT,
                  "repo_name": f"{OWNER}/repo1", "pr_number": 7},
        )

    assert resp.status_code == 404, resp.text
