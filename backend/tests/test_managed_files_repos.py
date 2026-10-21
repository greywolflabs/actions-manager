"""
One project per repository per file kind, and what a Managed Files project can hold (#2367).

A repository may be in at most one Managed Files project of a kind, across the
whole instance, enforced by the server on every path that adds one: create and
update. The error names the holding project only to a caller who can see it.
The check runs under database.serialize_writers, so two saves racing for the
same repository cannot both pass it.

A Managed Files project holds its one file and nothing else:
file_kinds.project_accepts refuses workflows, custom files, linked reusable
workflows and a validation repository at every entry point that adds them.

The picker learns which repositories are taken from
POST /api/managed-files/{kind}/taken-repos, which reads the database only.
"""
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import authorization
import file_kinds
import projects
from auth import user_tokens
from database import serialize_writers
from models import Account, Base, CustomFile, Project, ProjectRepo, Repo
from projects import MANAGED_FILES_REPOS_LOCK_ID, _process_project_repos
from tests.test_managed_files_section import (  # noqa: F401 - fixtures used by name
    OTHER,
    OWNER,
    TestingSessionLocal,
    _account,
    _managed,
    _payload,
    _sign_in,
    client,
    schema,
)

HELD = "A repository can be in one CODEOWNERS project."


def _refusal(holds: str) -> str:
    return f"A Managed Files project manages only its CODEOWNERS file, so it cannot hold {holds}."


@pytest.fixture
def owner(client):
    account = _account(OWNER)
    _sign_in(client, OWNER)
    return account


@pytest.fixture
def dependabot():
    """A second kind, defined only for these tests."""
    kind = file_kinds.FileKind("Dependabot", (".github/dependabot.yml",), lambda _db, _ids: {})
    with patch.dict(file_kinds.FILE_KINDS, {"dependabot": kind}):
        yield


def _create(client, name: str, repos: list, **fields):
    return client.post("/api/projects/", json=_managed(name, selected_repos=repos, **fields))


def _repos(client, name: str) -> list:
    return sorted(client.get(f"/api/projects/{name}", params={"github_user": OWNER}).json()["selected_repos"])


def _held_directly(repo_name: str, owner, project_name: str) -> None:
    """*repo_name* in a CODEOWNERS project written straight to the database, past the rule."""
    db = TestingSessionLocal()
    try:
        project = Project(project_name=project_name, user_id=owner.user_id,
                          project_type="managed_files", file_kind="codeowners")
        repo = db.query(Repo).filter_by(repo_name=repo_name).first() or Repo(repo_name=repo_name)
        db.add_all([project, repo])
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.commit()
    finally:
        db.close()


def _held_by_hidden_project(repo_name: str) -> None:
    """*repo_name* in a CODEOWNERS project of OTHER's that nobody else can see.

    No account here has a workspace row, so no one but OTHER can see it (see
    authorization.project_role).
    """
    _held_directly(repo_name, _account(OTHER, workspace_role=None), "Hidden")


# ---------------------------------------------------------------------------
# One project per repository per kind
# ---------------------------------------------------------------------------

def test_create_is_refused_a_repository_another_project_of_the_kind_holds(client, owner):
    assert _create(client, "Payments", ["octo/api"]).status_code == 200

    refused = _create(client, "Web", ["octo/web", "octo/api"])

    assert refused.status_code == 409
    assert refused.json()["detail"] == f"{HELD} octo/api is used by 'Payments'."
    listed = client.get("/api/projects/", params={"section": "managed_files"}).json()
    assert [entry["project_name"] for entry in listed] == ["Payments"]


def test_update_is_refused_a_held_repository_and_keeps_its_repositories(client, owner):
    _create(client, "Payments", ["octo/api"])
    web_id = _create(client, "Web", ["octo/web"]).json()["project_id"]

    refused = client.put(f"/api/projects/{web_id}/", json=_managed("Web", selected_repos=["octo/web", "octo/api"]))

    assert refused.status_code == 409
    assert refused.json()["detail"] == f"{HELD} octo/api is used by 'Payments'."
    assert _repos(client, "Web") == ["octo/web"]


