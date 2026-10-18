"""Drift is GitHub's copy on (repo, branch) differing from what ActionsManager delivered there.

Each bug test here is one the per-file hash and the two drift state tables
produced, and fails on the develop this replaced: the docstrings say how (a
"missing" variant beside one is a control that already passed there). They
seed the record the way production writes it, and run the drift check the way
Check Now and the sweep do (``run_project_drift_check``) against a faked GitHub.
"""
import hashlib
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
from models import (  # noqa: E402
    Account, Base, Codeowners, FileDelivery, Project, ProjectPRCampaign, ProjectPullRequest, ProjectRepo,
    ProjectWorkflow, Repo, Workflow,
)
from tests.delivery_records import record_delivered, record_pr_carries  # noqa: E402

USER = "record-reader"
APP, WEB = "acme/app", "acme/web"
FILENAME = "ci.yml"
CODEOWNERS_PATH = ".github/CODEOWNERS"
V1 = "name: ci\non: push\n"
V2 = "name: ci\non: [push, pull_request]\n"
V3 = "name: ci\non: push\n# hotfix made on GitHub\n"
CO_V1 = "* @acme/owners\n"
CO_V3 = "* @someone-else\n"

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _blob(content: str) -> str:
    """The blob SHA GitHub reports for *content*, as the fake GitHub below computes it."""
    data = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeGitHub:
    """Every repository's files on main: ``files[(repo, path)]`` is the content."""

    def __init__(self):
        self.files = {}

    def tree(self, owner, repo, _branch, _token, **_conditional):
        prefix = ".github/workflows/"
        return {path[len(prefix):]: _blob(content) for (name, path), content in self.files.items()
                if name == f"{owner}/{repo}" and path.startswith(prefix)}, None

    def workflow(self, owner, repo, filename, _token, **_branch):
        content = self.files.get((f"{owner}/{repo}", f".github/workflows/{filename}"))
        return None if content is None else {"content": content, "sha": _blob(content)}

    def codeowners(self, owner, repo, path, _branch, _token, *_etag):
        content = self.files.get((f"{owner}/{repo}", path))
        return (None, None) if content is None else ({"sha": _blob(content), "content": content}, None)

    def put(self, repo, content, path=f".github/workflows/{FILENAME}"):
        self.files[(repo, path)] = content

    def delete(self, repo, path=f".github/workflows/{FILENAME}"):
        self.files.pop((repo, path), None)


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
    fake = FakeGitHub()
    with patch("workflows.user_tokens", {USER: "token"}), \
            patch("workflows._resolve_branches_for_repo", return_value=["main"]), \
            patch("workflows.fetch_workflow_tree", side_effect=fake.tree), \
            patch("workflows.get_workflow_from_github", side_effect=fake.workflow), \
            patch("workflows._read_file_on_branch", side_effect=fake.codeowners):
        yield fake


@pytest.fixture
def project(db):
    account = Account(github_user=USER, github_email="r@example.com", account_type="free")
    db.add(account)
    db.flush()
    project = Project(project_name="record", project_code="REC", user_id=account.user_id,
                      project_type="standard", use_prefix=False, branch_option="default")
    db.add(project)
    db.commit()
    return project


def _add_repo(db, project, name):
    repo = Repo(repo_name=name)
    db.add(repo)
    db.flush()
    db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.commit()
    return repo


def _delivered_workflow(db, project, github, *repos):
    """ci, delivered as V1 to each of *repos*: committed, on GitHub, and on the record."""
    workflow = Workflow(workflow_name="ci", workflow_yaml=V1, workflow_git_hash=_blob(V1),
                        workflow_status="synced_with_github")
    db.add(workflow)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    for repo in repos:
        github.put(repo, V1)
        record_delivered(db, workflow, repo, "main", sha=_blob(V1))
    return workflow


def _edit_locally(db, project, content):
    """Commit Locally, as the editor's save does."""
    wf.create_or_update_workflow(db, wf.WorkflowSchema(name="ci", content=content), project.project_id, False, USER)


def _check(db, project):
    """A live check; returns the workflow results by repo, and the stored drift by repo."""
    drifted, unchecked, _, _ = wf.run_project_drift_check(db, USER, project)
    assert unchecked == []
    stored = {d.repo: d for d in wf._stored_project_drift_details(db, project)}
    return {d.repo: d for d in drifted}, stored


