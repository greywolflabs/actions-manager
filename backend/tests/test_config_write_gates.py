"""Secrets, environment variables, environments and rulesets refuse a viewer.

These modules had no project-role check between them — fourteen write routes,
zero calls to check_project_access or _require_project_editor. So hiding the
controls in the UI stopped a viewer clicking them and nothing more: the same
request made outside the interface was accepted.

They also take the caller's own name as a client-supplied field and use it to
select whose GitHub token is sent, which is #2087. Both are closed here by
require_project_write_access, which proves the name against the session before
it proves the role.
"""
import sys
import os
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (
    Base, Account, Project, ProjectMembership, ProjectRepo, Repo, WorkspaceMember,
)
from main import app
from projects import get_db as projects_get_db
from workflows import get_db as workflows_get_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_config_write_gates.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "owner-user"
MEMBER = "member-user"
PROJECT = "GatedConfigProject"
REPO = "owner-user/service"
OTHER_REPO = "owner-user/elsewhere"


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def setup_and_teardown_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _seed(project_role):
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email="o@e.com", account_type="pro")
        member = Account(github_user=MEMBER, github_email="m@e.com", account_type="pro")
        db.add_all([owner, member])
        db.flush()
        db.add(WorkspaceMember(user_id=member.user_id, workspace_role="member"))

        project = Project(
            project_name=PROJECT, project_code="GCFG", user_id=owner.user_id,
            project_type="standard",
        )
        db.add(project)
        db.flush()
        db.add(ProjectMembership(
            user_id=member.user_id, project_id=project.project_id, project_role=project_role,
        ))
        repo = Repo(repo_name=REPO)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def viewer(authenticate_client):
    app.dependency_overrides[projects_get_db] = override_get_db
    app.dependency_overrides[workflows_get_db] = override_get_db
    original = app.state.middleware_db_factory
    app.state.middleware_db_factory = TestingSessionLocal
    with mock.patch("mode_validation.validate_startup_configuration"):
        with TestClient(app) as client:
            authenticate_client(client, MEMBER, TestingSessionLocal)
            yield client
    app.state.middleware_db_factory = original
    app.dependency_overrides.pop(projects_get_db, None)
    app.dependency_overrides.pop(workflows_get_db, None)


# --- environment variables and environments --------------------------------

@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_viewer_cannot_update_env_vars(mock_tokens, viewer):
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    resp = viewer.post("/api/update-env-vars", json={
        "user": MEMBER, "project_name": PROJECT,
        "repo_names": [REPO], "env": [{"key": "K", "value": "V"}],
    })

    assert resp.status_code == 403, resp.text


@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_viewer_cannot_delete_env_vars(mock_tokens, viewer):
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    resp = viewer.request("DELETE", "/api/delete-env-vars", json={
        "user": MEMBER, "project_name": PROJECT, "repo_names": [REPO], "env": [],
    })

    assert resp.status_code == 403, resp.text


@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_viewer_cannot_create_an_environment(mock_tokens, viewer):
    """Repo-scoped: this route carries no project, so the gate resolves one."""
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    resp = viewer.post("/api/create-environment", json={
        "user": MEMBER, "repo_name": REPO, "environment_name": "production",
    })

    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("named", [OWNER, None])
