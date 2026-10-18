"""
Workflow lookups by name must be scoped to a project.

Workflow names are deliberately not globally unique — two projects may each own
a workflow called "ci", and a migration explicitly removed the old unique
constraint to allow it. Two lookups ignored that:

  * _update_workflow_git_hash did an unscoped filter_by(workflow_name).first(),
    so committing project A's workflow stamped A's GitHub blob SHA onto
    project B's row. That corrupts B's drift baseline, and because the drift
    check trusts the stored hash as a proxy for local content, B's real drift
    then reads as "synchronized".
  * The upsert lookup used ILIKE with raw user input, where '_' is a
    single-character wildcard, so saving "ci_build" could overwrite "ciXbuild".

Prefix mode does not protect against either: use_prefix is applied only when
building the GitHub path, so both modes store the bare stem.
"""

import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (  # noqa: E402
    Base, Account, Project, ProjectWorkflow, Workflow,
)
from unittest.mock import patch  # noqa: E402

from workflows import _commit_workflows_to_branch, _update_workflow_git_hash  # noqa: E402
from tests.delivery_records import record_pr_carries  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _project(db, code, *, use_prefix=False):
    account = Account(
        github_user=f"user-{code}", github_email=f"{code}@example.com", account_type="free"
    )
    db.add(account); db.commit(); db.refresh(account)
    project = Project(
        project_name=f"proj-{code}", project_code=code,
        user_id=account.user_id, use_prefix=use_prefix,
    )
    db.add(project); db.commit(); db.refresh(project)
    return project


def _workflow(db, project, name, *, git_hash="original", is_reusable=False):
    wf = Workflow(
        workflow_name=name,
        workflow_yaml=f"name: {name}\non: push\n",
        workflow_git_hash=git_hash,
        reusable_workflow=is_reusable,
    )
    db.add(wf); db.commit(); db.refresh(wf)
    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=wf.workflow_id))
    db.commit()
    return wf


class TestGitHashWriteIsProjectScoped:
    def test_committing_one_project_does_not_touch_another(self, db):
        """The headline bug: A's push stamped its SHA onto B's row."""
        project_a = _project(db, "AAA")
        project_b = _project(db, "BBB")
        wf_a = _workflow(db, project_a, "ci", git_hash="a-original")
        wf_b = _workflow(db, project_b, "ci", git_hash="b-original")

        # Deliberately update the *second* project. The old unscoped
        # .first() returns the lowest rowid — project A's row — so this
        # ordering is what makes the bug visible; updating A instead would
        # pass even with the bug.
        _update_workflow_git_hash(db, "ci", "b-new-sha", "BBB")

        db.refresh(wf_a); db.refresh(wf_b)
        assert wf_b.workflow_git_hash == "b-new-sha"
        assert wf_a.workflow_git_hash == "a-original"

    def test_prefix_mode_projects_collide_too(self, db):
        """Prefixing protects the GitHub filename, not the workflows table.

        Both projects use_prefix=True, so they push AM_AAA_ci.yml and
        AM_BBB_ci.yml — no file collision — yet both store the bare stem "ci"
        and were equally vulnerable.
        """
        project_a = _project(db, "PFA", use_prefix=True)
        project_b = _project(db, "PFB", use_prefix=True)
        wf_a = _workflow(db, project_a, "ci", git_hash="a-original")
        wf_b = _workflow(db, project_b, "ci", git_hash="b-original")

        # Second project again, for the same ordering reason as above.
        _update_workflow_git_hash(db, "ci", "b-new-sha", "PFB")

        db.refresh(wf_a); db.refresh(wf_b)
        assert wf_b.workflow_git_hash == "b-new-sha"
        assert wf_a.workflow_git_hash == "a-original"

    def test_regular_and_reusable_of_the_same_name_are_distinct(self, db):
        # One project may legitimately hold both.
        project = _project(db, "DUAL")
        # Regular first, so an unscoped .first() would wrongly pick it.
        regular = _workflow(db, project, "ci", git_hash="regular-original")
        reusable = _workflow(db, project, "ci", git_hash="reusable-original", is_reusable=True)

        _update_workflow_git_hash(db, "ci", "reusable-new", "DUAL", is_reusable=True)

        db.refresh(regular); db.refresh(reusable)
        assert reusable.workflow_git_hash == "reusable-new"
        assert regular.workflow_git_hash == "regular-original"

    def test_unknown_project_updates_nothing(self, db):
        project = _project(db, "REAL")
        wf = _workflow(db, project, "ci", git_hash="untouched")

        _update_workflow_git_hash(db, "ci", "new", "NOPE")

        db.refresh(wf)
        assert wf.workflow_git_hash == "untouched"

    def test_empty_sha_is_ignored(self, db):
        project = _project(db, "SKIP")
        wf = _workflow(db, project, "ci", git_hash="keep")

        _update_workflow_git_hash(db, "ci", "", "SKIP")

        db.refresh(wf)
        assert wf.workflow_git_hash == "keep"


