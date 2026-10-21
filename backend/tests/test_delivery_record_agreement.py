"""Every reader of the delivery record agrees, whatever wrote it.

"Was this file delivered here?" and "is an open PR carrying it here?" used to be
answered in several places, each from its own guess: the per-file hash, the
file's status, ``confirmed_present_at``, or the names on a PR row. They
disagreed, and the one users hit was an open PR to one branch hiding drift on
the repository's other branches. Every one of them reads ``file_deliveries``
now.

Each scenario runs the function production calls to write it, then asks every
reader about the same (file, repo, branch). They must all give the answer that
scenario's state implies.

Drift reads the record too, by one rule for every kind of file
(``workflows._decide_drift``). The decision table at the end runs each row of
that rule through the workflow check and the CODEOWNERS check alike, so either
growing its own rule fails it.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deliveries  # noqa: E402
import workflows as wf  # noqa: E402
from campaign_rollback import _carried_source_files, _deliver_one_target, _persist_rollback_campaign  # noqa: E402
from models import (  # noqa: E402
    Account, Base, Codeowners, CustomFile, FileDelivery, LinkedReusableWorkflow, Project,
    ProjectPRCampaign, ProjectPullRequest, ProjectRepo, ProjectWorkflow, Repo, Workflow,
)
from projects import _pending_delivery_repo_names  # noqa: E402
from tests.delivery_records import record_delivered, record_drift, record_pr_carries  # noqa: E402

USER = "deliverer"
CODE = "DLV"
REPO = "acme/app"
OWNER, NAME = REPO.split("/")
BRANCH = "main"
OTHER_BRANCH = "release/x"
WORKFLOW_FILE = f"AM_{CODE}_ci.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
SCRIPT_PATH = ".github/scripts/build.sh"
CODEOWNERS_PATH = ".github/CODEOWNERS"

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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
def s(db):
    """A project delivering one workflow, one custom file and its CODEOWNERS to one repo."""
    account = Account(github_user=USER, github_email="d@example.com", account_type="free")
    db.add(account)
    db.flush()
    project = Project(project_name="deliveries", project_code=CODE, user_id=account.user_id,
                      project_type="standard", use_prefix=True, pr_state="draft")
    repo = Repo(repo_name=REPO)
    workflow = Workflow(workflow_name="ci", workflow_yaml="name: ci\non: push\n", workflow_status="new")
    db.add_all([project, repo, workflow])
    db.flush()
    db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    codeowners = Codeowners(project_id=project.project_id, content="* @acme/owners\n",
                            file_path=CODEOWNERS_PATH, git_hash="c" * 40, status="committed_locally")
    custom_file = CustomFile(project_id=project.project_id, file_path=SCRIPT_PATH,
                             file_content="echo build\n", file_status="committed_locally")
    db.add_all([codeowners, custom_file])
    db.commit()
    return SimpleNamespace(project=project, repo=repo, workflow=workflow,
                           codeowners=codeowners, custom_file=custom_file)


def _response(status, payload=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    response.text = ""
    return response


# --------------------------------------------------------------------------- #
# Writers: what production calls for each scenario                           #
# --------------------------------------------------------------------------- #


def _open_pr(db, s, number=1, branch=BRANCH):
    """A campaign's commit chain for every file, and the PR it persists."""
    carried: list = []
    am_branch = f"actions-manager/dlv/app/{number}-{branch.replace('/', '-')}"
    workflow = {"id": s.workflow.workflow_id, "name": "ci", "content": s.workflow.workflow_yaml}
    custom_file = {"id": s.custom_file.id, "file_path": SCRIPT_PATH,
                   "file_content": s.custom_file.file_content, "pending_delete": False}
    codeowners = {"id": s.codeowners.id, "file_path": CODEOWNERS_PATH, "content": s.codeowners.content}
    with patch("workflows._update_workflow_to_github", return_value=(201, f"wf-blob-{number}")), \
            patch("workflows.github_get", return_value=_response(404)), \
            patch("workflows.requests.put",
                  return_value=_response(201, {"content": {"sha": f"file-blob-{number}"}})):
        wf._commit_workflows_to_branch([workflow], OWNER, NAME, CODE, am_branch, {}, REPO, USER, db, True,
                                       carried=carried)
        wf._commit_custom_files_to_branch([custom_file], OWNER, NAME, am_branch, CODE, USER, db, {},
                                          carried=carried)
        wf._commit_codeowners_to_branch(codeowners, OWNER, NAME, am_branch, CODE, USER, db, {},
                                        carried=carried)
    campaign = ProjectPRCampaign(project_id=s.project.project_id)
    db.add(campaign)
    db.commit()
    wf._persist_single_pr({
        "status": "pr_created", "pr_number": number, "pr_url": f"https://github.com/{REPO}/pull/{number}",
        "branch_name": am_branch, "workflows_committed": ["ci"], "custom_files_committed": [SCRIPT_PATH],
        "codeowners_committed": CODEOWNERS_PATH, "deliveries": carried,
    }, f"{REPO} on {branch}", s.project, campaign, db)
    return db.query(ProjectPullRequest).filter_by(pr_number=number).one()


