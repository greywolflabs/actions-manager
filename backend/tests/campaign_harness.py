"""PR campaigns run the way Create Pull Requests runs them, against a fake GitHub.

GitHub is ``FakeGitHub.files``, ``{(repo, branch, path): content}``. A campaign
cuts its AM branch fresh from each target branch, as _create_or_get_am_branch
does, and its commits are contents API writes and deletes on that branch.
Merging a PR makes the target branch what its AM branch holds, and records the
outcome as the merge button does (``apply_pr_outcome``); ``press_merge`` and
``press_close`` press the buttons themselves, through their routes.

A delete answers as GitHub does: 404 for a file that is not there, 409 for a
SHA that is not the file's. ``calls`` is every GitHub call made, and
``timeouts`` what each ``requests`` call was given. The project's branches
resolve to ``branch_config``. A pull request is opened unless ``pr_refusal``
says why GitHub refuses it. ``secrets`` are each repository's Actions
secrets, listed by ``actions_settings`` and deleted into ``secrets_deleted``.
"""

import base64
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote, unquote, urlsplit

from fastapi import BackgroundTasks

import custom_files
import workflows as wf
from deliveries import blob_sha
from models import ProjectPullRequest

USER = "campaigner"
BRANCH = "main"
PR_BODY = "A campaign"