class TestNameMatchingHasNoWildcards:
    def test_underscore_is_not_treated_as_a_wildcard(self, db):
        """'_' is a single-char wildcard in SQL LIKE, so ILIKE('ci_build')
        matched 'ciXbuild'. Both rows must exist or this proves nothing."""
        from sqlalchemy import func

        project = _project(db, "WILD")
        decoy = _workflow(db, project, "ciXbuild", git_hash="decoy")
        real = _workflow(db, project, "ci_build", git_hash="real")

        # The corrected matching used by the upsert paths.
        match = (
            db.query(Workflow)
            .join(ProjectWorkflow, ProjectWorkflow.workflow_id == Workflow.workflow_id)
            .filter(
                ProjectWorkflow.project_id == project.project_id,
                func.lower(Workflow.workflow_name) == "ci_build",
            )
            .all()
        )

        assert [m.workflow_id for m in match] == [real.workflow_id]
        assert decoy.workflow_id not in [m.workflow_id for m in match]

    def test_matching_is_still_case_insensitive(self, db):
        from sqlalchemy import func

        project = _project(db, "CASE")
        wf = _workflow(db, project, "CI")

        match = (
            db.query(Workflow)
            .join(ProjectWorkflow, ProjectWorkflow.workflow_id == Workflow.workflow_id)
            .filter(
                ProjectWorkflow.project_id == project.project_id,
                func.lower(Workflow.workflow_name) == "ci",
            )
            .first()
        )

        assert match is not None
        assert match.workflow_id == wf.workflow_id


