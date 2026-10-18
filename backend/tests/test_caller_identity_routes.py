"""A route takes the caller from the session, not from a name the client sends (#2241).

A ``github_user`` (or ``user``) parameter is client-supplied and proves nothing,
so every route that reads one had to remember to check it against the session.
Ruleset delete (#2013) and ruleset create (#2234) forgot, and any signed-in
member could act as someone else by naming them.

``auth.current_account`` takes the caller from the session instead, and owns the
legacy query parameter so a mismatch is refused in one place. This walks every
route and fails when one reads a caller name itself, so a new route can't forget;
using the dependency is not an exemption, since a route could depend on it and
still act on ``payload.github_user``. It reads the endpoint and the helpers of
its own module that it calls, since a helper can read the body for it.
``ALLOWLIST`` holds only the routes where ``github_user`` names a project
*owner*, which legitimately differs from the caller. Those still take the caller
from the session (``authorization.get_current_user``); the named owner only
picks between projects the caller may reach.
"""

import inspect
import os
import re
import sys
from types import SimpleNamespace

from fastapi.routing import APIRoute
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auth  # noqa: E402
import authorization  # noqa: E402
from main import app  # noqa: E402

CALLER_NAMES = {"github_user", "user"}
_RAW_READ = re.compile(r"""\.get\(\s*["'](?:github_user|user)["']""")
# GitHub's own "user" object, a pull request's author, read for its login. It
# names nobody the request acts as.
_GITHUB_USER_LOGIN = re.compile(
    r"""\.get\(\s*["']user["']\s*(?:,\s*\{\}\s*)?\)(?:\s*or\s*\{\}\s*\))?\s*\.get\(\s*["']login["']"""
)

OWNER = "github_user names the project owner, not the caller"

ALLOWLIST = {
    # projects.py
    ("DELETE", "/api/projects/{project_name}"): OWNER,
    ("DELETE", "/api/projects/{project_name}/linked-reusable-workflows/{workflow_id}"): OWNER,
    ("GET", "/api/projects/{project_name}"): OWNER,
    ("PATCH", "/api/projects/{project_id}/drift-config"): OWNER,
    ("PATCH", "/api/projects/{project_id}/project-color"): OWNER,
    ("PATCH", "/api/projects/{project_id}/project-name"): OWNER,
    ("POST", "/api/projects/{project_name}/linked-reusable-workflows"): OWNER,
    ("PUT", "/api/projects/{project_id}/"): OWNER,
    ("PUT", "/api/projects/{project_name}/linked-reusable-workflows/{workflow_id}"): OWNER,
}


_BACKEND_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))


def _is_core(route):
    """Core routers are all modules at the top of backend/. A test session that
    also loads the cloud entrypoint mounts its routes on this same app, and the
    public export drops those, so they are out of this walk's reach."""
    return os.path.dirname(os.path.realpath(inspect.getsourcefile(route.endpoint))) == _BACKEND_DIR


def _routes(routes=None, prefix=""):
    for route in app.routes if routes is None else routes:
        if isinstance(route, APIRoute):
            if _is_core(route):
                yield prefix + route.path, route
        elif hasattr(route, "original_router"):
            # FastAPI wraps an included router instead of copying its routes.
            yield from _routes(route.original_router.routes, prefix + route.include_context.prefix)


def _model_fields(param):
    annotation = getattr(param.field_info, "annotation", None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return set(annotation.model_fields)
    return set()


def _helpers_called(endpoint):
    """Functions of the endpoint's own module that it calls by name.

    update-env-vars read the caller from the body through such a helper, which
    a check of the endpoint's source alone never saw.
    """
    module = sys.modules.get(endpoint.__module__)
    code = getattr(endpoint, "__code__", None)
    names = code.co_names if code else ()
    return [
        fn for fn in (getattr(module, name, None) for name in names)
        if inspect.isfunction(fn) and fn.__module__ == endpoint.__module__
    ]


def _reads_caller_name(route):
    """True when the endpoint takes a caller name, itself or through a helper it calls."""
    dependant = route.dependant
    params = dependant.query_params + dependant.path_params + dependant.body_params
    if any(p.name in CALLER_NAMES or _model_fields(p) & CALLER_NAMES for p in params):
        return True
    for fn in [route.endpoint, *_helpers_called(route.endpoint)]:
        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if _RAW_READ.search(_GITHUB_USER_LOGIN.sub("", source)):
            return True
    return False


def _uses_current_account(dependant):
    return any(
        sub.call is auth.current_account or _uses_current_account(sub)
        for sub in dependant.dependencies
    )


def _takes_a_session(dependant):
    return any(
        sub.call in (auth.current_account, authorization.get_current_user) or _takes_a_session(sub)
        for sub in dependant.dependencies
    )


def _keys(path, route):
    return [(method, path) for method in sorted(route.methods)]


def _reading_caller_name():
    return {key for path, route in _routes() if _reads_caller_name(route) for key in _keys(path, route)}


def test_no_route_reads_a_caller_name_the_client_sent():
    offenders = sorted(_reading_caller_name() - set(ALLOWLIST))
    assert offenders == [], (
        "These routes read a caller name the client sent. Take the caller from "
        f"auth.CurrentAccount instead: {offenders}"
    )


def test_allowlist_has_no_stale_entries():
    stale = sorted(set(ALLOWLIST) - _reading_caller_name())
    assert stale == [], f"Converted or removed; delete these from ALLOWLIST: {stale}"


def test_allowlist_reasons_are_known():
    assert set(ALLOWLIST.values()) <= {OWNER}


def test_owner_routes_still_take_the_caller_from_the_session():
    """An OWNER route's github_user names whose project, never who is asking.
    Without a session dependency it would be back to trusting a name (#2241)."""
    no_session = sorted(
        key
        for path, route in _routes()
        for key in _keys(path, route)
        if key in ALLOWLIST and not _takes_a_session(route.dependant)
    )
    assert no_session == [], f"These ALLOWLIST routes take no session: {no_session}"


def test_the_walk_sees_a_converted_route():
    """Guards the walk itself: if it stopped seeing routes or dependencies,
    the two tests above would pass while checking nothing."""
    converted = {
        key
        for path, route in _routes()
        if _uses_current_account(route.dependant)
        for key in _keys(path, route)
    }
    assert ("GET", "/api/projects/{project_id}/build-metrics") in converted


def _caller_from_body(data):
    return data.get("user")


def _endpoint_reading_through_a_helper(data):
    return _caller_from_body(data)


def test_the_walk_sees_a_read_through_a_helper():
    """Guards the helper scan: no route reads a caller name through a helper
    any more, so if it stopped looking, nothing above would fail."""
    route = SimpleNamespace(
        endpoint=_endpoint_reading_through_a_helper,
        dependant=SimpleNamespace(query_params=[], path_params=[], body_params=[]),
    )

    assert _helpers_called(route.endpoint) == [_caller_from_body]
    assert _reads_caller_name(route)


def _endpoint_reading_a_pull_requests_author(pr):
    return pr.get("user", {}).get("login"), (pr.get("user") or {}).get("login")


def test_the_walk_does_not_count_a_pull_requests_author():
    """GitHub's own "user" object names the PR's author, not the caller, so
    reading its login is not a caller name the client sent."""
    route = SimpleNamespace(
        endpoint=_endpoint_reading_a_pull_requests_author,
        dependant=SimpleNamespace(query_params=[], path_params=[], body_params=[]),
    )

    assert not _reads_caller_name(route)
