"""
Drift for a project's custom files: the CODEOWNERS rule, through the same functions.

- A custom file drifts on (repo, branch) when GitHub's copy is not what
  ActionsManager delivered there (``workflows._decide_drift``), changed or
  deleted; an open PR carrying it there is no drift, and a merge clears it.
- Only where the delivery record has a row for it is GitHub read at all:
  anywhere else it is "not delivered yet" whatever GitHub holds. Where it is
  read, the read is conditional on the ETag the record keeps.
- A local edit resolves only the drift whose GitHub copy it now is; removing
  the file stops its drift counting.
- It is stored, served, seeded on the project load and notified with the
  project's other drift.
"""

import base64
import json
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deliveries  # noqa: E402
from database import get_db  # noqa: E402
from deliveries import blob_sha  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Account, Base, CustomFile, FileDelivery, NotificationEvent, Project, ProjectMembership, ProjectPullRequest,
    ProjectRepo, Repo, WorkspaceMember,
)
from tests.delivery_records import record_pr_carries, record_pr_merged  # noqa: E402
from workflows import (  # noqa: E402
    DELETE_DELETED, _Removal, _clear_drift_for_merged_pr, _collect_file_observations, _remove_directly, _target_on,
    run_project_drift_check,
)

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


client = TestClient(app)

OWNER = "cf_owner"
VIEWER = "cf_viewer"
PROJECT = "cf_project"
REPO_A = "octo/repo-a"
REPO_B = "octo/repo-b"
PATH = "scripts/build.sh"
DELIVERED = "#!/bin/sh\necho build\n"
CHANGED = "#!/bin/sh\necho hotfix\n"
TOKENS = {OWNER: "owner-token", VIEWER: "viewer-token"}


class FakeGitHub:
    """GitHub's contents API: ``files[(repo, branch, path)]`` is the content, absent a 404.

    Answers a matching If-None-Match with a 304, as GitHub does, and records
    every request as (repo, path, branch, If-None-Match).
    """

    def __init__(self):
        self.files = {}
        self.requests = []

    def put(self, repo, content, branch="main", path=PATH):
        self.files[(repo, branch, path)] = content

    def delete(self, repo, branch="main", path=PATH):
        self.files.pop((repo, branch, path), None)

    def get(self, url, headers=None, params=None, timeout=None):
        parsed = urlsplit(url)
        branch = (params or {}).get("ref") or parse_qs(parsed.query).get("ref", [None])[0]
        repo, path = re.match(r"/repos/([^/]+/[^/]+)/contents/(.+)", parsed.path).groups()
        etag_in = (headers or {}).get("If-None-Match")
        self.requests.append((repo, unquote(path), branch, etag_in))
        response = MagicMock()
        response.headers = {}
        content = self.files.get((repo, branch, unquote(path)))
        if content is None:
            response.status_code = 404
            return response
        response.headers = {"ETag": _etag(content)}
        if etag_in == response.headers["ETag"]:
            response.status_code = 304
            return response
        response.status_code = 200
        response.json.return_value = {"content": base64.b64encode(content.encode()).decode(),
                                      "sha": blob_sha(content)}
        return response


def _etag(content):
    return f'W/"{blob_sha(content)}"'


@pytest.fixture
def github(authenticate_client):
    app.dependency_overrides[get_db] = override_get_db
    Base.metadata.create_all(bind=engine)
    authenticate_client(client, OWNER, TestingSessionLocal)
    fake = FakeGitHub()
    fake.resolved = []

    def resolve(owner, repo, *_args, **_kwargs):
        fake.resolved.append(f"{owner}/{repo}")
        return ["main"]

    with patch("workflows._resolve_branches_for_repo", side_effect=resolve), \
            patch("requests.get", side_effect=fake.get), \
            patch("custom_files.user_tokens", TOKENS), \
            patch("workflows.user_tokens", TOKENS):
        yield fake
    Base.metadata.drop_all(bind=engine)
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def db(github):
    session = TestingSessionLocal()
    yield session
    session.close()


