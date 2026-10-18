"""Only the scheduler writes projects in bulk (#2343).

next_drift_check_at is derived from a project's last check, failure streak and
interval. Every ORM write to those reschedules the row in the same UPDATE,
through Project's mapper events, whatever the call site
(test_drift_sweep.TestEveryOrmWriteReschedules). A bulk write skips those
events, so one that touched an input would leave a stale time still marked as
scheduled, with nothing visibly wrong: the sweep only revisits a row whose
marker is empty or whose inherited default changed. The bulk writers live in
drift_worker and write the time and marker themselves; a new one elsewhere has
to be reviewed for that and added here. The match is deliberately broad (any
core-level use of the projects table, any bulk_save_objects) because a miss is
silent. The damage is bounded, though: the stale time still comes due once,
and that check records its result through the ORM, which reschedules the row.

Backup restore writes every table generically (table.insert() in
backup_engine), never naming Project. It is covered differently: restore runs
the migrations afterwards, and migrate_add_next_drift_check_at recomputes every
row and rewrites the ones that disagree. That also repairs, on the same boot,
whatever an earlier migration wrote in bulk, so the only migrations to review
are the ones that run after it.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from run_migrations import MIGRATION_SCRIPTS  # noqa: E402

SCHEDULE_CONVERGE = "migrate_add_next_drift_check_at.py"
BULK_PROJECT_WRITE = re.compile(
    r"(?:update|insert)\((?:\w+\.)*Project\b"               # update(Project...), insert(models.Project...)
    r"|Project\.__table__\b"                                # any core-level use of the table
    r"|bulk_(?:insert|update)_mappings\(\s*(?:\w+\.)*Project\b"  # session.bulk_*_mappings(Project, ...)
    r"|bulk_save_objects\("                                 # takes any objects, Projects among them
    r"|(?i:(?:UPDATE|INSERT\s+INTO)\s+\"?projects\b)",      # raw SQL
)
BULK_WRITERS = {"drift_worker.py"}


def _names_project(node) -> bool:
    """Project, models.Project, or an attribute of either (query(Project.project_id))."""
    while isinstance(node, ast.Attribute):
        if node.attr == "Project":
            return True
        node = node.value
    return isinstance(node, ast.Name) and node.id == "Project"


def _queries_projects(node, query_names) -> bool:
    """Whether node is a query chain over Project: from query(Project), or a name bound to one."""
    while True:
        if isinstance(node, ast.Name):
            return node.id in query_names
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "query"
                    and node.args and _names_project(node.args[0])):
                return True
            node = node.func
        else:
            return False


def _updates_a_project_query(source: str) -> bool:
    """query(Project)...update(...), however the chain is split over lines or variables.

    Parsed rather than matched: a regex cannot follow the codebase's usual
    db.query(X).filter(<newline> condition <newline>).update(...)."""
    tree = ast.parse(source)
    query_names = {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and _queries_projects(node.value, set())
                   for target in node.targets if isinstance(target, ast.Name)}
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
               and _queries_projects(node.func.value, query_names)
               for node in ast.walk(tree))


def _writes_projects_in_bulk(source: str) -> bool:
    return bool(BULK_PROJECT_WRITE.search(source)) or _updates_a_project_query(source)


def _bulk_writers():
    return {
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if "tests" not in path.parts
        and not path.name.startswith("migrate_")
        and _writes_projects_in_bulk(path.read_text(encoding="utf-8"))
    }


def test_only_the_scheduler_writes_projects_in_bulk():
    assert _bulk_writers() == BULK_WRITERS


def test_no_migration_after_the_schedule_converge_writes_projects_in_bulk():
    """One that did would leave its rows' times stale until the next boot."""
    later = MIGRATION_SCRIPTS[MIGRATION_SCRIPTS.index(SCHEDULE_CONVERGE) + 1:]
    assert [name for name in later
            if _writes_projects_in_bulk((BACKEND / name).read_text(encoding="utf-8"))] == []


@pytest.mark.parametrize("source", [
    "db.query(Project).filter(Project.user_id == uid).update({'drift_check_failure_count': 0})",
    "db.query(Project).filter(\n    Project.user_id == uid\n).update({'drift_check_failure_count': 0})",
    "db.query(models.Project).filter(x).update({})",
    "db.query(Project.project_id).filter(x).update({})",
    "projects = db.query(Project).filter(x)\nprojects.update({})",
    "db.execute(update(Project).values(x=1))",
    "db.execute(update(models.Project).values(x=1))",
    "db.execute(insert(Project), rows)",
    "db.execute(Project.__table__.update().values(x=1))",
    "db.bulk_update_mappings(Project, rows)",
    "db.bulk_save_objects(projects)",
    "db.execute(text('UPDATE projects SET x = 1'))",
    "db.execute(text('UPDATE \"projects\" SET x = 1'))",
])
def test_the_guard_recognises_a_bulk_project_write(source):
    assert _writes_projects_in_bulk(source)


def test_the_guard_ignores_other_tables_and_dicts():
    assert not _writes_projects_in_bulk(
        "db.query(Workflow).filter(\n    Workflow.project_id == project.project_id\n).update({})\n"
        "project = db.query(Project).first()\nsettings.update({'a': 1})\n")
