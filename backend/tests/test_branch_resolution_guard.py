"""Target branches come from the branch resolver, not the repo's GitHub default (#2301).

A project delivers to the branches its configuration names: the default branch,
or every branch matching a pattern, set per project or per repository.
resolve_branch_config_for_repo + _resolve_branches_for_repo turn that into branch
names, and delivery and drift both go through them. Calling get_default_branch
anywhere else skips the configuration, so a project delivering to release/* gets
compared against, or written to, main (#2300, #2301). A caller that already knows
the branch passes it instead.

The calls that remain are allowlisted below by path under backend/ and function,
each with why the default branch is the right one there. backend/cloud/ is checked
by its twin in tests/cloud/.
"""

import ast

from tests.test_no_exception_text_in_error_detail import BACKEND, _key, _sources

ALLOWED = {
    "workflows.py": {
        # The resolver itself: default-branch mode, and a pattern that matches nothing.
        "_resolve_branches_for_repo",
        # No project, so no branch configuration to honour (see its docstring).
        "_resolve_drift_branches_for_repo",
        # Reusable workflows are published on the RWX repository's default branch
        # (docs/src/content/docs/features/drift-detection.md, "Reusable Workflow Drift").
        "_list_reusable_repo",
        # Where a campaign delivers them, and a removal falls back to, alike.
        "_reusable_delivery_target",
        "_reusable_impact_targets",
    },
    "actions_projects.py": {
        # An action URL with no ref names that action's own repository, not a delivery target.
        "preview_actions_project",
    },
    "workflow_import.py": {
        # Discovery scans a repository for workflows to import: a source, not a delivery target.
        "discover_workflows",
    },
}


def _name(call):
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _calls(source, allowed=frozenset()):
    """Sorted lines that call get_default_branch outside a function named in *allowed*."""
    tree = ast.parse(source)
    exempt = {
        id(node)
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name in allowed
        for node in ast.walk(function)
    }
    return sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and id(node) not in exempt and _name(node) == "get_default_branch"
    )


def test_the_check_finds_a_direct_call():
    source = (
        "def deliver():\n"
        "    branch = get_default_branch(owner, repo, headers)\n"
        "    other = workflows.get_default_branch(owner, repo, headers)\n"
    )

    assert _calls(source) == [2, 3]
    assert _calls(source, {"deliver"}) == []


def test_no_target_branch_is_read_from_the_default_branch_outside_the_resolver():
    sources = _sources()
    assert BACKEND / "workflows.py" in sources

    calls = [
        f"{_key(path)}:{line}"
        for path in sources
        for line in _calls(path.read_text(encoding="utf-8"), ALLOWED.get(_key(path), frozenset()))
    ]

    assert calls == [], (
        "These call get_default_branch, which skips the project's branch configuration. "
        "Resolve branches with _resolve_effective_target_branches, or pass the branch the "
        "caller already knows:\n" + "\n".join(calls)
    )