def _delivered(db, s):
    """The workflow on (REPO, BRANCH), as a merged campaign records it."""
    deliveries.record_delivered(db, deliveries.WORKFLOW, s.workflow.workflow_id, REPO, BRANCH, WORKFLOW_PATH, "pushed")
    db.commit()


def _drifted(db, s):
    """Drift a check recorded on (REPO, BRANCH) for the workflow and the CODEOWNERS."""
    record_drift(db, s.workflow, REPO, BRANCH)
    record_drift(db, s.codeowners, REPO, BRANCH)


# --------------------------------------------------------------------------- #
# Readers: every one, asked about one (file, repo, branch)                    #
# --------------------------------------------------------------------------- #


OUTCOMES = ("pending merge", "deleted", "not delivered", "synchronized", "under review", "differs")


def _outcome(detail):
    """(outcome, has_drift, deleted) of a check's result, the outcome named by its message."""
    kind = next((kind for kind in OUTCOMES if kind in detail.message), None)
    assert kind is not None, detail.message
    return kind, detail.has_drift, detail.deleted_in_github


def _check_workflow(db, s, branch, github, reads=None):
    """The workflow check on (REPO, *branch*), GitHub holding *github* there: None, or (sha, content)."""
    shas = {} if github is None else {WORKFLOW_FILE: github[0]}

    def content(_owner, _repo, _name, _token, default_branch=None):
        if reads is not None:
            reads.append(default_branch)
        return {"content": github[1], "sha": github[0]}

    with patch("workflows.get_workflow_from_github", side_effect=content):
        return wf._check_regular_workflow_in_repo(
            db, s.workflow, REPO, OWNER, NAME, WORKFLOW_FILE, shas, CODE, "token", s.project.project_id, branch,
            deliveries.targets_for(db, s.project.project_id),
        )


def _check_codeowners(db, s, branch, github, reads=None):
    """The CODEOWNERS check on (REPO, *branch*), GitHub holding *github* there."""
    def read(_owner, _repo, _path, at_branch, _token, *_etag):
        if reads is not None:
            reads.append(at_branch)
        return (None, None) if github is None else ({"sha": github[0], "content": github[1]}, None)

    targets = deliveries.targets_for(db, s.project.project_id)
    run = wf._CheckRun(s.project, USER, "token", "now", targets, frozenset(key[:3] for key in targets))
    with patch("workflows._read_file_on_branch", side_effect=read):
        return wf._check_file_on_branch(db, run, wf._codeowners_file(s.codeowners), s.repo, branch).detail


EDITED = ("gh-blob", "# edited on GitHub\n")


def _drift_answers(check, db, s, branch):
    return {
        "missing": _outcome(check(db, s, branch, None))[0],
        "differs_is_drift": check(db, s, branch, EDITED).has_drift,
    }


