"""A rename is delivered as a rename: the campaign removes the file where it was delivered.

Git stores no rename: it is a remove plus an add that the diff detects by
content. So a campaign carries both halves on the same AM branch — then
GitHub's pull request shows a rename and merging it completes the change.

The file to remove is not a name kept beside the workflow. It is what the
delivery record says was delivered on each (repo, branch), so every target
removes its own copy, however many times the workflow was renamed since, and
whichever other targets a merged campaign has already moved on. The name kept
beside the workflow was forgotten by the first merged campaign, which orphaned
the old file in every repository that campaign had not targeted.
"""
import os
import sys
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deliveries  # noqa: E402
import workflows as wf  # noqa: E402
from models import Account, Base, Project, ProjectRepo, ProjectWorkflow, Repo, Workflow  # noqa: E402
from tests.campaign_harness import USER, FakeGitHub, run_campaign  # noqa: E402
from tests.delivery_records import record_delivered  # noqa: E402

CODE = "DLV"
APP, WEB = "deliver-org/app", "deliver-org/web"
CONTENT = "name: x\non: push\n"

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _path(name):
    return f".github/workflows/AM_{CODE}_{name}.yml"


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


@pytest.fixture
def project(db):
    account = Account(github_user=USER, github_email="r@example.com", account_type="free")
    db.add(account)
    db.flush()
    project = Project(project_name="renames", project_code=CODE, user_id=account.user_id,
                      project_type="standard", use_prefix=True, branch_option="default", pr_state="draft")
    db.add(project)
    db.flush()
    for name in (APP, WEB):
        repo = Repo(repo_name=name)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.commit()
    return project


def _workflow(db, project, name, content=CONTENT):
    workflow = Workflow(workflow_name=name, workflow_yaml=content, workflow_status="new")
    db.add(workflow)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    return workflow


def _rename(db, project, old, new):
    workflow = db.query(Workflow).filter_by(workflow_name=old).one()
    wf.create_or_update_workflow(db, wf.WorkflowSchema(name=new, content=workflow.workflow_yaml, original_name=old),
                                 project.project_id, False, USER)


def _merge_all(db, github, prs):
    for pr in prs:
        github.merge(db, pr)


def _deliver(db, github, project, *repos):
    """A campaign delivering every workflow to *repos*, merged."""
    _merge_all(db, github, run_campaign(db, github, project, repos))
    github.deletes.clear()


def _deletes(github):
    return [(repo, path) for repo, _am_branch, path in github.deletes]


def test_the_campaign_removes_the_delivered_path_not_a_guessed_one(db, github, project):
    """ci -> ci2 -> ci3 before delivering removes ci: ci2 never reached GitHub."""
    _workflow(db, project, "ci")
    _deliver(db, github, project, APP)
    _rename(db, project, "ci", "ci2")
    _rename(db, project, "ci2", "ci3")

    [pr] = run_campaign(db, github, project, [APP])

    assert _deletes(github) == [(APP, _path("ci"))]
    assert github.paths(APP, pr.branch_name) == {_path("ci3")}, "both halves are on the campaign branch"


def test_renaming_back_removes_nothing(db, github, project):
    """ci -> ci2 -> ci writes the delivered file again; removing it would take the workflow off GitHub."""
    _workflow(db, project, "ci")
    _deliver(db, github, project, APP)
    _rename(db, project, "ci", "ci2")
    _rename(db, project, "ci2", "ci")

    [pr] = run_campaign(db, github, project, [APP])

    assert github.deletes == []
    assert github.paths(APP, pr.branch_name) == {_path("ci")}


def test_a_rename_delivered_to_some_repos_still_removes_the_old_file_in_the_others(db, github, project):
    """The bug: merging a campaign to acme/app forgot the old name, so the next
    campaign, to acme/web, wrote the new file there and left the old one."""
    _workflow(db, project, "ci")
    _deliver(db, github, project, APP, WEB)
    _rename(db, project, "ci", "ci2")
    _deliver(db, github, project, APP)

    _merge_all(db, github, run_campaign(db, github, project, [WEB]))

    assert _deletes(github) == [(WEB, _path("ci"))]
    assert github.paths(APP) == github.paths(WEB) == {_path("ci2")}


def test_a_closed_rename_campaign_is_carried_by_the_next(db, github, project):
    """A closed campaign removed nothing from main, so the next one removes the old file again."""
    _workflow(db, project, "ci")
    _deliver(db, github, project, APP)
    _rename(db, project, "ci", "ci2")
    [closed] = run_campaign(db, github, project, [APP])
    wf.apply_pr_outcome(db, closed, "closed")
    github.deletes.clear()

    _merge_all(db, github, run_campaign(db, github, project, [APP]))

    assert _deletes(github) == [(APP, _path("ci"))]
    assert github.paths(APP) == {_path("ci2")}


