"""Deleting a file from GitHub removes it where the delivery record says it is.

A removal used to aim at the file's current name on the branches the project's
current config resolves (a campaign), or on every branch of the repository
(a direct delete, which listed them all and read each one first). So a file
renamed or moved before it was deleted left its delivered copy behind, a branch
the project no longer targets was never cleaned, and a direct delete cost
1 + 2 calls per branch. Now each (repo, branch) the record holds the file
delivered to has it removed at its recorded path, sending the recorded blob SHA:
one DELETE where the record is right. Only a file the record has never seen
falls back to the configured branches, at its current path.
"""
import ast
import inspect
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deliveries  # noqa: E402
import workflows as wf  # noqa: E402
from config import GITHUB_TIMEOUT_SECONDS  # noqa: E402
from deliveries import blob_sha  # noqa: E402
from models import Account, Base, FileDelivery, Project, ProjectRepo, ProjectWorkflow, Repo, Workflow  # noqa: E402
from tests.campaign_harness import BRANCH, USER, FakeGitHub, remove_workflow, run_campaign  # noqa: E402

CODE = "RMV"
APP, WEB = "remove-org/app", "remove-org/web"
RELEASE = "release/1"
CONTENT = "name: x\non: push\n"

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _path(name, code=CODE):
    return f".github/workflows/AM_{code}_{name}.yml"


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def github():
    return FakeGitHub()


def _project(db, name, code, project_type, *repos):
    owner = db.query(Account).filter_by(github_user=USER).first()
    if owner is None:
        owner = Account(github_user=USER, github_email="rm@example.com", account_type="free")
        db.add(owner)
        db.flush()
    project = Project(project_name=name, project_code=code, user_id=owner.user_id, project_type=project_type,
                      use_prefix=True, branch_option="default", pr_state="draft")
    db.add(project)
    db.flush()
    for repo_name in repos:
        repo = Repo(repo_name=repo_name)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.commit()
    return project


@pytest.fixture
def project(db):
    return _project(db, "removals", CODE, "standard", APP, WEB)


def _workflow(db, project, name, reusable=False, **columns):
    workflow = Workflow(workflow_name=name, workflow_yaml=CONTENT, reusable_workflow=reusable,
                        workflow_status=columns.pop("workflow_status", "new"), **columns)
    db.add(workflow)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    return workflow


def _merge_all(db, github, prs):
    for pr in prs:
        github.merge(db, pr)


def _deliver(db, github, project, *repos, **selection):
    """A campaign delivering to *repos*, merged; then a clean slate of calls."""
    _merge_all(db, github, run_campaign(db, github, project, repos, **selection))
    github.deletes.clear()
    github.calls.clear()
    github.timeouts.clear()


def _rename(db, project, old, new):
    workflow = db.query(Workflow).filter_by(workflow_name=old).one()
    wf.create_or_update_workflow(db, wf.WorkflowSchema(name=new, content=workflow.workflow_yaml, original_name=old),
                                 project.project_id, False, USER)


def _deletes(github):
    """(repo, path) of every file the removal took out."""
    return sorted((repo, path) for repo, _branch, path in github.deletes)


def _on_github(github, *paths):
    """Each (repo, branch, path) of *paths* still on a branch the project delivers to, not a campaign's own."""
    return sorted(key for key in github.files if key[2] in paths and not key[1].startswith("actions-manager/"))


def _remove(db, github, project, name, repos, delivery):
    """Remove *name* from *repos* by *delivery*; a campaign's pull requests are merged."""
    answer, prs = remove_workflow(db, github, project, name, repos, direct=delivery == "direct")
    _merge_all(db, github, prs)
    return answer, prs


BOTH = pytest.mark.parametrize("delivery", ["campaign", "direct"])


