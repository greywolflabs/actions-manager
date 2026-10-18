"""
Tests for migrate_add_file_deliveries: the table is created where missing, and
backfilled once from what the drift states and open PRs already know.
"""
import os
import sqlite3
import sys
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate_add_file_deliveries as migration  # noqa: E402
from models import (  # noqa: E402
    Account, Base, Codeowners, CustomFile, LinkedReusableWorkflow, Project,
    ProjectPullRequest, ProjectRepo, ProjectWorkflow, Repo, Workflow,
)
from tests import legacy_drift_tables as legacy  # noqa: E402

SEEN = "2026-08-01 12:00:00"


def _at(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _stored(text):
    """How SQLite holds a DateTime the ORM wrote, which the backfill copies as is."""
    return f"{text}.000000"


def _seed(engine):
    """Projects, files, drift states and PRs as the app leaves them before this migration."""
    db = sessionmaker(bind=engine)()
    try:
        db.add(Account(user_id=1, github_user="owner", github_email="o@e.com", account_type="free"))
        db.add_all([
            Project(project_id=1, project_code="CALL", project_name="caller", user_id=1, project_type="standard"),
            Project(project_id=2, project_code="RWX1", project_name="shared", user_id=1, project_type="rwx"),
            Repo(repo_id=10, repo_name="acme/app"),
            Repo(repo_id=11, repo_name="acme/shared"),
            Workflow(workflow_id=100, workflow_name="ci", workflow_yaml="on: push"),
            Workflow(workflow_id=101, workflow_name="lint", workflow_yaml="on: push"),
            Workflow(workflow_id=102, workflow_name="deploy", workflow_yaml="on: workflow_call", reusable_workflow=True),
        ])
        db.flush()
        db.add_all([
            ProjectWorkflow(project_id=1, workflow_id=100),
            ProjectWorkflow(project_id=1, workflow_id=101),
            ProjectWorkflow(project_id=2, workflow_id=102),
            LinkedReusableWorkflow(standard_project_id=1, rwx_project_id=2, workflow_id=102),
            CustomFile(id=200, project_id=1, file_path="scripts/build.sh", file_content="echo"),
            Codeowners(id=300, project_id=1, content="* @a", file_path=".github/CODEOWNERS"),
        ])
        # 1. Drift states: seen clean, seen drifted, never seen.
        db.execute(legacy.workflow_drift_states.insert(), [
            dict(project_id=1, workflow_id=100, repo_id=10, branch="main", has_drift=False,
                 github_sha="ci-blob", confirmed_present_at=_at(SEEN)),
            dict(project_id=1, workflow_id=100, repo_id=10, branch="dev", has_drift=True,
                 github_sha="someone-elses-blob", confirmed_present_at=_at("2026-08-02 12:00:00")),
            dict(project_id=1, workflow_id=101, repo_id=10, branch="main", has_drift=False,
                 github_sha=None, confirmed_present_at=None),
        ])
        # 2. CODEOWNERS, seen clean.
        db.execute(legacy.codeowners_drift_states.insert(), [
            dict(project_id=1, repo_id=10, branch="main", has_drift=False,
                 github_sha="co-blob", confirmed_present_at=_at("2026-08-03 12:00:00")),
        ])
        # 3. PRs: open to a repo with no row yet, open to a delivered branch,
        # merged, and a standalone CODEOWNERS PR from before CODEOWNERS rode campaigns.
        for pr_id, repo, state, target, workflow_names, file_names in (
            (1, "acme/new", "open", "main", "ci, deploy, missing",
             "scripts/build.sh, .github/CODEOWNERS, nope.txt"),
            (2, "acme/app", "open", "main", "ci", None),
            (3, "acme/app", "merged", "release", "lint", None),
            (4, "acme/app", "open", "legacy", "CODEOWNERS", None),
        ):
            db.add(ProjectPullRequest(
                pr_id=pr_id, project_id=1, repo_name=repo, pr_number=pr_id, pr_url=f"u{pr_id}",
                pr_state=state, branch_name=f"am/{pr_id}", target_branch=target,
                workflow_names=workflow_names, file_names=file_names,
            ))
        db.commit()
    finally:
        db.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute(
            "SELECT d.project_id, r.repo_name, d.branch, d.workflow_id, d.custom_file_id, d.codeowners_id, "
            "d.delivered_sha, d.delivered_at, d.pending_pr_id, d.pending_sha, d.pending_path, d.pending_delete "
            "FROM file_deliveries d JOIN repos r ON r.repo_id = d.repo_id"
        ).fetchall(), key=repr)
    finally:
        conn.close()


