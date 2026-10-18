"""
Migration: one delivery record per (file, repo, branch).

``file_deliveries`` records what ActionsManager delivered to each (file, repo,
branch), and what an open pull request carries there (see deliveries.py). It
replaces guesses made from the per-file hash, the file's status, the drift
state's ``confirmed_present_at`` and the name lists on project_pull_requests.

Backfilled once, from what those already know:

1. Every ``workflow_drift_states`` row a check confirmed present becomes a
   delivered row, delivered when it was first seen. Its SHA is kept only when
   that check found no drift: a drifted blob is not one ActionsManager
   delivered.
2. ``codeowners_drift_states`` the same way, for the project's CODEOWNERS.
   Both only where the table is still there: a later migration drops them.
3. Every open pull request becomes pending rows for the files it names: its
   ``workflow_names`` resolved by exact name to workflows of its project, or,
   on a PR to a repo that is not one of the project's own, to those linked
   into it; its ``file_names`` to the project's custom files and CODEOWNERS.
   Anything that resolves to nothing is skipped.

Idempotent, and supports both SQLite and PostgreSQL.
"""

from sqlalchemy import create_engine, text

from migration_utils import get_migration_database_url

TABLE = "file_deliveries"

_COLUMNS = """
    project_id INTEGER NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
    repo_id INTEGER NOT NULL REFERENCES repos (repo_id) ON DELETE CASCADE,
    branch VARCHAR(255) NOT NULL DEFAULT '',
    workflow_id INTEGER REFERENCES workflows (workflow_id) ON DELETE CASCADE,
    custom_file_id INTEGER REFERENCES custom_files (id) ON DELETE CASCADE,
    codeowners_id INTEGER REFERENCES codeowners (id) ON DELETE CASCADE,
    delivered_sha VARCHAR(255),
    delivered_path VARCHAR(500),
    delivered_at TIMESTAMP,
    delivered_pr_id INTEGER REFERENCES project_pull_requests (pr_id) ON DELETE SET NULL,
    pending_sha VARCHAR(255),
    pending_path VARCHAR(500),
    pending_delete BOOLEAN NOT NULL DEFAULT {false},
    pending_pr_id INTEGER REFERENCES project_pull_requests (pr_id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_file_delivery_workflow_repo_branch UNIQUE (workflow_id, repo_id, branch),
    CONSTRAINT uq_file_delivery_custom_file_repo_branch UNIQUE (custom_file_id, repo_id, branch),
    CONSTRAINT uq_file_delivery_codeowners_repo_branch UNIQUE (codeowners_id, repo_id, branch),
    CONSTRAINT ck_file_delivery_one_file CHECK (
        (CASE WHEN workflow_id IS NULL THEN 0 ELSE 1 END)
        + (CASE WHEN custom_file_id IS NULL THEN 0 ELSE 1 END)
        + (CASE WHEN codeowners_id IS NULL THEN 0 ELSE 1 END) = 1
    )
"""

INDEXED = ("project_id", "repo_id", "pending_pr_id", "delivered_pr_id")

_PARENT_TABLES = (
    "projects", "repos", "workflows", "project_workflows", "custom_files", "codeowners",
    "project_pull_requests", "linked_reusable_workflows",
)

# The SHA a drift check saw, unless that check found drift.
_CHECKED_SHA = "CASE WHEN s.has_drift THEN NULL ELSE s.github_sha END"

_DELIVERED_WORKFLOWS = f"""
INSERT INTO {TABLE} (project_id, repo_id, branch, workflow_id, delivered_sha, delivered_at,
                     pending_delete, created_at, updated_at)
SELECT pw.project_id, s.repo_id, s.branch, s.workflow_id, {_CHECKED_SHA}, s.confirmed_present_at,
       {{false}}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
FROM workflow_drift_states s
JOIN project_workflows pw ON pw.workflow_id = s.workflow_id
WHERE s.confirmed_present_at IS NOT NULL
"""

