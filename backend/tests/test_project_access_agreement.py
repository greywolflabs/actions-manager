"""Every project lookup and gate gives the same answer, from authorization.project_role.

Project access used to be decided in six lookups and four editor gates, and they
disagreed: a read_only owner was visible to some and not others, and two
owners' same-named projects resolved to the caller's own in one module and to a
409 in another (#2240). Each entry point below now delegates to one rule, and
this asserts they agree, so a new copy that drifts fails here.
"""
import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import custom_files  # noqa: E402
import projects  # noqa: E402
import workflows  # noqa: E402
from authorization import PROJECT_DELETING, has_project_role, project_role  # noqa: E402
from models import Account, Base, Project, ProjectMembership, WorkspaceMember  # noqa: E402

# caller -> (workspace role or None, owns the project, grant or None), (may see, may edit)
CALLERS = {
    "admin": (("admin", False, None), (True, True)),
    "owner": (("member", True, None), (True, True)),
    "owner without a workspace row": ((None, True, None), (True, True)),
    "read_only owner": (("read_only", True, None), (True, False)),
    "member with an editor grant": (("member", False, "project_editor"), (True, True)),
    "member with a viewer grant": (("member", False, "project_viewer"), (True, False)),
    "member without a grant": (("member", False, None), (True, False)),
    "read_only with an editor grant": (("read_only", False, "project_editor"), (True, False)),
    "read_only with a viewer grant": (("read_only", False, "project_viewer"), (True, False)),
    "read_only without a grant": (("read_only", False, None), (False, False)),
    "account without a workspace row": ((None, False, None), (False, False)),
}


@pytest.fixture
def make_db():
    sessions = []

    def make():
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        sessions.append(db)
        return db

    yield make
    for db in sessions:
        db.close()


def _world(db, role, owns, grant, pr_state="new"):
    other = Account(github_user="other", github_email="o@example.com", account_type="free")
    me = Account(github_user="me", github_email="m@example.com", account_type="free")
    db.add_all([other, me])
    db.flush()
    db.add(WorkspaceMember(user_id=other.user_id, workspace_role="admin"))
    if role:
        db.add(WorkspaceMember(user_id=me.user_id, workspace_role=role))
    project = Project(project_name="alpha", project_code="AAA", user_id=(me if owns else other).user_id,
                      pr_state=pr_state)
    db.add(project)
    db.flush()
    if grant:
        db.add(ProjectMembership(user_id=me.user_id, project_id=project.project_id, project_role=grant))
    db.commit()
    member = db.query(WorkspaceMember).filter_by(user_id=me.user_id).first()
    return me, member, project


def _allowed(call):
    try:
        result = call()
    except HTTPException as exc:
        assert exc.status_code in (403, 404), exc.detail
        return False
    return result is not False and (result is None or bool(result))


def _found(call):
    return call() is not None


def _seen(db, me, member, project):
    owner_arg = None if member is not None else me.github_user
    return {
        "project_role": project_role(db, me, project) is not None,
        "workflows._find_project_by_name": _found(lambda: workflows._find_project_by_name(db, "me", "alpha")),
        "workflows._caller_can_see_project": workflows._caller_can_see_project(db, "me", project),
        "projects._find_project_by_name": _found(
            lambda: projects._find_project_by_name(db, "alpha", member, github_user=owner_arg)),
        "projects._find_project_by_id": _found(
            lambda: projects._find_project_by_id(db, project.project_id, member, github_user="me")),
        "projects._require_project_access(viewer)": _allowed(
            lambda: projects._require_project_access(db, project.project_id, me, "project_viewer")),
        "custom_files._get_project_for_user": _allowed(
            lambda: custom_files._get_project_for_user(db, project.project_id, "me")),
    }


def _edited(db, me, project):
    return {
        "has_project_role(editor)": has_project_role(db, me, project, "project_editor"),
        "workflows._require_project_editor": _allowed(lambda: workflows._require_project_editor(db, "me", project)),
        "projects._require_project_editor": _allowed(lambda: projects._require_project_editor(db, me, project)),
        "projects._require_project_access(editor)": _allowed(
            lambda: projects._require_project_access(db, project.project_id, me, "project_editor")),
        "custom_files._require_project_editor": _allowed(
            lambda: custom_files._require_project_editor(db, project, "me")),
    }