@pytest.fixture
def project(db):
    owner = Account(github_user=OWNER, github_email="o@example.com", account_type="free")
    viewer = Account(github_user=VIEWER, github_email="v@example.com", account_type="free")
    db.add_all([owner, viewer])
    db.flush()
    project = Project(project_name=PROJECT, project_code="CFD", user_id=owner.user_id,
                      branch_option="default", pr_state="synced")
    db.add(project)
    db.flush()
    db.add(WorkspaceMember(user_id=viewer.user_id, workspace_role="member"))
    db.add(ProjectMembership(user_id=viewer.user_id, project_id=project.project_id, project_role="project_viewer"))
    for name in (REPO_A, REPO_B):
        repo = Repo(repo_name=name)
        db.add(repo)
        db.flush()
        db.add(ProjectRepo(project_id=project.project_id, repo_id=repo.repo_id))
    db.commit()
    return project


def _add_file(db, project, content=DELIVERED, path=PATH, status="synced_with_github"):
    cf = CustomFile(project_id=project.project_id, file_path=path, file_content=content,
                    git_hash=blob_sha(content), file_status=status)
    db.add(cf)
    db.commit()
    return cf


def _delivered(db, project, github, repos=(REPO_A, REPO_B), content=DELIVERED):
    """The file, delivered as *content* to each of *repos* on main: on GitHub and on the record."""
    cf = _add_file(db, project, content)
    for repo in repos:
        github.put(repo, content)
        deliveries.record_delivered(db, deliveries.CUSTOM_FILE, cf.id, repo, "main", PATH, blob_sha(content))
    db.commit()
    return cf


def _check(project):
    """A live check, as Check Now and the sweep run it."""
    response = client.get(f"/api/projects/{project.project_id}/drift", params={"refresh": "true"})
    assert response.status_code == 200, response.text
    return response.json()


def _stored(project):
    response = client.get(f"/api/projects/{project.project_id}/drift")
    assert response.status_code == 200, response.text
    return response.json()


def _drifted(summary):
    return sorted((d["repo"], d["branch"]) for d in summary["drifted_custom_files"])


def _details(project):
    """Every custom-file detail a live check produces, drifted or not, by (repo, branch)."""
    session = TestingSessionLocal()
    try:
        fresh = session.query(Project).filter_by(project_id=project.project_id).one()
        observations = _collect_file_observations(session, OWNER, fresh)
        session.commit()
        return {(o.detail.repo, o.detail.branch): o.detail for o in observations
                if o.kind == deliveries.CUSTOM_FILE}
    finally:
        session.close()


def _save(project, cf, as_client=client, **fields):
    return as_client.put(f"/api/projects/{project.project_id}/custom-files/{cf.id}", json=fields)


def _row(db, cf, repo):
    db.expire_all()
    return db.query(FileDelivery).join(Repo, Repo.repo_id == FileDelivery.repo_id).filter(
        FileDelivery.custom_file_id == cf.id, Repo.repo_name == repo, FileDelivery.branch == "main",
    ).one()


def _events(db, event_type):
    db.expire_all()
    return db.query(NotificationEvent).filter_by(event_type=event_type).all()


def _pr(db, project, state, repo=REPO_A):
    pr = ProjectPullRequest(
        project_id=project.project_id, repo_name=repo, pr_number=7, pr_url="u",
        branch_name="actions-manager/cfd-main", target_branch="main", pr_state=state, file_names=PATH,
    )
    db.add(pr)
    db.commit()
    return pr


# --------------------------------------------------------------------------- #
# The rule                                                                    #
# --------------------------------------------------------------------------- #


def test_a_delivered_file_github_still_holds_is_not_drift(db, project, github):
    _delivered(db, project, github)

    summary = _check(project)

    assert summary["drifted_custom_files"] == []
    assert summary["drift_status"] == "clean"


def test_a_change_on_github_after_delivery_is_drift_there(db, project, github):
    cf = _delivered(db, project, github)
    github.put(REPO_A, CHANGED)

    summary = _check(project)

    assert _drifted(summary) == [(REPO_A, "main")]
    detail = summary["drifted_custom_files"][0]
    assert detail["custom_file_id"] == cf.id
    assert detail["file_id"] == cf.id  # named as every drift row names its file
    assert detail["file_path"] == PATH
    assert detail["deleted_in_github"] is False
    assert detail["github_sha"] == blob_sha(CHANGED)
    assert detail["actionsmanager_sha"] == blob_sha(DELIVERED)
    assert detail["message"] == f"{PATH} content differs between local and {REPO_A}"
    assert (summary["drift_count"], summary["drift_status"]) == (1, "drifted")


