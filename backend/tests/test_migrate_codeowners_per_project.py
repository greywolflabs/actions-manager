"""
Tests for migrate_codeowners_per_project: per-(project, repo) CODEOWNERS rows
become one file per project, the most recently saved winning, and the
per-branch baselines of the previous design are dropped.
"""
import hashlib
import os
import sqlite3
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate_codeowners_per_project as migration

ZEROS = "0" * 40

OLD_SCHEMA = """
CREATE TABLE projects (project_id INTEGER PRIMARY KEY, project_name TEXT);
CREATE TABLE repos (repo_id INTEGER PRIMARY KEY, repo_name TEXT);
CREATE TABLE project_repos (id INTEGER PRIMARY KEY, project_id INTEGER, repo_id INTEGER);
CREATE TABLE codeowners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    repo_id INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    file_path VARCHAR(64) NOT NULL DEFAULT '.github/CODEOWNERS',
    git_hash VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'new',
    last_modified_by VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY(repo_id) REFERENCES repos(repo_id) ON DELETE CASCADE,
    CONSTRAINT uq_codeowners_project_repo UNIQUE (project_id, repo_id)
);
CREATE TABLE codeowners_branch_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    codeowners_id INTEGER NOT NULL REFERENCES codeowners(id) ON DELETE CASCADE,
    branch VARCHAR(255) NOT NULL,
    git_hash VARCHAR(255)
);
CREATE TABLE project_pull_requests (pr_id INTEGER PRIMARY KEY, repo_name TEXT, codeowners_sha VARCHAR(64));
INSERT INTO projects VALUES (1, 'one'), (2, 'two'), (3, 'three'), (4, 'four');
INSERT INTO repos VALUES (10, 'octo/a'), (11, 'octo/b');
INSERT INTO project_repos (project_id, repo_id) VALUES (1, 10), (1, 11), (2, 10), (2, 11), (3, 10), (3, 11), (4, 10), (4, 11);
INSERT INTO project_pull_requests VALUES (1, 'octo/a', 'abc');
"""


def _blob(content):
    data = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _row(conn, project_id, repo_id, content, status, updated_at, git_hash="sha"):
    conn.execute(
        "INSERT INTO codeowners (project_id, repo_id, content, git_hash, status, last_modified_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'someone', ?)",
        (project_id, repo_id, content, git_hash, status, updated_at),
    )


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "codeowners.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    # 1: different files per repo, the newer one wins.
    _row(conn, 1, 10, "* @older\n", "synced_with_github", "2026-01-01 10:00:00")
    _row(conn, 1, 11, "* @newer\n", "synced_with_github", "2026-02-01 10:00:00")
    # 2: the same file, synced on every repo of the project.
    _row(conn, 2, 10, "* @same\n", "synced_with_github", "2026-01-01 10:00:00")
    _row(conn, 2, 11, "* @same\n", "under_review", "2026-01-02 10:00:00")
    # 3: one repo's file, the other repo never had one.
    _row(conn, 3, 10, "* @only-a\n", "synced_with_github", "2026-01-01 10:00:00")
    # 4: a draft.
    _row(conn, 4, 11, "* @draft\n", "new", "2026-01-01 10:00:00", git_hash=None)
    conn.execute("INSERT INTO codeowners_branch_states (codeowners_id, branch, git_hash) VALUES (1, 'main', 'x')")
    conn.commit()
    conn.close()
    return path


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def _kept(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT project_id, content, status, git_hash FROM codeowners ORDER BY project_id")
        return {project_id: (content, status, git_hash) for project_id, content, status, git_hash in rows}
    finally:
        conn.close()


def test_each_project_keeps_its_most_recently_saved_file(db_path):
    _run(db_path)

    kept = _kept(db_path)
    assert {project: content for project, (content, _, _) in kept.items()} == {
        1: "* @newer\n", 2: "* @same\n", 3: "* @only-a\n", 4: "* @draft\n",
    }


def test_a_file_every_repo_held_keeps_its_delivered_state(db_path):
    _run(db_path)

    # Synced on one repo and waiting on its PR on the other: still under review.
    assert _kept(db_path)[2] == ("* @same\n", "under_review", _blob("* @same\n"))


def test_any_other_file_becomes_a_local_change(db_path):
    _run(db_path)

    kept = _kept(db_path)
    # A repo that held another copy, or none, reads "pending sync" rather than drift.
    assert kept[1] == ("* @newer\n", "committed_locally", ZEROS)
    assert kept[3] == ("* @only-a\n", "committed_locally", ZEROS)
    assert kept[4] == ("* @draft\n", "new", ZEROS)


def test_the_table_is_one_row_per_project(db_path):
    _run(db_path)

    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(codeowners)")]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO codeowners (project_id, content) VALUES (1, 'again')")
    finally:
        conn.close()
    assert "repo_id" not in columns


def test_the_per_branch_baselines_go(db_path):
    _run(db_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        pr_columns = [row[1] for row in conn.execute("PRAGMA table_info(project_pull_requests)")]
        prs = conn.execute("SELECT pr_id, repo_name FROM project_pull_requests").fetchall()
    finally:
        conn.close()
    assert "codeowners_branch_states" not in tables
    # Its drift is kept on file_deliveries now.
    assert "codeowners_drift_states" not in tables
    assert "codeowners_sha" not in pr_columns
    assert prs == [(1, "octo/a")]


def test_running_again_changes_nothing(db_path):
    _run(db_path)
    first = _kept(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE codeowners SET status = 'synced_with_github' WHERE project_id = 1")
    conn.commit()
    conn.close()

    _run(db_path)

    assert _kept(db_path)[1] == ("* @newer\n", "synced_with_github", first[1][2])
    assert _kept(db_path)[2] == first[2]
