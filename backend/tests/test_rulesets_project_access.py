"""Acting on a ruleset is decided by its project, not by who added it (#2236).

Sync status, apply and delete loaded the ruleset filtered to the caller's own
`Ruleset.user_id`, while the panel lists every ruleset attached to the project.
So an admin saw the owner's rulesets and got 404 acting on them, the owner got
404 on one an admin uploaded, and once the uploader lost edit access nobody
could remove it at all.

The rule now: anyone who can see a project a ruleset is attached to can check
it; anyone who can edit one can apply and remove it. A ruleset attached to no
project stays with the account that added it.
"""
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app
from models import (
    Account, Project, ProjectMembership, ProjectRepo, ProjectRuleset, Repo, Ruleset,
    WorkspaceMember,
)
from tests.conftest import TestingSessionLocal

OWNER = "rs-access-owner"
ADMIN = "rs-access-admin"
EDITOR = "rs-access-editor"
VIEWER = "rs-access-viewer"
OUTSIDER = "rs-access-outsider"
REPO = "rs-access-org/api"
GITHUB_RULESET_ID = 77

client = TestClient(app)


@pytest.fixture(autouse=True)
def write_protection(monkeypatch):
    """Let WriteProtectionMiddleware see this test's workspace, as it would in production.

    It reads its own session factory, which otherwise points at a database with
    no workspace members, and with none it enforces nothing.
    """
    monkeypatch.setattr(app.state, "middleware_db_factory", TestingSessionLocal)


def _ruleset(db, name, creator):
    ruleset = Ruleset(
        ruleset_name=name,
        ruleset_json=json.dumps({"name": name, "target": "branch"}),
        user_id=creator.user_id,
    )
    db.add(ruleset)
    db.flush()
    return ruleset


@pytest.fixture
def seeded():
    """OWNER's project with one repo and rulesets added by OWNER, ADMIN and EDITOR.

    VIEWER is a full member granted project_viewer; OUTSIDER is a read-only
    member with no grant, so the project is invisible to them. `loose` belongs
    to OWNER and is attached to nothing.
    """
    db = TestingSessionLocal()
    try:
        accounts = {
            name: Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
            for name in (OWNER, ADMIN, EDITOR, VIEWER, OUTSIDER)
        }
        db.add_all(accounts.values())
        db.flush()
        for name, role in ((OWNER, "member"), (ADMIN, "admin"), (EDITOR, "member"),
                           (VIEWER, "member"), (OUTSIDER, "read_only")):
            db.add(WorkspaceMember(user_id=accounts[name].user_id, workspace_role=role))

        project = Project(
            project_name="AccessProject", project_code="RSAC",
            user_id=accounts[OWNER].user_id, project_type="standard",
        )
        repo = Repo(repo_name=REPO)
        db.add_all([project, repo])
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.add(ProjectMembership(
            user_id=accounts[EDITOR].user_id, project_id=project.project_id,
            project_role="project_editor",
        ))
        db.add(ProjectMembership(
            user_id=accounts[VIEWER].user_id, project_id=project.project_id,
            project_role="project_viewer",
        ))

        ids = {}
        for key, creator in (("owner", OWNER), ("admin", ADMIN), ("editor", EDITOR)):
            ruleset = _ruleset(db, f"{key}-policy", accounts[creator])
            db.add(ProjectRuleset(project_id=project.project_id, ruleset_id=ruleset.ruleset_id))
            ids[key] = ruleset.ruleset_id
        ids["loose"] = _ruleset(db, "loose-policy", accounts[OWNER]).ruleset_id
        db.commit()
        return ids
    finally:
        db.close()


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeGitHub:
    """Answers ruleset list/delete calls and records the token each one used."""

    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs["headers"]["Authorization"]))
        names = ("owner-policy", "admin-policy", "editor-policy", "loose-policy")
        return FakeResponse(200, [{"id": GITHUB_RULESET_ID, "name": n} for n in names])

    async def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs["headers"]["Authorization"]))
        return FakeResponse(204)


