"""
Tests for PATCH /projects/{id}/project-name — rename-only endpoint.

Verifies that project_name persists and project_code remains unchanged.
Regression coverage for GitHub issue #1570.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (  # noqa: E402
    Account, Base, Project, ProjectMembership, WorkspaceMember,
)
from main import app  # noqa: E402
from projects import get_db  # noqa: E402
from auth import create_auth_session  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _signed_in(github_user):
    """Headers for a real server-side session as ``github_user`` (#2241)."""
    db = TestingSessionLocal()
    try:
        token = create_auth_session(github_user, db)
    finally:
        db.close()
    return {"Authorization": f"Bearer {token}"}


class TestProjectRename:
    @pytest.fixture(autouse=True)
    def setup_database(self):
        app.dependency_overrides[get_db] = override_get_db
        Base.metadata.create_all(bind=engine)

        db = TestingSessionLocal()
        try:
            db.add(Account(github_user="renameuser", github_email="r@example.com", account_type="free"))
            db.commit()
        finally:
            db.close()

        yield

        Base.metadata.drop_all(bind=engine)
        if get_db in app.dependency_overrides:
            del app.dependency_overrides[get_db]

    def setup_method(self):
        self.client = TestClient(app)

    def _create_project(self, name: str = "Original Name") -> dict:
        payload = {
            "github_user": "renameuser",
            "project_name": name,
            "selected_repos": ["owner/repo1"],
            "workflows": [],
            "branch_regex": "",
            "branch_option": "default",
            "branch_max_age_days": 30,
            "reusable_workflows_enabled": False,
            "use_prefix": False,
        }
        db = TestingSessionLocal()
        try:
            token = create_auth_session("renameuser", db)
        finally:
            db.close()
        resp = self.client.post("/api/projects/", json=payload, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_rename_succeeds_and_project_code_unchanged(self):
        created = self._create_project("My Project")
        project_id = created["project_id"]
        original_code = created["project_code"]

        resp = self.client.patch(
            f"/api/projects/{project_id}/project-name",
            json={"github_user": "renameuser", "project_name": "Renamed Project"},
            headers=_signed_in("renameuser"),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["project_name"] == "Renamed Project"
        assert data["project_code"] == original_code

        db = TestingSessionLocal()
        try:
            project = db.query(Project).filter(Project.project_id == project_id).first()
            assert project is not None
            assert project.project_name == "Renamed Project"
            assert project.project_code == original_code
        finally:
            db.close()

    def test_rename_trims_whitespace(self):
        created = self._create_project()
        project_id = created["project_id"]

        resp = self.client.patch(
            f"/api/projects/{project_id}/project-name",
            json={"github_user": "renameuser", "project_name": "  Trimmed Name  "},
            headers=_signed_in("renameuser"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["project_name"] == "Trimmed Name"

    def test_rename_empty_name_rejected(self):
        created = self._create_project()
        project_id = created["project_id"]

        resp = self.client.patch(
            f"/api/projects/{project_id}/project-name",
            json={"github_user": "renameuser", "project_name": "   "},
            headers=_signed_in("renameuser"),
        )
        assert resp.status_code == 422, resp.text

    def test_rename_nonexistent_project_returns_404(self):
        resp = self.client.patch(
            "/api/projects/99999/project-name",
            json={"github_user": "renameuser", "project_name": "New Name"},
            headers=_signed_in("renameuser"),
        )
        assert resp.status_code == 404, resp.text

    # --- project_code is immutable, including against a client that tries ---

    def test_update_ignores_a_client_supplied_project_code(self):
        """project_code is not on ProjectSchema, so a client that sends one is ignored.

        Pydantic v2 defaults to extra="ignore" and the backend sets no
        model_config, so the key is dropped during validation and never reaches
        the ORM. Nothing raises — the request succeeds and the code is simply
        unchanged.
        """
        created = self._create_project("Immutable Code Project")
        project_id = created["project_id"]
        original_code = created["project_code"]

        resp = self.client.put(
            f"/api/projects/{project_id}/",
            json={
                "github_user": "renameuser",
                "project_name": "Immutable Code Project",
                "project_code": "HACKED",
                "selected_repos": ["owner/repo1"],
                "workflows": [],
                "branch_regex": "",
                "branch_option": "default",
                "branch_max_age_days": 30,
                "reusable_workflows_enabled": False,
                "use_prefix": False,
            },
            headers=_signed_in("renameuser"),
        )
        assert resp.status_code == 200, resp.text

        db = TestingSessionLocal()
        try:
            project = db.query(Project).filter(Project.project_id == project_id).first()
            assert project.project_code == original_code
            assert project.project_code != "HACKED"
        finally:
            db.close()

    def test_update_ignores_custom_project_key(self):
        """custom_project_key IS accepted by the schema, but only read on create.

        The frontend sends it on every save (api/handlers.ts), so this is a real
        payload rather than a hypothetical one. On an update it must not
        re-key the project.
        """
        created = self._create_project("Custom Key Project")
        project_id = created["project_id"]
        original_code = created["project_code"]

        resp = self.client.put(
            f"/api/projects/{project_id}/",
            json={
                "github_user": "renameuser",
                "project_name": "Custom Key Project",
                "custom_project_key": "NEWKEY",
                "selected_repos": ["owner/repo1"],
                "workflows": [],
                "branch_regex": "",
                "branch_option": "default",
                "branch_max_age_days": 30,
                "reusable_workflows_enabled": False,
                "use_prefix": False,
            },
            headers=_signed_in("renameuser"),
        )
        assert resp.status_code == 200, resp.text

        db = TestingSessionLocal()
        try:
            project = db.query(Project).filter(Project.project_id == project_id).first()
            assert project.project_code == original_code
            assert project.project_code != "NEWKEY"
        finally:
            db.close()

    def test_rename_via_patch_leaves_project_code_alone_with_a_code_in_the_body(self):
        """ProjectNameUpdateSchema has no project_code either."""
        created = self._create_project("Patch Code Project")
        project_id = created["project_id"]
        original_code = created["project_code"]

        resp = self.client.patch(
            f"/api/projects/{project_id}/project-name",
            json={
                "github_user": "renameuser",
                "project_name": "Patched Name",
                "project_code": "HACKED",
            },
            headers=_signed_in("renameuser"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["project_code"] == original_code


class TestProjectRenameAuthorization:
    """Renaming is a write, so it needs at least project_editor.

    _find_project_by_id accepts any ProjectMembership regardless of
    project_role, so without an explicit check a documented read-only
    project_viewer could rename the project.
    """

    @pytest.fixture(autouse=True)
    def setup_database(self):
        app.dependency_overrides[get_db] = override_get_db
        Base.metadata.create_all(bind=engine)

        db = TestingSessionLocal()
        try:
            owner = Account(github_user="rn-owner", github_email="o@example.com", account_type="free")
            viewer = Account(github_user="rn-viewer", github_email="v@example.com", account_type="free")
            editor = Account(github_user="rn-editor", github_email="e@example.com", account_type="free")
            db.add_all([owner, viewer, editor])
            db.commit()
            for acct in (owner, viewer, editor):
                db.refresh(acct)

            project = Project(
                project_name="Authz Project",
                project_code="AUTHZ",
                user_id=owner.user_id,
                branch_option="default",
                project_type="standard",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

            db.add_all([
                WorkspaceMember(user_id=owner.user_id, workspace_role="member"),
                WorkspaceMember(user_id=viewer.user_id, workspace_role="member"),
                WorkspaceMember(user_id=editor.user_id, workspace_role="member"),
                ProjectMembership(user_id=viewer.user_id, project_id=project.project_id,
                                  project_role="project_viewer"),
                ProjectMembership(user_id=editor.user_id, project_id=project.project_id,
                                  project_role="project_editor"),
            ])
            db.commit()
            self.project_id = project.project_id
        finally:
            db.close()

        yield

        Base.metadata.drop_all(bind=engine)
        if get_db in app.dependency_overrides:
            del app.dependency_overrides[get_db]

    def setup_method(self):
        self.client = TestClient(app)

    def _rename(self, caller: str, new_name: str):
        return self.client.patch(
            f"/api/projects/{self.project_id}/project-name",
            json={"github_user": caller, "project_name": new_name},
            headers=_signed_in(caller),
        )

    def _stored_name(self) -> str:
        db = TestingSessionLocal()
        try:
            return db.query(Project).filter(Project.project_id == self.project_id).first().project_name
        finally:
            db.close()

    def test_project_viewer_cannot_rename(self):
        resp = self._rename("rn-viewer", "Viewer Renamed This")
        assert resp.status_code == 403, resp.text
        assert self._stored_name() == "Authz Project", "the rename was applied despite the 403"

    def test_project_editor_can_rename(self):
        resp = self._rename("rn-editor", "Editor Renamed This")
        assert resp.status_code == 200, resp.text
        assert self._stored_name() == "Editor Renamed This"

    def test_owner_can_rename(self):
        resp = self._rename("rn-owner", "Owner Renamed This")
        assert resp.status_code == 200, resp.text
        assert self._stored_name() == "Owner Renamed This"


class TestRenameToANameTheOwnerUses:
    """A project can't be renamed to a name its owner already uses (#2250).

    Everything that resolves a project by name takes the owner's first match,
    so a second project of the same name would make those actions, the drift
    sweep included, act on either one. Both rename paths are covered: the
    sidebar's PATCH and the project save's PUT, which also writes the name.
    """

    @pytest.fixture(autouse=True)
    def setup_database(self):
        app.dependency_overrides[get_db] = override_get_db
        Base.metadata.create_all(bind=engine)

        db = TestingSessionLocal()
        try:
            alice = Account(github_user="dup-alice", github_email="a@example.com", account_type="free")
            bob = Account(github_user="dup-bob", github_email="b@example.com", account_type="free")
            db.add_all([alice, bob])
            db.commit()
            ids = {}
            for name, code, owner in (
                ("Platform", "PLAT", alice), ("Infra", "INFRA", alice),
                ("Legacy", "LEG1", alice), ("Legacy", "LEG2", alice),
                ("Shared", "SHRD", bob),
            ):
                project = Project(project_name=name, project_code=code, user_id=owner.user_id,
                                  branch_option="default", project_type="standard")
                db.add(project)
                db.commit()
                ids[code] = project.project_id
            db.add_all([
                WorkspaceMember(user_id=alice.user_id, workspace_role="member"),
                WorkspaceMember(user_id=bob.user_id, workspace_role="member"),
                ProjectMembership(user_id=bob.user_id, project_id=ids["PLAT"], project_role="project_editor"),
            ])
            db.commit()
            self.ids = ids
        finally:
            db.close()

        yield

        Base.metadata.drop_all(bind=engine)
        if get_db in app.dependency_overrides:
            del app.dependency_overrides[get_db]

    def setup_method(self):
        self.client = TestClient(app)

    def _rename(self, code: str, new_name: str, caller: str = "dup-alice"):
        return self.client.patch(
            f"/api/projects/{self.ids[code]}/project-name",
            json={"project_name": new_name},
            headers=_signed_in(caller),
        )

    def _save(self, code: str, name: str, branch_regex: str = ""):
        return self.client.put(
            f"/api/projects/{self.ids[code]}/",
            json={
                "project_name": name,
                "selected_repos": [],
                "workflows": [],
                "branch_regex": branch_regex,
                "branch_option": "default",
                "branch_max_age_days": 30,
                "reusable_workflows_enabled": False,
                "use_prefix": False,
            },
            headers=_signed_in("dup-alice"),
        )

    def _names(self) -> dict:
        db = TestingSessionLocal()
        try:
            return {p.project_code: p.project_name for p in db.query(Project).all()}
        finally:
            db.close()

    @pytest.mark.parametrize("taken", ["Infra", "infra", "INFRA", "  Infra  "])
    def test_renaming_onto_another_of_the_owners_names_is_refused(self, taken):
        resp = self._rename("PLAT", taken)

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            f"This project's owner already has a project named '{taken.strip()}'. Choose another name."
        )
        assert self._names()["PLAT"] == "Platform"
        assert self._names()["INFRA"] == "Infra"

    def test_a_project_save_cannot_rename_onto_another_of_the_owners_names(self):
        """The PUT writes the name too, so it needs the same check, and refuses
        before writing anything else from the save."""
        resp = self._save("PLAT", "infra", branch_regex="release/.*")

        assert resp.status_code == 409, resp.text
        db = TestingSessionLocal()
        try:
            project = db.query(Project).filter(Project.project_id == self.ids["PLAT"]).first()
            assert project.project_name == "Platform"
            assert project.branch_regex != "release/.*"
        finally:
            db.close()

    def test_a_name_only_another_owner_uses_is_free(self):
        resp = self._rename("PLAT", "Shared")

        assert resp.status_code == 200, resp.text
        assert self._names()["PLAT"] == "Shared"

    def test_a_project_can_change_the_casing_of_its_own_name(self):
        resp = self._rename("PLAT", "PLATFORM")

        assert resp.status_code == 200, resp.text
        assert self._names()["PLAT"] == "PLATFORM"

    def test_the_check_is_against_the_projects_owner_not_the_caller(self):
        """bob edits alice's project: alice's names are taken, his own are not."""
        refused = self._rename("PLAT", "Infra", caller="dup-bob")
        assert refused.status_code == 409, refused.text

        allowed = self._rename("PLAT", "Shared", caller="dup-bob")
        assert allowed.status_code == 200, allowed.text
        assert self._names()["PLAT"] == "Shared"

    def test_a_pair_named_alike_before_this_check_keeps_saving(self):
        """An unchanged name is never checked, so an existing duplicate pair
        can still be saved, and renamed apart."""
        assert self._save("LEG1", "Legacy").status_code == 200

        resp = self._rename("LEG2", "Legacy Archive")
        assert resp.status_code == 200, resp.text
        assert self._names()["LEG2"] == "Legacy Archive"
