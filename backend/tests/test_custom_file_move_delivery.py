"""Moving a custom file is delivered as a move: the campaign removes it where it was delivered.

The move itself is a local edit (``custom_files.update_custom_file``). The
campaign used to write only the new path, so every repository it reached kept
the old copy for good. Now each (repo, branch) removes what the delivery
record says was delivered there, on the same AM branch as the write.
"""
import os
import sys
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import custom_files  # noqa: E402
from models import Account, Base, CustomFile, Project, ProjectRepo, Repo  # noqa: E402
from tests.campaign_harness import USER, FakeGitHub, account, run_campaign  # noqa: E402

APP, WEB = "acme/app", "acme/web"
OLD, NEW = ".github/scripts/build.sh", ".github/scripts/ci/build.sh"

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
    owner = Account(github_user=USER, github_email="m@example.com", account_type="free")
    db.add(owner)
    db.flush()
    project = Project(project_name="moves", project_code="MOV", user_id=owner.user_id,
                      project_type="standard", use_prefix=True, branch_option="default", pr_state="draft")
    db.add(project)
    db.flush()
    for name in (APP, WEB):
        repo = Repo(repo_name=name)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.add(CustomFile(project_id=project.project_id, file_path=OLD, file_content="echo build\n", file_status="new"))
    db.commit()
    return project


def _move(db, project, path):
    cf = db.query(CustomFile).filter_by(project_id=project.project_id).one()
    with patch("custom_files.user_tokens", {USER: "token"}):
        custom_files.update_custom_file(project.project_id, cf.id, custom_files.UpdateCustomFileRequest(file_path=path),
                                        db, account())


def _deliver(db, github, project, *repos):
    for pr in run_campaign(db, github, project, repos):
        github.merge(db, pr)
    github.deletes.clear()


def test_a_moved_custom_file_campaign_removes_the_old_path_on_the_same_branch(db, project):
    github = FakeGitHub()
    _deliver(db, github, project, APP)
    _move(db, project, NEW)

    [pr] = run_campaign(db, github, project, [APP])

    assert github.deletes == [(APP, pr.branch_name, OLD)], "the removal rides the campaign's own branch"
    assert github.paths(APP, pr.branch_name) == {NEW}
    github.merge(db, pr)
    assert github.paths(APP) == {NEW}


def test_a_move_never_delivered_there_removes_nothing(db, project):
    """acme/web never received the file, so the move has nothing to remove there."""
    github = FakeGitHub()
    _deliver(db, github, project, APP)
    _move(db, project, NEW)

    prs = run_campaign(db, github, project, [APP, WEB])

    by_repo = {pr.repo_name: pr for pr in prs}
    assert github.deletes == [(APP, by_repo[APP].branch_name, OLD)]
    assert github.paths(WEB, by_repo[WEB].branch_name) == {NEW}