@pytest.fixture
def github(monkeypatch):
    import rulesets as rulesets_module

    for name in (OWNER, ADMIN, EDITOR, VIEWER, OUTSIDER):
        rulesets_module.user_tokens[name] = f"tok-{name}"
    fake = FakeGitHub()
    monkeypatch.setattr(rulesets_module.httpx, "AsyncClient", lambda *a, **k: fake)
    apply_one = AsyncMock(return_value=True)
    monkeypatch.setattr(rulesets_module, "apply_ruleset_to_repo", apply_one)
    fake.apply_one = apply_one
    yield fake
    for name in (OWNER, ADMIN, EDITOR, VIEWER, OUTSIDER):
        rulesets_module.user_tokens.pop(name, None)


def _check(caller, ruleset_id):
    return client.post(
        f"/api/rulesets/{ruleset_id}/sync-status",
        json={"repo_names": [REPO], "github_user": caller},
    )


def _apply(caller, ruleset_id):
    return client.post(
        f"/api/rulesets/{ruleset_id}/apply",
        json={"repo_names": [REPO], "github_user": caller},
    )


def _delete(caller, ruleset_id, scope="project"):
    return client.delete(
        f"/api/rulesets/{ruleset_id}", params={"github_user": caller, "scope": scope},
    )


def _ruleset_exists(ruleset_id):
    db = TestingSessionLocal()
    try:
        return db.get(Ruleset, ruleset_id) is not None
    finally:
        db.close()


CROSS_ACCESS = [(OWNER, "admin"), (ADMIN, "owner"), (OWNER, "editor"), (EDITOR, "owner")]


@pytest.mark.parametrize("caller, key", CROSS_ACCESS)
def test_anyone_who_can_edit_the_project_can_check_it(seeded, github, caller, key, authenticate_client):
    authenticate_client(client, caller, TestingSessionLocal)

    resp = _check(caller, seeded[key])

    assert resp.status_code == 200, resp.text
    assert resp.json()["is_synced"] is True
    # Read with the caller's own token, never the creator's.
    assert {token for _, _, token in github.calls} == {f"token tok-{caller}"}


@pytest.mark.parametrize("caller, key", CROSS_ACCESS)
def test_anyone_who_can_edit_the_project_can_apply_it(seeded, github, caller, key, authenticate_client):
    authenticate_client(client, caller, TestingSessionLocal)

    resp = _apply(caller, seeded[key])

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied_count"] == 1
    assert github.apply_one.await_args.args[0] == caller


@pytest.mark.parametrize("named", [OWNER, None])
def test_checking_and_applying_act_as_the_session_caller(seeded, github, named, authenticate_client):
    """The caller comes from the session (#2241). A name in the body is ignored:
    naming the owner borrows nothing of theirs, and naming nobody still works."""
    authenticate_client(client, EDITOR, TestingSessionLocal)
    body = {"repo_names": [REPO], **({"github_user": named} if named else {})}

    assert client.post(f"/api/rulesets/{seeded['owner']}/sync-status", json=body).status_code == 200
    assert client.post(f"/api/rulesets/{seeded['owner']}/apply", json=body).status_code == 200

    assert {token for _, _, token in github.calls} == {f"token tok-{EDITOR}"}
    assert github.apply_one.await_args.args[0] == EDITOR


@pytest.mark.parametrize("caller, key", CROSS_ACCESS)
def test_anyone_who_can_edit_the_project_can_delete_it(seeded, github, caller, key, authenticate_client):
    authenticate_client(client, caller, TestingSessionLocal)

    resp = _delete(caller, seeded[key])

    assert resp.status_code == 200, resp.text
    assert not _ruleset_exists(seeded[key])


def test_a_viewer_can_check_but_not_apply_or_delete(seeded, github, authenticate_client):
    authenticate_client(client, VIEWER, TestingSessionLocal)

    assert _check(VIEWER, seeded["owner"]).status_code == 200
    assert _apply(VIEWER, seeded["owner"]).status_code == 403
    assert _delete(VIEWER, seeded["owner"], scope="project_and_github").status_code == 403

    assert _ruleset_exists(seeded["owner"])
    github.apply_one.assert_not_awaited()
    assert [c for c in github.calls if c[0] == "DELETE"] == []


