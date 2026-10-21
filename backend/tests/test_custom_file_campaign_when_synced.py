"""A campaign can carry a synced custom file: to put it back where it drifted, or
to a repository it never reached.

Create Pull Requests used to deliver only custom files with local changes, even
one picked by id, so a synced project could not undo a change made on GitHub or
fill a repository added after its last delivery. A file picked by id now rides
the campaign whatever its status, as CODEOWNERS does, and a branch that already
holds it gets nothing, so the repositories it never left get no pull request.
"""
import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import Account, Base, CustomFile, Project, ProjectRepo, Repo  # noqa: E402
import workflows as wf  # noqa: E402
from tests.campaign_harness import USER, FakeGitHub, account, github as on_github, run_campaign  # noqa: E402

APP, WEB = "acme/app", "acme/web"
BUILD, LINT = ".github/scripts/build.sh", ".github/scripts/lint.sh"
CONTENT = "echo build\n"

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
def project(db):
    owner = Account(github_user=USER, github_email="s@example.com", account_type="free")
    db.add(owner)
    db.flush()
    project = Project(project_name="synced", project_code="SYN", user_id=owner.user_id,
                      project_type="standard", use_prefix=True, branch_option="default", pr_state="draft")
    db.add(project)
    db.flush()
    for name in (APP, WEB):
        repo = Repo(repo_name=name)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.add(CustomFile(project_id=project.project_id, file_path=BUILD, file_content=CONTENT, file_status="new"))
    db.commit()
    return project


def _build_file(db, project):
    return db.query(CustomFile).filter_by(project_id=project.project_id, file_path=BUILD).one()


def _deliver(db, github, project, *repos):
    for pr in run_campaign(db, github, project, repos):
        github.merge(db, pr)
    assert _build_file(db, project).file_status == "synced_with_github"


def test_a_synced_file_picked_by_id_puts_back_its_drifted_copy(db, project):
    github = FakeGitHub()
    _deliver(db, github, project, APP, WEB)
    github.put_file(APP, BUILD, "echo edited on GitHub\n")

    prs = run_campaign(db, github, project, [APP, WEB], selected_custom_file_ids=[_build_file(db, project).id])

    # acme/web still holds the file, so only acme/app gets a pull request.
    # The file has no local change, so it stays synced throughout.
    assert [pr.repo_name for pr in prs] == [APP]
    github.merge(db, prs[0])
    assert github.files[(APP, "main", BUILD)] == CONTENT
    assert db.query(CustomFile).populate_existing().filter_by(file_path=BUILD).one().file_status == \
        "synced_with_github"


def test_a_synced_file_picked_by_id_reaches_a_repository_it_never_did(db, project):
    github = FakeGitHub()
    _deliver(db, github, project, APP)

    prs = run_campaign(db, github, project, [WEB], selected_custom_file_ids=[_build_file(db, project).id])

    assert [pr.repo_name for pr in prs] == [WEB]
    assert github.paths(WEB, prs[0].branch_name) == {BUILD}


def test_a_synced_file_is_not_delivered_unless_picked(db, project):
    """Without ids a campaign still takes only files with local changes."""
    github = FakeGitHub()
    _deliver(db, github, project, APP)

    with pytest.raises(HTTPException) as refused:
        run_campaign(db, github, project, [WEB])

    assert refused.value.status_code == 400


def test_a_repository_that_already_holds_the_synced_file_gets_no_pull_request(db, project):
    github = FakeGitHub()
    _deliver(db, github, project, APP)
    github.put_file(WEB, BUILD, CONTENT)

    prs = run_campaign(db, github, project, [WEB], selected_custom_file_ids=[_build_file(db, project).id])

    assert prs == []


def test_a_synced_file_no_pull_request_carries_stays_synced(db, project):
    """build.sh is already everywhere it was sent; the pull request lint.sh opens
    does not carry it, so it is not put under review for that PR."""
    github = FakeGitHub()
    _deliver(db, github, project, APP)
    lint = CustomFile(project_id=project.project_id, file_path=LINT, file_content="echo lint\n",
                      file_status="committed_locally")
    db.add(lint)
    db.commit()

    [pr] = run_campaign(db, github, project, [APP],
                        selected_custom_file_ids=[_build_file(db, project).id, lint.id])

    assert pr.file_names == LINT
    statuses = {cf.file_path: cf.file_status for cf in db.query(CustomFile).populate_existing()}
    assert statuses == {BUILD: "synced_with_github", LINT: "under_review"}


def test_a_file_with_local_changes_is_written_even_where_the_branch_holds_it(db, project):
    """Only a synced file is skipped where it is already there. A local change is
    written and carried as before, so the pull request settles it."""
    github = FakeGitHub()
    db.add(CustomFile(project_id=project.project_id, file_path=LINT, file_content="echo lint\n",
                      file_status="committed_locally"))
    db.commit()
    github.put_file(APP, LINT, "echo lint\n")

    [pr] = run_campaign(db, github, project, [APP])

    assert pr.file_names == f"{BUILD}, {LINT}"
    statuses = {cf.file_path: cf.file_status for cf in db.query(CustomFile).populate_existing()}
    assert statuses == {BUILD: "under_review", LINT: "under_review"}


def test_commit_and_update_pr_does_not_re_send_a_synced_file(db, project):
    """A push carries local changes only: a synced file named in it, after an
    edit that changed nothing to deliver, is not written over a GitHub change."""
    github = FakeGitHub()
    _deliver(db, github, project, APP)
    github.put_file(APP, BUILD, "echo edited on GitHub\n")

    push = wf.UpdateProjectFilesRequest(project_name=project.project_name, repo_names=[APP],
                                        custom_file_ids=[_build_file(db, project).id])
    caller = account()

    with on_github(github), pytest.raises(HTTPException) as refused:
        wf.update_project_files(push, db, caller)

    assert refused.value.status_code == 400
    assert github.files[(APP, "main", BUILD)] == "echo edited on GitHub\n"
