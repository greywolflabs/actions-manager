"""
The action usage inventory (#2045): the `uses:` parser, and GET /api/action-usage.

The parser reads the stored YAML only, so a `uses` in a comment or a `run:`
block is not a reference. The endpoint groups by action and ref, scopes to the
projects the caller may see, and attributes a repository with its own copy of a
workflow (an override) to what that copy uses.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from action_usage import BRANCH, DOCKER, LOCAL, SHA, TAG, classify_reference, parse_uses_references
from auth import create_auth_session
from database import get_db
from main import app
from models import (
    Account,
    Base,
    Project,
    ProjectMembership,
    ProjectRepo,
    ProjectWorkflow,
    Repo,
    RepoWorkflowOverride,
    Workflow,
    WorkspaceMember,
)

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

FULL_SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"
OWNER = "au-owner"
OTHER = "au-other"


def _workflow_yaml(*uses: str) -> str:
    steps = "\n".join(f"      - uses: {value}" for value in uses)
    return f"name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n{steps}\n"


@pytest.mark.parametrize(
    ("uses", "kind", "action", "path", "ref"),
    [
        (f"actions/checkout@{FULL_SHA}", SHA, "actions/checkout", "", FULL_SHA),
        (f"actions/checkout@{FULL_SHA.upper()}", SHA, "actions/checkout", "", FULL_SHA.upper()),
        ("actions/checkout@v4", TAG, "actions/checkout", "", "v4"),
        ("actions/checkout@v4.1.2", TAG, "actions/checkout", "", "v4.1.2"),
        ("actions/checkout@5.0.0-rc1", TAG, "actions/checkout", "", "5.0.0-rc1"),
        ("actions/checkout@main", BRANCH, "actions/checkout", "", "main"),
        ("actions/checkout@release/v4", BRANCH, "actions/checkout", "", "release/v4"),
        ("actions/checkout@11bd719", BRANCH, "actions/checkout", "", "11bd719"),
        ("github/codeql-action/init@v3", TAG, "github/codeql-action", "init", "v3"),
        (
            "octo/shared/.github/workflows/build.yml@main",
            BRANCH, "octo/shared", ".github/workflows/build.yml", "main",
        ),
    ],
)
def test_a_remote_reference_is_split_and_classified(uses, kind, action, path, ref):
    reference = classify_reference(uses)

    assert (reference.kind, reference.action, reference.path, reference.ref) == (kind, action, path, ref)
    assert reference.pinned is (kind == SHA)


@pytest.mark.parametrize(("uses", "kind"), [("./local/action", LOCAL), ("./.github/workflows/x.yml", LOCAL),
                                            ("docker://alpine:3", DOCKER)])
def test_local_and_docker_references_are_recorded_and_never_unpinned(uses, kind):
    reference = classify_reference(uses)

    assert (reference.kind, reference.action, reference.pinned) == (kind, None, True)


@pytest.mark.parametrize("uses", ["actions/checkout", "actions/checkout@", "checkout@v4", "/repo@v4", "@v4", ""])
def test_a_value_github_would_reject_is_not_a_reference(uses):
    assert classify_reference(uses) is None


def test_step_and_job_level_uses_are_both_read():
    text = (
        "on: push\njobs:\n"
        "  call:\n    uses: octo/shared/.github/workflows/build.yml@v2\n"
        "  build:\n    steps:\n      - uses: actions/checkout@v4\n      - run: echo hi\n"
    )

    assert [r.uses for r in parse_uses_references(text)] == [
        "octo/shared/.github/workflows/build.yml@v2",
        "actions/checkout@v4",
    ]


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   \n",
        "name: only a name\n",
        "jobs:\n",
        "jobs: nope\n",
        "jobs:\n  build: nope\n",
        "jobs:\n  build:\n    steps: nope\n",
        "jobs:\n  build:\n    steps:\n      - nope\n      - uses: 5\n",
        "jobs: [unclosed\n  - {",
        "- just\n- a list\n",
    ],
)
def test_a_workflow_without_readable_jobs_has_no_references(text):
    assert parse_uses_references(text) == []


def test_a_uses_in_a_comment_or_a_run_block_is_not_a_reference():
    text = (
        "on: push\njobs:\n  build:\n    steps:\n"
        "      # - uses: actions/checkout@v3\n"
        "      - run: |\n          echo 'uses: actions/checkout@v3'\n"
        "      - uses: actions/setup-node@v4 # uses: actions/cache@v1\n"
    )

    assert [r.uses for r in parse_uses_references(text)] == ["actions/setup-node@v4"]


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


def _account(name: str, workspace_role: str | None = "admin") -> SimpleNamespace:
    """An account, and its workspace membership unless *workspace_role* is None."""
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
        project = Project(project_name=name, project_code=name[:6].upper(), user_id=owner.user_id, **fields)
        db.add(project)
        db.flush()
        workflow_ids = {}
        for workflow_name, text in workflows.items():
            workflow = Workflow(workflow_name=workflow_name, workflow_yaml=text, workflow_status="synced_with_github")
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
        return {"project_id": project.project_id, "workflows": workflow_ids}
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


def _usage(client: TestClient) -> dict:
    response = client.get("/api/action-usage")
    assert response.status_code == 200
    return response.json()


def _action(body: dict, name: str) -> dict:
    return next(entry for entry in body["actions"] if entry["action"] == name)


@pytest.fixture
def owner(client):
    account = _account(OWNER)
    _sign_in(client, OWNER)
    return account


def test_the_endpoint_requires_a_signed_in_caller(client):
    assert client.get("/api/action-usage").status_code == 401


def test_a_workspace_without_workflows_has_an_empty_inventory(client, owner):
    assert _usage(client) == {
        "actions": [],
        "other": {"local": 0, "docker": 0},
        "summary": {"action_count": 0, "workflows_read": 0, "unpinned_workflows": 0},
    }


def test_actions_are_grouped_by_ref_with_the_workflows_and_repos_on_each(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3", f"actions/setup-node@{FULL_SHA}")},
             repos=("octo/api", "octo/web"))
    _project(owner, "Beta", {"build": _workflow_yaml("actions/checkout@v4", "actions/checkout@v4", "./local", "docker://alpine:3")})

    body = _usage(client)
    checkout = _action(body, "actions/checkout")

    assert [(v["ref"], v["kind"], v["pinned"], v["workflow_count"], v["repo_count"]) for v in checkout["versions"]] == [
        ("v3", TAG, False, 1, 2),
        ("v4", TAG, False, 1, 1),
    ]
    assert (checkout["workflow_count"], checkout["unpinned_workflow_count"]) == (2, 2)
    v3_usage = checkout["versions"][0]["usages"][0]
    assert (v3_usage["project_name"], v3_usage["workflow_file"], v3_usage["workflow_status"], v3_usage["repos"]) == (
        "Alpha", "AM_ALPHA_ci.yml", "synced_with_github", ["octo/api", "octo/web"],
    )
    setup_node = _action(body, "actions/setup-node")
    assert [(v["ref"], v["pinned"]) for v in setup_node["versions"]] == [(FULL_SHA, True)]
    assert setup_node["unpinned_workflow_count"] == 0
    assert body["other"] == {"local": 1, "docker": 1}
    assert body["summary"] == {"action_count": 2, "workflows_read": 2, "unpinned_workflows": 2}


def test_an_action_used_twice_in_one_workflow_counts_that_workflow_once(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v4", "actions/checkout@v4")})

    version = _action(_usage(client), "actions/checkout")["versions"][0]

    assert (version["workflow_count"], len(version["usages"])) == (1, 1)


def test_the_same_action_in_different_case_is_one_action(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("Actions/Checkout@v4", "actions/checkout@v4")})

    assert [a["action"] for a in _usage(client)["actions"]] == ["Actions/Checkout"]


def test_a_composite_action_path_is_listed_on_the_usage(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("github/codeql-action/init@v3", "github/codeql-action/analyze@v3")})

    usage = _action(_usage(client), "github/codeql-action")["versions"][0]["usages"][0]

    assert usage["paths"] == ["analyze", "init"]


def test_a_unprefixed_project_lists_the_workflow_by_its_own_file_name(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v4")}, use_prefix=False)

    usage = _action(_usage(client), "actions/checkout")["versions"][0]["usages"][0]

    assert usage["workflow_file"] == "ci.yml"


def test_workflows_a_campaign_is_removing_are_left_out(client, owner):
    project = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v4"), "old": _workflow_yaml("actions/cache@v3")})
    db = TestingSessionLocal()
    try:
        db.query(Workflow).filter(Workflow.workflow_id == project["workflows"]["old"]).update({"pending_delete": True})
        db.commit()
    finally:
        db.close()

    body = _usage(client)

    assert [a["action"] for a in body["actions"]] == ["actions/checkout"]
    assert body["summary"]["workflows_read"] == 1


def test_a_repo_with_its_own_copy_is_counted_for_what_that_copy_uses(client, owner):
    project = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")}, repos=("octo/api", "octo/web"))
    _override(project, "ci", "octo/web", _workflow_yaml(f"actions/checkout@{FULL_SHA}"))

    versions = _action(_usage(client), "actions/checkout")["versions"]

    assert [(v["ref"], v["usages"][0]["repos"], v["usages"][0]["override"]) for v in versions] == [
        ("v3", ["octo/api"], False),
        (FULL_SHA, ["octo/web"], True),
    ]


def test_the_base_version_is_not_listed_when_every_repo_has_its_own_copy(client, owner):
    project = _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v3")})
    _override(project, "ci", "octo/api", _workflow_yaml("actions/checkout@v4"))

    versions = _action(_usage(client), "actions/checkout")["versions"]

    assert [v["ref"] for v in versions] == ["v4"]


def test_a_project_without_repositories_still_lists_its_workflows(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v4")}, repos=())

    version = _action(_usage(client), "actions/checkout")["versions"][0]

    assert (version["usages"][0]["repos"], version["repo_count"]) == ([], 0)


def test_a_caller_never_sees_a_project_they_may_not_see(client, owner):
    _project(owner, "Granted", {"ci": _workflow_yaml("actions/checkout@v4")})
    _project(owner, "Hidden", {"ci": _workflow_yaml("actions/cache@v3")})
    reader = _account(OTHER, workspace_role="read_only")
    db = TestingSessionLocal()
    try:
        granted = db.query(Project).filter(Project.project_name == "Granted").one().project_id
        db.add(ProjectMembership(user_id=reader.user_id, project_id=granted, project_role="project_viewer"))
        db.commit()
    finally:
        db.close()
    _sign_in(client, OTHER)

    body = _usage(client)

    assert [a["action"] for a in body["actions"]] == ["actions/checkout"]
    assert [u["project_name"] for u in body["actions"][0]["versions"][0]["usages"]] == ["Granted"]


def test_a_workspace_member_sees_every_project(client, owner):
    _project(owner, "Alpha", {"ci": _workflow_yaml("actions/checkout@v4")})
    _account(OTHER, workspace_role="member")
    _sign_in(client, OTHER)

    assert [a["action"] for a in _usage(client)["actions"]] == ["actions/checkout"]



def test_each_usage_says_which_workflow_it_is_and_whether_the_caller_can_edit_it(client, owner):
    _project(owner, "Granted", {"ci": _workflow_yaml("actions/checkout@v4")})
    _project(owner, "Shared", {"release": _workflow_yaml("actions/checkout@v4")})
    reader = _account(OTHER, workspace_role="read_only")
    db = TestingSessionLocal()
    try:
        for project in db.query(Project).all():
            db.add(ProjectMembership(user_id=reader.user_id, project_id=project.project_id, project_role="project_viewer"))
        db.commit()
    finally:
        db.close()

    as_owner = _action(_usage(client), "actions/checkout")["versions"][0]["usages"]
    _sign_in(client, OTHER)
    as_reader = _action(_usage(client), "actions/checkout")["versions"][0]["usages"]

    assert [(u["workflow_name"], u["reusable"], u["editable"]) for u in as_owner] == [("ci", False, True), ("release", False, True)]
    assert [u["editable"] for u in as_reader] == [False, False]
