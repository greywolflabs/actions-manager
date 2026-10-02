"""
Nothing outside backend/cloud/ and backend/tests/cloud/ may import the cloud
package or compare against the cloud installation mode (#2139).

The public export (scripts/export_public_tree.sh) and the self-hosted image
drop both folders, and the export refuses to publish if any file left still
references the package. This runs the same check with the normal suite, so a
leak fails the PR that adds it rather than the next release. It also catches
what the export's grep cannot: a mode check such as
``config.INSTALLATION_MODE == <cloud>`` in core, which would keep cloud
behaviour in the public tree without importing anything.
"""

import ast
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "cloud"

# Where cloud code is allowed, plus what the export drops anyway (see
# scripts/export_public_tree.sh): scripts/validate_release.py checks the cloud
# release, and tools/ holds internal tooling.
ALLOWED = {
    Path("backend") / PACKAGE,
    Path("backend") / "tests" / PACKAGE,
    Path("scripts"),
    Path("tools"),
}
SKIPPED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages",
    ".pytest_cache", ".mypy_cache", ".tox", "build", "dist",
}
# INSTALLATION_MODE, get_installation_mode(), active_mode, VALID_MODES; not "model".
MODE_EXPRESSION = re.compile(r"(?<![a-z])modes?(?![a-z])", re.IGNORECASE)
# Only the cloud build uses the Marketplace billing and admin-override schema, so
# a migration for it belongs in backend/cloud/, where the export drops it (#2182).
# Named columns, not "marketplace_": core's GitHub Actions Marketplace code is fine.
CLOUD_SCHEMA = re.compile(
    r"MarketplaceWebhookEvent|marketplace_(?:webhook|account_id|plan|unit_count"
    r"|on_free_trial|next_billing|updated_at)|admin_override",
    re.IGNORECASE,
)


def _is_package(name):
    return name == PACKAGE or name.startswith(PACKAGE + ".")


def _is_cloud_literal(node):
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.lower() == PACKAGE
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_cloud_literal(element) for element in node.elts)
    return False


def _import_violation(node):
    if isinstance(node, ast.Import):
        return any(_is_package(alias.name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.level == 0 and bool(node.module) and _is_package(node.module)
    return False


def _dynamic_import_violation(node):
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    first = node.args[0]
    return (
        name in {"import_module", "__import__"}
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and _is_package(first.value)
    )


def _string_target_violation(node):
    # patch("<pkg>.admin.x"), monkeypatch.setattr("<pkg>.x", ...) and the like.
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.match(re.escape(PACKAGE) + r"\.[A-Za-z_]", node.value) is not None
    )


def _mode_comparison_violation(node):
    if not isinstance(node, ast.Compare):
        return False
    operands = [node.left, *node.comparators]
    if not any(_is_cloud_literal(operand) for operand in operands):
        return False
    return any(
        MODE_EXPRESSION.search(ast.unparse(operand))
        for operand in operands
        if not _is_cloud_literal(operand)
    )


def _mode_match_violation(node):
    # match config.INSTALLATION_MODE: case <cloud>: ...
    if not isinstance(node, ast.Match) or not MODE_EXPRESSION.search(ast.unparse(node.subject)):
        return False
    return any(
        isinstance(pattern, ast.MatchValue) and _is_cloud_literal(pattern.value)
        for case in node.cases
        for pattern in ast.walk(case.pattern)
    )


CHECKS = (
    ("imports the cloud package", _import_violation),
    ("imports the cloud package dynamically", _dynamic_import_violation),
    ("names a cloud module", _string_target_violation),
    ("compares against the cloud installation mode", _mode_comparison_violation),
    ("matches on the cloud installation mode", _mode_match_violation),
)


def violations_in(source, filename="<source>"):
    """Return ``"<file>:<line>: <what>"`` for each boundary violation in *source*."""
    found = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        for description, check in CHECKS:
            if check(node):
                found.append(f"{filename}:{node.lineno}: {description}")
    return found


def _core_python_files():
    for directory, subdirectories, filenames in os.walk(REPO_ROOT):
        relative = Path(directory).relative_to(REPO_ROOT)
        subdirectories[:] = [
            name for name in subdirectories
            if name not in SKIPPED_DIRS
            and relative / name not in ALLOWED
            and not (Path(directory) / name / "pyvenv.cfg").exists()
        ]
        for filename in filenames:
            if filename.endswith(".py"):
                yield Path(directory) / filename


def test_core_never_references_the_cloud_package_or_mode():
    found = []
    for path in _core_python_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        found.extend(violations_in(path.read_text(encoding="utf-8"), relative))
    assert found == [], (
        "Cloud-only code outside backend/cloud/ (tests: backend/tests/cloud/). "
        "Move it there, or register it from cloud/app.py:\n" + "\n".join(found)
    )


def test_core_migrations_never_touch_cloud_only_schema():
    backend = REPO_ROOT / "backend"
    # run_migrations.py also runs scripts without the migrate_ prefix.
    listed = re.findall(r'"(\w+\.py)"', (backend / "run_migrations.py").read_text(encoding="utf-8"))
    listed_paths = {backend / name for name in listed if (backend / name).exists()}
    migrations = sorted(set(backend.glob("migrate_*.py")) | listed_paths)
    assert migrations, "No core migrations found - the check would pass on nothing"
    found = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in migrations
        if CLOUD_SCHEMA.search(path.read_text(encoding="utf-8"))
    ]
    assert found == [], (
        "Core migrations that touch cloud-only schema (Marketplace, admin override). "
        "Move them to backend/cloud/:\n" + "\n".join(found)
    )


def test_the_check_scans_core_code():
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _core_python_files()}
    assert "backend/main.py" in scanned
    assert "backend/mode_validation.py" in scanned
    assert not any(name.startswith(f"backend/{PACKAGE}/") for name in scanned)


def test_the_check_catches_each_kind_of_violation():
    # Built from PACKAGE so this file never contains what the export's grep
    # looks for.
    pkg = PACKAGE
    leaks = [
        f"import {pkg}",
        f"import {pkg}.app",
        f"import main, {pkg}",
        f"from {pkg} import admin",
        f"from {pkg}.admin import router",
        f"importlib.import_module('{pkg}.app')",
        f"__import__('{pkg}')",
        f"patch('{pkg}.admin.x')",
        f"if config.INSTALLATION_MODE == '{pkg}': pass",
        f"if config.get_installation_mode() != '{pkg}': pass",
        f"if active_mode in ('{pkg}', 'self-hosted'): pass",
        f"assert '{pkg}' == mode",
        f"if config.INSTALLATION_MODE.upper() == '{pkg.upper()}': pass",
        f"match config.INSTALLATION_MODE:\n    case '{pkg}':\n        pass",
        f"match get_installation_mode():\n    case 'self-hosted' | '{pkg}':\n        pass",
    ]
    for source in leaks:
        assert violations_in(source), source


def test_the_check_allows_unrelated_uses_of_the_word():
    pkg = PACKAGE
    fine = [
        f"from google.{pkg} import storage",
        f"from .{pkg} import helper",
        f"assert '{pkg}' in message",
        f"if provider == '{pkg}': pass",
        f"if model == '{pkg}': pass",
        f"match provider:\n    case '{pkg}':\n        pass",
        "if config.INSTALLATION_MODE == 'self-hosted': pass",
        f"path = 'backend/{pkg}/app.py'",
        f"os.environ['INSTALLATION_MODE'] = '{pkg}'",
    ]
    for source in fine:
        assert violations_in(source) == [], source