_DELIVERED_CODEOWNERS = f"""
INSERT INTO {TABLE} (project_id, repo_id, branch, codeowners_id, delivered_sha, delivered_at,
                     pending_delete, created_at, updated_at)
SELECT c.project_id, s.repo_id, s.branch, c.id, {_CHECKED_SHA}, s.confirmed_present_at,
       {{false}}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
FROM codeowners_drift_states s
JOIN codeowners c ON c.project_id = s.project_id
WHERE s.confirmed_present_at IS NOT NULL
"""

# Workflows a PR of this project can carry: its own, and, on a PR to a repo
# that is not one of its own (the linked RWX project's), those linked into it.
_OWN_WORKFLOWS = """
SELECT w.workflow_id, w.workflow_name, pw.project_id
FROM workflows w
JOIN project_workflows pw ON pw.workflow_id = w.workflow_id
WHERE pw.project_id = :project_id
"""
_LINKED_WORKFLOWS = """
SELECT w.workflow_id, w.workflow_name, pw.project_id
FROM workflows w
JOIN project_workflows pw ON pw.workflow_id = w.workflow_id
JOIN linked_reusable_workflows l ON l.workflow_id = w.workflow_id
WHERE l.standard_project_id = :project_id
"""
_OWN_REPO = """
SELECT 1 FROM project_repos p JOIN repos r ON r.repo_id = p.repo_id
WHERE p.project_id = :project_id AND r.repo_name = :repo_name
"""


def _table_exists(conn, sqlite: bool, table: str) -> bool:
    if sqlite:
        query = "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
    else:
        query = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"
    return conn.execute(text(query), {"t": table}).fetchone() is not None


def _create_table(conn, sqlite: bool) -> None:
    key = "id INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "id SERIAL PRIMARY KEY"
    columns = _COLUMNS.format(false="0" if sqlite else "FALSE")
    conn.execute(text(f"CREATE TABLE IF NOT EXISTS {TABLE} ({key},{columns})"))
    for column in INDEXED:
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_{column} ON {TABLE} ({column})"))


def _names(value) -> list:
    return [name.strip() for name in (value or "").split(",") if name.strip()]


def _repo_id(conn, repo_name: str):
    """The repo row for this name, created if a PR names a repo that has none."""
    select = text("SELECT repo_id FROM repos WHERE repo_name = :name")
    row = conn.execute(select, {"name": repo_name}).fetchone()
    if row is None:
        conn.execute(text("INSERT INTO repos (repo_name) VALUES (:name)"), {"name": repo_name})
        row = conn.execute(select, {"name": repo_name}).fetchone()
    return row[0]


def _files_at_path(conn, project_id: int, path: str) -> list:
    """(column, id, project) for the project's custom file or CODEOWNERS at *path*."""
    files = []
    for column, table in (("custom_file_id", "custom_files"), ("codeowners_id", "codeowners")):
        files += [
            (column, file_id, project_id) for (file_id,) in conn.execute(
                text(f"SELECT id FROM {table} WHERE project_id = :project_id AND file_path = :path"),
                {"project_id": project_id, "path": path},
            )
        ]
    return files


def _codeowners_file(conn, project_id: int) -> list:
    return [
        ("codeowners_id", file_id, project_id) for (file_id,) in conn.execute(
            text("SELECT id FROM codeowners WHERE project_id = :project_id"), {"project_id": project_id},
        )
    ]


def _pr_workflows(conn, pr, names: set) -> list:
    """(column, workflow id, owning project) for the workflows an open PR names.

    A linked workflow is delivered to its RWX project's repository, never to
    the caller's own, so a name on a PR to one of the caller's repos is the
    caller's workflow even when a linked one shares it.
    """
    params = {"project_id": pr["project_id"], "repo_name": pr["repo_name"]}
    queries = [_OWN_WORKFLOWS]
    if conn.execute(text(_OWN_REPO), params).fetchone() is None:
        queries.append(_LINKED_WORKFLOWS)
    return [
        ("workflow_id", workflow_id, owner)
        for query in queries
        for workflow_id, name, owner in conn.execute(text(query), params)
        if name in names
    ]