def test_a_deletion_on_github_is_drift_deleted(db, project, github):
    _delivered(db, project, github)
    github.delete(REPO_B)

    summary = _check(project)

    assert _drifted(summary) == [(REPO_B, "main")]
    assert summary["drifted_custom_files"][0]["deleted_in_github"] is True
    assert summary["drifted_custom_files"][0]["message"] == f"{PATH} was deleted from {REPO_B}"


def test_where_it_was_never_delivered_github_is_never_read(db, project, github):
    """No row on the record there: "not delivered yet" whatever GitHub holds,
    so neither the file nor the repository's branches cost a GitHub call."""
    _delivered(db, project, github, repos=(REPO_A,))
    github.put(REPO_B, CHANGED)
    github.requests.clear()

    summary = _check(project)

    assert summary["drifted_custom_files"] == []
    assert {request[0] for request in github.requests} == {REPO_A}
    assert REPO_B not in github.resolved


def test_a_file_on_no_record_costs_no_github_call(db, project, github):
    _add_file(db, project, status="new")
    github.put(REPO_A, CHANGED)

    assert _details(project) == {}
    assert github.requests == []
    assert github.resolved == []


def test_an_open_pr_carrying_it_is_no_drift(db, project, github):
    cf = _delivered(db, project, github)
    github.put(REPO_A, CHANGED)
    record_pr_carries(db, _pr(db, project, "open"), cf)

    details = _details(project)

    assert details[(REPO_A, "main")].has_drift is False
    assert details[(REPO_A, "main")].message == f"{PATH} under review in open PR for {REPO_A}"
    github.delete(REPO_A)
    assert _details(project)[(REPO_A, "main")].message == f"{PATH} in open PR - pending merge to {REPO_A}"


def test_a_new_file_an_open_pr_carries_is_checked_only_there(db, project, github):
    cf = _add_file(db, project, status="under_review")
    record_pr_carries(db, _pr(db, project, "open"), cf)

    details = _details(project)

    assert list(details) == [(REPO_A, "main")]
    assert details[(REPO_A, "main")].has_drift is False
    assert {request[0] for request in github.requests} == {REPO_A}


def test_a_file_on_its_way_out_is_not_checked(db, project, github):
    cf = _delivered(db, project, github)
    cf.pending_delete = True
    db.commit()
    github.put(REPO_A, CHANGED)

    assert _details(project) == {}
    assert github.requests == []


def test_a_local_path_change_reads_where_it_was_delivered(db, project, github):
    """A local edit never makes drift: the copy delivered at the old path is still there."""
    cf = _delivered(db, project, github)

    assert _save(project, cf, file_path="scripts/ci/build.sh").status_code == 200

    details = _details(project)
    assert details[(REPO_A, "main")].has_drift is False
    assert details[(REPO_A, "main")].file_path == PATH
    assert {request[1] for request in github.requests} == {PATH}


# --------------------------------------------------------------------------- #
# Cost                                                                        #
# --------------------------------------------------------------------------- #


def test_an_unchanged_branch_answers_304_with_no_content_read(db, project, github):
    _delivered(db, project, github)
    _check(project)
    github.requests.clear()

    _check(project)

    assert sorted(github.requests) == [
        (REPO_A, PATH, "main", _etag(DELIVERED)), (REPO_B, PATH, "main", _etag(DELIVERED)),
    ]


def test_the_etag_is_kept_on_the_record(db, project, github):
    cf = _delivered(db, project, github)

    _check(project)

    assert _row(db, cf, REPO_A).etag == _etag(DELIVERED)


# --------------------------------------------------------------------------- #
# Clears                                                                      #
# --------------------------------------------------------------------------- #


def _drift_on_both(db, project, github):
    cf = _delivered(db, project, github)
    github.put(REPO_A, CHANGED)
    github.put(REPO_B, CHANGED)
    _check(project)
    return cf