def _workflow_answers(db, s, branch):
    w = s.workflow
    return {
        **_drift_answers(_check_workflow, db, s, branch),
        "was_delivered": wf._was_delivered_per_db(db, w),
        "locked": w.workflow_id in wf._reusable_workflow_ids_locked_by_open_campaign(db, [w.workflow_id]),
        "rename_blocked": wf._rename_blocker_open_pr(db, s.project, w) is not None,
        "blocks_another_project": wf._find_blocking_reusable_workflow_pr(
            db, w, s.project.project_id + 1) is not None,
        "awaiting_delivery": REPO in _pending_delivery_repo_names(db, s.project, [w]),
        "delivered_at": (s.repo.repo_id, branch) in deliveries.delivered_at_by_target(
            db, deliveries.WORKFLOW, w.workflow_id),
    }


def _codeowners_answers(db, s, branch):
    return _drift_answers(_check_codeowners, db, s, branch)


# Drift is no longer "differs" where nothing was delivered, and a missing file
# never delivered there now reports so instead of nothing, as CODEOWNERS did.
NEVER = {"missing": "not delivered", "differs_is_drift": False, "was_delivered": False, "locked": False,
         "rename_blocked": False, "blocks_another_project": False, "awaiting_delivery": True,
         "delivered_at": False}
PENDING = {"missing": "pending merge", "differs_is_drift": False, "was_delivered": True, "locked": True,
           "rename_blocked": True, "blocks_another_project": True, "awaiting_delivery": False,
           "delivered_at": False}
DELIVERED = {"missing": "deleted", "differs_is_drift": True, "was_delivered": True, "locked": False,
             "rename_blocked": False, "blocks_another_project": False, "awaiting_delivery": False,
             "delivered_at": True}
# One rule, so CODEOWNERS answers exactly what a workflow does.
CODEOWNERS_NEVER = {"missing": "not delivered", "differs_is_drift": False}
CODEOWNERS_PENDING = {"missing": "pending merge", "differs_is_drift": False}
CODEOWNERS_DELIVERED = {"missing": "deleted", "differs_is_drift": True}


# --------------------------------------------------------------------------- #
# Scenarios                                                                   #
# --------------------------------------------------------------------------- #


def test_never_delivered(db, s):
    assert _workflow_answers(db, s, BRANCH) == NEVER
    assert _codeowners_answers(db, s, BRANCH) == CODEOWNERS_NEVER


def test_a_pr_open_to_one_branch(db, s):
    _open_pr(db, s)

    assert _workflow_answers(db, s, BRANCH) == PENDING
    assert _codeowners_answers(db, s, BRANCH) == CODEOWNERS_PENDING


def test_a_pr_open_to_one_branch_carries_nothing_to_the_others(db, s):
    _open_pr(db, s)

    # Still on its way somewhere, so the workflow is locked and not "new"; but
    # nothing is on its way to this branch, nor was anything delivered there.
    assert _workflow_answers(db, s, OTHER_BRANCH) == {
        **PENDING, "missing": "not delivered", "differs_is_drift": False,
    }
    assert _codeowners_answers(db, s, OTHER_BRANCH) == CODEOWNERS_NEVER


def test_a_merged_pr(db, s):
    pr = _open_pr(db, s)
    _drifted(db, s)

    wf.apply_pr_outcome(db, pr, "merged")

    assert _workflow_answers(db, s, BRANCH) == DELIVERED
    assert _codeowners_answers(db, s, BRANCH) == CODEOWNERS_DELIVERED
    # The drift clears read what this PR delivered.
    assert {row.has_drift for row in db.query(FileDelivery).filter_by(branch=BRANCH)} == {False}


def test_a_closed_pr(db, s):
    pr = _open_pr(db, s)

    wf.apply_pr_outcome(db, pr, "closed")

    # Never delivered, except to the rename save path, which is conservative by
    # design: the campaign's commit left a real blob SHA on the workflow, and a
    # delivery from before the record could carry one too.
    assert _workflow_answers(db, s, BRANCH) == {**NEVER, "was_delivered": True}
    assert _codeowners_answers(db, s, BRANCH) == CODEOWNERS_NEVER


