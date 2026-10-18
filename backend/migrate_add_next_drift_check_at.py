"""
Migration: store each project's next drift check time (issue #2343).

The sweep used to derive due-ness on every tick from the last check, the
project's interval and its failure streak, as an OR-tree no index could serve.
It now compares projects.next_drift_check_at with the clock and finds the
owners of failing projects through an index on drift_check_failure_count.
projects.drift_check_scheduled_minutes records the interval each time was
computed with.

After adding the columns and indexes, every row is brought to what its inputs
derive. Only rows that differ are written, so this converges rather than
filling once: a time left stale by older code running in between (a rollback,
then upgrading again) or by a restored backup is corrected, and a run over an
up-to-date table writes nothing. Indexes are created only when missing, so an
ordinary boot takes no lock on projects for them.

Self-contained on purpose: this runs on every boot for as long as it is in
run_migrations, so it must not change meaning when drift_worker does. The
schedule formula is a frozen copy; test_migrate_add_next_drift_check_at fails
if the app's own (drift_worker.next_check_at) ever stops agreeing with it,
which is the cue for a new migration to re-converge on the new formula.
"""

from datetime import timedelta

from sqlalchemy import (Column, DateTime, Integer, MetaData, Table, and_, bindparam, create_engine,
                        select, text, update)

from migration_utils import get_migration_database_url

TABLE = "projects"
COLUMNS = {
    "next_drift_check_at": {"sqlite": "DATETIME", "postgresql": "TIMESTAMP WITHOUT TIME ZONE"},
    "drift_check_scheduled_minutes": {"sqlite": "INTEGER", "postgresql": "INTEGER"},
}
INDEXES = {
    "ix_projects_next_drift_check_at": "next_drift_check_at",
    "ix_projects_drift_check_failure_count": "drift_check_failure_count",
    "ix_projects_drift_check_scheduled_minutes": "drift_check_scheduled_minutes",
    "ix_projects_drift_inherited_schedule": "drift_check_interval_minutes, drift_check_scheduled_minutes",
}
# Rows per transaction: restore runs this while the app is serving.
CHUNK = 500

# Frozen as of #2343; see the module docstring.
DEFAULT_RECHECK_INTERVAL_MINUTES = 15
BACKOFF_MAX_MULTIPLIER = 32

_projects = Table(
    TABLE, MetaData(),
    Column("project_id", Integer, primary_key=True),
    Column("last_drift_check_at", DateTime),
    Column("drift_check_failure_count", Integer),
    Column("drift_check_interval_minutes", Integer),
    Column("next_drift_check_at", DateTime),
    Column("drift_check_scheduled_minutes", Integer),
)


def next_check_at(last_checked_at, interval_minutes, failure_count, default_minutes):
    if last_checked_at is None:
        return None
    minutes = default_minutes if interval_minutes is None else interval_minutes
    multiplier = min(2 ** max((failure_count or 0) - 1, 0), BACKOFF_MAX_MULTIPLIER)
    return last_checked_at + timedelta(minutes=minutes * multiplier)


def _table_names(conn, is_sqlite: bool) -> set:
    if is_sqlite:
        return {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    rows = conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"))
    return {r[0] for r in rows}


def _existing_columns(conn, is_sqlite: bool) -> set:
    if is_sqlite:
        return {r[1] for r in conn.execute(text(f"PRAGMA table_info({TABLE})"))}
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns "
             "WHERE table_schema = current_schema() AND table_name = :t"),
        {"t": TABLE},
    )
    return {r[0] for r in rows}


def _existing_indexes(conn, is_sqlite: bool) -> set:
    if is_sqlite:
        return {r[1] for r in conn.execute(text(f"PRAGMA index_list({TABLE})"))}
    rows = conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :t"),
        {"t": TABLE},
    )
    return {r[0] for r in rows}


def _default_minutes(conn, tables: set) -> int:
    if "drift_settings" not in tables:
        return DEFAULT_RECHECK_INTERVAL_MINUTES
    saved = conn.execute(text(
        "SELECT recheck_interval_minutes FROM drift_settings ORDER BY settings_id LIMIT 1")).scalar()
    return saved or DEFAULT_RECHECK_INTERVAL_MINUTES


def _converge_chunk(conn, default_minutes: int, after_id: int):
    """Rewrite the rows of one chunk that disagree; returns (written, last id read or None)."""
    c = _projects.c
    rows = conn.execute(select(_projects).where(c.project_id > after_id)
                        .order_by(c.project_id).limit(CHUNK)).all()
    moved = []
    for row in rows:
        scheduled = next_check_at(row.last_drift_check_at, row.drift_check_interval_minutes,
                                  row.drift_check_failure_count, default_minutes)
        interval = default_minutes if row.drift_check_interval_minutes is None else row.drift_check_interval_minutes
        if (scheduled, interval) != (row.next_drift_check_at, row.drift_check_scheduled_minutes):
            moved.append({"b_id": row.project_id, "b_was_next": row.next_drift_check_at,
                          "b_was_marker": row.drift_check_scheduled_minutes,
                          "b_streak": row.drift_check_failure_count,
                          "b_interval": row.drift_check_interval_minutes,
                          "b_next": scheduled, "b_marker": interval})
    written = 0
    if moved:
        # Only while the row is as it was read: the app may be writing it too.
        written = conn.execute(
            update(_projects).where(and_(
                c.project_id == bindparam("b_id"),
                c.next_drift_check_at.is_not_distinct_from(bindparam("b_was_next")),
                c.drift_check_scheduled_minutes.is_not_distinct_from(bindparam("b_was_marker")),
                c.drift_check_failure_count == bindparam("b_streak"),
                c.drift_check_interval_minutes.is_not_distinct_from(bindparam("b_interval")),
            )).values(next_drift_check_at=bindparam("b_next"),
                      drift_check_scheduled_minutes=bindparam("b_marker")),
            moved,
        ).rowcount
    return written, (rows[-1].project_id if len(rows) == CHUNK else None)


def run_migration():
    """Add and index the drift schedule columns, and bring every stored time up to date."""
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    print("🔄 Adding the stored drift schedule...")
    is_sqlite = "sqlite" in database_url
    dialect = "sqlite" if is_sqlite else "postgresql"
    engine = create_engine(database_url)
    with engine.begin() as conn:
        tables = _table_names(conn, is_sqlite)
        if TABLE not in tables:
            print(f"⏭️ {TABLE} table does not exist yet, skipping migration")
            return
        present = _existing_columns(conn, is_sqlite)
        for column, ddl in COLUMNS.items():
            if column not in present:
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {column} {ddl[dialect]}"))
                print(f"   ➕ {column}")
        indexed = _existing_indexes(conn, is_sqlite)
        for index, column in INDEXES.items():
            if index not in indexed:
                conn.execute(text(f"CREATE INDEX {index} ON {TABLE} ({column})"))
                print(f"   ➕ {index}")
        default_minutes = _default_minutes(conn, tables)

    rewritten, after_id = 0, 0
    while after_id is not None:
        with engine.begin() as conn:
            written, after_id = _converge_chunk(conn, default_minutes, after_id)
        rewritten += written

    print(f"✅ drift schedule columns present ({rewritten} schedules brought up to date)")


if __name__ == "__main__":
    run_migration()
