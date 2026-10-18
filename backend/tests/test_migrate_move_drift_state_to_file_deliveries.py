"""
Tests for migrate_move_drift_state_to_file_deliveries: the drift columns are
added to file_deliveries where missing, both drift state tables are copied onto
it once, and then dropped.
"""
import os
import sqlite3
import sys
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate_add_file_deliveries  # noqa: E402
import migrate_move_drift_state_to_file_deliveries as migration  # noqa: E402
from models import (  # noqa: E402
    Account, Base, Codeowners, FileDelivery, Project, ProjectWorkflow, Repo, Workflow,
)
from tests import legacy_drift_tables as legacy  # noqa: E402

LEGACY_TABLES = ("codeowners_drift_states", "workflow_drift_states")

CHECKED = datetime(2026, 9, 1, 12, 0, 0)
DELIVERED = datetime(2026, 8, 1, 12, 0, 0)


def _seed(engine):
    """Drift states as the app leaves them before this migration, and the record PR A wrote."""
    db = sessionmaker(bind=engine)()
    try:
        db.add(Account(user_id=1, github_user="owner", github_email="o@e.com", account_type="free"))
        db.add_all([
            Project(project_id=1, project_code="P1", project_name="one", user_id=1, project_type="standard"),
            Repo(repo_id=10, repo_name="acme/app"),
            Repo(repo_id=11, repo_name="acme/web"),
            Workflow(workflow_id=100, workflow_name="ci", workflow_yaml="on: push"),
            # Its project link is gone, so it has no owner to give a new row.
            Workflow(workflow_id=101, workflow_name="orphan", workflow_yaml="on: push"),
        ])
        db.flush()
        db.add_all([
            ProjectWorkflow(project_id=1, workflow_id=100),
            Codeowners(id=300, project_id=1, content="* @a", file_path=".github/CODEOWNERS"),
            # Delivered and drifted: the copy lands on the existing row.
            FileDelivery(project_id=1, repo_id=10, branch="main", workflow_id=100,
                         delivered_sha="delivered-blob", delivered_at=DELIVERED),
        ])
        db.execute(legacy.workflow_drift_states.insert(), [
            dict(project_id=1, workflow_id=100, repo_id=10, branch="main", has_drift=True,
                 deleted_in_github=False, github_sha="their-blob", content_hash="h1",
                 drift_cycle_count=2, last_checked_at=CHECKED),
            # Checked clean where nothing was recorded: a new row with no delivery.
            dict(project_id=1, workflow_id=100, repo_id=11, branch="dev", has_drift=False,
                 deleted_in_github=False, github_sha="dev-blob", content_hash="h2", drift_cycle_count=0,
                 last_checked_at=CHECKED),
            dict(project_id=1, workflow_id=101, repo_id=10, branch="main", has_drift=True,
                 deleted_in_github=False, github_sha=None, content_hash=None, drift_cycle_count=0,
                 last_checked_at=CHECKED),
        ])
        db.execute(legacy.codeowners_drift_states.insert(), [
            dict(project_id=1, repo_id=10, branch="main", has_drift=True, deleted_in_github=True,
                 github_sha=None, etag='W/"co"', content_hash="h3", drift_cycle_count=1,
                 last_checked_at=CHECKED),
        ])
        db.commit()
    finally:
        db.close()


def _drop_drift_columns(path):
    """file_deliveries as migrate_add_file_deliveries created it, before these columns."""
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT project_id, repo_id, branch, workflow_id, delivered_sha, delivered_at "
                                 "FROM file_deliveries")).mappings().all()
        conn.execute(text("DROP TABLE file_deliveries"))
        migrate_add_file_deliveries._create_table(conn, True)
        for row in rows:
            conn.execute(text("INSERT INTO file_deliveries (project_id, repo_id, branch, workflow_id, delivered_sha, "
                              "delivered_at, pending_delete) VALUES (:project_id, :repo_id, :branch, :workflow_id, "
                              ":delivered_sha, :delivered_at, 0)"), dict(row))
    engine.dispose()


