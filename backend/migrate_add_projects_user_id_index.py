"""
Migration: index projects by owner (issue #2346).

Neither SQLite nor PostgreSQL indexes a foreign key column on its own, so
every lookup of an owner's projects read the whole table: project lists and
name checks, and the drift sweep on every tick (due projects of checkable
owners, pauses for owners without a credential).

The index is (user_id, next_drift_check_at). Lookups by owner use its leading
column. The sweep's due reads use both columns: an owner's due projects come
back in next-check order. On user_id alone, SQLite (which keeps no planner
statistics) would read and sort all of a large owner's projects every tick,
where it now reads only the due ones.

Created only when missing, so an ordinary boot takes no lock on projects:
restore runs the migrations while the app is serving. IF NOT EXISTS covers two
runners racing past the check.
"""

from sqlalchemy import create_engine, text

from migration_utils import get_migration_database_url

TABLE = "projects"
INDEX = "ix_projects_user_id_next_drift_check_at"
COLUMNS = ("user_id", "next_drift_check_at")


def _has_table(conn, is_sqlite: bool) -> bool:
    if is_sqlite:
        query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :t"
    else:
        query = ("SELECT 1 FROM information_schema.tables "
                 "WHERE table_schema = current_schema() AND table_name = :t")
    return conn.execute(text(query), {"t": TABLE}).first() is not None


def _has_index(conn, is_sqlite: bool) -> bool:
    if is_sqlite:
        return INDEX in {row[1] for row in conn.execute(text(f"PRAGMA index_list({TABLE})"))}
    query = "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :t AND indexname = :i"
    return conn.execute(text(query), {"t": TABLE, "i": INDEX}).first() is not None


def run_migration():
    """Create the owner index if it does not exist yet."""
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    engine = create_engine(database_url)
    is_sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        if not _has_table(conn, is_sqlite):
            print(f"⏭️ {TABLE} table does not exist yet, skipping migration")
            return
        if _has_index(conn, is_sqlite):
            print(f"✅ {INDEX} present")
            return
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} ({', '.join(COLUMNS)})"))
    print(f"✅ {INDEX} created")


if __name__ == "__main__":
    run_migration()
