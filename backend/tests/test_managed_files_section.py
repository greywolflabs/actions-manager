"""
The Managed Files section's projects (#2367), self-hosted rules.

A Managed Files project manages one file kind, fixed at creation. It has its
own project limit (3 on the self-hosted beta) that never counts the Caller or
Reusable Workflow Projects, and they never count it. It has no color. It is
listed only in its section: GET /api/projects/ without a section is the
Projects page, whose list, counts and order never include it. The cloud
limits are in tests/cloud/test_managed_files_limits.py.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import deliveries
from auth import create_auth_session
from database import get_db
from main import app
from models import (
    Account,
    Base,
    Codeowners,
    Project,
    ProjectDisplayOrder,
    ProjectMembership,
    ProjectRepo,
    Repo,
    WorkspaceMember,
)

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

OWNER = "mf-owner"
OTHER = "mf-other"
SELF_HOSTED_LIMIT_MESSAGE = (
    "Self-hosted beta allows up to 3 Managed Files project(s). "
    "You have reached the beta limit for Managed Files projects. "
    "Paid plans are not available during the self-hosted beta."
)
# What a Managed Files entry in GET /api/projects/ adds to the common fields.
MANAGED_FIELDS = {"file_kind", "owner", "repo_count", "file_path", "drifted_repos"}


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    # The write-protection middleware opens its own sessions; point it here too.
    with patch.dict(app.dependency_overrides, {get_db: override_get_db}), \
            patch.object(app.state, "middleware_db_factory", TestingSessionLocal), \
            patch("mode_validation.validate_startup_configuration"), \
            TestClient(app) as test_client:
        yield test_client


def _account(name: str, workspace_role: str | None = "admin") -> SimpleNamespace:
    """An account, and its workspace membership unless *workspace_role* is None."""
    db = TestingSessionLocal()
    try:
        account = Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
        db.add(account)
        db.flush()
        if workspace_role:
            db.add(WorkspaceMember(user_id=account.user_id, workspace_role=workspace_role))
        db.commit()
        return SimpleNamespace(user_id=account.user_id, github_user=name)
    finally:
        db.close()


def _sign_in(client: TestClient, name: str) -> None:
    db = TestingSessionLocal()
    try:
        client.headers["Authorization"] = f"Bearer {create_auth_session(name, db)}"
    finally:
        db.close()


def _payload(name: str, **fields) -> dict:
    return {"project_name": name, "selected_repos": ["octo/api"], "workflows": [], **fields}


def _managed(name: str, **fields) -> dict:
    """A Managed Files project, with no repositories unless *fields* names some: each can be in one."""
    return _payload(name, **{"project_type": "managed_files", "file_kind": "codeowners", "selected_repos": [], **fields})


def _insert_projects(owner, project_type: str, count: int, **fields) -> list:
    """Projects written straight to the database, so a test can start at a limit."""
    db = TestingSessionLocal()
    try:
        rows = [
            Project(project_name=f"{project_type}-{i}", user_id=owner.user_id, project_type=project_type, **fields)
            for i in range(count)
        ]
        db.add_all(rows)
        db.commit()
        return [row.project_id for row in rows]
    finally:
        db.close()


@pytest.fixture
def owner(client):
    account = _account(OWNER)
    _sign_in(client, OWNER)
    return account


# ---------------------------------------------------------------------------
# Creating one
# ---------------------------------------------------------------------------

def test_a_managed_files_project_keeps_its_kind(client, owner):
    response = client.post("/api/projects/", json=_managed("Code owners"))

    assert response.status_code == 200
    project = client.get("/api/projects/Code owners", params={"github_user": OWNER}).json()
    assert project["project_type"] == "managed_files"
    assert project["file_kind"] == "codeowners"
    assert project["project_color"] is None


@pytest.mark.parametrize("fields, detail", [
    ({"project_type": "managed_files"}, "file_kind must be one of: codeowners"),
    ({"project_type": "managed_files", "file_kind": "dependabot"}, "file_kind must be one of: codeowners"),
    ({"project_type": "standard", "file_kind": "codeowners"}, "file_kind is only for Managed Files projects"),
    ({"project_type": "rwx", "file_kind": "codeowners"}, "file_kind is only for Managed Files projects"),
    ({"project_type": "workflows"}, "project_type must be one of: standard, rwx, managed_files"),
])
def test_type_and_kind_are_checked_before_anything_is_created(client, owner, fields, detail):
    response = client.post("/api/projects/", json=_payload("Refused", **fields))

    assert response.status_code == 422
    assert response.json()["detail"] == detail
    db = TestingSessionLocal()
    try:
        assert db.query(Project).count() == 0
    finally:
        db.close()


def test_the_fourth_managed_files_project_is_refused(client, owner):
    for n in range(3):
        assert client.post("/api/projects/", json=_managed(f"Owners {n}")).status_code == 200

    response = client.post("/api/projects/", json=_managed("Owners 3"))

    assert response.status_code == 403
    assert response.json()["detail"] == SELF_HOSTED_LIMIT_MESSAGE


def test_managed_files_projects_never_count_toward_the_workflow_project_limits(client, owner):
    _insert_projects(owner, "managed_files", 3, file_kind="codeowners")

    for n in range(4):
        assert client.post("/api/projects/", json=_payload(f"Caller {n}")).status_code == 200
    for n in range(2):
        assert client.post("/api/projects/", json=_payload(f"Reusable {n}", project_type="rwx")).status_code == 200
    refused = client.post("/api/projects/", json=_payload("Caller 4"))

    assert refused.status_code == 403
    assert "4 Caller Workflow Project(s)" in refused.json()["detail"]


def test_workflow_projects_never_count_toward_the_managed_files_limit(client, owner):
    _insert_projects(owner, "standard", 4)
    _insert_projects(owner, "rwx", 2)

    assert client.post("/api/projects/", json=_managed("Owners")).status_code == 200


def test_another_owners_managed_files_projects_do_not_count(client, owner):
    _insert_projects(_account(OTHER), "managed_files", 3, file_kind="codeowners")

    assert client.post("/api/projects/", json=_managed("Owners")).status_code == 200


# ---------------------------------------------------------------------------
# No color
# ---------------------------------------------------------------------------

NO_COLOR = "Managed Files projects have no color; project_color must be null."


def test_a_color_is_refused_on_create(client, owner):
    response = client.post("/api/projects/", json=_managed("Owners", project_color="slate"))

    assert response.status_code == 422
    assert response.json()["detail"] == NO_COLOR


def test_a_color_is_refused_on_update(client, owner):
    project_id = client.post("/api/projects/", json=_managed("Owners")).json()["project_id"]

    response = client.put(f"/api/projects/{project_id}/", json=_managed("Owners", project_color="blue"))

    assert response.status_code == 422
    assert response.json()["detail"] == NO_COLOR


def test_a_color_is_refused_on_the_color_patch_and_null_is_kept(client, owner):
    project_id = client.post("/api/projects/", json=_managed("Owners")).json()["project_id"]

    refused = client.patch(f"/api/projects/{project_id}/project-color", json={"project_color": "purple"})
    cleared = client.patch(f"/api/projects/{project_id}/project-color", json={"project_color": None})

    assert refused.status_code == 422
    assert refused.json()["detail"] == NO_COLOR
    assert cleared.status_code == 200
    assert cleared.json()["project_color"] is None


# ---------------------------------------------------------------------------
# Listed only in their section
# ---------------------------------------------------------------------------

def _names(response) -> list:
    assert response.status_code == 200
    return [entry["project_name"] for entry in response.json()]


def test_the_projects_page_never_lists_managed_files_projects(client, owner):
    client.post("/api/projects/", json=_payload("Caller"))
    client.post("/api/projects/", json=_payload("Reusable", project_type="rwx"))
    client.post("/api/projects/", json=_managed("Owners"))

    assert sorted(_names(client.get("/api/projects/"))) == ["Caller", "Reusable"]
    assert sorted(_names(client.get("/api/projects/", params={"section": "workflows"}))) == ["Caller", "Reusable"]
    assert _names(client.get("/api/projects/", params={"section": "managed_files"})) == ["Owners"]
    assert sorted(_names(client.get("/api/projects/", params={"section": "all"}))) == ["Caller", "Owners", "Reusable"]


def test_an_unknown_section_is_refused(client, owner):
    assert client.get("/api/projects/", params={"section": "dependabot"}).status_code == 422


def test_workflow_project_entries_keep_their_shape(client, owner):
    client.post("/api/projects/", json=_payload("Caller"))
    client.post("/api/projects/", json=_managed("Owners"))

    entries = {entry["project_name"]: entry for entry in client.get("/api/projects/", params={"section": "all"}).json()}

    assert not MANAGED_FIELDS & set(entries["Caller"])
    assert set(entries["Owners"]) == set(entries["Caller"]) | MANAGED_FIELDS


def test_a_managed_files_entry_carries_what_its_card_shows(client, owner):
    project_id = client.post(
        "/api/projects/", json=_managed("Owners", selected_repos=["octo/api", "octo/web", "octo/docs"]),
    ).json()["project_id"]
    db = TestingSessionLocal()
    try:
        codeowners = Codeowners(project_id=project_id, content="* @octo/team", file_path="CODEOWNERS")
        db.add(codeowners)
        db.flush()
        for name in ("octo/web", "octo/api"):
            repo_id = db.query(Repo.repo_id).filter(Repo.repo_name == name).scalar()
            for branch in ("main", "release"):
                deliveries.record_check(
                    db, deliveries.CODEOWNERS, codeowners.id, repo_id, branch, has_drift=True, deleted=False,
                    github_sha="abc", content_hash="hash", now=datetime.now(timezone.utc),
                )
        db.commit()
    finally:
        db.close()

    [entry] = client.get("/api/projects/", params={"section": "managed_files"}).json()

    assert {field: entry[field] for field in MANAGED_FIELDS} == {
        "file_kind": "codeowners",
        "owner": OWNER,
        "repo_count": 3,
        "file_path": "CODEOWNERS",
        "drifted_repos": ["octo/api", "octo/web"],
    }


def test_a_repository_that_left_the_project_is_not_named_as_drifted(client, owner):
    project_id = client.post(
        "/api/projects/", json=_managed("Owners", selected_repos=["octo/api", "octo/web"]),
    ).json()["project_id"]
    db = TestingSessionLocal()
    try:
        codeowners = Codeowners(project_id=project_id, content="* @octo/team", file_path="CODEOWNERS")
        db.add(codeowners)
        db.flush()
        for name in ("octo/api", "octo/web"):
            repo_id = db.query(Repo.repo_id).filter(Repo.repo_name == name).scalar()
            deliveries.record_check(
                db, deliveries.CODEOWNERS, codeowners.id, repo_id, "main", has_drift=True, deleted=False,
                github_sha="abc", content_hash="hash", now=datetime.now(timezone.utc),
            )
        db.commit()
    finally:
        db.close()

    # No later check visits octo/web again, so its drift row stays behind.
    client.put(f"/api/projects/{project_id}/", json=_managed("Owners", selected_repos=["octo/api"]))
    [entry] = client.get("/api/projects/", params={"section": "managed_files"}).json()

    assert entry["drifted_repos"] == ["octo/api"]


def _synced_with_file(project_id: int, content: str | None) -> None:
    db = TestingSessionLocal()
    try:
        if content is not None:
            db.add(Codeowners(project_id=project_id, content=content, file_path="CODEOWNERS"))
        db.get(Project, project_id).pr_state = "synced"
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("content, state", [("* @octo/team", "draft"), (None, "synced")])
def test_saving_a_synced_projects_repositories_makes_it_a_draft_when_it_has_its_file(client, owner, content, state):
    project_id = client.post("/api/projects/", json=_managed("Owners", selected_repos=["octo/api"])).json()["project_id"]
    _synced_with_file(project_id, content)

    # A new repository or branch is reached only by a campaign, as for a workflow project's save.
    saved = client.put(f"/api/projects/{project_id}/", json=_managed("Owners", selected_repos=["octo/api", "octo/web"]))

    assert (saved.status_code, saved.json()["pr_state"]) == (200, state)


def test_a_managed_files_entry_before_the_file_is_saved(client, owner):
    client.post("/api/projects/", json=_managed("Owners", selected_repos=[]))

    [entry] = client.get("/api/projects/", params={"section": "managed_files"}).json()

    assert entry["repo_count"] == 0
    assert entry["file_path"] is None
    assert entry["drifted_repos"] == []


def test_the_section_lists_in_creation_order_and_saves_no_order(client, owner):
    base = datetime(2026, 1, 1)
    ids = _insert_projects(owner, "managed_files", 2, file_kind="codeowners")
    db = TestingSessionLocal()
    try:
        # The last one created is the most recently edited, so the Projects
        # page's updated_at order would put it first.
        db.get(Project, ids[0]).created_at, db.get(Project, ids[0]).updated_at = base, base
        db.get(Project, ids[1]).created_at, db.get(Project, ids[1]).updated_at = (
            base + timedelta(days=1), base + timedelta(days=2),
        )
        db.commit()
    finally:
        db.close()

    listed = client.get("/api/projects/", params={"section": "managed_files"}).json()
    client.get("/api/projects/", params={"section": "all"})
    client.get("/api/projects/")

    assert [entry["project_id"] for entry in listed] == ids
    db = TestingSessionLocal()
    try:
        assert db.query(ProjectDisplayOrder).filter(ProjectDisplayOrder.project_id.in_(ids)).count() == 0
    finally:
        db.close()


def test_a_read_only_member_sees_only_their_granted_managed_files_projects(client, owner):
    granted = [
        client.post("/api/projects/", json=_managed("Granted owners")).json()["project_id"],
        client.post("/api/projects/", json=_payload("Granted caller")).json()["project_id"],
    ]
    client.post("/api/projects/", json=_managed("Other owners"))
    reader = _account(OTHER, workspace_role="read_only")
    db = TestingSessionLocal()
    try:
        db.add_all([
            ProjectMembership(user_id=reader.user_id, project_id=project_id, project_role="project_viewer")
            for project_id in granted
        ])
        db.commit()
    finally:
        db.close()
    _sign_in(client, OTHER)

    assert _names(client.get("/api/projects/", params={"section": "managed_files"})) == ["Granted owners"]
    assert _names(client.get("/api/projects/")) == ["Granted caller"]


def test_reordering_the_projects_page_leaves_managed_files_projects_out(client, owner):
    caller_id = client.post("/api/projects/", json=_payload("Caller")).json()["project_id"]
    managed_id = client.post("/api/projects/", json=_managed("Owners")).json()["project_id"]

    saved = client.put("/api/projects/order", json={"project_ids": [caller_id]})
    refused = client.put("/api/projects/order", json={"project_ids": [caller_id, managed_id]})

    assert saved.status_code == 200
    assert refused.status_code == 400
    assert str(managed_id) in refused.json()["detail"]


def test_the_backup_export_carries_the_file_kind(client, owner):
    managed_id = client.post("/api/projects/", json=_managed("Owners")).json()["project_id"]
    caller_id = client.post("/api/projects/", json=_payload("Caller")).json()["project_id"]

    managed = client.get(f"/api/projects/{managed_id}/backup-export").json()["project"]
    caller = client.get(f"/api/projects/{caller_id}/backup-export").json()["project"]

    assert (managed["project_type"], managed["file_kind"]) == ("managed_files", "codeowners")
    assert caller["file_kind"] is None


# ---------------------------------------------------------------------------
# GET /api/managed-files
# ---------------------------------------------------------------------------

def test_the_section_reports_kinds_and_the_limit_the_server_enforces(client, owner):
    _insert_projects(_account(OTHER), "managed_files", 1, file_kind="codeowners")
    client.post("/api/projects/", json=_managed("Owners 0"))
    client.post("/api/projects/", json=_payload("Caller"))

    section = client.get("/api/managed-files").json()

    assert section == {
        "kinds": [
            {"kind": "codeowners", "label": "CODEOWNERS", "paths": [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]},
        ],
        "owned_count": 1,
        "limit": 3,
        "limit_message": "Beta limit reached (3/3 Managed Files projects).",
    }
    for n in range(1, section["limit"]):
        assert client.post("/api/projects/", json=_managed(f"Owners {n}")).status_code == 200
    assert client.get("/api/managed-files").json()["owned_count"] == section["limit"]
    assert client.post("/api/projects/", json=_managed("One too many")).status_code == 403


def test_the_section_needs_a_session(client):
    assert client.get("/api/managed-files").status_code == 401
