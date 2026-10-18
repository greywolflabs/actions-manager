"""
Migration: keep drift state on the delivery record.

Drift is now read against ``file_deliveries`` (deliveries.py), so the last
check's outcome lives on the same row: whether it drifted, whether that was a
deletion, the blob it saw, the conditional-read ETag (CODEOWNERS), the
notification dedup hash and cycle count, and when it was checked.

1. Adds those columns to ``file_deliveries`` where missing.
2. Copies ``workflow_drift_states`` and ``codeowners_drift_states`` onto it,
   once: only while no ``file_deliveries`` row has ``last_checked_at`` yet.
   ``run_migrations.py`` creates every ORM table before any migration runs, so
   "the columns were just added" cannot be the trigger; once a check has
   recorded anything, a copy would overwrite it with older state. A state row
   with no matching delivery row gets one that records no delivery. A state
   whose file has no owner any more is skipped.

3. Drops the old tables, in the same transaction as the copy: a copy that
   fails leaves them in place for the next start. Once a check has recorded
   anything they hold only state older than the record, so they go then too.

``projects`` is not written: the same drifted rows move, so its drift counts
stay right.

Idempotent, and supports both SQLite and PostgreSQL.
"""

from sqlalchemy import create_engine, text

from migration_utils import get_migration_database_url

TABLE = "file_deliveries"

_FALSE_FLAG = ("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")
_SHA = ("VARCHAR(255)", "VARCHAR(255)")

# (SQLite, PostgreSQL)
COLUMNS = {
    "has_drift": _FALSE_FLAG,
    "deleted_in_github": _FALSE_FLAG,
    "github_sha": _SHA,
    "etag": _SHA,
    "content_hash": ("VARCHAR(64)", "VARCHAR(64)"),
    "drift_cycle_count": ("INTEGER NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0"),
    "last_checked_at": ("TIMESTAMP", "TIMESTAMP"),
}

_DRIFT = ("s.has_drift, s.deleted_in_github, s.github_sha, s.content_hash, s.drift_cycle_count, "
          "s.last_checked_at")

# Each old state with its file and that file's owner. A workflow's owner is
# the project whose ProjectWorkflow holds it, as deliveries._OWNER says.
_STATES = (
    ("workflow_drift_states", "workflow_id", f"""
        SELECT s.workflow_id AS file_id, pw.project_id, s.repo_id, s.branch, NULL AS etag, {_DRIFT}
        FROM workflow_drift_states s
        JOIN project_workflows pw ON pw.workflow_id = s.workflow_id
    """),
    ("codeowners_drift_states", "codeowners_id", f"""
        SELECT c.id AS file_id, c.project_id, s.repo_id, s.branch, s.etag, {_DRIFT}
        FROM codeowners_drift_states s
        JOIN codeowners c ON c.project_id = s.project_id
    """),
)

# Never a NULL last_checked_at: a copied row without one would leave the copy's
# gate open, and a later start would copy this old state over newer checks.
_SET_DRIFT = f"""
UPDATE {TABLE} SET has_drift = :has_drift, deleted_in_github = :deleted_in_github,
       github_sha = :github_sha, etag = :etag, content_hash = :content_hash,
       drift_cycle_count = :drift_cycle_count,
       last_checked_at = COALESCE(:last_checked_at, CURRENT_TIMESTAMP)
WHERE id = :id
"""


def _table_exists(conn, sqlite: bool, table: str) -> bool:
    if sqlite:
        query = "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    else:
        query = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"
    return conn.execute(text(query), {"t": table}).fetchone() is not None


def _existing_columns(conn, sqlite: bool) -> set:
    if sqlite:
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info({TABLE})"))}
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"),
        {"t": TABLE},
    )
    return {row[0] for row in rows}


def _add_columns(conn, sqlite: bool) -> int:
    present = _existing_columns(conn, sqlite)
    added = 0
    for name, (sqlite_ddl, postgres_ddl) in COLUMNS.items():
        if name not in present:
            conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {sqlite_ddl if sqlite else postgres_ddl}"))
            added += 1
            print(f"   ➕ {name}")
    return added


def _delivery_row_id(conn, column: str, state, false: str) -> int:
    """The file_deliveries row for the state's (file, repo, branch), inserted with no delivery if missing."""
    key = {"file_id": state["file_id"], "repo_id": state["repo_id"], "branch": state["branch"] or ""}
    select = text(f"SELECT id FROM {TABLE} WHERE {column} = :file_id AND repo_id = :repo_id AND branch = :branch")
    row = conn.execute(select, key).fetchone()
    if row is None:
        conn.execute(
            text(f"""
                INSERT INTO {TABLE} (project_id, repo_id, branch, {column}, pending_delete, created_at, updated_at)
                VALUES (:project_id, :repo_id, :branch, :file_id, {false}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """),
            {**key, "project_id": state["project_id"]},
        )
        row = conn.execute(select, key).fetchone()
    return row[0]


def _copy_states(conn, column: str, query: str, false: str) -> int:
    states = conn.execute(text(query)).mappings().all()
    for state in states:
        conn.execute(text(_SET_DRIFT), {
            "id": _delivery_row_id(conn, column, state, false),
            "has_drift": bool(state["has_drift"]), "deleted_in_github": bool(state["deleted_in_github"]),
            "github_sha": state["github_sha"], "etag": state["etag"], "content_hash": state["content_hash"],
            "drift_cycle_count": state["drift_cycle_count"] or 0, "last_checked_at": state["last_checked_at"],
        })
    return len(states)


def _copy(conn, sqlite: bool, tables: list) -> None:
    if conn.execute(text(f"SELECT 1 FROM {TABLE} WHERE last_checked_at IS NOT NULL LIMIT 1")).fetchone():
        print(f"✅ {TABLE} already holds drift checks, nothing to copy")
        return
    false = "0" if sqlite else "FALSE"
    for table, column, query in _STATES:
        if table in tables:
            print(f"   {_copy_states(conn, column, query, false)} {table} row(s) copied")


def _drop(conn, tables: list) -> None:
    for table in tables:
        conn.execute(text(f"DROP TABLE {table}"))
        print(f"   🗑️ dropped {table}")


def run_migration():
    """Add the drift columns to file_deliveries, move the old drift state onto them, and drop its tables."""
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    print(f"🔄 Moving drift state onto {TABLE}...")
    sqlite = "sqlite" in database_url
    engine = create_engine(database_url)
    with engine.begin() as conn:
        if not _table_exists(conn, sqlite, TABLE):
            print(f"⏭️ {TABLE} does not exist yet, skipping migration")
            return
        print(f"   {_add_columns(conn, sqlite)} column(s) added")
        tables = [table for table, _, _ in _STATES if _table_exists(conn, sqlite, table)]
        _copy(conn, sqlite, tables)
        _drop(conn, tables)


if __name__ == "__main__":
    run_migration()