def test_a_rename_merge_moves_the_delivered_path(db, s):
    """A merged rename leaves one delivery there, at the new path, and nothing
    at the old one: the campaign that carried it removed the old file."""
    wf.apply_pr_outcome(db, _open_pr(db, s), "merged")
    s.workflow.workflow_name = "ci-v2"
    db.commit()
    new_path = f".github/workflows/AM_{CODE}_ci-v2.yml"
    carried: list = []
    removed = []
    workflow = {"id": s.workflow.workflow_id, "name": "ci-v2", "content": s.workflow.workflow_yaml}
    with patch("workflows._update_workflow_to_github", return_value=(201, "renamed-blob")), \
            patch("workflows._delete_path_from_branch",
                  side_effect=lambda *args: removed.append(args[2]) or (wf.DELETE_DELETED, None)):
        wf._commit_workflows_to_branch([workflow], OWNER, NAME, CODE, "actions-manager/dlv/app/2-main", {}, REPO,
                                       USER, db, True, carried=carried, target=(REPO, BRANCH))
    pr = ProjectPullRequest(project_id=s.project.project_id, repo_name=REPO, pr_number=2, pr_url="u",
                            pr_state="open", branch_name="actions-manager/dlv/app/2-main", target_branch=BRANCH)
    db.add(pr)
    db.flush()
    deliveries.record_pending(db, pr, carried)

    wf.apply_pr_outcome(db, pr, "merged")

    assert removed == [WORKFLOW_PATH]
    row = _workflow_delivery(db, s)
    assert (row.delivered_path, row.delivered_sha) == (new_path, "renamed-blob")
    assert deliveries.superseded_path(db, deliveries.WORKFLOW, s.workflow.workflow_id, REPO, BRANCH, new_path) is None


def test_a_merge_is_recorded_together_with_what_it_delivered(db, s):
    """Once a PR's state is recorded the outcome never runs again, so the state
    and the settlement of what it carried must commit together or not at all."""
    pr = _open_pr(db, s)

    with patch("deliveries.record_pr_outcome", side_effect=RuntimeError("connection lost")):
        with pytest.raises(RuntimeError):
            wf.apply_pr_outcome(db, pr, "merged")
    db.rollback()
    db.refresh(pr)
    assert pr.pr_state == "open"

    wf.apply_pr_outcome(db, pr, "merged")

    assert _workflow_answers(db, s, BRANCH) == DELIVERED


def test_an_adopt_from_github(db, s):
    with patch("workflows.get_workflow_from_github",
               return_value={"content": "name: ci\non: push\n# from GitHub\n", "sha": "adopted"}):
        outcome = wf._apply_use_github_resolution(db, USER, s.workflow, REPO, f"AM_{CODE}_ci.yml", "token",
                                                  branch=BRANCH)
    assert outcome["success"] is True

    assert _workflow_answers(db, s, BRANCH) == DELIVERED


def test_a_direct_delete(db, s):
    _delivered(db, s)

    # What delete_workflow records once the DELETE lands.
    with patch("workflows.requests.delete", return_value=_response(200)):
        wf._delete_workflow_directly(db, s.project, "ci", USER, {}, [REPO])

    assert _workflow_answers(db, s, BRANCH) == NEVER


def test_a_drift_check_that_finds_the_file(db, s):
    """GitHub holding exactly ActionsManager's content is the delivery a check witnesses."""
    _check_workflow(db, s, BRANCH, ("seen", s.workflow.workflow_yaml))
    _check_codeowners(db, s, BRANCH, ("seen", s.codeowners.content))
    db.commit()

    assert _workflow_answers(db, s, BRANCH) == DELIVERED
    assert _codeowners_answers(db, s, BRANCH) == CODEOWNERS_DELIVERED


# --------------------------------------------------------------------------- #
# The bug: an open PR to one branch hid drift on the repo's other branches    #
# --------------------------------------------------------------------------- #


