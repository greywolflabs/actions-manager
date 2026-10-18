"""
Tests for keeping persisted drift current when actions resolve drift.

Before this, GET /api/projects/{id}/drift was the only code path that wrote
either drift cache, so resolving/adopting/merging left both claiming drift
that was already fixed until someone re-opened the project and triggered a
live check. The project page seeds its banner from those caches, so a stale
cache showed a drift alert for drift that no longer existed.
"""

import sys
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deliveries  # noqa: E402
from models import (
    Base, Account, Project, Workflow, Repo, ProjectWorkflow,
    NotificationEvent, FileDelivery, ProjectPullRequest,
)
from workflows import _clear_drift_for_merged_pr
from tests.delivery_records import record_delivered, record_drift, record_pr_merged  # noqa: E402
from drift_notifications import (
    clear_drift,
    recompute_project_drift_summary,
    drop_workflow_drift,
)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _setup_project(db, repo_names=("acme/widgets",)):
    account = Account(github_user="driftuser", github_email="drift@example.com", account_type="free")
    db.add(account)
    db.commit()
    db.refresh(account)

    project = Project(project_name="drift_project", project_code="DFT", user_id=account.user_id)
    db.add(project)
    db.commit()
    db.refresh(project)

    workflow = Workflow(workflow_name="ci.yml", workflow_yaml="name: ci")
    db.add(workflow)
    db.commit()
    db.refresh(workflow)

    db.add(ProjectWorkflow(project_id=project.project_id, workflow_id=workflow.workflow_id))
    db.commit()

    repos = []
    for name in repo_names:
        repo = Repo(repo_name=name)
        db.add(repo)
        db.commit()
        db.refresh(repo)
        repos.append(repo)

    return project, workflow, repos


def _drifted_state(db, workflow, repo, has_drift=True, branch="main"):
    # Drift is per (workflow, repo, branch). Defaults to "main" to match
    # _merged_pr's target_branch, which is how production pairs up. A clean
    # result is recorded over a delivery, the only place a check keeps one.
    if not has_drift:
        record_delivered(db, workflow, repo.repo_name, branch)
    record_drift(db, workflow, repo.repo_name, branch, has_drift=has_drift, content_hash="hash-1")


def _row(db, workflow, repo, branch="main"):
    db.expire_all()
    return db.query(FileDelivery).filter_by(workflow_id=workflow.workflow_id, repo_id=repo.repo_id,
                                            branch=branch).one()


def _rows(db):
    db.expire_all()
    return db.query(FileDelivery).all()


def _clear(db, project, workflow_id, repo_name=None, branch=None):
    return clear_drift(db, project, deliveries.WORKFLOW, workflow_id, repo_name, branch)


