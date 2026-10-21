"""
Migration: add projects.file_kind, the file a Managed Files project manages (#2367).

One nullable column. It is set only on projects of type ``managed_files``, so
every existing row stays NULL and nothing is backfilled.

Uses get_migration_database_url() rather than the app engine because ALTER
TABLE needs the table owner, which on Kubernetes is not the application user.
"""

from sqlalchemy import create_engine, inspect, text

from migration_utils import get_migration_database_url

TABLE = "projects"
COLUMN = "file_kind"


def run_migration():
    """Add projects.file_kind when it is missing."""
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    engine = create_engine(database_url)
    with engine.begin() as conn:
        inspector = inspect(conn)
        if TABLE not in inspector.get_table_names():
            print(f"⏭️ {TABLE} table does not exist yet, skipping migration")
            return
        if COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}:
            print(f"⏭️ {TABLE}.{COLUMN} already exists")
            return
        conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} VARCHAR(32)"))

    print(f"✅ {TABLE}.{COLUMN} added")


if __name__ == "__main__":
    run_migration()
