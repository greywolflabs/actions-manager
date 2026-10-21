"""
CODEOWNERS is managed only in Managed Files (#2367).

A Caller or Reusable Workflow Project can no longer save or import a
CODEOWNERS file, nor hold a custom file at a path GitHub reads CODEOWNERS
from. Rolling back one of its older campaigns leaves a CODEOWNERS file it
delivered to the project that manages it now.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import campaign_rollback
from auth import user_tokens
from models import CustomFile, Project, ProjectPullRequest, ProjectRepo, Repo
from tests.test_managed_files_section import (  # noqa: F401 - fixtures used by name
    OWNER,
    TestingSessionLocal,
    _account,
    _sign_in,
    client,
    schema,
)

PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
NOT_HERE = "CODEOWNERS is managed in Managed Files. Manage it from a CODEOWNERS project there."


def _at_path(path: str) -> str:
    return (f"{path} is a CODEOWNERS file, and CODEOWNERS is managed in Managed Files. "
            "Manage it from a CODEOWNERS project there.")


@pytest.fixture
def owner(client: TestClient):
    account = _account(OWNER)
    _sign_in(client, OWNER)
    with patch.dict(user_tokens, {OWNER: "token"}):
        yield account


def _project(owner, project_type: str = "standard", file_kind=None, repos=("acme/api",)) -> int:
    db = TestingSessionLocal()
    try:
        project = Project(project_name=f"{project_type}-project", user_id=owner.user_id,
                          project_type=project_type, file_kind=file_kind)
        db.add(project)
        db.flush()
        for name in repos:
            repo = Repo(repo_name=name)
            db.add(repo)
            db.flush()
            db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
        db.commit()
        return project.project_id
    finally:
        db.close()


@pytest.mark.parametrize("project_type", ["standard", "rwx"])
def test_a_workflow_project_cannot_save_codeowners(client, owner, project_type):
    project_id = _project(owner, project_type)

    refused = client.put(f"/api/projects/{project_id}/codeowners", json={"content": "* @acme\n"})

    assert refused.status_code == 400
    assert refused.json()["detail"] == NOT_HERE
    assert client.get(f"/api/projects/{project_id}/codeowners").json() == {"codeowners": None}


def test_a_workflow_project_cannot_import_a_repositorys_codeowners(client, owner):
    _project(owner)

    with patch("requests.get") as github:
        refused = client.get("/api/repos/acme/api/codeowners", params={"project_name": "standard-project"})

    assert refused.status_code == 400
    assert refused.json()["detail"] == NOT_HERE
    github.assert_not_called()


def test_a_codeowners_project_still_saves_its_file(client, owner):
    project_id = _project(owner, "managed_files", "codeowners")

    saved = client.put(f"/api/projects/{project_id}/codeowners", json={"content": "* @acme\n"})

    assert saved.status_code == 200


# Every way of spelling one of those paths is that path.
SPELLINGS = ("./CODEOWNERS", ".github//CODEOWNERS", "docs/./CODEOWNERS", "docs\\CODEOWNERS", " CODEOWNERS ")


@pytest.mark.parametrize("path", PATHS + SPELLINGS)
def test_a_custom_file_cannot_sit_where_github_reads_codeowners(client, owner, path):
    project_id = _project(owner)

    refused = client.post(f"/api/projects/{project_id}/custom-files", json={"file_path": path, "file_content": "x"})

    assert refused.status_code == 400
    assert refused.json()["detail"] == _at_path(path.strip())


def test_a_custom_file_cannot_move_there_or_come_back_from_there(client, owner):
    project_id = _project(owner)
    base = f"/api/projects/{project_id}/custom-files"
    elsewhere = client.post(base, json={"file_path": "scripts/build.sh", "file_content": "x"})
    db = TestingSessionLocal()
    try:
        # As if one were left from before the rule existed, queued for removal.
        legacy = CustomFile(project_id=project_id, file_path="CODEOWNERS", file_content="x", pending_delete=True)
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    finally:
        db.close()

    moved = client.put(f"{base}/{elsewhere.json()['custom_file']['id']}", json={"file_path": "docs/CODEOWNERS"})
    restored = client.post(f"{base}/{legacy_id}/restore")

    assert elsewhere.status_code == 200
    assert (moved.status_code, moved.json()["detail"]) == (400, _at_path("docs/CODEOWNERS"))
    assert (restored.status_code, restored.json()["detail"]) == (400, _at_path("CODEOWNERS"))


def test_a_custom_file_left_at_a_codeowners_path_can_only_move_away(client, owner):
    project_id = _project(owner)
    db = TestingSessionLocal()
    try:
        # As if the cleanup migration had not run yet.
        legacy = CustomFile(project_id=project_id, file_path="CODEOWNERS", file_content="x")
        db.add(legacy)
        db.commit()
        url = f"/api/projects/{project_id}/custom-files/{legacy.id}"
    finally:
        db.close()

    edited = client.put(url, json={"file_content": "* @acme\n"})
    moved = client.put(url, json={"file_path": "docs/owners.txt"})

    assert (edited.status_code, edited.json()["detail"]) == (400, _at_path("CODEOWNERS"))
    assert moved.status_code == 200


# ---------------------------------------------------------------------------
# Rolling back an older campaign
# ---------------------------------------------------------------------------

def _merged_pr(project_id: int) -> ProjectPullRequest:
    return ProjectPullRequest(project_id=project_id, repo_name="acme/api", target_branch="main", pr_number=7,
                              pr_url="https://github.com/acme/api/pull/7", workflow_names="ci")


def _inverted(_repo, entry, *_rest):
    """Every file the rollback reaches inverts; which files it reaches is the question."""
    return {"path": entry["filename"], "action": "delete"}, None


def _invert(project_id: int, changed: list) -> dict:
    db = TestingSessionLocal()
    try:
        with patch.object(campaign_rollback, "_pre_merge_base_sha", return_value=("base", None)), \
                patch.object(campaign_rollback, "_pr_changed_files", return_value=(changed, None)), \
                patch.object(campaign_rollback, "_invert_one_file", side_effect=_inverted):
            return campaign_rollback._invert_one_pr(_merged_pr(project_id), OWNER, db, {})
    finally:
        db.close()


WORKFLOW = {"filename": ".github/workflows/ci.yml", "status": "added"}
CODEOWNERS = {"filename": ".github/CODEOWNERS", "status": "added"}


def test_a_workflow_projects_rollback_leaves_codeowners_to_managed_files(owner):
    project_id = _project(owner)

    target = _invert(project_id, [WORKFLOW, CODEOWNERS])

    assert target["invertible"] is True
    assert [f["path"] for f in target["files"]] == [".github/workflows/ci.yml"]
    assert target["left_alone"] == [".github/CODEOWNERS"]  # so the preview can say it stays


def test_a_rollback_of_only_codeowners_says_why_there_is_nothing_to_do(owner):
    project_id = _project(owner)

    target = _invert(project_id, [CODEOWNERS])

    assert target["invertible"] is False
    assert target["reason"] == (
        "This pull request changed only .github/CODEOWNERS, which Managed Files manages now, "
        "so there is nothing here to roll back."
    )


def test_a_codeowners_projects_rollback_still_reverts_its_file(owner):
    project_id = _project(owner, "managed_files", "codeowners")

    target = _invert(project_id, [CODEOWNERS])

    assert [f["path"] for f in target["files"]] == [".github/CODEOWNERS"]
    assert target["left_alone"] == []
