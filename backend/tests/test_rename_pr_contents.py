"""A rename reaches GitHub as two file changes on one campaign branch.

The user's requirement, stated plainly: opening the pull request must do two
things — write the new filename, and delete the old one — so the PR carries two
changes rather than a new file plus an orphan nobody removes.

The old one is where the delivery record says the workflow was delivered on the
target. These assert on the HTTP calls themselves: the PUT path, the DELETE
path, and that both name the same AM branch.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import Account, Base, Project, ProjectWorkflow, Workflow  # noqa: E402
from tests.delivery_records import record_delivered  # noqa: E402
from workflows import _commit_workflows_to_branch  # noqa: E402

OWNER, REPO, CODE, AM_BRANCH = "acme", "app", "MYP1", "AM_MYP1_update"

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _delivered(db, name, delivered_file, use_prefix):
    """Workflow *name*, delivered to acme/app on main as *delivered_file*; returns its campaign dict."""
    account = Account(github_user="octocat", github_email="o@example.com", account_type="free")
    db.add(account)
    db.flush()
    project = Project(project_name="pr-contents", project_code=CODE, user_id=account.user_id,
                      project_type="standard", use_prefix=use_prefix)
    workflow = Workflow(workflow_name=name, workflow_yaml="name: x\non: push", workflow_status="committed_locally")
    db.add_all([project, workflow])
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    if delivered_file:
        record_delivered(db, workflow, f"{OWNER}/{REPO}", "main", sha="a" * 40,
                         path=f".github/workflows/{delivered_file}")
    return {"id": workflow.workflow_id, "name": name, "content": "name: x\non: push"}


def _run(name, delivered_file):
    """Drive the campaign's commit loop with GitHub's HTTP surface mocked."""
    db = Session()
    puts, deletes = [], []

    def fake_put(url, *a, **kwargs):
        puts.append((url, kwargs.get("json", {})))
        return MagicMock(status_code=201, json=lambda: {"content": {"sha": "f" * 40}})

    def fake_delete(url, **kwargs):
        deletes.append((url, kwargs.get("json", {})))
        return MagicMock(status_code=200, json=lambda: {})

    def fake_get(url, *a, **kw):
        # The new path is absent (so the write is a create, not an update); the
        # old path is present, so the delete has a sha to send. Everything else
        # — the AM branch existence check — answers 200.
        if "AM_MYP1_renamed.yml" in url:
            return MagicMock(status_code=404, json=lambda: {})
        return MagicMock(status_code=200, json=lambda: {"sha": "a" * 40})

    try:
        workflow_dict = _delivered(db, name, delivered_file, True)
        with patch("workflows.github_put", side_effect=fake_put), \
             patch("workflows.requests.delete", side_effect=fake_delete), \
             patch("workflows.github_get", side_effect=fake_get), \
             patch("workflows.requests.get", side_effect=fake_get), \
             patch("workflows._update_workflow_git_hash"):
            committed, errors = _commit_workflows_to_branch(
                [workflow_dict], OWNER, REPO, CODE, AM_BRANCH,
                {}, f"{OWNER}/{REPO}", "octocat", db, True, target=(f"{OWNER}/{REPO}", "main"),
            )
        return committed, errors, puts, deletes
    finally:
        db.close()


def test_opening_the_pr_writes_the_new_file_and_deletes_the_old_one():
    committed, errors, puts, deletes = _run("renamed", "AM_MYP1_okokok.yml")

    assert errors == []
    assert committed == ["renamed"]

    assert len(puts) == 1, "the new filename is written once"
    assert ".github/workflows/AM_MYP1_renamed.yml" in puts[0][0]
    assert puts[0][1]["branch"] == AM_BRANCH

    assert len(deletes) == 1, "the old filename is removed — this is the second change"
    assert ".github/workflows/AM_MYP1_okokok.yml" in deletes[0][0]
    assert deletes[0][1]["branch"] == AM_BRANCH
    assert deletes[0][1]["sha"] == "a" * 40


def test_an_ordinary_edit_deletes_nothing():
    _, errors, puts, deletes = _run("renamed", "AM_MYP1_renamed.yml")

    assert errors == []
    assert len(puts) == 1
    assert deletes == [], "only a rename removes a file"


# --- the delete must never target the path the same campaign just wrote ------


def _run_unprefixed(name, delivered_file):
    """Same as _run, without the AM_<CODE>_ prefix, where names collide."""
    db = Session()
    puts, deletes = [], []

    def fake_put(url, *a, **kwargs):
        puts.append(url)
        return MagicMock(status_code=201, json=lambda: {"content": {"sha": "f" * 40}})

    def fake_delete(url, **kwargs):
        deletes.append(url)
        return MagicMock(status_code=200, json=lambda: {})

    def fake_get(url, *a, **kw):
        return MagicMock(status_code=200, json=lambda: {"sha": "a" * 40})

    try:
        workflow_dict = _delivered(db, name, delivered_file, False)
        with patch("workflows.github_put", side_effect=fake_put), \
             patch("workflows.requests.delete", side_effect=fake_delete), \
             patch("workflows.github_get", side_effect=fake_get), \
             patch("workflows.requests.get", side_effect=fake_get), \
             patch("workflows._update_workflow_git_hash"):
            _commit_workflows_to_branch(
                [workflow_dict], OWNER, REPO, CODE, AM_BRANCH,
                {}, f"{OWNER}/{REPO}", "octocat", db, False, target=(f"{OWNER}/{REPO}", "main"),
            )
        return puts, deletes
    finally:
        db.close()


def test_adding_the_yml_suffix_does_not_delete_the_file_just_written():
    """Without a prefix, `ci` and `ci.yml` are both `ci.yml`.

    A guard comparing names saw `ci` != `ci.yml` and removed the file —
    PUT ci.yml then DELETE ci.yml on the same branch. Merging that campaign
    removed the workflow from every target repository while ActionsManager
    reported the rename as delivered. The record holds paths, so the delivered
    `ci.yml` is the one being written.
    """
    puts, deletes = _run_unprefixed("ci.yml", "ci.yml")

    assert len(puts) == 1
    assert puts[0].endswith("/.github/workflows/ci.yml")
    assert deletes == [], "the file just written must not be deleted"


def test_dropping_the_yml_suffix_is_the_same_collision():
    puts, deletes = _run_unprefixed("ci", "ci.yml")

    assert len(puts) == 1
    assert deletes == []


def test_a_case_only_difference_does_not_delete_the_file_just_written():
    """`ci` -> `ci2` -> `CI` is the two-step route the case-only refusal
    recommends, and it leaves `ci.yml` delivered against workflow_name='CI'.
    If GitHub's contents API folds case, deleting `ci.yml` destroys `CI.yml`.
    Leaving an orphan is the cheaper mistake, so the comparison ignores case.
    """
    puts, deletes = _run_unprefixed("CI", "ci.yml")

    assert len(puts) == 1
    assert deletes == []


def test_a_genuine_rename_still_removes_the_old_file():
    """The guard must skip the collision and nothing else."""
    puts, deletes = _run_unprefixed("ci-v2", "ci.yml")

    assert len(puts) == 1
    assert puts[0].endswith("/.github/workflows/ci-v2.yml")
    assert len(deletes) == 1
    assert deletes[0].endswith("/.github/workflows/ci.yml")
