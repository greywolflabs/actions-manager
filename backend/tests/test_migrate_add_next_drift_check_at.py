"""
Tests for the stored-drift-schedule migration (#2343).

It adds the columns and indexes, then brings every project's stored time to
what its inputs derive, with a frozen copy of the app's formula. It runs on every
boot, so a run over an up-to-date table must write nothing, must not take a lock
on projects to re-create indexes that exist, and must still correct a time that
older code or a restored backup left behind.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drift_worker  # noqa: E402
import migrate_add_next_drift_check_at as migration  # noqa: E402
from models import DriftSettings  # noqa: E402

CHECKED_AT = "2026-01-01 12:00:00.000000"
EDITED_AT = "2025-06-01 09:00:00.000000"

OLD_SCHEMA = f"""
CREATE TABLE projects (
    project_id INTEGER PRIMARY KEY,
    project_name VARCHAR(255) NOT NULL,
    last_drift_check_at DATETIME,
    drift_check_failure_count INTEGER NOT NULL DEFAULT 0,
    drift_check_interval_minutes INTEGER,
    updated_at DATETIME
);
INSERT INTO projects VALUES (1, 'never-checked', NULL, 0, NULL, '{EDITED_AT}');
INSERT INTO projects VALUES (2, 'inherits', '{CHECKED_AT}', 0, NULL, '{EDITED_AT}');
INSERT INTO projects VALUES (3, 'backed-off-daily', '{CHECKED_AT}', 3, 1440, '{EDITED_AT}');
INSERT INTO projects VALUES (4, 'switched-off', '{CHECKED_AT}', 0, 0, '{EDITED_AT}');
"""


@pytest.fixture
def db_path():
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{path}")
    DriftSettings.__table__.create(engine)
    engine.dispose()
    _sql(path, "INSERT INTO drift_settings (sweep_enabled, recheck_interval_minutes, batch_size, "
               "poll_interval_seconds) VALUES (1, 60, 5, 60)")
    yield path
    if os.path.exists(path):
        os.remove(path)


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def _sql(path, statement):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(statement).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _schedules(path):
    return {name: (when, marker) for name, when, marker in _sql(
        path, "SELECT project_name, next_drift_check_at, drift_check_scheduled_minutes FROM projects")}


def test_it_adds_the_columns_and_indexes_what_the_sweep_looks_up(db_path):
    _run(db_path)

    assert {"next_drift_check_at", "drift_check_scheduled_minutes"} <= {
        row[1] for row in _sql(db_path, "PRAGMA table_info(projects)")}
    assert set(migration.INDEXES) <= {row[1] for row in _sql(db_path, "PRAGMA index_list(projects)")}


def test_every_project_is_scheduled_as_its_inputs_derive(db_path):
    _run(db_path)

    assert _schedules(db_path) == {
        "never-checked": (None, 60),                                 # NULL is "due now"
        "inherits": ("2026-01-01 13:00:00.000000", 60),              # the saved 60-minute default
        "backed-off-daily": ("2026-01-05 12:00:00.000000", 1440),    # 1440 x 4 for a streak of 3
        "switched-off": (CHECKED_AT, 0),                             # never consulted while off
    }


def test_without_saved_settings_the_built_in_default_is_used(db_path):
    _sql(db_path, "DROP TABLE drift_settings")

    _run(db_path)

    assert _schedules(db_path)["inherits"] == ("2026-01-01 12:15:00.000000", 15)


def test_a_time_left_stale_by_older_code_is_corrected(db_path):
    """Upgrade, roll back, upgrade again: the older code moved the inputs
    without the time, and leaves a marker that no longer matches them."""
    _run(db_path)
    _sql(db_path, "UPDATE projects SET last_drift_check_at = '2026-02-01 12:00:00.000000' WHERE project_id = 2")

    _run(db_path)

    assert _schedules(db_path)["inherits"] == ("2026-02-01 13:00:00.000000", 60)


def test_an_up_to_date_table_is_not_rewritten(db_path, capsys):
    _run(db_path)
    capsys.readouterr()

    _run(db_path)

    output = capsys.readouterr().out
    assert "(0 schedules brought up to date)" in output
    assert "➕" not in output


def test_a_table_larger_than_one_chunk_converges_a_transaction_at_a_time(db_path):
    """Restore runs this while the app is serving."""
    with patch.object(migration, "CHUNK", 1):
        _run(db_path)

    assert _schedules(db_path)["backed-off-daily"] == ("2026-01-05 12:00:00.000000", 1440)
    assert _schedules(db_path)["switched-off"] == (CHECKED_AT, 0)


def test_a_check_committing_mid_converge_is_not_overwritten(db_path):
    """Restore runs this while the app is serving, and the app may be checking
    a project between the read and the write."""
    real_next_check_at = migration.next_check_at
    interleaved = []

    def a_check_commits_first(*args):
        if not interleaved:
            interleaved.append(True)
            _sql(db_path, "UPDATE projects SET last_drift_check_at = '2026-03-01 12:00:00.000000', "
                          "next_drift_check_at = '2026-03-01 13:00:00.000000', "
                          "drift_check_scheduled_minutes = 60 WHERE project_id = 2")
        return real_next_check_at(*args)

    with patch.object(migration, "next_check_at", side_effect=a_check_commits_first):
        _run(db_path)

    assert interleaved
    assert _schedules(db_path)["inherits"] == ("2026-03-01 13:00:00.000000", 60)


def test_scheduling_is_not_an_edit(db_path):
    """The dashboard orders projects by updated_at."""
    _run(db_path)

    assert {row[0] for row in _sql(db_path, "SELECT updated_at FROM projects")} == {EDITED_AT}


@pytest.mark.parametrize("streak", range(8))
@pytest.mark.parametrize("interval", [None, 0, 15, 1440])
@pytest.mark.parametrize("last", [None, datetime(2026, 1, 1, 12, 0)])
def test_the_frozen_formula_still_matches_the_app(streak, interval, last):
    """This migration keeps its own copy so it cannot change meaning when the
    app's does. If this fails, the app's formula changed: add a migration that
    re-converges stored times on it, rather than editing this one."""
    for default in (15, 60):
        assert migration.next_check_at(last, interval, streak, default) == drift_worker.next_check_at(
            last, interval, streak, default)


def test_a_database_without_projects_is_skipped(db_path):
    _sql(db_path, "DROP TABLE projects")

    _run(db_path)