def test_an_open_pr_to_one_branch_does_not_hide_drift_on_another(db, s):
    """Fails on the develop this record replaced: its open-PR check matched the
    repository and never the PR's target branch, so a campaign to main reported
    release/x "under review" too and its drift went unreported."""
    for branch in (BRANCH, OTHER_BRANCH):
        record_delivered(db, s.workflow, REPO, branch, sha="delivered-blob")
    pr = ProjectPullRequest(
        project_id=s.project.project_id, repo_name=REPO, pr_number=5,
        pr_url=f"https://github.com/{REPO}/pull/5", pr_state="open",
        branch_name="actions-manager/dlv/app/5-main", target_branch=BRANCH, workflow_names="ci",
    )
    db.add(pr)
    record_pr_carries(db, pr, s.workflow)

    # Each branch holds its own copy, neither of them ActionsManager's.
    blobs = {BRANCH: "main-blob", OTHER_BRANCH: "release-blob"}

    def tree(_owner, _repo, branch, _token, etag=None):
        return {f"AM_{CODE}_ci.yml": blobs[branch]}, None

    def content(_owner, _repo, _name, _token, default_branch=None):
        return {"content": f"name: ci\non: push\n# edited on {default_branch}\n", "sha": blobs[default_branch]}

    with patch("workflows._resolve_branches_for_repo", return_value=[BRANCH, OTHER_BRANCH]), \
            patch("workflows.fetch_workflow_tree", side_effect=tree), \
            patch("workflows.get_workflow_from_github", side_effect=content):
        statuses = wf._process_regular_workflows(
            db, [s.workflow], [REPO], CODE, "token", True,
            project_id=s.project.project_id, project=s.project, user=USER,
        )

    by_branch = {status.branch: status for status in statuses}
    assert by_branch[BRANCH].has_drift is False, by_branch[BRANCH].message
    assert by_branch[OTHER_BRANCH].has_drift is True, by_branch[OTHER_BRANCH].message


# --------------------------------------------------------------------------- #
# The editor's "Commit and update PR": /api/update-project-files             #
# --------------------------------------------------------------------------- #


def _push_edit(db, s, blob, onto=None, **files):
    """The route pushing the saved ``ci``, onto the open PR *onto* when given.

    *files* names project files to push instead (``custom_file_ids``,
    ``include_codeowners``).
    """
    number = onto.pr_number if onto else 9
    am_branch = onto.branch_name if onto else "actions-manager/dlv/app/9-main"
    pr = {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}", "title": "t",
          "user": {"login": USER}, "body": "b"}
    with patch("workflows.user_tokens", {USER: "token"}), \
            patch("workflows._resolve_branches_for_repo", return_value=[BRANCH]), \
            patch("workflows._create_or_get_am_branch", return_value=(am_branch, onto is None, None)), \
            patch("workflows._fetch_branch_protection", return_value={"status": "none"}), \
            patch("workflows._update_workflow_to_github", return_value=(201, blob)), \
            patch("workflows.github_get", return_value=_response(404)), \
            patch("workflows.requests.put", return_value=_response(201, {"content": {"sha": blob}})), \
            patch("workflows._check_existing_pr", return_value=pr if onto else None), \
            patch("workflows._create_pull_request", return_value=(pr, None)):
        response = wf.update_project_files(
            wf.UpdateProjectFilesRequest(project_name=s.project.project_name, repo_names=[REPO],
                                         workflows=[] if files else [{"name": "ci"}], **files),
            db, SimpleNamespace(github_user=USER),
        )
    assert response["results"][f"{REPO} on {BRANCH}"]["status"] == ("pr_updated" if onto else "pr_created")
    return db.query(ProjectPullRequest).filter_by(pr_number=number).one()


def _workflow_delivery(db, s):
    return db.query(FileDelivery).filter_by(workflow_id=s.workflow.workflow_id, branch=BRANCH).one()


def test_a_push_that_opens_a_pr_records_it(db, s):
    """It saved no PR row, so the PR it opened was invisible to every reader and
    its merge delivered nothing the record knew of."""
    pr = _push_edit(db, s, "pushed-blob")

    assert pr.campaign_id is not None
    assert _workflow_answers(db, s, BRANCH) == PENDING

    wf.apply_pr_outcome(db, pr, "merged")

    assert _workflow_answers(db, s, BRANCH) == DELIVERED
    assert _workflow_delivery(db, s).delivered_sha == "pushed-blob"


