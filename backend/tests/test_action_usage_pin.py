"""
Updating an action's version from Action Usage (#2046): GET /api/action-usage/preview and /latest,
POST /api/action-usage/update-workflow and /apply.

An update saves the new version into the workflow in the project, as Commit Locally does, and never
opens a pull request, so the checks are on the stored workflows, the project's state and that nothing
reached GitHub. GitHub's answers (the latest release, a tag's commit) are patched.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth import create_auth_session
from database import get_db
from main import app
from models import (
    Account,
    Base,
    LinkedReusableWorkflow,
    Project,
    ProjectMembership,
    ProjectPullRequest,
    ProjectRepo,
    ProjectWorkflow,
    Repo,
    RepoWorkflowOverride,
    Workflow,
    WorkspaceMember,
)

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "pin-owner"
OTHER = "pin-other"
ACTION = "actions/checkout"
SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"
OLD_SHA = "08c6903cd8c0fde910a37f88322edcfb5dd907a8"


def _workflow_yaml(*uses: str, reusable: bool = False) -> str:
    steps = "\n".join(f"      - uses: {value}" for value in uses)
    trigger = "workflow_call" if reusable else "push"
    return f"name: ci\non: {trigger}\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n{steps}\n"


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with patch.dict(app.dependency_overrides, {get_db: override_get_db}), \
            patch.object(app.state, "middleware_db_factory", TestingSessionLocal), \
            patch("mode_validation.validate_startup_configuration"), \
            TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    session = TestingSessionLocal()
    yield session
    session.close()


def _account(name: str, workspace_role: str | None = "admin") -> SimpleNamespace:
    db = TestingSessionLocal()
    try:
        account = Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
        db.add(account)
        db.flush()
        if workspace_role:
            db.add(WorkspaceMember(user_id=account.user_id, workspace_role=workspace_role))
        db.commit()
        return SimpleNamespace(user_id=account.user_id, github_user=name)
    finally:
        db.close()


def _sign_in(client: TestClient, name: str) -> None:
    db = TestingSessionLocal()
    try:
        client.headers["Authorization"] = f"Bearer {create_auth_session(name, db)}"
    finally:
        db.close()


def _project(owner, name: str, workflows: dict, repos: tuple = ("octo/api",), **fields) -> dict:
    """A project with *workflows* ({name: yaml}) and *repos*; returns its id and workflow ids by name."""
    db = TestingSessionLocal()
    try:
        project = Project(
            project_name=name, project_code=name[:6].upper(), user_id=owner.user_id, pr_state="synced", **fields
        )
        db.add(project)
        db.flush()
        workflow_ids = {}
        for workflow_name, text in workflows.items():
            workflow = Workflow(
                workflow_name=workflow_name, workflow_yaml=text, workflow_status="synced_with_github",
                reusable_workflow="workflow_call" in text,
            )
            db.add(workflow)
            db.flush()
            db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
            workflow_ids[workflow_name] = workflow.workflow_id
        for repo_name in repos:
            repo = db.query(Repo).filter(Repo.repo_name == repo_name).first() or Repo(repo_name=repo_name)
            db.add(repo)
            db.flush()
            db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.commit()
        return {"project_id": project.project_id, "workflows": workflow_ids, "code": project.project_code}
    finally:
        db.close()


def _shared_reusable(owner, callers: list, name: str = "Shared") -> dict:
    """A Reusable Workflow Project with one repo and a reusable ``deploy`` on actions/checkout@v3, linked into *callers*."""
    db = TestingSessionLocal()
    try:
        rwx = Project(project_name=name, project_code="SHARED", user_id=owner.user_id, project_type="rwx",
                      use_prefix=True, pr_state="synced")
        repo = Repo(repo_name="acme/shared")
        deploy = Workflow(workflow_name="deploy", workflow_yaml=_workflow_yaml("actions/checkout@v3", reusable=True),
                          reusable_workflow=True, workflow_status="synced_with_github")
        db.add_all([rwx, repo, deploy])
        db.flush()
        db.add(ProjectRepo(project_id=rwx.project_id, repo_id=repo.repo_id))
        db.add(ProjectWorkflow(project_id=rwx.project_id, workflow_id=deploy.workflow_id))
        for caller in callers:
            db.add(LinkedReusableWorkflow(
                standard_project_id=caller["project_id"], rwx_project_id=rwx.project_id, workflow_id=deploy.workflow_id,
            ))
        db.commit()
        return {"project_id": rwx.project_id, "workflows": {"deploy": deploy.workflow_id}, "code": "SHARED"}
    finally:
        db.close()


def _override(project: dict, workflow: str, repo_name: str, text: str) -> None:
    db = TestingSessionLocal()
    try:
        repo = db.query(Repo).filter(Repo.repo_name == repo_name).one()
        db.add(RepoWorkflowOverride(
            project_id=project["project_id"], repo_id=repo.repo_id, workflow_id=project["workflows"][workflow],
            workflow_name=workflow, workflow_yaml=text,
        ))
        db.commit()
    finally:
        db.close()


def _grant(project: dict, account, role: str) -> None:
    db = TestingSessionLocal()
    try:
        db.add(ProjectMembership(user_id=account.user_id, project_id=project["project_id"], project_role=role))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def owner(client):
    account = _account(OWNER)
    _sign_in(client, OWNER)
    return account


def _preview(client, ref="v5.0.0", user=OWNER, resolved=SHA):
    with patch("action_usage_pin.resolve_ref_to_sha", return_value=resolved) as resolve, \
            patch.dict("auth.user_tokens", {user: "token"}):
        response = client.get("/api/action-usage/preview", params={"action": ACTION, "ref": ref})
    return response, resolve


def _preview_latest(client, user=OWNER, found=("v5.0.0", SHA)):
    with patch("action_usage_pin.latest_release", return_value=found) as latest, \
            patch("action_usage_pin.resolve_ref_to_sha") as resolve, \
            patch.dict("auth.user_tokens", {user: "token"}):
        response = client.get("/api/action-usage/preview", params={"action": ACTION})
    return response, latest, resolve


def _apply(client, project: dict, **body):
    payload = {"project_id": project["project_id"], "action": ACTION, "sha": SHA, "comment": "v5.0.0", **body}
    return client.post("/api/action-usage/apply", json=payload)


def _update(client, project: dict, workflow: str, commit: bool = True, **body):
    payload = {
        "workflow_id": project["workflows"][workflow], "action": ACTION, "sha": SHA, "comment": "v5.0.0",
        "commit": commit, **body,
    }
    return client.post("/api/action-usage/update-workflow", json=payload)


def _stored(db, workflow_id: int) -> Workflow:
    db.expire_all()
    return db.query(Workflow).filter(Workflow.workflow_id == workflow_id).one()


def _project_state(db, project: dict) -> str:
    db.expire_all()
    return db.query(Project).filter(Project.project_id == project["project_id"]).one().pr_state


def _set_status(project: dict, workflow: str, status: str) -> None:
    session = TestingSessionLocal()
    try:
        session.query(Workflow).filter(Workflow.workflow_id == project["workflows"][workflow]).update({"workflow_status": status})
        session.commit()
    finally:
        session.close()


PINNED = f"actions/checkout@{SHA} # v5.0.0"


def test_preview_resolves_the_tag_once_and_lists_the_changed_lines_of_every_project(client, owner):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3"), "lint": _workflow_yaml("actions/checkout@v4")})
    _project(owner, "Beta", {"build": _workflow_yaml("actions/checkout@v4", "actions/cache@v4")}, repos=("octo/web",))
    _project(owner, "Gamma", {"other": _workflow_yaml("actions/cache@v4")})

    response, resolve = _preview(client)

    body = response.json()
    assert resolve.call_count == 1
    assert (body["sha"], body["comment"]) == (SHA, "v5.0.0")
    assert [p["project_name"] for p in body["projects"]] == ["Alpha", "Beta"]
    first = body["projects"][0]
    assert (first["project_id"], first["project_type"], first["editable"]) == (alpha["project_id"], "standard", True)
    assert [w["workflow_name"] for w in first["workflows"]] == ["ci", "lint"]
    assert first["workflows"][0]["workflow_file"] == "AM_ALPHA_ci.yml"
    assert first["workflows"][0]["blocked"] is None
    assert first["workflows"][0]["lines"] == [
        {"line": 7, "before": "      - uses: actions/checkout@v3", "after": f"      - uses: {PINNED}"},
    ]


def test_preview_without_a_ref_defaults_to_the_latest_release_with_one_lookup(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    _project(owner, "Beta", {"build": _workflow_yaml("actions/checkout@v4")}, repos=("octo/web",))

    response, latest, resolve = _preview_latest(client)

    body = response.json()
    assert (latest.call_count, resolve.call_count) == (1, 0)
    assert (body["ref"], body["source"], body["sha"], body["comment"]) == ("v5.0.0", "latest release", SHA, "v5.0.0")
    assert [p["project_name"] for p in body["projects"]] == ["Alpha", "Beta"]


def test_preview_with_a_ref_says_it_was_the_one_asked_for(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})

    body = _preview(client, ref="v4.1.0")[0].json()

    assert (body["ref"], body["source"]) == ("v4.1.0", "requested")


def test_preview_without_a_ref_passes_on_what_the_lookup_could_not_find(client, owner):
    failure = HTTPException(status_code=404, detail="actions/checkout has no release or version tag to default to.")
    with patch("action_usage_pin.latest_release", side_effect=failure), patch.dict("auth.user_tokens", {OWNER: "token"}):
        response = client.get("/api/action-usage/preview", params={"action": ACTION})

    assert response.status_code == 404
    assert "no release or version tag" in response.json()["detail"]


def test_preview_of_a_bare_sha_writes_no_comment(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})

    body = _preview(client, ref=SHA)[0].json()

    assert body["comment"] is None
    assert body["projects"][0]["workflows"][0]["lines"][0]["after"].endswith(f"checkout@{SHA}")


def test_preview_lists_the_repos_with_their_own_copy_as_keeping_it(client, owner):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")}, repos=("octo/api", "octo/web"))
    _override(alpha, "ci", "octo/web", _workflow_yaml("actions/checkout@v3"))

    project = _preview(client)[0].json()["projects"][0]

    assert project["skipped_repos"] == ["octo/web"]


def test_preview_marks_a_workflow_with_an_open_pull_request_as_blocked(client, owner):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3"), "lint": _workflow_yaml("actions/checkout@v3")})
    _set_status(alpha, "lint", "under_review")

    workflows = _preview(client)[0].json()["projects"][0]["workflows"]

    assert [(w["workflow_name"], bool(w["blocked"])) for w in workflows] == [("ci", False), ("lint", True)]


def test_preview_includes_a_reusable_workflow_projects_own_workflows(client, owner):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    _shared_reusable(owner, [alpha])

    projects = _preview(client)[0].json()["projects"]

    assert [(p["project_name"], p["project_type"]) for p in projects] == [("Alpha", "standard"), ("Shared", "rwx")]


def test_preview_marks_a_project_the_caller_cannot_edit(client, owner):
    mine = _project(owner, "Mine", {"ci": _workflow_yaml("actions/checkout@v3")})
    _project(owner, "Theirs", {"ci": _workflow_yaml("actions/checkout@v3")})
    reader = _account(OTHER, workspace_role="read_only")
    _grant(mine, reader, "project_viewer")
    _sign_in(client, OTHER)

    response, _ = _preview(client, user=OTHER)

    assert response.status_code == 200
    assert [(p["project_name"], p["editable"]) for p in response.json()["projects"]] == [("Mine", False)]


def test_preview_needs_a_github_token(client, owner):
    with patch.dict("auth.user_tokens", clear=True):
        response = client.get("/api/action-usage/preview", params={"action": ACTION, "ref": "v5"})

    assert response.status_code == 401


@pytest.mark.parametrize("params", [{"action": "not-an-action", "ref": "v1"}, {"action": ACTION, "ref": ""}, {"ref": "v1"}])
def test_preview_rejects_a_malformed_request(client, owner, params):
    assert client.get("/api/action-usage/preview", params=params).status_code == 422


def test_latest_gives_the_release_and_the_commit_it_is_on(client, owner):
    with patch("action_usage_pin.latest_release", return_value=("v5.0.0", SHA)) as latest, \
            patch.dict("auth.user_tokens", {OWNER: "token"}):
        response = client.get("/api/action-usage/latest", params={"action": ACTION})

    assert response.status_code == 200
    assert response.json() == {"action": ACTION, "ref": "v5.0.0", "sha": SHA, "comment": "v5.0.0"}
    assert latest.call_count == 1


def test_latest_needs_a_github_token(client, owner):
    with patch.dict("auth.user_tokens", clear=True):
        response = client.get("/api/action-usage/latest", params={"action": ACTION})

    assert response.status_code == 401


def test_update_commits_one_workflow_locally_and_leaves_the_project_with_changes_to_deliver(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3", "actions/cache@v4"), "lint": _workflow_yaml("actions/checkout@v3")})
    assert _project_state(db, alpha) == "synced"

    response = _update(client, alpha, "ci")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["workflow_name"], body["workflow_file"], body["changed"]) == ("ci", "AM_ALPHA_ci.yml", 1)
    assert (body["pr_state"], body["workflow_status"]) == ("draft", "committed_locally")
    assert _stored(db, alpha["workflows"]["ci"]).workflow_yaml.strip() == _workflow_yaml(PINNED, "actions/cache@v4").strip()
    assert _stored(db, alpha["workflows"]["lint"]).workflow_status == "synced_with_github"
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["lint"]).workflow_yaml
    assert _project_state(db, alpha) == "draft"


def test_update_never_opens_a_pull_request_or_calls_github(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")}, repos=("octo/api", "octo/web"))

    with patch("requests.request") as sent, patch("workflows.create_pull_requests") as campaign:
        assert _update(client, alpha, "ci").status_code == 200

    assert sent.call_count == 0
    assert campaign.call_count == 0
    assert db.query(ProjectPullRequest).count() == 0


def test_update_without_commit_returns_the_new_yaml_and_saves_nothing(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})

    response = _update(client, alpha, "ci", commit=False)

    assert response.status_code == 200, response.text
    assert response.json()["content"].strip() == _workflow_yaml(PINNED).strip()
    assert "pr_state" not in response.json()
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["ci"]).workflow_yaml
    assert _stored(db, alpha["workflows"]["ci"]).workflow_status == "synced_with_github"
    assert _project_state(db, alpha) == "synced"


def test_update_commits_the_own_workflow_of_a_reusable_workflow_project(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    shared = _shared_reusable(owner, [alpha])

    response = _update(client, shared, "deploy")

    assert response.status_code == 200, response.text
    assert response.json()["reusable"] is True
    assert PINNED in _stored(db, shared["workflows"]["deploy"]).workflow_yaml
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["ci"]).workflow_yaml


def test_update_refuses_to_commit_a_workflow_with_an_open_pull_request_but_returns_its_yaml(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    _set_status(alpha, "ci", "under_review")

    refused = _update(client, alpha, "ci")
    allowed = _update(client, alpha, "ci", commit=False)

    assert refused.status_code == 409, refused.text
    assert "Under review" in refused.json()["detail"]
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["ci"]).workflow_yaml
    assert allowed.status_code == 200, allowed.text


def test_update_of_a_workflow_that_does_not_use_the_action_at_another_version_is_a_400(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/cache@v4"), "done": _workflow_yaml(PINNED)})

    assert _update(client, alpha, "ci").status_code == 400
    assert _update(client, alpha, "done").status_code == 400
    assert _project_state(db, alpha) == "synced"


def test_update_needs_an_editor_and_saves_nothing_for_a_viewer(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    reader = _account(OTHER, workspace_role="read_only")
    _grant(alpha, reader, "project_viewer")
    _sign_in(client, OTHER)

    response = _update(client, alpha, "ci")

    assert response.status_code == 403
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["ci"]).workflow_yaml


def test_update_by_a_member_who_only_views_the_project_is_refused_by_the_server(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    viewer = _account(OTHER, workspace_role="member")
    _grant(alpha, viewer, "project_viewer")
    _sign_in(client, OTHER)

    response = _update(client, alpha, "ci")

    assert response.status_code == 403
    assert "project_editor" in response.json()["detail"]
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["ci"]).workflow_yaml


def test_update_of_a_workflow_that_does_not_exist_or_is_being_removed_is_a_404(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    session = TestingSessionLocal()
    try:
        session.query(Workflow).filter(Workflow.workflow_id == alpha["workflows"]["ci"]).update({"pending_delete": True})
        session.commit()
    finally:
        session.close()

    assert _update(client, alpha, "ci").status_code == 404
    assert _update(client, {"workflows": {"ci": 9999}}, "ci").status_code == 404


@pytest.mark.parametrize(
    "body",
    [{"sha": "v5"}, {"sha": SHA[:39]}, {"comment": "two\nlines"}, {"action": "nope"}, {"workflow_id": 0}],
)
def test_update_rejects_a_malformed_request(client, owner, body):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})

    assert _update(client, alpha, "ci", **body).status_code == 422


def test_apply_commits_every_changed_workflow_of_the_project_locally_and_opens_no_pull_request(client, owner, db):
    alpha = _project(
        owner, "Alpha",
        {"ci": _workflow_yaml("actions/checkout@v3", "actions/cache@v4"), "lint": _workflow_yaml("actions/checkout@v4"), "untouched": _workflow_yaml("actions/cache@v4")},
        repos=("octo/api", "octo/web"),
    )

    with patch("requests.request") as sent, patch("workflows.create_pull_requests") as campaign:
        response = _apply(client, alpha)

    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 2, "workflows": ["ci", "lint"], "pr_state": "draft"}
    assert (sent.call_count, campaign.call_count, db.query(ProjectPullRequest).count()) == (0, 0, 0)
    assert PINNED in _stored(db, alpha["workflows"]["ci"]).workflow_yaml
    assert PINNED in _stored(db, alpha["workflows"]["lint"]).workflow_yaml
    assert _stored(db, alpha["workflows"]["ci"]).workflow_status == "committed_locally"
    untouched = _stored(db, alpha["workflows"]["untouched"])
    assert (untouched.workflow_status, "actions/cache@v4" in untouched.workflow_yaml) == ("synced_with_github", True)


def test_apply_commits_a_reusable_workflow_project_s_own_workflows(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    shared = _shared_reusable(owner, [alpha])

    response = _apply(client, shared)

    assert response.status_code == 200, response.text
    assert PINNED in _stored(db, shared["workflows"]["deploy"]).workflow_yaml
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["ci"]).workflow_yaml


def test_apply_leaves_a_workflow_with_an_open_pull_request_as_it_is(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3"), "lint": _workflow_yaml("actions/checkout@v3")})
    _set_status(alpha, "lint", "under_review")

    response = _apply(client, alpha)

    assert response.status_code == 200, response.text
    assert response.json()["workflows"] == ["ci"]
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["lint"]).workflow_yaml


def test_apply_when_every_changed_workflow_has_an_open_pull_request_saves_nothing(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    _set_status(alpha, "ci", "under_review")

    response = _apply(client, alpha)

    assert response.status_code == 409, response.text
    assert _project_state(db, alpha) == "synced"


def test_apply_a_second_time_has_nothing_left_to_change(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    assert _apply(client, alpha).status_code == 200

    assert _apply(client, alpha).status_code == 400


def test_apply_to_a_project_already_on_the_sha_is_refused_without_saving(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml(PINNED)})

    assert _apply(client, alpha).status_code == 400
    assert _stored(db, alpha["workflows"]["ci"]).workflow_status == "synced_with_github"


def test_apply_needs_an_editor_and_saves_nothing_for_a_viewer(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    reader = _account(OTHER, workspace_role="read_only")
    _grant(alpha, reader, "project_viewer")
    _sign_in(client, OTHER)

    response = _apply(client, alpha)

    assert response.status_code == 403
    assert "actions/checkout@v3" in _stored(db, alpha["workflows"]["ci"]).workflow_yaml


def test_apply_to_a_project_that_does_not_exist_is_a_404(client, owner):
    assert _apply(client, {"project_id": 9999, "workflows": {}, "code": "NONE"}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [{"sha": "v5"}, {"sha": SHA[:39]}, {"comment": "two\nlines"}, {"action": "nope"}, {"project_id": 0}],
)
def test_apply_rejects_a_malformed_request(client, owner, body):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})

    assert _apply(client, alpha, **body).status_code == 422


def test_a_linked_reusable_workflow_is_never_changed_through_a_caller(client, owner, db):
    alpha = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")}, repos=("octo/api", "octo/web"))
    beta = _project(owner, "Beta", {"ci": _workflow_yaml("actions/checkout@v3")}, repos=("octo/b1",))
    shared = _shared_reusable(owner, [alpha, beta])

    assert _apply(client, alpha).status_code == 200
    assert _apply(client, beta).status_code == 200

    deploy = _stored(db, shared["workflows"]["deploy"])
    assert ("actions/checkout@v3" in deploy.workflow_yaml, deploy.workflow_status) == (True, "synced_with_github")
    assert db.query(ProjectPullRequest).count() == 0