@pytest.fixture(params=["created_by_the_orm", "created_by_the_migration"])
def db_path(tmp_path, request):
    """Every ORM table, seeded; file_deliveries left to the migration in one variant.

    run_migrations.py runs create_all first, so the table normally exists and is
    empty; the other variant covers running the script on its own.
    """
    path = str(tmp_path / "deliveries.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    legacy.create(engine)
    _seed(engine)
    if request.param == "created_by_the_migration":
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE file_deliveries")
        conn.commit()
        conn.close()
    return path


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def test_the_backfill_records_what_drift_and_open_prs_know(db_path):
    _run(db_path)

    assert _rows(db_path) == sorted([
        # 1. Seen by a check: delivered then; the SHA only when that check found no drift.
        (1, "acme/app", "main", 100, None, None, "ci-blob", _stored(SEEN), 2, None, None, 0),
        (1, "acme/app", "dev", 100, None, None, None, _stored("2026-08-02 12:00:00"), None, None, None, 0),
        # 2. CODEOWNERS the same way.
        (1, "acme/app", "main", None, None, 300, "co-blob", _stored("2026-08-03 12:00:00"), None, None, None, 0),
        # 3. Open PRs: by exact name to the project's own and linked workflows (the
        # linked one owned by its RWX project), by path to custom files and CODEOWNERS.
        (1, "acme/new", "main", 100, None, None, None, None, 1, None, None, 0),
        (2, "acme/new", "main", 102, None, None, None, None, 1, None, None, 0),
        (1, "acme/new", "main", None, 200, None, None, None, 1, None, None, 0),
        (1, "acme/new", "main", None, None, 300, None, None, 1, None, None, 0),
        # A standalone CODEOWNERS PR from before CODEOWNERS rode campaigns.
        (1, "acme/app", "legacy", None, None, 300, None, None, 4, None, None, 0),
    ], key=repr)


def test_a_linked_name_on_a_pr_to_the_callers_own_repo_is_the_callers_workflow(db_path):
    """A linked workflow is delivered to its RWX project's repository, never the
    caller's. A caller owning a deploy of its own and linking RWX's deploy: its
    PR to its own repo carries its own, and must not lock RWX's for everyone."""
    db = sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))()
    try:
        db.add_all([ProjectRepo(project_id=1, repo_id=10),
                    Workflow(workflow_id=103, workflow_name="deploy", workflow_yaml="on: push")])
        db.flush()
        db.add(ProjectWorkflow(project_id=1, workflow_id=103))
        db.add(ProjectPullRequest(pr_id=5, project_id=1, repo_name="acme/app", pr_number=5, pr_url="u5",
                                  pr_state="open", branch_name="am/5", target_branch="feature",
                                  workflow_names="deploy"))
        db.commit()
    finally:
        db.close()

    _run(db_path)

    workflow_id, pending_pr_id = 3, 8
    assert [row[workflow_id] for row in _rows(db_path) if row[pending_pr_id] == 5] == [103]


def test_a_repo_an_open_pr_names_gets_a_row(db_path):
    _run(db_path)

    conn = sqlite3.connect(db_path)
    try:
        names = [name for (name,) in conn.execute("SELECT repo_name FROM repos ORDER BY repo_name")]
        project_repos = conn.execute("SELECT COUNT(*) FROM project_repos").fetchone()[0]
    finally:
        conn.close()
    assert names == ["acme/app", "acme/new", "acme/shared"]
    assert project_repos == 0


def test_running_again_changes_nothing(db_path):
    _run(db_path)
    first = _rows(db_path)

    _run(db_path)

    assert _rows(db_path) == first


def test_a_table_that_already_holds_deliveries_is_not_backfilled(db_path):
    _run(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM file_deliveries WHERE workflow_id IS NOT NULL")
    conn.commit()
    conn.close()
    kept = _rows(db_path)
    assert kept

    _run(db_path)

    assert _rows(db_path) == kept


def test_the_constraints_hold_on_a_table_the_migration_created(tmp_path):
    path = str(tmp_path / "fresh.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    legacy.create(engine)
    _seed(engine)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE file_deliveries")
    conn.commit()
    conn.close()

    _run(path)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):  # no file at all
            conn.execute("INSERT INTO file_deliveries (project_id, repo_id, branch, pending_delete) "
                         "VALUES (1, 10, 'x', 0)")
        with pytest.raises(sqlite3.IntegrityError):  # the same workflow on the same branch twice
            conn.execute("INSERT INTO file_deliveries (project_id, repo_id, branch, workflow_id, pending_delete) "
                         "VALUES (1, 10, 'main', 100, 0)")
        indexes = {name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='file_deliveries'")}
    finally:
        conn.close()
    assert {f"ix_file_deliveries_{c}" for c in migration.INDEXED} <= indexes


def test_without_the_drift_tables_open_prs_are_still_backfilled(tmp_path):
    """A database restored from before the record, whose drift tables were already dropped."""
    path = str(tmp_path / "no-drift.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    legacy.create(engine)
    _seed(engine)
    engine.dispose()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE workflow_drift_states")
    conn.execute("DROP TABLE codeowners_drift_states")
    conn.commit()
    conn.close()

    _run(path)

    rows = _rows(path)
    assert rows
    # Each one carried by an open PR, none delivered: only a drift check knew that.
    assert all(row[7] is None for row in rows)
    assert all(row[8] is not None for row in rows)


def test_without_the_parent_tables_it_does_nothing(tmp_path):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()

    _run(path)

    conn = sqlite3.connect(path)
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    assert tables == []