def _response(status, payload=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    response.text = ""
    response.headers = {}
    return response


def _repo_and_path(url):
    repo, path = re.match(r"/repos/([^/]+/[^/]+)/contents/(.+)", urlsplit(url).path).groups()
    return repo, unquote(path)


_SECRETS = re.compile(r"/repos/([^/]+/[^/]+)/actions/secrets(?:/([^/]+))?$")


class FakeGitHub:
    def __init__(self):
        self.files = {}
        self.deletes = []
        self.prs = 0
        self.calls = []
        self.timeouts = []
        self.branch_config = [BRANCH]
        self.pr_refusal = None
        self.secrets = {}
        self.secrets_deleted = []

    def put_file(self, repo, path, content, branch=BRANCH):
        self.files[(repo, branch, path)] = content

    def paths(self, repo, branch=BRANCH):
        return {path for (name, at, path) in self.files if (name, at) == (repo, branch)}

    def _write(self, repo, branch, path, content):
        unchanged = self.files.get((repo, branch, path)) == content
        self.files[(repo, branch, path)] = content
        return 204 if unchanged else 201, blob_sha(content)

    # The contents API, as workflows calls it.
    def get(self, url, *_args, params=None, **_kwargs):
        repo, path = _repo_and_path(url)
        ref = (params or {}).get("ref")
        self.calls.append(("GET", repo, ref, path))
        content = self.files.get((repo, ref, path))
        return _response(404) if content is None else _response(200, {"sha": blob_sha(content)})

    def put(self, url, json=None, timeout=None, **_kwargs):
        self.timeouts.append(timeout)
        repo, path = _repo_and_path(url)
        _status, sha = self._write(repo, json["branch"], path, base64.b64decode(json["content"]).decode())
        return _response(201, {"content": {"sha": sha}})

    def delete(self, url, json=None, timeout=None, **_kwargs):
        self.timeouts.append(timeout)
        secret = _SECRETS.match(urlsplit(url).path)
        if secret:
            self.secrets_deleted.append(secret.groups())
            return _response(204)
        repo, path = _repo_and_path(url)
        key = (repo, json["branch"], path)
        self.calls.append(("DELETE", *key))
        content = self.files.get(key)
        if content is None:
            return _response(404)
        if json.get("sha") != blob_sha(content):
            return _response(409, {"message": f"{path} does not match {json.get('sha')}"})
        self.deletes.append(key)
        del self.files[key]
        return _response(200)

    def actions_settings(self, url, *_args, **_kwargs):
        """GitHub's Actions secrets of a repository, as project deletion lists them; no variables or environments."""
        listed = _SECRETS.match(urlsplit(url).path)
        if not listed:
            return _response(200, {})
        return _response(200, {"secrets": [{"name": name} for name in self.secrets.get(listed.group(1), [])]})

    def branches(self, owner, repo, *_args, **_kwargs):
        self.calls.append(("BRANCHES", f"{owner}/{repo}", None, None))
        return list(self.branch_config)

    def default_branch(self, owner, repo, *_args, **_kwargs):
        self.calls.append(("DEFAULT_BRANCH", f"{owner}/{repo}", None, None))
        return BRANCH

    def put_workflow(self, owner, repo, workflow, project_code, am_branch, _headers, _repo_name, _user=None,
                     _db=None, use_prefix=True):
        path = f".github/workflows/{wf.format_workflow_name(workflow['name'], project_code, use_prefix)}"
        return self._write(f"{owner}/{repo}", am_branch, path, workflow["content"])

    # Branches and pull requests.
    def cut_branch(self, owner, repo, target_branch, project_code, *_args, **_kwargs):
        self.prs += 1
        am_branch = f"actions-manager/{project_code.lower()}/{self.prs}-{target_branch}"
        name = f"{owner}/{repo}"
        for path in self.paths(name, target_branch):
            self.files[(name, am_branch, path)] = self.files[(name, target_branch, path)]
        return am_branch, True, None

    def open_pr(self, owner, repo, am_branch, *_args, **_kwargs):
        if self.pr_refusal:
            return None, self.pr_refusal
        number = int(am_branch.rsplit("/", 1)[1].split("-", 1)[0])
        return {"number": number, "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
                "title": "campaign", "user": {"login": USER}, "body": PR_BODY}, None

    def merge_files(self, pr):
        """GitHub's side of merging *pr*: its target branch becomes what its AM branch holds."""
        for path in self.paths(pr.repo_name, pr.target_branch):
            del self.files[(pr.repo_name, pr.target_branch, path)]
        for path in self.paths(pr.repo_name, pr.branch_name):
            self.files[(pr.repo_name, pr.target_branch, path)] = self.files[(pr.repo_name, pr.branch_name, path)]

    def merge(self, db, pr):
        self.merge_files(pr)
        wf.apply_pr_outcome(db, pr, "merged")


@contextmanager
def github(fake, user=USER):
    tokens = {user: "token"}
    with patch("workflows.user_tokens", tokens), \
            patch.dict("auth.user_tokens", tokens), \
            patch("custom_files.user_tokens", tokens), \
            patch("workflows._ensure_preflight_allows_campaign"), \
            patch("workflows._resolve_branches_for_repo", side_effect=fake.branches), \
            patch("workflows.get_default_branch", side_effect=fake.default_branch), \
            patch("workflows._create_or_get_am_branch", side_effect=fake.cut_branch), \
            patch("workflows._fetch_branch_protection", return_value={"status": "none"}), \
            patch("workflows._update_workflow_to_github", side_effect=fake.put_workflow), \
            patch("workflows.github_get", side_effect=fake.get), \
            patch("workflows.requests.put", side_effect=fake.put), \
            patch("workflows.requests.delete", side_effect=fake.delete), \
            patch("workflows._check_existing_pr", return_value=None), \
            patch("workflows._campaign_pr_body", return_value=PR_BODY), \
            patch("workflows._create_pull_request", side_effect=fake.open_pr):
        yield fake


def _newest_pr_id(db):
    newest = db.query(ProjectPullRequest.pr_id).order_by(ProjectPullRequest.pr_id.desc()).first()
    return newest[0] if newest else 0


def _prs_after(db, pr_id):
    return db.query(ProjectPullRequest).filter(ProjectPullRequest.pr_id > pr_id).all()


def run_campaign(db, fake, project, repos, user=USER, **selection):
    """Create Pull Requests for *repos* as *user*; returns the PR rows it opened.

    *selection* is the request's, e.g. ``include_codeowners=True``.
    """
    newest = _newest_pr_id(db)
    with github(fake, user):
        wf.create_pull_requests(
            wf.CreatePullRequestsRequest(project_name=project.project_name, project_id=project.project_id,
                                         selected_repos=list(repos), **selection),
            BackgroundTasks(), db, github_user=user,
        )
    return _prs_after(db, newest)


def remove_workflow(db, fake, project, name, repos, direct=False, user=USER):
    """Delete workflow *name* from GitHub in *repos*, as Delete from ActionsManager and GitHub does.

    By PR Campaign, or committed directly. Returns (the route's answer, the PR rows it opened).
    """
    newest = _newest_pr_id(db)
    with github(fake, user):
        if direct:
            answer = wf._delete_workflow_directly(db, project, name, user, {}, list(repos))
        else:
            answer = wf._delete_workflow_via_campaign(db, project, name, user, {}, list(repos))
    return answer, _prs_after(db, newest)


def remove_custom_file(db, fake, project, cf, direct=False, user=USER):
    """Delete custom file *cf* from GitHub through its route; returns (the route's answer, the PR rows it opened)."""
    newest = _newest_pr_id(db)
    with github(fake, user):
        answer = custom_files.delete_custom_file(project.project_id, cf.id, db, account(),
                                                 scope="project_and_github", delivery="direct" if direct else "campaign")
    return answer, _prs_after(db, newest)


def account():
    return SimpleNamespace(github_user=USER)


def _pr_body(project_name, pr):
    return {"project_name": project_name, "repo_name": pr.repo_name, "pr_number": pr.pr_number}


def press_merge(client, fake, project_name, pr):
    """Merge *pr* with the merge button, signed in as *client* is; returns the route's response."""
    fake.merge_files(pr)
    with github(fake), \
            patch("workflows._merge_pull_request_with_fallback",
                  return_value=(_response(200, {"sha": "merged"}), "merge", None)), \
            patch("workflows._delete_actions_manager_branch", return_value=(True, None)):
        return client.put("/api/merge-pull-request", json=_pr_body(project_name, pr))


def press_close(client, fake, project_name, pr):
    """Close *pr* unmerged with the close button; returns the route's response."""
    with github(fake), patch("workflows.github_patch", return_value=_response(200, {"state": "closed"})):
        return client.patch("/api/close-pull-request", json=_pr_body(project_name, pr))


def delete_project(client, fake, db, project_name, **choice):
    """Delete the project from its Danger Zone; *choice* is the dialog's, e.g. ``remove_files=True``.

    Returns (the route's response, the PR rows it opened, read through *db*).
    """
    newest = _newest_pr_id(db)
    with github(fake):
        response = client.request("DELETE", f"/api/projects/{quote(project_name, safe='')}/enhanced",
                                  json={"project_name": project_name, **choice})
    db.expire_all()
    return response, _prs_after(db, newest)