def test_a_merged_pr_carrying_it_clears_its_drift_there(db, project, github):
    cf = _drift_on_both(db, project, github)
    pr = _pr(db, project, "merged")
    record_pr_merged(db, pr, cf)

    _clear_drift_for_merged_pr(db, project, pr)

    assert _row(db, cf, REPO_A).has_drift is False
    assert _row(db, cf, REPO_B).has_drift is True
    db.refresh(project)
    assert project.drift_count == 1


def test_a_direct_delete_clears_its_drift_where_it_removed_the_file(db, project, github):
    cf = _drift_on_both(db, project, github)

    with patch("workflows._delete_path_from_branch", return_value=(DELETE_DELETED, None)):
        _remove_directly(db, project, _target_on(REPO_A, "main"), _Removal(deliveries.CUSTOM_FILE, cf.id, PATH, PATH),
                         OWNER, {})

    assert _row(db, cf, REPO_A).has_drift is False
    assert _row(db, cf, REPO_A).delivered_at is None
    assert _row(db, cf, REPO_B).has_drift is True
    assert _drifted(_stored(project)) == [(REPO_B, "main")]


@pytest.mark.parametrize("saved", [CHANGED, CHANGED.rstrip("\n")], ids=["exact", "without-final-newline"])
def test_a_local_edit_adopting_github_s_copy_resolves_its_drift(db, project, github, saved):
    cf = _drift_on_both(db, project, github)
    github.put(REPO_B, "#!/bin/sh\necho something else\n")
    _check(project)

    body = _save(project, cf, file_content=saved).json()

    assert body["custom_file"]["file_status"] == "committed_locally"
    stored = _stored(project)
    assert _drifted(stored) == [(REPO_B, "main")]
    assert stored["drift_count"] == 1
    assert _row(db, cf, REPO_A).delivered_sha == blob_sha(CHANGED)
    resolved = [json.loads(e.payload) for e in _events(db, "drift.resolved")]
    assert [(e["repo"], e["workflow_name"]) for e in resolved] == [(REPO_A, PATH)]


def test_another_local_edit_leaves_its_drift(db, project, github):
    cf = _drift_on_both(db, project, github)

    _save(project, cf, file_content="#!/bin/sh\necho mine\n")

    assert _drifted(_stored(project)) == [(REPO_A, "main"), (REPO_B, "main")]
    assert _drifted(_check(project)) == [(REPO_A, "main"), (REPO_B, "main")]


def test_removing_it_from_the_project_stops_its_drift_counting(db, project, github):
    cf = _drift_on_both(db, project, github)

    response = client.delete(f"/api/projects/{project.project_id}/custom-files/{cf.id}", params={"scope": "project"})

    assert response.status_code == 200, response.text
    stored = _stored(project)
    assert (stored["drift_count"], stored["drift_status"]) == (0, "clean")
    db.refresh(project)
    assert project.drift_count == 0


def test_a_deletion_campaign_stops_its_drift_counting(db, project, github):
    """The check skips a file on its way out, so drift found before would otherwise stay counted."""
    cf = _drift_on_both(db, project, github)
    campaign = {"prs_created": 2, "campaign_id": 5, "results": {}}

    with patch("workflows._run_removal_campaign", return_value=campaign):
        response = client.delete(f"/api/projects/{project.project_id}/custom-files/{cf.id}")

    assert response.status_code == 200, response.text
    assert response.json()["pending_delete"] is True
    assert _row(db, cf, REPO_A).has_drift is False
    assert _row(db, cf, REPO_A).delivered_sha == blob_sha(DELIVERED)
    db.refresh(project)
    assert (project.drift_status, project.drift_count) == ("clean", 0)


# --------------------------------------------------------------------------- #
# Reads                                                                       #
# --------------------------------------------------------------------------- #


def test_the_drift_endpoint_lists_it_without_github(db, project, github):
    cf = _delivered(db, project, github)
    github.put(REPO_A, CHANGED)
    _check(project)
    github.requests.clear()

    stored = _stored(project)

    assert github.requests == []
    assert stored["drift_count"] == 1
    detail = stored["drifted_custom_files"][0]
    assert {key: detail[key] for key in ("custom_file_id", "file_path", "repo", "branch", "has_drift",
                                         "deleted_in_github", "github_sha", "actionsmanager_sha", "message")} == {
        "custom_file_id": cf.id, "file_path": PATH, "repo": REPO_A, "branch": "main", "has_drift": True,
        "deleted_in_github": False, "github_sha": blob_sha(CHANGED), "actionsmanager_sha": blob_sha(DELIVERED),
        "message": f"{PATH} content differs between local and {REPO_A}",
    }
    assert detail["repo_id"] is not None
    assert detail["last_checked"]


