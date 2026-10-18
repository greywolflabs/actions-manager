"""Where a reusable workflow is delivered is decided by who owns it.

A workflow a project owns lives in that project's repositories, whether or not
it carries the reusable label — so a PR campaign delivers it there, with the
rest of the files that project changed. Only a workflow LINKED from a Reusable
Workflow Project belongs in that project's repository instead.

`_get_reusable_workflow_repo` handled two cases — an RWX project (its own repo)
and a standard project with a link (the linked project's repo) — and a standard
project that owns its reusable workflows is neither. It fell through both to
`f"{user}/am-reuseable-workflow"`, so the workflow was delivered to a repository
with nothing to do with the project and never appeared in the campaign.
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
    Base,
    Account,
    LinkedReusableWorkflow,
    Project,
    ProjectPullRequest,
    ProjectRepo,
    ProjectWorkflow,
    Repo,
    Workflow,
)
from main import app
from projects import get_db as projects_get_db
from tests.delivery_records import record_pr_carries
from workflows import get_db as workflows_get_db

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_owned_reusable_workflow_delivery.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

USER = "testuser"
CALLER_REPO = "testuser/caller-repo"
RWX_REPO = "testuser/rwx-repo"


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


@pytest.fixture
def client(authenticate_client):
    app.dependency_overrides[projects_get_db] = override_get_db
    app.dependency_overrides[workflows_get_db] = override_get_db
    with mock.patch("mode_validation.validate_startup_configuration"):
        with TestClient(app) as c:
            authenticate_client(c, USER, TestingSessionLocal)
            yield c
    app.dependency_overrides.pop(projects_get_db, None)
    app.dependency_overrides.pop(workflows_get_db, None)


def _project(db, account, name, code, project_type):
    project = Project(
        project_name=name, project_code=code, user_id=account.user_id,
        project_type=project_type, branch_option="default", use_prefix=True,
        pr_state="draft", reusable_workflows_enabled=(project_type == "rwx"),
    )
    db.add(project)
    db.flush()
    return project


def _repo(db, project, repo_name):
    repo = Repo(repo_name=repo_name)
    db.add(repo)
    db.flush()
    db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    return repo


def _workflow(db, project, name, reusable):
    workflow = Workflow(
        workflow_name=name, workflow_yaml="on: push",
        reusable_workflow=reusable, workflow_status="new",
    )
    db.add(workflow)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    return workflow


def _campaign(client, project_name, repos, regular=None, reusable=None):
    body = {"github_user": USER, "project_name": project_name, "selected_repos": repos}
    if regular is not None:
        body["selected_workflows"] = regular
    if reusable is not None:
        body["selected_reusable_workflows"] = reusable
    return client.post("/api/create-pull-requests", json=body)


@pytest.fixture
def caller_project_owning_a_reusable_workflow():
    """A Caller Workflow Project whose reusable workflow sits in its own repo."""
    db = TestingSessionLocal()
    try:
        account = Account(github_user=USER, github_email="t@t.com", account_type="pro")
        db.add(account)
        db.flush()
        project = _project(db, account, "CallerProject", "CALL", "standard")
        _repo(db, project, CALLER_REPO)
        _workflow(db, project, "build-stuff", False)
        _workflow(db, project, "shared-workflow", True)
        db.commit()
    finally:
        db.close()


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_owned_reusable_workflow_goes_to_the_projects_own_repo(
    mock_tokens, mock_regular, mock_reusable, client,
    caller_project_owning_a_reusable_workflow,
):
    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = {}
    mock_reusable.return_value = {}

    response = _campaign(
        client, "CallerProject", [CALLER_REPO],
        regular=["build-stuff"], reusable=["shared-workflow"],
    )

    assert response.status_code in (200, 400), response.text
    assert mock_regular.called
    kwargs = mock_regular.call_args.kwargs
    assert list(kwargs["repo_names"]) == [CALLER_REPO]
    assert sorted(w["name"] for w in kwargs["workflows"]) == ["build-stuff", "shared-workflow"]
    # Carried so the commit stamps the delivered SHA on the reusable row (#2085).
    assert {w["name"]: w["is_reusable"] for w in kwargs["workflows"]} == {
        "build-stuff": False, "shared-workflow": True,
    }

    # Never to {user}/am-reuseable-workflow, which is not this project's repo.
    assert not mock_reusable.called


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_an_owned_reusable_workflow_alone_still_targets_the_projects_repos(
    mock_tokens, mock_regular, mock_reusable, client,
    caller_project_owning_a_reusable_workflow,
):
    """Selecting only the reusable workflow must not make the run target nothing."""
    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = {}
    mock_reusable.return_value = {}

    response = _campaign(client, "CallerProject", [CALLER_REPO], reusable=["shared-workflow"])

    assert response.status_code in (200, 400), response.text
    assert mock_regular.called
    kwargs = mock_regular.call_args.kwargs
    assert list(kwargs["repo_names"]) == [CALLER_REPO]
    assert [w["name"] for w in kwargs["workflows"]] == ["shared-workflow"]
    assert not mock_reusable.called


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_linked_reusable_workflow_still_goes_to_the_rwx_repo(
    mock_tokens, mock_regular, mock_reusable, client,
):
    """The case the single-repo path exists for, and which must not change."""
    db = TestingSessionLocal()
    try:
        account = Account(github_user=USER, github_email="t@t.com", account_type="pro")
        db.add(account)
        db.flush()
        rwx = _project(db, account, "SharedWorkflows", "RWX1", "rwx")
        _repo(db, rwx, RWX_REPO)
        shared = _workflow(db, rwx, "linked-shared", True)
        caller = _project(db, account, "CallerLinked", "CLNK", "standard")
        _repo(db, caller, CALLER_REPO)
        db.add(LinkedReusableWorkflow(
            standard_project_id=caller.project_id,
            rwx_project_id=rwx.project_id,
            workflow_id=shared.workflow_id,
        ))
        db.commit()
    finally:
        db.close()

    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = {}
    mock_reusable.return_value = {}

    response = _campaign(client, "CallerLinked", [CALLER_REPO], reusable=["linked-shared"])

    assert response.status_code in (200, 400), response.text
    assert mock_reusable.called, "a linked reusable workflow must still reach the RWX repo"
    assert mock_reusable.call_args.kwargs["reusable_repo"] == RWX_REPO
    assert [w["name"] for w in mock_reusable.call_args.kwargs["rxworkflows"]] == ["linked-shared"]


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_an_rwx_project_delivers_its_own_workflows_unchanged(
    mock_tokens, mock_regular, mock_reusable, client,
):
    db = TestingSessionLocal()
    try:
        account = Account(github_user=USER, github_email="t@t.com", account_type="pro")
        db.add(account)
        db.flush()
        rwx = _project(db, account, "SharedWorkflows", "RWX1", "rwx")
        _repo(db, rwx, RWX_REPO)
        _workflow(db, rwx, "rwx-own", True)
        db.commit()
    finally:
        db.close()

    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = {}
    mock_reusable.return_value = {}

    response = _campaign(client, "SharedWorkflows", [RWX_REPO], reusable=["rwx-own"])

    assert response.status_code in (200, 400), response.text
    assert mock_reusable.called
    assert mock_reusable.call_args.kwargs["reusable_repo"] == RWX_REPO
    assert [w["name"] for w in mock_reusable.call_args.kwargs["rxworkflows"]] == ["rwx-own"]


@pytest.fixture
def caller_owning_and_linking_deploy():
    """A caller project that owns a reusable ``deploy`` and links an RWX ``deploy``.

    The linked one surfaces as ``AM_RWX1_deploy.yml``; both normalise to
    ``deploy``, which is how selecting one used to drag in the other (#2309).
    """
    db = TestingSessionLocal()
    try:
        account = Account(github_user=USER, github_email="t@t.com", account_type="pro")
        db.add(account)
        db.flush()
        rwx = _project(db, account, "SharedWorkflows", "RWX1", "rwx")
        _repo(db, rwx, RWX_REPO)
        linked = _workflow(db, rwx, "deploy", True)
        caller = _project(db, account, "CallerBoth", "CBTH", "standard")
        _repo(db, caller, CALLER_REPO)
        _workflow(db, caller, "deploy", True)
        db.add(LinkedReusableWorkflow(
            standard_project_id=caller.project_id,
            rwx_project_id=rwx.project_id,
            workflow_id=linked.workflow_id,
        ))
        db.commit()
    finally:
        db.close()


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_selecting_the_owned_name_does_not_drag_in_the_linked_one(
    mock_tokens, mock_regular, mock_reusable, client, caller_owning_and_linking_deploy,
):
    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = {}
    mock_reusable.return_value = {}

    response = _campaign(client, "CallerBoth", [CALLER_REPO], reusable=["deploy"])

    assert response.status_code in (200, 400), response.text
    assert [(w["name"], w["is_reusable"]) for w in mock_regular.call_args.kwargs["workflows"]] == [
        ("deploy", True),
    ]
    assert not mock_reusable.called, "the linked deploy was not selected"


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_selecting_the_linked_display_name_does_not_drag_in_the_owned_one(
    mock_tokens, mock_regular, mock_reusable, client, caller_owning_and_linking_deploy,
):
    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = {}
    mock_reusable.return_value = {}

    response = _campaign(client, "CallerBoth", [CALLER_REPO], reusable=["AM_RWX1_deploy.yml"])

    assert response.status_code in (200, 400), response.text
    assert mock_reusable.called
    assert mock_reusable.call_args.kwargs["reusable_repo"] == RWX_REPO
    assert not mock_regular.called, "the owned deploy was not selected"


def _deploy_statuses():
    """(owned deploy status, linked RWX deploy status)."""
    db = TestingSessionLocal()
    try:
        caller = db.query(Project).filter_by(project_name="CallerBoth").first()
        rwx = db.query(Project).filter_by(project_name="SharedWorkflows").first()

        def status_in(project):
            return (
                db.query(Workflow).join(ProjectWorkflow)
                .filter(ProjectWorkflow.project_id == project.project_id,
                        Workflow.workflow_name == "deploy")
                .one().workflow_status
            )
        return status_in(caller), status_in(rwx)
    finally:
        db.close()


@pytest.mark.parametrize(
    "project_names, reusable_names, expected",
    [
        (["deploy"], [], ("under_review", "new")),
        ([], ["deploy"], ("new", "under_review")),
    ],
    ids=["owned-delivered", "linked-delivered"],
)
def test_a_campaign_marks_only_the_deploy_it_delivered(
    caller_owning_and_linking_deploy, project_names, reusable_names, expected,
):
    """#2309: statuses were set by bare name across both kinds, so delivering
    the owned deploy also locked the RWX one under_review for every project
    linking it, and the reverse."""
    from workflows import _update_linked_workflow_statuses

    db = TestingSessionLocal()
    try:
        caller = db.query(Project).filter_by(project_name="CallerBoth").first()
        _update_linked_workflow_statuses(db, caller, project_names, reusable_names)
    finally:
        db.close()

    assert _deploy_statuses() == expected


def _save_and_push(client, rxworkflows):
    return client.post("/api/update-project-files", json={
        "project_name": "CallerProject",
        "repo_names": [CALLER_REPO],
        "workflows": [],
        "rxworkflows": rxworkflows,
    })


def _opened(repo):
    """What the commit chain reports for a pull request it opened on *repo*'s main."""
    return {f"{repo} on main": {
        "status": "pr_created", "pr_number": 7, "pr_url": f"https://github.com/{repo}/pull/7",
        "branch_name": "actions-manager/call-main",
    }}


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_save_and_push_sends_an_owned_reusable_workflow_to_the_projects_repos(
    mock_tokens, mock_regular, mock_reusable, client,
    caller_project_owning_a_reusable_workflow,
):
    """#2309: a save that pushes an edited owned reusable workflow updated the
    fallback repository, so the project's repo kept the old copy and drift,
    which reads it there (#2085), reported it drifted."""
    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = _opened(CALLER_REPO)

    response = _save_and_push(client, [{"name": "shared-workflow", "content": "on: workflow_call"}])

    assert response.status_code == 200, response.text
    kwargs = mock_regular.call_args.kwargs
    assert kwargs["repo_names"] == [CALLER_REPO]
    assert [(w["name"], w["is_reusable"]) for w in kwargs["workflows"]] == [("shared-workflow", True)]
    assert not mock_reusable.called


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_save_and_push_of_a_workflow_the_project_does_not_have_pushes_nothing(
    mock_tokens, mock_regular, mock_reusable, client,
    caller_project_owning_a_reusable_workflow,
):
    """It pushed the request's copy of any name to the fallback reusable
    repository. It delivers what the project stores, so a name the project
    neither owns nor links selects nothing."""
    mock_tokens[USER] = "fake_token"

    response = _save_and_push(client, [{"name": "not-this-projects", "content": "on: workflow_call"}])

    assert response.status_code == 400, response.text
    assert not mock_regular.called
    assert not mock_reusable.called


@pytest.mark.parametrize("use_prefix", [True, False])
@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_save_and_push_names_the_file_the_way_campaigns_and_drift_do(
    mock_tokens, mock_regular, mock_reusable, client,
    caller_project_owning_a_reusable_workflow, use_prefix,
):
    """The save path left use_prefix at its default, so a no-prefix project's
    push wrote AM_CALL_shared-workflow.yml while campaigns and drift use
    shared-workflow.yml, and the real file kept reporting drift."""
    db = TestingSessionLocal()
    try:
        db.query(Project).filter_by(project_name="CallerProject").update({"use_prefix": use_prefix})
        db.commit()
    finally:
        db.close()
    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = _opened(CALLER_REPO)

    response = _save_and_push(client, [{"name": "shared-workflow", "content": "on: workflow_call"}])

    assert response.status_code == 200, response.text
    assert mock_regular.call_args.kwargs["use_prefix"] is use_prefix


def test_the_editor_save_reports_a_workflow_its_trigger_moved(
    client, caller_project_owning_a_reusable_workflow,
):
    """#2318: the UI groups workflows by kind, so a save that changed one's
    kind says which, and the editor moves it without a reload. A workflow that
    kept its kind is not reported."""
    response = client.post("/api/save-workflows", json={
        "project_name": "CallerProject",
        "workflows": [{"name": "build-stuff", "content": "on: workflow_call"}],
        "rxworkflows": [{"name": "shared-workflow", "content": "on: workflow_call"}],
    })

    assert response.status_code == 200, response.text
    assert response.json()["kind_changes"] == [
        {"name": "build-stuff", "is_reusable": True, "workflow_status": "committed_locally"},
    ]


def test_the_project_save_reports_a_workflow_its_trigger_moved(
    client, caller_project_owning_a_reusable_workflow,
):
    """The toolbar save lists workflows by section too; dropping workflow_call
    from a reusable one makes it regular there as well."""
    db = TestingSessionLocal()
    try:
        project_id = db.query(Project).filter_by(project_name="CallerProject").one().project_id
    finally:
        db.close()

    response = client.put(
        f"/api/projects/{project_id}/",
        json={
            "github_user": USER,
            "project_name": "CallerProject",
            "selected_repos": [CALLER_REPO],
            "workflows": [{"name": "build-stuff", "content": "on: push"}],
            "rxworkflows": [{"name": "shared-workflow", "content": "on: push\n# no longer called"}],
            "branch_regex": "",
            "branch_option": "default",
            "branch_max_age_days": 30,
            "reusable_workflows_enabled": False,
            "use_prefix": True,
        },
        headers={"X-GitHub-User": USER},
    )

    assert response.status_code == 200, response.text
    assert response.json()["kind_changes"] == [
        {"name": "shared-workflow", "is_reusable": False, "workflow_status": "committed_locally"},
    ]


def test_a_refused_kind_change_writes_nothing_from_the_same_save(
    client, caller_project_owning_a_reusable_workflow,
):
    """create_or_update_workflow commits per workflow, so the refusal has to come
    before the first one is written, not when the save reaches the workflow."""
    db = TestingSessionLocal()
    try:
        project = db.query(Project).filter_by(project_name="CallerProject").one()
        pr = ProjectPullRequest(
            project_id=project.project_id, repo_name=CALLER_REPO, pr_number=3,
            pr_url="https://github.com/testuser/caller-repo/pull/3", pr_state="open",
            branch_name="am-branch", target_branch="main", workflow_names="build-stuff",
        )
        db.add(pr)
        build_stuff = db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id, Workflow.workflow_name == "build-stuff",
        ).one()
        record_pr_carries(db, pr, build_stuff)
    finally:
        db.close()

    response = client.post("/api/save-workflows", json={
        "project_name": "CallerProject",
        "workflows": [
            {"name": "written-first", "content": "on: push"},
            {"name": "build-stuff", "content": "on: workflow_call"},
        ],
        "rxworkflows": [],
    })

    assert response.status_code == 409, response.text
    assert "open PR campaign" in response.json()["detail"]
    db = TestingSessionLocal()
    try:
        assert db.query(Workflow).filter_by(workflow_name="written-first").count() == 0
    finally:
        db.close()


