"""
Migration: pin where each delivery on the record is.

A rename or a move removes the copy a file left behind, and a drift check reads
the file, at the path ``file_deliveries`` says it was delivered to on each
(repo, branch) (``deliveries.path_on``). The backfill in
migrate_add_file_deliveries.py recorded no paths, and a merge of a PR it
backfilled delivered none, so those rows read as delivered nowhere in
particular: a later rename would leave their old copy behind.

Fills, data only:

1. ``delivered_path`` of every delivered row without one. A workflow's is the
   file its owning project (``file_deliveries.project_id``) named it on
   GitHub: ``renamed_from`` while a rename is still undelivered, else its
   name, as ``workflows.format_workflow_name`` formats it with that project's
   code and prefix setting. A custom file's or the CODEOWNERS' is its path.
2. ``pending_path`` of every row an open PR carries a change to, other than a
   removal, without one. The current name: a rename is refused while an open
   PR carries the workflow, and the backfill matched an open PR's files to
   workflows by their current name, so that is the name the PR carries.

Then drops ``workflows.renamed_from``, which nothing else reads: the record
says where a renamed file was delivered. SQLite before 3.35 cannot drop a
column, so there it stays, unread.

Only NULLs are filled, so it is idempotent. Supports both SQLite and PostgreSQL.
"""

import sqlite3

from sqlalchemy import create_engine, text

from migration_utils import get_migration_database_url

TABLE = "file_deliveries"


def _formatted(name: str) -> str:
    """``format_workflow_name`` of the SQL expression *name*, under ``.github/workflows/``.

    The suffix check is case-sensitive like ``str.endswith``, so it compares
    SUBSTRs: SQLite's LIKE ignores case. LENGTH - 3 and LENGTH - 4 stay at
    1 or more for any name long enough to end in the suffix, where SQLite's
    and PostgreSQL's SUBSTR agree.
    """
    return f"""'.github/workflows/' || CASE
        WHEN p.use_prefix THEN 'AM_' || p.project_code || '_' || {name} || '.yml'
        WHEN SUBSTR({name}, LENGTH({name}) - 3) = '.yml' OR SUBSTR({name}, LENGTH({name}) - 4) = '.yaml'
            THEN {name}
        ELSE {name} || '.yml'
    END"""


def _workflow_path(name: str) -> str:
    return f"""
        SELECT {_formatted(name)} FROM workflows w, projects p
        WHERE w.workflow_id = {TABLE}.workflow_id AND p.project_id = {TABLE}.project_id
    """


RENAMED_FROM = "renamed_from"


def _files(renamed_from: bool) -> tuple:
    """(file column, path when delivered, path an open PR carries) for each kind of file."""
    delivered_name = f"COALESCE(w.{RENAMED_FROM}, w.workflow_name)" if renamed_from else "w.workflow_name"
    return (
        ("workflow_id", _workflow_path(delivered_name), _workflow_path("w.workflow_name")),
        ("custom_file_id", f"SELECT f.file_path FROM custom_files f WHERE f.id = {TABLE}.custom_file_id", None),
        ("codeowners_id", f"SELECT c.file_path FROM codeowners c WHERE c.id = {TABLE}.codeowners_id", None),
    )

_DELIVERED = "delivered_at IS NOT NULL AND delivered_path IS NULL"
_PENDING = ("pending_path IS NULL AND pending_delete = {false} AND pending_pr_id IN "
            "(SELECT pr_id FROM project_pull_requests WHERE pr_state = 'open')")


def _table_exists(conn, sqlite: bool, table: str) -> bool:
    if sqlite:
        query = "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    else:
        query = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"
    return conn.execute(text(query), {"t": table}).fetchone() is not None


def _has_renamed_from(conn, sqlite: bool) -> bool:
    if sqlite:
        return any(row[1] == RENAMED_FROM for row in conn.execute(text("PRAGMA table_info(workflows)")))
    query = ("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
             "AND table_name = 'workflows' AND column_name = :c")
    return conn.execute(text(query), {"c": RENAMED_FROM}).fetchone() is not None


def _fill(conn, column: str, value: str, file_column: str, where: str) -> int:
    statement = f"UPDATE {TABLE} SET {column} = ({value}) WHERE {file_column} IS NOT NULL AND {where}"
    return conn.execute(text(statement)).rowcount or 0


def pin(conn, sqlite: bool) -> tuple:
    """Fill the missing paths; returns how many (delivered, pending) rows it filled."""
    pending_where = _PENDING.format(false="0" if sqlite else "FALSE")
    delivered = pending = 0
    for file_column, delivered_value, pending_value in _files(_has_renamed_from(conn, sqlite)):
        delivered += _fill(conn, "delivered_path", delivered_value, file_column, _DELIVERED)
        pending += _fill(conn, "pending_path", pending_value or delivered_value, file_column, pending_where)
    return delivered, pending


def run_migration():
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    print(f"🔄 Pinning delivered paths on {TABLE}...")
    sqlite = "sqlite" in database_url
    engine = create_engine(database_url)
    with engine.begin() as conn:
        if not _table_exists(conn, sqlite, TABLE):
            print(f"⏭️ {TABLE} does not exist yet, skipping migration")
            return
        delivered, pending = pin(conn, sqlite)
        _drop_renamed_from(conn, sqlite)
    print(f"✅ Pinned {delivered} delivered and {pending} pending paths")


def _drop_renamed_from(conn, sqlite: bool) -> None:
    if not _has_renamed_from(conn, sqlite):
        return
    if sqlite and sqlite3.sqlite_version_info < (3, 35, 0):
        print(f"⏭️ SQLite is too old to drop workflows.{RENAMED_FROM}; nothing reads it")
        return
    conn.execute(text(f"ALTER TABLE workflows DROP COLUMN {RENAMED_FROM}"))
    print(f"🗑️ Dropped workflows.{RENAMED_FROM}")


if __name__ == "__main__":
    run_migration()