def test_the_project_load_seeds_which_files_drifted(db, project, github):
    cf = _delivered(db, project, github)
    _add_file(db, project, path="scripts/other.sh")
    github.put(REPO_A, CHANGED)
    _check(project)

    response = client.get(f"/api/projects/{PROJECT}", params={"github_user": OWNER})

    assert response.status_code == 200, response.text
    assert response.json()["drifted_custom_file_ids"] == [cf.id]


def test_the_sweep_returns_it_and_counts_it(db, project, github):
    _delivered(db, project, github)
    github.put(REPO_B, CHANGED)

    drifted, unchecked, codeowners, custom_files = run_project_drift_check(db, OWNER, project)

    assert (drifted, unchecked, codeowners) == ([], [], [])
    assert [(d.repo, d.branch) for d in custom_files] == [(REPO_B, "main")]
    assert (project.drift_status, project.drift_count) == ("drifted", 1)


def test_a_github_failure_is_unchecked_not_clean(db, project, github):
    _delivered(db, project, github)
    github.put(REPO_A, CHANGED)
    _check(project)

    with patch("requests.get", return_value=MagicMock(status_code=500, headers={})):
        summary = _check(project)

    assert summary["unchecked_count"] == 2
    assert summary["drift_status"] == "check_failed"
    assert _drifted(_stored(project)) == [(REPO_A, "main")]
    assert _events(db, "drift.resolved") == []


def test_its_github_copy_is_read_where_the_check_reads_it(db, project, github, authenticate_client):
    cf = _delivered(db, project, github)
    github.put(REPO_A, CHANGED)
    _save(project, cf, file_path="scripts/ci/build.sh")
    github.requests.clear()
    viewer = TestClient(app)
    authenticate_client(viewer, VIEWER, TestingSessionLocal)

    response = viewer.get(f"/api/projects/{project.project_id}/custom-files/{cf.id}/github",
                          params={"repo": REPO_A, "branch": "main"})

    assert response.status_code == 200, response.text
    assert response.json()["github"] == {"exists": True, "content": CHANGED, "sha": blob_sha(CHANGED), "path": PATH}
    assert github.requests == [(REPO_A, PATH, "main", None)]


def test_its_github_copy_is_read_only_in_the_project_s_repositories(db, project, github):
    cf = _delivered(db, project, github)
    db.add(Repo(repo_name="other/orphan"))
    db.commit()
    github.requests.clear()

    response = client.get(f"/api/projects/{project.project_id}/custom-files/{cf.id}/github",
                          params={"repo": "other/orphan", "branch": "main"})

    assert response.status_code == 404
    assert github.requests == []


def test_a_missing_github_copy_says_so(db, project, github):
    cf = _delivered(db, project, github)
    github.delete(REPO_A)

    response = client.get(f"/api/projects/{project.project_id}/custom-files/{cf.id}/github",
                          params={"repo": REPO_A, "branch": "main"})

    assert response.json()["github"] == {"exists": False, "content": None, "sha": None, "path": PATH}


# --------------------------------------------------------------------------- #
# Notifications                                                               #
# --------------------------------------------------------------------------- #


def test_drift_is_notified_once_and_its_resolution_too(db, project, github):
    cf = _delivered(db, project, github)
    github.put(REPO_A, CHANGED)
    _check(project)
    _check(project)

    detected = _events(db, "drift.detected")
    assert [(json.loads(e.payload)["repo"], json.loads(e.payload)["workflow_name"]) for e in detected] == [
        (REPO_A, PATH),
    ]
    assert f":custom_file:{cf.id}:main:" in detected[0].dedup_key

    github.put(REPO_A, DELIVERED)
    _check(project)
    _check(project)

    assert len(_events(db, "drift.resolved")) == 1

    # The same change again is a new drift, so it notifies again.
    github.put(REPO_A, CHANGED)
    _check(project)
    assert len(_events(db, "drift.detected")) == 2