def test_swapped_names_never_delete_a_file_this_branch_writes(db, github, project):
    """a and b trade names: each is delivered where the other is now written, and neither may be removed."""
    _workflow(db, project, "a", "name: a\non: push\n")
    _workflow(db, project, "b", "name: b\non: push\n")
    _deliver(db, github, project, APP)
    _rename(db, project, "a", "tmp")
    _rename(db, project, "b", "a")
    _rename(db, project, "tmp", "b")

    _merge_all(db, github, run_campaign(db, github, project, [APP]))

    assert github.deletes == []
    assert github.files[(APP, "main", _path("a"))].startswith("name: b")
    assert github.files[(APP, "main", _path("b"))].startswith("name: a")


# --- the commit loop: both halves, in order, reported honestly --------------


def _commit(db, project, *, delete_outcome=(wf.DELETE_DELETED, None), status=201):
    """ci-v2, delivered to APP on main as ci, through the campaign's commit loop with GitHub mocked."""
    workflow = _workflow(db, project, "ci-v2")
    record_delivered(db, workflow, APP, "main", sha="b" * 40, path=_path("ci"))
    entry = {"id": workflow.workflow_id, "name": "ci-v2", "content": CONTENT}
    with patch("workflows._update_workflow_to_github", return_value=(status, "c" * 40)), \
            patch("workflows._update_workflow_git_hash"), \
            patch("workflows._delete_path_from_branch", return_value=delete_outcome) as deleter:
        committed, errors = wf._commit_workflows_to_branch(
            [entry], "deliver-org", "app", CODE, "AM_DLV_update", {}, APP, USER, db, True,
            target=(APP, "main"),
        )
    return committed, errors, deleter


def test_the_old_file_is_removed_from_the_campaign_branch(db, project):
    committed, errors, deleter = _commit(db, project)

    assert (committed, errors) == (["ci-v2"], [])
    args = deleter.call_args[0]
    assert (args[2], args[3]) == (_path("ci"), "AM_DLV_update")


def test_an_unchanged_new_file_still_removes_the_old_one(db, project):
    """204 means the add already landed — from a campaign closed before the remove did."""
    _, errors, deleter = _commit(db, project, status=204)

    assert errors == []
    deleter.assert_called_once()


def test_an_absent_old_file_is_not_an_error(db, project):
    """Removed by hand, or carried by an earlier campaign that was then closed."""
    committed, errors, _ = _commit(db, project, delete_outcome=(wf.DELETE_ABSENT, None))

    assert (committed, errors) == (["ci-v2"], [])


def test_a_failed_removal_is_reported_rather_than_swallowed(db, project):
    """Otherwise the campaign reports success while the old file is still there."""
    committed, errors, _ = _commit(db, project, delete_outcome=(wf.DELETE_FAILED, "ci: HTTP 403 - forbidden"))

    assert committed == ["ci-v2"], "the add is counted once even though two files changed"
    assert errors == ["ci: HTTP 403 - forbidden"]


def test_the_old_file_is_only_removed_after_the_new_one_lands(db, project):
    """Removing first would leave the workflow absent if the write then failed."""
    _, errors, deleter = _commit(db, project, status=500)

    assert errors == ["ci-v2: HTTP 500"]
    deleter.assert_not_called()


def test_a_target_never_delivered_to_removes_nothing(db, project):
    """A file never delivered to (repo, branch) left nothing there, whatever it was called."""
    workflow = _workflow(db, project, "ci-v2")

    assert deliveries.superseded_path(db, deliveries.WORKFLOW, workflow.workflow_id, APP, "main",
                                      _path("ci-v2")) is None


def test_adopting_github_s_copy_after_a_local_rename_reads_it_where_it_was_delivered(db, github, project):
    """Drift after a local rename is reported at the delivered path, so Keep GitHub
    version reads it there, and the rename still waits for a campaign: marking it
    synced would leave the old name in GitHub with nothing left to deliver."""
    _workflow(db, project, "ci")
    _deliver(db, github, project, APP)
    _rename(db, project, "ci", "ci2")
    workflow = db.query(Workflow).filter_by(workflow_name="ci2").one()
    reads = []

    def github_copy(_owner, _repo, filename, _token, default_branch=None):
        reads.append((filename, default_branch))
        return {"content": "name: x\non: [push]\n", "sha": "edited-on-github"}

    with patch("workflows.get_workflow_from_github", side_effect=github_copy):
        outcome = wf._apply_use_github_resolution(db, USER, workflow, APP, f"AM_{CODE}_ci2.yml", "token",
                                                  branch="main")

    assert outcome["success"] is True
    assert reads == [(f"AM_{CODE}_ci.yml", "main")]
    db.refresh(workflow)
    assert (workflow.workflow_yaml, workflow.workflow_status) == ("name: x\non: [push]", "committed_locally")
    assert deliveries.delivered_path(db, deliveries.WORKFLOW, workflow.workflow_id, APP, "main") == _path("ci")