def test_a_push_onto_an_open_pr_records_what_it_now_carries(db, s):
    """The record kept the campaign's blob as what the PR carried, so merging it
    recorded a SHA that was not the one that landed."""
    campaign_pr = _open_pr(db, s)
    campaign_id = campaign_pr.campaign_id

    pr = _push_edit(db, s, "edited-blob", onto=campaign_pr)

    assert (pr.pr_id, pr.campaign_id) == (campaign_pr.pr_id, campaign_id)
    assert _workflow_delivery(db, s).pending_sha == "edited-blob"

    wf.apply_pr_outcome(db, pr, "merged")

    assert _workflow_delivery(db, s).delivered_sha == "edited-blob"


def test_a_push_onto_an_open_pr_keeps_what_it_already_carried(db, s):
    """Saving the push's names in place of the PR's made its campaign card, and a
    rollback's "keep", forget the other workflows the PR still carries."""
    campaign_pr = _open_pr(db, s)
    campaign_pr.workflow_names = "build, ci"
    db.commit()

    pr = _push_edit(db, s, "edited-blob", onto=campaign_pr)

    assert (pr.workflow_names, pr.file_names) == ("build, ci", f"{SCRIPT_PATH}, {CODEOWNERS_PATH}")


def test_a_push_of_other_project_files_delivers_only_those(db, s):
    """A campaign naming no workflow delivers every one, so a push of a custom
    file and the CODEOWNERS must not name none."""
    _push_edit(db, s, "file-blob", custom_file_ids=[s.custom_file.id], include_codeowners=True)

    args = (db, deliveries.CUSTOM_FILE, s.custom_file.id, REPO, BRANCH)
    assert deliveries.pending_here(*args)
    assert _codeowners_answers(db, s, BRANCH) == CODEOWNERS_PENDING
    assert _workflow_answers(db, s, BRANCH) == NEVER


def test_an_unlocked_file_saved_under_review_rides_its_open_pr(db, s):
    """Commit and Update PR for a custom file and the CODEOWNERS: the save made
    them committed_locally, and the push adds them to the PR already open."""
    pr = _open_pr(db, s)
    s.custom_file.file_status = s.codeowners.status = "committed_locally"
    db.commit()

    # GitHub answers with the blob of what it stored, which is what locks CODEOWNERS.
    blob = deliveries.blob_sha(s.codeowners.content)
    _push_edit(db, s, blob, onto=pr, custom_file_ids=[s.custom_file.id], include_codeowners=True)

    db.refresh(s.custom_file)
    db.refresh(s.codeowners)
    assert (s.custom_file.file_status, s.codeowners.status) == ("under_review", "under_review")
    assert _codeowners_answers(db, s, BRANCH) == CODEOWNERS_PENDING


# --------------------------------------------------------------------------- #
# Custom files: no drift readers yet, so the record itself                    #
# --------------------------------------------------------------------------- #


def _custom_file_state(db, s):
    args = (db, deliveries.CUSTOM_FILE, s.custom_file.id, REPO, BRANCH)
    return {"pending": deliveries.pending_here(*args), "delivered": deliveries.delivered_here(*args)}


def test_a_custom_file_campaign_is_pending_until_it_merges(db, s):
    pr = _open_pr(db, s)
    assert _custom_file_state(db, s) == {"pending": True, "delivered": False}

    wf.apply_pr_outcome(db, pr, "merged")

    assert _custom_file_state(db, s) == {"pending": False, "delivered": True}


def test_a_closed_custom_file_campaign_delivers_nothing(db, s):
    pr = _open_pr(db, s)

    wf.apply_pr_outcome(db, pr, "closed")

    assert _custom_file_state(db, s) == {"pending": False, "delivered": False}


def test_a_direct_custom_file_delete_is_recorded(db, s):
    wf.apply_pr_outcome(db, _open_pr(db, s), "merged")

    with patch("workflows._get_authenticated_headers", return_value={"Authorization": "token t"}), \
            patch("workflows._delete_path_from_branch", return_value=(wf.DELETE_DELETED, None)):
        outcome = wf.delete_custom_file_directly(db, s.project, s.custom_file, USER)

    assert outcome["removed"] is True
    assert _custom_file_state(db, s) == {"pending": False, "delivered": False}


