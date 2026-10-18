"""
Tests for migrate_pin_delivered_paths: each delivery the backfill recorded
without a path gets the one its file was delivered at, as workflows formats it,
every path already recorded is left alone, and workflows.renamed_from is
dropped once nothing needs it.
"""
import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate_pin_delivered_paths as migration  # noqa: E402
from models import (  # noqa: E402
    Account, Base, Codeowners, CustomFile, FileDelivery, Project, ProjectPullRequest, ProjectWorkflow, Repo, Workflow,
)
from workflows import format_workflow_name  # noqa: E402

DELIVERED = datetime(2026, 8, 1, 12, 0, 0)
APP, WEB = "acme/app", "acme/web"
MAIN, DEV = "main", "dev"

# (workflow id, stored name, renamed_from, owning project id)
WORKFLOWS = [
    (100, "ci", None, 1),
    (101, "deploy", "release", 1),
    (102, "lint", None, 2),
    (103, "build.yaml", None, 2),
    (104, "test.YML", None, 2),
    (105, "pages.yml", "site", 2),
    # Shorter than the suffixes, where SQLite's and PostgreSQL's SUBSTR differ.
    (106, "go", None, 2),
    (107, "yml", None, 2),
]
# project id -> (code, use_prefix)
PROJECTS = {1: ("PRE", True), 2: ("BARE", False)}


def _seed(engine):
    db = sessionmaker(bind=engine)()
    try:
        db.add(Account(user_id=1, github_user="owner", github_email="o@e.com", account_type="free"))
        db.add_all([Project(project_id=pid, project_code=code, project_name=code.lower(), user_id=1,
                            project_type="standard", use_prefix=prefix)
                    for pid, (code, prefix) in PROJECTS.items()])
        db.add_all([Repo(repo_id=10, repo_name=APP), Repo(repo_id=11, repo_name=WEB)])
        db.add_all([Workflow(workflow_id=wid, workflow_name=name, workflow_yaml="on: push")
                    for wid, name, _, _ in WORKFLOWS])
        db.flush()
        db.add_all([ProjectWorkflow(project_id=pid, workflow_id=wid) for wid, _, _, pid in WORKFLOWS])
        db.add_all([
            CustomFile(id=200, project_id=1, file_path=".github/scripts/build.sh", file_content="x"),
            Codeowners(id=300, project_id=1, content="* @a", file_path="CODEOWNERS"),
            ProjectPullRequest(pr_id=1, project_id=1, repo_name=WEB, pr_number=1, pr_url="u",
                               branch_name="b", target_branch=MAIN, pr_state="open"),
            ProjectPullRequest(pr_id=2, project_id=1, repo_name=WEB, pr_number=2, pr_url="u",
                               branch_name="b", target_branch=DEV, pr_state="closed"),
        ])
        db.flush()
        # Delivered by the backfill, with no path.
        db.add_all([FileDelivery(project_id=pid, repo_id=10, branch=MAIN, workflow_id=wid, delivered_at=DELIVERED)
                    for wid, _, _, pid in WORKFLOWS])
        db.add_all([
            FileDelivery(project_id=1, repo_id=10, branch=MAIN, custom_file_id=200, delivered_at=DELIVERED),
            FileDelivery(project_id=1, repo_id=10, branch=MAIN, codeowners_id=300, delivered_at=DELIVERED),
            # A path already recorded is kept, and a row with no delivery gets none.
            FileDelivery(project_id=1, repo_id=10, branch=DEV, workflow_id=100, delivered_at=DELIVERED,
                         delivered_path=".github/workflows/somewhere.yml"),
            FileDelivery(project_id=1, repo_id=10, branch="drift-only", workflow_id=100),
            # Carried by an open PR: its current name, renamed_from or not.
            FileDelivery(project_id=1, repo_id=11, branch=MAIN, workflow_id=101, pending_pr_id=1),
            FileDelivery(project_id=1, repo_id=11, branch=MAIN, codeowners_id=300, pending_pr_id=1),
            # A removal, and a PR no longer open, carry no path to fill.
            FileDelivery(project_id=1, repo_id=11, branch=MAIN, workflow_id=100, pending_pr_id=1,
                         pending_delete=True),
            FileDelivery(project_id=1, repo_id=11, branch=DEV, workflow_id=100, pending_pr_id=2),
        ])
        db.commit()
    finally:
        db.close()


