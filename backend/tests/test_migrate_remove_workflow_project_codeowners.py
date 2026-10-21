"""
Tests for the migration that takes CODEOWNERS out of workflow projects (#2367).

It runs on every boot, so a second run must delete nothing. It must delete
only what workflow projects hold of CODEOWNERS (their records, custom files at
its paths, and those files' delivery rows) and leave a Managed Files
project's file, every other custom file and every workflow alone, and the
project list's cached drift summary must count what is left.
"""
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import deliveries
import migrate_remove_workflow_project_codeowners as migration
from file_kinds import FILE_KINDS, kind_for_path
from models import Account, Base, Codeowners, CustomFile, FileDelivery, Project, Repo


@pytest.fixture
def db_path():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield path
    os.remove(path)


def _seed(path) -> dict:
    """Two workflow projects and a CODEOWNERS project, each with files delivered to one repository."""
    engine = create_engine(f"sqlite:///{path}")
    with sessionmaker(bind=engine)() as db:
        owner = Account(github_user="owner", github_email="o@example.com", account_type="free")
        repo = Repo(repo_name="acme/api")
        db.add_all([owner, repo])
        db.flush()
        ids = {}
        for name, project_type, kind in (("caller", "standard", None), ("reusable", "rwx", None),
                                         ("owners", "managed_files", "codeowners")):
            project = Project(project_name=name, user_id=owner.user_id, project_type=project_type, file_kind=kind)
            db.add(project)
            db.flush()
            record = Codeowners(project_id=project.project_id, content="* @acme\n", file_path=".github/CODEOWNERS")
            db.add(record)
            db.flush()
            db.add(FileDelivery(project_id=project.project_id, repo_id=repo.repo_id, branch="main", codeowners_id=record.id))
            ids[name] = project.project_id
        for file_path in ("docs/CODEOWNERS", ".github//CODEOWNERS", "scripts/build.sh"):
            custom_file = CustomFile(project_id=ids["caller"], file_path=file_path, file_content="x")
            db.add(custom_file)
            db.flush()
            db.add(FileDelivery(project_id=ids["caller"], repo_id=repo.repo_id, branch="main",
                                custom_file_id=custom_file.id))
        db.commit()
    engine.dispose()
    return ids


def _run(path):
    with patch.object(migration, "get_migration_database_url", return_value=f"sqlite:///{path}"):
        migration.run_migration()


def _left(path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {
            "codeowners": conn.execute(
                "SELECT p.project_name FROM codeowners c JOIN projects p ON p.project_id = c.project_id"
            ).fetchall(),
            "custom_files": conn.execute("SELECT file_path FROM custom_files").fetchall(),
            "deliveries": conn.execute(
                "SELECT codeowners_id IS NOT NULL, custom_file_id IS NOT NULL FROM file_deliveries ORDER BY id"
            ).fetchall(),
        }
    finally:
        conn.close()


def test_deletes_only_what_workflow_projects_hold_of_codeowners(db_path):
    _seed(db_path)

    _run(db_path)

    assert _left(db_path) == {
        "codeowners": [("owners",)],
        "custom_files": [("scripts/build.sh",)],
        # The CODEOWNERS project's delivery, and the build script's.
        "deliveries": [(1, 0), (0, 1)],
    }


def _mark_all_drifted(path, status: str = "drifted"):
    """Every delivery drifted, and each project's cached summary counting its own."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE file_deliveries SET has_drift = 1")
        conn.execute("UPDATE projects SET drift_status = ?, drift_count = "
                     "(SELECT COUNT(*) FROM file_deliveries d WHERE d.project_id = projects.project_id)", (status,))
        conn.commit()
    finally:
        conn.close()


def _summaries(path) -> dict:
    """Each project's cached (drift_status, drift_count), and the count deliveries.drift_count gives."""
    engine = create_engine(f"sqlite:///{path}")
    try:
        with sessionmaker(bind=engine)() as db:
            return {
                project.project_name: (project.drift_status, project.drift_count,
                                       deliveries.drift_count(db, project.project_id))
                for project in db.query(Project)
            }
    finally:
        engine.dispose()


def test_recounts_the_drift_summary_of_projects_that_lose_drift(db_path):
    _seed(db_path)
    _mark_all_drifted(db_path)

    _run(db_path)

    assert _summaries(db_path) == {
        "caller": ("drifted", 1, 1),  # the build script's drift is left
        "reusable": ("clean", 0, 0),
        "owners": ("drifted", 1, 1),  # a CODEOWNERS project keeps its file
    }


def test_leaves_a_failed_checks_summary_to_the_next_check(db_path):
    _seed(db_path)
    _mark_all_drifted(db_path, status="check_failed")

    _run(db_path)

    assert _summaries(db_path)["reusable"] == ("check_failed", 1, 0)


def test_a_second_run_deletes_nothing(db_path):
    _seed(db_path)
    _run(db_path)
    first = _left(db_path)

    _run(db_path)

    assert _left(db_path) == first


def test_waits_for_the_file_kind_column(db_path):
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE projects DROP COLUMN file_kind")
    conn.commit()
    conn.close()

    _run(db_path)  # file_kind's own migration failed: nothing can tell a CODEOWNERS project apart

    assert len(_left(db_path)["codeowners"]) == 3


def test_the_frozen_paths_are_the_kind_definitions():
    assert migration.CODEOWNERS_PATHS == FILE_KINDS["codeowners"].paths


@pytest.mark.parametrize("path", ["./CODEOWNERS", ".github//CODEOWNERS", "docs\\CODEOWNERS", "scripts/build.sh"])
def test_the_frozen_normal_form_is_kind_for_paths(path):
    assert (migration.normal_path(path) in migration.CODEOWNERS_PATHS) is (kind_for_path(path) == "codeowners")


def test_is_registered_after_the_column_it_reads():
    from run_migrations import MIGRATION_SCRIPTS

    assert (MIGRATION_SCRIPTS.index("migrate_add_project_file_kind.py")
            < MIGRATION_SCRIPTS.index("migrate_remove_workflow_project_codeowners.py")
            < len(MIGRATION_SCRIPTS) - 2)
