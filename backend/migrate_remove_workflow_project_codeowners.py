"""
Migration: CODEOWNERS leaves Caller and Reusable Workflow Projects (#2367).

CODEOWNERS is managed only in the Managed Files section now, by one project
per repository. This deletes what workflow projects still hold of it:

1. the delivery and drift rows of every CODEOWNERS record whose project is
   not a CODEOWNERS project in Managed Files,
2. those records,
3. the delivery and drift rows of every custom file at a path GitHub reads
   CODEOWNERS from,
4. those custom files,

then recounts, with deliveries.drift_count, the cached drift summary
(projects.drift_count and drift_status) of each drifted project that lost a
drifted row, so the project list stops reporting drift the project page no
longer shows. Through the ORM rather than one UPDATE: this runs after
migrate_add_next_drift_check_at, so only Project's mapper events would keep
its drift schedule current (test_drift_schedule_writers).

Children first, with explicit deletes, so the result is the same whether or
not the connection enforces foreign keys; all of it in one transaction.
Files on GitHub are not touched, nor is history (notification events, pull
requests' file names). A second run finds nothing to delete. A database
without projects.file_kind yet (its migration failed) is left alone.

The paths, and how a path is put in normal form ("./CODEOWNERS" is
CODEOWNERS), are frozen here: test_migrate_remove_workflow_project_codeowners
fails if file_kinds stops agreeing.
"""

import posixpath

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.orm import Session

import deliveries
from migration_utils import get_migration_database_url
from models import Project

CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

_REQUIRED = (
    ("projects", "file_kind"),
    ("projects", "drift_count"),
    ("codeowners", "id"),
    ("custom_files", "file_path"),
    ("file_deliveries", "codeowners_id"),
    ("file_deliveries", "has_drift"),
)

# The CODEOWNERS records of every project but a Managed Files one of the kind.
_STRAY_CODEOWNERS = """
    SELECT c.id FROM codeowners c
    JOIN projects p ON p.project_id = c.project_id
    WHERE NOT (p.project_type = 'managed_files' AND COALESCE(p.file_kind, '') = 'codeowners')
"""
CODEOWNERS_STEPS = (
    ("delivery rows of workflow projects' CODEOWNERS",
     f"DELETE FROM file_deliveries WHERE codeowners_id IN ({_STRAY_CODEOWNERS})"),
    ("workflow projects' CODEOWNERS", f"DELETE FROM codeowners WHERE id IN ({_STRAY_CODEOWNERS})"),
)
CUSTOM_FILE_STEPS = (
    ("delivery rows of custom files at CODEOWNERS paths", "DELETE FROM file_deliveries WHERE custom_file_id IN :ids"),
    ("custom files at CODEOWNERS paths", "DELETE FROM custom_files WHERE id IN :ids"),
)

# Projects with a drifted row the deletes are about to remove.
_LOSING_DRIFT = f"""
    SELECT DISTINCT project_id FROM file_deliveries
    WHERE has_drift AND (codeowners_id IN ({_STRAY_CODEOWNERS}) OR custom_file_id IN :ids)
"""


def normal_path(path):
    """*path* as a kind's path is compared: no ".", empty segment or backslash."""
    return posixpath.normpath((path or "").strip().replace("\\", "/")).lstrip("/")


def _codeowners_custom_files(conn) -> list:
    rows = conn.execute(text("SELECT id, file_path FROM custom_files"))
    return [file_id for file_id, path in rows if normal_path(path) in CODEOWNERS_PATHS]


def _with_ids(conn, statement: str, ids: list):
    """*statement* with :ids bound to *ids*, an empty list included."""
    return conn.execute(text(statement).bindparams(bindparam("ids", expanding=True)), {"ids": ids})


def _recount_drift(conn, project_ids: list) -> int:
    """Recount the summary of each of *project_ids* that says drifted; one whose check failed waits for the next."""
    with Session(bind=conn) as db:
        drifted = db.query(Project).filter(Project.project_id.in_(project_ids), Project.drift_status == "drifted").all()
        for project in drifted:
            project.drift_count = deliveries.drift_count(db, project.project_id)
            project.drift_status = "drifted" if project.drift_count else "clean"
        db.flush()
        return len(drifted)


def _schema_ready(conn) -> bool:
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    for table, column in _REQUIRED:
        if table not in tables or column not in {c["name"] for c in inspector.get_columns(table)}:
            print(f"⏭️ {table}.{column} does not exist yet, skipping migration")
            return False
    return True


def run_migration():
    """Delete what Caller and Reusable Workflow Projects hold of CODEOWNERS."""
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    engine = create_engine(database_url)
    with engine.begin() as conn:
        if not _schema_ready(conn):
            return
        ids = _codeowners_custom_files(conn)
        losing_drift = [project_id for (project_id,) in _with_ids(conn, _LOSING_DRIFT, ids)]
        for label, statement in CODEOWNERS_STEPS:
            print(f"   🗑️ {label}: {conn.execute(text(statement)).rowcount}")
        for label, statement in CUSTOM_FILE_STEPS:
            print(f"   🗑️ {label}: {_with_ids(conn, statement, ids).rowcount}")
        print(f"   🔄 drift summaries recounted: {_recount_drift(conn, losing_drift)}")

    print("✅ CODEOWNERS is held only by Managed Files projects")


if __name__ == "__main__":
    run_migration()
