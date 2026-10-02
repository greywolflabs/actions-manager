"""#2130: the repo-per-project limit applies to PUT /api/projects/{id}/, not only create."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from models import Account, Project, ProjectRepo, Repo
from tests.conftest import TestingSessionLocal

LIMIT = 10
OWNER = "repolimituser"


@pytest.fixture(autouse=True)
def free_tier():
    with patch("tier_service.license.get_installation_tier", return_value="free"):
        yield


@pytest.fixture
def client():
    return TestClient(app)


def _repos(n, prefix="org/repo"):
    return [f"{prefix}{i}" for i in range(n)]


def _seed_project(repo_count):
    db = TestingSessionLocal()
    try:
        owner = Account(github_user=OWNER, github_email="r@example.com", account_type="free")
        db.add(owner)
        db.flush()
        project = Project(project_name="limits", user_id=owner.user_id, project_code="LIM")
        db.add(project)
        db.flush()
        for name in _repos(repo_count):
            repo = Repo(repo_name=name)
            db.add(repo)
            db.flush()
            db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id, branch_config_mode="inherit"))
        db.commit()
        return project.project_id
    finally:
        db.close()


def _repo_names(project_id):
    db = TestingSessionLocal()
    try:
        rows = (
            db.query(Repo.repo_name)
            .join(ProjectRepo, ProjectRepo.repo_id == Repo.repo_id)
            .filter(ProjectRepo.project_id == project_id)
            .all()
        )
        return sorted(name for (name,) in rows)
    finally:
        db.close()


def _payload(selected_repos, name="limits"):
    return {
        "github_user": OWNER,
        "project_name": name,
        "selected_repos": selected_repos,
        "workflows": [],
        "rxworkflows": [],
        "branch_regex": "",
        "branch_option": "default",
        "reusable_workflows_enabled": False,
    }


def _put(client, project_id, selected_repos, name="limits"):
    return client.put(
        f"/api/projects/{project_id}/",
        json=_payload(selected_repos, name),
        headers={"X-GitHub-User": OWNER},
    )


def test_update_past_limit_is_rejected_and_leaves_repos_unchanged(client):
    project_id = _seed_project(LIMIT)
    before = _repo_names(project_id)

    response = _put(client, project_id, _repos(LIMIT + 1))

    assert response.status_code == 403
    assert f"up to {LIMIT} repositories per project" in response.json()["detail"]
    assert "Upgrade" not in response.json()["detail"]
    assert _repo_names(project_id) == before


def test_update_up_to_limit_is_allowed(client):
    project_id = _seed_project(LIMIT - 1)

    response = _put(client, project_id, _repos(LIMIT))

    assert response.status_code == 200
    assert len(_repo_names(project_id)) == LIMIT


def test_over_limit_project_keeps_saving_at_same_count(client):
    project_id = _seed_project(LIMIT + 2)

    response = _put(client, project_id, _repos(LIMIT + 2), name="renamed")

    assert response.status_code == 200
    assert len(_repo_names(project_id)) == LIMIT + 2


def test_over_limit_project_can_shrink(client):
    project_id = _seed_project(LIMIT + 2)

    response = _put(client, project_id, _repos(LIMIT + 1))

    assert response.status_code == 200
    assert len(_repo_names(project_id)) == LIMIT + 1


def test_over_limit_project_cannot_grow(client):
    project_id = _seed_project(LIMIT + 2)

    response = _put(client, project_id, _repos(LIMIT + 3))

    assert response.status_code == 403
    assert len(_repo_names(project_id)) == LIMIT + 2


def test_duplicates_and_blanks_count_once_on_update(client):
    project_id = _seed_project(LIMIT - 1)
    selected = _repos(LIMIT) + [" org/repo0 ", "org/repo1", "", "   "]

    response = _put(client, project_id, selected)

    assert response.status_code == 200
    assert len(_repo_names(project_id)) == LIMIT


def test_create_past_limit_is_rejected(client):
    db = TestingSessionLocal()
    try:
        db.add(Account(github_user=OWNER, github_email="r@example.com", account_type="free"))
        db.commit()
    finally:
        db.close()

    response = client.post("/api/projects/", json=_payload(_repos(LIMIT + 1), name="fresh"))

    assert response.status_code == 403
    assert f"up to {LIMIT} repositories per project" in response.json()["detail"]


def test_create_counts_duplicates_once(client):
    db = TestingSessionLocal()
    try:
        db.add(Account(github_user=OWNER, github_email="r@example.com", account_type="free"))
        db.commit()
    finally:
        db.close()

    response = client.post("/api/projects/", json=_payload(_repos(LIMIT) + ["org/repo0", " "], name="fresh"))

    assert response.status_code == 200