def test_the_error_does_not_name_a_project_the_caller_cannot_see(client):
    _held_by_hidden_project("octo/api")
    _account(OWNER, workspace_role=None)
    _sign_in(client, OWNER)

    refused = _create(client, "Mine", ["octo/api"])

    assert refused.status_code == 409
    assert refused.json()["detail"] == f"{HELD} octo/api is used by another CODEOWNERS project."


def test_repository_names_compare_case_insensitively(client, owner):
    _create(client, "Payments", ["Octo/API"])

    refused = _create(client, "Web", ["octo/api"])

    assert refused.status_code == 409
    assert refused.json()["detail"] == f"{HELD} octo/api is used by 'Payments'."


def test_a_save_that_adds_no_repository_is_never_refused(client, owner):
    payments_id = _create(client, "Payments", ["octo/api"]).json()["project_id"]
    # Two projects sharing a repository from before the rule: saving either
    # one unchanged must still work, or neither could be fixed.
    _held_directly("octo/api", owner, "Legacy")

    saved = client.put(f"/api/projects/{payments_id}/", json=_managed("Payments", selected_repos=["octo/api"]))

    assert saved.status_code == 200


def test_a_project_never_takes_a_repository_from_itself(client, owner):
    payments_id = _create(client, "Payments", ["octo/api"]).json()["project_id"]

    saved = client.put(f"/api/projects/{payments_id}/", json=_managed("Payments", selected_repos=["Octo/API"]))

    assert saved.status_code == 200
    assert _repos(client, "Payments") == ["Octo/API"]


def test_removing_a_repository_frees_it(client, owner):
    payments_id = _create(client, "Payments", ["octo/api"]).json()["project_id"]
    client.put(f"/api/projects/{payments_id}/", json=_managed("Payments", selected_repos=[]))

    assert _create(client, "Web", ["octo/api"]).status_code == 200


def test_the_rule_is_per_kind(client, owner, dependabot):
    _create(client, "Payments", ["octo/api"])

    other_kind = _create(client, "Updates", ["octo/api"], file_kind="dependabot")
    same_kind = _create(client, "More updates", ["octo/api"], file_kind="dependabot")

    assert other_kind.status_code == 200
    assert same_kind.status_code == 409
    assert same_kind.json()["detail"] == (
        "A repository can be in one Dependabot project. octo/api is used by 'Updates'."
    )


def test_workflow_projects_still_share_repositories_with_anything(client, owner):
    assert _create(client, "Payments", ["octo/api"]).status_code == 200
    for name in ("Caller one", "Caller two"):
        assert client.post("/api/projects/", json=_payload(name, selected_repos=["octo/api"])).status_code == 200


# ---------------------------------------------------------------------------
# Two saves racing for one repository
# ---------------------------------------------------------------------------

