"""A campaign reaching a branch that already holds what it carries reports that branch up to date.

Campaigns target repositories, and each repository's configured branches. A
campaign putting one branch's drift right therefore also reaches the
repository's other branches, which already hold the workflow. A file found
already there is skipped; an unchanged workflow is still "committed", so only
GitHub's refusal of an empty pull request shows it, and that refusal used to
be reported as a failure.
"""
import os
import sys
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import workflows as wf  # noqa: E402
from models import Account, Base, Project, ProjectRepo, ProjectWorkflow, Repo, Workflow  # noqa: E402
from tests.campaign_harness import USER, FakeGitHub, github as on_github  # noqa: E402

CODE = "UTD"
APP = "uptodate-org/app"
PATH = f".github/workflows/AM_{CODE}_ci.yml"
CONTENT = "name: ci\non: push\n"

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class GitHub(FakeGitHub):
    """Refuses a pull request whose branch adds nothing to its target, as GitHub does."""

    def open_pr(self, owner, repo, am_branch, target_branch, *args, **kwargs):
        name = f"{owner}/{repo}"
        paths = self.paths(name, am_branch) | self.paths(name, target_branch)
        if all(self.files.get((name, am_branch, p)) == self.files.get((name, target_branch, p)) for p in paths):
            return None, f"Validation Failed — No commits between {target_branch} and {am_branch}"
        return super().open_pr(owner, repo, am_branch, target_branch, *args, **kwargs)


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
    account = Account(github_user=USER, github_email="u@example.com", account_type="free")
    db.add(account)
    db.flush()
    project = Project(project_name="uptodate", project_code=CODE, user_id=account.user_id,
                      project_type="standard", use_prefix=True, branch_option="default", pr_state="synced")
    db.add(project)
    db.flush()
    repo = Repo(repo_name=APP)
    db.add(repo)
    db.flush()
    db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    workflow = Workflow(workflow_name="ci", workflow_yaml=CONTENT, workflow_status="synced_with_github")
    db.add(workflow)
    db.flush()
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()
    return project


def _restore(db, github, project) -> dict:
    """The campaign "Keep ActionsManager version" opens: only the workflow, to its repository."""
    with on_github(github), patch("workflows._delete_actions_manager_branch", return_value=(True, None)) as drop:
        answer = wf.create_pull_requests(
            wf.CreatePullRequestsRequest(project_name=project.project_name, project_id=project.project_id,
                                         selected_repos=[APP], selected_workflows=["ci"],
                                         selected_custom_file_ids=[]),
            BackgroundTasks(), db, github_user=USER,
        )
    return answer["results"], drop


def test_a_branch_already_holding_the_workflow_is_up_to_date_not_failed(db, project):
    github = GitHub()
    github.branch_config = ["main", "release"]
    github.put_file(APP, PATH, CONTENT, branch="main")
    github.put_file(APP, PATH, CONTENT + "# changed on GitHub\n", branch="release")

    results, drop = _restore(db, github, project)

    assert results[f"{APP} on release"]["status"] == "pr_created"
    assert results[f"{APP} on main"]["nothing_to_deliver"] is True
    # The branch cut for main, identical to it, is not left behind.
    assert [call.args[2] for call in drop.call_args_list] == [f"actions-manager/{CODE.lower()}/1-main"]


def test_another_refusal_is_still_a_failure(db, project):
    github = GitHub()
    github.put_file(APP, PATH, CONTENT + "# changed on GitHub\n", branch="main")
    github.pr_refusal = "Resource not accessible by integration"

    results, drop = _restore(db, github, project)

    assert results[f"{APP} on main"]["status"] == "error"
    assert results[f"{APP} on main"].get("nothing_to_deliver") is None
    drop.assert_not_called()