def test_a_removal_the_branch_had_nothing_to_do_for_records_the_file_gone(db, s):
    """A campaign's AM branch is cut fresh from the target, so a file it lacks is
    not on the target either."""
    wf.apply_pr_outcome(db, _open_pr(db, s), "merged")

    carried: list = []
    with patch("workflows.github_get", return_value=_response(404)):
        wf._commit_custom_files_to_branch(
            [{"id": s.custom_file.id, "file_path": SCRIPT_PATH, "file_content": "", "pending_delete": True}],
            OWNER, NAME, "actions-manager/dlv/app/2-main", CODE, USER, db, {}, carried=carried,
        )
    assert wf._record_absent_removals(db, carried, REPO, BRANCH) == []

    assert _custom_file_state(db, s) == {"pending": False, "delivered": False}


def _linked_rwx_workflow(db, s):
    """RWX project ``RWX``'s reusable ``deploy``, linked into the caller project."""
    rwx = Project(project_name="shared", project_code="RWX", user_id=s.project.user_id,
                  project_type="rwx", use_prefix=True)
    rwx_repo = Repo(repo_name="acme/shared")
    deploy = Workflow(workflow_name="deploy", workflow_yaml="on: workflow_call\n", reusable_workflow=True,
                      workflow_status="synced_with_github")
    db.add_all([rwx, rwx_repo, deploy])
    db.flush()
    db.add(ProjectRepo(project_id=rwx.project_id, repo_id=rwx_repo.repo_id))
    db.add(ProjectWorkflow(project_id=rwx.project_id, workflow_id=deploy.workflow_id))
    db.add(LinkedReusableWorkflow(standard_project_id=s.project.project_id, rwx_project_id=rwx.project_id,
                                  workflow_id=deploy.workflow_id))
    db.commit()
    return rwx, deploy


def test_rolling_back_a_linked_reusable_workflow_keeps_it_locked(db, s):
    """The caller's campaign delivered RWX's deploy to the RWX repo under the RWX
    project's prefix. Its rollback PR carries that file, so while it is open the
    workflow stays locked for every project, as a forward campaign does."""
    rwx, deploy = _linked_rwx_workflow(db, s)
    target = {
        "repo_name": "acme/shared", "target_branch": BRANCH, "workflow_names": "deploy",
        "files": [{"path": ".github/workflows/AM_RWX_deploy.yml", "action": "restore",
                   "before": "on: workflow_call\n", "after": "on: workflow_call\n# before\n"}],
    }
    opened = {"number": 9, "html_url": "https://github.com/acme/shared/pull/9", "title": "t",
              "user": {"login": USER}, "body": "b"}
    with patch("workflows._create_or_get_am_branch", return_value=("actions-manager/dlv/shared/9-main", True, None)), \
            patch("workflows._fetch_branch_protection", return_value={"status": "none"}), \
            patch("workflows.github_get", return_value=_response(404)), \
            patch("workflows.requests.put", return_value=_response(201, {"content": {"sha": "rolled-back"}})), \
            patch("workflows._check_existing_pr", return_value=None), \
            patch("workflows._campaign_pr_body", return_value="body"), \
            patch("workflows._create_pull_request", return_value=(opened, None)):
        key, result = _deliver_one_target(target, s.project, {}, USER, db, {},
                                          {"base_commits": {}, "protection": {}})
    campaign_id, prs = _persist_rollback_campaign(db, s.project, {key: result}, ["acme/shared"],
                                                  {"created_by": USER})

    assert prs == 1
    assert campaign_id is not None
    assert wf._reusable_workflow_ids_locked_by_open_campaign(db, [deploy.workflow_id]) == {deploy.workflow_id}
    assert wf._find_blocking_reusable_workflow_pr(db, deploy, rwx.project_id) is not None