def _race_database(tmp_path):
    """A file database with Payments and Web, two empty CODEOWNERS projects, and octo/api."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 5, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    make_session = sessionmaker(bind=engine, autoflush=False)
    with make_session() as setup:
        owner = Account(github_user=OWNER, github_email="o@example.com", account_type="free")
        setup.add_all([owner, Repo(repo_name="octo/api")])
        setup.flush()
        for name in ("Payments", "Web"):
            setup.add(Project(project_name=name, user_id=owner.user_id,
                              project_type="managed_files", file_kind="codeowners"))
        setup.commit()
    return engine, make_session


def _save(make_session, project_name: str, outcome: dict) -> None:
    """Save *project_name* with octo/api in its own session, recording the result."""
    with make_session() as db:
        project = db.query(Project).filter_by(project_name=project_name).one()
        try:
            _process_project_repos(db, project, ["octo/api"], db.query(Account).one())
            db.commit()
            outcome[project_name] = "saved"
        except HTTPException as exc:
            db.rollback()
            outcome[project_name] = exc.status_code


def test_a_second_save_waits_for_the_first_and_then_sees_its_repository(tmp_path):
    engine, make_session = _race_database(tmp_path)
    first = make_session()
    _process_project_repos(first, first.query(Project).filter_by(project_name="Payments").one(),
                           ["octo/api"], first.query(Account).one())
    # The second save signals as it reaches the lock. A check that ran before
    # the lock would already have read the database without the first save.
    reached_lock = threading.Event()
    take_lock = projects.serialize_writers

    def signalling_lock(db, lock_id):
        if threading.current_thread() is not threading.main_thread():
            reached_lock.set()
        take_lock(db, lock_id)

    outcome = {}
    with patch.object(projects, "serialize_writers", signalling_lock):
        second = threading.Thread(target=_save, args=(make_session, "Web", outcome))
        second.start()
        assert reached_lock.wait(5)
        first.commit()
        first.close()
        second.join(10)

    with make_session() as db:
        web_repos = db.query(ProjectRepo).join(Project).filter(Project.project_name == "Web").count()
    engine.dispose()
    assert outcome == {"Web": 409}
    assert web_repos == 0


def test_postgresql_takes_a_transaction_scoped_advisory_lock():
    executed = []
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=lambda statement, params: executed.append((str(statement), params)),
    )

    serialize_writers(session, MANAGED_FILES_REPOS_LOCK_ID)

    assert executed == [("SELECT pg_advisory_xact_lock(:lock_id)", {"lock_id": MANAGED_FILES_REPOS_LOCK_ID})]


def test_the_first_member_decision_takes_the_same_lock():
    with patch.object(authorization, "serialize_writers") as lock:
        authorization.lock_first_member_decision("session")

    lock.assert_called_once_with("session", authorization.FIRST_MEMBER_LOCK_ID)


# ---------------------------------------------------------------------------
# What a Managed Files project can hold
# ---------------------------------------------------------------------------

WORKFLOW = {"name": "ci", "content": "on: push\njobs: {}\n"}


@pytest.mark.parametrize("fields, holds", [
    ({"workflows": [WORKFLOW]}, "workflows"),
    ({"rxworkflows": [WORKFLOW]}, "workflows"),
    ({"validation_repo": "octo/validation"}, "a validation repository"),
    ({"preflight_required": True}, "a validation repository"),
])
def test_create_refuses_what_the_project_cannot_hold(client, owner, fields, holds):
    refused = _create(client, "Owners", ["octo/api"], **fields)

    assert refused.status_code == 400
    assert refused.json()["detail"] == _refusal(holds)


@pytest.mark.parametrize("fields, holds", [
    ({"workflows": [WORKFLOW]}, "workflows"),
    ({"validation_repo": "octo/validation"}, "a validation repository"),
])
def test_update_refuses_what_the_project_cannot_hold(client, owner, fields, holds):
    project_id = _create(client, "Owners", ["octo/api"]).json()["project_id"]

    refused = client.put(f"/api/projects/{project_id}/", json=_managed("Owners", **fields))

    assert refused.status_code == 400
    assert refused.json()["detail"] == _refusal(holds)


def test_saving_workflows_is_refused(client, owner):
    _create(client, "Owners", ["octo/api"])

    refused = client.post("/api/save-workflows", json={"project_name": "Owners", "workflows": [WORKFLOW]})

    assert refused.status_code == 400
    assert refused.json()["detail"] == _refusal("workflows")


def test_importing_workflows_is_refused(client, owner):
    project_id = _create(client, "Owners", ["octo/api"]).json()["project_id"]
    item = {"source_repo": "octo/api", "source_branch": "main", "workflow_path": ".github/workflows/ci.yml"}

    with patch.dict(user_tokens, {OWNER: "token"}):
        refused = client.post(
            f"/api/projects/{project_id}/workflow-import", json={"project_name": "Owners", "workflows": [item]},
        )

    assert refused.status_code == 400
    assert refused.json()["detail"] == _refusal("workflows")


def test_linking_a_reusable_workflow_is_refused(client, owner):
    _create(client, "Owners", ["octo/api"])

    refused = client.post(
        "/api/projects/Owners/linked-reusable-workflows", json={"workflow_id": 1, "rwx_project_id": 1},
    )

    assert refused.status_code == 400
    assert refused.json()["detail"] == _refusal("linked reusable workflows")


def test_running_preflight_is_refused(client, owner):
    _create(client, "Owners", ["octo/api"])

    with patch.dict(user_tokens, {OWNER: "token"}):
        refused = client.post("/api/run-preflight-validation", json={"project_name": "Owners"})

    assert refused.status_code == 400
    assert refused.json()["detail"] == _refusal("a validation repository")


def test_custom_files_are_refused_on_create_update_and_restore(client, owner):
    project_id = _create(client, "Owners", ["octo/api"]).json()["project_id"]
    db = TestingSessionLocal()
    try:
        # As if one were left from before the rule existed.
        custom_file = CustomFile(project_id=project_id, file_path="notes.txt", file_content="x")
        db.add(custom_file)
        db.commit()
        file_id = custom_file.id
    finally:
        db.close()
    base = f"/api/projects/{project_id}/custom-files"

    with patch.dict(user_tokens, {OWNER: "token"}):
        responses = [
            client.post(base, json={"file_path": "notes.md", "file_content": "x"}),
            client.put(f"{base}/{file_id}", json={"file_path": "moved.txt"}),
            client.post(f"{base}/{file_id}/restore"),
        ]

    assert [(r.status_code, r.json()["detail"]) for r in responses] == [(400, _refusal("custom files"))] * 3


def test_workflow_projects_still_hold_workflows_and_custom_files(client, owner):
    created = client.post("/api/projects/", json=_payload("Caller", workflows=[WORKFLOW]))
    with patch.dict(user_tokens, {OWNER: "token"}):
        custom_file = client.post(
            f"/api/projects/{created.json()['project_id']}/custom-files", json={"file_path": "notes.md"},
        )

    assert created.status_code == 200
    assert custom_file.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/managed-files/{kind}/taken-repos
# ---------------------------------------------------------------------------

def _taken(client, names: list, kind: str = "codeowners", **fields):
    return client.post(f"/api/managed-files/{kind}/taken-repos", json={"repo_names": names, **fields})


def test_reports_only_the_names_asked_that_a_project_holds(client, owner):
    payments_id = _create(client, "Payments", ["octo/api", "octo/billing"]).json()["project_id"]

    taken = _taken(client, ["OCTO/API", "octo/web"])

    assert taken.status_code == 200
    assert taken.json() == {"taken": {"OCTO/API": {"project_id": payments_id, "project_name": "Payments"}}}


def test_a_holder_the_caller_cannot_see_has_no_id_or_name(client):
    _held_by_hidden_project("octo/api")
    _account(OWNER, workspace_role=None)
    _sign_in(client, OWNER)

    assert _taken(client, ["octo/api"]).json() == {
        "taken": {"octo/api": {"project_id": None, "project_name": None}},
    }


def test_the_project_being_edited_does_not_take_its_own_repositories(client, owner):
    payments_id = _create(client, "Payments", ["octo/api"]).json()["project_id"]

    assert _taken(client, ["octo/api"], exclude_project_id=payments_id).json() == {"taken": {}}


def test_excluding_a_project_the_caller_cannot_see_is_refused(client):
    _held_by_hidden_project("octo/api")
    _account(OWNER, workspace_role=None)
    _sign_in(client, OWNER)
    db = TestingSessionLocal()
    try:
        hidden_id = db.query(Project.project_id).filter_by(project_name="Hidden").scalar()
    finally:
        db.close()

    assert _taken(client, ["octo/api"], exclude_project_id=hidden_id).status_code == 404


def test_answers_per_kind(client, owner, dependabot):
    _create(client, "Payments", ["octo/api"])

    assert _taken(client, ["octo/api"], kind="dependabot").json() == {"taken": {}}


@pytest.mark.parametrize("kind, names, status", [
    ("renovate", ["octo/api"], 404),
    ("codeowners", ["not a repo"], 422),
    ("codeowners", ["octo/api/extra"], 422),
    ("codeowners", [f"octo/r{n}" for n in range(1001)], 422),
])
def test_refuses_an_unknown_kind_or_bad_names(client, owner, kind, names, status):
    assert _taken(client, names, kind=kind).status_code == status


def test_needs_a_session(client):
    assert _taken(client, ["octo/api"]).status_code == 401


def test_a_read_only_member_cannot_ask(client, owner):
    """Only callers who can already see every project in the workspace may ask.

    Members see the whole workspace, and read_only members, who see only their
    grants, are refused every non-safe method. So the answer never tells a
    caller about a project they could not already list.
    """
    _create(client, "Payments", ["octo/api"])
    _account(OTHER, workspace_role="read_only")
    _sign_in(client, OTHER)

    assert _taken(client, ["octo/api"]).status_code == 403
