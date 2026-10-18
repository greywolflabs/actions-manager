"""A catch-all handler never passes its exception's text on (#2295, #2297).

``except Exception as e`` catches whatever went wrong, and its text says too
much: a database error carries its SQL and bound parameters, a network error the
internal hosts and proxy it went through. Wherever it ends up (an HTTPException
detail, a returned dict, a task status, a per-item message, a stored field), a
client can read it. So inside a catch-all the exception may only be:

- logged: print, debug_log, and logger.*, logging.* or traceback.* calls;
- re-raised or chained: ``raise e``, ``raise X(...) from e``;
- inspected: isinstance, type(e), e.status_code, getattr(e, "status_code");
- described by kind: failure_kind(e).

A handler for a specific exception type is not checked. Most catch an error
whose message is written for the reader, such as a parse error in the caller's
own input or what GitHub answered about a repository. Handlers for OS, network
and database errors are the exception: their text says as much as a catch-all's
and they are not checked yet.

backend/cloud/ is dropped by the public export, so its twin in tests/cloud/
checks that tree with the same rules. Migration scripts run from the command
line, not behind a route, so they are not checked.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {"tests", "cloud", "__pycache__", ".venv", "venv", "site-packages"}
CATCH_ALL = {"Exception", "BaseException"}
LOG_FUNCTIONS = {"print", "debug_log"}
LOG_RECEIVERS = {"logger", "logging", "traceback"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "print_exc", "format_exc"}
INSPECTORS = {"isinstance", "type", "failure_kind"}

# Catch-alls that keep the text on purpose, by path under backend/ and
# function, with why.
ALLOWED = {
    "workflows.py": {
        # Stored as drift_error_summary; redacting it is #2062.
        "run_project_drift_check",
    },
    "license.py": {
        # Its only caller, get_installation_tier, prints the reason; no route returns it.
        "validate_license_key",
    },
}


def _is_catch_all(handler):
    caught = handler.type
    types = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(t, ast.Name) and t.id in CATCH_ALL for t in types)


def _call_name(call):
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _allowed_call(call):
    name = _call_name(call)
    if isinstance(call.func, ast.Name) and (name in INSPECTORS or name in LOG_FUNCTIONS):
        return True
    receiver = getattr(call.func, "value", None)
    if name in LOG_METHODS and isinstance(receiver, ast.Name) and receiver.id in LOG_RECEIVERS:
        return True
    return (
        name == "getattr" and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant) and call.args[1].value == "status_code"
    )


def _allowed_use(name, parents, handler):
    """Whether this read of the exception's name only logs, chains or inspects it."""
    node = name
    while node is not handler:
        parent = parents[node]
        if isinstance(parent, ast.Raise) and node is name:
            return True  # raise e / raise X from e
        if isinstance(parent, ast.Attribute) and parent.attr == "status_code":
            return True
        if isinstance(parent, ast.Call) and _allowed_call(parent):
            return True
        if isinstance(parent, ast.stmt):
            return False
        node = parent
    return False


def _own_names(handler):
    """Reads of *handler*'s name in its body, minus nested handlers that rebind it."""
    stack = list(handler.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.ExceptHandler) and node.name == handler.name:
            continue
        if isinstance(node, ast.Name) and node.id == handler.name:
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _leaks(source, allowed=frozenset()):
    """Sorted lines where a catch-all handler passes its exception on.

    Handlers inside a function named in *allowed* are skipped.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    exempt = {
        id(node)
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name in allowed
        for node in ast.walk(function)
    }
    lines = set()
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name and _is_catch_all(handler)):
            continue
        if id(handler) in exempt:
            continue
        lines.update(name.lineno for name in _own_names(handler) if not _allowed_use(name, parents, handler))
    return sorted(lines)


def _sources():
    return sorted(
        path for path in BACKEND.rglob("*.py")
        if not SKIPPED_DIRS.intersection(path.relative_to(BACKEND).parts)
        and not path.name.startswith("migrate_")
    )


def _key(path):
    return path.relative_to(BACKEND).as_posix()


def _handler(body):
    return "try:\n    pass\nexcept Exception as e:\n" + "".join(f"    {line}\n" for line in body)


def test_the_check_finds_each_way_the_text_leaves():
    source = "".join([
        _handler(["raise HTTPException(500, detail=str(e))"]),        # 4
        _handler(["return {'error': str(e)}"]),                       # 8
        _handler(["tasks[task_id]['error'] = f'failed: {e}'"]),       # 12
        _handler(["errors.append(f'{name}: {e.args[0]}')"]),          # 16
        _handler(["raise _ApiError(502, f'failed: {e}')"]),           # 20
        _handler(["msg = str(e)"]),                                   # 24
        _handler(["results.error(str(e))"]),                          # 28
        _handler(["return {'error': helper.failure_kind(e)}"]),       # 32
    ])

    assert _leaks(source) == [4, 8, 12, 16, 20, 24, 28, 32]


def test_logging_chaining_and_inspecting_are_allowed():
    source = "".join([
        _handler(["print(f'failed: {e}')", "logger.warning('x %s', e)", "traceback.print_exc()"]),
        _handler(["raise ValueError('bad') from e"]),
        _handler(["raise e"]),
        _handler(["missing = getattr(e, 'status_code', None) == 404", "code = e.status_code"]),
        _handler(["name = type(e).__name__", "network = isinstance(e, OSError)"]),
        _handler(["return {'error': f'Failed: {failure_kind(e)}'}"]),
        "try:\n    pass\nexcept ValueError as e:\n    raise HTTPException(400, detail=str(e))\n",
    ])

    assert _leaks(source) == []


def test_a_nested_handler_is_judged_on_its_own():
    """A typed handler inside a catch-all may reuse the name; a nested leak is reported once."""
    source = (
        "try:\n    pass\nexcept Exception as e:\n"
        "    try:\n        pass\n    except ValueError as e:\n"
        "        raise HTTPException(400, detail=str(e))\n"
        "try:\n    pass\nexcept Exception as e:\n"
        "    try:\n        pass\n    except Exception as e:\n"
        "        raise HTTPException(500, detail=str(e))\n"
    )

    assert _leaks(source) == [14]


def test_an_allowed_function_is_skipped():
    source = "def keep():\n" + "".join(f"    {line}\n" for line in _handler(["return str(e)"]).splitlines())

    assert _leaks(source) == [5]
    assert _leaks(source, {"keep"}) == []


def test_no_catch_all_handler_passes_its_exception_on():
    sources = _sources()
    assert BACKEND / "workflows.py" in sources

    leaks = [
        f"{path.relative_to(BACKEND)}:{line}"
        for path in sources
        for line in _leaks(path.read_text(encoding="utf-8"), ALLOWED.get(_key(path), frozenset()))
    ]

    assert leaks == [], (
        "These catch-all handlers pass the exception's text on, where a client can "
        "read it. Return a fixed message or failure_kind(e), and print the exception:\n"
        + "\n".join(leaks)
    )