class TestOneProjectPerWorkflow:
    def test_a_workflow_cannot_be_added_to_a_second_project(self, db):
        project_a = _project(db, "ONE")
        project_b = _project(db, "TWO")
        wf = _workflow(db, project_a, "ci")

        db.add(ProjectWorkflow(project_id=project_b.project_id, workflow_id=wf.workflow_id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_the_unique_index_exists(self, db):
        indexes = db.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='project_workflows'")
        ).fetchall()
        assert "uq_project_workflows_workflow_id" in [i[0] for i in indexes]


class TestCommitStampsTheRowItDelivered:
    """#2085: a reusable workflow a standard project owns is delivered through
    the regular commit path, which stamped every SHA as a regular workflow's.
    A lone reusable row matched nothing, so its drift baseline never moved."""

    def _commit(self, db, workflow_dict, new_sha):
        with patch("workflows._update_workflow_to_github", return_value=(201, new_sha)):
            committed, errors = _commit_workflows_to_branch(
                [workflow_dict], "acme", "app", "OWN", "am-branch", {},
                "acme/app", "user-OWN", db, False,
            )
        assert committed == [workflow_dict["name"]]
        assert errors == []

    def test_an_owned_reusable_workflow_gets_the_delivered_sha(self, db):
        project = _project(db, "OWN")
        reusable = _workflow(db, project, "shared", git_hash="imported", is_reusable=True)

        self._commit(db, {"name": "shared", "content": "x", "is_reusable": True}, "delivered")

        db.refresh(reusable)
        assert reusable.workflow_git_hash == "delivered"

    def test_it_lands_on_the_reusable_row_not_a_regular_one_of_the_same_name(self, db):
        project = _project(db, "OWN")
        regular = _workflow(db, project, "ci", git_hash="regular-original")
        reusable = _workflow(db, project, "ci", git_hash="reusable-original", is_reusable=True)

        self._commit(db, {"name": "ci", "content": "x", "is_reusable": True}, "reusable-new")

        db.refresh(regular); db.refresh(reusable)
        assert reusable.workflow_git_hash == "reusable-new"
        assert regular.workflow_git_hash == "regular-original"

    def test_a_dict_without_the_flag_is_still_a_regular_workflow(self, db):
        project = _project(db, "OWN")
        regular = _workflow(db, project, "ci", git_hash="regular-original")

        self._commit(db, {"name": "ci", "content": "x"}, "regular-new")

        db.refresh(regular)
        assert regular.workflow_git_hash == "regular-new"


class TestTheTriggerDecidesTheKind:
    """#2309 made one name one workflow in a project; #2318 lets its trigger
    decide its kind. A save finds the workflow by name across both kinds and,
    when an edit adds or removes workflow_call, converts it in place."""

    @staticmethod
    def _save(db, project, name, *, listed_reusable, content):
        from types import SimpleNamespace
        from workflows import create_or_update_workflow
        return create_or_update_workflow(
            db, SimpleNamespace(name=name, content=content, original_name=None),
            project.project_id, is_reusable=listed_reusable,
        )

    @staticmethod
    def _rows(db, project):
        return (
            db.query(Workflow).join(ProjectWorkflow)
            .filter(ProjectWorkflow.project_id == project.project_id)
            .all()
        )

    def test_adding_workflow_call_converts_the_workflow_in_place(self, db):
        project = _project(db, "CONV")
        regular = _workflow(db, project, "deploy")

        saved = self._save(db, project, "deploy", listed_reusable=False, content="on: workflow_call")

        assert [w.workflow_id for w in self._rows(db, project)] == [regular.workflow_id]
        assert saved.workflow_id == regular.workflow_id
        assert saved.reusable_workflow is True

    def test_removing_workflow_call_makes_it_regular(self, db):
        project = _project(db, "BACK")
        _workflow(db, project, "deploy", is_reusable=True)

        saved = self._save(db, project, "deploy", listed_reusable=True, content="on: push")

        assert saved.reusable_workflow is False
        assert len(self._rows(db, project)) == 1

    def test_a_new_workflow_takes_the_kind_its_trigger_declares(self, db):
        project = _project(db, "NEWK")

        saved = self._save(db, project, "deploy", listed_reusable=False, content="on: workflow_call")

        assert saved.reusable_workflow is True

    def test_a_stale_list_still_updates_the_one_workflow(self, db):
        """Listed as the other kind, in other casing: still that workflow, and
        its YAML, not the list, says what it is."""
        project = _project(db, "STAL")
        regular = _workflow(db, project, "deploy")

        saved = self._save(db, project, "Deploy", listed_reusable=True, content="on: pull_request")

        assert saved.workflow_id == regular.workflow_id
        assert saved.reusable_workflow is False
        assert len(self._rows(db, project)) == 1

    def test_an_unchanged_save_keeps_the_kind(self, db):
        """A project save posts every workflow. One nobody edited keeps its kind
        even where its YAML and flag already disagree."""
        project = _project(db, "SAME")
        legacy = _workflow(db, project, "deploy")
        legacy.workflow_yaml = "on: workflow_call"
        db.commit()

        saved = self._save(db, project, "deploy", listed_reusable=False, content="on: workflow_call")

        assert saved.reusable_workflow is False

    def test_yaml_that_says_nothing_keeps_the_kind(self, db):
        """Half-typed YAML must not read as 'regular'."""
        project = _project(db, "HALF")
        _workflow(db, project, "deploy", is_reusable=True)

        saved = self._save(db, project, "deploy", listed_reusable=True, content="jobs: [unclosed")

        assert saved.reusable_workflow is True

    def test_a_reusable_workflow_projects_workflows_stay_reusable(self, db):
        project = _project(db, "RWXK")
        project.project_type = "rwx"
        db.commit()
        _workflow(db, project, "deploy", is_reusable=True)

        saved = self._save(db, project, "deploy", listed_reusable=True, content="on: push")

        assert saved.reusable_workflow is True

    def test_an_open_campaign_blocks_the_change(self, db):
        from fastapi import HTTPException
        from models import ProjectPullRequest, ProjectRepo, Repo
        project = _project(db, "OPEN")
        regular = _workflow(db, project, "deploy")
        repo = Repo(repo_name="acme/app")
        db.add(repo); db.commit(); db.refresh(repo)
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        pr = ProjectPullRequest(
            project_id=project.project_id, repo_name="acme/app", pr_number=7,
            pr_url="https://github.com/acme/app/pull/7", pr_state="open",
            branch_name="am-branch", target_branch="main", workflow_names="deploy, build",
        )
        db.add(pr)
        record_pr_carries(db, pr, regular)

        with pytest.raises(HTTPException) as refused:
            self._save(db, project, "deploy", listed_reusable=False, content="on: workflow_call")

        assert refused.value.status_code == 409
        assert "open PR campaign" in refused.value.detail
        db.rollback()
        db.refresh(regular)
        assert regular.reusable_workflow is False

    def test_a_same_name_pair_cannot_collapse_into_one_kind(self, db):
        from fastapi import HTTPException
        project = _project(db, "PAIR")
        _workflow(db, project, "deploy")
        _workflow(db, project, "deploy", is_reusable=True)

        with pytest.raises(HTTPException) as refused:
            self._save(db, project, "deploy", listed_reusable=False, content="on: workflow_call")

        assert refused.value.status_code == 409
        assert "Rename one of them first" in refused.value.detail

    def test_a_pair_that_already_exists_keeps_saving(self, db):
        project = _project(db, "KEEP")
        regular = _workflow(db, project, "deploy")
        _workflow(db, project, "deploy", is_reusable=True)

        self._save(db, project, "deploy", listed_reusable=False, content="on: pull_request")

        db.refresh(regular)
        assert regular.workflow_yaml == "on: pull_request"
        assert regular.reusable_workflow is False

    def test_another_projects_workflow_does_not_count(self, db):
        other = _project(db, "OTHR")
        _workflow(db, other, "deploy")
        project = _project(db, "MINE")

        self._save(db, project, "deploy", listed_reusable=True, content="on: workflow_call")

        mine = self._rows(db, project)
        assert [(w.workflow_name, w.reusable_workflow) for w in mine] == [("deploy", True)]

    def test_a_tombstone_of_the_other_kind_holds_its_name(self, db):
        """A removal campaign deletes the file by name, so a new row of either
        kind would be removed and upserted on one branch. The refusal names the
        campaign, not a workflow the user can no longer see."""
        from fastapi import HTTPException
        project = _project(db, "TOMB")
        tombstone = _workflow(db, project, "deploy")
        tombstone.pending_delete = True
        db.commit()

        with pytest.raises(HTTPException) as refused:
            self._save(db, project, "deploy", listed_reusable=True, content="on: workflow_call")

        assert refused.value.status_code == 409
        assert "queued for removal" in refused.value.detail