@pytest.fixture(params=["created_by_the_orm", "created_before_these_columns"])
def db_path(tmp_path, request):
    path = str(tmp_path / "drift.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    legacy.create(engine)
    _seed(engine)
    engine.dispose()
    if request.param == "created_before_these_columns":
        _drop_drift_columns(path)
    return path


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute(
            "SELECT project_id, repo_id, branch, workflow_id, codeowners_id, delivered_sha, delivered_at IS NOT NULL, "
            "has_drift, deleted_in_github, github_sha, etag, content_hash, drift_cycle_count, last_checked_at "
            "FROM file_deliveries"
        ).fetchall(), key=repr)
    finally:
        conn.close()


def _record(path):
    """file_deliveries' rows, by the columns it has before and after the migration."""
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute(
            "SELECT id, project_id, repo_id, branch, workflow_id, codeowners_id, delivered_sha FROM file_deliveries"
        ).fetchall(), key=repr)
    finally:
        conn.close()


def _legacy_tables(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if name in LEGACY_TABLES)
    finally:
        conn.close()


def _columns(path):
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(file_deliveries)")}
    finally:
        conn.close()


CHECKED_STORED = "2026-09-01 12:00:00.000000"


def test_both_tables_are_copied_and_missing_rows_record_no_delivery(db_path):
    _run(db_path)

    assert set(migration.COLUMNS) <= _columns(db_path)
    assert _rows(db_path) == sorted([
        # The delivered row keeps its delivery and gains the drift.
        (1, 10, "main", 100, None, "delivered-blob", 1, 1, 0, "their-blob", None, "h1", 2, CHECKED_STORED),
        # A state with no delivery row: a new one that says nothing was delivered.
        (1, 11, "dev", 100, None, None, 0, 0, 0, "dev-blob", None, "h2", 0, CHECKED_STORED),
        # CODEOWNERS, ETag included. The orphaned workflow's state is skipped.
        (1, 10, "main", None, 300, None, 0, 1, 1, None, 'W/"co"', "h3", 1, CHECKED_STORED),
    ], key=repr)
    assert _legacy_tables(db_path) == []


def test_a_copy_that_fails_keeps_the_old_tables(db_path):
    """Dropped in the copy's own transaction, so nothing is lost when the copy is."""
    before = _record(db_path)
    with patch.object(migration, "_copy_states", side_effect=RuntimeError("copy failed")):
        with pytest.raises(RuntimeError):
            _run(db_path)

    assert _legacy_tables(db_path) == sorted(LEGACY_TABLES)
    assert _record(db_path) == before

    _run(db_path)

    assert _legacy_tables(db_path) == []


def test_running_again_changes_nothing(db_path):
    _run(db_path)
    first = _rows(db_path)

    _run(db_path)

    assert _rows(db_path) == first


def test_old_state_found_once_a_check_has_recorded_a_row_is_dropped_not_copied(db_path):
    """The copy would overwrite what the new checks recorded with older state."""
    _run(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE file_deliveries SET has_drift = 0, content_hash = 'new' WHERE workflow_id = 100 "
                 "AND branch = 'main'")
    conn.commit()
    conn.close()
    kept = _rows(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    legacy.create(engine)
    with engine.begin() as conn:
        conn.execute(legacy.workflow_drift_states.insert(), [
            dict(project_id=1, workflow_id=100, repo_id=10, branch="main", has_drift=True, drift_cycle_count=9,
                 last_checked_at=CHECKED),
        ])
    engine.dispose()

    _run(db_path)

    assert _rows(db_path) == kept
    assert _legacy_tables(db_path) == []


def test_a_state_without_a_check_time_still_closes_the_gate(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE workflow_drift_states SET last_checked_at = NULL")
    conn.execute("DELETE FROM codeowners_drift_states")
    conn.commit()
    conn.close()

    _run(db_path)

    conn = sqlite3.connect(db_path)
    try:
        unchecked = conn.execute("SELECT COUNT(*) FROM file_deliveries WHERE workflow_id = 100 "
                                 "AND last_checked_at IS NULL").fetchone()[0]
    finally:
        conn.close()
    assert unchecked == 0


def test_without_file_deliveries_it_does_nothing(tmp_path):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()

    _run(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        conn.close()


def test_without_the_old_tables_it_only_adds_the_columns(tmp_path):
    path = str(tmp_path / "new.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    _drop_drift_columns(path)

    _run(path)

    assert set(migration.COLUMNS) <= _columns(path)
    assert _rows(path) == []


def test_a_fresh_database_never_has_the_old_tables():
    assert not set(LEGACY_TABLES) & set(Base.metadata.tables)