def _add_renamed_from(engine):
    """workflows as an existing database has it: renamed_from, set on the renames not yet delivered."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE workflows ADD COLUMN renamed_from VARCHAR(255)"))
        for wid, _, old, _ in WORKFLOWS:
            conn.execute(text("UPDATE workflows SET renamed_from = :old WHERE workflow_id = :wid"),
                         {"old": old, "wid": wid})


def _seeded(path, renamed_from: bool):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    _seed(engine)
    if renamed_from:
        _add_renamed_from(engine)
    engine.dispose()
    return path


@pytest.fixture
def db_path(tmp_path):
    return _seeded(str(tmp_path / "pin.db"), renamed_from=True)


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def _paths(path):
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT r.repo_name, f.branch, COALESCE(f.workflow_id, f.custom_file_id, f.codeowners_id), "
                "f.delivered_path, f.pending_path FROM file_deliveries f JOIN repos r ON r.repo_id = f.repo_id"
            )).fetchall()
        return {(repo, branch, file_id): (delivered, pending) for repo, branch, file_id, delivered, pending in rows}
    finally:
        engine.dispose()


def _workflow_columns(path):
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return {row[1] for row in conn.execute(text("PRAGMA table_info(workflows)"))}
    finally:
        engine.dispose()


def _formatted(name, project_id):
    return ".github/workflows/" + format_workflow_name(name, *PROJECTS[project_id])


def test_every_delivered_workflow_gets_the_path_format_workflow_name_gives_it(db_path):
    """Prefix, no prefix, a .yaml name, an uppercase .YML one, which the suffix
    check must not read as .yml (format_workflow_name compares case-sensitively),
    and names shorter than the suffix."""
    _run(db_path)

    paths = _paths(db_path)
    for wid, name, renamed_from, pid in WORKFLOWS:
        assert paths[(APP, MAIN, wid)][0] == _formatted(renamed_from or name, pid), name
    assert paths[(APP, MAIN, 103)][0] == ".github/workflows/build.yaml"
    assert paths[(APP, MAIN, 104)][0] == ".github/workflows/test.YML.yml"


def test_an_undelivered_rename_is_still_where_it_was_delivered(db_path):
    _run(db_path)

    paths = _paths(db_path)
    assert paths[(APP, MAIN, 101)][0] == ".github/workflows/AM_PRE_release.yml"
    assert paths[(APP, MAIN, 105)][0] == ".github/workflows/site.yml"


def test_custom_files_and_codeowners_are_where_they_are_kept(db_path):
    _run(db_path)

    paths = _paths(db_path)
    assert paths[(APP, MAIN, 200)][0] == ".github/scripts/build.sh"
    assert paths[(APP, MAIN, 300)][0] == "CODEOWNERS"


def test_an_open_pr_carries_the_current_name(db_path):
    _run(db_path)

    paths = _paths(db_path)
    assert paths[(WEB, MAIN, 101)] == (None, ".github/workflows/AM_PRE_deploy.yml")
    assert paths[(WEB, MAIN, 300)] == (None, "CODEOWNERS")


def test_existing_paths_removals_and_rows_with_nothing_delivered_are_left_alone(db_path):
    _run(db_path)

    paths = _paths(db_path)
    assert paths[(APP, DEV, 100)] == (".github/workflows/somewhere.yml", None)
    assert paths[(APP, "drift-only", 100)] == (None, None)
    assert paths[(WEB, MAIN, 100)] == (None, None)
    assert paths[(WEB, DEV, 100)] == (None, None)


def test_running_again_changes_nothing(db_path):
    _run(db_path)
    first = _paths(db_path)

    _run(db_path)

    assert _paths(db_path) == first


def test_renamed_from_is_dropped_once_the_paths_are_pinned(db_path):
    _run(db_path)

    assert "renamed_from" not in _workflow_columns(db_path)


def test_sqlite_too_old_to_drop_a_column_keeps_renamed_from_and_still_pins(db_path):
    with patch.object(migration.sqlite3, "sqlite_version_info", (3, 34, 0)):
        _run(db_path)

    assert "renamed_from" in _workflow_columns(db_path)
    assert _paths(db_path)[(APP, MAIN, 101)][0] == ".github/workflows/AM_PRE_release.yml"


def test_without_renamed_from_each_workflow_is_where_its_name_says(tmp_path):
    """A database created after the column was dropped."""
    path = _seeded(str(tmp_path / "fresh.db"), renamed_from=False)

    _run(path)

    paths = _paths(path)
    for wid, name, _, pid in WORKFLOWS:
        assert paths[(APP, MAIN, wid)][0] == _formatted(name, pid), name


def test_a_database_without_the_record_is_skipped(tmp_path):
    path = str(tmp_path / "empty.db")
    create_engine(f"sqlite:///{path}").dispose()

    _run(path)