class TestClearDrift:
    def test_clears_drift_and_emits_resolved_event(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        cleared = _clear(db_session, project, workflow.workflow_id, repo.repo_name)

        assert cleared == 1
        assert _rows(db_session)[0].has_drift is False
        events = db_session.query(NotificationEvent).all()
        assert [e.event_type for e in events] == ["drift.resolved"]

    def test_updates_the_project_level_summary(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        _clear(db_session, project, workflow.workflow_id, repo.repo_name)

        db_session.refresh(project)
        assert project.drift_status == "clean"
        assert project.drift_count == 0

    def test_repo_scope_leaves_other_repos_drifted(self, db_session):
        project, workflow, (repo_a, repo_b) = _setup_project(
            db_session, repo_names=("acme/widgets", "acme/gadgets")
        )
        _drifted_state(db_session, workflow, repo_a)
        _drifted_state(db_session, workflow, repo_b)

        _clear(db_session, project, workflow.workflow_id, repo_a.repo_name)

        by_repo = {s.repo_id: s.has_drift for s in _rows(db_session)}
        assert by_repo[repo_a.repo_id] is False
        assert by_repo[repo_b.repo_id] is True

        db_session.refresh(project)
        assert project.drift_status == "drifted"
        assert project.drift_count == 1

    def test_unknown_repo_name_clears_nothing(self, db_session):
        # Guards the dangerous failure mode: silently falling back to clearing
        # every repo would wrongly resolve drift the caller never touched.
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        cleared = _clear(db_session, project, workflow.workflow_id, "acme/not-a-repo")

        assert cleared == 0
        assert _rows(db_session)[0].has_drift is True

    def test_is_idempotent(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        _clear(db_session, project, workflow.workflow_id, repo.repo_name)
        second = _clear(db_session, project, workflow.workflow_id, repo.repo_name)

        assert second == 0
        # Re-clearing must not emit a duplicate resolved notification.
        assert db_session.query(NotificationEvent).count() == 1

    def test_clearing_records_no_delivery(self, db_session):
        """Every caller that put the file on GitHub records that delivery itself
        (deliveries.record_delivered, or a merge's record_pr_outcome). A clear
        that recorded one too would invent a delivery for a resolution that
        made none."""
        project, workflow, (repo,) = _setup_project(db_session)

        _clear(db_session, project, workflow.workflow_id, repo.repo_name, "main")

        assert _rows(db_session) == []

    def test_clearing_keeps_the_delivery_already_recorded(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        record_delivered(db_session, workflow, repo.repo_name, "main", sha="delivered",
                         at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        _drifted_state(db_session, workflow, repo)

        _clear(db_session, project, workflow.workflow_id, repo.repo_name, "main")

        row = _row(db_session, workflow, repo)
        assert (row.has_drift, row.delivered_sha) == (False, "delivered")
        assert (row.delivered_at.year, row.delivered_at.month) == (2026, 1)

    def test_clearing_every_repo_records_no_delivery(self, db_session):
        """Without a repo and branch there is no pairing to confirm."""
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        _clear(db_session, project, workflow.workflow_id)

        assert _rows(db_session)[0].delivered_at is None

    def test_omitting_repo_clears_every_repo_for_the_workflow(self, db_session):
        project, workflow, (repo_a, repo_b) = _setup_project(
            db_session, repo_names=("acme/widgets", "acme/gadgets")
        )
        _drifted_state(db_session, workflow, repo_a)
        _drifted_state(db_session, workflow, repo_b)

        cleared = _clear(db_session, project, workflow.workflow_id)

        assert cleared == 2
        assert all(s.has_drift is False for s in _rows(db_session))


def _merged_pr(db, project, repo_name, workflow_names, *carried):
    """A merged PR row, with the delivery record its campaign and merge write for *carried*."""
    pr = ProjectPullRequest(
        project_id=project.project_id,
        repo_name=repo_name,
        pr_number=7,
        pr_url="https://github.com/acme/widgets/pull/7",
        pr_state="merged",
        branch_name="actionsmanager/fix",
        target_branch="main",
        workflow_names=workflow_names,
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    record_pr_merged(db, pr, *carried)
    return pr


class TestClearDriftForMergedPR:
    """The webhook merge path has no UI to trigger a live re-check, so without
    this the caches keep reporting drift the merge already fixed."""

    def test_clears_drift_for_the_workflows_the_pr_carried(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)
        pr = _merged_pr(db_session, project, repo.repo_name, "ci.yml", workflow)

        _clear_drift_for_merged_pr(db_session, project, pr)

        assert _row(db_session, workflow, repo).has_drift is False
        db_session.refresh(project)
        assert project.drift_status == "clean"

    def test_leaves_workflows_not_in_the_pr_drifted(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        other_wf = Workflow(workflow_name="release.yml", workflow_yaml="name: release")
        db_session.add(other_wf)
        db_session.commit()
        db_session.refresh(other_wf)
        db_session.add(ProjectWorkflow(
            project_id=project.project_id, workflow_id=other_wf.workflow_id
        ))
        db_session.commit()
        _drifted_state(db_session, other_wf, repo)

        pr = _merged_pr(db_session, project, repo.repo_name, "ci.yml", workflow)
        _clear_drift_for_merged_pr(db_session, project, pr)

        by_wf = {s.workflow_id: s.has_drift for s in _rows(db_session)}
        assert by_wf[workflow.workflow_id] is False
        assert by_wf[other_wf.workflow_id] is True

    def test_leaves_other_repos_drifted(self, db_session):
        project, workflow, (repo_a, repo_b) = _setup_project(
            db_session, repo_names=("acme/widgets", "acme/gadgets")
        )
        _drifted_state(db_session, workflow, repo_a)
        _drifted_state(db_session, workflow, repo_b)

        pr = _merged_pr(db_session, project, repo_a.repo_name, "ci.yml", workflow)
        _clear_drift_for_merged_pr(db_session, project, pr)

        by_repo = {s.repo_id: s.has_drift for s in _rows(db_session)}
        assert by_repo[repo_a.repo_id] is False
        assert by_repo[repo_b.repo_id] is True

    def test_pr_without_recorded_workflows_is_a_no_op(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)
        pr = _merged_pr(db_session, project, repo.repo_name, None)

        _clear_drift_for_merged_pr(db_session, project, pr)

        assert _row(db_session, workflow, repo).has_drift is True


class TestRecomputeProjectDriftSummary:
    def test_reports_drifted_with_a_count(self, db_session):
        project, workflow, (repo_a, repo_b) = _setup_project(
            db_session, repo_names=("acme/widgets", "acme/gadgets")
        )
        _drifted_state(db_session, workflow, repo_a)
        _drifted_state(db_session, workflow, repo_b)

        recompute_project_drift_summary(db_session, project)

        db_session.refresh(project)
        assert project.drift_status == "drifted"
        assert project.drift_count == 2
        assert project.last_drift_check_at is not None

    def test_reports_clean_when_nothing_is_drifted(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo, has_drift=False)

        recompute_project_drift_summary(db_session, project)

        db_session.refresh(project)
        assert project.drift_status == "clean"
        assert project.drift_count == 0

    def test_ignores_other_projects_drift(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        other = Project(project_name="other", project_code="OTH", user_id=project.user_id)
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        recompute_project_drift_summary(db_session, other)

        db_session.refresh(other)
        assert other.drift_status == "clean"
        assert other.drift_count == 0

    def test_resets_the_sweep_backoff_streak(self, db_session):
        # A real answer ("clean" or "drifted"), same as
        # workflows.py's _cache_project_drift_summary — otherwise a project
        # resolved via the UI stays backed off in the sweep despite the
        # dashboard showing it as clean.
        project, workflow, (repo,) = _setup_project(db_session)
        project.drift_check_failure_count = 5
        db_session.commit()
        _drifted_state(db_session, workflow, repo, has_drift=False)

        recompute_project_drift_summary(db_session, project)

        db_session.refresh(project)
        assert project.drift_check_failure_count == 0


class TestDropWorkflowDrift:
    def test_resets_its_drift_and_recomputes(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        drop_workflow_drift(db_session, project, workflow.workflow_id)

        row = _row(db_session, workflow, repo)
        assert (row.has_drift, row.github_sha, row.content_hash) == (False, None, None)
        db_session.refresh(project)
        assert project.drift_status == "clean"
        assert project.drift_count == 0

    def test_keeps_what_was_delivered(self, db_session):
        # The record of a delivery outlives the drift check that saw it: only
        # what the checks saw is forgotten.
        project, workflow, (repo,) = _setup_project(db_session)
        record_delivered(db_session, workflow, repo.repo_name, "main", sha="delivered")
        _drifted_state(db_session, workflow, repo)

        drop_workflow_drift(db_session, project, workflow.workflow_id)

        row = _row(db_session, workflow, repo)
        assert (row.has_drift, row.delivered_sha) == (False, "delivered")
        assert row.delivered_at is not None

    def test_leaves_other_workflows_in_the_same_project_alone(self, db_session):
        project, workflow, (repo,) = _setup_project(db_session)
        _drifted_state(db_session, workflow, repo)

        kept = Workflow(workflow_name="release.yml", workflow_yaml="name: release")
        db_session.add(kept)
        db_session.commit()
        db_session.refresh(kept)
        db_session.add(ProjectWorkflow(project_id=project.project_id, workflow_id=kept.workflow_id))
        db_session.commit()
        _drifted_state(db_session, kept, repo)

        drop_workflow_drift(db_session, project, workflow.workflow_id)

        remaining = [row for row in _rows(db_session) if row.has_drift]
        assert len(remaining) == 1
        assert remaining[0].workflow_id == kept.workflow_id
        db_session.refresh(project)
        assert project.drift_count == 1