def _pr_files(conn, pr) -> list:
    """(column, file id, owning project) for every file an open PR names."""
    names = set(_names(pr["workflow_names"]))
    files = _pr_workflows(conn, pr, names)
    for path in _names(pr["file_names"]):
        files += _files_at_path(conn, pr["project_id"], path)
    if names == {"CODEOWNERS"}:
        # A standalone CODEOWNERS PR from before CODEOWNERS rode campaigns
        # names the file in workflow_names instead.
        files += _codeowners_file(conn, pr["project_id"])
    return files


def _mark_pending(conn, pr, repo_id: int, column: str, file_id: int, owner: int, false: str) -> None:
    key = {"file_id": file_id, "repo_id": repo_id, "branch": pr["target_branch"]}
    existing = conn.execute(
        text(f"SELECT id FROM {TABLE} WHERE {column} = :file_id AND repo_id = :repo_id AND branch = :branch"),
        key,
    ).fetchone()
    if existing:
        conn.execute(text(f"UPDATE {TABLE} SET pending_pr_id = :pr_id WHERE id = :id"),
                     {"pr_id": pr["pr_id"], "id": existing[0]})
        return
    conn.execute(
        text(f"""
            INSERT INTO {TABLE} (project_id, repo_id, branch, {column}, pending_delete, pending_pr_id,
                                 created_at, updated_at)
            VALUES (:owner, :repo_id, :branch, :file_id, {false}, :pr_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """),
        {**key, "owner": owner, "pr_id": pr["pr_id"]},
    )


def _backfill_open_prs(conn, false: str) -> int:
    prs = conn.execute(text(
        "SELECT pr_id, project_id, repo_name, target_branch, workflow_names, file_names "
        "FROM project_pull_requests WHERE pr_state = 'open' ORDER BY pr_id"
    )).mappings().all()
    marked = 0
    for pr in prs:
        files = _pr_files(conn, pr)
        if not files:
            continue
        repo_id = _repo_id(conn, pr["repo_name"])
        for column, file_id, owner in files:
            _mark_pending(conn, pr, repo_id, column, file_id, owner, false)
            marked += 1
    return marked


def _from_drift_state(conn, sqlite: bool, table: str, insert: str, false: str) -> int:
    """Deliveries *table*'s checks confirmed; none once migrate_move_drift_state_to_file_deliveries dropped it."""
    if not _table_exists(conn, sqlite, table):
        return 0
    return conn.execute(text(insert.format(false=false))).rowcount or 0


def _backfill(conn, sqlite: bool) -> None:
    false = "0" if sqlite else "FALSE"
    workflows = _from_drift_state(conn, sqlite, "workflow_drift_states", _DELIVERED_WORKFLOWS, false)
    codeowners = _from_drift_state(conn, sqlite, "codeowners_drift_states", _DELIVERED_CODEOWNERS, false)
    pending = _backfill_open_prs(conn, false)
    print(f"✅ Backfilled {TABLE}: {workflows} workflow and {codeowners} CODEOWNERS deliveries, "
          f"{pending} pending in open pull requests")


def run_migration():
    """Create file_deliveries and backfill it once."""
    database_url = get_migration_database_url()
    if not database_url:
        print("⚠️ No database URL configured, skipping migration")
        return

    print(f"🔄 Adding {TABLE}...")
    sqlite = "sqlite" in database_url
    engine = create_engine(database_url)
    with engine.begin() as conn:
        if not all(_table_exists(conn, sqlite, table) for table in _PARENT_TABLES):
            print("⏭️ parent tables do not exist yet, skipping migration")
            return
        _create_table(conn, sqlite)
        # Only into an empty table. run_migrations.py creates every ORM table
        # before any migration runs, so "the table did not exist" can never be
        # the trigger. Empty is true on the first start after this upgrade and
        # after restoring a backup from before it, and false from then on:
        # ActionsManager writes the record as it delivers.
        if conn.execute(text(f"SELECT 1 FROM {TABLE} LIMIT 1")).fetchone() is not None:
            print(f"✅ {TABLE} already holds deliveries, nothing to backfill")
            return
        _backfill(conn, sqlite)


if __name__ == "__main__":
    run_migration()
