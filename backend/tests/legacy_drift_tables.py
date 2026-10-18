"""
The drift state tables an existing database still has before
migrate_move_drift_state_to_file_deliveries moves them onto file_deliveries
and drops them. The app no longer maps them, so the migration tests that start
from such a database create and fill them through these.

Columns and defaults as the models last declared them. No foreign keys or
unique constraints: the parents are not on this metadata, and no test here
relies on either.
"""
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, func

metadata = MetaData()


def _drift_columns():
    return (
        Column("branch", String(255), nullable=False, default="", server_default=""),
        Column("has_drift", Boolean, nullable=False, default=False),
        Column("content_hash", String(64)),
        Column("github_sha", String(255)),
        Column("deleted_in_github", Boolean, nullable=False, default=False, server_default="0"),
        Column("confirmed_present_at", DateTime),
        Column("drift_cycle_count", Integer, nullable=False, default=0),
        Column("last_checked_at", DateTime, default=func.now()),
    )


workflow_drift_states = Table(
    "workflow_drift_states", metadata,
    Column("state_id", Integer, primary_key=True),
    Column("project_id", Integer, nullable=False),
    Column("workflow_id", Integer, nullable=False),
    Column("repo_id", Integer, nullable=False),
    *_drift_columns(),
)

codeowners_drift_states = Table(
    "codeowners_drift_states", metadata,
    Column("state_id", Integer, primary_key=True),
    Column("project_id", Integer, nullable=False),
    Column("repo_id", Integer, nullable=False),
    Column("etag", String(255)),
    *_drift_columns(),
)


def create(engine) -> None:
    metadata.create_all(bind=engine)
