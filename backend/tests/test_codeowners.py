"""
Tests for the project's CODEOWNERS file: its endpoints in ``backend/codeowners.py``
and its drift, which ``workflows.run_project_drift_check`` decides by the
workflow rules.

- One file per project, saved locally: a change is committed_locally with its
  hash cleared, and resolves only the drift that makes it GitHub's copy.
- Drift per (repo, branch) the project delivers to, read against the delivery
  record: the delivered-blob fast path, the normalized compare, an open PR
  under review, and a missing file a deletion only where it was delivered. A
  local edit neither hides drift nor makes it.
- It is stored, counted and notified alongside workflow drift, and a merged PR
  carrying the file clears it, a rollback included.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (  # noqa: E402
    Base, Account, Codeowners, FileDelivery, NotificationEvent, Project, ProjectMembership,
    ProjectPRCampaign, ProjectPullRequest, ProjectRepo, ProjectWorkflow, Repo, Workflow, WorkspaceMember,
)
from main import app  # noqa: E402
from codeowners import get_db  # noqa: E402
from deliveries import blob_sha as _git_blob_sha  # noqa: E402
from tests.campaign_harness import FakeGitHub as CampaignGitHub, run_campaign  # noqa: E402
from tests.delivery_records import record_delivered, record_pr_carries, record_pr_merged  # noqa: E402
from workflows import (  # noqa: E402
    _clear_drift_for_merged_pr, _collect_file_observations, run_project_drift_check,
)


SQLALCHEMY_DATABASE_URL = "sqlite:///./test_codeowners.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


client = TestClient(app)

TEST_USER = "co_user"
VIEWER = "co_viewer"
TEST_PROJECT = "co_project"
REPO_A = "octo/repo-a"
REPO_B = "octo/repo-b"
PATH = ".github/CODEOWNERS"
DELIVERED = "* @octo/owners\n"
CHANGED = "* @someone-else\n"
TOKENS = {TEST_USER: "fake-token", VIEWER: "viewer-token"}


class FakeGitHub:
    """GitHub's contents API: ``files[(repo, branch, path)]`` is the content, absent is a 404.

    Answers a matching If-None-Match with a 304, as GitHub does, and records
    every request as (repo, path, branch, If-None-Match).
    """

    def __init__(self):
        self.files = {}
        self.requests = []
        self.status = None

    def put(self, repo, content, branch="main", path=PATH):
        self.files[(repo, branch, path)] = content

    def delete(self, repo, branch="main", path=PATH):
        self.files.pop((repo, branch, path), None)

    def get(self, url, headers=None, params=None, timeout=None):
        parsed = urlsplit(url)
        branch = (params or {}).get("ref") or parse_qs(parsed.query).get("ref", [None])[0]
        repo, path = re.match(r"/repos/([^/]+/[^/]+)/contents/(.+)", parsed.path).groups()
        path = unquote(path)
        etag_in = (headers or {}).get("If-None-Match")
        self.requests.append((repo, path, branch, etag_in))
        response = MagicMock()
        response.headers = {}
        if self.status:
            response.status_code = self.status
            return response
        content = self.files.get((repo, branch or "main", path))
        if content is None:
            response.status_code = 404
            return response
        sha = _git_blob_sha(content)
        response.headers = {"ETag": f'W/"{sha}"'}
        if etag_in == response.headers["ETag"]:
            response.status_code = 304
            return response
        response.status_code = 200
        response.json.return_value = {"content": base64.b64encode(content.encode()).decode(), "sha": sha}
        return response


def _etag(content):
    return f'W/"{_git_blob_sha(content)}"'


class TestCodeowners:

    @pytest.fixture(autouse=True)
    def setup_db(self, authenticate_client):
        app.dependency_overrides[get_db] = override_get_db
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        authenticate_client(client, TEST_USER, TestingSessionLocal)
        self.github = FakeGitHub()
        # The branches the project delivers to; the resolver itself is covered by its own tests.
        self.branches = ["main"]
        self.resolved = []

        def resolve(owner, repo, *_args, **_kwargs):
            self.resolved.append(f"{owner}/{repo}")
            return list(self.branches)

        with patch("workflows._resolve_branches_for_repo", side_effect=resolve), \
                patch("requests.get", side_effect=self.github.get), \
                patch("codeowners.user_tokens", TOKENS), \
                patch("workflows.user_tokens", TOKENS):
            self._seed()
            yield
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.pop(get_db, None)

    def _seed(self):
        owner = Account(github_user=TEST_USER, github_email="co@example.com", account_type="free")
        viewer = Account(github_user=VIEWER, github_email="v@example.com", account_type="free")
        self.db.add_all([owner, viewer])
        self.db.flush()
        # Only a CODEOWNERS project in Managed Files holds the file (#2367).
        self.project = Project(
            project_name=TEST_PROJECT, project_code="COX", user_id=owner.user_id,
            branch_option="default", pr_state="synced",
            project_type="managed_files", file_kind="codeowners",
        )
        self.db.add(self.project)
        self.db.flush()
        self.db.add(WorkspaceMember(user_id=viewer.user_id, workspace_role="member"))
        self.db.add(ProjectMembership(user_id=viewer.user_id, project_id=self.project.project_id,
                                      project_role="project_viewer"))
        self.repos = {}
        for name in (REPO_A, REPO_B):
            repo = Repo(repo_name=name)
            self.db.add(repo)
            self.db.flush()
            self.db.add(ProjectRepo(project_id=self.project.project_id, repo_id=repo.repo_id))
            self.repos[name] = repo
        self.db.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_record(self, content=DELIVERED, status="synced_with_github", git_hash=None):
        record = Codeowners(project_id=self.project.project_id, content=content, file_path=PATH,
                            git_hash=git_hash or _git_blob_sha(content), status=status)
        self.db.add(record)
        self.db.commit()
        return record

    def _delivered(self, content=DELIVERED, status="synced_with_github"):
        """A CODEOWNERS file on every repo, and the check that recorded it delivered there."""
        record = self._add_record(content, status)
        for repo in (REPO_A, REPO_B):
            self.github.put(repo, content)
        self._check()
        return record

    def _record(self):
        self.db.expire_all()
        return self.db.query(Codeowners).filter_by(project_id=self.project.project_id).one()

    def _save(self, content, file_path=PATH, as_client=client):
        return as_client.put(f"/api/projects/{self.project.project_id}/codeowners",
                             json={"content": content, "file_path": file_path})

    def _check(self):
        """A live check, as Check Now and the sweep run it."""
        response = client.get(f"/api/projects/{self.project.project_id}/drift", params={"refresh": "true"})
        assert response.status_code == 200, response.text
        return response.json()

    def _stored(self):
        response = client.get(f"/api/projects/{self.project.project_id}/drift")
        assert response.status_code == 200, response.text
        return response.json()

    def _details(self):
        """Every CODEOWNERS detail a live check produces, drifted or not, by (repo, branch)."""
        db = TestingSessionLocal()
        try:
            project = db.query(Project).filter_by(project_id=self.project.project_id).one()
            observations = _collect_file_observations(db, TEST_USER, project)
            db.commit()
            return {(o.detail.repo, o.detail.branch): o.detail for o in observations}
        finally:
            db.close()

    @staticmethod
    def _drifted(summary):
        return sorted((d["repo"], d["branch"]) for d in summary["drifted_codeowners"])

    def _events(self, event_type):
        self.db.expire_all()
        return [json.loads(e.payload) for e in self.db.query(NotificationEvent).filter_by(event_type=event_type)]

    # ------------------------------------------------------------------
    # The project's file
    # ------------------------------------------------------------------

    def test_a_project_without_codeowners_has_none(self):
        response = client.get(f"/api/projects/{self.project.project_id}/codeowners")
        assert response.status_code == 200, response.text
        assert response.json() == {"codeowners": None}

    def test_the_first_save_is_a_new_local_file(self):
        response = self._save(DELIVERED)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["codeowners"]["status"] == "new"
        assert body["codeowners"]["git_hash"] is None
        assert body["codeowners"]["last_modified_by"] == TEST_USER
        assert "repo_id" not in body["codeowners"]
        # Create Pull Requests needs a draft project to deliver it.
        assert body["pr_state"] == "draft"

    def test_editing_a_delivered_file_is_a_local_change(self):
        self._delivered()

        body = self._save("* @octo/new-owners\n").json()

        assert body["codeowners"]["status"] == "committed_locally"
        assert body["codeowners"]["git_hash"] is None

    def test_saving_the_same_file_changes_nothing(self):
        record = self._delivered()

        body = self._save(DELIVERED).json()

        assert body["message"] == "No changes to save"
        assert body["codeowners"]["status"] == "synced_with_github"
        assert body["codeowners"]["git_hash"] == record.git_hash
        assert body["pr_state"] == "synced"

    def test_moving_the_file_is_a_local_change(self):
        self._delivered()

        body = self._save(DELIVERED, file_path="CODEOWNERS").json()

        assert body["codeowners"]["file_path"] == "CODEOWNERS"
        assert body["codeowners"]["git_hash"] is None

    def test_a_file_under_review_saves_for_its_open_pull_request(self):
        self._add_record(status="under_review")

        response = self._save("* @octo/new-owners\n")

        assert response.status_code == 200, response.text
        assert self._record().content == "* @octo/new-owners\n"
        assert self._record().status == "committed_locally"

    def test_an_unsupported_path_is_refused(self):
        assert self._save("* @a\n", file_path="src/CODEOWNERS").status_code == 400

    def test_every_path_github_reads_is_accepted(self):
        """The kind definition's paths, docs/CODEOWNERS included (#2367)."""
        for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            assert self._save("* @a\n", file_path=path).status_code == 200

    def test_a_viewer_reads_but_cannot_save(self, authenticate_client):
        self._add_record()
        viewer = TestClient(app)
        authenticate_client(viewer, VIEWER, TestingSessionLocal)

        read = viewer.get(f"/api/projects/{self.project.project_id}/codeowners")
        assert read.json()["codeowners"]["content"] == DELIVERED
        assert self._save("* @a\n", as_client=viewer).status_code == 403
        assert self._record().content == DELIVERED

    def test_save_reports_syntax_warnings(self):
        body = self._save("*.py\n* not-an-owner\n").json()
        assert len(body["validation_warnings"]) == 2

    # ------------------------------------------------------------------
    # A repository's copy on GitHub
    # ------------------------------------------------------------------

    def test_reads_a_repositorys_copy_alongside_the_projects(self):
        self.github.put(REPO_A, "* @from-github\n", path="CODEOWNERS")
        self._save("* @local\n")

        response = client.get(f"/api/repos/{REPO_A}/codeowners", params={"project_name": TEST_PROJECT})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["github"] == {"exists": True, "content": "* @from-github\n",
                                  "sha": _git_blob_sha("* @from-github\n"), "path": "CODEOWNERS"}
        assert body["local"]["content"] == "* @local\n"

    def test_reads_the_branch_and_path_asked_for(self):
        self.github.put(REPO_A, "* @release\n", branch="release/1.0")
        self.github.put(REPO_A, "* @root\n", branch="release/1.0", path="CODEOWNERS")

        response = client.get(f"/api/repos/{REPO_A}/codeowners", params={
            "project_name": TEST_PROJECT, "branch": "release/1.0", "path": PATH,
        })

        assert response.json()["github"]["content"] == "* @release\n"
        assert self.github.requests == [(REPO_A, PATH, "release/1.0", None)]

    def test_a_repository_outside_the_project_is_not_read(self):
        self.db.add(Repo(repo_name="other/orphan"))
        self.db.commit()

        response = client.get("/api/repos/other/orphan/codeowners", params={"project_name": TEST_PROJECT})

        assert response.status_code == 404
        assert self.github.requests == []

    # ------------------------------------------------------------------
    # Drift: the workflow rules
    # ------------------------------------------------------------------

    def test_a_delivered_file_github_still_holds_is_not_drift(self):
        self._delivered()

        summary = self._check()

        assert summary["drifted_codeowners"] == []
        assert summary["drift_status"] == "clean"

    def test_a_change_on_github_is_drift_on_that_repo_only(self):
        self._delivered()
        self.github.put(REPO_A, CHANGED)

        summary = self._check()

        assert self._drifted(summary) == [(REPO_A, "main")]
        assert summary["drift_count"] == 1
        assert summary["drift_status"] == "drifted"
        assert summary["drifted_codeowners"][0]["message"] == f"CODEOWNERS content differs between local and {REPO_A}"

    def test_a_drift_row_names_the_file_it_is_about(self):
        """The page acts on a CODEOWNERS row by its record, as it does on a workflow's by the workflow."""
        self._delivered()
        self.github.put(REPO_A, CHANGED)

        live = self._check()["drifted_codeowners"]
        stored = self._stored()["drifted_codeowners"]

        assert [row["file_id"] for row in live + stored] == [self._record().id] * 2

    def test_moving_the_file_locally_is_not_drift(self):
        """A check reads the file where it was delivered: reading the new path
        found nothing there and reported the delivered copy deleted."""
        self._delivered()

        assert self._save(DELIVERED, file_path="CODEOWNERS").status_code == 200

        assert self._drifted(self._check()) == []
        assert self._details()[(REPO_A, "main")].file_path == PATH

    def test_a_local_edit_does_not_hide_drift(self):
        """Saving CODEOWNERS used to resolve its drift and read every later
        difference as the edit pending sync, even when someone changed the
        delivered file on GitHub. GitHub still holds a copy ActionsManager never
        delivered, so it is still drift; the edit is the file's status."""
        self._delivered()
        self.github.put(REPO_A, CHANGED)
        assert self._drifted(self._check()) == [(REPO_A, "main")]

        body = self._save("* @octo/new-owners\n").json()

        assert body["codeowners"]["status"] == "committed_locally"
        assert self._drifted(self._stored()) == [(REPO_A, "main")]
        details = self._details()
        assert details[(REPO_A, "main")].message == f"CODEOWNERS content differs between local and {REPO_A}"
        # Nor does it make drift: repo B still holds what was delivered there.
        assert details[(REPO_B, "main")].has_drift is False
        assert details[(REPO_B, "main")].message == f"CODEOWNERS synchronized with {REPO_B}"
        assert self._drifted(self._check()) == [(REPO_A, "main")]

    def test_a_local_edit_equal_to_github_resolves_its_drift_at_once(self):
        """Loading the drifted GitHub version and saving it adopts that copy: drift
        there is resolved without a check, and the blob is what is delivered."""
        self._delivered()
        self.github.put(REPO_A, CHANGED)
        self._check()

        self._save(CHANGED)  # the GitHub version, loaded and saved

        stored = self._stored()
        assert stored["drifted_codeowners"] == []
        assert stored["drift_status"] == "clean"
        assert [(e["repo"], e["workflow_name"]) for e in self._events("drift.resolved")] == [(REPO_A, "CODEOWNERS")]
        assert self._row(REPO_A).delivered_sha == _git_blob_sha(CHANGED)
        assert self._record().git_hash is None
        details = self._details()
        assert details[(REPO_A, "main")].message == f"CODEOWNERS synchronized with {REPO_A}"
        assert details[(REPO_B, "main")].message == f"CODEOWNERS synchronized with {REPO_B}"

    def test_line_endings_are_not_drift_and_the_record_takes_github_s_blob(self):
        self._delivered()
        crlf = DELIVERED.replace("\n", "\r\n")
        self.github.put(REPO_A, crlf)
        self.github.put(REPO_B, crlf)

        assert self._drifted(self._check()) == []
        assert self._row(REPO_A).delivered_sha == _git_blob_sha(crlf)
        assert self._record().git_hash == _git_blob_sha(DELIVERED)

    def test_an_open_pr_carrying_the_file_is_under_review(self):
        record = self._delivered(status="under_review")
        self.github.put(REPO_A, "* @older\n")
        pr = ProjectPullRequest(
            project_id=self.project.project_id, repo_name=REPO_A, pr_number=7, pr_url="u",
            branch_name="actions-manager/cox-main", target_branch="main", pr_state="open", file_names=PATH,
        )
        self.db.add(pr)
        record_pr_carries(self.db, pr, record)

        details = self._details()

        assert details[(REPO_A, "main")].has_drift is False
        assert details[(REPO_A, "main")].message == f"CODEOWNERS under review in open PR for {REPO_A}"

    def test_a_deleted_file_is_drift_only_where_it_was_delivered(self):
        self._delivered()
        self.github.delete(REPO_A)

        summary = self._check()

        assert self._drifted(summary) == [(REPO_A, "main")]
        assert summary["drifted_codeowners"][0]["deleted_in_github"] is True

    def test_a_file_never_seen_on_a_branch_is_not_a_deletion(self):
        # Committed by a campaign whose PR never merged: a real hash, never on the branch.
        self._add_record(status="committed_locally")

        details = self._details()

        assert details[(REPO_A, "main")].has_drift is False
        assert details[(REPO_A, "main")].message == f"CODEOWNERS not delivered to {REPO_A} yet"

    def test_drift_is_per_branch(self):
        self.branches = ["main", "release/1.0"]
        self._add_record()
        for repo in (REPO_A, REPO_B):
            for branch in self.branches:
                self.github.put(repo, DELIVERED, branch=branch)
        self._check()
        self.github.put(REPO_B, "* @hotfix\n", branch="release/1.0")

        assert self._drifted(self._check()) == [(REPO_B, "release/1.0")]

    def test_a_different_file_where_it_was_never_delivered_is_not_drift(self):
        """A repository added after delivery, or one already holding its own
        CODEOWNERS: ActionsManager never delivered there, so the copy there is
        not drift from anything."""
        self._add_record()
        self.github.put(REPO_A, CHANGED)

        details = self._details()

        assert details[(REPO_A, "main")].has_drift is False
        assert details[(REPO_A, "main")].message == f"CODEOWNERS not delivered to {REPO_A} yet"

    def test_a_github_failure_is_unchecked_not_clean(self):
        self._delivered()
        self.github.put(REPO_A, CHANGED)
        self._check()
        self.github.status = 500

        summary = self._check()

        assert summary["unchecked_count"] == 2
        assert summary["drift_status"] == "check_failed"
        # The last known state stands: an unanswered check resolves nothing.
        assert self._drifted(self._stored()) == [(REPO_A, "main")]
        assert self._events("drift.resolved") == []

    def test_a_project_without_codeowners_checks_none(self):
        summary = self._check()

        assert summary["drifted_codeowners"] == []
        assert self.github.requests == []

    # ------------------------------------------------------------------
    # Drift: cost
    # ------------------------------------------------------------------

    def test_an_unchanged_branch_is_read_conditionally(self):
        self._delivered()
        self.github.requests.clear()

        self._check()

        assert self.github.requests == [
            (REPO_A, PATH, "main", _etag(DELIVERED)), (REPO_B, PATH, "main", _etag(DELIVERED)),
        ]

    def test_a_304_for_another_sha_fetches_the_content_once(self):
        self._delivered()
        # e.g. a delivery recorded without the blob GitHub went on to report
        record_delivered(self.db, self._record(), REPO_A, "main", sha="f" * 40)
        self.github.requests.clear()

        details = self._details()

        assert details[(REPO_A, "main")].has_drift is False
        assert self.github.requests[:2] == [
            (REPO_A, PATH, "main", _etag(DELIVERED)), (REPO_A, PATH, "main", None),
        ]

    def test_workflows_and_codeowners_share_one_branch_resolution(self):
        self._delivered()
        workflow = Workflow(workflow_name="ci", workflow_yaml="on: push", workflow_git_hash=None,
                            workflow_status="committed_locally")
        self.db.add(workflow)
        self.db.flush()
        self.db.add(ProjectWorkflow(project_id=self.project.project_id, workflow_id=workflow.workflow_id))
        self.db.commit()
        self.resolved.clear()

        with patch("workflows.fetch_workflow_tree", return_value=({}, None)):
            self._check()

        assert sorted(self.resolved) == [REPO_A, REPO_B]

    # ------------------------------------------------------------------
    # Drift: stored, counted and notified with workflow drift
    # ------------------------------------------------------------------

    def test_stored_drift_is_served_without_github(self):
        self._delivered()
        self.github.put(REPO_A, CHANGED)
        self._check()
        self.github.requests.clear()

        stored = self._stored()

        assert self._drifted(stored) == [(REPO_A, "main")]
        assert stored["drift_count"] == 1
        assert self.github.requests == []

    def test_the_project_load_seeds_codeowners_drift(self):
        self._delivered()
        self.github.put(REPO_A, CHANGED)
        self._check()

        response = client.get(f"/api/projects/{TEST_PROJECT}", params={"github_user": TEST_USER})

        assert response.status_code == 200, response.text
        assert response.json()["codeowners_drifted"] is True

    def test_drift_is_notified_once_and_its_resolution_too(self):
        self._delivered()
        self.github.put(REPO_A, CHANGED)
        self._check()
        self._check()

        detected = self._events("drift.detected")
        assert [(e["repo"], e["branch"], e["workflow_name"]) for e in detected] == [(REPO_A, "main", "CODEOWNERS")]

        self.github.put(REPO_A, DELIVERED)
        self._check()
        self._check()

        resolved = self._events("drift.resolved")
        assert [(e["repo"], e["workflow_name"]) for e in resolved] == [(REPO_A, "CODEOWNERS")]

    def test_the_sweep_checks_codeowners(self):
        self._delivered()
        self.github.put(REPO_B, CHANGED)
        db = TestingSessionLocal()
        try:
            project = db.query(Project).filter_by(project_id=self.project.project_id).one()

            drifted, unchecked, drifted_codeowners, _ = run_project_drift_check(db, TEST_USER, project)

            assert (drifted, unchecked) == ([], [])
            assert [(d.repo, d.branch) for d in drifted_codeowners] == [(REPO_B, "main")]
            assert (project.drift_status, project.drift_count) == ("drifted", 1)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def _merged_pr(self, file_names=PATH, workflow_names=None, campaign_id=None):
        pr = ProjectPullRequest(
            project_id=self.project.project_id, repo_name=REPO_A, pr_number=9, pr_url="u",
            branch_name="actions-manager/cox-main", target_branch="main", pr_state="merged",
            file_names=file_names, workflow_names=workflow_names, campaign_id=campaign_id,
        )
        self.db.add(pr)
        self.db.commit()
        # What a campaign and its merge record for a PR that carried the file;
        # the migration records the same for a standalone CODEOWNERS PR.
        if file_names == PATH or workflow_names == "CODEOWNERS":
            record_pr_merged(self.db, pr, self._record())
        return pr

    def _row(self, repo):
        """The project's CODEOWNERS on the record, on (repo, main)."""
        self.db.expire_all()
        return self.db.query(FileDelivery).filter(
            FileDelivery.codeowners_id.isnot(None),
            FileDelivery.repo_id == self.repos[repo].repo_id, FileDelivery.branch == "main",
        ).one()

    def _drift_on_both(self):
        self._delivered()
        self.github.put(REPO_A, CHANGED)
        self.github.put(REPO_B, CHANGED)
        self._check()

    def test_a_merged_pr_carrying_the_file_clears_its_drift_there(self):
        self._drift_on_both()

        _clear_drift_for_merged_pr(self.db, self.project, self._merged_pr())

        assert self._row(REPO_A).has_drift is False
        assert self._row(REPO_B).has_drift is True
        self.db.refresh(self.project)
        assert self.project.drift_count == 1

    def test_a_standalone_codeowners_pr_from_before_campaigns_counts(self):
        self._drift_on_both()

        _clear_drift_for_merged_pr(
            self.db, self.project, self._merged_pr(file_names=None, workflow_names="CODEOWNERS"),
        )

        assert self._row(REPO_A).has_drift is False

    def test_a_merged_pr_without_the_file_leaves_its_drift(self):
        self._drift_on_both()

        _clear_drift_for_merged_pr(
            self.db, self.project, self._merged_pr(file_names=None, workflow_names="ci"),
        )

        assert self._row(REPO_A).has_drift is True

    def test_a_merged_rollback_clears_its_drift_as_for_a_workflow(self):
        """CODEOWNERS used to leave a merged rollback's drift to the next check
        while workflows cleared theirs. A merged rollback delivered the blob it
        carried, which GitHub now holds, for every kind of file alike."""
        self._drift_on_both()
        original = ProjectPRCampaign(project_id=self.project.project_id, created_by=TEST_USER)
        self.db.add(original)
        self.db.flush()
        rollback = ProjectPRCampaign(project_id=self.project.project_id, created_by=TEST_USER,
                                     rollback_of_campaign_id=original.campaign_id)
        self.db.add(rollback)
        self.db.commit()

        _clear_drift_for_merged_pr(self.db, self.project, self._merged_pr(campaign_id=rollback.campaign_id))

        assert self._row(REPO_A).has_drift is False
        assert self._row(REPO_B).has_drift is True

    def test_a_merge_records_the_file_on_a_branch_no_check_saw(self):
        self._add_record(status="under_review")

        _clear_drift_for_merged_pr(self.db, self.project, self._merged_pr())

        assert self._row(REPO_A).delivered_at is not None

    # ------------------------------------------------------------------
    # Moving the file
    # ------------------------------------------------------------------

    def _campaign(self, github):
        """A campaign delivering the CODEOWNERS to repo-a; returns its PR."""
        [pr] = run_campaign(self.db, github, self.project, [REPO_A], user=TEST_USER, include_codeowners=True)
        return pr

    def _delivered_then_moved(self, github):
        """The file delivered to repo-a at .github/CODEOWNERS by a merged campaign, then moved to the root."""
        self._add_record(status="new")
        github.merge(self.db, self._campaign(github))
        assert self._save(DELIVERED, file_path="CODEOWNERS").status_code == 200
        self.db.expire_all()

    def test_moving_codeowners_removes_the_delivered_copy(self):
        github = CampaignGitHub()
        self._delivered_then_moved(github)

        pr = self._campaign(github)

        assert github.deletes == [(REPO_A, pr.branch_name, PATH)]
        assert github.paths(REPO_A, pr.branch_name) == {"CODEOWNERS"}

    def test_moving_onto_an_identical_copy_still_opens_the_pr(self):
        """The new path already holds this file, so writing it changes nothing.
        Removing the delivered copy is the change, and a PR has to carry it."""
        github = CampaignGitHub()
        self._delivered_then_moved(github)
        github.put_file(REPO_A, "CODEOWNERS", DELIVERED)

        pr = self._campaign(github)

        assert github.deletes == [(REPO_A, pr.branch_name, PATH)]
        github.merge(self.db, pr)
        assert github.paths(REPO_A) == {"CODEOWNERS"}