def _settled(db, me, project):
    """The gates merging and closing a pull request pass, which still work on a project being deleted."""
    return {
        "has_project_role(editor, settling)": has_project_role(db, me, project, "project_editor", settling=True),
        "workflows._require_project_editor(settling)": _allowed(
            lambda: workflows._require_project_editor(db, "me", project, settling=True)),
    }


@pytest.mark.parametrize("caller", CALLERS)
def test_every_entry_point_agrees_with_the_rule(make_db, caller):
    (role, owns, grant), (may_see, may_edit) = CALLERS[caller]
    db = make_db()
    me, member, project = _world(db, role, owns, grant)

    seen = _seen(db, me, member, project)
    edited = {**_edited(db, me, project), **_settled(db, me, project)}

    assert seen == {name: may_see for name in seen}
    assert edited == {name: may_edit for name in edited}


@pytest.mark.parametrize("caller", CALLERS)
def test_a_project_being_deleted_is_read_only_to_everyone(make_db, caller):
    """Everyone who could see it still can; nobody edits it, admins included, but
    whoever could edit it may still merge or close its removal pull requests."""
    (role, owns, grant), (may_see, may_edit) = CALLERS[caller]
    db = make_db()
    me, member, project = _world(db, role, owns, grant, pr_state=PROJECT_DELETING)

    seen = _seen(db, me, member, project)
    edited = _edited(db, me, project)
    settled = _settled(db, me, project)

    assert seen == {name: may_see for name in seen}
    assert edited == {name: False for name in edited}
    assert settled == {name: may_edit for name in settled}
    assert project_role(db, me, project) == ("project_viewer" if may_see else None)


def _two_alphas(db, caller_role, caller_is_ann):
    ann = Account(github_user="ann", github_email="a@example.com", account_type="free")
    bob = Account(github_user="bob", github_email="b@example.com", account_type="free")
    accounts = [ann, bob]
    me = ann if caller_is_ann else Account(github_user="me", github_email="m@example.com", account_type="free")
    if not caller_is_ann:
        accounts.append(me)
    db.add_all(accounts)
    db.flush()
    for account in accounts:
        db.add(WorkspaceMember(user_id=account.user_id,
                               workspace_role=caller_role if account is me else "member"))
    anns = Project(project_name="alpha", project_code="ANN", user_id=ann.user_id)
    bobs = Project(project_name="alpha", project_code="BOB", user_id=bob.user_id)
    db.add_all([anns, bobs])
    db.commit()
    return me, db.query(WorkspaceMember).filter_by(user_id=me.user_id).first(), anns, bobs


def _both_lookups(db, me, member):
    results = []
    for lookup in (lambda: workflows._find_project_by_name(db, me.github_user, "alpha"),
                   lambda: projects._find_project_by_name(db, "alpha", member)):
        try:
            results.append(lookup())
        except HTTPException as exc:
            results.append(exc.status_code)
    return results


@pytest.mark.parametrize("caller_role, caller_is_ann, hint, expected", [
    ("member", True, None, "ann"),     # the caller's own project of that name
    ("member", True, "bob", "bob"),    # the page says which one
    ("admin", False, None, 409),       # two others' projects: refuse to guess
    ("admin", False, "bob", "bob"),
    ("member", False, None, 409),
])
def test_same_named_projects_resolve_the_same_way_everywhere(make_db, caller_role, caller_is_ann, hint, expected):
    db = make_db()
    me, member, anns, bobs = _two_alphas(db, caller_role, caller_is_ann)
    token = workflows.set_project_hint(str(bobs.project_id) if hint else None)
    try:
        results = _both_lookups(db, me, member)
    finally:
        workflows.reset_project_hint(token)

    want = {"ann": anns, "bob": bobs}.get(expected, expected)
    assert results == [want, want]