@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_env_vars_act_as_the_session_caller(mock_tokens, named, viewer):
    """The caller comes from the session (#2241): naming the owner, or nobody,
    writes with the member's own token and never the owner's."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_tokens[OWNER] = "owner_token"
    write = mock.AsyncMock(return_value=201)
    seen = []

    class _GitHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, _url, headers):
            seen.append(headers["Authorization"])
            return mock.Mock(status_code=200)

    as_named = {"user": named} if named else {}
    with mock.patch("github_env_vars._check_free_account_limits", mock.AsyncMock(return_value=None)), \
         mock.patch("github_env_vars._update_or_create_variable", write), \
         mock.patch("github_env_vars._check_environment_limit", mock.AsyncMock(return_value=None)), \
         mock.patch("github_env_vars.httpx.AsyncClient", lambda *a, **k: _GitHub()):
        updated = viewer.post("/api/update-env-vars", json={
            "project_name": PROJECT, "repo_names": [REPO], "env": [{"key": "K", "value": "V"}], **as_named,
        })
        created = viewer.post("/api/create-environment", json={
            "repo_name": REPO, "environment_name": "production", **as_named,
        })

    assert updated.status_code == 200, updated.text
    assert created.status_code == 200, created.text
    assert write.await_args.args[3]["Authorization"] == "token fake_token"
    assert seen == ["token fake_token"]


@pytest.mark.parametrize("named", [OWNER, None])
@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_environment_reads_use_the_session_callers_token(mock_tokens, named, viewer):
    """get-environments and environments-count have no project gate, so the
    token is all that scopes them: naming the owner must not send theirs."""
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"
    mock_tokens[OWNER] = "owner_token"
    response = mock.Mock(status_code=200)
    response.json.return_value = {"environments": [{"name": "production"}]}
    github = mock.AsyncMock()
    github.get.return_value = response
    async_client = mock.MagicMock()
    async_client.return_value.__aenter__.return_value = github
    as_named = {"user": named} if named else {}

    with mock.patch("github_env_vars.httpx.AsyncClient", async_client):
        listed = viewer.get("/api/get-environments", params={"repo_name": REPO, **as_named})
        counted = viewer.get("/api/environments-count", params={"repo_names": REPO, **as_named})

    assert listed.json() == {"environments": [{"name": "production"}]}
    assert counted.json() == {"count": 1}
    sent = [call.kwargs["headers"]["Authorization"] for call in github.get.await_args_list]
    assert sent == ["token fake_token", "token fake_token"]


ENV_VAR_WRITES = {
    "update-env-vars": ("POST", {"env": [{"key": "K", "value": "V"}]}),
    "delete-env-vars": ("DELETE", {"env": [{"env_key": "K"}]}),
    "sync-env-var": ("POST", {"env_key": "K"}),
    "sync-environment": ("POST", {"environment_name": "production"}),
}


class _RecordingGitHub:
    """Answers every GitHub call, recording the URL: 404 (or *get_status*) to a
    read, 201 to a write."""

    def __init__(self, get_status=404):
        self.urls = []
        self.get_status = get_status

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def __getattr__(self, method):
        async def call(url, *_args, **_kwargs):
            self.urls.append(url)
            return mock.Mock(status_code=self.get_status if method == "get" else 201)
        return call


@pytest.mark.parametrize("route", ENV_VAR_WRITES)
@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_env_var_writes_stay_in_the_projects_repositories(mock_tokens, route, viewer):
    """Editing one project reached any repository the caller's token can write
    to, and in no-prefix mode a variable's name is not scoped to the project.
    Refused before any call, so the panel cannot report a change on the
    project's own repositories that it then half-made."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    method, body = ENV_VAR_WRITES[route]
    github = _RecordingGitHub()

    with mock.patch("github_env_vars.httpx.AsyncClient", github):
        resp = viewer.request(method, f"/api/{route}", json={
            "project_name": PROJECT, "repo_names": [REPO, OTHER_REPO], **body,
        })

    assert resp.status_code == 400, resp.text
    assert "save the project first" in resp.json()["detail"]
    assert github.urls == []


@pytest.mark.parametrize("method, route, body", [
    ("DELETE", "delete-environment", {"repo_names": [REPO], "environment_name": ".."}),
    ("POST", "create-environment", {"repo_name": REPO, "environment_name": ".."}),
    ("POST", "sync-environment", {"project_name": PROJECT, "repo_names": [REPO], "environment_name": ".."}),
    ("DELETE", "delete-environment", {"repo_names": ["owner-user/.."], "environment_name": "production"}),
    ("DELETE", "delete-environment", {"repo_names": [REPO], "environment_name": "../../service"}),
])
@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_a_dot_segment_never_reaches_a_github_url(mock_tokens, method, route, body, viewer):
    """httpx resolves '.' and '..': DELETE .../environments/.. became
    DELETE /repos/{owner}/{repo}, GitHub's delete-repository endpoint."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    github = _RecordingGitHub()

    with mock.patch("github_env_vars.httpx.AsyncClient", github):
        resp = viewer.request(method, f"/api/{route}", json=body)

    assert resp.status_code == 400, resp.text
    assert github.urls == []


@pytest.mark.parametrize("method, route, body", [
    ("POST", "update-env-vars", {"env": [{"key": "../../../../victim/repo/actions/variables/K", "value": "V"}]}),
    ("DELETE", "delete-env-vars", {"env": [{"env_key": "K?x"}]}),
    ("POST", "sync-env-var", {"env_key": None}),
])
@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_a_variable_name_cannot_leave_its_repository(mock_tokens, method, route, body, viewer):
    """The name goes into the URL path, and httpx resolves '../': a crafted name
    reached a repository the scope check never approved."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    github = _RecordingGitHub()

    with mock.patch("github_env_vars.httpx.AsyncClient", github):
        resp = viewer.request(method, f"/api/{route}", json={
            "project_name": PROJECT, "repo_names": [REPO], **body,
        })

    assert resp.status_code == 400, resp.text
    assert github.urls == []