def test_a_check_never_turns_a_local_edit_into_github_s_blob(db, project, github):
    """Bug 1. A check that found content matching ActionsManager's wrote GitHub's
    SHA over the workflow's hash, the zeroed local-edit marker included. One hash
    stands for every repository: here acme/app holds the edit (someone applied
    it on GitHub), acme/web still holds the delivered V1. On develop the check
    stamps acme/app's blob, and then reads acme/web's untouched delivery as drift."""
    _add_repo(db, project, APP)
    _add_repo(db, project, WEB)
    workflow = _delivered_workflow(db, project, github, APP, WEB)
    _edit_locally(db, project, V2)
    hash_after_the_edit = workflow.workflow_git_hash
    github.put(APP, V2)

    _check(db, project)
    drifted, stored = _check(db, project)

    db.refresh(workflow)
    assert workflow.workflow_git_hash == hash_after_the_edit
    assert (drifted, stored) == ({}, {})


def test_a_preflight_stamp_does_not_make_a_local_edit_drift(db, project, github):
    """Bug 2. Preflight commits the edited workflow to the validation repository,
    and _update_workflow_git_hash stamps that blob on the workflow, replacing the
    local-edit marker. On develop the next check then compares acme/app's
    delivered V1 against that stamp and reports the unsent edit as drift."""
    _add_repo(db, project, APP)
    _delivered_workflow(db, project, github, APP)
    _edit_locally(db, project, V2)

    wf._update_workflow_git_hash(db, "ci", _blob(V2), project.project_code)
    drifted, stored = _check(db, project)

    assert (drifted, stored) == ({}, {})


def test_a_local_edit_does_not_hide_a_change_made_on_github(db, project, github):
    """Bug 3. A zeroed hash read every difference from GitHub as "modified locally
    - pending sync". On develop someone's hotfix to the delivered file goes
    unreported for as long as the edit waits to be delivered."""
    _add_repo(db, project, APP)
    _delivered_workflow(db, project, github, APP)
    _edit_locally(db, project, V2)
    github.put(APP, V3)

    drifted, stored = _check(db, project)

    assert (list(drifted), list(stored)) == ([APP], [APP])
    assert "differs" in stored[APP].message


def _codeowners(db, project, github, git_hash, *repos):
    record = Codeowners(project_id=project.project_id, content=CO_V1, file_path=CODEOWNERS_PATH,
                        git_hash=git_hash, status="synced_with_github")
    db.add(record)
    db.commit()
    for repo in repos:
        github.put(repo, CO_V1, path=CODEOWNERS_PATH)
        record_delivered(db, record, repo, "main", sha=_blob(CO_V1))
    return record


def _stored_codeowners(db, project):
    return [d.repo for d in wf._stored_file_drift_details(db, project, deliveries.CODEOWNERS)]


def test_codeowners_without_a_hash_still_drifts(db, project, github):
    """Bug 4. CODEOWNERS read a NULL hash as a local edit, where a workflow read
    it as no edit at all: the same record state, two answers. On develop a
    delivered CODEOWNERS whose hash is NULL never reports a change made on GitHub."""
    _add_repo(db, project, APP)
    _codeowners(db, project, github, None, APP)
    github.put(APP, CO_V3, path=CODEOWNERS_PATH)

    wf.run_project_drift_check(db, USER, project)

    assert _stored_codeowners(db, project) == [APP]


def test_a_merged_rollback_clears_codeowners_drift_as_it_does_a_workflow_s(db, project, github):
    """Bug 4. A merged PR clears the drift of the files it delivered. CODEOWNERS
    made an exception of rollback PRs that workflows did not, so on develop the
    same merge clears the workflow's drift and leaves CODEOWNERS drifted."""
    _add_repo(db, project, APP)
    workflow = _delivered_workflow(db, project, github, APP)
    record = _codeowners(db, project, github, _blob(CO_V1), APP)
    github.put(APP, V3)
    github.put(APP, CO_V3, path=CODEOWNERS_PATH)
    wf.run_project_drift_check(db, USER, project)
    assert list({d.repo for d in wf._stored_project_drift_details(db, project)}) == [APP]
    assert _stored_codeowners(db, project) == [APP]
    original = ProjectPRCampaign(project_id=project.project_id)
    db.add(original)
    db.flush()
    rollback = ProjectPRCampaign(project_id=project.project_id, rollback_of_campaign_id=original.campaign_id)
    db.add(rollback)
    db.flush()
    pr = ProjectPullRequest(project_id=project.project_id, campaign_id=rollback.campaign_id, repo_name=APP,
                            pr_number=3, pr_url=f"https://github.com/{APP}/pull/3", pr_state="open",
                            branch_name="actions-manager/rec/app/3-main", target_branch="main",
                            workflow_names="ci", file_names=CODEOWNERS_PATH)
    db.add(pr)
    record_pr_carries(db, pr, workflow, record)

    wf.apply_pr_outcome(db, pr, "merged")

    assert wf._stored_project_drift_details(db, project) == []
    assert _stored_codeowners(db, project) == []


