"""
Tests for the projects.file_kind migration (#2367).

It runs on every boot, so a second run must change nothing, and existing
projects must stay NULL: only a Managed Files project has a file kind.
"""
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

import migrate_add_project_file_kind as migration

OLD_SCHEMA = """
CREATE TABLE projects (
    project_id INTEGER PRIMARY KEY,
    project_name VARCHAR(255) NOT NULL,
    project_type VARCHAR(20) NOT NULL DEFAULT 'standard'
);
INSERT INTO projects (project_id, project_name, project_type) VALUES (1, 'caller', 'standard');
INSERT INTO projects (project_id, project_name, project_type) VALUES (2, 'reusable', 'rwx');
"""


@pytest.fixture
def db_path():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()
    yield path
    os.remove(path)


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT project_id, file_kind FROM projects ORDER BY project_id").fetchall()
    finally:
        conn.close()


def test_adds_the_column_and_leaves_existing_projects_null(db_path):
    _run(db_path)

    assert _rows(db_path) == [(1, None), (2, None)]


def test_a_second_run_changes_nothing(db_path):
    _run(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE projects SET file_kind = 'codeowners' WHERE project_id = 2")
    conn.commit()
    conn.close()

    _run(db_path)

    assert _rows(db_path) == [(1, None), (2, "codeowners")]


def test_skips_a_database_without_the_projects_table():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        _run(path)  # a fresh install: models.py creates the table with the column

        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        finally:
            conn.close()
    finally:
        os.remove(path)


def test_is_registered_with_the_runner():
    from run_migrations import MIGRATION_SCRIPTS

    assert "migrate_add_project_file_kind.py" in MIGRATION_SCRIPTS
    # The last two purge and constrain every table, so they must stay last.
    assert MIGRATION_SCRIPTS.index("migrate_add_project_file_kind.py") < len(MIGRATION_SCRIPTS) - 2