def test_campaign_removal_deletes_the_delivered_path_on_each_recorded_branch(db, github, project):
    workflow = _workflow(db, project, "ci")
    _deliver(db, github, project, APP, WEB)

    answer, prs = remove_workflow(db, github, project, "ci", [APP, WEB])

    assert answer["prs_created"] == 2
    assert {(pr.repo_name, pr.target_branch) for pr in prs} == {(APP, BRANCH), (WEB, BRANCH)}
    # On each PR's own branch, sending the blob the record holds there.
    assert sorted(github.deletes) == sorted((pr.repo_name, pr.branch_name, _path("ci")) for pr in prs)
    assert not any(method == "GET" for method, *_ in github.calls), "the recorded SHA was right"
    db.refresh(workflow)
    assert (workflow.pending_delete, workflow.workflow_status) == (True, "under_review")
    rows = db.query(FileDelivery).filter_by(workflow_id=workflow.workflow_id).all()
    assert {row.pending_delete for row in rows} == {True}

    _merge_all(db, github, prs)

    assert not _on_github(github, _path("ci"))
    assert db.query(Workflow).filter_by(workflow_name="ci").first() is None


@BOTH
def test_a_branch_outside_the_current_config_is_still_cleaned(db, github, project, delivery):
    """Delivered to main and release/1; the project now targets only main. Both copies go."""
    _workflow(db, project, "ci")
    github.branch_config = [BRANCH, RELEASE]
    _deliver(db, github, project, APP)
    github.branch_config = [BRANCH]

    assert _on_github(github, _path("ci")) == [(APP, BRANCH, _path("ci")), (APP, RELEASE, _path("ci"))]

    _, prs = _remove(db, github, project, "ci", [APP], delivery)

    if delivery == "campaign":
        assert sorted(pr.target_branch for pr in prs) == [BRANCH, RELEASE]
    assert not _on_github(github, _path("ci"))
    assert not any(method == "BRANCHES" for method, *_ in github.calls), "the record named the branches"


@BOTH
def test_a_renamed_undelivered_workflow_removal_deletes_the_old_name(db, github, project, delivery):
    """Renamed after delivery and deleted before the rename was delivered: GitHub only ever had the old name."""
    _workflow(db, project, "ci")
    _deliver(db, github, project, APP, WEB)
    _rename(db, project, "ci", "ci-v2")

    _remove(db, github, project, "ci-v2", [APP, WEB], delivery)

    assert _deletes(github) == [(APP, _path("ci")), (WEB, _path("ci"))]
    assert not _on_github(github, _path("ci"), _path("ci-v2"))
    assert not any(path == _path("ci-v2") for *_, path in github.calls), "the new name was never delivered"


@BOTH
def test_a_file_the_record_never_saw_falls_back_to_configured_branches(db, github, project, delivery):
    """Delivered before the record existed: its synced status says it may be on GitHub, at its current name."""
    _workflow(db, project, "legacy", workflow_status="synced_with_github", workflow_git_hash="c" * 40)
    for repo in (APP, WEB):
        github.put_file(repo, _path("legacy"), CONTENT)

    _remove(db, github, project, "legacy", [APP, WEB], delivery)

    assert not _on_github(github, _path("legacy"))
    assert sorted(repo for method, repo, *_ in github.calls if method == "BRANCHES") == [APP, WEB]


def test_a_workflow_the_record_never_saw_and_never_left_new_reaches_no_github(db, github, project):
    """Not on the record and never delivered before it either: there is nothing to remove, nowhere."""
    _workflow(db, project, "draft")

    answer, prs = remove_workflow(db, github, project, "draft", [APP, WEB])

    assert (answer["prs_created"], prs) == (0, [])
    assert github.calls == []
    assert db.query(Workflow).filter_by(workflow_name="draft").first() is None


def test_an_rwx_workflow_can_be_removed_by_campaign(db, github):
    shared = "remove-org/shared-workflows"
    rwx = _project(db, "shared", "RWX", "rwx", shared)
    workflow = _workflow(db, rwx, "deploy", reusable=True)
    _deliver(db, github, rwx, shared, selected_reusable_workflows=["deploy"])
    assert github.files[(shared, BRANCH, _path("deploy", "RWX"))] == CONTENT

    answer, [pr] = remove_workflow(db, github, rwx, "deploy", [shared])

    assert answer["prs_created"] == 1
    assert (pr.repo_name, pr.target_branch) == (shared, BRANCH)
    assert github.deletes == [(shared, pr.branch_name, _path("deploy", "RWX"))]
    db.refresh(workflow)
    assert (workflow.pending_delete, workflow.workflow_status) == (True, "under_review")

    github.merge(db, pr)

    assert not _on_github(github, _path("deploy", "RWX"))
    assert db.query(Workflow).filter_by(workflow_name="deploy").first() is None


