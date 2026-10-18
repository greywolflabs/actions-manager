"""
Tests for the owner index migration (#2346).

It runs on every boot, and restore runs it while the app is serving, so a run
over a database that already has the index must not touch projects.
"""
import os
import sqlite3
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate_add_projects_user_id_index as migration  # noqa: E402
from models import Project  # noqa: E402

OLD_SCHEMA = """
CREATE TABLE projects (
    project_id INTEGER PRIMARY KEY,
    project_name VARCHAR(255) NOT NULL,
    user_id INTEGER NOT NULL,
    next_drift_check_at DATETIME
);
INSERT INTO projects VALUES (1, 'one', 7, NULL);
"""


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "projects.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def _sql(path, statement):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(statement).fetchall()
    finally:
        conn.close()


def _indexes(path):
    return {row[1] for row in _sql(path, "PRAGMA index_list(projects)")}


def test_it_indexes_each_projects_owner_then_next_check(db_path):
    _run(db_path)

    assert [row[2] for row in _sql(db_path, f"PRAGMA index_info({migration.INDEX})")] == list(migration.COLUMNS)


def test_it_is_the_index_a_fresh_install_gets_from_the_model(db_path):
    """create_all and the migration must agree, or fresh and upgraded
    installs end up with different indexes."""
    model_index = next(index for index in Project.__table__.indexes if index.name == migration.INDEX)

    assert tuple(column.name for column in model_index.columns) == migration.COLUMNS


def test_a_second_run_leaves_the_index_alone(db_path, capsys):
    _run(db_path)
    capsys.readouterr()

    _run(db_path)

    assert "created" not in capsys.readouterr().out
    assert _indexes(db_path) == {migration.INDEX}


def test_an_owners_projects_are_looked_up_through_it(db_path):
    _run(db_path)

    plan = [row[-1] for row in _sql(db_path, "EXPLAIN QUERY PLAN SELECT project_id FROM projects WHERE user_id = 7")]
    assert any(migration.INDEX in step for step in plan), plan


def test_a_database_without_projects_is_skipped(db_path, capsys):
    _sql(db_path, "DROP TABLE projects")

    _run(db_path)

    assert "skipping" in capsys.readouterr().out
