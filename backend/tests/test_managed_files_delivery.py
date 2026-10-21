"""A CODEOWNERS project delivers and checks its one file with no workflows (#2367).

A Managed Files project holds no workflows, so its campaign carries only the
file: one pull request per repository and branch, through the same campaign
path every project uses. Its drift check reads only the file, per repository
and branch.
"""
import os
import sys
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deliveries import blob_sha  # noqa: E402
from models import Account, Base, Codeowners, Project, ProjectRepo, Repo  # noqa: E402
import workflows as wf  # noqa: E402
from tests.campaign_harness import USER, FakeGitHub, run_campaign  # noqa: E402
from tests.test_codeowners import FakeGitHub as ContentsGitHub  # noqa: E402

APP, WEB = "acme/app", "acme/web"
PATH = ".github/CODEOWNERS"
CONTENT = "* @acme/owners\n"
EDITED = "* @someone-else\n"

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


def _managed_project(db, status: str) -> Project:
    owner = Account(github_user=USER, github_email="m@example.com", account_type="free")
    db.add(owner)
    db.flush()
    project = Project(project_name="Owners", project_code="OWN", user_id=owner.user_id,
                      project_type="managed_files", file_kind="codeowners",
                      branch_option="default", pr_state="draft")
    db.add(project)
    db.flush()
    for name in (APP, WEB):
        repo = Repo(repo_name=name)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.add(Codeowners(project_id=project.project_id, content=CONTENT, file_path=PATH,
                      git_hash=blob_sha(CONTENT), status=status))
    db.commit()
    return project


def test_a_campaign_opens_one_pull_request_per_repository_carrying_only_the_file(db):
    project = _managed_project(db, status="new")
    github = FakeGitHub()
    opened = []
    open_pr = github.open_pr

    def recording_open_pr(*args, **kwargs):
        opened.append((args[5], kwargs.get("title")))
        return open_pr(*args, **kwargs)

    with patch.object(github, "open_pr", side_effect=recording_open_pr):
        prs = run_campaign(db, github, project, [APP, WEB], include_codeowners=True)

    assert sorted(pr.repo_name for pr in prs) == [APP, WEB]
    assert {pr.repo_name: github.paths(pr.repo_name, pr.branch_name) for pr in prs} == {APP: {PATH}, WEB: {PATH}}
    # No workflow rides along, so _create_pull_request's own title says files.
    assert opened == [([], None), ([], None)]

    for pr in prs:
        github.merge(db, pr)
    db.expire_all()
    assert db.query(Codeowners).filter_by(project_id=project.project_id).one().status == "synced_with_github"
    assert github.paths(APP) == {PATH}


def test_a_drift_check_reports_the_file_per_repository_and_branch(db):
    project = _managed_project(db, status="synced_with_github")
    github = ContentsGitHub()
    for repo in (APP, WEB):
        for branch in ("main", "release"):
            github.put(repo, CONTENT, branch=branch)

    with patch("workflows._resolve_branches_for_repo", return_value=["main", "release"]), \
            patch("requests.get", side_effect=github.get), \
            patch("workflows.user_tokens", {USER: "token"}):
        wf.run_project_drift_check(db, USER, project)
        github.put(APP, EDITED, branch="release")
        drifted, unchecked, drifted_codeowners, drifted_custom_files = wf.run_project_drift_check(db, USER, project)

    assert sorted((d.repo, d.branch) for d in drifted_codeowners) == [(APP, "release")]
    assert (drifted, unchecked, drifted_custom_files) == ([], [], [])
    db.refresh(project)
    assert (project.drift_status, project.drift_count) == ("drifted", 1)