def test_a_check_clears_the_deletion_it_reported_once_nothing_is_delivered_there(db, project, github):
    """Bug 4. A missing workflow that was never delivered anywhere got no status
    at all, so the recorder never saw that (repo, branch) again. Here a check
    reports the deletion, then a removal campaign finds the file already gone
    and records it removed. On develop the deletion stays reported for good."""
    _add_repo(db, project, APP)
    workflow = _delivered_workflow(db, project, github, APP)
    github.delete(APP)
    drifted, _ = _check(db, project)
    assert drifted[APP].deleted_in_github is True

    entry = {"kind": deliveries.WORKFLOW, "id": workflow.workflow_id, "path": f".github/workflows/{FILENAME}",
             "sha": None, "delete": True, "absent": True}
    wf._record_absent_removals(db, [entry], APP, "main")
    drifted, stored = _check(db, project)

    assert (drifted, stored) == ({}, {})


@pytest.mark.parametrize("on_github", [None, V3], ids=["missing", "a different file"])
def test_a_repository_it_was_never_delivered_to_is_not_drift(db, project, github, on_github):
    """Bug 5. acme/web was added after the workflow was delivered to acme/app, and
    may already hold a file of its own at the same path. ActionsManager never
    delivered there, so neither is drift. On develop a different file there is
    reported as drift (its content differs and the hash is a real blob); the
    missing file already read as not delivered."""
    _add_repo(db, project, APP)
    _delivered_workflow(db, project, github, APP)
    _add_repo(db, project, WEB)
    if on_github:
        github.put(WEB, on_github)

    drifted, stored = _check(db, project)

    assert (drifted, stored) == ({}, {})
    details = {d.repo: d for d in wf._collect_project_drift_details(db, USER, project)}
    assert "not delivered" in details[WEB].message


@pytest.mark.parametrize("on_github", [None, V3], ids=["missing", "a different file"])
def test_a_check_leaves_what_has_any_record_answers_alone(db, project, github, on_github):
    """The invariant has_any_record's docstring states. Checks of a workflow the
    record has never seen, in the project's own repository, add no row, so a
    delivery made before the record existed to a repository outside the
    project still reads as possible there afterwards. (On develop a check that
    found a different file recorded it as delivered.)"""
    _add_repo(db, project, APP)
    workflow = Workflow(workflow_name="ci", workflow_yaml=V1, workflow_status="committed_locally")
    db.add(workflow)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    if on_github:
        github.put(APP, on_github)
    outside = "someone-else/rwx-source"
    assert wf._may_be_delivered_unrecorded(db, workflow, outside) is True

    wf.run_project_drift_check(db, USER, project)
    wf.run_project_drift_check(db, USER, project)

    assert deliveries.has_any_record(db, deliveries.WORKFLOW, workflow.workflow_id) is False
    assert wf._may_be_delivered_unrecorded(db, workflow, outside) is True
    assert db.query(FileDelivery).count() == 0


def test_a_workflow_renamed_locally_is_read_where_it_was_delivered(db, project, github):
    """Bug 6. A rename is a local edit until a campaign delivers it, so GitHub
    still holds ci.yml. On develop the check read the new name's file, found
    nothing there, and reported the delivered workflow deleted."""
    _add_repo(db, project, APP)
    workflow = _delivered_workflow(db, project, github, APP)
    record_delivered(db, workflow, APP, "main", sha=_blob(V1), path=f".github/workflows/{FILENAME}")
    wf.create_or_update_workflow(db, wf.WorkflowSchema(name="ci2", content=V1, original_name="ci"),
                                 project.project_id, False, USER)

    drifted, stored = _check(db, project)

    assert (drifted, stored) == ({}, {})
    details = {d.repo: d for d in wf._collect_project_drift_details(db, USER, project)}
    assert "synchronized" in details[APP].message
