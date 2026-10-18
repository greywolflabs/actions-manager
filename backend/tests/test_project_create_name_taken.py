"""Creating a project with a name its owner already uses is refused (#2338).

POST /api/projects/ looked the owner's project up by name and, finding one,
updated it in place: the wizard's settings replaced the existing project's,
and its repositories and workflows were merged in, with no error and no new
project. Saving an existing project goes through PUT /projects/{id}/.
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import Account, Base, Project, ProjectRepo  # noqa: E402
from main import app  # noqa: E402
from projects import get_db  # noqa: E402
from auth import create_auth_session  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

ALICE = "taken-alice"
BOB = "taken-bob"


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def database():
    app.dependency_overrides[get_db] = override_get_db
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        db.add_all([
            Account(github_user=name, github_email=f"{name}@example.com", account_type="free")
            for name in (ALICE, BOB)
        ])
        db.commit()
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    app.dependency_overrides.pop(get_db, None)


def _create(caller, name, **overrides):
    db = TestingSessionLocal()
    try:
        token = create_auth_session(caller, db)
    finally:
        db.close()
    payload = {
        "github_user": caller, "project_name": name, "selected_repos": [f"{caller}/one"],
        "workflows": [], "branch_regex": "", "branch_option": "default", "branch_max_age_days": 30,
        "reusable_workflows_enabled": False, "use_prefix": True, **overrides,
    }
    return TestClient(app).post("/api/projects/", json=payload, headers={"Authorization": f"Bearer {token}"})


def _projects():
    db = TestingSessionLocal()
    try:
        return [
            (p.project_name, p.branch_option, p.use_prefix,
             db.query(ProjectRepo).filter(ProjectRepo.project_id == p.project_id).count())
            for p in db.query(Project).order_by(Project.project_id).all()
        ]
    finally:
        db.close()


@pytest.mark.parametrize("taken", ["Infra", "infra", "INFRA", "  Infra  "])
def test_creating_a_name_the_owner_already_uses_is_refused_and_changes_nothing(taken):
    assert _create(ALICE, "Infra").status_code == 200

    resp = _create(ALICE, taken, branch_option="pattern", branch_regex="release/.*", use_prefix=False,
                   selected_repos=[f"{ALICE}/one", f"{ALICE}/two"])

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == (
        f"You already have a project named '{taken.strip()}'. Choose another name."
    )
    assert _projects() == [("Infra", "default", True, 1)]


def test_a_name_only_another_owner_uses_is_free():
    assert _create(BOB, "Infra").status_code == 200

    resp = _create(ALICE, "Infra")

    assert resp.status_code == 200, resp.text
    assert len(_projects()) == 2
