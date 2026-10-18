"""
Authorization tests for the rulesets API.

The caller picks whose GitHub token is used, or whose account a new ruleset is
stored under, so it comes from the session (#2241). Routes that took it as a
`github_user` query parameter still accept one and refuse any other name; a
`github_user` form or body field is ignored.

`DELETE /api/rulesets/{ruleset_id}` takes the caller's name as a `github_user`
query parameter. Scoping the lookup to that account is not access control — the
caller chooses the account — so the route must assert the session owns the name
it was given. Before that assertion existed, any signed-in member could delete
another user's rulesets by naming them.

These use a real server-issued session via `authenticate_client` rather than
patching the resolver, so they exercise the authorization path the app runs.
"""
import pytest
from fastapi.testclient import TestClient

from main import app
from models import Account, Ruleset
from tests.conftest import TestingSessionLocal

OWNER = "ruleset-owner"
OTHER = "someone-else"

client = TestClient(app)


@pytest.fixture
def ruleset_id():
    """One account owning one ruleset, plus a second account to act as the caller."""
    db = TestingSessionLocal()
    try:
        owner = Account(
            github_user=OWNER, github_email=f"{OWNER}@example.com", account_type="free"
        )
        other = Account(
            github_user=OTHER, github_email=f"{OTHER}@example.com", account_type="free"
        )
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)

        ruleset = Ruleset(
            ruleset_name="require-reviews",
            ruleset_json='{"name": "require-reviews", "target": "branch"}',
            description="Protected branch policy",
            user_id=owner.user_id,
        )
        db.add(ruleset)
        db.commit()
        db.refresh(ruleset)
        return ruleset.ruleset_id
    finally:
        db.close()


def _ruleset_count() -> int:
    db = TestingSessionLocal()
    try:
        return db.query(Ruleset).count()
    finally:
        db.close()


def _ruleset_exists(rid: int) -> bool:
    db = TestingSessionLocal()
    try:
        return db.query(Ruleset).filter(Ruleset.ruleset_id == rid).first() is not None
    finally:
        db.close()


class TestDeleteRulesetAuthorization:
    def test_rejects_a_caller_naming_someone_else(self, ruleset_id, authenticate_client):
        """Session belongs to OTHER; the request claims to be OWNER."""
        authenticate_client(client, OTHER, TestingSessionLocal)

        resp = client.delete(f"/api/rulesets/{ruleset_id}", params={"github_user": OWNER})

        assert resp.status_code == 403
        # The point of the test: the ruleset must survive the attempt.
        assert _ruleset_exists(ruleset_id)

    def test_allows_the_owner(self, ruleset_id, authenticate_client):
        authenticate_client(client, OWNER, TestingSessionLocal)

        resp = client.delete(f"/api/rulesets/{ruleset_id}", params={"github_user": OWNER})

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert not _ruleset_exists(ruleset_id)

    def test_allows_the_owner_naming_nobody(self, ruleset_id, authenticate_client):
        authenticate_client(client, OWNER, TestingSessionLocal)

        resp = client.delete(f"/api/rulesets/{ruleset_id}")

        assert resp.status_code == 200, resp.text
        assert not _ruleset_exists(ruleset_id)

    def test_missing_ruleset_returns_404_not_500(self, ruleset_id, authenticate_client):
        """The route's catch-all used to swallow its own 404 and report a 500."""
        authenticate_client(client, OWNER, TestingSessionLocal)

        resp = client.delete("/api/rulesets/999999", params={"github_user": OWNER})

        assert resp.status_code == 404


class TestApplyRulesetAuthorization:
    """`github_user` in the body used to pick whose token creates the ruleset."""

    def test_naming_someone_else_borrows_nothing_of_theirs(self, ruleset_id, authenticate_client):
        authenticate_client(client, OTHER, TestingSessionLocal)

        resp = client.post(
            f"/api/rulesets/{ruleset_id}/apply",
            json={"repo_names": ["victim-org/prod"], "github_user": OWNER},
        )

        # Before the session check this reached GitHub with OWNER's token and
        # created the ruleset on a repository OTHER named. Acting as OTHER, the
        # ruleset is OWNER's alone and does not exist for them.
        assert resp.status_code == 404
        assert _ruleset_exists(ruleset_id)

    def test_missing_ruleset_returns_404_not_500(self, ruleset_id, authenticate_client):
        """The route's catch-all used to report its own 404 as a 500."""
        authenticate_client(client, OWNER, TestingSessionLocal)

        resp = client.post(
            "/api/rulesets/999999/apply",
            json={"repo_names": ["acme/api"], "github_user": OWNER},
        )

        assert resp.status_code == 404


class TestSyncStatusAuthorization:
    """Reads another account's rulesets and spends their rate limit."""

    def test_naming_someone_else_borrows_nothing_of_theirs(self, ruleset_id, authenticate_client):
        authenticate_client(client, OTHER, TestingSessionLocal)

        resp = client.post(
            f"/api/rulesets/{ruleset_id}/sync-status",
            json={"repo_names": ["victim-org/prod"], "github_user": OWNER},
        )

        assert resp.status_code == 404


class TestCreateRulesetAuthorization:
    """With no project_name, nothing but the session decides whose ruleset it is."""

    def _create(self, github_user):
        return client.post(
            "/api/rulesets/create",
            params={"github_user": github_user},
            json={"name": "planted", "ruleset_json": {"name": "planted", "target": "branch"}},
        )

    def _owned_by(self, username):
        db = TestingSessionLocal()
        try:
            account = db.query(Account).filter_by(github_user=username).one()
            return [r.ruleset_name for r in db.query(Ruleset).filter_by(user_id=account.user_id)]
        finally:
            db.close()

    def test_rejects_a_caller_naming_someone_else(self, ruleset_id, authenticate_client):
        authenticate_client(client, OTHER, TestingSessionLocal)

        resp = self._create(OWNER)

        assert resp.status_code == 403
        assert _ruleset_count() == 1

    def test_allows_a_caller_naming_themselves(self, ruleset_id, authenticate_client):
        authenticate_client(client, OWNER, TestingSessionLocal)

        resp = self._create(OWNER)

        assert resp.status_code == 200, resp.text
        assert sorted(self._owned_by(OWNER)) == ["planted", "require-reviews"]

    def test_accepts_the_callers_name_in_another_case(self, ruleset_id, authenticate_client):
        """The session check ignores case, so the account lookup after it must too."""
        authenticate_client(client, OWNER, TestingSessionLocal)

        resp = self._create(OWNER.upper())

        assert resp.status_code == 200, resp.text
        assert sorted(self._owned_by(OWNER)) == ["planted", "require-reviews"]

    def test_naming_nobody_creates_it_for_the_caller(self, ruleset_id, authenticate_client):
        authenticate_client(client, OWNER, TestingSessionLocal)

        resp = client.post(
            "/api/rulesets/create",
            json={"name": "planted", "ruleset_json": {"name": "planted", "target": "branch"}},
        )

        assert resp.status_code == 200, resp.text
        assert sorted(self._owned_by(OWNER)) == ["planted", "require-reviews"]
