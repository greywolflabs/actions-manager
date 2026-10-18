"""
Migration: one CODEOWNERS file per project.

CODEOWNERS used to be stored per (project, repo), each repository with its own
draft. A project now owns one file, delivered to every repository and branch it
delivers to, and its drift follows the workflow rules. So:

1. ``codeowners`` loses ``repo_id`` and becomes unique per project. A project
   with several rows keeps the most recently saved one. It stays synced (or
   under review) only when every repository of the project held exactly that
   file and was synced (or waiting on its pull request). Anything else becomes
   a local change waiting to be delivered, with the zeroed hash workflows use
   for one, so a repository that held a different copy reads "pending sync"
   rather than drift.
2. The per-branch baselines of the previous design go:
   ``codeowners_branch_states`` and ``project_pull_requests.codeowners_sha``.

Its drift is kept on the delivery record (file_deliveries), like every file's.

Idempotent, and supports both SQLite and PostgreSQL.
"""

import hashlib
import sqlite3

from sqlalchemy import create_engine, text

from migration_utils import get_migration_database_url

# workflows.LOCAL_COMMIT_HASH, not imported: migrations stay free of the app modules.
LOCAL_COMMIT_HASH = "0" * 40
DELIVERED = {"synced_with_github", "under_review"}
PENDING = ("new", "committed_locally")

SQLITE_CODEOWNERS = """
CREATE TABLE codeowners__new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
    content TEXT NOT NULL DEFAULT '',
    file_path VARCHAR(64) NOT NULL DEFAULT '.github/CODEOWNERS',
    git_hash VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'new',
    last_modified_by VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_codeowners_project UNIQUE (project_id)
)
"""

ROW_COLUMNS = "id, project_id, content, file_path, git_hash, status, last_modified_by, created_at, updated_at"


def _git_blob_sha(content: str) -> str:
    data = (content or "").encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _table_exists(conn, sqlite: bool, table: str) -> bool:
    if sqlite:
        query = "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    else:
        query = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"
    return conn.execute(text(query), {"t": table}).fetchone() is not None


def _has_column(conn, sqlite: bool, table: str, column: str) -> bool:
    if sqlite:
        return any(row[1] == column for row in conn.execute(text(f"PRAGMA table_info({table})")))
    return conn.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
        {"t": table, "c": column},
    ).fetchone() is not None


def _kept_state(rows: list, kept: dict, project_repo_ids: set) -> tuple:
    """(status, git_hash) of the one row a project keeps."""
    same_file = all((r["content"], r["file_path"]) == (kept["content"], kept["file_path"]) for r in rows)
    covers_project = project_repo_ids <= {r["repo_id"] for r in rows}
    statuses = {r["status"] for r in rows}
    if same_file and covers_project and statuses <= DELIVERED:
        status = "under_review" if "under_review" in statuses else "synced_with_github"
        return status, _git_blob_sha(kept["content"])
    return (kept["status"] if kept["status"] in PENDING else "committed_locally"), LOCAL_COMMIT_HASH


def _kept_rows(conn) -> list:
    """Each project's most recently saved row, with the state it keeps."""
    rows = [dict(r) for r in conn.execute(text(f"SELECT repo_id, {ROW_COLUMNS} FROM codeowners")).mappings()]
    repos_by_project: dict = {}
    for project_id, repo_id in conn.execute(text("SELECT project_id, repo_id FROM project_repos")):
        repos_by_project.setdefault(project_id, set()).add(repo_id)

    by_project: dict = {}
    for row in rows:
        by_project.setdefault(row["project_id"], []).append(row)

    kept_rows = []
    for project_id, project_rows in by_project.items():
        kept = max(project_rows, key=lambda r: (r["updated_at"] is not None, r["updated_at"] or "", r["id"]))
        kept["status"], kept["git_hash"] = _kept_state(project_rows, kept, repos_by_project.get(project_id, set()))
        kept_rows.append(kept)
    return kept_rows


def _make_codeowners_per_project_sqlite(conn, kept_rows: list) -> None:
    conn.execute(text("DROP TABLE IF EXISTS codeowners__new"))
    conn.execute(text(SQLITE_CODEOWNERS))
    for row in kept_rows:
        conn.execute(
            text(f"INSERT INTO codeowners__new ({ROW_COLUMNS}) VALUES "
                 "(:id, :project_id, :content, :file_path, :git_hash, :status, :last_modified_by, "
                 ":created_at, :updated_at)"),
            {key: row[key] for key in ROW_COLUMNS.split(", ")},
        )
    conn.execute(text("DROP TABLE codeowners"))
    conn.execute(text("ALTER TABLE codeowners__new RENAME TO codeowners"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_codeowners_project_id ON codeowners (project_id)"))


def _make_codeowners_per_project_postgres(conn, kept_rows: list) -> None:
    kept_ids = [row["id"] for row in kept_rows]
    if kept_ids:
        conn.execute(text("DELETE FROM codeowners WHERE NOT (id = ANY(:ids))"), {"ids": kept_ids})
    for row in kept_rows:
        conn.execute(
            text("UPDATE codeowners SET status = :status, git_hash = :git_hash WHERE id = :id"),
            {"status": row["status"], "git_hash": row["git_hash"], "id": row["id"]},
        )
    # Takes its foreign key, index and the (project_id, repo_id) constraint with it.
    conn.execute(text("ALTER TABLE codeowners DROP COLUMN repo_id"))
    conn.execute(text("ALTER TABLE codeowners ADD CONSTRAINT uq_codeowners_project UNIQUE (project_id)"))


def _make_codeowners_per_project(conn, sqlite: bool) -> None:
    if not _table_exists(conn, sqlite, "codeowners") or not _has_column(conn, sqlite, "codeowners", "repo_id"):
        print("✅ codeowners is already one row per project")
        return
    kept_rows = _kept_rows(conn)
    if sqlite:
        _make_codeowners_per_project_sqlite(conn, kept_rows)
    else:
        _make_codeowners_per_project_postgres(conn, kept_rows)
    print(f"✅ codeowners is one row per project ({len(kept_rows)} kept)")


def _drop_branch_baselines(conn, sqlite: bool) -> None:
    conn.execute(text("DROP TABLE IF EXISTS codeowners_branch_states"))
    if not _table_exists(conn, sqlite, "project_pull_requests"):
        return
    if not _has_column(conn, sqlite, "project_pull_requests", "codeowners_sha"):
        return
    if sqlite and sqlite3.sqlite_version_info < (3, 35, 0):
        print("⏭️ SQLite is too old to drop project_pull_requests.codeowners_sha; nothing reads it")
        return
    conn.execute(text("ALTER TABLE project_pull_requests DROP COLUMN codeowners_sha"))
    print("✅ Dropped project_pull_requests.codeowners_sha")


def run_migration():
    """Make CODEOWNERS one file per project."""
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    print("🔄 Making CODEOWNERS one file per project...")
    sqlite = "sqlite" in database_url
    engine = create_engine(database_url)
    with engine.begin() as conn:
        # First: its rows reference the codeowners table rebuilt below.
        _drop_branch_baselines(conn, sqlite)
        _make_codeowners_per_project(conn, sqlite)


if __name__ == "__main__":
    run_migration()