def test_a_rollback_records_the_files_its_paths_belong_to(db, s):
    """The rollback commits plain paths; each is recorded against the project's
    own file, and a path that is none of them is dropped."""
    entries = [{"kind": deliveries.CUSTOM_FILE, "id": None, "path": path, "sha": "x", "delete": False}
               for path in (WORKFLOW_PATH, SCRIPT_PATH, CODEOWNERS_PATH, "docs/unmanaged.md")]

    mapped = _carried_source_files(db, s.project, entries)

    assert {(entry["kind"], entry["id"]) for entry in mapped} == {
        (deliveries.WORKFLOW, s.workflow.workflow_id),
        (deliveries.CUSTOM_FILE, s.custom_file.id),
        (deliveries.CODEOWNERS, s.codeowners.id),
    }


# --------------------------------------------------------------------------- #
# The drift rule: one decision table, the same for every kind of file         #
# --------------------------------------------------------------------------- #

CHECKS = {"workflow": _check_workflow, "codeowners": _check_codeowners}
# What _open_pr's mocked commits leave as each file's blob on the PR branch.
CAMPAIGN_BLOB = {"workflow": "wf-blob-1", "codeowners": "file-blob-1"}


def _seed(db, s, seed):
    """The record each row starts from, written by what production calls."""
    if seed == "pending":
        _open_pr(db, s)
    elif seed == "delivered":
        wf.apply_pr_outcome(db, _open_pr(db, s), "merged")


def _github(s, kind, held):
    expected = {"workflow": s.workflow.workflow_yaml, "codeowners": s.codeowners.content}[kind]
    return {
        None: None,
        # Another copy under the delivered blob: had the rule read it, it would differ.
        "the delivered blob": (CAMPAIGN_BLOB[kind], EDITED[1]),
        "another copy": EDITED,
        # ActionsManager's own content, only the line endings changed.
        "the expected content": ("gh-equal", expected.replace("\n", "\r\n")),
    }[held]


# (GitHub holds, record, outcome as (name, has_drift, deleted_in_github), whether the content is read)
DECISIONS = [
    (None, "pending", ("pending merge", False, False), False),
    (None, "delivered", ("deleted", True, True), False),
    (None, None, ("not delivered", False, False), False),
    ("the delivered blob", "delivered", ("synchronized", False, False), False),
    ("another copy", "pending", ("under review", False, False), False),
    ("the expected content", None, ("synchronized", False, False), True),
    ("another copy", None, ("not delivered", False, False), True),
    ("another copy", "delivered", ("differs", True, False), True),
]


@pytest.mark.parametrize("kind", sorted(CHECKS))
@pytest.mark.parametrize("held, seed, outcome, reads_content", DECISIONS,
                         ids=[f"{held or 'missing'}-{seed or 'nothing'}" for held, seed, _, _ in DECISIONS])
def test_every_kind_is_judged_by_the_one_drift_rule(db, s, kind, held, seed, outcome, reads_content):
    _seed(db, s, seed)
    reads = []

    detail = CHECKS[kind](db, s, BRANCH, _github(s, kind, held), reads)

    assert _outcome(detail) == outcome, detail.message
    if kind == "workflow":
        # CODEOWNERS reads its content with its SHA, in one request.
        assert bool(reads) is reads_content


@pytest.mark.parametrize("kind", sorted(CHECKS))
def test_a_copy_equal_to_the_expected_content_is_what_is_delivered(db, s, kind):
    """The rule's one write: GitHub holding exactly ActionsManager's content
    means its blob is the one delivered there, so the next check reads nothing."""
    file = {"workflow": s.workflow, "codeowners": s.codeowners}[kind]
    file_id = s.workflow.workflow_id if kind == "workflow" else file.id

    CHECKS[kind](db, s, BRANCH, _github(s, kind, "the expected content"))

    row = db.query(FileDelivery).filter(
        getattr(FileDelivery, deliveries._FILE_COLUMN[kind]) == file_id, FileDelivery.branch == BRANCH,
    ).one()
    assert (row.delivered_sha, row.delivered_at is not None) == ("gh-equal", True)
    again = CHECKS[kind](db, s, BRANCH, ("gh-equal", EDITED[1]))
    assert _outcome(again) == ("synchronized", False, False)