@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_environment_routes_refuse_malformed_input_before_the_gate(mock_tokens, viewer):
    """The gate ran first: a missing name was "not found", a number a 500
    reported as 200, and a string was checked letter by letter."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"

    created = viewer.post("/api/create-environment", json={"repo_name": None, "environment_name": "production"})
    unsplit = viewer.post("/api/create-environment", json={"repo_name": "noslash", "environment_name": "production"})
    as_string = viewer.request("DELETE", "/api/delete-environment", json={
        "repo_names": REPO, "environment_name": "production",
    })
    as_number = viewer.request("DELETE", "/api/delete-environment", json={
        "repo_names": [123], "environment_name": "production",
    })

    assert created.status_code == 400
    assert created.json()["detail"] == "Missing or invalid repository name"
    assert unsplit.status_code == as_string.status_code == as_number.status_code == 400


@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_environment_reads_refuse_a_path_for_a_repository_name(mock_tokens, viewer):
    """A full member skips the read_only gate, so a name like 'a/b/../../../user'
    reached GET /user with the caller's token."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    github = _RecordingGitHub()

    with mock.patch("github_env_vars.httpx.AsyncClient", github):
        counted = viewer.get("/api/environments-count", params={"repo_names": "a/b/../../../user"})
        listed = viewer.get("/api/get-environments", params={"repo_name": "a/b/../../../user"})

    assert counted.status_code == listed.status_code == 400
    assert github.urls == []


@mock.patch("github_env_vars.user_tokens", new_callable=dict)
def test_sync_environment_says_when_github_could_not_answer(mock_tokens, viewer):
    """'Not found in any repository' was the answer when GitHub had refused the
    probe, which sent the user looking for an environment that may be there."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"

    with mock.patch("github_env_vars.httpx.AsyncClient", _RecordingGitHub(get_status=502)):
        resp = viewer.post("/api/sync-environment", json={
            "project_name": PROJECT, "repo_names": [REPO], "environment_name": "production",
        })

    assert resp.json() == {"error": f"Could not check environment 'production' in {REPO}", "status": 502}


# --- secrets ---------------------------------------------------------------

@mock.patch("github_secrets.user_tokens", new_callable=dict)
def test_viewer_cannot_create_secrets(mock_tokens, viewer):
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    resp = viewer.post("/api/create-secrets", json={
        "user": MEMBER, "project_name": PROJECT,
        "repo_names": [REPO], "secrets": [{"secret_key": "K", "secret_value": "V"}],
    })

    assert resp.status_code == 403, resp.text


@mock.patch("github_secrets.user_tokens", new_callable=dict)
def test_viewer_cannot_delete_secrets(mock_tokens, viewer):
    _seed("project_viewer")
    mock_tokens[MEMBER] = "fake_token"

    resp = viewer.request("DELETE", "/api/delete-secrets", json={
        "user": MEMBER, "project_name": PROJECT,
        "repo_names": [REPO], "secret_name": "K",
    })

    assert resp.status_code == 403, resp.text


# --- an editor is unaffected ----------------------------------------------

@mock.patch("github_secrets.user_tokens", new_callable=dict)
def test_an_editor_passes_the_gate(mock_tokens, viewer):
    """The gate must not lock out the role that is meant to configure these.

    Asserts only that the request is not refused: what happens after depends on
    GitHub, which is not reachable here.
    """
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"

    resp = viewer.post("/api/create-secrets", json={
        "user": MEMBER, "project_name": PROJECT,
        "repo_names": [REPO], "secrets": [{"secret_key": "K", "secret_value": "V"}],
    })

    assert resp.status_code != 403, resp.text


@pytest.mark.parametrize("named", [OWNER, None])
@mock.patch("github_secrets.user_tokens", new_callable=dict)
def test_naming_another_user_borrows_nothing_of_theirs(mock_tokens, named, viewer):
    """#2087: the name selected whose GitHub token is used. The caller now comes
    from the session (#2241), so naming the owner, or nobody, sends the
    member's own token and never the owner's."""
    _seed("project_editor")
    mock_tokens[MEMBER] = "fake_token"
    mock_tokens[OWNER] = "owner_token"
    counted = mock.AsyncMock(return_value=0)
    public_key = mock.AsyncMock(return_value=(None, {"error": "stopped before GitHub"}))
    body = {
        "project_name": PROJECT,
        "repo_names": [REPO], "secrets": [{"secret_key": "K", "secret_value": "V"}],
        **({"user": named} if named else {}),
    }

    with mock.patch("github_secrets.count_project_secrets", counted), \
         mock.patch("github_secrets._get_repo_public_key", public_key):
        resp = viewer.post("/api/create-secrets", json=body)

    assert resp.status_code == 200, resp.text
    assert counted.await_args.args[0] == MEMBER
    _repo, headers, _client = public_key.await_args.args
    assert headers["Authorization"] == "token fake_token"