def test_direct_removal_lists_no_branches(db, github, project):
    """One DELETE per (repo, branch) the record holds it on: no branch listing, no read."""
    _workflow(db, project, "ci")
    _deliver(db, github, project, APP, WEB)

    answer, _ = remove_workflow(db, github, project, "ci", [APP, WEB], direct=True)

    assert sorted(github.calls) == [("DELETE", APP, BRANCH, _path("ci")), ("DELETE", WEB, BRANCH, _path("ci"))]
    assert answer == {APP: {"deleted": [BRANCH], "failed": {}}, WEB: {"deleted": [BRANCH], "failed": {}}}
    assert not _on_github(github, _path("ci"))


def test_direct_removal_uses_the_recorded_sha_first(db, github, project):
    workflow = _workflow(db, project, "ci")
    _deliver(db, github, project, APP)
    recorded = deliveries.delivered_targets(db, deliveries.WORKFLOW, workflow.workflow_id, _path("ci"))
    assert [place.sha for place in recorded] == [blob_sha(CONTENT)]

    remove_workflow(db, github, project, "ci", [APP], direct=True)

    # Nothing was read, and GitHub answers a SHA that is not the file's with 409.
    assert github.calls == [("DELETE", APP, BRANCH, _path("ci"))]
    assert not _on_github(github, _path("ci"))


def test_a_stale_sha_falls_back_to_reading_it(db, github, project):
    """Someone changed the file on GitHub since it was delivered: GitHub refuses the recorded SHA (409)."""
    workflow = _workflow(db, project, "ci")
    _deliver(db, github, project, APP)
    github.put_file(APP, _path("ci"), CONTENT + "# edited on GitHub\n")

    answer, _ = remove_workflow(db, github, project, "ci", [APP], direct=True)

    assert [call[0] for call in github.calls] == ["DELETE", "GET", "DELETE"]
    assert answer[APP] == {"deleted": [BRANCH], "failed": {}}
    assert not _on_github(github, _path("ci"))
    assert not deliveries.delivered_anywhere(db, deliveries.WORKFLOW, workflow.workflow_id)


def _requests_calls_without_the_timeout(function) -> list:
    """Each ``requests.*(...)`` call in *function*'s source that does not pass timeout=GITHUB_TIMEOUT_SECONDS."""
    tree = ast.parse(inspect.getsource(function).lstrip())
    missing = []
    for node in ast.walk(tree):
        is_requests_call = (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == "requests")
        timeouts = [kw.value for kw in getattr(node, "keywords", []) if kw.arg == "timeout"]
        if is_requests_call and not (timeouts and getattr(timeouts[0], "id", None) == "GITHUB_TIMEOUT_SECONDS"):
            missing.append(ast.unparse(node))
    return missing


def test_every_removal_call_has_a_timeout(db, github, project):
    """``requests`` has no default timeout, so a hung socket would pin the worker."""
    _workflow(db, project, "ci")
    _workflow(db, project, "lint")
    _deliver(db, github, project, APP)
    github.put_file(APP, _path("lint"), CONTENT + "# stale\n")

    remove_workflow(db, github, project, "ci", [APP])
    remove_workflow(db, github, project, "lint", [APP], direct=True)

    assert github.timeouts
    assert set(github.timeouts) == {GITHUB_TIMEOUT_SECONDS}
    for function in (wf._send_delete, wf._delete_path_from_branch, wf._remove_on_branch, wf._remove_directly,
                     wf._delete_directly, wf._commit_removals, wf._open_removal_prs, wf._removal_targets):
        assert _requests_calls_without_the_timeout(function) == [], function.__name__