def test_creating_a_project_reports_a_workflow_its_trigger_made_reusable(
    client, caller_project_owning_a_reusable_workflow,
):
    response = client.post("/api/projects/", json={
        "github_user": USER,
        "project_name": "NewCaller",
        "selected_repos": ["testuser/new-repo"],
        "workflows": [{"name": "deploy", "content": "on: workflow_call"}],
        "rxworkflows": [],
        "branch_regex": "",
        "branch_option": "default",
        "reusable_workflows_enabled": False,
        "project_type": "standard",
    })

    assert response.status_code == 200, response.text
    assert response.json()["kind_changes"] == [
        {"name": "deploy", "is_reusable": True, "workflow_status": "new"},
    ]


def _push(client, repo_names, workflows=None, rxworkflows=None, project_name="CallerProject"):
    return client.post("/api/update-project-files", json={
        "project_name": project_name,
        "repo_names": repo_names,
        "workflows": workflows if workflows is not None else [{"name": "build-stuff", "content": "on: push"}],
        "rxworkflows": rxworkflows or [],
    })


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_save_and_push_writes_only_to_the_projects_repositories(
    mock_tokens, mock_regular, mock_reusable, client,
    caller_project_owning_a_reusable_workflow,
):
    """#2316: the repositories came straight from the request, so an editor of
    one project could open branches and pull requests carrying its workflows
    in any repository their token can write to. The rest are skipped, and
    named in the response so the client can say they got nothing."""
    mock_tokens[USER] = "fake_token"
    mock_regular.return_value = _opened(CALLER_REPO)

    response = _push(client, [f" {CALLER_REPO} ", "someone-else/unrelated"])

    assert response.status_code == 200, response.text
    # Owned the way deletion judges it, surrounding whitespace and all.
    assert mock_regular.call_args.kwargs["repo_names"] == [CALLER_REPO]
    assert response.json()["skipped_repos"] == ["someone-else/unrelated"]


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_save_and_push_to_no_project_repository_is_refused(
    mock_tokens, mock_regular, mock_reusable, client,
    caller_project_owning_a_reusable_workflow,
):
    """Reporting "nothing processed" here read as success to every client."""
    mock_tokens[USER] = "fake_token"

    response = _push(client, ["someone-else/unrelated"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == 400
    assert "None of the selected repositories belong to this project" in body["error"]
    assert not mock_regular.called


@mock.patch("workflows._process_reusable_workflows_update")
@mock.patch("workflows._process_regular_workflows_update")
@mock.patch("workflows.user_tokens", new_callable=dict)
def test_a_push_with_nothing_for_the_projects_repositories_is_not_refused(
    mock_tokens, mock_regular, mock_reusable, client, caller_owning_and_linking_deploy,
):
    """A linked reusable workflow goes to its RWX repository, so the repositories
    the editor sends alongside it do not decide anything."""
    mock_tokens[USER] = "fake_token"
    mock_reusable.return_value = _opened(RWX_REPO)

    response = _push(client, ["someone-else/unrelated"], workflows=[], project_name="CallerBoth",
                     rxworkflows=[{"name": "AM_RWX1_deploy.yml", "content": "on: workflow_call"}])

    assert response.status_code == 200, response.text
    assert "error" not in response.json()
    assert mock_reusable.call_args.kwargs["reusable_repo"] == RWX_REPO
    assert not mock_regular.called


def test_the_unscoped_sync_workflow_route_is_gone(client, caller_project_owning_a_reusable_workflow):
    """#2329: it copied a workflow into any repositories the body named, with no
    project behind the write, and nothing called it."""
    response = client.post("/api/sync-workflow", json={
        "repo_names": ["someone-else/unrelated"], "workflow_name": "ci",
    })

    assert response.status_code == 404


def test_the_unused_workflow_status_route_is_gone(client, caller_project_owning_a_reusable_workflow):
    """It fetched run status one GitHub call per workflow per repository, for any
    repositories the query named. Build metrics replaced it and nothing called it."""
    response = client.get("/api/workflow-status", params={
        "project_name": "CallerProject", "repo_names": "someone-else/unrelated", "workflow_names": "ci",
    })

    assert response.status_code == 404