@pytest.mark.parametrize("send", [_check, _apply, _delete])
def test_a_read_only_member_without_a_grant_is_refused(seeded, github, send, authenticate_client):
    authenticate_client(client, OUTSIDER, TestingSessionLocal)

    resp = send(OUTSIDER, seeded["owner"])

    assert resp.status_code == 403, resp.text
    assert _ruleset_exists(seeded["owner"])
    assert github.calls == []
    github.apply_one.assert_not_awaited()


@pytest.mark.parametrize("send", [_check, _apply, _delete])
def test_without_workspace_members_only_the_owner_sees_the_project(seeded, github, send, authenticate_client):
    """With no members the middleware enforces nothing, and a stranger's project is invisible."""
    db = TestingSessionLocal()
    try:
        db.query(WorkspaceMember).delete()
        db.commit()
    finally:
        db.close()
    authenticate_client(client, OUTSIDER, TestingSessionLocal)

    resp = send(OUTSIDER, seeded["owner"])

    assert resp.status_code == 404, resp.text
    assert _ruleset_exists(seeded["owner"])
    assert github.calls == []


@pytest.mark.parametrize("send", [_apply, _delete])
def test_editing_needs_every_project_the_ruleset_is_attached_to(seeded, github, send, authenticate_client):
    """Removal reaches every attached project's repositories, not just the caller's."""
    db = TestingSessionLocal()
    try:
        stranger = Account(github_user="rs-access-stranger", github_email="s@example.com", account_type="free")
        db.add(stranger)
        db.flush()
        other = Project(project_name="Elsewhere", project_code="RSEL", user_id=stranger.user_id, project_type="standard")
        db.add(other)
        db.flush()
        db.add(ProjectRuleset(project_id=other.project_id, ruleset_id=seeded["owner"]))
        db.commit()
    finally:
        db.close()
    authenticate_client(client, OWNER, TestingSessionLocal)

    resp = send(OWNER, seeded["owner"])

    assert resp.status_code == 403, resp.text
    assert _ruleset_exists(seeded["owner"])
    assert github.calls == []
    github.apply_one.assert_not_awaited()


def test_the_callers_name_in_another_case_acts_as_them(seeded, github, authenticate_client):
    """The session check ignores case, so the repository gate and the token lookup must too."""
    authenticate_client(client, OWNER, TestingSessionLocal)
    shouted = OWNER.upper()

    assert _check(shouted, seeded["admin"]).status_code == 200
    assert _apply(shouted, seeded["admin"]).status_code == 200
    assert github.apply_one.await_args.args[0] == OWNER
    resp = _delete(shouted, seeded["admin"], scope="project_and_github")

    assert resp.status_code == 200, resp.text
    assert {token for _, _, token in github.calls} == {f"token tok-{OWNER}"}


@pytest.mark.parametrize("send", [_check, _apply, _delete])
def test_only_its_creator_can_act_on_an_unattached_ruleset(seeded, github, send, authenticate_client):
    authenticate_client(client, ADMIN, TestingSessionLocal)

    resp = send(ADMIN, seeded["loose"])

    assert resp.status_code == 404, resp.text
    assert _ruleset_exists(seeded["loose"])
    assert github.calls == []


def test_the_creator_can_still_check_an_unattached_ruleset(seeded, github, authenticate_client):
    authenticate_client(client, OWNER, TestingSessionLocal)

    assert _check(OWNER, seeded["loose"]).status_code == 200


def test_the_owner_can_remove_a_ruleset_after_its_uploader_loses_access(
    seeded, github, authenticate_client
):
    db = TestingSessionLocal()
    try:
        editor = db.query(Account).filter_by(github_user=EDITOR).one()
        db.query(ProjectMembership).filter_by(user_id=editor.user_id).delete()
        db.commit()
    finally:
        db.close()

    authenticate_client(client, EDITOR, TestingSessionLocal)
    assert _delete(EDITOR, seeded["editor"]).status_code == 403

    authenticate_client(client, OWNER, TestingSessionLocal)
    resp = _delete(OWNER, seeded["editor"], scope="project_and_github")

    assert resp.status_code == 200, resp.text
    assert resp.json()["removed_from_repos"] == [REPO]
    assert ("DELETE", f"https://api.github.com/repos/{REPO}/rulesets/{GITHUB_RULESET_ID}",
            f"token tok-{OWNER}") in github.calls
    assert not _ruleset_exists(seeded["editor"])
