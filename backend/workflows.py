from fastapi import APIRouter, HTTPException, Request, Depends, BackgroundTasks

import requests
import base64
import functools
import json
import numbers
import re
import hmac
import hashlib
import os
import uuid
from datetime import datetime, timezone, timedelta
import ipaddress
import contextvars
from urllib.parse import quote, urlsplit
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError
from sqlalchemy.exc import SQLAlchemyError
from config import GITHUB_TIMEOUT_SECONDS
from database import SessionLocal, get_db
import auth as auth_module
from auth import user_tokens, get_github_api_endpoints
from models import Project, Workflow, ProjectWorkflow, Account, ProjectPullRequest, ProjectPRCampaign, Repo, ProjectRepo, WorkflowVersion, LinkedReusableWorkflow, RepoWorkflowOverride, CustomFile, Codeowners, WorkflowTreeCache, ProjectDisplayOrder
from pydantic import BaseModel, Field, field_validator
from typing import Annotated, Callable, List, NamedTuple, Optional
from authorization import PROJECT_DELETING, PROJECT_ROLE_LEVEL, find_project_by_name, project_role
from mode_validation import resolve_app_url, _host_is_loopback
from reusable_workflow_visibility import validate_reusable_workflow_link
from reusable_workflow_detection import declared_reusable
from workflow_templates import generate_template_set
from github_api_tracker import RateLimitExceeded, github_get, github_put, github_patch
from error_detail import failure_kind
import deliveries
from file_kinds import refuse_unless_accepted
from drift_notifications import (
    CODEOWNERS_NAME,
    DriftObservation,
    record_drift_transitions,
    record_drift_check_failed,
    clear_drift,
    resolve_drift_for_local_edit,
    recompute_project_drift_summary,
    drop_workflow_drift,
)
from campaign_notifications import (
    record_campaign_opened,
    record_campaign_pr_transition,
    record_campaign_status_transition,
)

NOT_AUTHENTICATED_DETAIL = "User not authenticated"
MISSING_WORKFLOW_NAME_DETAIL = "Missing workflow name"

class WorkflowSchema(BaseModel):
    name: str
    content: str
    original_name: Optional[str] = None  # Previous name when renaming a workflow

class SaveProjectWorkflowsRequest(BaseModel):
    project_name: str
    workflows: List[WorkflowSchema]
    rxworkflows: List[WorkflowSchema] = []

# Past a 32-bit integer column the database raises rather than finding nothing.
_MAX_PROJECT_ID = 2**31 - 1


class CreatePullRequestsRequest(BaseModel):
    project_name: str
    # Picks between same-named projects of different owners (#2240).
    project_id: Optional[int] = Field(default=None, gt=0, le=_MAX_PROJECT_ID)
    selected_repos: Optional[List[str]] = None  # If None, create PRs for all repos in project
    selected_workflows: Optional[List[str]] = None  # If None, include all workflows in the PR
    selected_reusable_workflows: Optional[List[str]] = None  # If None, no reusable workflows included
    selected_custom_file_ids: Optional[List[int]] = None  # None = all changed, [] = none
    # The project's CODEOWNERS goes to every repository in the campaign, as custom files do.
    include_codeowners: bool = False
    async_mode: Optional[bool] = False
    # Bounded here rather than in the UI — this endpoint is reachable directly.
    campaign_name: Optional[str] = Field(default=None, max_length=200)
    campaign_description: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("campaign_name", "campaign_description")
    @classmethod
    def _blank_is_unnamed(cls, value: Optional[str]) -> Optional[str]:
        # A whitespace-only name must fall back to the derived one rather than
        # storing an empty title that renders as a blank campaign card.
        cleaned = (value or "").strip()
        return cleaned or None


class RunPreflightRequest(BaseModel):
    project_name: str
    selected_workflows: Optional[List[str]] = None

class ClosePreflightValidationRequest(BaseModel):
    project_name: str
    cleanup_branch: bool = True

class MergePreflightValidationRequest(BaseModel):
    project_name: str
    cleanup_branch: bool = True

class MergePullRequestRequest(BaseModel):
    project_name: str
    repo_name: str
    pr_number: int

class ClosePullRequestRequest(BaseModel):
    project_name: str
    repo_name: str
    pr_number: int

class PRStatusResponse(BaseModel):
    repo_name: str
    pr_number: int
    pr_url: str
    pr_state: str
    branch_name: str
    target_branch: str
    created_at: str
    updated_at: str
    # Set when the PR originates from a linked project (cross-project visibility)
    source_project_name: Optional[str] = None
    mergeable: Optional[bool] = None
    mergeable_state: Optional[str] = None
    draft: Optional[bool] = None
    can_merge: Optional[bool] = None
    merge_block_reason: Optional[str] = None
    can_close: Optional[bool] = None
    close_block_reason: Optional[str] = None

class ProjectPRStatusResponse(BaseModel):
    project_state: str  # editing, pr_open
    pull_requests: List[PRStatusResponse]
    total_prs: int
    open_prs: int
    merged_prs: int
    closed_prs: int
    # Canonical workflow IDs that must remain ``under_review`` because an open
    # PR campaign still references them in *some* project sharing the workflow
    # (the owning RWX project or any linking caller project). Surfacing this
    # lets the frontend keep linked reusable workflow badges locked even when
    # the local project's PR view contains no open PRs of its own — a sibling
    # caller's campaign is invisible to ``open_prs`` here but must still hold
    # the lock. See ``_reusable_workflow_ids_locked_by_open_campaign``.
    locked_workflow_ids: List[int] = []

class PRHistoryItemResponse(BaseModel):
    """A single pull request record: the base of each PR row /api/project-pr-campaigns returns.

    ``source_project_name`` is only set when the PR was created by a *different*
    project that is linked to the queried project via a ``LinkedReusableWorkflow``
    relationship.  When the PR belongs directly to the queried project this field
    is ``None``.
    """
    pr_id: int
    repo_name: str
    pr_number: int
    pr_url: str
    pr_state: str  # "open", "merged" or "closed"
    branch_name: str
    target_branch: str
    title: Optional[str] = None
    author: Optional[str] = None
    body: Optional[str] = None
    workflow_names: Optional[str] = None
    created_at: str
    updated_at: str
    merged_at: Optional[str] = None
    closed_at: Optional[str] = None
    # Set when the PR originates from a linked project (cross-project visibility)
    source_project_name: Optional[str] = None
    mergeable: Optional[bool] = None
    mergeable_state: Optional[str] = None
    draft: Optional[bool] = None
    can_merge: Optional[bool] = None
    merge_block_reason: Optional[str] = None
    can_close: Optional[bool] = None
    close_block_reason: Optional[str] = None

class PRCampaignPRResponse(PRHistoryItemResponse):
    """A pull request row included in a derived PR Campaign."""
    actor: Optional[str] = None
    file_names: Optional[str] = None
    is_reusable_workflow_pr: bool = False


class PRCampaignResponse(BaseModel):
    """Derived campaign grouped from existing Actions Manager pull requests."""
    campaign_id: str
    campaign_name: str
    campaign_description: Optional[str] = None
    campaign_status: str
    project_name: str
    project_code: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None
    target_branches: List[str]
    # The project's configured branch mode ("default"/"pattern"), not the
    # branches it resolved to — those are target_branches.
    branch_option: Optional[str] = None
    workflow_names: List[str]
    custom_file_paths: List[str] = []
    repositories: List[str]
    open_count: int
    merged_count: int
    closed_count: int
    failed_count: int = 0
    completion_percentage: int
    pull_requests: List[PRCampaignPRResponse]
    # Creation-time snapshot. Empty for legacy campaigns created before it was recorded.
    target_repos: List[str] = []
    base_commits: dict = {}
    policy_version: dict = {}
    branch_protection: dict = {}
    target_pr_urls: dict = {}
    # Set only on a rollback campaign: the campaign it reverts, and what the user
    # chose to happen to ActionsManager's stored copy once it merges.
    rollback_of_campaign_id: Optional[int] = None
    rollback_am_action: Optional[str] = None


class PRCampaignsResponse(BaseModel):
    """Response envelope for the PR Campaigns endpoint."""
    campaigns: List[PRCampaignResponse]
    pull_requests: List[PRCampaignPRResponse]
    total_campaigns: int
    active_campaigns: int
    completed_campaigns: int
    open_prs: int
    merged_prs: int
    closed_prs: int
    repositories_affected: int

class NotModified(Exception):
    """GitHub answered 304 — the resource is unchanged since our stored ETag.

    Not an error: it is the cheap path. The caller replays its cached copy.
    Carried as an exception rather than a sentinel return so it cannot be
    mistaken for "no workflow files", the way a bare {} once was.
    """


class DriftCheckUnavailable(Exception):
    """GitHub could not tell us the current state of a repo.

    Distinct from "the file is not there": a revoked token, a rate limit or a
    5xx means the answer is unknown. Treating those as absence made every
    workflow in the repo look deleted, and treating them as "no drift" emitted
    a false drift.resolved for workflows that were still drifted.

    ``status_code`` carries what GitHub answered, as a field rather than only
    inside the message, so a caller can tell a missing repository (404) from an
    outage without parsing prose.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# Their messages say what GitHub answered about a repository, or when the
# user's GitHub API quota resets, which the user can act on. Any other failure
# is reported by kind (#2297).
_SHOWN_GITHUB_ERRORS = (DriftCheckUnavailable, RateLimitExceeded)


class DriftStatus(BaseModel):
    workflow_name: str
    # Which repo and branch this result is about. Carried as data because the
    # repo used to be recovered by substring-matching the repo name out of
    # ``message``, which cannot express a branch and breaks whenever the
    # message wording changes. Empty only for statuses that aren't tied to a
    # repo at all (deployment variables).
    repo: str = ""
    branch: str = ""
    has_drift: bool
    github_content: Optional[str] = None
    local_content: Optional[str] = None
    github_sha: Optional[str] = None
    local_sha: Optional[str] = None
    message: str
    drift_type: str = "workflow"  # workflow, reusable_workflow, deployment_vars
    # True when the check could not be completed. Such a status carries no
    # opinion about drift: it must never be persisted or notified on, because
    # "we don't know" is not "resolved".
    check_failed: bool = False
    # True when the workflow file is absent from GitHub. Previously this was
    # only distinguishable by substring-matching the message, so consumers
    # could not tell "deleted" from "empty file" — the UI rendered a blank
    # diff pane and offered to adopt content that does not exist.
    deleted_in_github: bool = False

class DriftDetectionRequest(BaseModel):
    project_name: str
    repo_names: List[str]
    check_deployment_vars: bool = False

class WorkflowDriftDetail(BaseModel):
    """Per-(workflow,repo,branch) drift detail."""
    workflow_id: int
    workflow_name: str
    workflow_filename: str
    repo: str
    branch: str
    has_drift: bool
    actionsmanager_yaml: Optional[str] = None
    github_yaml: Optional[str] = None
    actionsmanager_sha: Optional[str] = None
    github_sha: Optional[str] = None
    last_checked: str
    message: str
    # Scope-aware drift resolution metadata (issue: design-level drift fix)
    project_id: Optional[int] = None
    repo_id: Optional[int] = None
    is_shared_workflow: bool = False  # True if the workflow is associated with >1 repo in the project
    has_repo_override: bool = False   # True if a repo-specific override exists for this (project, repo, workflow)
    override_id: Optional[int] = None
    affected_repo_count: int = 0      # Other repos in the project that share this workflow without overrides
    affected_repos: List[str] = Field(default_factory=list)  # Repo names of the affected repos (without the source repo)
    source_repo_name: Optional[str] = None  # Convenience copy of ``repo`` to align with the spec
    # The check could not be completed (revoked token, rate limit, GitHub 5xx).
    # has_drift carries no meaning when this is True — the state is unknown, and
    # it is never persisted or notified on.
    check_failed: bool = False
    # The workflow file is absent from GitHub. github_yaml is None rather than
    # empty, and adopting GitHub's version is impossible — there is nothing to
    # adopt.
    deleted_in_github: bool = False


class CodeownersDriftDetail(BaseModel):
    """The project's CODEOWNERS on one (repo, branch); its WorkflowDriftDetail counterpart."""
    # The drifted file's record, so the page can act on this row as it does on a workflow's.
    file_id: Optional[int] = None
    file_path: str
    repo: str
    repo_id: Optional[int] = None
    branch: str
    has_drift: bool
    actionsmanager_sha: Optional[str] = None
    github_sha: Optional[str] = None
    last_checked: str
    message: str
    check_failed: bool = False
    deleted_in_github: bool = False


class CustomFileDriftDetail(CodeownersDriftDetail):
    """One of the project's custom files on one (repo, branch), checked as CODEOWNERS is."""
    custom_file_id: int


class AdoptGithubVersionRequest(BaseModel):
    """Body for POST /api/drift/adopt-github-version."""
    project_id: int
    repo_id: Optional[int] = None  # Optional if repo_name is supplied
    repo_name: Optional[str] = None
    # The branch whose drift the user is resolving; required (400 without it).
    # Falling back to the repo's default branch let a project delivering to
    # release/2.1 import main's version of the file instead.
    branch: Optional[str] = None
    workflow_id: int
    resolution_mode: str  # adopt_project_and_sync | adopt_local_only | create_repo_override
    delivery_mode: Optional[str] = "pr"  # only "pr": drift is never fixed by a direct commit
    target_repo_ids: Optional[List[int]] = None  # If None, default to all affected repos


class ProjectDriftSummary(BaseModel):
    """Project-level drift summary returned by GET /api/projects/{project_id}/drift."""
    project_id: int
    project_name: str
    drift_count: int
    drifted_workflows: List[WorkflowDriftDetail]
    # When the reported state was established — null when no check has ever
    # run. Not the time of this request: an empty list from a check that never
    # happened must not read as "verified clean just now".
    last_checked: Optional[str] = None
    # Workflow/repo pairs GitHub could not be queried about. Non-zero means the
    # drift picture is incomplete, so an empty drifted_workflows must not be
    # presented as "everything is in sync".
    unchecked_count: int = 0
    # Why the state above may be older than it looks — e.g. the background
    # sweep cannot check this project because its owner has no saved token.
    # Without this the timestamp just stops moving and nothing explains it.
    stale_reason: Optional[str] = None
    # The project's saved drift status (unknown/clean/drifted/check_failed) -
    # the value the Projects list badge reads. The status row renders from it:
    # an empty list here cannot tell "clean" from "some pairs could not be
    # checked", because unchecked_count is only known to a live check.
    drift_status: Optional[str] = None
    # The project's CODEOWNERS and custom files on each (repo, branch) where
    # they drifted, counted in drift_count alongside drifted_workflows.
    drifted_codeowners: List[CodeownersDriftDetail] = Field(default_factory=list)
    drifted_custom_files: List[CustomFileDriftDetail] = Field(default_factory=list)


class WorkflowDriftResponse(BaseModel):
    """Per-workflow drift detail returned by GET /api/workflows/{workflow_id}/drift."""
    workflow_id: int
    workflow_name: str
    workflow_filename: str
    has_drift: bool
    drift_details: List[WorkflowDriftDetail]
    last_checked: str


class ResolveWorkflowDriftRequest(BaseModel):
    """Body for POST /api/workflows/{workflow_id}/resolve-drift."""
    repo: str
    branch: str
    resolution: str  # "use_github" or "restore_actionsmanager"
    delivery_mode: Optional[str] = "pr"  # only "pr": drift is never fixed by a direct commit
    # The GitHub blob SHA the drift was computed against. When supplied, use_github
    # is refused with 409 if GitHub has moved on since, so a stale page cannot
    # adopt content the user never saw. Optional so existing callers keep working.
    expected_github_sha: Optional[str] = None


class BulkResolveDriftItem(BaseModel):
    workflow_id: int
    repo: str
    branch: str
    # See ResolveWorkflowDriftRequest.expected_github_sha.
    expected_github_sha: Optional[str] = None


class BulkResolveDriftRequest(BaseModel):
    """Body for POST /api/projects/{project_id}/drift/bulk-resolve.

    resolution/delivery_mode apply uniformly to every item in the batch -
    mixing resolutions per item isn't supported in one request.
    """
    items: List[BulkResolveDriftItem]
    resolution: str  # "use_github" or "restore_actionsmanager"
    delivery_mode: Optional[str] = "pr"  # only "pr": drift is never fixed by a direct commit


class BulkResolveDriftItemResult(BaseModel):
    workflow_id: int
    repo: str
    branch: str
    success: bool
    message: str
    pr_url: Optional[str] = None


class BulkResolveDriftResponse(BaseModel):
    success: bool
    results: List[BulkResolveDriftItemResult]


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize a stored (naive UTC) drift timestamp with an explicit UTC marker.

    Without the designator the browser reads the string as *local* time, so a
    check that just ran renders hours off — which reads as the previous
    automatic sweep's time rather than the manual one the user just triggered.

    Emitted as ``+00:00`` rather than ``Z``: both are valid ISO-8601 UTC and
    ``new Date()`` accepts either, and this is byte-identical to what the live
    ``refresh=true`` path already produces from ``datetime.now(timezone.utc)``,
    so the two drift surfaces cannot disagree on format.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def _cache_project_drift_summary(
    db: Session,
    project: Project,
    drift_status: str,
    drift_count: int,
    error_summary: Optional[str] = None,
) -> None:
    """Persist the latest project-level drift summary from a manual check."""
    project.drift_status = drift_status
    project.drift_count = max(int(drift_count or 0), 0)
    project.last_drift_check_at = datetime.now(timezone.utc)
    project.drift_error_summary = (error_summary or "").strip()[:500] or None
    # "check_failed" is the only outcome that didn't get a real answer from
    # GitHub; "clean" and "drifted" both mean the check succeeded, even when
    # the answer is "you have drift", so both reset the streak.
    if drift_status == "check_failed":
        project.drift_check_failure_count = (project.drift_check_failure_count or 0) + 1
    else:
        project.drift_check_failure_count = 0
    db.commit()


class WorkflowTemplateRequest(BaseModel):
    user_org: str
    build_type: str = "generic"
    project_code: Optional[str] = None

class TemplateResponse(BaseModel):
    template_type: str
    content: str
    name: str
    description: str

router = APIRouter()

# Error responses these endpoints can return, declared on each route so they
# appear in the OpenAPI schema (and so generated clients know about them).
# Codes raised inside shared helpers count too - the rule tracks the call.
_ERR_DRIFT_STALE = "The file on GitHub changed since drift was checked"
_ERR_ADOPT_NEEDS_BRANCH = "branch is required to adopt the GitHub version"

_ERROR_RESPONSES = {
    400: {"description": "Invalid request"},
    401: {"description": "Not authenticated"},
    403: {"description": "Access denied"},
    404: {"description": "Not found"},
    409: {"description": "Conflicts with the current state"},
    429: {"description": "GitHub API rate limit reached"},
    500: {"description": "Unexpected server error"},
    502: {"description": "Upstream GitHub request failed"},
}


def _responses(*codes: int) -> dict:
    """Subset of _ERROR_RESPONSES for a route's `responses=` parameter."""
    return {code: _ERROR_RESPONSES[code] for code in codes}


GITHUB_API_URL = "https://api.github.com"
ACCEPT_HEADER = "application/vnd.github+json"
X_API_VERSION = "2022-11-28"
# GITHUB_TIMEOUT_SECONDS is imported at the top of this module - it lives in
# config so auth.py and github_api_tracker.py can reach it too, since this
# module imports both and they cannot import back.
PROJECT_ERROR = "Project not found"
ACCOUNT_ERROR = "Account not found"
BRANCH_INFO_NOT_FOUND = "Branch information not found; branch not deleted"
PROGRESS_CREATING_BRANCH = "Creating branch"
PROGRESS_COMMITTING_FILES = "Committing files"
PROGRESS_OPENING_PR = "Opening PR"
PR_CREATION_FAILED = "Failed to create PR"
NO_WORKFLOWS_COMMITTED = "No workflows committed"
NOTHING_SELECTED_FOR_CAMPAIGN = "No workflows, custom files, or CODEOWNERS were selected"

# Outcome of deleting one file from one branch. "absent" is a success for the
# caller's intent (the file is not there) but produces no commit, so a caller
# about to open a PR has to tell the two apart.
DELETE_DELETED = "deleted"
DELETE_ABSENT = "absent"
DELETE_FAILED = "error"

# How a workflow deletion reaches GitHub. "campaign" runs a PR Campaign carrying
# the removal, so it is reviewed and tracked like every other delivery;
# "direct" commits it to every branch holding the file, unreviewed.
DELIVERY_CAMPAIGN = "campaign"
DELIVERY_DIRECT = "direct"
VALID_DELETE_DELIVERIES = (DELIVERY_CAMPAIGN, DELIVERY_DIRECT)

_ERR_WORKFLOW_NOT_FOUND = "Workflow not found"
_ERR_STALE_VALIDATION_URL = "Stored validation PR URL is invalid."
_ERR_VALIDATION_PR_MISSING = "Validation PR not found (may have been deleted)."


# GitHub webhook secret for PR event signature verification
GITHUB_PR_WEBHOOK_SECRET = os.getenv("GITHUB_PR_WEBHOOK_SECRET", "").strip()

_WORKFLOWS_DIR = ".github/workflows/"


# ✅ Helper function to format workflow names
def format_workflow_name(workflow_name: str, project_code: str, use_prefix: bool = True) -> str:
    """Format workflow name with or without project prefix"""
    if use_prefix:
        return f"AM_{project_code}_{workflow_name}.yml"
    else:
        # Ensure it has .yml extension
        if not workflow_name.endswith('.yml') and not workflow_name.endswith('.yaml'):
            return f"{workflow_name}.yml"
        return workflow_name


def _validate_workflow_name(workflow_name: Optional[str]) -> str:
    """Trim and validate a workflow name before it is stored or used as a path."""
    trimmed = (workflow_name or "").strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="Workflow name is required")

    stem = re.sub(r"\.(yml|yaml)$", "", trimmed, flags=re.IGNORECASE)
    if not stem:
        raise HTTPException(status_code=400, detail="Workflow name cannot be empty")
    if "/" in stem or "\\" in stem:
        raise HTTPException(status_code=400, detail="Workflow name cannot contain path separators")
    if ".." in stem:
        raise HTTPException(status_code=400, detail='Workflow name cannot contain ".." sequences')
    if stem.startswith(".") or stem.endswith("."):
        raise HTTPException(status_code=400, detail="Workflow name cannot start or end with a dot")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", stem):
        raise HTTPException(
            status_code=400,
            detail="Workflow name may only contain letters, numbers, dots, underscores, and hyphens",
        )
    if len(stem) > 100:
        raise HTTPException(status_code=400, detail="Workflow name is too long (max 100 characters)")
    return trimmed


def _normalize_reusable_workflow_name(name: Optional[str]) -> str:
    """Normalize a reusable workflow filename for case-insensitive equality.

    Strips a trailing ``.yml`` / ``.yaml`` extension and a single leading
    ``AM_{code}_`` project-prefix segment that ``format_workflow_name`` may
    have added.  Returns the lowercase stem.

    This is the inverse of ``format_workflow_name`` for matching purposes:
    the canonical ``Workflow.workflow_name`` stored in the DB is the raw
    stem, while the display name surfaced to the frontend (and round-tripped
    back as ``selected_reusable_workflows``) is prefixed and ``.yml``-suffixed
    by ``_load_linked_reusable_workflows``.  Normalising both sides allows
    selections of linked reusable workflows to match the underlying DB row.
    """
    normalized = (name or "").strip()
    normalized = re.sub(r"\.(yml|yaml)$", "", normalized, flags=re.IGNORECASE)
    # Strip a single leading AM_<code>_ prefix.  ``code`` is enforced to be
    # alphanumeric in the project schema, so a non-greedy [^_]+ match is safe
    # and will not accidentally consume legitimate name segments after the
    # first underscore-delimited prefix.
    normalized = re.sub(r"^AM_[^_]+_", "", normalized, flags=re.IGNORECASE)
    return normalized.lower()


def _strip_duplicated_project_prefix(db: Session, project_id: int, name: str) -> str:
    """Normalize an inbound workflow name to avoid duplicated project prefixes.

    For projects that use the ``AM_{project_code}_`` prefix mode, callers may
    accidentally submit a name that already carries the project prefix (for
    example, from a legacy client that didn't separate the locked prefix from
    the editable suffix in the rename UI).  This helper strips a single
    matching ``AM_{project_code}_`` prefix (case-insensitive) so that
    ``format_workflow_name`` does not later produce a duplicated
    ``AM_CODE_AM_CODE_name.yml`` filename.

    The trailing ``.yml`` / ``.yaml`` extension (if any) is preserved so that
    existing workflows whose canonical ``workflow_name`` includes the
    extension continue to round-trip unchanged.

    Returns the name with at most one matching project prefix removed. If the
    project cannot be loaded or does not use prefix mode, the input is
    returned unchanged so existing behaviour is preserved on error.
    """
    if not name:
        return name
    stripped = name.strip()
    if not stripped:
        return stripped

    try:
        project = db.query(Project).filter_by(project_id=project_id).first()
    except SQLAlchemyError:
        # On DB errors, preserve the input unchanged so existing behavior is maintained.
        return stripped
    if project is None:
        return stripped

    if not getattr(project, "use_prefix", False):
        return stripped
    project_code = getattr(project, "project_code", None)
    if not project_code:
        return stripped

    expected_prefix = f"AM_{project_code.upper()}_"
    # Case-insensitive single-prefix strip via re.sub (mirrors the pattern used
    # by _normalize_reusable_workflow_name above).  ``count=1`` ensures we never
    # strip more than one prefix even if the input pathologically contains two.
    stripped = re.sub(
        f"^{re.escape(expected_prefix)}",
        "",
        stripped,
        count=1,
        flags=re.IGNORECASE,
    )
    return stripped

# ✅ Database dependency


def _caller_can_see_project(db: Session, github_user: str, project: Project) -> bool:
    """Whether *github_user* may see *project*, decided by its id (see authorization.project_role)."""
    account = db.query(Account).filter_by(github_user=github_user).first()
    return project_role(db, account, project) is not None


# The project a request was made from, as X-Project-Id (#2240). Names are unique
# per owner only, and the page already holds the id that tells two owners'
# same-named projects apart. Set per request by main.py.
_request_project_hint: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "request_project_hint", default=None
)


def set_project_hint(header_value: Optional[str]):
    try:
        hint = int(header_value) if header_value else None
    except ValueError:
        hint = None
    return _request_project_hint.set(hint if hint and 0 < hint <= _MAX_PROJECT_ID else None)


def reset_project_hint(token) -> None:
    _request_project_hint.reset(token)


def project_hint() -> Optional[int]:
    return _request_project_hint.get()


def _find_project_by_name(db: Session, github_user: str, project_name: str, project_id: Optional[int] = None):
    """The project *project_name* means for *github_user*, or None (see authorization.find_project_by_name).

    *project_id*, or else the request's X-Project-Id, picks between same-named
    projects the caller can see (#2240).
    """
    account = db.query(Account).filter_by(github_user=github_user).first()
    return find_project_by_name(db, account, project_name, hint=project_id or project_hint())

def count_project_workflows(user: str, project_name: str) -> int:
    """Helper function to count regular workflows for a project"""
    db = SessionLocal()
    try:
        if user not in user_tokens:
            return 0
        
        project = _find_project_by_name(db, user, project_name)
        
        if not project:
            return 0
        # Defensive: treat unexpected/mocked objects as "not found"
        # to avoid returning non-integer Mock counts in unit tests.
        if not isinstance(getattr(project, "project_id", None), numbers.Integral):
            return 0
        
        # Count regular workflows (not reusable) for this project
        workflow_count = db.query(Workflow) \
            .join(ProjectWorkflow, Workflow.workflow_id == ProjectWorkflow.workflow_id) \
            .filter(
                ProjectWorkflow.project_id == project.project_id,
                Workflow.reusable_workflow == False
            ).count()
        
        return workflow_count
    except Exception as e:
        print(f"❌ Error counting workflows: {str(e)}")
        return 0
    finally:
        db.close()

def get_workflow_from_github(owner, repo, workflow_filename, token, default_branch):
    """Fetch a specific workflow file from one branch of a GitHub repository.

    ``default_branch`` is the branch to read (the name predates that) and is
    required. Falling back to the repo's default branch read a different file
    than the caller's whenever a project delivers elsewhere (#2300, #2301).
    """
    if not default_branch:
        raise ValueError("a branch is required to read a workflow from GitHub")
    headers = {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION
    }

    # Try to get the workflow file from default branch
    workflow_path = f".github/workflows/{workflow_filename}"
    file_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{workflow_path}?ref={quote(default_branch, safe='')}"
    
    response = requests.get(file_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
    
    if response.status_code == 200:
        file_data = response.json()
        # Decode base64 content
        content = base64.b64decode(file_data["content"]).decode('utf-8')
        sha = file_data.get("sha")  # Get the Git SHA of the file
        return {"content": content, "sha": sha}
    elif response.status_code == 404:
        return None  # File doesn't exist
    else:
        raise DriftCheckUnavailable(f"GitHub API error: {response.status_code}", status_code=response.status_code)

def fetch_workflow_tree(owner: str, repo: str, branch: str, token: str,
                        etag: Optional[str] = None) -> tuple:
    """
    List a branch's workflow files via the Git Trees API, optionally conditionally.

    Args:
        etag: A previously returned ETag. When supplied the request carries
            ``If-None-Match`` and GitHub answers 304 if nothing changed —
            **304s do not count against the rate limit**, so re-verifying an
            untouched branch is free. Raises NotModified in that case, leaving
            the caller to replay its cached mapping.

    Returns:
        tuple: ({filename: blob_sha}, etag) — the etag to store for next time.

    Raises:
        NotModified: unchanged since ``etag`` was issued.
        DriftCheckUnavailable: GitHub could not tell us what is there.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION
    }
    if etag:
        headers["If-None-Match"] = etag

    # Use Git Trees API to get all files in .github/workflows directory.
    # The branch is percent-encoded because '#' is legal in a git ref but ends
    # the URL path — GitHub then 404s, which this function reads as "no
    # workflow files here" and every workflow in the repo looks deleted.
    # ('/' needs no encoding: GitHub resolves the tree-ish server-side and
    # accepts it either way. Encoding it is harmless and verified against the
    # live API.)
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/trees/{quote(branch, safe='')}:.github/workflows"
    response = requests.get(url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

    if response.status_code == 304:
        raise NotModified(f"{owner}/{repo}@{branch} unchanged")

    if response.status_code in (404, 409):
        # Both are GitHub answering "there is nothing here", not failing to
        # answer. 404: the directory genuinely has no workflows. 409 "Git
        # Repository is empty.": a repository with zero commits, which is the
        # normal state of the reusable-workflow repo until the first delivery,
        # because _ensure_reusable_repo_exists creates it with auto_init=False.
        return {}, response.headers.get("ETag")

    if response.status_code != 200:
        # Anything else (401 revoked token, 403/429 rate limit, 5xx) means we do
        # not know what is in the repo. Returning {} here used to be
        # indistinguishable from "no workflows", which made every workflow in
        # the repo look deleted from GitHub.
        raise DriftCheckUnavailable(
            f"GitHub returned {response.status_code} listing workflows in "
            f"{owner}/{repo}@{branch}",
            status_code=response.status_code,
        )

    # Extract blob SHAs from the tree response
    tree_data = response.json()
    shas = {
        item["path"]: item["sha"]
        for item in tree_data.get("tree", [])
        if item["type"] == "blob"
    }
    return shas, response.headers.get("ETag")


def get_all_workflow_shas(owner: str, repo: str, branch: str, token: str) -> dict:
    """Unconditional listing of a branch's workflow file SHAs.

    Kept for callers with nowhere to cache an ETag. The drift path uses
    ``fetch_workflow_tree`` directly so it can go conditional.
    """
    shas, _etag = fetch_workflow_tree(owner, repo, branch, token)
    return shas

def get_default_branch(owner, repo, headers, user: str = None, db: Session = None):
    """Get the default branch for a repository.

    Raises DriftCheckUnavailable when GitHub does not tell us. This used to
    return "main" on any failure, which turned a revoked token or a rate limit
    into a *wrong answer* rather than an error: drift was then compared against
    a branch nobody chose, and reported "synchronized" against a file the
    project had never written to. Callers that genuinely want a best-effort
    guess now have to say so at the call site.
    """
    repo_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}"
    if user and db:
        response = github_get(repo_url, user, db, headers=headers)
    else:
        response = requests.get(repo_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

    if response.status_code != 200:
        raise DriftCheckUnavailable(
            f"GitHub returned {response.status_code} resolving the default branch of {owner}/{repo}",
            status_code=response.status_code,
        )

    default_branch = response.json().get("default_branch")
    if not default_branch:
        raise DriftCheckUnavailable(f"GitHub did not report a default branch for {owner}/{repo}")
    return default_branch

def verify_workflow_belongs_to_project(workflow_content, project_code, workflow_name):
    """
    Verify that a workflow found in GitHub actually belongs to the current project.
    This prevents custom workflows or workflows from other projects from being flagged as drifted.
    """
    if not workflow_content:
        return False
    
    # Check for project-specific patterns in the content
    # Actions Manager workflows typically contain the project code in various forms
    project_indicators = [
        f"AM_{project_code}",  # Project code appears in the content (most common)
        f"# Generated by Actions Manager for project {project_code}",  # Potential comment
        f"# Project: {project_code}",  # Alternative comment format
        f"AM_{project_code.lower()}",  # Lowercase version
    ]
    
    # Also check for patterns that suggest this is from a different project
    other_project_patterns = [
        "AM_" + code + "_" for code in ["ABCD", "EFGH", "IJKL", "MNOP", "QRST", "UVWX", "YZ12", "3456"] 
        if code != project_code
    ]
    
    # Count how many indicators are found
    indicators_found = 0
    for indicator in project_indicators:
        if indicator in workflow_content:
            indicators_found += 1
    
    # Check if this workflow seems to belong to a different project
    other_project_found = False
    for pattern in other_project_patterns:
        if pattern in workflow_content:
            other_project_found = True
            break
    
    # If we find indicators of a different project, definitely reject
    if other_project_found:
        return False
    
    # If we find project indicators, it's likely this workflow belongs to our project
    if indicators_found > 0:
        return True
    
    # If no clear indicators are found, we'll allow it for backward compatibility
    # but this is where we could be more strict in the future
    return True


def _validate_user_and_get_project(db: Session, user: str, project_name: str, project_id: Optional[int] = None):
    """
    Validate user authentication and retrieve project information.
    
    Returns:
        tuple: (token, project, project_code)
    
    Raises:
        HTTPException: If user is not authenticated or project is not found
    """
    if user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
    
    token = user_tokens[user]
    
    project = _find_project_by_name(db, user, project_name, project_id)
    
    if not project:
        raise HTTPException(status_code=404, detail=PROJECT_ERROR)
    
    project_code = project.project_code.upper()
    
    return token, project, project_code


def _get_project_workflows(db: Session, project):
    """
    Retrieve all workflows for a project and separate them by type.

    ``pending_delete`` rows are excluded. They are queued for removal from
    GitHub by an open campaign, so between that campaign merging and
    ``_reap_deleted_workflows`` destroying the row, a drift check would find the
    file legitimately gone and report it as "deleted from GitHub" — drift for a
    deletion ActionsManager itself asked for. Delivery reads these rows from
    ``_build_regular_workflow_results`` instead, which must keep seeing them.

    The split follows where each workflow is delivered, not its reusable label.
    A reusable workflow a standard project owns goes to that project's own
    repositories (``_owned_reusable_selected``), so it is checked there with the
    regular ones; checking it against ``_get_reusable_workflow_repo`` read a
    repository it was never delivered to (#2085). Only an RWX project's
    workflows live in the single reusable repository. Linked workflows are not
    in this set at all: the query goes through ``ProjectWorkflow``.

    Returns:
        tuple: (workflows checked in the project's repos, workflows checked in
        the reusable workflow repo)
    """
    workflows = db.query(Workflow) \
        .join(ProjectWorkflow, Workflow.workflow_id == ProjectWorkflow.workflow_id) \
        .filter(ProjectWorkflow.project_id == project.project_id,
                Workflow.pending_delete.is_(False)) \
        .all()

    if project.project_type != "rwx":
        return workflows, []

    regular_workflows = [w for w in workflows if not w.reusable_workflow]
    reusable_workflows = [w for w in workflows if w.reusable_workflow]

    return regular_workflows, reusable_workflows


def _normalize_yaml_for_comparison(yaml_content: str) -> str:
    """Normalize YAML content for drift comparison.

    Normalizes:
    - Line endings (CRLF → LF)
    - Trailing whitespace on each line
    - Ensures single trailing newline

    Does NOT reformat YAML structure or change content semantics.
    """
    if not yaml_content:
        return ""

    # Normalize line endings
    normalized = yaml_content.replace("\r\n", "\n").replace("\r", "\n")

    # Strip trailing whitespace from each line
    lines = normalized.split("\n")
    lines = [line.rstrip() for line in lines]

    # Join back and ensure single trailing newline
    normalized = "\n".join(lines)
    normalized = normalized.strip() + "\n" if normalized.strip() else ""

    return normalized


def _create_drift_status(workflow_name: str, has_drift: bool,
                        github_content: Optional[str], local_content: Optional[str],
                        github_sha: Optional[str], local_sha: Optional[str],
                        message: str, drift_type: str,
                        check_failed: bool = False,
                        deleted_in_github: bool = False,
                        repo: str = "", branch: str = "") -> DriftStatus:
    """Create a DriftStatus object with the given parameters."""
    return DriftStatus(
        workflow_name=workflow_name,
        repo=repo,
        branch=branch,
        has_drift=has_drift,
        github_content=github_content,
        local_content=local_content,
        github_sha=github_sha,
        local_sha=local_sha,
        message=message,
        drift_type=drift_type,
        check_failed=check_failed,
        deleted_in_github=deleted_in_github,
    )


def _find_blocking_reusable_workflow_pr(db: Session, workflow: "Workflow", current_project_id: int):
    """Find an open PR campaign owned by a *different* project that is already
    reviewing the given reusable workflow.

    Reusable workflows are canonical, shared assets identified by ``workflow_id``.
    Once any caller (or the owning RWX project) opens a PR campaign for the
    workflow, it is globally locked: no other project may open a second campaign
    until that PR is merged or closed.  This prevents conflicting or duplicate
    PR campaigns against the same source-of-truth workflow.

    Args:
        db: Database session
        workflow: The canonical reusable Workflow record being considered.
        current_project_id: The project attempting to open a PR campaign. PRs
            owned by this project are ignored (updating one's own campaign is
            allowed).

    Returns:
        A ``(ProjectPullRequest, Project)`` tuple describing the blocking PR and
        its owning project, or ``None`` when the workflow is free to review.
    """
    for pr in deliveries.open_prs_carrying(db, deliveries.WORKFLOW, workflow.workflow_id):
        if pr.project_id != current_project_id:
            return pr, db.query(Project).filter_by(project_id=pr.project_id).first()
    return None


def _reusable_workflow_ids_locked_by_open_campaign(db: Session, workflow_ids) -> set:
    """Return the subset of ``workflow_ids`` that must stay ``under_review``.

    Reusable workflows are canonical, shared assets identified by
    ``workflow_id``.  Their ``under_review`` lock is a *workflow-level* state
    that must persist globally until **every** open PR campaign referencing
    the workflow — from the owning RWX project or any linking caller project —
    is merged or closed.

    Status transitions triggered by a single project (merge/close endpoints,
    PR webhooks, page-load PR refreshes, local saves) only inspect that
    project's own PR rows, so an open campaign held by a *different* project
    would otherwise be invisible and the workflow could be wrongly unlocked.
    Callers use this helper to exclude such workflows from any
    ``under_review`` → ``synced_with_github``/``committed_locally`` downgrade.

    Read from the delivery record, which names the workflow by id, so neither
    a rename nor a same-named workflow elsewhere can move the lock.

    Args:
        db: Database session.
        workflow_ids: Candidate canonical reusable workflow IDs.

    Returns:
        The set of ``workflow_ids`` that still have an open PR campaign
        somewhere and therefore must remain locked.
    """
    return deliveries.workflow_ids_with_open_pending(db, workflow_ids)


def _reset_drift_lookup_caches(db: Session, project_id: int) -> None:
    """Drop the lookup caches a drift check memoizes on the Session.

    Scopes them to one check. The background sweep reuses a single Session for a
    whole batch, which can span minutes of GitHub I/O, so anything cached for the
    Session's lifetime outlives the check that built it: a repo attached
    mid-batch would be missing from its repo map and branch list.
    """
    db.info.pop("repo_id_by_name", None)
    db.info.pop(f"drift_branches:{project_id}", None)


class _Outcome(NamedTuple):
    """What the drift rule found, worded after the file's name."""
    has_drift: bool
    deleted: bool
    message: str


_PENDING_MERGE = _Outcome(False, False, "in open PR - pending merge to {repo}")
_DELETED = _Outcome(True, True, "was deleted from {repo}")
_NOT_DELIVERED = _Outcome(False, False, "not delivered to {repo} yet")
_SYNCED = _Outcome(False, False, "synchronized with {repo}")
_UNDER_REVIEW = _Outcome(False, False, "under review in open PR for {repo}")
_DIFFERS = _Outcome(True, False, "content differs between local and {repo}")


class _GitHubCopy:
    """GitHub's copy of a file on one (repo, branch), as a drift check sees it.

    ``sha`` is its blob, None when the file is missing. The content is read
    only when the rule needs it, and the read can find the file gone.
    """

    def __init__(self, sha: Optional[str], read=None, content: Optional[str] = None):
        self.sha = sha
        self.content = content
        self._read = read

    def read(self) -> Optional[str]:
        if self.content is None and self._read is not None:
            self.content, self._read = self._read(), None
            if self.content is None:
                self.sha = None
        return self.content


def _never() -> bool:
    return False


def _delivered(target, legacy) -> bool:
    return (target is not None and target.row.delivered_at is not None) or legacy()


def _drift_when_missing(target, legacy) -> _Outcome:
    if target is not None and target.pending:
        return _PENDING_MERGE
    return _DELETED if _delivered(target, legacy) else _NOT_DELIVERED


def _decide_drift(db: Session, targets: dict, key: tuple, github: _GitHubCopy, expected: str,
                  legacy=_never, path=None) -> _Outcome:
    """The drift rule every kind of file is judged by: is GitHub's copy on
    (repo, branch) what ActionsManager delivered there?

    *key* is ``(kind, file_id, repo_id, branch)`` in *targets*, the project's
    delivery record (``deliveries.targets_for``). *expected* is ActionsManager's
    content for that repository now. *legacy* says whether a workflow may have
    been delivered there before the record existed. *path* is where GitHub's
    copy was read, recorded with a match.

    =======  ==================================  =============================
    GitHub   first condition that holds          outcome
    =======  ==================================  =============================
    missing  an open PR carries it here          pending merge
    missing  delivered here                      drift: deleted
    missing  otherwise                           not delivered yet
    present  the blob delivered here             synchronized, nothing read
    present  an open PR carries it here          under review
    present  its content is *expected*           synchronized, and that blob
                                                 recorded as delivered
    present  not delivered here                  not delivered yet
    present  otherwise                           drift: content differs
    =======  ==================================  =============================

    A local edit only changes *expected*, which matters only once GitHub's
    copy is not the delivered one: an edit never hides drift and never makes
    it. The file's status is what shows the edit.
    """
    target = targets.get(key)
    if github.sha is None:
        return _drift_when_missing(target, legacy)
    if target is not None and github.sha == target.row.delivered_sha:
        return _SYNCED
    if target is not None and target.pending:
        return _UNDER_REVIEW
    content = github.read()
    if content is None:
        return _drift_when_missing(target, legacy)
    if _normalize_yaml_for_comparison(content) == _normalize_yaml_for_comparison(expected):
        row = deliveries.record_matched(db, *key, github.sha, path)
        if row is not None:
            targets.setdefault(key, deliveries.Target(row, False))
        return _SYNCED
    return _DIFFERS if _delivered(target, legacy) else _NOT_DELIVERED


def _drift_message(name: str, outcome: _Outcome, repo_name: str) -> str:
    return f"{name} {outcome.message.format(repo=repo_name)}"


def _drift_record(db: Session, project_id: Optional[int], targets: Optional[dict]) -> dict:
    """The delivery record a check reads: *targets* when the caller loaded it, else the project's."""
    if targets is not None:
        return targets
    return deliveries.targets_for(db, project_id) if db is not None and project_id is not None else {}


def _repo_ids_by_name(db: Session) -> dict:
    """Every repository's id by name, read once per check (see _reset_drift_lookup_caches)."""
    if db is None:
        return {}
    repo_id_by_name = db.info.get("repo_id_by_name")
    if repo_id_by_name is None:
        repo_id_by_name = {r.repo_name: r.repo_id for r in db.query(Repo).all()}
        db.info["repo_id_by_name"] = repo_id_by_name
    return repo_id_by_name


def _drift_key(db: Session, kind: str, file_id, repo_name: str, branch: str) -> tuple:
    """A file's key in the delivery record on (repo, branch), from any repository's name."""
    return (kind, file_id, _repo_ids_by_name(db).get(repo_name), branch or "")


def _delivered_sha(targets: dict, key: tuple) -> Optional[str]:
    target = targets.get(key)
    return target.row.delivered_sha if target is not None else None


def _expected_yaml(workflow, override=None) -> str:
    """What ActionsManager delivers of *workflow* to a repository: its override there, if one exists."""
    return (override.workflow_yaml if override is not None else workflow.workflow_yaml) or ""


def _get_repo_workflow_override(db: Session, project_id: int, repo_name: str, workflow_id: int):
    """Return the ``RepoWorkflowOverride`` row for (project, repo, workflow) or None.

    Drift detection calls this in a (workflow x repo) nested loop, so we cache
    the per-name repo-id map and the project's overrides in ``db.info`` to
    avoid an N+1 pattern. Subsequent calls within the same SQLAlchemy session
    do O(1) dict lookups.
    """
    if not db or project_id is None or workflow_id is None or not repo_name:
        return None

    repo_id = _repo_ids_by_name(db).get(repo_name)
    if repo_id is None:
        return None

    overrides_by_project = db.info.setdefault("repo_workflow_overrides_by_project", {})
    project_overrides = overrides_by_project.get(project_id)
    if project_overrides is None:
        project_overrides = {
            (o.repo_id, o.workflow_id): o
            for o in db.query(RepoWorkflowOverride).filter_by(project_id=project_id).all()
        }
        overrides_by_project[project_id] = project_overrides

    return project_overrides.get((repo_id, workflow_id))


def _resolve_drift_branches_for_repo(db: Session, project: "Project", repo_name: str,
                                     owner: str, repo: str, headers: dict, user: str) -> List[str]:
    """Which branches drift should check for one repo.

    Deliberately the *same* resolution delivery uses, so drift is measured
    against the branches ActionsManager actually writes to. Previously drift
    always read the repo's GitHub default branch, so a project delivering to
    ``release/*`` was compared against ``main`` and reported "synchronized"
    against a file it had never written.

    Without a project (direct callers that check a bare repo list) there is no
    branch configuration to honour, so this falls back to the repo's default
    branch — the same shape as ``_resolve_effective_target_branches``.
    """
    if project is None or db is None:
        return [get_default_branch(owner, repo, headers, user, db)]

    # Kept for the rest of the check (_reset_drift_lookup_caches starts the
    # next one afresh): its workflows and its CODEOWNERS are checked on the
    # same branches, and resolving them costs GitHub calls.
    resolved = db.info.setdefault(f"drift_branches:{project.project_id}", {})
    if repo_name not in resolved:
        cfg = resolve_branch_config_for_repo(db, project, repo_name)
        resolved[repo_name] = _resolve_branches_for_repo(
            owner, repo,
            cfg["branch_option"], cfg["branch_regex"], cfg["branch_max_age_days"],
            headers, user, db,
            recency_cache_repo=repo_name,
        )
    return list(resolved[repo_name])


def _get_tree_cache_row(db: Session, repo_name: str, branch: str):
    """The cached tree listing for one (repo, branch), or None."""
    if db is None:
        return None
    repo = db.query(Repo).filter(Repo.repo_name == repo_name).first()
    if repo is None:
        return None
    return (
        db.query(WorkflowTreeCache)
        .filter(WorkflowTreeCache.repo_id == repo.repo_id,
                WorkflowTreeCache.branch == branch)
        .first()
    )


def _store_tree_cache(db: Session, repo_name: str, branch: str, shas: dict, etag: Optional[str]) -> None:
    """Remember a listing and its ETag so the next check can go conditional."""
    if db is None or not etag:
        return
    repo = db.query(Repo).filter(Repo.repo_name == repo_name).first()
    if repo is None:
        return
    row = _get_tree_cache_row(db, repo_name, branch)
    if row is None:
        row = WorkflowTreeCache(repo_id=repo.repo_id, branch=branch)
        db.add(row)
    row.etag = etag
    row.sha_map_json = json.dumps(shas)
    row.fetched_at = datetime.now(timezone.utc)
    db.commit()


def _fetch_tree_using_cache(db: Session, repo_name: str, owner: str, repo: str,
                            branch: str, token: str) -> dict:
    """Listing for one (repo, branch), conditional on a stored ETag when we have one.

    A 304 means nothing changed, so the cached mapping is replayed and the call
    costs nothing against the rate limit. If a 304 arrives with no usable cached
    mapping the request is retried unconditionally — replaying an empty map
    would report every workflow in the repo as deleted.
    """
    row = _get_tree_cache_row(db, repo_name, branch)
    etag = row.etag if row else None

    try:
        shas, new_etag = fetch_workflow_tree(owner, repo, branch, token, etag=etag)
    except NotModified:
        if row is not None and row.sha_map_json:
            print(f"🟢 {repo_name}@{branch} unchanged (304, no rate-limit cost)")
            return json.loads(row.sha_map_json)
        print(f"⚠️ {repo_name}@{branch} returned 304 with nothing cached — refetching")
        shas, new_etag = fetch_workflow_tree(owner, repo, branch, token)

    _store_tree_cache(db, repo_name, branch, shas, new_etag)
    return shas


def _prefetch_workflow_shas_per_repo(repo_names: List[str], token: str, db: Session = None,
                                     project: "Project" = None, user: str = None) -> dict:
    """Batch-fetch workflow file SHAs via the Git Trees API, keyed by (repo, branch).

    One Trees call per (repo, branch) rather than per workflow, and that call is
    conditional on a stored ETag — an untouched branch answers 304 and costs
    nothing against the rate limit.

    A branch whose listing fails maps to None ("unknown"), never {} ("no
    workflow files") — callers turn None into a check_failed status, whereas {}
    would report every workflow in that repo as deleted from GitHub.
    """
    repo_sha_cache: dict = {}
    for repo_name in repo_names:
        if "/" not in repo_name:
            continue
        owner, repo = repo_name.split("/", 1)
        headers = {
            "Authorization": f"token {token}",
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION
        }
        try:
            branches = _resolve_drift_branches_for_repo(db, project, repo_name, owner, repo, headers, user)
        except Exception as e:
            # Branch resolution itself failed, so we don't know what to compare
            # against. Record one unknown entry so the repo reports check_failed
            # rather than silently falling back to a branch nobody chose.
            print(f"⚠️ Could not resolve branches for {repo_name}: {e}")
            repo_sha_cache[(repo_name, "")] = None
            continue

        for branch in branches:
            try:
                all_workflow_shas = _fetch_tree_using_cache(db, repo_name, owner, repo, branch, token)
                repo_sha_cache[(repo_name, branch)] = all_workflow_shas
                print(f"🔍 {len(all_workflow_shas)} workflow file SHAs for {repo_name}@{branch}")
            except Exception as e:
                print(f"⚠️ Could not list workflows in {repo_name}@{branch}: {e}")
                repo_sha_cache[(repo_name, branch)] = None
    return repo_sha_cache


def _workflow_filename_on(db: Session, targets: dict, workflow, repo_name: str, branch: str, filename: str) -> str:
    """The name *workflow* is read by in (repo, branch)'s listing: the delivered file's, else *filename*.

    The listing holds the names under ``.github/workflows``, so a local rename
    costs no extra read.
    """
    target = targets.get(_drift_key(db, deliveries.WORKFLOW, workflow.workflow_id, repo_name, branch))
    path = deliveries.path_on(target.row if target is not None else None, _WORKFLOWS_DIR + filename)
    return path.rsplit("/", 1)[-1]


def _delivered_filename(db: Session, workflow, repo_name: str, branch: str, filename: str) -> str:
    """*workflow*'s file name on (repo, branch): the delivered file's, else *filename*.

    Drift after a local rename is reported where the file was delivered, so
    adopting GitHub's copy reads it there.
    """
    path = deliveries.delivered_path(db, deliveries.WORKFLOW, workflow.workflow_id, repo_name, branch)
    return (path or _WORKFLOWS_DIR + filename).rsplit("/", 1)[-1]


class _WorkflowPlace(NamedTuple):
    """One workflow file on one (repo, branch) a drift check reads."""
    repo_name: str
    owner: str
    repo: str
    branch: str
    filename: str
    token: str


class _ForeignWorkflow(Exception):
    """The file at a workflow's path belongs to another project (verify_workflow_belongs_to_project)."""


def _read_own_workflow(place: _WorkflowPlace, project_code: str, workflow_name: str) -> Optional[str]:
    """GitHub's content at *place*, or None when the file is gone."""
    data = get_workflow_from_github(place.owner, place.repo, place.filename, place.token, default_branch=place.branch)
    if data is None:
        return None
    if not verify_workflow_belongs_to_project(data["content"], project_code, workflow_name):
        raise _ForeignWorkflow()
    return data["content"]


def _workflow_label(drift_type: str) -> str:
    return "Reusable workflow" if drift_type == "reusable_workflow" else "Workflow"


def _workflow_check_failed(workflow, expected: str, place: _WorkflowPlace, drift_type: str,
                           message: str) -> DriftStatus:
    """A workflow GitHub could not tell us about on *place*: no opinion about drift."""
    return _create_drift_status(
        workflow_name=workflow.workflow_name, has_drift=False, github_content=None, local_content=expected,
        github_sha=None, local_sha=deliveries.blob_sha(expected), message=message, drift_type=drift_type,
        check_failed=True, repo=place.repo_name, branch=place.branch,
    )


def _judge_workflow(db: Session, workflow, expected: str, place: _WorkflowPlace, github_sha: Optional[str],
                    targets: dict, project_code: str, drift_type: str) -> Optional[DriftStatus]:
    """Drift-check one workflow on *place* by the rule, from the blob SHA the listing gave.

    None when the file there is another project's, which is not this
    workflow's drift either way.
    """
    key = _drift_key(db, deliveries.WORKFLOW, workflow.workflow_id, place.repo_name, place.branch)
    github = _GitHubCopy(github_sha, functools.partial(_read_own_workflow, place, project_code, workflow.workflow_name))
    try:
        outcome = _decide_drift(db, targets, key, github, expected,
                                legacy=lambda: _may_be_delivered_unrecorded(db, workflow, place.repo_name),
                                path=_WORKFLOWS_DIR + place.filename)
    except _ForeignWorkflow:
        print(f"⚠️ Skipping workflow {workflow.workflow_name} in {place.repo_name} - doesn't belong to project {project_code}")
        return None
    if outcome.has_drift:
        print(f"✅ Drift detected for {_workflow_label(drift_type).lower()} '{workflow.workflow_name}' "
              f"in {place.repo_name}@{place.branch}: {outcome.message.format(repo=place.repo_name)}")
    return _create_drift_status(
        workflow_name=workflow.workflow_name, has_drift=outcome.has_drift,
        github_content=github.content, local_content=expected,
        github_sha=github.sha, local_sha=deliveries.blob_sha(expected),
        message=_drift_message(_workflow_label(drift_type), outcome, place.repo_name),
        drift_type=drift_type, deleted_in_github=outcome.deleted, repo=place.repo_name, branch=place.branch,
    )


def _check_regular_workflow_in_repo(db: Session, workflow, repo_name: str, owner: str, repo: str,
                                    formatted_workflow_name: str, workflow_shas: dict, project_code: str,
                                    token: str, project_id: int, branch: str = "",
                                    targets: Optional[dict] = None) -> Optional[DriftStatus]:
    """Drift-check one workflow against one (repo, branch), from the prefetched listing of its workflows.

    The expected content is the repo's override when one exists, otherwise the
    shared project workflow: a repo with an override is compared against the
    override, so it does not constantly re-report drift.
    """
    override = _get_repo_workflow_override(db, project_id, repo_name, workflow.workflow_id)
    expected = _expected_yaml(workflow, override)
    targets = targets if targets is not None else {}
    filename = _workflow_filename_on(db, targets, workflow, repo_name, branch, formatted_workflow_name)
    place = _WorkflowPlace(repo_name, owner, repo, branch, filename, token)

    if workflow_shas is None:
        # We could not list this repo's workflows at all, so we do not know
        # whether the file is missing or we simply couldn't see it.
        return _workflow_check_failed(workflow, expected, place, "workflow",
                                      f"Could not check {repo_name} — GitHub did not return its workflows")
    try:
        return _judge_workflow(db, workflow, expected, place, workflow_shas.get(filename),
                               targets, project_code, "workflow")
    except Exception as e:
        print(f"⚠️ Error checking workflow {workflow.workflow_name} in {repo_name}: {e}")
        # A failed check used to be recorded as clean, which emitted
        # drift.resolved for workflows that were still drifted.
        return _workflow_check_failed(workflow, expected, place, "workflow",
                                      f"Could not check {repo_name}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")


def _process_regular_workflows(db: Session, regular_workflows: List, repo_names: List[str],
                               project_code: str, token: str, use_prefix: bool = True, project_id: int = None,
                               project: "Project" = None, user: str = None,
                               targets: Optional[dict] = None) -> List[DriftStatus]:
    """
    Process regular workflows for drift detection across multiple repositories.
    Uses Git Trees API for batch SHA comparison to minimize API calls.

    Yields one status per (workflow, repo, branch): a project can deliver the
    same workflow to several branches, and each is independently drifted or not.
    ``targets`` is the project's delivery record, loaded here when not given.

    Returns:
        List[DriftStatus]: List of drift statuses for regular workflows
    """
    drift_results = []

    if not regular_workflows:
        return drift_results

    targets = _drift_record(db, project_id, targets)
    repo_sha_cache = _prefetch_workflow_shas_per_repo(repo_names, token, db, project, user)

    # Now process each workflow
    for workflow in regular_workflows:
        # Skip workflows with empty or null names
        if not workflow.workflow_name or not workflow.workflow_name.strip():
            print(f"⚠️ Skipping workflow with empty name (ID: {workflow.workflow_id})")
            continue

        formatted_workflow_name = format_workflow_name(workflow.workflow_name, project_code, use_prefix)

        # Check each (repo, branch) this project delivers to
        for (repo_name, branch), workflow_shas in repo_sha_cache.items():
            if "/" not in repo_name or repo_name not in repo_names:
                continue

            owner, repo = repo_name.split("/", 1)

            drift_status = _check_regular_workflow_in_repo(
                db, workflow, repo_name, owner, repo, formatted_workflow_name,
                workflow_shas, project_code, token, project_id, branch, targets,
            )
            if drift_status:
                drift_results.append(drift_status)

    return drift_results


def _project_codeowners(db: Session, project_id: int) -> Optional[Codeowners]:
    return db.query(Codeowners).filter(Codeowners.project_id == project_id).first()


def _read_file_on_branch(owner: str, repo: str, path: str, branch: str, token: str,
                         etag: Optional[str] = None) -> tuple:
    """The file at *path* on *branch*: ({"sha", "content"} or None when absent, etag).

    Conditional on *etag* when given, raising NotModified on GitHub's 304,
    which costs no rate limit. Raises DriftCheckUnavailable when GitHub cannot
    say what is there; that is never reported as an absent file.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION,
    }
    if etag:
        headers["If-None-Match"] = etag
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}?ref={quote(branch, safe='')}"
    response = requests.get(url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
    if response.status_code == 304:
        raise NotModified(f"{owner}/{repo}@{branch} {path} unchanged")
    if response.status_code in (404, 409):
        # As in fetch_workflow_tree: no such file, or an empty repository.
        return None, None
    if response.status_code != 200:
        raise DriftCheckUnavailable(
            f"GitHub returned {response.status_code} reading {path} in {owner}/{repo}@{branch}",
            status_code=response.status_code,
        )
    data = response.json()
    content = base64.b64decode(data.get("content") or "").decode("utf-8")
    return {"sha": data.get("sha"), "content": content}, response.headers.get("ETag")


def _fetch_file_on_branch(target, owner: str, repo: str, path: str, branch: str, token: str) -> tuple:
    """GitHub's copy of a file read on its own on *branch*, conditional on the read *target*'s row last stored.

    Returns (``_GitHubCopy``, ETag to store). A 304 replays the stored SHA and
    keeps the stored ETag, so an untouched branch costs nothing; the content is
    then read only if the rule needs it, as workflow drift does after its tree
    listing. A 304 with no stored SHA refetches rather than reading as an
    absent file.
    """
    row = target.row if target is not None else None
    try:
        found, etag = _read_file_on_branch(owner, repo, path, branch, token, row.etag if row else None)
    except NotModified:
        if row is not None and row.github_sha:
            def reread():
                found, _ = _read_file_on_branch(owner, repo, path, branch, token)
                return found["content"] if found else None
            return _GitHubCopy(row.github_sha, reread), deliveries.UNSET
        found, etag = _read_file_on_branch(owner, repo, path, branch, token)
    if found is None:
        return _GitHubCopy(None), etag
    return _GitHubCopy(found["sha"], content=found["content"]), etag


class _File(NamedTuple):
    """A file a drift check reads on its own on each (repo, branch): the project's CODEOWNERS or a custom file.

    ``detail`` builds its CodeownersDriftDetail or CustomFileDriftDetail.
    ``from_record`` files are read only where the delivery record has a row
    for them. Anywhere else they are "not delivered yet" whatever GitHub
    holds, so reading would only spend rate limit.
    """
    kind: str
    file_id: int
    name: str
    path: str
    content: str
    detail: Callable
    from_record: bool = False


class _CheckRun(NamedTuple):
    """What every read of one drift check shares; ``recorded`` is each (kind, file_id, repo_id) on the record."""
    project: Project
    user: str
    token: str
    now_iso: str
    targets: dict
    recorded: frozenset


def _codeowners_file(record: Codeowners) -> _File:
    return _File(deliveries.CODEOWNERS, record.id, CODEOWNERS_NAME, record.file_path, record.content,
                 functools.partial(CodeownersDriftDetail, file_id=record.id))


def _custom_file(cf: CustomFile) -> _File:
    return _File(deliveries.CUSTOM_FILE, cf.id, cf.file_path, cf.file_path, cf.file_content or "",
                 functools.partial(CustomFileDriftDetail, custom_file_id=cf.id, file_id=cf.id), from_record=True)


def _single_files(db: Session, project: Project, kind: Optional[str] = None) -> List[_File]:
    """The project's files read one by one, of *kind* when given; not a custom file on its way out."""
    files = []
    record = _project_codeowners(db, project.project_id) if kind in (None, deliveries.CODEOWNERS) else None
    if record is not None:
        files.append(_codeowners_file(record))
    if kind in (None, deliveries.CUSTOM_FILE):
        files += [_custom_file(cf) for cf in db.query(CustomFile).filter(
            CustomFile.project_id == project.project_id, CustomFile.pending_delete.is_(False),
        ).order_by(CustomFile.id)]
    return files


def _reads_in_repo(run: _CheckRun, file: _File, repo_id) -> bool:
    return not file.from_record or (file.kind, file.file_id, repo_id) in run.recorded


def _reads_on_branch(run: _CheckRun, file: _File, repo_id, branch: str) -> bool:
    return not file.from_record or (file.kind, file.file_id, repo_id, branch) in run.targets


def _unchecked_file(run: _CheckRun, file: _File, repo: Repo, branch: str, reason: str) -> DriftObservation:
    """*file* on (repo, branch) when GitHub could not say what is there: no opinion about drift."""
    detail = file.detail(file_path=file.path, repo=repo.repo_name, repo_id=repo.repo_id, branch=branch,
                         last_checked=run.now_iso, has_drift=False, check_failed=True,
                         message=f"Could not check {repo.repo_name}: {reason}")
    return DriftObservation(file.kind, file.file_id, file.name, detail)


def _check_file_on_branch(db: Session, run: _CheckRun, file: _File, repo: Repo, branch: str) -> DriftObservation:
    """Drift-check one file on one (repo, branch) by the rule workflows are judged by."""
    key = (file.kind, file.file_id, repo.repo_id, branch)
    target = run.targets.get(key)
    path = deliveries.path_on(target.row if target is not None else None, file.path)
    owner, name = repo.repo_name.split("/", 1)
    try:
        github, etag = _fetch_file_on_branch(target, owner, name, path, branch, run.token)
        outcome = _decide_drift(db, run.targets, key, github, file.content, path=path)
    except Exception as e:
        print(f"⚠️ Error checking {file.name} in {repo.repo_name}@{branch}: {type(e).__name__}")
        return _unchecked_file(run, file, repo, branch, failure_kind(e, _SHOWN_GITHUB_ERRORS))
    detail = file.detail(
        file_path=path, repo=repo.repo_name, repo_id=repo.repo_id, branch=branch, last_checked=run.now_iso,
        has_drift=outcome.has_drift, deleted_in_github=outcome.deleted, github_sha=github.sha,
        actionsmanager_sha=_delivered_sha(run.targets, key),
        message=_drift_message(file.name, outcome, repo.repo_name),
    )
    return DriftObservation(file.kind, file.file_id, file.name, detail, etag)


def _check_files_in_repo(db: Session, run: _CheckRun, files: List[_File], repo: Repo) -> List[DriftObservation]:
    """Each file on every branch the project delivers this repo's files to, resolved as workflow drift resolves them."""
    files = [file for file in files if _reads_in_repo(run, file, repo.repo_id)]
    if not files or "/" not in (repo.repo_name or ""):
        return []
    owner, name = repo.repo_name.split("/", 1)
    headers = {
        "Authorization": f"token {run.token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION,
    }
    try:
        branches = _resolve_drift_branches_for_repo(db, run.project, repo.repo_name, owner, name, headers, run.user)
    except Exception as e:
        reason = failure_kind(e, _SHOWN_GITHUB_ERRORS)
        return [_unchecked_file(run, file, repo, "", reason) for file in files]
    return [
        _check_file_on_branch(db, run, file, repo, branch)
        for branch in branches for file in files if _reads_on_branch(run, file, repo.repo_id, branch)
    ]


def _collect_file_observations(db: Session, user: str, project: Project,
                               targets: Optional[dict] = None) -> List[DriftObservation]:
    """Check the project's CODEOWNERS and custom files on every (repo, branch) it delivers them to.

    By the rule workflows are judged by (``_decide_drift``), so they can never
    disagree. One conditional read per (file, repo, branch), so a branch
    nobody touched costs no rate limit. ``targets`` is the project's delivery
    record, loaded here when not given.
    """
    files = _single_files(db, project)
    if not files:
        return []
    token = user_tokens.get(user)
    if not token:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
    targets = _drift_record(db, project.project_id, targets)
    run = _CheckRun(project, user, token, datetime.now(timezone.utc).isoformat(), targets,
                    frozenset(key[:3] for key in targets))
    repos = db.query(Repo).join(ProjectRepo).filter(ProjectRepo.project_id == project.project_id).all()
    observations: List[DriftObservation] = []
    for repo in repos:
        observations.extend(_check_files_in_repo(db, run, files, repo))
    return observations


def custom_file_on_github(db: Session, cf: CustomFile, repo: Repo, branch: str, token: str) -> tuple:
    """GitHub's copy of *cf* on (repo, branch), read where its drift check reads it.

    Returns ({"sha", "content"} or None when absent, the path read). One
    contents call; raises DriftCheckUnavailable when GitHub cannot say.
    """
    target = deliveries.targets_for(db, cf.project_id).get((deliveries.CUSTOM_FILE, cf.id, repo.repo_id, branch))
    path = deliveries.path_on(target.row if target is not None else None, cf.file_path)
    owner, name = repo.repo_name.split("/", 1)
    found, _ = _read_file_on_branch(owner, name, path, branch, token)
    return found, path


class _ReusableListing(NamedTuple):
    """The reusable workflow repository's default branch and its workflow files' blob SHAs.

    ``shas`` is None, never {}, when GitHub could not list it: an empty mapping
    means "no workflow files", which made a rate limit or a revoked token
    report every reusable workflow as deleted from GitHub.
    """
    branch: str
    shas: Optional[dict]
    error: Optional[str] = None
    repo_missing: bool = False


def _list_reusable_repo(owner: str, repo: str, token: str) -> _ReusableListing:
    headers = {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION
    }
    branch = ""
    try:
        branch = get_default_branch(owner, repo, headers)
        shas = get_all_workflow_shas(owner, repo, branch, token)
        print(f"🔍 Fetched {len(shas)} reusable workflow file SHAs from {owner}/{repo} using Trees API")
        return _ReusableListing(branch, shas)
    except Exception as e:
        print(f"⚠️ Error fetching workflow SHAs from {owner}/{repo}: {e}")
        return _ReusableListing(branch, None, failure_kind(e, _SHOWN_GITHUB_ERRORS),
                                getattr(e, "status_code", None) == 404)


def _reusable_repo_unlisted(db: Session, reusable_workflows: List, place: _WorkflowPlace,
                            listing: _ReusableListing) -> List[DriftStatus]:
    """check_failed for each reusable workflow, when GitHub could not list the repository."""
    if listing.repo_missing:
        # GitHub can see no such repository, so nothing was ever delivered
        # there under a name we could check. For a workflow never delivered
        # anywhere that is the whole story, and reporting check_failed
        # showed the project as "Needs Attention" with "GitHub didn't
        # respond" for a workflow nothing had delivered.
        # _get_reusable_workflow_repo falls back to
        # {user}/am-reuseable-workflow when an RWX project has no repo, and
        # that repository need not exist.
        #
        # A delivered workflow was written to GitHub at some point, and 404
        # cannot distinguish "the repository was deleted" from "this token
        # can no longer see a private one" - GitHub answers 404 to both.
        # Calling that a deletion would drive the Delete Everywhere flow on
        # a token-scope change, which is the failure #1981 added
        # check_failed to prevent, so those stay unchecked and visible.
        reusable_workflows = [w for w in reusable_workflows if _may_be_on_unreachable_repo(db, w)]
        if not reusable_workflows:
            print(f"ℹ️  {place.repo_name} does not exist; its reusable "
                  f"workflows have never been delivered, so there is nothing to check")
    return [
        _workflow_check_failed(w, _expected_yaml(w), place, "reusable_workflow",
                               f"Could not check {place.repo_name}: {listing.error}")
        for w in reusable_workflows
        if w.workflow_name and w.workflow_name.strip()
    ]


def _check_reusable_workflow(db: Session, workflow, place: _WorkflowPlace, github_sha: Optional[str],
                             targets: dict, project_code: str) -> Optional[DriftStatus]:
    try:
        return _judge_workflow(db, workflow, _expected_yaml(workflow), place, github_sha, targets,
                               project_code, "reusable_workflow")
    except Exception as e:
        print(f"⚠️ Error checking reusable workflow {workflow.workflow_name} in {place.repo_name}: {e}")
        # Not a clean result: an unchecked workflow used to be recorded
        # as resolved, silently clearing genuine drift.
        return _workflow_check_failed(workflow, _expected_yaml(workflow), place, "reusable_workflow",
                                      f"Could not check {place.repo_name}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")


def _process_reusable_workflows(db: Session, reusable_workflows: List, user: str,
                                project_code: str, token: str, use_prefix: bool = True,
                                reusable_repo_name: str = None, project_id: Optional[int] = None,
                                targets: Optional[dict] = None) -> List[DriftStatus]:
    """
    Process reusable workflows for drift detection in the dedicated reusable workflow repository.
    Uses Git Trees API for batch SHA comparison to minimize API calls.
    ``targets`` is the owning project's delivery record, loaded here when not given.

    Returns:
        List[DriftStatus]: List of drift statuses for reusable workflows
    """
    if not reusable_workflows:
        return []
    if not reusable_repo_name:
        reusable_repo_name = f"{user}/am-reuseable-workflow"
    if "/" not in reusable_repo_name:
        return []

    owner, repo = reusable_repo_name.split("/", 1)
    listing = _list_reusable_repo(owner, repo, token)
    place = _WorkflowPlace(reusable_repo_name, owner, repo, listing.branch, "", token)
    if listing.shas is None:
        return _reusable_repo_unlisted(db, reusable_workflows, place, listing)

    targets = _drift_record(db, project_id, targets)
    drift_results = []
    for workflow in reusable_workflows:
        if not workflow.workflow_name or not workflow.workflow_name.strip():
            print(f"⚠️ Skipping reusable workflow with empty name (ID: {workflow.workflow_id})")
            continue
        filename = _workflow_filename_on(db, targets, workflow, place.repo_name, place.branch,
                                         format_workflow_name(workflow.workflow_name, project_code, use_prefix))
        drift_status = _check_reusable_workflow(db, workflow, place._replace(filename=filename),
                                                listing.shas.get(filename), targets, project_code)
        if drift_status:
            drift_results.append(drift_status)
    return drift_results


def _handle_deployment_variables(check_deployment_vars: bool) -> List[DriftStatus]:
    """
    Handle deployment variables drift detection.
    
    Returns:
        List[DriftStatus]: List containing deployment variables drift status if applicable
    """
    if not check_deployment_vars:
        return []
    
    try:
        # For deployment variables, we don't have direct content comparison
        # But if the user has deployment environments configured, we should treat them as changed
        # since they are typically new additions or modifications
        drift_status = _create_drift_status(
            workflow_name="deployment_variables",
            has_drift=True,  # Mark as changed to auto-select in modal
            github_content=None,
            local_content=None,
            github_sha=None,
            local_sha=None,
            message="Deployment variables detected for update",
            drift_type="deployment_vars"
        )
        print(f"✅ Deployment variables marked as changed for auto-selection")
        return [drift_status]
    except Exception as e:
        print(f"⚠️ Error checking deployment variables: {e}")
        return []


def detect_workflow_drift(
    db: Session,
    user: str,
    project_name: str,
    repo_names: List[str],
    check_deployment_vars: bool = False,
    project_id: Optional[int] = None,
    targets: Optional[dict] = None,
) -> List[DriftStatus]:
    """
    Detect drift between workflows stored in database and workflows in GitHub repositories.
    Uses Git SHA comparison for accurate drift detection.
    Only checks workflows that belong to the current project to prevent custom workflows 
    or workflows from other projects from being flagged as drifted.
    
    Returns a list of drift statuses for each workflow. ``targets`` is the
    project's delivery record, loaded here when the caller has not.
    """
    # Validate user and get project information
    token, project, project_code = _validate_user_and_get_project(db, user, project_name, project_id)
    targets = _drift_record(db, project.project_id, targets)
    
    # Get and separate workflows by type
    regular_workflows, reusable_workflows = _get_project_workflows(db, project)
    
    # Get prefix setting from project
    use_prefix = project.use_prefix
    
    # Process all workflow types and collect drift results
    drift_results = []
    drift_results.extend(_process_regular_workflows(
        db, regular_workflows, repo_names, project_code, token, use_prefix,
        project_id=project.project_id, project=project, user=user, targets=targets,
    ))
    drift_results.extend(_process_reusable_workflows(
        db, reusable_workflows, user, project_code, token, use_prefix,
        reusable_repo_name=_get_reusable_workflow_repo(project, user, db), targets=targets,
    ))
    drift_results.extend(_handle_deployment_variables(check_deployment_vars))
    
    return drift_results

def create_workflow_version(db: Session, workflow_id: int, content: str, metadata: Optional[dict] = None, commit: bool = True):
    """
    Create a new version entry for a workflow.
    
    Args:
        db: Database session
        workflow_id: ID of the workflow
        content: Workflow YAML content
        metadata: Optional metadata dictionary (author, session info, etc.)
    
    Returns:
        WorkflowVersion: Created version object
    """
    try:
        # Get the current highest version number for this workflow
        max_version = db.query(WorkflowVersion).filter(
            WorkflowVersion.workflow_id == workflow_id
        ).order_by(WorkflowVersion.version_number.desc()).first()
        
        next_version = 1 if not max_version else max_version.version_number + 1
        
        # Create version entry
        version = WorkflowVersion(
            workflow_id=workflow_id,
            version_number=next_version,
            content=content,
            version_metadata=json.dumps(metadata) if metadata else None
        )
        
        db.add(version)
        if commit:
            db.commit()
            db.refresh(version)
        
        print(f"✅ Created workflow version {next_version} for workflow ID {workflow_id}")
        return version
    except Exception as e:
        print(f"❌ Error creating workflow version: {str(e)}")
        if commit:
            db.rollback()
        # Don't fail the entire workflow save if version creation fails
        # This ensures backward compatibility
        return None

def _assert_name_free(db, project_id: int, name: str) -> None:
    """Refuse a name a pending_delete row of this project still owns.

    A tombstone is a workflow queued for removal from GitHub by an open
    campaign. It keeps its name until that campaign merges and
    ``_reap_deleted_workflows`` destroys the row, because the name is what
    ``_commit_workflows_to_branch`` deletes. Letting a live workflow take the
    same name in the same project puts two rows behind one delivered path.

    Either kind counts. A standard project delivers its regular and its own
    reusable workflows to its own repositories under ``format_workflow_name``
    (#2084), so two rows of one name are one file (#2309). A live row needs no
    refusal here: a save finds its workflow by name across both kinds
    (``_find_live_workflow``), so it only creates a row when no live workflow
    has the name, and a rename onto a taken name is refused by
    ``_rename_blocker_name_collision``.
    """
    tombstone = db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project_id,
        func.lower(Workflow.workflow_name) == (name or "").lower(),
        Workflow.pending_delete.is_(True),
    ).first()
    if tombstone:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{tombstone.workflow_name}' is queued for removal from GitHub by an open "
                f"PR campaign. Merge or close that campaign before reusing the name."
            ),
        )


def _find_live_workflow(db, project_id: int, name: str, listed_reusable: bool):
    """The project's live workflow of this name, preferring the kind it was listed as.

    One name is one workflow (#2309), so a row of the other kind is the same
    workflow: its trigger changed, or the client's list predates a change made
    elsewhere (#2318). Preferring the listed kind still picks the row the
    client is editing in a same-name pair left from before names were unique
    across kinds. Case-insensitive equality, not ILIKE: '_' is a LIKE wildcard
    and workflow names routinely contain it. pending_delete rows are excluded so
    a save never revives a tombstone and cancels the deletion its campaign is
    carrying.
    """
    rows = db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project_id,
        func.lower(Workflow.workflow_name) == (name or "").lower(),
        Workflow.pending_delete.is_(False),
    ).all()
    listed = (w for w in rows if bool(w.reusable_workflow) == listed_reusable)
    return next(listed, rows[0] if rows else None)


def _kind_for_save(db, project_id: int, content: str, listed_reusable: bool, workflow=None) -> bool:
    """The kind a save leaves a workflow with: its trigger decides (#2318).

    ``workflow_call`` in ``on:`` makes a workflow reusable, as on GitHub. YAML
    that does not parse, or has no ``on:``, says nothing, so the workflow keeps
    its kind and a new one takes the kind it was listed as. An existing workflow
    changes kind only when this save edits its YAML: a project save posts every
    workflow, and one nobody touched keeps its kind even if the two disagree.
    A Reusable Workflow Project's workflows stay reusable, since linked caller
    projects call them through ``workflow_call``.
    """
    current = listed_reusable if workflow is None else bool(workflow.reusable_workflow)
    declared = declared_reusable(content)
    if declared is None or declared == current:
        return current
    if workflow is not None and (workflow.workflow_yaml or "") == content:
        return current
    project = db.query(Project).filter_by(project_id=project_id).first()
    if project is None or project.project_type == "rwx":
        return current
    if workflow is not None:
        _assert_kind_change_allowed(db, project, workflow, declared)
    return declared


def _assert_kind_changes_allowed(db, project_id: int, listed: list) -> None:
    """Run a save's kind-change refusals before any of its workflows is written.

    create_or_update_workflow commits each workflow as it goes, so a refusal
    raised for the third workflow of a save would leave the first two written
    while the client is told the save failed. ``listed`` pairs each workflow
    with the section it came from; only one whose trigger disagrees with that
    section can change kind, so only those are looked up.
    """
    for workflow, listed_reusable in listed:
        content = (workflow.content or "").strip()
        if not (workflow.name or "").strip() or declared_reusable(content) in (None, listed_reusable):
            continue
        original = getattr(workflow, "original_name", None)
        name = original if isinstance(original, str) and original.strip() else workflow.name
        name = _strip_duplicated_project_prefix(db, project_id, _validate_workflow_name(name))
        existing = _find_live_workflow(db, project_id, name, listed_reusable)
        if existing is not None:
            _kind_for_save(db, project_id, content, listed_reusable, existing)


def _assert_kind_change_allowed(db, project: "Project", workflow: "Workflow", reusable: bool) -> None:
    """Refuse a kind change while it would leave state behind (#2318).

    An open campaign's lock and selection are tracked per kind, so a workflow
    it is delivering keeps its kind until the campaign merges or closes. A
    same-name pair from before names were unique across kinds would become two
    rows of one kind, which no lookup can tell apart.
    """
    kind = "reusable" if reusable else "regular"
    if _rename_blocker_open_pr(db, project, workflow):
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{workflow.workflow_name}' is in an open PR campaign. Merge or close that "
                f"campaign before making it a {kind} workflow."
            ),
        )
    clash = db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project.project_id,
        func.lower(Workflow.workflow_name) == workflow.workflow_name.lower(),
        Workflow.workflow_id != workflow.workflow_id,
    ).first()
    if clash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{workflow.workflow_name}' cannot become a {kind} workflow while this project "
                f"has another workflow of that name. Rename one of them first."
            ),
        )


_CASE_ONLY_RENAME_REFUSAL = (
    "ActionsManager cannot change only the capitalisation of a workflow name. "
    "Rename it to something different first, then to the casing you want."
)


def _assert_not_case_only_rename(original_name: str, new_name: str,
                                 is_rename: bool) -> None:
    """Refuse a rename that differs from the old name only in capitalisation.

    ``is_rename`` compares the two lowercased, so such a request never reaches
    the rename branch at all and the save quietly keeps the old casing.

    Reported, not raised. create_or_update_workflow commits per workflow with no
    outer transaction, so a 409 here abandoned a project save part-way: the
    workflows ahead of it were already committed, the ones behind it silently
    lost their edits, and the client was told nothing had been saved. Because
    savedName is not restamped either, the same save then failed on every retry
    and the project could not be saved at all until the user restored the
    original casing — a cosmetic edit bricking the project.

    The refusal still reaches the user where it costs nothing: the rename report
    returns it as a blocker, so the dialog explains it before anything is sent.
    """
    if original_name and not is_rename and original_name != new_name:
        print(f"📌 {_CASE_ONLY_RENAME_REFUSAL} (keeping '{original_name}')")


def _was_delivered_per_db(db, workflow) -> bool:
    """Whether this workflow may be on GitHub, from the database alone.

    Delivered somewhere, or carried by an open PR, per the delivery record; or
    anything else suggesting it left the database. The record only adds
    positive evidence: a delivery made before it existed, to a branch no drift
    check ever saw it on, left no row, and an emptied row from a later campaign
    that closed says nothing about that one.

    "May be" is the honest reading, and the asymmetry justifies it: a false
    "delivered" costs one DELETE that answers 404 and a conservative refusal
    the user can act on, while a false "never delivered" silently orphans a
    live file and breaks a caller's ``uses:`` line.

    Deliberately DB-only: the save path must not grow GitHub round trips.
    """
    return (deliveries.delivered_anywhere(db, deliveries.WORKFLOW, workflow.workflow_id)
            or deliveries.pending_anywhere(db, deliveries.WORKFLOW, workflow.workflow_id)
            or _may_predate_delivery_record(workflow))


def _may_predate_delivery_record(file) -> bool:
    """Whether a workflow, custom file or CODEOWNERS may have reached GitHub before file_deliveries existed:
    a real blob SHA, or any status past "new"."""
    if isinstance(file, CustomFile):
        git_hash, status = file.git_hash, file.file_status
    elif isinstance(file, Codeowners):
        git_hash, status = file.git_hash, file.status
    else:
        git_hash, status = file.workflow_git_hash, file.workflow_status
    # A local edit used to write 40 zeros where it now writes NULL; neither is a blob.
    return bool((git_hash or "").strip("0")) or (status or "new") != "new"


def _record_never_saw(db: Session, kind: str, file_id, file) -> bool:
    """Whether *file* may be on GitHub although the record has never tracked it anywhere.

    Only a file that may predate the record: one it has a row for, even an
    emptied one, is one the record has been tracking.
    """
    return _may_predate_delivery_record(file) and not deliveries.has_any_record(db, kind, file_id)


def _may_be_on_unreachable_repo(db: Session, workflow) -> bool:
    """Whether to keep *workflow* visible as check_failed when its repository 404s.

    The record only adds positive evidence here. A workflow delivered before the
    record existed and never seen by a drift check has no row, or only a row a
    closed campaign emptied, even in the project's own repository; dropping it
    would turn an unreachable repository into nothing to report (#1981).
    """
    return (deliveries.delivered_anywhere(db, deliveries.WORKFLOW, workflow.workflow_id)
            or _may_predate_delivery_record(workflow))


def _may_be_delivered_unrecorded(db: Session, workflow, repo_name: str) -> bool:
    """Whether a workflow the record has never seen may still be on *repo_name*.

    Only for a repository outside the workflow's own project, such as the
    {user}/am-reuseable-workflow fallback. Drift records nothing there (its
    details carry no repo id), so a delivery made there before the record
    existed left nothing to backfill or observe, and the record's silence
    proves nothing. Reading it as "never delivered" would downgrade a real
    deletion, or an unreachable repository, to nothing to report (#1981).
    """
    if not _record_never_saw(db, deliveries.WORKFLOW, workflow.workflow_id, workflow):
        return False
    _, project = _resolve_workflow_owner_project(db, workflow.workflow_id)
    return project is None or not _project_owns_repo(db, project, repo_name)


def _assert_rename_allowed(db, project_id: int, workflow, new_name: str,
                           user: Optional[str]) -> None:
    """Refuse a rename the impact report would call blocked.

    Calls the same ``_rename_blockers`` the preview calls, so the dialog and
    the save cannot disagree about what is allowed — the preview saying
    "blocked" while /api/save-workflows performed it anyway was the whole
    hazard. Every check below is a database query; no GitHub call is added.
    """
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if project is None:
        return

    # Consumers only matter to the blocker that refuses a *delivered reusable*
    # workflow, so skip the scan otherwise. It loads every workflow row of every
    # linked caller project — full YAML column — and substring-matches in Python,
    # which is ~19 queries and hundreds of materialised rows on a project with a
    # handful of callers. Paid on every rename save and every preview, and
    # discarded unread in all the cases below.
    delivered = _was_delivered_per_db(db, workflow)
    consumers = (
        _rename_consumers(db, project, workflow, user or "")
        if delivered and workflow.reusable_workflow
        else []
    )

    blocker = _rename_blockers(
        db, project, workflow, new_name, consumers, delivered=delivered,
    )
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)


def create_or_update_workflow(db, workflow, project_id, is_reusable, last_modified_by=None):
    """
    Create or update a workflow within the scope of a specific project.
    This ensures that each project maintains its own workflows without cross-project interference.
    Automatically creates a version entry on each save.

    When `workflow.original_name` is provided and differs from `workflow.name`, a true rename
    is performed: the existing record is found by `original_name` and its `workflow_name` is
    updated to the new name instead of creating a duplicate entry.

    `is_reusable` is the kind the caller listed the workflow as. It picks the row
    of a same-name pair and the kind of a new workflow whose YAML says nothing;
    otherwise the trigger decides (`_kind_for_save`, #2318). Returns the saved row.
    """
    # Normalize: strip whitespace and convert blank to None
    if last_modified_by:
        last_modified_by = last_modified_by.strip() or None
    new_name = _validate_workflow_name(workflow.name)
    raw_original = getattr(workflow, 'original_name', None)
    # Use isinstance so that non-string values (e.g. None, or MagicMock in tests)
    # are treated as "no original name provided" rather than triggering a rename.
    # WorkflowSchema declares original_name as Optional[str], so at runtime this
    # will always be None or a str when called via the API.
    original_name = _validate_workflow_name(raw_original) if isinstance(raw_original, str) and raw_original.strip() else ''

    # Defense-in-depth: when the project uses the ``AM_{code}_`` prefix, the
    # canonical ``Workflow.workflow_name`` stored in the DB does not include
    # the project prefix (the prefix is re-applied by ``format_workflow_name``
    # on push to GitHub).  If a client accidentally submits a name that
    # already includes the prefix — for example because of a stale frontend
    # that did not split the editable suffix from the locked prefix — strip a
    # single matching prefix here so we never persist a duplicated
    # ``AM_CODE_AM_CODE_name`` segment.
    new_name = _strip_duplicated_project_prefix(db, project_id, new_name)
    new_name = _validate_workflow_name(new_name)
    if original_name:
        original_name = _strip_duplicated_project_prefix(db, project_id, original_name)
        original_name = _validate_workflow_name(original_name)

    is_rename = bool(original_name and original_name.lower() != new_name.lower())

    _assert_not_case_only_rename(original_name, new_name, is_rename)

    print(f"✅ Creating/updating workflow '{new_name}' for project {project_id}, reusable: {is_reusable}"
          + (f" (renamed from '{original_name}')" if is_rename else ""))

    existing_workflow = None

    if is_rename:
        # Look up the workflow by its previous name so we perform a true rename.
        existing_workflow = _find_live_workflow(db, project_id, original_name, is_reusable)

        if existing_workflow:
            # A rename *takes* the new name, so it needs the same refusal as a
            # fresh save. The guard below used to sit only in the
            # `existing_workflow is None` block, which is exactly the branch a
            # rename skips — so renaming onto a tombstone's name produced the
            # duplicate pair the guard exists to prevent.
            _assert_name_free(db, project_id, new_name)
            # The rest of the refusals the impact report computes. Until this
            # call existed the report could say "blocked" and the save would
            # rename anyway, orphaning the delivered file and breaking every
            # consumer's uses: line with no warning.
            _assert_rename_allowed(db, project_id, existing_workflow, new_name, last_modified_by)

            print(f"📌 ✅ Renaming workflow '{original_name}' → '{new_name}' in project {project_id}")
            existing_workflow.workflow_name = new_name

            # No duplicate sweep here any more. _assert_rename_allowed above
            # refuses on a strict superset of what the sweep used to match —
            # same project, same lower(name), either kind, other row — and it
            # includes tombstones, which the sweep excluded. So the sweep
            # could only ever run against an empty list, including the
            # LinkedReusableWorkflow re-pointing it carried for a case that
            # can no longer reach it.
        else:
            print(f"⚠️ Original workflow '{original_name}' not found; treating as new workflow '{new_name}'")

    if existing_workflow is None:
        # Search for an existing workflow within the current project only.
        existing_workflow = _find_live_workflow(db, project_id, new_name, is_reusable)

        # ...but excluding them must not let a second row take the same name.
        # Both rows format to one GitHub path, and a campaign selecting that name
        # picks up both: _commit_workflows_to_branch would then remove the file
        # and upsert it on the same AM branch, with DB order deciding which wins.
        # Refuse instead, and say what the user has to do about it.
        _assert_name_free(db, project_id, new_name)

    if existing_workflow:
        _apply_workflow_update(db, existing_workflow, workflow.content.strip(), project_id, is_reusable,
                               last_modified_by, is_rename)
    else:
        print(f"📌 Creating new workflow for project: {new_name}")
        new_workflow = Workflow(
            workflow_name=new_name,
            workflow_yaml=workflow.content.strip(),
            reusable_workflow=_kind_for_save(db, project_id, workflow.content.strip(), is_reusable),
            # New workflows start with "new" status
            workflow_status="new",
            # Audit: record who created this workflow
            last_modified_by=last_modified_by
        )
        db.add(new_workflow)
        db.commit()
        db.refresh(new_workflow)
        existing_workflow = new_workflow
        
        # Create project association for new workflow
        db.add(ProjectWorkflow(
            project_id=project_id,
            workflow_id=existing_workflow.workflow_id
        ))
        db.commit()
        print(f"✅ Created project association for workflow {existing_workflow.workflow_id}")
        print("✅ New workflow, not yet pushed, workflow status: new")
        
        # Create initial version entry for this new workflow
        create_workflow_version(
            db, 
            existing_workflow.workflow_id, 
            workflow.content.strip(),
            metadata={'action': 'create', 'timestamp': datetime.now(timezone.utc).isoformat()}
        )
    return existing_workflow


def _apply_workflow_update(db, existing_workflow, content: str, project_id, is_reusable: bool,
                           last_modified_by, is_rename: bool) -> None:
    """Save *content* to an existing workflow, for create_or_update_workflow."""
    print(f"📌 ✅ Updating existing workflow in project: {existing_workflow.workflow_name}")
    # Whether the YAML actually moved. A project save posts every workflow it
    # holds, so settings-only saves — adding a repository, renaming the
    # project — arrive here for workflows nobody edited. projects.py used to
    # have its own copy of this function that never touched workflow_status;
    # now that the duplicate is gone and this one runs on that path, an
    # unchanged re-save flipped every synced workflow to committed_locally
    # and the badges with it. The hash is gated with it: an unchanged
    # workflow is still the blob it was committed as.
    content_changed = (existing_workflow.workflow_yaml or "") != content
    existing_workflow.reusable_workflow = _kind_for_save(db, project_id, content, is_reusable, existing_workflow)
    existing_workflow.workflow_yaml = content
    if content_changed:
        # No blob SHA for content GitHub has not been given.
        existing_workflow.workflow_git_hash = None

    # Check if this workflow has any open PRs before downgrading status.
    # Any project's: a reusable workflow stays locked while any caller or
    # its owning RWX project has a campaign carrying it.
    has_open_pr = False
    try:
        has_open_pr = deliveries.pending_anywhere(db, deliveries.WORKFLOW, existing_workflow.workflow_id)
    except Exception as e:
        print(f"⚠️ Error checking for open PRs: {e}")
        import traceback
        traceback.print_exc()

    # Update status: if there's an open PR, keep under_review; otherwise set to committed_locally
    if has_open_pr:
        # Preserve under_review status when PR is open
        if existing_workflow.workflow_status != "under_review":
            existing_workflow.workflow_status = "under_review"
        print("✅ Workflow has open PR - preserving status: under_review")
    elif content_changed:
        # No open PR: editing a "synced" or "under_review" workflow brings it back to committed_locally
        existing_workflow.workflow_status = "committed_locally"
        print("✅ Local modification, workflow status: committed_locally")

    # Audit: record who made this change
    if last_modified_by:
        existing_workflow.last_modified_by = last_modified_by
    db.commit()
    if content_changed:
        project = db.query(Project).filter_by(project_id=project_id).first()
        resolve_drift_for_local_edit(db, project, deliveries.WORKFLOW, existing_workflow.workflow_id, content)

    # Create version entry for this update
    create_workflow_version(
        db,
        existing_workflow.workflow_id,
        content,
        metadata={'action': 'rename' if is_rename else 'update', 'timestamp': datetime.now(timezone.utc).isoformat()}
    )


def _listed_kind_change(saved, listed_reusable: bool) -> list:
    """The saved workflow, if its kind is not the one the client listed it as.

    The client groups workflows by kind. When a save changed one's kind (#2318),
    or found the client's list out of date, the response says so and the UI
    moves the workflow to the other section without a reload.
    """
    if saved is None or bool(saved.reusable_workflow) == listed_reusable:
        return []
    return [{
        "name": saved.workflow_name,
        "is_reusable": bool(saved.reusable_workflow),
        "workflow_status": saved.workflow_status,
    }]


def cleanup_orphaned_workflows(db: Session):
    """
    Remove workflows that are no longer associated with any projects.
    This helps keep the database clean after project deletions.
    """
    try:
        # Find workflows that have no project associations
        orphaned_workflows = db.query(Workflow).outerjoin(ProjectWorkflow).filter(
            ProjectWorkflow.workflow_id.is_(None)
        ).all()
        
        if orphaned_workflows:
            print(f"🧹 Cleaning up {len(orphaned_workflows)} orphaned workflows")
            for workflow in orphaned_workflows:
                print(f"  - Deleting orphaned workflow: {workflow.workflow_name} (ID: {workflow.workflow_id})")
                db.delete(workflow)
            
            db.commit()
            print(f"✅ Cleaned up {len(orphaned_workflows)} orphaned workflows")
        else:
            print("✅ No orphaned workflows found")
            
    except Exception as e:
        print(f"❌ Error cleaning up orphaned workflows: {str(e)}")
        db.rollback()


def delete_project_record(db: Session, project: Project) -> None:
    """Remove *project* from ActionsManager, and the workflows no other project holds. Commits.

    Its rows go with it: foreign keys are enforced on SQLite too (issue
    #1811). The saved display positions are removed explicitly anyway, so the
    cleanup does not depend on engine configuration.
    """
    db.query(ProjectDisplayOrder).filter(
        ProjectDisplayOrder.project_id == project.project_id
    ).delete(synchronize_session=False)
    db.delete(project)
    db.commit()
    cleanup_orphaned_workflows(db)


def _joined_names(carried: Optional[str], added: str) -> str:
    names = _split_workflow_names(carried)
    names += [name for name in _split_workflow_names(added) if name not in names]
    return ", ".join(names)


def _refresh_pr_row(row: ProjectPullRequest, pr_number: int, pr_url: str,
                    campaign_id: Optional[int], is_new_pr: bool, fields: dict) -> None:
    """Point an existing PR row at this run's pull request.

    Commits added to a PR that is still open leave what it already carried
    there, so its names are joined by this run's rather than replaced: a push
    of one workflow onto a campaign's PR made its card, and a rollback's
    "keep", forget the rest. A fresh PR on a reused row carries only its own.
    """
    carries_on = not is_new_pr and row.pr_state == "open" and row.pr_number == pr_number
    row.pr_number = pr_number
    row.pr_url = pr_url
    row.pr_state = "open"
    # A fresh GitHub PR (or a legacy row without a campaign) joins the
    # new campaign; updates to an existing open PR stay in their campaign.
    if campaign_id is not None and (is_new_pr or row.campaign_id is None):
        row.campaign_id = campaign_id
    for column, value in fields.items():
        if value is None:
            continue
        if carries_on and column in ("workflow_names", "file_names"):
            value = _joined_names(getattr(row, column), value)
        setattr(row, column, value)


def _save_pr_to_database(db: Session, project_id: int, repo_name: str,
                        pr_number: int, pr_url: str, branch_name: str,
                        target_branch: str, title: Optional[str] = None,
                        author: Optional[str] = None, body: Optional[str] = None,
                        workflow_names: Optional[str] = None,
                        file_names: Optional[str] = None,
                        campaign_id: Optional[int] = None,
                        is_new_pr: bool = True) -> ProjectPullRequest:
    """
    Save or update PR information in the database.

    Args:
        db: Database session
        project_id: Project ID
        repo_name: Full repository name (owner/repo)
        pr_number: GitHub PR number
        pr_url: GitHub PR URL
        branch_name: Actions Manager branch name
        target_branch: Target/base branch name
        title: PR title captured from GitHub (optional)
        author: PR author GitHub login (optional)
        body: PR description/body from GitHub (optional)
        workflow_names: Comma-separated workflow names associated with this PR (optional)
        campaign_id: The PR campaign this row belongs to (optional)
        is_new_pr: True when a brand-new GitHub PR was created in this run.
            When False (commits were added to an existing open PR), the row
            keeps its current campaign instead of moving to the new one.

    Returns:
        The saved ``ProjectPullRequest`` row.

    Raises:
        Exception: re-raises any database error after rolling back, so callers
            surface the failure instead of silently leaving the PR untracked.
    """
    try:
        # Check if PR entry already exists
        existing_pr = db.query(ProjectPullRequest).filter_by(
            project_id=project_id,
            repo_name=repo_name,
            branch_name=branch_name,
            target_branch=target_branch
        ).first()
        fields = {"title": title, "author": author, "body": body,
                  "workflow_names": workflow_names, "file_names": file_names}

        if existing_pr:
            _refresh_pr_row(existing_pr, pr_number, pr_url, campaign_id, is_new_pr, fields)
            print(f"✅ Updated PR entry in database: PR #{pr_number} for {repo_name}")
        else:
            # Create new PR entry
            new_pr = ProjectPullRequest(
                project_id=project_id,
                campaign_id=campaign_id,
                repo_name=repo_name,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_state="open",
                branch_name=branch_name,
                target_branch=target_branch,
                **fields,
            )
            db.add(new_pr)
            existing_pr = new_pr
            print(f"✅ Saved new PR entry to database: PR #{pr_number} for {repo_name}")

        db.commit()
        return existing_pr

    except Exception as e:
        # Failing to record a PR (e.g. the database schema is missing the
        # campaign migration) must be a loud backend error — silently
        # continuing would leave GitHub PRs untracked or grouped incorrectly.
        print(f"❌ Error saving PR to database: {str(e)}")
        db.rollback()
        raise


def _update_project_pr_state(db: Session, project_id: int, new_state: str) -> None:
    """
    Update the state of a project.
    
    Args:
        db: Database session
        project_id: Project ID
        new_state: New project state (new, draft, open, synced)
    """
    try:
        project = db.query(Project).filter_by(project_id=project_id).first()
        if project:
            project.pr_state = new_state
            db.commit()
            print(f"✅ Updated project state to: {new_state}")
        else:
            print(f"⚠️ Project with ID {project_id} not found")
            
    except Exception as e:
        print(f"❌ Error updating project state: {str(e)}")
        db.rollback()


def _get_linked_workflow_ids_for_project(
    db: Session,
    standard_project_id: int,
    workflow_names: Optional[List[str]] = None,
    only_if_status: Optional[str] = None,
) -> List[int]:
    """Return workflow IDs for linked reusable workflows associated with a standard project.

    Linked workflows live in the RWX project's ProjectWorkflow, so they are not
    reachable via a simple ProjectWorkflow.project_id filter.  This helper
    centralises the LinkedReusableWorkflow join used in several places.

    Args:
        standard_project_id: The owning standard project's ID.
        workflow_names:       If provided, only include workflows whose names are in this list.
        only_if_status:       If provided, only include workflows currently at this status.
    """
    query = (
        db.query(Workflow.workflow_id)
        .join(LinkedReusableWorkflow, LinkedReusableWorkflow.workflow_id == Workflow.workflow_id)
        .filter(LinkedReusableWorkflow.standard_project_id == standard_project_id)
    )
    if only_if_status is not None:
        query = query.filter(Workflow.workflow_status == only_if_status)
    if workflow_names is not None:
        query = query.filter(Workflow.workflow_name.in_(workflow_names))
    return [r[0] for r in query.all()]


def _reap_deleted_workflows(db: Session, project_id: int, workflows: list,
                            new_status: str) -> tuple:
    """Drop the rows a merged campaign has just removed from GitHub.

    Returns (surviving_workflows, removed_ids). Only on the transition to
    synced_with_github: before that the pull request carrying the removal is
    still open, and the row is what Restore acts on. Mirrors
    _update_project_custom_files_status.
    """
    if new_status != "synced_with_github":
        return workflows, []

    removed = [w for w in workflows if w.pending_delete]
    if not removed:
        return workflows, []

    for workflow in removed:
        _detach_workflow_from_project(db, project_id, workflow)
    print(f"🗑️ Removed {len(removed)} deleted workflow(s) from the project")
    return [w for w in workflows if not w.pending_delete], [w.workflow_id for w in removed]


def _cancel_pending_deletes(workflows: list, new_status: str) -> None:
    """Unmark workflows whose removal campaign was closed without merging.

    A transition back to committed_locally means every pull request carrying the
    change was closed, so the deletion did not happen. Leaving pending_delete set
    would strand the workflow: nothing surfaces the flag, there is no Restore to
    clear it, and the next campaign the user opens for an unrelated edit would
    silently delete the file instead of updating it.
    """
    if new_status != "committed_locally":
        return
    for workflow in workflows:
        if workflow.pending_delete:
            workflow.pending_delete = False
            print(f"↩️ Cancelled the pending deletion of '{workflow.workflow_name}' (campaign closed)")


def _drop_drift_for_removed(db: Session, project_id: int, removed_ids: list) -> None:
    """Clear persisted drift for workflows that no longer exist.

    Called after the caller's commit: drop_workflow_drift commits and recomputes
    the project's drift summary, so it has to see the rows already gone.
    """
    if not removed_ids:
        return
    project = db.query(Project).filter_by(project_id=project_id).first()
    if not project:
        return
    for workflow_id in removed_ids:
        drop_workflow_drift(db, project, workflow_id)


def _update_project_workflows_status(db: Session, project_id: int, new_status: str,
                                     only_if_status: Optional[str] = None,
                                     non_reusable_only: bool = False,
                                     workflow_names: Optional[List[str]] = None) -> None:
    """
    Update the workflow_status of all workflows in a project.

    Reusable workflows that are still referenced by an open PR campaign in
    *any* related project (the owning RWX project or any linking caller) are
    never downgraded out of ``under_review`` — the global review lock only
    clears once every campaign is merged or closed.

    Args:
        db: Database session
        project_id: Project ID
        new_status: New workflow status
        only_if_status: If set, only update workflows currently in this status
        non_reusable_only: If True, only update non-reusable (standard) workflows
        workflow_names: If set, only update workflows whose names are in this list
    """
    try:
        query = db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project_id
        )
        if only_if_status:
            query = query.filter(Workflow.workflow_status == only_if_status)
        if workflow_names is not None:
            query = query.filter(Workflow.workflow_name.in_(workflow_names))
        if non_reusable_only:
            # isnot(True) matches both False and NULL, so workflows that have
            # reusable_workflow=NULL (older DB records without an explicit value)
            # are correctly treated as non-reusable and will be updated.
            query = query.filter(Workflow.reusable_workflow.isnot(True))
        workflows = query.all()
        if only_if_status == "under_review" and new_status != "under_review":
            # Global lock: a reusable workflow stays under_review while any
            # other project still has an open PR campaign referencing it.
            reusable_ids = [w.workflow_id for w in workflows if w.reusable_workflow]
            locked_ids = _reusable_workflow_ids_locked_by_open_campaign(db, reusable_ids)
            if locked_ids:
                workflows = [w for w in workflows if w.workflow_id not in locked_ids]
                print(
                    f"🔒 Kept {len(locked_ids)} reusable workflow(s) under_review "
                    f"(open PR campaign in another project)"
                )
        workflows, removed_ids = _reap_deleted_workflows(db, project_id, workflows, new_status)
        _cancel_pending_deletes(workflows, new_status)
        for workflow in workflows:
            workflow.workflow_status = new_status
        db.commit()
        _drop_drift_for_removed(db, project_id, removed_ids)
        print(f"✅ Updated {len(workflows)} workflow(s) status to: {new_status}")
    except Exception as e:
        print(f"❌ Error updating workflow statuses: {str(e)}")
        db.rollback()


def _detach_workflow_from_project(db: Session, project_id: int, workflow: Workflow) -> None:
    """Remove a workflow's project link and the workflow row itself.

    A workflow belongs to exactly one project (unique index on
    project_workflows.workflow_id), so dropping the association always leaves it
    orphaned — there is no other project keeping it alive. Does not commit;
    persisted drift is dropped by the caller afterwards, because
    drop_workflow_drift commits and recomputes the project's summary.
    """
    db.query(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project_id,
        ProjectWorkflow.workflow_id == workflow.workflow_id,
    ).delete(synchronize_session=False)
    db.delete(workflow)


def _update_project_custom_files_status(
    db: Session, project_id: int, new_status: str, only_if_status: Optional[str] = None
) -> None:
    """Update file_status for all custom files in a project.

    When transitioning to synced_with_github, rows marked pending_delete are
    hard-deleted because the file has been successfully removed from GitHub.
    """
    try:
        query = db.query(CustomFile).filter(CustomFile.project_id == project_id)
        if only_if_status:
            query = query.filter(CustomFile.file_status == only_if_status)
        files = query.all()
        if new_status == "synced_with_github":
            to_delete = [f for f in files if f.pending_delete]
            to_update = [f for f in files if not f.pending_delete]
            for f in to_delete:
                db.delete(f)
            for f in to_update:
                f.file_status = new_status
        else:
            for f in files:
                f.file_status = new_status
        db.commit()
        print(f"✅ Updated {len(files)} custom file(s) status to: {new_status}")
    except Exception as e:
        print(f"❌ Error updating custom file statuses: {str(e)}")
        db.rollback()


def _update_project_codeowners_status(
    db: Session, project_id: int, new_status: str, only_if_status: Optional[str] = None
) -> None:
    """Update the status of the project's CODEOWNERS file."""
    try:
        query = db.query(Codeowners).filter(Codeowners.project_id == project_id)
        if only_if_status:
            query = query.filter(Codeowners.status == only_if_status)
        records = query.all()
        for r in records:
            r.status = new_status
        db.commit()
        print(f"✅ Updated {len(records)} codeowners record(s) status to: {new_status}")
    except Exception as e:
        print(f"❌ Error updating codeowners statuses: {str(e)}")
        db.rollback()


@router.post("/api/save-workflows", responses=_responses(400, 401, 404, 409, 500))
def save_workflows(
    payload: SaveProjectWorkflowsRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    try:
        github_user = account.github_user
        project = _find_project_by_name(db, github_user, payload.project_name)

        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)

        # _find_project_by_name admits any ProjectMembership, project_viewer
        # included. Without this a viewer could save — and a save can rename a
        # delivered workflow, which arms a DELETE of the delivered file in every
        # target repository on the next campaign. The read-only preview was already
        # gated at project_editor, so the write that performs the rename was the
        # looser of the two.
        #
        # Here rather than inside create_or_update_workflow: that function has no
        # authenticated principal (its only identity argument is the audit field
        # last_modified_by, which one caller fills straight from the request
        # body), and it commits per workflow with no outer transaction — a 403
        # raised inside the loop would leave the workflows ahead of it written.
        _require_project_editor(db, github_user, project)
        if payload.workflows or payload.rxworkflows:
            refuse_unless_accepted(project, "workflows")

        # Each list is the kind the client shows the workflow as; the trigger
        # decides what it is saved as (#2318).
        _assert_kind_changes_allowed(
            db, project.project_id,
            [(w, False) for w in payload.workflows] + [(w, True) for w in payload.rxworkflows],
        )
        kind_changes = []
        for workflow in payload.workflows:
            print(f"✅ ✅ REG: {workflow}")
            saved = create_or_update_workflow(db, workflow, project.project_id, is_reusable=False, last_modified_by=github_user)
            kind_changes += _listed_kind_change(saved, False)

        for workflow in payload.rxworkflows:
            print(f"✅ ❌ ❌ ❌ RW: {workflow}")
            saved = create_or_update_workflow(db, workflow, project.project_id, is_reusable=True, last_modified_by=github_user)
            kind_changes += _listed_kind_change(saved, True)

        # Update project audit trail and state
        project.last_modified_by = github_user or None
        db.commit()
        old_state = project.pr_state
        if project.pr_state in ["new", "synced"]:
            _update_project_pr_state(db, project.project_id, "draft")
            # Refresh project to get updated state
            db.refresh(project)

        return {
            "message": "Workflows saved successfully",
            "pr_state": project.pr_state,
            "state_changed": old_state != project.pr_state,
            "kind_changes": kind_changes,
        }
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        print(f"❌ Error in save_workflows endpoint: {str(e)}")
        import traceback
        traceback.print_exc()
        db.rollback()
        raise HTTPException(status_code=500, detail="Error saving workflows")

@router.post("/api/detect-drift", responses=_responses(401, 404, 409, 500))
def detect_drift(
    payload: DriftDetectionRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Detect drift between local workflows and GitHub workflows."""
    try:
        github_user = account.github_user
        drift_statuses = detect_workflow_drift(
            db,
            github_user,
            payload.project_name,
            payload.repo_names,
            payload.check_deployment_vars
        )
        
        return {
            "message": "Drift detection completed",
            "drift_results": [status.model_dump() for status in drift_statuses]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error detecting drift: {e}")
        raise HTTPException(status_code=500, detail="Error detecting drift")

# -----------------------------------------------------------------------------
# Drift detection v2 — issue-spec endpoints
# -----------------------------------------------------------------------------

def _drift_status_to_detail(
    workflow,
    drift_status: DriftStatus,
    repo_name: str,
    branch: str,
    project_code: str,
    use_prefix: bool,
    now_iso: str,
    *,
    project_id: Optional[int] = None,
    repo_id: Optional[int] = None,
    is_shared_workflow: bool = False,
    has_repo_override: bool = False,
    override_id: Optional[int] = None,
    affected_repos: Optional[List[str]] = None,
) -> WorkflowDriftDetail:
    """Convert a legacy DriftStatus into the spec WorkflowDriftDetail shape."""
    affected = affected_repos or []
    return WorkflowDriftDetail(
        workflow_id=workflow.workflow_id,
        workflow_name=workflow.workflow_name,
        workflow_filename=format_workflow_name(workflow.workflow_name, project_code, use_prefix),
        repo=repo_name,
        branch=branch,
        has_drift=drift_status.has_drift,
        actionsmanager_yaml=drift_status.local_content,
        github_yaml=drift_status.github_content,
        github_sha=drift_status.github_sha,
        last_checked=now_iso,
        message=drift_status.message,
        check_failed=drift_status.check_failed,
        deleted_in_github=drift_status.deleted_in_github,
        project_id=project_id,
        repo_id=repo_id,
        is_shared_workflow=is_shared_workflow,
        has_repo_override=has_repo_override,
        override_id=override_id,
        affected_repo_count=len(affected),
        affected_repos=affected,
        source_repo_name=repo_name,
    )


def _match_repo_for_status(status, candidate_repo_names: List[str]) -> Optional[str]:
    """Associate a drift status with a specific repo.

    Producers set ``status.repo`` directly; that is authoritative. The
    message-parsing below is only a fallback for statuses that predate the
    field (or aren't tied to a repo), and cannot express a branch at all —
    which is why the repo/branch pair is carried as data now.
    """
    if getattr(status, "repo", ""):
        return status.repo

    # Longest name first: one repo's name can be a prefix of another's
    # ("acme/api" vs "acme/api-gateway"), and a plain substring scan in
    # arbitrary order would attribute the longer repo's drift to the shorter one.
    for rn in sorted(candidate_repo_names, key=len, reverse=True):
        if rn and rn in (status.message or ""):
            return rn
    if len(candidate_repo_names) == 1:
        return candidate_repo_names[0]
    return None


def _affected_repos_for(workflow_id: int, repo_for_status: str, project_repo_names: List[str],
                         repo_id_by_name: dict, overrides_by_key: dict) -> list:
    """Other repos sharing this workflow with no per-repo override — they'll drift too if the
    project workflow changes without syncing them. Skips repos we couldn't map to a repo_id
    (defensive — should not happen)."""
    affected = []
    for rn in project_repo_names:
        if rn == repo_for_status:
            continue
        other_repo_id = repo_id_by_name.get(rn)
        if other_repo_id is None:
            continue
        if overrides_by_key.get((workflow_id, other_repo_id)):
            continue
        affected.append(rn)
    return affected


def _build_drift_detail_for_status(status, workflows_by_name: dict, only_workflow_id: Optional[int],
                                    candidate_repo_names: List[str], repo_id_by_name: dict,
                                    project_repo_names: List[str], overrides_by_key: dict, project: Project,
                                    now_iso: str) -> Optional[WorkflowDriftDetail]:
    """Turn one legacy DriftStatus into a spec-shaped WorkflowDriftDetail, or None if it should be skipped."""
    wf = workflows_by_name.get(status.workflow_name)
    if not wf:
        return None
    if only_workflow_id is not None and wf.workflow_id != only_workflow_id:
        return None

    repo_for_status = _match_repo_for_status(status, candidate_repo_names)
    if repo_for_status is None:
        # Skip statuses we cannot pin to a repo
        return None

    repo_id = repo_id_by_name.get(repo_for_status)
    is_shared = len(project_repo_names) > 1
    override = overrides_by_key.get((wf.workflow_id, repo_id)) if repo_id else None
    affected = _affected_repos_for(wf.workflow_id, repo_for_status, project_repo_names, repo_id_by_name, overrides_by_key)

    return _drift_status_to_detail(
        wf,
        status,
        repo_for_status,
        # The branch the check actually used. This used to be an independent
        # default-branch lookup, so the branch shown could differ from the one
        # compared against.
        # The branch the check actually used — no second, independent lookup
        # that could disagree with what was compared.
        status.branch,
        project.project_code or "",
        project.use_prefix,
        now_iso,
        project_id=project.project_id,
        repo_id=repo_id,
        is_shared_workflow=is_shared,
        has_repo_override=override is not None,
        override_id=(override.id if override else None),
        affected_repos=affected,
    )


def _collect_project_drift_details(
    db: Session,
    user: str,
    project: Project,
    only_workflow_id: Optional[int] = None,
    targets: Optional[dict] = None,
) -> List[WorkflowDriftDetail]:
    """Run drift detection for a project and return spec-shaped details.

    When ``only_workflow_id`` is provided, the returned list is filtered to
    that single workflow (still per repo/branch). ``targets`` is the
    project's delivery record, loaded here when not given.
    """
    targets = _drift_record(db, project.project_id, targets)
    repo_names = [
        r.repo_name for r in
        db.query(Repo).join(ProjectRepo).filter(
            ProjectRepo.project_id == project.project_id
        ).all()
    ]

    # Include the reusable workflow repo so reusable drift statuses can be
    # associated to a repo and surfaced via the v2 endpoints.
    reusable_repo = _get_reusable_workflow_repo(project, user, db)
    candidate_repo_names = list(repo_names)
    if reusable_repo and reusable_repo not in candidate_repo_names:
        candidate_repo_names.append(reusable_repo)

    statuses = detect_workflow_drift(
        db, user, project.project_name, repo_names, check_deployment_vars=False,
        project_id=project.project_id, targets=targets,
    )

    # Index workflows by name for quick lookup of workflow_id and yaml content
    workflows_by_name = {
        w.workflow_name: w for w in
        db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id
        ).all()
    }

    now_iso = datetime.now(timezone.utc).isoformat()

    # Pre-compute repo metadata used to enrich each drift detail with
    # scope-aware fields (is_shared_workflow / affected_repos / etc).
    project_repo_rows = (
        db.query(Repo).join(ProjectRepo).filter(ProjectRepo.project_id == project.project_id).all()
    )
    repo_id_by_name = {r.repo_name: r.repo_id for r in project_repo_rows}
    project_repo_names = [r.repo_name for r in project_repo_rows]

    # All overrides for this project, indexed by (workflow_id, repo_id)
    override_rows = (
        db.query(RepoWorkflowOverride)
        .filter_by(project_id=project.project_id)
        .all()
    )
    overrides_by_key = {(o.workflow_id, o.repo_id): o for o in override_rows}

    details: List[WorkflowDriftDetail] = []
    for status in statuses:
        detail = _build_drift_detail_for_status(
            status, workflows_by_name, only_workflow_id, candidate_repo_names,
            repo_id_by_name, project_repo_names, overrides_by_key, project,
            now_iso
        )
        if detail:
            detail.actionsmanager_sha = _delivered_sha(targets, _drift_key(
                db, deliveries.WORKFLOW, detail.workflow_id, detail.repo, detail.branch))
            details.append(detail)

    return details


def _workflow_observations(details: List[WorkflowDriftDetail]) -> List[DriftObservation]:
    return [DriftObservation(deliveries.WORKFLOW, d.workflow_id, d.workflow_name, d) for d in details]


NOT_AUTHORIZED_PROJECT_DETAIL = "Not authorized for this project"
DELIVERY_MODE_ERROR_DETAIL = (
    "delivery_mode must be 'pr': drift is put right through a pull request, never a direct commit"
)
_ERR_INSUFFICIENT_PROJECT_ROLE = "Insufficient project permissions. Required: project_editor"
_ERR_REPO_NOT_IN_PROJECT = "Repository is not part of this project"


def _require_project_editor(db: Session, github_user: str, project: Project, *, settling: bool = False) -> None:
    """Require at least project_editor before writing to the project's repositories.

    Drift resolution and workflow deletion write to GitHub, so seeing the
    project is not enough. The role comes from authorization.project_role,
    *settling* included: merging or closing a pull request still works while
    the project is being deleted.
    """
    account = db.query(Account).filter_by(github_user=github_user).first()
    role = project_role(db, account, project, settling=settling)
    if role is None:
        raise HTTPException(status_code=403, detail=NOT_AUTHORIZED_PROJECT_DETAIL)
    if PROJECT_ROLE_LEVEL[role] < PROJECT_ROLE_LEVEL["project_editor"]:
        raise HTTPException(status_code=403, detail=_ERR_INSUFFICIENT_PROJECT_ROLE)


def require_project_write_access(db: Session, request, github_user: str, project_name: str) -> Project:
    """Prove the caller is who they claim, and may write to this project.

    For routes that take the caller's own name as a client-supplied field and
    then act with that user's GitHub token. Three things have to hold, and the
    secrets, environment-variable and ruleset modules were checking none of
    them:

    1. the session owns the name it was given - the parameter is chosen by the
       caller, so it proves nothing on its own (see assert_session_owns_user);
    2. the project resolves for *this* caller, through the access-aware lookup
       rather than a bare global name match;
    3. the caller holds project_editor on it, because every route using this
       writes to GitHub or to the project's stored configuration.

    Returns the resolved project so callers need not look it up twice.
    """
    auth_module.assert_session_owns_user(github_user, request, db)
    project = _find_project_by_name(db, github_user, project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    _require_project_editor(db, github_user, project)
    return project


def require_repo_write_access(db: Session, request, github_user: str, repo_name: str) -> Project:
    """Prove write access for a route that names a repository and no project.

    Deployment environments are created on a repository, so these routes carry
    no project_name to gate on. The rule that does apply: a caller may
    configure a repository when it belongs to at least one project they can
    edit. Without that, the repository is not theirs to configure *through
    ActionsManager*, whatever their own GitHub token would allow — which is the
    boundary these routes were relying on, and it is GitHub's, not ours.

    Returns the project that granted access, for callers that want to log it.
    """
    auth_module.assert_session_owns_user(github_user, request, db)

    repo = db.query(Repo).filter(Repo.repo_name == (repo_name or "").strip()).first()
    if not repo:
        raise HTTPException(status_code=404, detail=f"Repository '{repo_name}' not found")

    linked = (
        db.query(Project)
        .join(ProjectRepo, ProjectRepo.project_id == Project.project_id)
        .filter(ProjectRepo.repo_id == repo.repo_id)
        .all()
    )
    for project in linked:
        try:
            _require_project_editor(db, github_user, project)
            return project
        except HTTPException:
            continue

    raise HTTPException(status_code=403, detail=_ERR_INSUFFICIENT_PROJECT_ROLE)


def _require_repo_in_project(db: Session, project: Project, repo_name: str, github_user: str) -> None:
    """Require that *repo_name* is a repository this project may write to.

    Without this the resolve endpoints accept any caller-supplied repo and
    commit to it, so the target was effectively attacker-chosen. Mirrors the
    check _resolve_source_repo already performs for adopt-github-version.

    Reusable workflows are the exception: _collect_project_drift_details
    deliberately surfaces drift against the reusable-workflow repo, which for a
    standard project is the *linked RWX project's* repo and so is not in this
    project's project_repos. Rejecting it would make every reusable-workflow
    drift unresolvable from the UI. That repo is derived from the project's own
    links (or the authenticated user), never from the request, so accepting it
    keeps the target out of the caller's control.
    """
    _require_owned_repo_format(repo_name)
    normalized = repo_name.strip()

    if _project_owns_repo(db, project, normalized):
        return

    if normalized == _get_reusable_workflow_repo(project, github_user, db):
        return

    raise HTTPException(status_code=400, detail=_ERR_REPO_NOT_IN_PROJECT)


def _require_owned_repo_format(repo_name: str) -> None:
    if not repo_name or "/" not in repo_name:
        raise HTTPException(status_code=400, detail="repo must be in 'owner/repo' format")


def _project_owns_repo(db: Session, project: Project, repo_name: str) -> bool:
    """True when *repo_name* is one of the project's own repositories."""
    repo = db.query(Repo).filter(Repo.repo_name == repo_name.strip()).first()
    if not repo:
        return False
    return db.query(ProjectRepo).filter_by(
        project_id=project.project_id, repo_id=repo.repo_id
    ).first() is not None


def _require_owned_repo(db: Session, project: Project, repo_name: str) -> None:
    """Require that *repo_name* is one of the project's own repositories.

    Stricter than _require_repo_in_project, which also accepts the reusable-
    workflow repo so reusable-workflow drift stays resolvable. That repo belongs
    to the *linked RWX project*, so accepting it for a deletion would remove the
    shared copy every other caller project linking that project depends on —
    the very thing _require_owns_reusable_repo refuses on the sibling endpoint.
    """
    _require_owned_repo_format(repo_name)
    if not _project_owns_repo(db, project, repo_name):
        raise HTTPException(status_code=400, detail=_ERR_REPO_NOT_IN_PROJECT)


def _require_recorded_pull_request(
    db: Session, project: Project, repo_name: str, pr_number: int
) -> ProjectPullRequest:
    """Require that this project actually opened *pr_number* in *repo_name*.

    Merge and close took both straight from the request and checked neither
    against the project, so the target was caller-chosen. Every legitimate
    caller works from the project's own PR list, and _handle_merged_pull_request
    already looks this row up - it just tolerated the row being absent while
    still running the "no open PRs left" transitions afterwards, so merging an
    unrecorded pull request could move project and workflow state off the back
    of an unrelated merge.
    """
    pr_record = db.query(ProjectPullRequest).filter_by(
        project_id=project.project_id,
        repo_name=repo_name.strip(),
        pr_number=pr_number,
    ).first()
    if not pr_record:
        raise HTTPException(
            status_code=404,
            detail=f"No pull request #{pr_number} is recorded for this project in {repo_name}",
        )
    return pr_record


def _record_drift_check_failure(db: Session, project: Project, message: str) -> None:
    """Best-effort: persist that this project's drift check could not complete."""
    try:
        _cache_project_drift_summary(db, project, "check_failed", 0, message)
        record_drift_check_failed(db, project, message)
    except Exception:
        db.rollback()


def _determine_drift_summary_status(drifted: List[WorkflowDriftDetail], unchecked: List[WorkflowDriftDetail]) -> str:
    if drifted:
        return "drifted"
    if unchecked:
        # Some repos could not be reached, so "clean" would be a claim we
        # cannot support — the project list must not show a green badge
        # just because GitHub was rate-limiting.
        return "check_failed"
    return "clean"


def _resolve_workflow_owner_project(db: Session, workflow_id: int):
    """Return (workflow, project) or (None, None) for a workflow_id."""
    wf = db.query(Workflow).filter_by(workflow_id=workflow_id).first()
    if not wf:
        return None, None
    pw = db.query(ProjectWorkflow).filter_by(workflow_id=workflow_id).first()
    if not pw:
        return wf, None
    project = db.query(Project).filter_by(project_id=pw.project_id).first()
    return wf, project


def _stored_project_drift_details(db: Session, project: Project) -> List[WorkflowDriftDetail]:
    """Rebuild the drifted-workflow list from the delivery record, calling nothing.

    Everything here is either stored on the record's row or already local:
    the managed YAML comes from the project's own workflow (or its per-repo
    override), and the rest is what was delivered and what the last check saw.

    ``github_yaml`` is deliberately left unset. A diff is only meaningful
    against GitHub's *current* content, so the UI fetches it when the user
    opens one rather than replaying a snapshot that may already be stale.
    """
    states = deliveries.drifted_rows(db, project.project_id, deliveries.WORKFLOW)
    if not states:
        return []

    repo_name_by_id = {r.repo_id: r.repo_name for r in db.query(Repo).all()}
    project_repo_rows = (
        db.query(Repo).join(ProjectRepo).filter(ProjectRepo.project_id == project.project_id).all()
    )
    project_repo_names = [r.repo_name for r in project_repo_rows]
    repo_id_by_name = {r.repo_name: r.repo_id for r in project_repo_rows}
    overrides_by_key = {
        (o.workflow_id, o.repo_id): o
        for o in db.query(RepoWorkflowOverride).filter_by(project_id=project.project_id).all()
    }

    details: List[WorkflowDriftDetail] = []
    for state in states:
        workflow = db.query(Workflow).filter_by(workflow_id=state.workflow_id).first()
        repo_name = repo_name_by_id.get(state.repo_id)
        if not workflow or not repo_name:
            continue

        override = overrides_by_key.get((state.workflow_id, state.repo_id))
        filename = format_workflow_name(workflow.workflow_name, project.project_code or "", project.use_prefix)
        affected = _affected_repos_for(
            workflow.workflow_id, repo_name, project_repo_names, repo_id_by_name, overrides_by_key
        )

        details.append(WorkflowDriftDetail(
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.workflow_name,
            workflow_filename=filename,
            repo=repo_name,
            branch=state.branch or "",
            has_drift=True,
            actionsmanager_yaml=_expected_yaml(workflow, override),
            github_yaml=None,          # fetched on demand — see docstring
            actionsmanager_sha=state.delivered_sha,
            github_sha=state.github_sha,
            last_checked=_utc_iso(state.last_checked_at) or "",
            message=_drift_message("Workflow", _DELETED if state.deleted_in_github else _DIFFERS, repo_name),
            project_id=project.project_id,
            repo_id=state.repo_id,
            is_shared_workflow=len(project_repo_names) > 1,
            has_repo_override=override is not None,
            override_id=(override.id if override else None),
            affected_repos=affected,
            affected_repo_count=len(affected),
            source_repo_name=repo_name,
            deleted_in_github=bool(state.deleted_in_github),
        ))
    return details


def _stored_file_drift_details(db: Session, project: Project, kind: str) -> list:
    """The stored drift list's CODEOWNERS or custom-file rows, calling nothing (see _stored_project_drift_details)."""
    details = []
    for file in _single_files(db, project, kind):
        rows = deliveries.drifted_rows(db, project.project_id, kind, file.file_id)
        repo_names = dict(db.query(Repo.repo_id, Repo.repo_name).filter(Repo.repo_id.in_({row.repo_id for row in rows})))
        details += [
            file.detail(
                file_path=deliveries.path_on(row, file.path),
                repo=repo_names[row.repo_id],
                repo_id=row.repo_id,
                branch=row.branch or "",
                has_drift=True,
                actionsmanager_sha=row.delivered_sha,
                github_sha=row.github_sha,
                last_checked=_utc_iso(row.last_checked_at) or "",
                message=_drift_message(file.name, _DELETED if row.deleted_in_github else _DIFFERS,
                                       repo_names[row.repo_id]),
                deleted_in_github=bool(row.deleted_in_github),
            )
            for row in rows
        ]
    return details


def _drifted_of_kind(observations: List[DriftObservation], kind: str) -> list:
    return [o.detail for o in observations if o.kind == kind and o.detail.has_drift and not o.detail.check_failed]


def run_project_drift_check(db: Session, user: str, project: Project):
    """Check one project for drift, persist the result, return
    (drifted, unchecked, drifted_codeowners, drifted_custom_files).

    The single place a live drift check happens. Both "Check now" and the
    background sweep call this, so an automatic check can never diverge from
    the one a user asks for. ``unchecked`` holds workflow, CODEOWNERS and
    custom-file details alike.
    """
    _reset_drift_lookup_caches(db, project.project_id)
    try:
        # One read of the record for the whole check, for every kind of file.
        targets = deliveries.targets_for(db, project.project_id)
        details = _collect_project_drift_details(db, user, project, targets=targets)
        observed_files = _collect_file_observations(db, user, project, targets)
    except HTTPException:
        _record_drift_check_failure(db, project, "Drift detection request failed")
        raise
    except Exception as e:
        _record_drift_check_failure(db, project, str(e))
        raise

    files = [observation.detail for observation in observed_files]
    drifted = [d for d in details if d.has_drift and not d.check_failed]
    drifted_codeowners = _drifted_of_kind(observed_files, deliveries.CODEOWNERS)
    drifted_custom_files = _drifted_of_kind(observed_files, deliveries.CUSTOM_FILE)
    drifted_files = drifted_codeowners + drifted_custom_files
    unchecked = [d for d in details + files if d.check_failed]
    try:
        _cache_project_drift_summary(
            db,
            project,
            _determine_drift_summary_status(drifted + drifted_files, unchecked),
            len(drifted) + len(drifted_files),
            f"{len(unchecked)} file/repo pair(s) could not be checked" if unchecked else None,
        )
        record_drift_transitions(db, project, _workflow_observations(details) + observed_files)
    except Exception:
        db.rollback()

    return drifted, unchecked, drifted_codeowners, drifted_custom_files


@router.get(
    "/api/projects/{project_id}/drift",
    response_model=ProjectDriftSummary,
    responses={
        401: {"description": NOT_AUTHENTICATED_DETAIL},
        403: {"description": NOT_AUTHORIZED_PROJECT_DETAIL},
        404: {"description": "Project not found"},
        409: {"description": "More than one project has this name"},
        500: {"description": "Unexpected error while computing drift"},
    },
)
def get_project_drift(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    refresh: bool = False,
):
    """Project-level drift summary — all drifted workflows/repos/branches.

    Serves the last known state by default, which costs no GitHub API calls, so
    opening a project is free however often it is done. ``refresh=true`` runs a
    live check and updates that state.

    ``last_checked`` is the time the state was actually established, not the
    time of this request — otherwise "clean" would look freshly verified when
    nothing had been checked in days.
    """
    github_user = account.github_user
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

    project = db.query(Project).filter_by(project_id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=PROJECT_ERROR)

    # Authorization: the caller must be able to see this project (owner,
    # membership or admin), decided by its id rather than its name.
    if not _caller_can_see_project(db, github_user, project):
        raise HTTPException(status_code=403, detail=NOT_AUTHORIZED_PROJECT_DETAIL)

    if not refresh:
        drifted = _stored_project_drift_details(db, project)
        drifted_codeowners = _stored_file_drift_details(db, project, deliveries.CODEOWNERS)
        drifted_custom_files = _stored_file_drift_details(db, project, deliveries.CUSTOM_FILE)
        return ProjectDriftSummary(
            project_id=project.project_id,
            project_name=project.project_name,
            drift_count=len(drifted) + len(drifted_codeowners) + len(drifted_custom_files),
            drifted_workflows=drifted,
            drifted_codeowners=drifted_codeowners,
            drifted_custom_files=drifted_custom_files,
            # None when no check has ever run — the UI must say "not checked
            # yet" rather than implying a clean result.
            last_checked=_utc_iso(project.last_drift_check_at),
            unchecked_count=0,
            stale_reason=project.drift_error_summary,
            drift_status=project.drift_status or "unknown",
        )

    try:
        drifted, unchecked, drifted_codeowners, drifted_custom_files = run_project_drift_check(
            db, github_user, project)
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Drift detection failed: {e}")
        raise HTTPException(status_code=500, detail="Drift detection failed")

    return ProjectDriftSummary(
        project_id=project.project_id,
        project_name=project.project_name,
        drift_count=len(drifted) + len(drifted_codeowners) + len(drifted_custom_files),
        drifted_workflows=drifted,
        drifted_codeowners=drifted_codeowners,
        drifted_custom_files=drifted_custom_files,
        last_checked=datetime.now(timezone.utc).isoformat(),
        unchecked_count=len(unchecked),
        # What the check just saved, so the row and the Projects list agree.
        drift_status=project.drift_status or "unknown",
    )


@router.get("/api/workflows/{workflow_id}/drift", response_model=WorkflowDriftResponse, responses=_responses(401, 403, 404, 409, 500))
def get_workflow_drift(
    workflow_id: int,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Per-workflow drift detail — includes both YAML versions and SHAs per repo/branch."""
    github_user = account.github_user
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

    wf, project = _resolve_workflow_owner_project(db, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=_ERR_WORKFLOW_NOT_FOUND)
    if not project:
        raise HTTPException(status_code=404, detail="Workflow is not associated with a project")

    if not _caller_can_see_project(db, github_user, project):
        raise HTTPException(status_code=403, detail="Not authorized for this workflow")

    try:
        details = _collect_project_drift_details(db, github_user, project, only_workflow_id=workflow_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Drift detection failed: {e}")
        raise HTTPException(status_code=500, detail="Drift detection failed")

    return WorkflowDriftResponse(
        workflow_id=wf.workflow_id,
        workflow_name=wf.workflow_name,
        workflow_filename=format_workflow_name(wf.workflow_name, project.project_code or "", project.use_prefix),
        has_drift=any(d.has_drift for d in details),
        drift_details=details,
        last_checked=datetime.now(timezone.utc).isoformat(),
    )


def _apply_use_github_resolution(
    db: Session,
    github_user: str,
    wf: Workflow,
    repo: str,
    formatted_name: str,
    token: str,
    *,
    branch: str,
    expected_sha: Optional[str] = None,
) -> dict:
    """Overwrite ``wf``'s local YAML with the current GitHub version for one repo/branch.

    Pure mutation - does not re-check drift afterward. The single-workflow
    endpoint re-checks drift itself for response shaping; a bulk caller
    re-fetches the whole project's drift summary once at the end instead of
    once per item, since a live GitHub diff check per item would be far more
    expensive than the fetch this helper already does.

    ``branch`` is required and never empty: drift is per branch, and
    ``get_workflow_from_github`` reads the repo's default branch when given
    none, which imports a different file than the one the user was shown (#2300).

    ``expected_sha`` is the blob SHA the user's decision was based on. Adopting
    GitHub's version discards the managed YAML, so if GitHub moved on since the
    drift was shown the user would be accepting content they never saw — the
    mirror image of the stale overwrite the restore path guards against.

    Returns {"success": bool, "github_sha": str|None, "error": str|None,
    "conflict": bool}.
    """
    if not branch:
        raise ValueError(_ERR_ADOPT_NEEDS_BRANCH)
    owner, repo_short = repo.split("/", 1)
    filename = _delivered_filename(db, wf, repo, branch, formatted_name)
    github_data = get_workflow_from_github(owner, repo_short, filename, token, default_branch=branch)
    if not github_data:
        return {
            "success": False,
            "github_sha": None,
            "error": f"Workflow '{filename}' not found in {repo}@{branch}",
        }

    github_content = (github_data.get("content") or "").strip()
    github_sha = github_data.get("sha")

    if expected_sha and github_sha and github_sha != expected_sha:
        return {
            "success": False,
            "conflict": True,
            "github_sha": github_sha,
            "error": (
                f"{filename} in {repo}@{branch} changed since drift was checked. "
                "Re-run the drift check and review the current difference before resolving."
            ),
        }

    _update_project_workflow(db, wf, github_content, github_sha, github_user, renamed=filename != formatted_name)
    # ActionsManager's copy is now what GitHub holds on this branch.
    deliveries.record_delivered(db, deliveries.WORKFLOW, wf.workflow_id, repo, branch,
                                _WORKFLOWS_DIR + filename, github_sha)
    db.commit()

    return {"success": True, "github_sha": github_sha, "error": None}


def _resolve_use_github_drift(db: Session, github_user: str, wf, project, payload: ResolveWorkflowDriftRequest,
                               branch: str, workflow_id: int, formatted_name: str, token: str) -> dict:
    """Overwrite local YAML with GitHub's version, then re-check drift to confirm resolution."""
    print(f"🔄 Resolving drift for workflow {workflow_id} (use_github): {payload.repo}@{payload.branch}")
    print(f"  Old local SHA: {wf.workflow_git_hash}")
    print(f"  Old local content length: {len(wf.workflow_yaml or '')}")

    resolution = _apply_use_github_resolution(
        db, github_user, wf, payload.repo, formatted_name, token,
        branch=branch, expected_sha=payload.expected_github_sha,
    )
    if not resolution["success"]:
        if resolution.get("conflict"):
            raise HTTPException(status_code=409, detail=resolution["error"])
        raise HTTPException(status_code=404, detail=resolution["error"])
    github_sha = resolution["github_sha"]

    print(f"  New GitHub SHA: {github_sha}")
    print("  ✅ Database updated and committed")

    # Refresh the workflow from database to ensure we have the latest state
    db.refresh(wf)

    # Re-check drift with the updated workflow to confirm resolution
    print("  🔍 Re-checking drift after resolution...")
    try:
        drift_details = _collect_project_drift_details(
            db, github_user, project, only_workflow_id=workflow_id
        )

        # This re-check is authoritative, so persist it instead of leaving the
        # caches claiming drift that was just resolved. record_drift_transitions
        # only touches the rows it is given, and only_workflow_id scopes those
        # to this workflow.
        record_drift_transitions(db, project, _workflow_observations(drift_details))
        recompute_project_drift_summary(db, project)

        # Find the specific drift detail for this repo/branch
        matching_drift = None
        for detail in drift_details:
            if detail.repo == payload.repo and detail.branch == branch:
                matching_drift = detail
                break

        if not matching_drift:
            # No drift detail found for this repo/branch after resolution
            print("  ✅ No drift detail found - assuming synced")
            return {
                "message": f"Workflow '{wf.workflow_name}' updated from GitHub version",
                "action": "use_github",
                "workflow_id": wf.workflow_id,
                "repo": payload.repo,
                "branch": branch,
                "state": "synced",
                "stored_hash": wf.workflow_git_hash,
                "github_hash": github_sha,
                "content_matches": True,
            }

        has_drift_after = matching_drift.has_drift
        print(f"  Drift state after resolution: {'drifted' if has_drift_after else 'synced'}")

        if has_drift_after:
            # Drift still exists - something went wrong
            print("  ⚠️ WARNING: Drift was not resolved!")
            print(f"    Local content length: {len(wf.workflow_yaml or '')}")
            print(f"    GitHub content length: {len(matching_drift.github_yaml or '')}")
            print(f"    Message: {matching_drift.message}")

            return {
                "message": f"Drift resolution failed: {matching_drift.message}",
                "action": "use_github",
                "workflow_id": wf.workflow_id,
                "repo": payload.repo,
                "branch": branch,
                "state": "drifted",
                "stored_hash": wf.workflow_git_hash,
                "github_hash": matching_drift.github_sha,
                "content_matches": False,
            }

        print("  ✅ Drift successfully resolved")
        return {
            "message": f"GitHub version kept. Workflow '{wf.workflow_name}' is now synced.",
            "action": "use_github",
            "workflow_id": wf.workflow_id,
            "repo": payload.repo,
            "branch": branch,
            "state": "synced",
            "stored_hash": wf.workflow_git_hash,
            "github_hash": matching_drift.github_sha,
            "content_matches": True,
        }
    except Exception as drift_check_error:
        print(f"  ⚠️ Error re-checking drift: {drift_check_error}")
        # Database was updated, so return success but note drift check failed
        return {
            "message": f"Workflow '{wf.workflow_name}' updated from GitHub version (drift check failed: {failure_kind(drift_check_error, _SHOWN_GITHUB_ERRORS)})",
            "action": "use_github",
            "workflow_id": wf.workflow_id,
            "repo": payload.repo,
            "branch": branch,
            "state": "unknown",
            "stored_hash": wf.workflow_git_hash,
            "github_hash": github_sha,
        }


def _require_pr_delivery(delivery_mode: Optional[str]) -> None:
    """Refuse any drift fix that is not a pull request (the field is kept so old callers sending "pr" still work)."""
    if (delivery_mode or "pr").lower() != "pr":
        raise HTTPException(status_code=400, detail=DELIVERY_MODE_ERROR_DETAIL)


def _campaign_selection(workflows) -> dict:
    """The campaign selection that delivers ``workflows``, and nothing else — a restore or a removal.

    ``selected_workflows`` only ever selects regular rows, so a reusable one
    named there matched nothing and the campaign opened no pull request. A
    standard project's own reusable workflows live in its repos (#2085), so
    restoring or removing one has to select it the way a campaign does.
    """
    regular = [w.workflow_name for w in workflows if not w.reusable_workflow]
    reusable = [w.workflow_name for w in workflows if w.reusable_workflow]
    return {
        "selected_workflows": regular or None,
        "selected_reusable_workflows": reusable or None,
        # [] rather than None, which means every locally changed custom file:
        # a pull request fixing one workflow must carry nothing else.
        "selected_custom_file_ids": [],
    }


def _resolve_restore_actionsmanager_drift(db: Session, github_user: str, wf, project,
                                           payload: ResolveWorkflowDriftRequest, branch: str, workflow_id: int) -> dict:
    """Put the managed version back on GitHub through a pull request, carrying only this workflow.

    Drift is never restored by committing straight to a branch: every fix is a
    reviewed pull request, for workflows as for the project's other files.
    """
    _require_pr_delivery(payload.delivery_mode)
    print(f"🔄 Resolving drift for workflow {workflow_id} (restore_actionsmanager, pr): {payload.repo}@{payload.branch}")
    try:
        # create_pull_requests is a route function whose signature is
        # (payload, background_tasks, db, ...) - when called directly (not via
        # FastAPI routing) db has no injected default, so background_tasks must
        # be supplied positionally and db passed explicitly. async_mode
        # defaults False, so the BackgroundTasks instance is never used.
        pr_payload = CreatePullRequestsRequest(
            project_name=project.project_name,
            project_id=project.project_id,
            selected_repos=[payload.repo],
            **_campaign_selection([wf]),
        )
        pr_result = create_pull_requests(
            pr_payload, BackgroundTasks(), db, github_user=github_user
        )
        print("  ✅ PR created for restoring workflow")
        return {
            "message": f"PR opened to restore '{wf.workflow_name}' in {payload.repo}",
            "action": "restore_actionsmanager",
            "delivery_mode": "pr",
            "workflow_id": wf.workflow_id,
            "repo": payload.repo,
            "branch": branch,
            "state": "pr_pending",
            "pr_result": pr_result,
        }
    except HTTPException:
        # Already-shaped errors (pipeline failures, bad input) carry a useful
        # status + detail - let them through unchanged.
        raise
    except Exception as e:  # noqa: BLE001 - convert unexpected errors to a safe 500
        import traceback
        traceback.print_exc()
        db.rollback()
        # Server-side context for diagnosis - never logs the token/headers.
        print(
            f"❌ Drift restore failed (workflow_id={workflow_id}, "
            f"repo={payload.repo}, branch={branch}): {type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=500, detail="Failed to restore workflow via a pull request")


@router.post(
    "/api/workflows/{workflow_id}/resolve-drift",
    responses={
        400: {"description": "resolution or delivery_mode is invalid"},
        401: {"description": NOT_AUTHENTICATED_DETAIL},
        403: {"description": "Not authorized for this workflow, or caller lacks project_editor rights"},
        404: {"description": "Workflow, project, or GitHub file not found"},
        409: {"description": _ERR_DRIFT_STALE},
        500: {"description": "Unexpected error during drift restoration"},
        502: {"description": "Upstream GitHub request failed"},
    },
)
def resolve_workflow_drift(
    workflow_id: int,
    payload: ResolveWorkflowDriftRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Resolve drift for a single workflow on a specific repo/branch.

    Resolutions:
      * ``use_github``               – overwrite local YAML with GitHub YAML and refresh hash.
      * ``restore_actionsmanager``   – put the local YAML back on GitHub through a pull request.
    """
    github_user = account.github_user
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

    wf, project = _resolve_workflow_owner_project(db, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail=_ERR_WORKFLOW_NOT_FOUND)
    if not project:
        raise HTTPException(status_code=404, detail="Workflow is not associated with a project")

    if not _caller_can_see_project(db, github_user, project):
        raise HTTPException(status_code=403, detail="Not authorized for this workflow")

    # Resolving drift writes to GitHub, so seeing the project is not enough.
    _require_project_editor(db, github_user, project)
    # And the target must be one of the project's own repos, not any repo the
    # caller's token happens to be able to write to.
    _require_repo_in_project(db, project, payload.repo, github_user)

    # No silent fallback: defaulting to "main" because the caller omitted a
    # branch would act on a branch the user never chose.
    branch = (payload.branch or "").strip()
    if not branch:
        raise HTTPException(status_code=400, detail="branch is required to restore a workflow")

    if payload.resolution == "use_github":
        formatted_name = format_workflow_name(wf.workflow_name, project.project_code or "", project.use_prefix)
        return _resolve_use_github_drift(db, github_user, wf, project, payload, branch, workflow_id, formatted_name,
                                         user_tokens[github_user])

    if payload.resolution == "restore_actionsmanager":
        return _resolve_restore_actionsmanager_drift(db, github_user, wf, project, payload, branch, workflow_id)

    raise HTTPException(
        status_code=400,
        detail="resolution must be 'use_github' or 'restore_actionsmanager'",
    )


def _validate_bulk_resolve_items(db: Session, project_id: int, items) -> list:
    """Resolve + validate every item up front.

    Fails the whole batch before any writes if one item doesn't actually
    belong to this project.
    """
    resolved_items = []
    for item in items:
        wf, wf_project = _resolve_workflow_owner_project(db, item.workflow_id)
        if not wf:
            raise HTTPException(status_code=404, detail=f"Workflow {item.workflow_id} not found")
        if not wf_project or wf_project.project_id != project_id:
            raise HTTPException(
                status_code=403,
                detail=f"Workflow {item.workflow_id} is not part of this project",
            )
        if "/" not in item.repo:
            raise HTTPException(status_code=400, detail="repo must be in 'owner/repo' format")
        resolved_items.append((item, wf))
    return resolved_items


def _bulk_resolve_use_github(
    db: Session, github_user: str, project: Project, resolved_items: list, token: str
) -> List[BulkResolveDriftItemResult]:
    results: List[BulkResolveDriftItemResult] = []
    for item, wf in resolved_items:
        formatted_name = format_workflow_name(wf.workflow_name, project.project_code or "", project.use_prefix)
        # The item names the branch the user was looking at. Reading the repo
        # default instead would import a different file than the one they were
        # shown, so an item without one fails, as the single route's 400 does.
        branch = (item.branch or "").strip()
        if not branch:
            results.append(BulkResolveDriftItemResult(
                workflow_id=item.workflow_id, repo=item.repo, branch=item.branch,
                success=False, message=_ERR_ADOPT_NEEDS_BRANCH,
            ))
            continue
        try:
            outcome = _apply_use_github_resolution(
                db, github_user, wf, item.repo, formatted_name, token,
                branch=branch, expected_sha=item.expected_github_sha,
            )
            if outcome["success"]:
                clear_drift(db, project, deliveries.WORKFLOW, wf.workflow_id, item.repo, branch)
            results.append(BulkResolveDriftItemResult(
                workflow_id=item.workflow_id,
                repo=item.repo,
                branch=item.branch,
                success=outcome["success"],
                message=outcome["error"] or f"Workflow '{wf.workflow_name}' updated from GitHub version",
            ))
        except Exception as e:  # noqa: BLE001 - convert unexpected errors to a per-item failure
            db.rollback()
            results.append(BulkResolveDriftItemResult(
                workflow_id=item.workflow_id, repo=item.repo, branch=item.branch,
                success=False, message=f"Failed to adopt GitHub version: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}",
            ))
    return results


def _build_pr_item_result(item, wf, repo: str, matching: dict, top_level_error) -> BulkResolveDriftItemResult:
    ok = matching.get("status") in ("pr_created", "pr_updated")
    if ok:
        message = f"PR opened to restore '{wf.workflow_name}' in {repo}"
    else:
        message = (
            f"Failed to create PR for '{wf.workflow_name}' in {repo}: "
            f"{matching.get('error') or top_level_error or 'unknown error'}"
        )
    return BulkResolveDriftItemResult(
        workflow_id=item.workflow_id, repo=item.repo, branch=item.branch,
        success=ok, message=message,
        pr_url=matching.get("pr_url") if ok else None,
    )


def _bulk_resolve_pr_group(
    db: Session, github_user: str, project: Project, repo: str, group: list,
) -> List[BulkResolveDriftItemResult]:
    """Create one PR covering every workflow in ``group`` (all share ``repo``)."""
    try:
        pr_payload = CreatePullRequestsRequest(
            project_name=project.project_name,
            project_id=project.project_id,
            selected_repos=[repo],
            **_campaign_selection([wf for _item, wf in group]),
        )
        pr_result = create_pull_requests(
            pr_payload, BackgroundTasks(), db, github_user=github_user
        )
        pr_results_by_key = pr_result.get("results", {}) if isinstance(pr_result, dict) else {}
        top_level_error = pr_results_by_key.get("error") if isinstance(pr_results_by_key, dict) else None
        results = []
        for item, wf in group:
            branch = item.branch or "main"
            matching = pr_results_by_key.get(f"{repo} on {branch}", {})
            matching = matching if isinstance(matching, dict) else {}
            results.append(_build_pr_item_result(item, wf, repo, matching, top_level_error))
        return results
    except HTTPException as e:
        return [
            BulkResolveDriftItemResult(
                workflow_id=item.workflow_id, repo=item.repo, branch=item.branch,
                success=False, message=str(e.detail),
            )
            for item, wf in group
        ]
    except Exception as e:  # noqa: BLE001 - convert unexpected errors to a per-item failure
        db.rollback()
        return [
            BulkResolveDriftItemResult(
                workflow_id=item.workflow_id, repo=item.repo, branch=item.branch,
                success=False, message=f"Failed to create PR: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}",
            )
            for item, wf in group
        ]


def _bulk_resolve_restore_pr(
    db: Session, github_user: str, project: Project, resolved_items: list
) -> List[BulkResolveDriftItemResult]:
    """Group items by repo and call create_pull_requests once per repo.

    Multiple workflows restored to the SAME repo land in one PR -
    _process_regular_workflows_update already commits every workflow in
    selected_workflows to a single Actions Manager branch/PR per repo (branch
    isn't forwarded to create_pull_requests - it resolves target branches
    itself from the project's branch_option, same as the single-item
    endpoint).
    """
    by_repo: dict = {}
    for item, wf in resolved_items:
        by_repo.setdefault(item.repo, []).append((item, wf))

    results: List[BulkResolveDriftItemResult] = []
    for repo, group in by_repo.items():
        results.extend(_bulk_resolve_pr_group(db, github_user, project, repo, group))
    return results


@router.post(
    "/api/projects/{project_id}/drift/bulk-resolve",
    response_model=BulkResolveDriftResponse,
    responses={
        400: {"description": "Invalid resolution, delivery_mode, repo format, or empty items list"},
        401: {"description": NOT_AUTHENTICATED_DETAIL},
        403: {"description": NOT_AUTHORIZED_PROJECT_DETAIL},
        404: {"description": "Project or workflow not found"},
        409: {"description": _ERR_DRIFT_STALE},
        500: {"description": "Unexpected error during drift resolution"},
    },
)
def bulk_resolve_project_drift(
    project_id: int,
    payload: BulkResolveDriftRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Resolve drift for multiple workflows in one request.

    All items must belong to workflows owned by ``project_id``. Partial
    failure is supported - one bad item doesn't block the rest.
    """
    github_user = account.github_user
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

    if payload.resolution not in ("use_github", "restore_actionsmanager"):
        raise HTTPException(
            status_code=400,
            detail="resolution must be 'use_github' or 'restore_actionsmanager'",
        )
    if not payload.items:
        raise HTTPException(status_code=400, detail="items must not be empty")

    project = db.query(Project).filter_by(project_id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=PROJECT_ERROR)

    if not _caller_can_see_project(db, github_user, project):
        raise HTTPException(status_code=403, detail=NOT_AUTHORIZED_PROJECT_DETAIL)

    # Bulk resolve writes to GitHub for every item, so require editor rights.
    _require_project_editor(db, github_user, project)

    resolved_items = _validate_bulk_resolve_items(db, project_id, payload.items)
    # Validate every target repo up front — the batch is rejected whole rather
    # than pushing to the valid repos and failing partway.
    for item, _wf in resolved_items:
        _require_repo_in_project(db, project, item.repo, github_user)
    token = user_tokens[github_user]

    if payload.resolution == "use_github":
        results = _bulk_resolve_use_github(db, github_user, project, resolved_items, token)
    else:  # restore_actionsmanager
        _require_pr_delivery(payload.delivery_mode)
        results = _bulk_resolve_restore_pr(db, github_user, project, resolved_items)

    return BulkResolveDriftResponse(success=all(r.success for r in results), results=results)


# -----------------------------------------------------------------------------
# Scope-aware drift resolution (issue: design-level drift fix)
# -----------------------------------------------------------------------------

ADOPT_PROJECT_AND_SYNC = "adopt_project_and_sync"
ADOPT_LOCAL_ONLY = "adopt_local_only"
CREATE_REPO_OVERRIDE = "create_repo_override"

_VALID_RESOLUTION_MODES = {ADOPT_PROJECT_AND_SYNC, ADOPT_LOCAL_ONLY, CREATE_REPO_OVERRIDE}


def _validate_adopt_github_request(
    db: Session,
    payload: AdoptGithubVersionRequest,
    github_user: str,
) -> tuple:
    """Validate authentication, authorization, and return validated entities.

    Returns:
        tuple: (project, workflow, source_repo, token, formatted_name, owner, repo)

    Raises:
        HTTPException: On validation failure
    """
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

    if payload.resolution_mode not in _VALID_RESOLUTION_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"resolution_mode must be one of: {sorted(_VALID_RESOLUTION_MODES)}",
        )
    if not (payload.branch or "").strip():
        raise HTTPException(status_code=400, detail=_ERR_ADOPT_NEEDS_BRANCH)
    payload.branch = payload.branch.strip()
    # Before anything is adopted: a refused sync must not leave the project changed.
    _require_pr_delivery(payload.delivery_mode)

    project = db.query(Project).filter_by(project_id=payload.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=PROJECT_ERROR)

    if not _caller_can_see_project(db, github_user, project):
        raise HTTPException(status_code=403, detail=NOT_AUTHORIZED_PROJECT_DETAIL)

    # Adopting can push to every project repo, so require editor rights.
    # (_resolve_source_repo below already checks the repo belongs to the project.)
    _require_project_editor(db, github_user, project)

    workflow = db.query(Workflow).filter_by(workflow_id=payload.workflow_id).first()
    if not workflow:
        raise HTTPException(status_code=404, detail=_ERR_WORKFLOW_NOT_FOUND)

    pw = db.query(ProjectWorkflow).filter_by(
        project_id=project.project_id, workflow_id=workflow.workflow_id,
    ).first()
    if not pw:
        raise HTTPException(status_code=404, detail="Workflow is not associated with this project")

    source_repo = _resolve_source_repo(db, payload, project)
    if "/" not in source_repo.repo_name:
        raise HTTPException(status_code=400, detail="Source repo must be in 'owner/repo' format")

    owner, repo = source_repo.repo_name.split("/", 1)
    token = user_tokens[github_user]
    formatted_name = format_workflow_name(
        workflow.workflow_name, project.project_code or "", project.use_prefix,
    )

    return project, workflow, source_repo, token, formatted_name, owner, repo


def _resolve_source_repo(db: Session, payload: AdoptGithubVersionRequest, project: Project) -> Repo:
    """Resolve source repo by id or name and validate it belongs to the project.

    Returns:
        Repo: The resolved source repo

    Raises:
        HTTPException: If repo not found or not part of the project
    """
    source_repo = None
    if payload.repo_id is not None:
        source_repo = db.query(Repo).filter_by(repo_id=payload.repo_id).first()
    if source_repo is None and payload.repo_name:
        source_repo = db.query(Repo).filter_by(repo_name=payload.repo_name).first()
    if source_repo is None:
        raise HTTPException(status_code=404, detail="Source repo not found")

    in_project = db.query(ProjectRepo).filter_by(
        project_id=project.project_id, repo_id=source_repo.repo_id,
    ).first()
    if not in_project:
        raise HTTPException(status_code=400, detail="Source repo is not part of this project")

    return source_repo


def _fetch_github_content_and_affected_repos(
    db: Session,
    project: Project,
    workflow: Workflow,
    source_repo: Repo,
    formatted_name: str,
    owner: str,
    repo: str,
    token: str,
    branch: str,
) -> tuple:
    """Fetch GitHub workflow content and identify affected repos.

    ``branch`` is the branch whose drift is being resolved. Falling back to the
    repo default would adopt a different file than the one the user was shown.

    Returns:
        tuple: (github_content, github_sha, affected_repos)

    Raises:
        HTTPException: If workflow not found on GitHub
    """
    github_data = get_workflow_from_github(owner, repo, formatted_name, token, default_branch=branch)
    if not github_data:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{formatted_name}' not found in {source_repo.repo_name}@{branch}",
        )
    github_content = (github_data.get("content") or "").strip()
    github_sha = github_data.get("sha")

    project_repos = (
        db.query(Repo).join(ProjectRepo).filter(
            ProjectRepo.project_id == project.project_id,
        ).all()
    )
    overrides_by_repo = {
        o.repo_id: o for o in db.query(RepoWorkflowOverride).filter_by(
            project_id=project.project_id, workflow_id=workflow.workflow_id,
        ).all()
    }
    affected_repos = [
        r for r in project_repos
        if r.repo_id != source_repo.repo_id and r.repo_id not in overrides_by_repo
    ]

    return github_content, github_sha, affected_repos


def _handle_create_repo_override(
    db: Session,
    project: Project,
    workflow: Workflow,
    source_repo: Repo,
    github_content: str,
    github_sha: str,
    github_user: str,
    affected_repos: list,
) -> dict:
    """Handle CREATE_REPO_OVERRIDE resolution mode.

    Creates or updates a per-repo override without modifying the project workflow.

    Returns:
        dict: API response for the override operation
    """
    existing = db.query(RepoWorkflowOverride).filter_by(
        project_id=project.project_id,
        repo_id=source_repo.repo_id,
        workflow_id=workflow.workflow_id,
    ).first()

    if existing:
        existing.workflow_yaml = github_content
        existing.workflow_git_hash = github_sha
        existing.source_repo_name = source_repo.repo_name
        existing.last_modified_by = github_user
        override = existing
        created = False
    else:
        override = RepoWorkflowOverride(
            project_id=project.project_id,
            repo_id=source_repo.repo_id,
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.workflow_name,
            workflow_yaml=github_content,
            workflow_git_hash=github_sha,
            source_repo_name=source_repo.repo_name,
            last_modified_by=github_user,
        )
        db.add(override)
        created = True

    db.commit()
    db.refresh(override)
    # Invalidate cached overrides for this project
    db.info.get("repo_workflow_overrides_by_project", {}).pop(project.project_id, None)

    return {
        "success": True,
        "message": (
            f"Repo override {'created' if created else 'updated'} for "
            f"{source_repo.repo_name}. The shared project workflow was not modified."
        ),
        "resolution_mode": CREATE_REPO_OVERRIDE,
        "updated_project_workflow": False,
        "created_or_updated_override": {
            "override_id": override.id,
            "project_id": override.project_id,
            "repo_id": override.repo_id,
            "workflow_id": override.workflow_id,
            "workflow_name": override.workflow_name,
            "source_repo_name": override.source_repo_name,
            "workflow_git_hash": override.workflow_git_hash,
        },
        "affected_repos": [r.repo_name for r in affected_repos],
        "sync_results": None,
        "new_drift_status": "synced",
    }


def _update_project_workflow(
    db: Session,
    workflow: Workflow,
    github_content: str,
    github_sha: str,
    github_user: str,
    renamed: bool = False,
) -> None:
    """Update the project workflow content from GitHub version.

    *renamed*: GitHub holds it under the name it was delivered by, not its
    current one, so the rename still waits for a campaign to deliver it.
    """
    workflow.workflow_yaml = github_content
    workflow.workflow_git_hash = github_sha
    workflow.last_modified_by = github_user
    workflow.workflow_status = "committed_locally" if renamed else "synced_with_github"
    db.commit()
    db.refresh(workflow)


def _handle_adopt_local_only(
    db: Session,
    project: Project,
    source_repo: Repo,
    affected_repos: list,
) -> dict:
    """Handle ADOPT_LOCAL_ONLY resolution mode.

    Returns:
        dict: API response for the local-only adoption
    """
    if affected_repos and project.pr_state == "synced":
        project.pr_state = "draft"
        db.commit()

    return {
        "success": True,
        "message": (
            f"Project workflow updated from GitHub version in "
            f"{source_repo.repo_name}. Other repositories using this "
            f"shared workflow keep the previous version until it is delivered to them."
        ),
        "resolution_mode": ADOPT_LOCAL_ONLY,
        "updated_project_workflow": True,
        "created_or_updated_override": None,
        "affected_repos": [r.repo_name for r in affected_repos],
        "sync_results": None,
        "new_drift_status": "drifted_on_other_repos" if affected_repos else "synced",
    }


def _get_target_repos_for_sync(
    payload: AdoptGithubVersionRequest,
    affected_repos: list,
) -> list:
    """Filter affected repos by caller-supplied target_repo_ids when present.

    Returns:
        list: Target repos to sync
    """
    if payload.target_repo_ids is not None:
        target_set = set(payload.target_repo_ids)
        return [r for r in affected_repos if r.repo_id in target_set]
    return list(affected_repos)


def _handle_sync_pr_mode(
    db: Session,
    project: Project,
    workflow: Workflow,
    source_repo: Repo,
    target_repos: list,
    github_user: str,
) -> dict:
    """Handle PR delivery mode for adopt_project_and_sync.

    Returns:
        dict: API response for the PR-based sync
    """
    pr_payload = CreatePullRequestsRequest(
        project_name=project.project_name,
        project_id=project.project_id,
        selected_repos=[r.repo_name for r in target_repos],
        **_campaign_selection([workflow]),
    )
    # create_pull_requests is a route function (payload, background_tasks, db,
    # ...); called directly it needs background_tasks supplied and db passed
    # explicitly. async_mode defaults False, so BackgroundTasks() is unused.
    pr_result = create_pull_requests(
        pr_payload, BackgroundTasks(), db, github_user=github_user
    )

    if project.pr_state in ("new", "draft", "synced"):
        project.pr_state = "open"
        db.commit()

    sync_results: dict = {
        "delivery_mode": "pr",
        "repos": [{"repo": r.repo_name, "status": "pr_requested"} for r in target_repos],
        "pr_result": pr_result,
    }

    return {
        "success": True,
        "message": (
            f"Project workflow adopted from {source_repo.repo_name}. PR(s) "
            f"opened to sync {len(target_repos)} repo(s)."
        ),
        "resolution_mode": ADOPT_PROJECT_AND_SYNC,
        "updated_project_workflow": True,
        "created_or_updated_override": None,
        "affected_repos": [r.repo_name for r in target_repos],
        "sync_results": sync_results,
        "new_drift_status": "pr_pending",
    }


def _handle_adopt_project_and_sync(
    db: Session,
    payload: AdoptGithubVersionRequest,
    project: Project,
    workflow: Workflow,
    source_repo: Repo,
    affected_repos: list,
    github_user: str,
) -> dict:
    """Handle ADOPT_PROJECT_AND_SYNC: open pull requests carrying the adopted workflow to the other repos.

    Returns:
        dict: API response for the sync operation
    """
    target_repos = _get_target_repos_for_sync(payload, affected_repos)

    if not target_repos:
        return {
            "success": True,
            "message": (
                f"Project workflow adopted from {source_repo.repo_name}. "
                "No other repositories require syncing."
            ),
            "resolution_mode": ADOPT_PROJECT_AND_SYNC,
            "updated_project_workflow": True,
            "created_or_updated_override": None,
            "affected_repos": [],
            "sync_results": {"delivery_mode": "pr", "repos": []},
            "new_drift_status": "synced",
        }

    return _handle_sync_pr_mode(
        db, project, workflow, source_repo, target_repos, github_user,
    )


@router.post(
    "/api/drift/adopt-github-version",
    responses={
        400: {"description": "Invalid resolution_mode, delivery_mode or repo format, or no branch"},
        401: {"description": NOT_AUTHENTICATED_DETAIL},
        403: {"description": NOT_AUTHORIZED_PROJECT_DETAIL},
        404: {"description": "Project or workflow not found"},
        409: {"description": _ERR_DRIFT_STALE},
        500: {"description": "Unexpected error during drift adoption"},
    },
)
def adopt_github_version(
    payload: AdoptGithubVersionRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Scope-aware drift resolution flow.

    Replaces the simple "Keep GitHub Version" action with three explicit modes:

      * ``adopt_project_and_sync`` – Use the GitHub version from the source repo
        as the new project workflow content, then sync the other repos in the
        project through pull requests.
      * ``adopt_local_only``       – Update only the local project workflow.
        Other repos sharing it keep the previous version until it is delivered
        to them; they still hold what was delivered, so they are not drift.
      * ``create_repo_override``   – Record a per-repo override so the source
        repo can intentionally diverge from the shared project workflow without
        re-triggering drift on every detection cycle.
    """
    github_user = account.github_user

    # Validate request and get entities
    project, workflow, source_repo, token, formatted_name, owner, repo = (
        _validate_adopt_github_request(db, payload, github_user)
    )

    # Fetch GitHub content and identify affected repos
    read_name = _delivered_filename(db, workflow, source_repo.repo_name, payload.branch, formatted_name)
    github_content, github_sha, affected_repos = _fetch_github_content_and_affected_repos(
        db, project, workflow, source_repo, read_name, owner, repo, token, payload.branch,
    )

    # Handle CREATE_REPO_OVERRIDE mode
    if payload.resolution_mode == CREATE_REPO_OVERRIDE:
        return _handle_create_repo_override(
            db, project, workflow, source_repo, github_content, github_sha,
            github_user, affected_repos,
        )

    # Update project workflow for ADOPT_LOCAL_ONLY and ADOPT_PROJECT_AND_SYNC
    _update_project_workflow(db, workflow, github_content, github_sha, github_user,
                             renamed=read_name != formatted_name)
    deliveries.record_delivered(db, deliveries.WORKFLOW, workflow.workflow_id, source_repo.repo_name,
                                payload.branch, _WORKFLOWS_DIR + read_name, github_sha)
    # The managed version now matches GitHub in the source repo, so its
    # persisted drift is resolved. Other repos are left alone — they may
    # legitimately still drift against the newly adopted content.
    clear_drift(db, project, deliveries.WORKFLOW, workflow.workflow_id, source_repo.repo_name, payload.branch)

    # Handle ADOPT_LOCAL_ONLY mode
    if payload.resolution_mode == ADOPT_LOCAL_ONLY:
        return _handle_adopt_local_only(db, project, source_repo, affected_repos)

    # Handle ADOPT_PROJECT_AND_SYNC mode
    return _handle_adopt_project_and_sync(db, payload, project, workflow, source_repo, affected_repos, github_user)


def _get_project_and_token(payload: "CreatePullRequestsRequest", db: Session, github_user: str = None):
    """Validate authentication and return (token, project).

    ``github_user`` is the authenticated caller (#2241); payloads carry no caller name.
    """
    user = github_user or ""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
    token = user_tokens[user]
    project = _find_project_by_name(db, user, payload.project_name, getattr(payload, "project_id", None))
    if not project:
        raise HTTPException(status_code=404, detail=PROJECT_ERROR)
    return token, project


def _get_filtered_repo_names(project: Project, selected_repos, db: Session):
    """Return repo names for the project, filtered by selected_repos when provided."""
    project_repos = db.query(Repo).join(ProjectRepo).filter(
        ProjectRepo.project_id == project.project_id
    ).all()
    if not project_repos:
        raise HTTPException(status_code=404, detail="No repositories found for this project")
    repo_names = [repo.repo_name for repo in project_repos]
    if selected_repos is not None:
        if not selected_repos:
            raise HTTPException(status_code=400, detail="No valid repositories selected")
        repo_names = [r for r in repo_names if r in selected_repos]
    if not repo_names:
        raise HTTPException(status_code=400, detail="No valid repositories selected")
    return repo_names


def _get_validation_repo_name(project: Project, db: Session) -> Optional[str]:
    if not getattr(project, "validation_repo_id", None):
        return None
    repo = db.query(Repo).filter(Repo.repo_id == project.validation_repo_id).first()
    return repo.repo_name if repo else None


def _set_preflight_result(
    project: Project,
    status: str,
    db: Session,
    error: Optional[str] = None,
    pr_url: Optional[str] = None,
) -> None:
    project.last_preflight_status = status
    project.last_preflight_run_at = datetime.now(timezone.utc)
    project.last_preflight_error = (error[:500] if error else None)
    project.last_preflight_pr_url = pr_url
    db.commit()


def _sanitize_preflight_error(value: object) -> str:
    message = str(value or "Preflight validation failed.")
    message = re.sub(r"(token|authorization)\s+[A-Za-z0-9_.\-:]+", r"\1 [redacted]", message, flags=re.IGNORECASE)
    message = re.sub(r"(ghp|github_pat)_[A-Za-z0-9_]+", "[redacted]", message)
    return message[:500]


def _extract_preflight_results(results: dict, validation_repo: str) -> tuple[list, Optional[str]]:
    """Extract failures and PR URL from preflight workflow processing results."""
    failed = []
    pr_url = None
    for result in results.values():
        if not isinstance(result, dict) or result.get("status") not in ["pr_created", "pr_updated"]:
            failed.append(result.get("error") if isinstance(result, dict) else "Preflight failed")
        elif not pr_url:
            try:
                pr_number = int(result.get("pr_number"))
                pr_url = f"https://github.com/{validation_repo}/pull/{pr_number}"
            except (TypeError, ValueError):
                pr_url = None
    return failed, pr_url


def _compute_preflight_content_hash(workflow_dicts: list, validation_repo: str) -> str:
    """Return a stable SHA-256 fingerprint of a set of workflows + validation repo.

    The hash is computed over all regular workflows for the project (sorted by name)
    and the validation repository name so that any content change — editing YAML,
    adding/removing a workflow, renaming one, or changing the validation repository —
    produces a different fingerprint and invalidates a prior approval.
    """
    sorted_wfs = sorted(workflow_dicts, key=lambda w: (w.get("name") or ""))
    fingerprint_data = {
        "validation_repo": validation_repo,
        "workflows": [{"name": w.get("name"), "content": w.get("content")} for w in sorted_wfs],
    }
    return hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True).encode()).hexdigest()


def _compute_current_project_preflight_hash(project: Project, db: Session) -> Optional[str]:
    """Compute the preflight fingerprint for the project's current workflow state.

    Returns ``None`` when no validation repository is configured or no regular
    workflows exist.
    """
    validation_repo = _get_validation_repo_name(project, db)
    if not validation_repo:
        return None

    all_regular_workflows = (
        db.query(Workflow)
        .join(ProjectWorkflow)
        .filter(
            ProjectWorkflow.project_id == project.project_id,
            Workflow.reusable_workflow.isnot(True),
        )
        .all()
    )
    workflow_dicts = [
        {"name": w.workflow_name, "content": w.workflow_yaml}
        for w in all_regular_workflows
        if w.workflow_name and w.workflow_yaml
    ]
    if not workflow_dicts:
        return None

    return _compute_preflight_content_hash(workflow_dicts, validation_repo)


def _mark_stale_if_content_changed(project: Project, db: Session) -> bool:
    """If the project's workflow content has changed since the last preflight run,
    downgrade status to ``stale`` and return ``True``.

    Only acts when the stored status is in ``_PREFLIGHT_PASSING_STATUSES`` and a
    stored content hash is present; safe to call on every status read.
    """
    if project.last_preflight_status not in _PREFLIGHT_PASSING_STATUSES:
        return False
    stored_hash = getattr(project, "last_preflight_content_hash", None)
    if not stored_hash:
        return False
    current_hash = _compute_current_project_preflight_hash(project, db)
    if current_hash is None or current_hash == stored_hash:
        return False
    _update_preflight_cached_fields(
        project,
        db,
        status="stale",
        error="Workflow changes were made after the last preflight approval. Re-run preflight to validate the current changes.",
    )
    return True


_PREFLIGHT_PASSING_STATUSES = {"passed"}

_VALIDATION_REPO_PERMISSION_ERROR = (
    "GitHub returned 403 Forbidden when accessing the validation repository. "
    "Ensure the authenticated token has read access to pull requests in the validation repository. "
    "For GitHub Apps, verify the installation grants 'Pull requests: read' permission on the validation repository. "
    "For personal access tokens, the 'repo' scope (classic) or 'Pull requests: read' permission (fine-grained) is required."
)


def _ensure_preflight_allows_campaign(project: Project, *, github_user: str, token: str, db: Session) -> None:
    if not project.validation_repo_id:
        return
    if not bool(getattr(project, "preflight_required", False)):
        return
    # Invalidate a stale approval before refreshing from GitHub.
    _mark_stale_if_content_changed(project, db)
    if project.last_preflight_status not in _PREFLIGHT_PASSING_STATUSES:
        # Avoid requiring a manual Refresh Status click: if a validation PR URL
        # exists, refresh once so merges/closes done in GitHub are recognized.
        _refresh_preflight_status_from_github(project=project, github_user=github_user, token=token, db=db)
    if project.last_preflight_status not in _PREFLIGHT_PASSING_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Preflight validation must pass before creating this PR Campaign.",
        )


def _custom_file_has_local_change():
    """A custom file with a change no campaign has delivered yet: an edit, or its removal."""
    return CustomFile.pending_delete.is_(True) | (CustomFile.file_status != "synced_with_github")


def _build_custom_files_for_delivery(project: Project, payload: "CreatePullRequestsRequest", db: Session) -> List[dict]:
    """Return custom files to include in this PR Campaign.

    ``payload.selected_custom_file_ids = None``  → all changed/pending files.
    ``payload.selected_custom_file_ids = []``     → none (user deselected all).
    ``payload.selected_custom_file_ids = [1,2]``  → only those specific files,
    whatever their status, as for CODEOWNERS: so a campaign can also put a
    synced file back where it drifted, or deliver it to a repository it never
    reached. ``synced`` marks such a file, for _upsert_custom_file_to_am_branch.
    """
    if payload.selected_custom_file_ids == []:
        return []
    query = db.query(CustomFile).filter(CustomFile.project_id == project.project_id)
    if payload.selected_custom_file_ids:
        query = query.filter(CustomFile.id.in_(payload.selected_custom_file_ids))
    else:
        query = query.filter(_custom_file_has_local_change())
    files = query.all()
    return [
        {
            "id": f.id,
            "file_path": f.file_path,
            "file_content": f.file_content,
            "pending_delete": f.pending_delete,
            "synced": f.file_status == "synced_with_github" and not f.pending_delete,
        }
        for f in files
    ]


def _build_codeowners_for_delivery(project: Project, payload: "CreatePullRequestsRequest", db: Session) -> Optional[dict]:
    """The project's CODEOWNERS file for this PR Campaign, or None to leave it out.

    Whatever its status when included, so a campaign can also put it back where
    it drifted, or deliver it to a repository added since.
    """
    if not payload.include_codeowners:
        return None
    record = _project_codeowners(db, project.project_id)
    if record is None or not record.content:
        return None
    return {"id": record.id, "file_path": record.file_path or ".github/CODEOWNERS", "content": record.content}


def _owned_reusable_selected(project: Project, payload, db: Session) -> list:
    """Reusable workflows this project owns and this run selected.

    What decides where a workflow is delivered is whether the project *owns* it,
    not whether it carries the reusable label. A workflow a standard project
    owns lives in that project's own repositories, so it is delivered with the
    rest of the files that project changed. Only a workflow LINKED from a
    Reusable Workflow Project belongs in that project's repository instead.

    RWX projects are left alone: their own repo is already what
    _get_reusable_workflow_repo resolves to, and their prefix handling differs.

    Matched by the stored name, which is what every caller sends for a workflow
    the project owns. Normalising here also matched a linked workflow's display
    name, so selecting a linked ``AM_RWW1_deploy.yml`` dragged in an owned
    ``deploy`` (#2309).
    """
    if payload.selected_reusable_workflows is None:
        return []
    selected = _lowered_names(payload.selected_reusable_workflows)
    return [
        w for w in _owned_reusable_rows(project, db)
        if not _delivers_to_reusable_repo(project, w) and (w.workflow_name or "").lower() in selected
    ]


def _delivers_to_reusable_repo(project: Project, workflow) -> bool:
    """Whether a campaign delivers *workflow*, one *project* owns, to the reusable-workflow repository.

    Only a Reusable Workflow Project's reusable workflows, which go to its
    repository's default branch (_reusable_delivery_target). Everything else a
    project owns, a caller project's own reusable workflows included (#2084),
    goes to the branches it delivers its own repositories.
    """
    return project.project_type == "rwx" and bool(workflow.reusable_workflow)


def _lowered_names(names) -> set:
    return {(n or "").strip().lower() for n in names or [] if n}


def _owned_reusable_rows(project: Project, db: Session) -> list:
    return db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project.project_id,
        Workflow.reusable_workflow == True
    ).all()


def _includes_regular_workflows(payload, project: Optional[Project] = None,
                                db: Optional[Session] = None) -> bool:
    """Whether this run delivers to the project's caller repos at all.

    Regular workflows are included when the caller explicitly listed
    selected_workflows, or when neither workflow type was requested (the
    backward-compatible default).

    A run carrying reusable workflows the project *owns* also targets those
    repos, because that is where they live — pass project and db to have that
    counted. Only a linked-reusable-only run leaves the caller repos untouched.
    """
    if (
        payload.selected_workflows is not None
        or payload.selected_reusable_workflows is None
    ):
        return True
    if project is None or db is None:
        return False
    return bool(_owned_reusable_selected(project, payload, db))


def _campaign_meta(payload, project: Project, db: Session,
                   workflow_names: Optional[List[str]] = None) -> dict:
    """Everything the PR body needs that is the same for every target.

    The policy table and the project lookup are identical on every repo/branch
    in a campaign, so they are resolved once here rather than per PR — a
    50-repo campaign would otherwise re-run them a hundred times inside the
    GitHub PR-creation loop.
    """
    return {
        "name": getattr(payload, "campaign_name", None),
        "description": getattr(payload, "campaign_description", None),
        "project_name": project.project_name,
        "policy": _build_policy_version(db, project.project_id, workflow_names or []),
    }


def _build_regular_workflow_results(project: Project, payload: "CreatePullRequestsRequest", repo_names: List[str], headers: dict, db: Session, github_user: str = None, progress_callback=None, custom_files: Optional[List[dict]] = None, codeowners: Optional[dict] = None):
    """Process regular (non-reusable) workflows and return (results, selected_names).

    Regular workflows are included when the caller explicitly listed
    selected_workflows, or when neither workflow type was requested (backward-
    compatible default).  They are skipped when only reusable workflows were
    requested (selected_reusable_workflows is set but selected_workflows is None).

    ``custom_files`` are committed to the same AM branch as workflows so they
    land in the same PR Campaign PR.
    """
    include_regular = _includes_regular_workflows(payload)
    custom_files = custom_files or []
    workflow_dicts = []
    selected_names = []
    delivered: list = []
    if include_regular:
        all_regular_workflows = db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id,
            Workflow.reusable_workflow.isnot(True)
        ).all()
        regular_workflows = all_regular_workflows
        if payload.selected_workflows is not None:
            regular_workflows = [w for w in regular_workflows if w.workflow_name in payload.selected_workflows]
        delivered = list(regular_workflows)

    # A reusable workflow this project owns lives in these same repositories, so
    # it travels with them. Disjoint from the query above, which excludes
    # reusable rows, so no deduplication is needed.
    delivered.extend(_owned_reusable_selected(project, payload, db))

    if delivered:
        workflow_dicts = [
            {"id": w.workflow_id, "name": w.workflow_name, "content": w.workflow_yaml,
             "pending_delete": bool(w.pending_delete),
             "is_reusable": bool(w.reusable_workflow)}
            for w in delivered if w.workflow_name and w.workflow_yaml
        ]
        selected_names = [w.workflow_name for w in delivered if w.workflow_name]

    if not workflow_dicts and not custom_files and not codeowners:
        return {}, []

    user = github_user or ""
    results = _process_regular_workflows_update(
        repo_names=repo_names,
        workflows=workflow_dicts,
        project_code=project.project_code,
        branch_option=project.branch_option or "default",
        regex_pattern=project.branch_regex or "",
        branch_max_age_days=project.branch_max_age_days or 30,
        headers=headers,
        db=db,
        user=user,
        use_prefix=project.use_prefix,
        project=project,
        codeowners=codeowners,
        progress_callback=progress_callback,
        campaign_meta=_campaign_meta(payload, project, db, selected_names),
        custom_files=custom_files,
    )
    return results, selected_names


def _validate_linked_workflow_selections(project: Project, selected_linked_rows: list, db: Session):
    """Validate that all selected linked reusable workflows are compatible."""
    for _workflow, rwx_project in selected_linked_rows:
        validation = validate_reusable_workflow_link(project, rwx_project, db)
        if not validation.allowed:
            raise HTTPException(
                status_code=400,
                detail=validation.reason or "Unable to validate reusable workflow compatibility. Please contact support.",
            )


def _check_reusable_workflow_locks(db: Session, reusable_workflows: list, project_id):
    """Block PR creation if any reusable workflow is already under review elsewhere."""
    for workflow in reusable_workflows:
        blocking = _find_blocking_reusable_workflow_pr(db, workflow, project_id)
        if blocking is not None:
            blocking_pr, owning_project = blocking
            owner_label = owning_project.project_name if owning_project else "another project"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Reusable workflow '{workflow.workflow_name}' is already under review "
                    f"by an open PR campaign in project '{owner_label}' (PR #{blocking_pr.pr_number}). "
                    "It is locked until that PR is merged or closed."
                ),
            )


def _log_reusable_workflow_selection(project_name: str, reusable_workflows: list, linked_only: list):
    """Log which linked reusable workflows are included vs skipped."""
    linked_ids_set = {w.workflow_id for w in linked_only}
    included_linked = [
        w.workflow_name for w in reusable_workflows if w.workflow_id in linked_ids_set
    ]
    if included_linked:
        print(
            f"📌 Including {len(included_linked)} linked reusable workflow(s) in PR "
            f"creation for project '{project_name}': {included_linked}"
        )
    skipped_linked = [
        w.workflow_name for w in linked_only if w not in reusable_workflows
    ]
    if skipped_linked:
        print(
            f"ℹ️ Skipping {len(skipped_linked)} linked reusable workflow(s) not "
            f"selected for project '{project_name}': {skipped_linked}"
        )


def _build_reusable_workflow_results(project: Project, payload: "CreatePullRequestsRequest", headers: dict, db: Session, github_user: str = None, progress_callback=None):
    """Process reusable workflows and return (results, selected_names).

    Only runs when the caller explicitly provided selected_reusable_workflows.
    Merges workflows owned directly by the project (RWX) with those linked from
    RWX projects (standard projects), deduplicating by workflow_id.

    Selection matching uses ``_normalize_reusable_workflow_name`` so that
    display-formatted names sent by the frontend for *linked* reusable
    workflows (e.g. ``AM_RWW1_deploy.yml`` produced by
    ``_load_linked_reusable_workflows``) correctly resolve to the canonical
    raw stem stored in ``Workflow.workflow_name`` (e.g. ``deploy``).
    Without this normalisation, linked reusable workflows are silently
    dropped from PR creation and no PR is opened against the producer repo.
    """
    if payload.selected_reusable_workflows is None:
        return {}, []
    # Workflows owned directly by this project (RWX projects).
    #
    # A standard project's own reusable workflows are deliberately NOT here:
    # they live in that project's own repositories and are delivered with the
    # rest of its files by _build_regular_workflow_results. Sending them here
    # too would open a second pull request against the reusable-workflow repo
    # for a file that is not in it.
    own_rows = _owned_reusable_rows(project, db)
    owned = [w for w in own_rows if _delivers_to_reusable_repo(project, w)]
    # Workflows linked from RWX projects (standard projects)
    linked_rows = db.query(Workflow, LinkedReusableWorkflow, Project).join(
        LinkedReusableWorkflow, LinkedReusableWorkflow.workflow_id == Workflow.workflow_id
    ).join(
        Project, Project.project_id == LinkedReusableWorkflow.rwx_project_id
    ).filter(
        LinkedReusableWorkflow.standard_project_id == project.project_id
    ).all()
    linked_rx = [workflow for workflow, _link, _rwx_project in linked_rows]
    seen_ids = {w.workflow_id for w in owned}
    linked_only = [w for w in linked_rx if w.workflow_id not in seen_ids]
    all_reusable = list(owned) + linked_only
    # Normalise both sides so display names (with prefix + .yml extension) match
    # the canonical raw stem stored in ``Workflow.workflow_name``. A name one of
    # this project's own reusable workflows answers to selects that workflow,
    # not a linked one that normalises the same (#2309).
    own_names = _lowered_names(w.workflow_name for w in own_rows if not _delivers_to_reusable_repo(project, w))
    selected_norm = {
        _normalize_reusable_workflow_name(n) for n in payload.selected_reusable_workflows
        if (n or "").strip().lower() not in own_names
    }
    reusable_workflows = [
        w for w in all_reusable
        if _normalize_reusable_workflow_name(w.workflow_name) in selected_norm
    ]
    reusable_workflow_ids = {workflow.workflow_id for workflow in reusable_workflows}
    selected_linked_rows = [
        (workflow, rwx_project)
        for workflow, _link, rwx_project in linked_rows
        if workflow.workflow_id in reusable_workflow_ids
    ]
    _validate_linked_workflow_selections(project, selected_linked_rows, db)
    _check_reusable_workflow_locks(db, reusable_workflows, project.project_id)
    _log_reusable_workflow_selection(project.project_name, reusable_workflows, linked_only)
    if not reusable_workflows:
        return {}, []
    rxworkflow_dicts = [
        {"id": w.workflow_id, "name": w.workflow_name, "content": w.workflow_yaml}
        for w in reusable_workflows if w.workflow_name and w.workflow_yaml
    ]
    selected_names = [w.workflow_name for w in reusable_workflows if w.workflow_name]
    user = github_user or ""

    # For linked reusable workflows in a standard (caller) project, prefix
    # behaviour must come from the owning RWX project, not the caller project.
    effective_code = project.project_code
    effective_prefix = project.use_prefix
    if project.project_type == "standard" and selected_linked_rows:
        # All linked workflows in this batch go to the same RWX project's repo
        # (see _get_reusable_workflow_repo logic).  Use that RWX project's settings.
        _first_linked_rwx = selected_linked_rows[0][1]
        effective_code = _first_linked_rwx.project_code
        effective_prefix = _first_linked_rwx.use_prefix

    results = _process_reusable_workflows_update(
        rxworkflows=rxworkflow_dicts,
        user=user,
        project_code=effective_code,
        regex_pattern=project.branch_regex or "",
        branch_max_age_days=project.branch_max_age_days or 30,
        headers=headers,
        db=db,
        reusable_repo=_get_reusable_workflow_repo(project, user, db),
        use_prefix=effective_prefix,
        progress_callback=progress_callback,
        campaign_meta=_campaign_meta(payload, project, db, selected_names),
    )
    return results, selected_names


def _parse_repo_branch(repo_branch: str):
    """Extract repo_name and target_branch from a 'repo on branch' key."""
    if " on " in repo_branch:
        parts = repo_branch.split(" on ")
        return parts[0], parts[1]
    return repo_branch, "main"


def _persist_single_pr(result: dict, repo_branch: str, project: Project, campaign, db: Session):
    """Save a single PR record, and what it carries, to the database.

    Returns whether the row is attached to *campaign*.
    """
    repo_name, target_branch = _parse_repo_branch(repo_branch)
    am_branch = result.get("branch_name") or (
        f"actions-manager/{project.project_code.lower()}"
        f"-{target_branch.replace('/', '-').replace('\\', '-')}"
    )
    per_pr_workflows = result.get("workflows_committed") or []
    workflow_names_str = ", ".join(per_pr_workflows) if per_pr_workflows else None
    per_pr_files: List[str] = list(result.get("custom_files_committed") or [])
    codeowners_path = result.get("codeowners_committed") or ""
    if codeowners_path:
        per_pr_files.append(codeowners_path)
    file_names_str = ", ".join(per_pr_files) if per_pr_files else None
    is_new_pr = result.get("status") == "pr_created"
    campaign_id = getattr(campaign, "campaign_id", None)
    try:
        pr_row = _save_pr_to_database(
            db=db,
            project_id=project.project_id,
            repo_name=repo_name,
            pr_number=result.get("pr_number"),
            pr_url=result.get("pr_url"),
            branch_name=am_branch,
            target_branch=target_branch,
            title=result.get("pr_title"),
            author=result.get("pr_author"),
            body=result.get("pr_body"),
            workflow_names=workflow_names_str,
            file_names=file_names_str,
            campaign_id=campaign_id,
            is_new_pr=is_new_pr,
        )
        deliveries.record_pending(db, pr_row, result.get("deliveries"))
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"❌ Could not record PR #{result.get('pr_number')} for {repo_name}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=(
                f"Pull request #{result.get('pr_number')} for {repo_name} was created on GitHub "
                "but could not be recorded in the ActionsManager database. "
                "The database schema may be out of date — restart the container or run "
                "'python run_migrations.py' from the backend directory, then refresh PR status."
            ),
        ) from exc
    # The campaign was created by this run, so a row carries its id only if it
    # was attached to it.
    attached = campaign_id is not None and pr_row.campaign_id == campaign_id
    if is_new_pr and not attached:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Pull request #{result.get('pr_number')} for {repo_name} was saved without a "
                "campaign id. The database schema may be missing the PR campaigns migration — "
                "restart the container or run 'python run_migrations.py' from the backend directory."
            ),
        )
    return attached


def _update_linked_workflow_statuses(db: Session, project: Project, project_names: List[str],
                                    reusable_names: List[str]):
    """Mark the workflows a campaign delivers under_review after PR creation.

    A standard project's own workflows, its reusable ones included (#2084),
    arrive in ``project_names``, so ``reusable_names`` are only the ones it
    links. Matching one combined list against both kinds also marked a
    same-named owned or linked workflow the campaign did not deliver, and an
    RWX workflow under_review is locked for every project linking it (#2309).
    An RWX project owns everything it delivers.
    """
    owned, linked = (
        (project_names + reusable_names, []) if project.project_type == "rwx"
        else (project_names, reusable_names)
    )
    if owned:
        _update_project_workflows_status(db, project.project_id, "under_review", workflow_names=owned)
    linked_wf_ids = (
        _get_linked_workflow_ids_for_project(db, project.project_id, workflow_names=linked)
        if linked else []
    )
    if linked_wf_ids:
        db.query(Workflow).filter(
            Workflow.workflow_id.in_(linked_wf_ids)
        ).update({"workflow_status": "under_review"}, synchronize_session=False)
        db.commit()
        print(f"✅ Set {len(linked_wf_ids)} linked reusable workflow(s) to under_review")


def _build_policy_version(db: Session, project_id: int, all_selected: List[str]) -> dict:
    """Pin the exact workflow content this campaign is delivering.

    ``version`` is what a human reads back ("which version went out"), but it is
    None for a workflow that was never saved through the versioning path, so the
    content hash is recorded alongside it — that one is always available and
    identifies the YAML exactly.
    """
    if not all_selected:
        return {}

    workflows = db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project_id,
        Workflow.workflow_name.in_(all_selected),
    ).all()
    linked_ids = _get_linked_workflow_ids_for_project(db, project_id, workflow_names=all_selected)
    if linked_ids:
        already = {w.workflow_id for w in workflows}
        workflows += db.query(Workflow).filter(
            Workflow.workflow_id.in_([i for i in linked_ids if i not in already])
        ).all()

    if not workflows:
        return {}

    latest = {}
    for workflow_id, number, content in (
        db.query(WorkflowVersion.workflow_id, WorkflowVersion.version_number, WorkflowVersion.content)
        .filter(WorkflowVersion.workflow_id.in_([w.workflow_id for w in workflows]))
        .order_by(WorkflowVersion.workflow_id, WorkflowVersion.version_number.desc())
        .all()
    ):
        latest.setdefault(workflow_id, (number, content))

    applied = {}
    for workflow in workflows:
        yaml_text = workflow.workflow_yaml or ""
        number, content = latest.get(workflow.workflow_id, (None, None))
        # create_workflow_version swallows its own failures and several paths
        # assign workflow_yaml directly, so the newest version row is not
        # necessarily what is about to ship. Reporting "v7" for content that is
        # not v7 is the exact misreading this snapshot exists to prevent — when
        # they disagree, only the hash is claimed.
        applied[workflow.workflow_name] = {
            "version": number if content == yaml_text else None,
            "sha256": hashlib.sha256(yaml_text.encode()).hexdigest(),
        }
    return applied


def _build_campaign_snapshot(db: Session, project: Project, repo_names: Optional[List[str]],
                             results: dict, all_selected: List[str]) -> dict:
    """Freeze what this campaign was aimed at, as of right now.

    Everything else a campaign shows is derived live from its surviving PR rows,
    so without this a campaign re-reads against today's repo list, branch heads
    and workflow content, and a target that produced no PR leaves no trace.
    Returned as ``ProjectPRCampaign`` kwargs — JSON strings, written once at
    construction and never updated.
    """
    targets = {
        result_key: result
        for result_key, result in (results or {}).items()
        if isinstance(result, dict)
    }
    return {
        "target_repos": json.dumps(repo_names or []),
        "base_commits": json.dumps({k: r.get("base_sha") for k, r in targets.items()}),
        "branch_protection": json.dumps(
            {k: r["branch_protection"] for k, r in targets.items() if r.get("branch_protection")}
        ),
        # Straight off the create-PR response from this same run, so the URL is
        # recorded at the point the PR was opened rather than looked up later.
        "target_pr_urls": json.dumps(
            {k: r["pr_url"] for k, r in targets.items() if r.get("pr_url")}
        ),
        "policy_version": json.dumps(_build_policy_version(db, project.project_id, all_selected)),
    }


def _save_prs_and_update_status(results: dict, project: Project, selected_workflow_names: List[str], selected_reusable_workflow_names: List[str], db: Session, github_user: Optional[str] = None, custom_file_ids: Optional[List[int]] = None, repo_names: Optional[List[str]] = None, campaign_name: Optional[str] = None, campaign_description: Optional[str] = None, project_state: str = "open"):
    """Persist PR records to the database and update project/workflow statuses.

    Every run that produces PR records creates a NEW unique campaign record so
    newly-created PRs are never appended to a previous campaign. PR rows whose
    existing open PR merely received new commits stay in their original campaign.
    The project's pr_state becomes *project_state* once any PR is recorded.
    A deletion's (PROJECT_DELETING) files are not put under review: the
    project's state says what is happening, and a deletion called off leaves
    each file synced where its removal did not merge (_reopen_removed_files).

    Returns (pr_count, campaign_id) — campaign_id is None when no campaign was created.
    """
    all_selected = selected_workflow_names + selected_reusable_workflow_names

    actionable_results = {
        repo_branch: result
        for repo_branch, result in results.items()
        if isinstance(result, dict) and result.get("status") in ["pr_created", "pr_updated"]
    }

    campaign = None
    campaign_id = None
    campaign_pr_count = 0
    if actionable_results:
        campaign = ProjectPRCampaign(
            project_id=project.project_id,
            created_by=github_user,
            campaign_name=campaign_name,
            campaign_description=campaign_description,
            **_build_campaign_snapshot(db, project, repo_names, results, all_selected),
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        campaign_id = campaign.campaign_id

    pr_count = 0
    for repo_branch, result in actionable_results.items():
        attached = _persist_single_pr(result, repo_branch, project, campaign, db)
        if attached:
            campaign_pr_count += 1
        pr_count += 1

    if campaign and campaign_pr_count == 0:
        db.delete(campaign)
        db.commit()
        campaign_id = None

    if pr_count == 0:
        return 0, None
    if campaign_id:
        try:
            record_campaign_opened(db, project, campaign, results)
        except Exception as exc:
            print(f"⚠️ Error recording campaign.opened notification: {exc}")
            db.rollback()
    _update_project_pr_state(db, project.project_id, project_state)
    if all_selected:
        _update_linked_workflow_statuses(
            db, project, selected_workflow_names, selected_reusable_workflow_names
        )
    _lock_custom_files_for_review(db, custom_file_ids)
    if project_state != PROJECT_DELETING:
        _lock_codeowners_for_review(db, project.project_id, actionable_results)
    return pr_count, campaign_id


def _lock_custom_files_for_review(db: Session, custom_file_ids: Optional[List[int]]) -> None:
    if not custom_file_ids:
        return
    try:
        db.query(CustomFile).filter(
            CustomFile.id.in_(custom_file_ids),
            CustomFile.file_status != "synced_with_github",
        ).update({"file_status": "under_review"}, synchronize_session=False)
        db.commit()
        print(f"✅ Set {len(custom_file_ids)} custom file(s) to under_review")
    except Exception as e:
        print(f"❌ Error setting custom files to under_review: {str(e)}")
        db.rollback()


def _lock_codeowners_for_review(db: Session, project_id: int, actionable_results: dict) -> None:
    """Mark the project's CODEOWNERS under review once a PR carries it, as custom files are.

    Not a copy saved again since it was committed: its hash, cleared by the
    save, marks a change no PR carries yet, so it stays a local change to deliver.
    """
    if not any(result.get("codeowners_committed") for result in actionable_results.values()):
        return
    try:
        record = _project_codeowners(db, project_id)
        if record is not None and record.git_hash == deliveries.blob_sha(record.content):
            record.status = "under_review"
            db.commit()
    except Exception as e:
        print(f"❌ Error setting CODEOWNERS to under_review: {str(e)}")
        db.rollback()


pr_campaign_tasks = {}

def _run_create_pull_requests_async(task_id: str, payload: CreatePullRequestsRequest, github_user: str):
    pr_campaign_tasks[task_id]["status"] = "running"
    
    def progress_callback(repo_name: str, step: str, status: str, error: Optional[str] = None):
        if repo_name not in pr_campaign_tasks[task_id]["repos"]:
            pr_campaign_tasks[task_id]["repos"][repo_name] = {"step": step, "status": status, "error": error}
        else:
            pr_campaign_tasks[task_id]["repos"][repo_name].update({"step": step, "status": status, "error": error})

    try:
        with SessionLocal() as db:
            token, project = _get_project_and_token(payload, db, github_user=github_user)
            _ensure_preflight_allows_campaign(project, github_user=github_user, token=token, db=db)
            repo_names = _get_filtered_repo_names(project, payload.selected_repos, db)
            headers = {
                "Accept": ACCEPT_HEADER,
                "X-GitHub-Api-Version": X_API_VERSION,
                "Authorization": f"token {token}"
            }
            custom_files_for_delivery = _build_custom_files_for_delivery(project, payload, db)
            codeowners_for_delivery = _build_codeowners_for_delivery(project, payload, db)
            regular_results, selected_workflow_names = _build_regular_workflow_results(
                project, payload, repo_names, headers, db, github_user=github_user,
                progress_callback=progress_callback, custom_files=custom_files_for_delivery,
                codeowners=codeowners_for_delivery,
            )
            reusable_results, selected_reusable_workflow_names = _build_reusable_workflow_results(
                project, payload, headers, db, github_user=github_user, progress_callback=progress_callback
            )
            results = {**regular_results, **reusable_results}
            if not results:
                pr_campaign_tasks[task_id]["status"] = "error"
                pr_campaign_tasks[task_id]["error"] = NOTHING_SELECTED_FOR_CAMPAIGN
                return

            custom_file_ids = [cf["id"] for cf in custom_files_for_delivery]
            pr_count, campaign_id = _save_prs_and_update_status(
                results, project, selected_workflow_names, selected_reusable_workflow_names, db,
                github_user=github_user, custom_file_ids=custom_file_ids,
                repo_names=repo_names if _includes_regular_workflows(payload, project, db) else [],
                campaign_name=payload.campaign_name, campaign_description=payload.campaign_description,
            )

            pr_campaign_tasks[task_id]["status"] = "completed"
            pr_campaign_tasks[task_id]["results"] = results
            pr_campaign_tasks[task_id]["prs_created"] = pr_count
            pr_campaign_tasks[task_id]["campaign_id"] = campaign_id
    except HTTPException as e:
        # Written for the user, as the synchronous path returns it.
        pr_campaign_tasks[task_id]["status"] = "error"
        pr_campaign_tasks[task_id]["error"] = str(e.detail)
    except Exception as e:
        print(f"❌ Error in async PR creation: {str(e)}")
        import traceback
        traceback.print_exc()
        pr_campaign_tasks[task_id]["status"] = "error"
        pr_campaign_tasks[task_id]["error"] = "Error creating pull requests"


@router.post("/api/create-pull-requests", responses=_responses(400, 401, 403, 404, 409, 500))
def create_pull_requests_route(
    payload: CreatePullRequestsRequest,
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """
    Create pull requests for workflows in specified repositories.
    This endpoint transitions the project from 'draft' to 'open' state.
    Supports both regular workflows (per-repo PRs) and reusable workflows
    (PR against the dedicated am-reuseable-workflow repository).
    """
    return create_pull_requests(payload, background_tasks, db, github_user=account.github_user)


def create_pull_requests(
    payload: CreatePullRequestsRequest,
    background_tasks: BackgroundTasks,
    db: Session,
    *,
    github_user: str,
):
    """Open a PR campaign as *github_user*, who the caller has already authenticated.

    The route passes the session's caller (#2241); internal callers (custom files,
    workflow import, drift restore) pass the user they act for. It used to read
    a ``github_user`` query parameter for them, which any HTTP client could set.
    """
    token, project = _get_project_and_token(payload, db, github_user=github_user)
    # A campaign commits workflow files and opens pull requests across every
    # repository in the project, so proving the caller can *see* it is not
    # enough - _get_project_and_token never reads ProjectMembership.project_role.
    # Same reasoning as drift resolution and campaign rollback, which write to
    # GitHub for the same reason. Resolved before the async branch so a
    # read-only member gets a 403 rather than a task id that fails out of sight.
    _require_project_editor(db, github_user, project)

    if payload.async_mode:
        task_id = str(uuid.uuid4())
        pr_campaign_tasks[task_id] = {
            "status": "queued",
            "repos": {},
            "results": {},
            "prs_created": 0
        }
        background_tasks.add_task(_run_create_pull_requests_async, task_id, payload, github_user)
        return {"task_id": task_id, "status": "running"}

    try:
        _ensure_preflight_allows_campaign(project, github_user=github_user, token=token, db=db)
        repo_names = _get_filtered_repo_names(project, payload.selected_repos, db)
        headers = {
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION,
            "Authorization": f"token {token}"
        }
        custom_files_for_delivery = _build_custom_files_for_delivery(project, payload, db)
        codeowners_for_delivery = _build_codeowners_for_delivery(project, payload, db)
        regular_results, selected_workflow_names = _build_regular_workflow_results(
            project, payload, repo_names, headers, db, github_user=github_user,
            custom_files=custom_files_for_delivery, codeowners=codeowners_for_delivery,
        )
        reusable_results, selected_reusable_workflow_names = _build_reusable_workflow_results(
            project, payload, headers, db, github_user=github_user
        )
        results = {**regular_results, **reusable_results}
        if not results:
            raise HTTPException(status_code=400, detail=NOTHING_SELECTED_FOR_CAMPAIGN)
        custom_file_ids = [cf["id"] for cf in custom_files_for_delivery]
        pr_count, campaign_id = _save_prs_and_update_status(
            results, project, selected_workflow_names, selected_reusable_workflow_names, db,
            github_user=github_user, custom_file_ids=custom_file_ids,
            repo_names=repo_names if _includes_regular_workflows(payload, project, db) else [],
            campaign_name=payload.campaign_name, campaign_description=payload.campaign_description,
        )
        return {
            "message": "Pull requests created successfully",
            "results": results,
            "prs_created": pr_count,
            "campaign_id": campaign_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error creating pull requests: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error creating pull requests")


@router.get("/api/create-pull-requests/{task_id}", responses=_responses(404))
def get_create_pull_requests_status(task_id: str):
    if task_id not in pr_campaign_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return pr_campaign_tasks[task_id]

@router.post("/api/run-preflight-validation", responses=_responses(400, 401, 403, 404, 409, 500))
def run_preflight_validation(
    payload: RunPreflightRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Run project preflight in the configured validation repository."""
    github_user = account.github_user
    try:
        token, project = _get_project_and_token(payload, db, github_user=github_user)
        # Preflight opens a pull request in the validation repository, so it is a
        # GitHub write and needs the same gate as the campaign it validates.
        _require_project_editor(db, github_user, project)
        refuse_unless_accepted(project, "validation_repo")
        validation_repo = _get_validation_repo_name(project, db)
        if not validation_repo:
            raise HTTPException(status_code=400, detail="Preflight validation is not configured.")

        headers = {
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION,
            "Authorization": f"token {token}"
        }
        _check_validation_repo_access(project, validation_repo, github_user, db, headers)

        project.last_preflight_status = "running"
        project.last_preflight_error = None
        project.last_preflight_pr_url = None
        db.commit()

        workflow_dicts, all_workflow_dicts = _collect_preflight_workflows(project, payload, db)

        content_hash = _compute_preflight_content_hash(all_workflow_dicts, validation_repo)

        results = _process_regular_workflows_update(
            repo_names=[validation_repo],
            workflows=workflow_dicts,
            project_code=project.project_code,
            branch_option=project.branch_option or "default",
            regex_pattern=project.branch_regex or "",
            branch_max_age_days=project.branch_max_age_days or 30,
            headers=headers,
            db=db,
            user=github_user,
            use_prefix=project.use_prefix,
            project=project,
        )
        failed, pr_url = _extract_preflight_results(results, validation_repo)
        if failed:
            message = _sanitize_preflight_error("; ".join(str(item) for item in failed if item))
            _set_preflight_result(project, "failed", db, message, pr_url)
            raise HTTPException(status_code=400, detail=message)

        # Preflight approval is based on the validation PR lifecycle:
        # - Open PR => waiting for user review
        # - Merged PR => approved/passed
        # - Closed PR (not merged) => rejected/not approved
        _set_preflight_result(project, "validation_pr_open", db, None, pr_url)
        # Record the content fingerprint so future status checks can detect staleness.
        project.last_preflight_content_hash = content_hash
        db.commit()
        return {
            "status": "validation_pr_open",
            "validation_repo": validation_repo,
            "last_preflight_run_at": project.last_preflight_run_at.isoformat() if project.last_preflight_run_at else None,
            "last_preflight_pr_url": project.last_preflight_pr_url,
        }
    except HTTPException:
        raise
    except Exception as e:
        try:
            project = _find_project_by_name(db, github_user, payload.project_name)
            if project:
                kind = failure_kind(e, _SHOWN_GITHUB_ERRORS)
                _set_preflight_result(project, "failed", db, f"Preflight validation failed: {kind}")
        except Exception:
            db.rollback()
        print(f"❌ Error running preflight validation: {str(e)}")
        raise HTTPException(status_code=500, detail="Preflight validation failed.")


def _check_validation_repo_access(project, validation_repo: str, github_user: str, db, headers: dict):
    """Check that the validation repository is accessible, raising HTTPException on failure."""
    owner, repo = validation_repo.split("/", 1)
    repo_response = github_get(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}",
        github_user,
        db,
        headers=headers,
    )
    if repo_response.status_code == 404:
        _set_preflight_result(
            project,
            "validation_repo_inaccessible",
            db,
            "Validation repository is inaccessible, deleted, or unauthorized.",
        )
        raise HTTPException(status_code=400, detail="Validation repository is inaccessible, deleted, or unauthorized.")
    if repo_response.status_code == 403:
        _set_preflight_result(
            project,
            "failed",
            db,
            _VALIDATION_REPO_PERMISSION_ERROR,
        )
        raise HTTPException(status_code=403, detail=_VALIDATION_REPO_PERMISSION_ERROR)
    if repo_response.status_code >= 400:
        _set_preflight_result(project, "failed", db, "Unable to access validation repository.")
        raise HTTPException(status_code=400, detail="Unable to access validation repository.")


def _collect_preflight_workflows(project, payload, db):
    """Collect and filter workflows for preflight validation. Returns (selected_dicts, all_dicts)."""
    all_regular_workflows = db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project.project_id,
        Workflow.reusable_workflow.isnot(True)
    ).all()
    selected_names = set(payload.selected_workflows or [])
    regular_workflows = [
        workflow for workflow in all_regular_workflows
        if not selected_names or workflow.workflow_name in selected_names
    ]
    workflow_dicts = [
        {"name": w.workflow_name, "content": w.workflow_yaml}
        for w in regular_workflows if w.workflow_name and w.workflow_yaml
    ]
    if not workflow_dicts:
        _set_preflight_result(project, "failed", db, "No regular workflows selected for preflight validation.")
        raise HTTPException(status_code=400, detail="No regular workflows selected for preflight validation.")

    all_workflow_dicts = [
        {"name": w.workflow_name, "content": w.workflow_yaml}
        for w in all_regular_workflows if w.workflow_name and w.workflow_yaml
    ]
    return workflow_dicts, all_workflow_dicts


def _parse_github_pr_url(pr_url: str) -> Optional[tuple[str, str, int]]:
    """Parse a GitHub PR URL into (owner, repo, pr_number)."""
    value = (pr_url or "").strip()
    if not value:
        return None

    web_match = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", value)
    if web_match:
        owner, repo, pr_number = web_match.group(1), web_match.group(2), web_match.group(3)
        try:
            return owner, repo, int(pr_number)
        except ValueError:
            return None

    api_match = re.match(r"^https?://api\.github\.com/repos/([^/]+)/([^/]+)/pulls/(\d+)", value)
    if api_match:
        owner, repo, pr_number = api_match.group(1), api_match.group(2), api_match.group(3)
        try:
            return owner, repo, int(pr_number)
        except ValueError:
            return None

    return None


def _update_preflight_cached_fields(
    project: Project,
    db: Session,
    *,
    status: Optional[str] = None,
    error: Optional[str] = None,
    pr_url: Optional[str] = None,
    update_run_at: bool = False,
) -> None:
    """Update preflight fields, optionally refreshing last_preflight_run_at."""
    if status is not None:
        project.last_preflight_status = status
    if update_run_at:
        project.last_preflight_run_at = datetime.now(timezone.utc)
    if error is not None:
        project.last_preflight_error = (error[:500] if error else None)
    if pr_url is not None:
        project.last_preflight_pr_url = pr_url
    db.commit()


def _github_error_message(response, fallback: str) -> str:
    """Return a concise, actionable GitHub API error message."""
    error_data = _safe_json(response)
    if not isinstance(error_data, dict):
        return fallback

    message = error_data.get("message")
    details = _github_error_detail_strings(error_data.get("errors"))
    if details:
        return f"{message or fallback}: {'; '.join(details)}"
    return message or fallback


def _safe_json(response) -> object:
    """Decode a response body as JSON, returning ``{}`` on any failure."""
    try:
        return response.json() if getattr(response, "text", "") else {}
    except Exception:
        return {}


def _github_error_detail_strings(errors) -> list:
    """Extract up to three human-readable strings from a GitHub ``errors`` list."""
    if not isinstance(errors, list) or not errors:
        return []
    details = []
    for item in errors[:3]:
        if isinstance(item, dict):
            details.append(str(item.get("message") or item.get("code") or item))
        else:
            details.append(str(item))
    return details


def _is_pull_request_merged(pr_data: dict) -> bool:
    """Return True when PR appears merged based on GitHub REST payload fields."""
    if pr_data.get("merged") is True:
        return True
    merged_at = pr_data.get("merged_at")
    return bool(merged_at)


def _derive_pr_action_capabilities(pr_data: dict) -> dict:
    """Summarize whether ActionsManager can offer PR lifecycle actions."""
    pr_state = "merged" if _is_pull_request_merged(pr_data) else pr_data.get("state", "open")
    draft = bool(pr_data.get("draft", False))
    has_mergeability = "mergeable" in pr_data or "mergeable_state" in pr_data
    mergeable = pr_data.get("mergeable")
    mergeable_state = pr_data.get("mergeable_state")

    can_close = pr_state == "open"
    close_block_reason = None if can_close else "Pull request is not open."

    can_merge = False
    merge_block_reason = None
    if pr_state != "open":
        merge_block_reason = "Pull request is not open."
    elif draft:
        merge_block_reason = "Pull request is a draft."
    elif not has_mergeability:
        can_merge = None
    elif mergeable is None or mergeable_state in {None, "unknown"}:
        merge_block_reason = "GitHub is still computing mergeability; refresh status before merging."
    elif mergeable is False or mergeable_state == "dirty":
        merge_block_reason = "Pull request has merge conflicts."
    elif mergeable_state in {"blocked", "behind", "unstable"}:
        merge_block_reason = "Required checks, reviews, branch protection, or base branch updates are blocking merge."
    else:
        can_merge = True

    return {
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "draft": draft,
        "can_merge": can_merge,
        "merge_block_reason": merge_block_reason,
        "can_close": can_close,
        "close_block_reason": close_block_reason,
    }


def _derive_preflight_status_from_github(
    *,
    pr_data: dict,
) -> tuple[str, Optional[str], Optional[str]]:
    """Return (status, error_summary, pr_state) based on validation PR lifecycle state.

    Preflight pass/fail is determined by PR lifecycle state — not by GitHub
    Actions check or workflow run results on the PR head SHA:

    - Merged PR  => ``passed`` (approved)
    - Closed PR (without merge) => ``closed`` (rejected/not approved)
    - Open PR    => ``validation_pr_open`` (waiting for user review/action)
    """
    pr_state = "merged" if _is_pull_request_merged(pr_data) else pr_data.get("state", "open")
    if pr_state == "merged":
        return "passed", None, pr_state
    if pr_state == "closed":
        # Closed without merge => rejected/not approved.
        return "closed", None, pr_state
    if pr_state == "open":
        return "validation_pr_open", None, pr_state

    return "stale", f"Unexpected validation PR state: {pr_state}", pr_state


def _refresh_preflight_status_from_github(
    *,
    project: Project,
    github_user: str,
    token: str,
    db: Session,
) -> tuple[str, Optional[str], Optional[str]]:
    """Best-effort refresh of preflight status from the stored validation PR URL.

    Returns (status, error_summary, pr_state). If the PR URL is missing or the
    GitHub API is unavailable, returns the current cached status.
    """
    current_status = project.last_preflight_status or "not_run"
    pr_url = project.last_preflight_pr_url
    if not pr_url:
        # Normalize legacy check-based states into the PR-state model.
        if current_status == "waiting_for_checks":
            return "validation_pr_open", project.last_preflight_error, None
        return current_status, project.last_preflight_error, None

    parsed = _parse_github_pr_url(pr_url)
    if not parsed:
        _update_preflight_cached_fields(project, db, status="stale", error=_ERR_STALE_VALIDATION_URL)
        return project.last_preflight_status, project.last_preflight_error, None

    owner, repo, pr_number = parsed
    headers = {
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION,
        "Authorization": f"token {token}",
    }
    pr_api_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    pr_response = github_get(pr_api_url, github_user, db, headers=headers)
    if pr_response.status_code == 404:
        _update_preflight_cached_fields(project, db, status="stale", error=_ERR_VALIDATION_PR_MISSING)
        return project.last_preflight_status, project.last_preflight_error, None
    if pr_response.status_code == 403:
        _update_preflight_cached_fields(project, db, status="validation_repo_inaccessible", error=_VALIDATION_REPO_PERMISSION_ERROR)
        return project.last_preflight_status, project.last_preflight_error, None
    if pr_response.status_code != 200:
        # Do not clobber the cached status for transient API failures.
        if current_status == "waiting_for_checks":
            return "validation_pr_open", project.last_preflight_error, None
        return current_status, project.last_preflight_error, None

    pr_data = pr_response.json() or {}
    derived_status, derived_error, pr_state = _derive_preflight_status_from_github(
        pr_data=pr_data,
    )
    _update_preflight_cached_fields(project, db, status=derived_status, error=derived_error)
    return project.last_preflight_status or derived_status, project.last_preflight_error, pr_state


@router.get("/api/preflight-validation-status", responses=_responses(401, 404, 409, 500))
def get_preflight_validation_status(
    project_name: str,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    refresh_from_github: bool = True,
):
    """Return (and optionally refresh) the project's preflight validation status."""
    github_user = account.github_user
    try:
        if github_user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
        token = user_tokens[github_user]

        project = _find_project_by_name(db, github_user, project_name)
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)

        validation_repo = _get_validation_repo_name(project, db)
        if not validation_repo:
            return {
                "status": "not_configured",
                "validation_repo": None,
                "last_preflight_run_at": None,
                "last_preflight_error": None,
                "last_preflight_pr_url": None,
                "pr_state": None,
            }

        current_status = project.last_preflight_status or "not_run"
        if current_status == "waiting_for_checks":
            # Legacy: older versions used checks-based statuses.
            current_status = "validation_pr_open"
        pr_url = project.last_preflight_pr_url

        # Check whether workflow content has changed since the last approval and
        # downgrade to stale so the UI reflects that re-run is needed.
        _mark_stale_if_content_changed(project, db)
        current_status = project.last_preflight_status or current_status

        if not refresh_from_github or not pr_url:
            return {
                "status": current_status,
                "validation_repo": validation_repo,
                "last_preflight_run_at": project.last_preflight_run_at.isoformat() if project.last_preflight_run_at else None,
                "last_preflight_error": project.last_preflight_error,
                "last_preflight_pr_url": pr_url,
                "pr_state": None,
            }

        parsed = _parse_github_pr_url(pr_url)
        if not parsed:
            _update_preflight_cached_fields(project, db, status="stale", error=_ERR_STALE_VALIDATION_URL)
            return {
                "status": project.last_preflight_status,
                "validation_repo": validation_repo,
                "last_preflight_run_at": project.last_preflight_run_at.isoformat() if project.last_preflight_run_at else None,
                "last_preflight_error": project.last_preflight_error,
                "last_preflight_pr_url": project.last_preflight_pr_url,
                "pr_state": None,
            }

        owner, repo, pr_number = parsed
        headers = {
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION,
            "Authorization": f"token {token}",
        }
        pr_api_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
        pr_response = github_get(pr_api_url, github_user, db, headers=headers)
        if pr_response.status_code == 404:
            _update_preflight_cached_fields(project, db, status="stale", error=_ERR_VALIDATION_PR_MISSING)
            return {
                "status": project.last_preflight_status,
                "validation_repo": validation_repo,
                "last_preflight_run_at": project.last_preflight_run_at.isoformat() if project.last_preflight_run_at else None,
                "last_preflight_error": project.last_preflight_error,
                "last_preflight_pr_url": project.last_preflight_pr_url,
                "pr_state": None,
            }
        if pr_response.status_code == 403:
            _update_preflight_cached_fields(project, db, status="validation_repo_inaccessible", error=_VALIDATION_REPO_PERMISSION_ERROR)
            return {
                "status": project.last_preflight_status,
                "validation_repo": validation_repo,
                "last_preflight_run_at": project.last_preflight_run_at.isoformat() if project.last_preflight_run_at else None,
                "last_preflight_error": project.last_preflight_error,
                "last_preflight_pr_url": project.last_preflight_pr_url,
                "pr_state": None,
            }
        if pr_response.status_code != 200:
            # Don't clobber the cached status for transient API failures.
            return {
                "status": current_status,
                "validation_repo": validation_repo,
                "last_preflight_run_at": project.last_preflight_run_at.isoformat() if project.last_preflight_run_at else None,
                "last_preflight_error": project.last_preflight_error,
                "last_preflight_pr_url": project.last_preflight_pr_url,
                "pr_state": None,
            }

        pr_data = pr_response.json() or {}
        action_capabilities = _derive_pr_action_capabilities(pr_data)
        derived_status, derived_error, pr_state = _derive_preflight_status_from_github(
            pr_data=pr_data,
        )
        _update_preflight_cached_fields(project, db, status=derived_status, error=derived_error)
        # If the PR was just detected as merged (passed), immediately check whether
        # content has changed since the approval was recorded.
        _mark_stale_if_content_changed(project, db)
        return {
            "status": project.last_preflight_status or derived_status,
            "validation_repo": validation_repo,
            "last_preflight_run_at": project.last_preflight_run_at.isoformat() if project.last_preflight_run_at else None,
            "last_preflight_error": project.last_preflight_error,
            "last_preflight_pr_url": project.last_preflight_pr_url,
            "pr_state": pr_state,
            **action_capabilities,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error refreshing preflight status: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to refresh preflight status.")


def _fetch_validation_pr_or_raise(project, db, pr_api_url, github_user, headers):
    """Fetch a project's validation PR, updating cached state and raising on errors."""
    pr_response = github_get(pr_api_url, github_user, db, headers=headers)
    if pr_response.status_code == 404:
        _update_preflight_cached_fields(project, db, status="stale", error=_ERR_VALIDATION_PR_MISSING)
        raise HTTPException(status_code=404, detail="Validation PR not found.")
    if pr_response.status_code == 403:
        _update_preflight_cached_fields(project, db, status="validation_repo_inaccessible", error=_VALIDATION_REPO_PERMISSION_ERROR)
        raise HTTPException(status_code=403, detail=_VALIDATION_REPO_PERMISSION_ERROR)
    if pr_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Unable to fetch validation PR from GitHub.")
    return pr_response.json() or {}


def _close_validation_pr_or_raise(pr_api_url, github_user, db, headers):
    """Close an open validation PR via the GitHub API, raising on failure."""
    close_response = github_patch(pr_api_url, github_user, db, json={"state": "closed"}, headers=headers)
    if close_response.status_code != 200:
        raise HTTPException(
            status_code=close_response.status_code,
            detail=_github_error_message(
                close_response,
                f"Failed to close validation PR: {close_response.status_code}",
            ),
        )


def _cleanup_validation_pr_branch(pr_data, owner, repo, github_user):
    """Delete the validation PR source branch when head/base refs are available."""
    head_ref = (pr_data.get("head") or {}).get("ref")
    base_ref = (pr_data.get("base") or {}).get("ref")
    if head_ref and base_ref:
        return _delete_actions_manager_branch(
            owner=owner,
            repo=repo,
            branch_name=head_ref,
            target_branch=base_ref,
            github_user=github_user,
        )
    return False, BRANCH_INFO_NOT_FOUND


def _validate_merge_preflight_request(
    github_user: str,
    project,
    db: Session,
) -> tuple:
    """Validate auth/project/PR URL and return (owner, repo, pr_number, headers, token).

    Raises HTTPException on validation failure.
    """
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
    token = user_tokens[github_user]

    if not project:
        raise HTTPException(status_code=404, detail=PROJECT_ERROR)

    pr_url = project.last_preflight_pr_url
    if not pr_url:
        raise HTTPException(status_code=400, detail="No validation PR is recorded for this project.")

    parsed = _parse_github_pr_url(pr_url)
    if not parsed:
        _update_preflight_cached_fields(project, db, status="stale", error=_ERR_STALE_VALIDATION_URL)
        raise HTTPException(status_code=400, detail=_ERR_STALE_VALIDATION_URL)

    owner, repo, pr_number = parsed
    headers = {
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION,
        "Authorization": f"token {token}",
    }
    return owner, repo, pr_number, headers, token


def _handle_already_merged_validation_pr(
    project,
    db: Session,
    pr_data: dict,
    owner: str,
    repo: str,
    github_user: str,
    cleanup_branch: bool,
) -> dict:
    """Handle the case where a validation PR is already merged.

    Updates preflight status and optionally cleans up the branch.
    Returns the response dict.
    """
    _update_preflight_cached_fields(project, db, status="passed", error=None)

    branch_deleted = False
    branch_delete_warning = None
    if cleanup_branch:
        branch_deleted, branch_delete_warning = _cleanup_validation_pr_branch(
            pr_data, owner, repo, github_user
        )

    return {
        "message": "Validation PR already merged",
        "status": project.last_preflight_status,
        "last_preflight_pr_url": project.last_preflight_pr_url,
        "branch_deleted": branch_deleted,
        "branch_delete_warning": branch_delete_warning,
    }


def _execute_validation_pr_merge_and_respond(
    owner: str,
    repo: str,
    pr_number: int,
    headers: dict,
    github_user: str,
    db: Session,
    project,
    pr_data: dict,
    cleanup_branch: bool,
) -> dict:
    """Merge the validation PR and return the response dict.

    Raises HTTPException on merge failure.
    """
    merge_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/merge"
    merge_response, merge_method, merge_error = _merge_pull_request_with_fallback(
        merge_url=merge_url,
        github_user=github_user,
        db=db,
        headers=headers,
        commit_title=f"Merge validation preflight PR #{pr_number}",
    )

    if merge_response.status_code == 405:
        raise HTTPException(
            status_code=400,
            detail=merge_error or _github_error_message(
                merge_response,
                "Validation PR cannot be merged. Check permissions, branch protection, draft status, "
                "conflicts, and required checks.",
            ),
        )
    if merge_response.status_code == 404:
        raise HTTPException(status_code=404, detail="Validation PR not found.")
    if merge_response.status_code != 200:
        raise HTTPException(
            status_code=merge_response.status_code,
            detail=_github_error_message(merge_response, f"Failed to merge validation PR: {merge_response.status_code}"),
        )

    branch_deleted = False
    branch_delete_warning = None
    if cleanup_branch:
        branch_deleted, branch_delete_warning = _cleanup_validation_pr_branch(
            pr_data, owner, repo, github_user
        )

    _update_preflight_cached_fields(project, db, status="passed", error=None)
    return {
        "message": "Validation PR merged",
        "status": project.last_preflight_status,
        "last_preflight_pr_url": project.last_preflight_pr_url,
        "merge_method": merge_method,
        "branch_deleted": branch_deleted,
        "branch_delete_warning": branch_delete_warning,
    }


@router.patch("/api/close-preflight-validation-pr", responses=_responses(400, 401, 403, 404, 409, 500))
def close_preflight_validation_pr(
    payload: ClosePreflightValidationRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Close the project's validation PR and optionally delete the source branch."""
    github_user = account.github_user
    try:
        if github_user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
        token = user_tokens[github_user]

        project = _find_project_by_name(db, github_user, payload.project_name)
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)
        # Closing the validation PR writes to GitHub and can delete its source
        # branch, so it needs the same gate as running preflight.
        _require_project_editor(db, github_user, project)

        pr_url = project.last_preflight_pr_url
        if not pr_url:
            raise HTTPException(status_code=400, detail="No validation PR is recorded for this project.")

        parsed = _parse_github_pr_url(pr_url)
        if not parsed:
            _update_preflight_cached_fields(project, db, status="stale", error=_ERR_STALE_VALIDATION_URL)
            raise HTTPException(status_code=400, detail=_ERR_STALE_VALIDATION_URL)

        owner, repo, pr_number = parsed
        headers = {
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION,
            "Authorization": f"token {token}",
        }

        pr_api_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
        pr_data = _fetch_validation_pr_or_raise(project, db, pr_api_url, github_user, headers)
        pr_is_merged = _is_pull_request_merged(pr_data)
        current_state = pr_data.get("state", "open")

        if not pr_is_merged and current_state != "closed":
            _close_validation_pr_or_raise(pr_api_url, github_user, db, headers)

        branch_deleted = False
        branch_delete_warning = None
        if payload.cleanup_branch:
            branch_deleted, branch_delete_warning = _cleanup_validation_pr_branch(
                pr_data, owner, repo, github_user
            )

        # Closing an open validation PR rejects the preflight; a merged PR stays approved/passed.
        new_status = "passed" if pr_is_merged else "closed"
        _update_preflight_cached_fields(project, db, status=new_status, error=None)

        return {
            "message": "Validation PR closed",
            "status": new_status,
            "last_preflight_pr_url": project.last_preflight_pr_url,
            "branch_deleted": branch_deleted,
            "branch_delete_warning": branch_delete_warning,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error closing validation PR: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to close validation PR.")


@router.put("/api/merge-preflight-validation-pr", responses=_responses(400, 401, 403, 404, 409, 500))
def merge_preflight_validation_pr(
    payload: MergePreflightValidationRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Merge the project's validation PR to approve preflight."""
    github_user = account.github_user
    try:
        project = _find_project_by_name(db, github_user, payload.project_name)
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)
        # Merging the validation PR sets preflight to approved, and approved
        # preflight is what _ensure_preflight_allows_campaign checks before a
        # campaign may run. Without this the gate on running preflight can be
        # walked around by the role it exists to stop.
        _require_project_editor(db, github_user, project)

        owner, repo, pr_number, headers, _ = _validate_merge_preflight_request(
            github_user, project, db
        )

        pr_api_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
        pr_data = _fetch_validation_pr_or_raise(project, db, pr_api_url, github_user, headers)

        if _is_pull_request_merged(pr_data):
            return _handle_already_merged_validation_pr(
                project, db, pr_data, owner, repo, github_user, payload.cleanup_branch
            )

        if pr_data.get("state") != "open":
            _update_preflight_cached_fields(project, db, status="closed", error=None)
            raise HTTPException(status_code=400, detail="Validation PR is not open.")

        action_capabilities = _derive_pr_action_capabilities(pr_data)
        if action_capabilities.get("can_merge") is False:
            raise HTTPException(
                status_code=400,
                detail=action_capabilities.get("merge_block_reason") or "Validation PR cannot be merged.",
            )

        return _execute_validation_pr_merge_and_respond(
            owner, repo, pr_number, headers, github_user, db, project, pr_data, payload.cleanup_branch
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error merging validation PR: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to merge validation PR.")


def _fetch_pr_state_from_github(pr, github_user: str, token: str, db: Session) -> str:
    """Fetch the current state of a single PR from GitHub and persist any change to the DB.

    Returns the (possibly updated) PR state string.  Falls back to the cached
    ``pr.pr_state`` if the API call fails or returns a non-200 status.
    """
    headers = {
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION,
        "Authorization": f"token {token}",
    }
    owner, repo = pr.repo_name.split("/", 1)
    pr_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr.pr_number}"

    try:
        response = github_get(pr_url, github_user, db, headers=headers)
        if response.status_code != 200:
            pr._github_action_capabilities = {
                "can_merge": False,
                "merge_block_reason": f"Unable to refresh PR from GitHub: {response.status_code}",
                "can_close": False,
                "close_block_reason": f"Unable to refresh PR from GitHub: {response.status_code}",
            }
            return pr.pr_state

        pr_data = response.json()
        pr._github_action_capabilities = _derive_pr_action_capabilities(pr_data)
        pr_state = "merged" if _is_pull_request_merged(pr_data) else pr_data.get("state", "open")

        if pr.pr_state == pr_state:
            return pr_state
        return _apply_refreshed_outcome(db, pr, pr_state, pr_data, github_user)

    except ProjectDeleted:
        raise
    except Exception as e:
        print(f"⚠️ Error fetching PR #{pr.pr_number} state: {e}")
        return pr.pr_state


def _apply_refreshed_outcome(db: Session, pr, pr_state: str, pr_data: dict, github_user: str) -> str:
    """Record the outcome a refresh found for *pr*; returns its state now.

    Raises ProjectDeleted when the outcome finished the project's deletion,
    which took *pr* and the request's other rows with it.
    """
    if not pr.title and pr_data.get("title"):
        pr.title = pr_data["title"]
    if not pr.author and pr_data.get("user", {}).get("login"):
        pr.author = pr_data["user"]["login"]
    # GitHub's own timestamp: a refresh can find a PR merged days ago.
    at = _github_time(pr_data.get("merged_at" if pr_state == "merged" else "closed_at"))
    original_state, project_id = pr.pr_state, pr.project_id
    try:
        deleted = apply_pr_outcome(db, pr, pr_state, at=at, github_user=github_user)
    except Exception as commit_err:
        print(f"⚠️ Error persisting PR #{pr.pr_number} state: {commit_err}")
        db.rollback()
        pr.pr_state = original_state
        return original_state
    if deleted:
        raise ProjectDeleted(project_id)
    return pr_state


def _resolve_pr_state(pr, refresh_from_github: bool, github_user: str, token: str, db: Session) -> str:
    """Return the current state for a PR, optionally refreshing from GitHub."""
    # merged/closed are terminal — they cannot change, so skip the API call.
    if refresh_from_github and "/" in pr.repo_name and pr.pr_state == "open":
        return _fetch_pr_state_from_github(pr, github_user, token, db)
    return pr.pr_state


def _pr_action_fields(pr) -> dict:
    return getattr(pr, "_github_action_capabilities", {}) or {}


def _count_workflows_under_review(db: Session, project_id: int) -> int:
    return (
        db.query(Workflow)
        .join(ProjectWorkflow, Workflow.workflow_id == ProjectWorkflow.workflow_id)
        .filter(
            ProjectWorkflow.project_id == project_id,
            Workflow.workflow_status == "under_review",
        )
        .count()
    )


def _mark_owning_rwx_projects_synced(db: Session, linked_wf_ids: list) -> None:
    """Set pr_state synced on each owning RWX project with nothing left under review."""
    rwx_pids = (
        db.query(LinkedReusableWorkflow.rwx_project_id)
        .filter(LinkedReusableWorkflow.workflow_id.in_(linked_wf_ids))
        .distinct()
        .all()
    )
    for (rwx_pid,) in rwx_pids:
        if _count_workflows_under_review(db, rwx_pid) == 0:
            _update_project_pr_state(db, rwx_pid, "synced")


def _settle_linked_workflows(db: Session, project_id: int, new_wf_status: str, all_merged: bool) -> None:
    """Move a caller project's linked reusable workflows out of review with it."""
    linked_wf_ids = _get_linked_workflow_ids_for_project(db, project_id, only_if_status="under_review")
    # Global lock: linked reusable workflows still referenced by an open
    # PR campaign in another project must stay under_review.
    locked_ids = _reusable_workflow_ids_locked_by_open_campaign(db, linked_wf_ids)
    if locked_ids:
        print(
            f"🔒 Page-load sync: kept {len(locked_ids)} linked workflow(s) "
            f"under_review (open PR campaign in another project)"
        )
        linked_wf_ids = [wid for wid in linked_wf_ids if wid not in locked_ids]
    if not linked_wf_ids:
        return
    try:
        db.query(Workflow).filter(
            Workflow.workflow_id.in_(linked_wf_ids)
        ).update({"workflow_status": new_wf_status}, synchronize_session=False)
        db.commit()
        print(f"✅ Page-load sync: {len(linked_wf_ids)} linked workflow(s) → {new_wf_status}")
        if all_merged:
            _mark_owning_rwx_projects_synced(db, linked_wf_ids)
    except Exception as e:
        print(f"❌ Error updating linked workflow statuses: {str(e)}")
        db.rollback()


def _apply_all_prs_resolved_transitions(db: Session, project, all_merged: bool) -> None:
    """Apply workflow/project state transitions after all PRs have been resolved.

    When every PR was merged the workflows move to *synced_with_github*; when at
    least one PR was closed without a merge they revert to *committed_locally*.

    This function also handles the matching updates for any linked reusable
    workflows so the caller does not need to repeat that pattern. Call it
    through settle_project_prs, which decides ``all_merged``.
    """
    new_wf_status = "synced_with_github" if all_merged else "committed_locally"
    new_pr_state = "synced" if all_merged else "draft"

    _update_project_workflows_status(
        db, project.project_id, new_wf_status, only_if_status="under_review"
    )
    _update_project_custom_files_status(db, project.project_id, new_wf_status, only_if_status="under_review")
    _update_project_codeowners_status(db, project.project_id, new_wf_status, only_if_status="under_review")
    _settle_linked_workflows(db, project.project_id, new_wf_status, all_merged)

    # A project is only marked resolved (synced/draft) when none of its own
    # workflows remain under_review — e.g. an RWX project whose workflow is
    # still locked by a caller's open PR campaign keeps its current pr_state.
    remaining_under_review = _count_workflows_under_review(db, project.project_id)
    if remaining_under_review:
        print(
            f"🔒 Page-load sync: kept pr_state for project {project.project_id} — "
            f"{remaining_under_review} workflow(s) still under_review"
        )
        return

    _update_project_pr_state(db, project.project_id, new_pr_state)

    if all_merged:
        print("✅ Page-load sync: all PRs merged, workflows set to synced_with_github")
    else:
        print("✅ Page-load sync: at least one PR closed without merge, workflows reverted to committed_locally")


def _latest_campaign_prs(db: Session, project_id: int) -> list:
    """The PR rows of the project's most recent campaign.

    Closed PR rows are never removed, so judging "all merged" over the project's
    whole history meant one campaign closed months ago kept every later merge
    from syncing. Only projects whose PRs all predate campaigns fall back to
    every row.
    """
    latest = (
        db.query(func.max(ProjectPullRequest.campaign_id))
        .filter(ProjectPullRequest.project_id == project_id)
        .scalar()
    )
    query = db.query(ProjectPullRequest).filter(ProjectPullRequest.project_id == project_id)
    if latest is not None:
        query = query.filter(ProjectPullRequest.campaign_id == latest)
    return query.all()


def _has_files_under_review(db: Session, project) -> bool:
    """Whether anything in the project is waiting on a PR, so settling has work to do.

    Without this a page load with no open PR would stamp pr_state from the last
    campaign's outcome, overwriting the draft state of edits made since.
    """
    if project.pr_state in ("open", PROJECT_DELETING):
        return True
    pid = project.project_id
    if db.query(Workflow.workflow_id).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == pid, Workflow.workflow_status == "under_review"
    ).first():
        return True
    if db.query(CustomFile.id).filter(
        CustomFile.project_id == pid, CustomFile.file_status == "under_review"
    ).first():
        return True
    if db.query(Codeowners.id).filter(
        Codeowners.project_id == pid, Codeowners.status == "under_review"
    ).first():
        return True
    return bool(_get_linked_workflow_ids_for_project(db, pid, only_if_status="under_review"))


# What settle_project_prs returns when the PRs it settled finished the project's
# deletion, and get_project_pr_status's project_state once the project is gone.
PROJECT_DELETED = "deleted"
PROJECT_DELETED_DETAIL = "Project was deleted: its removal pull requests merged"


class ProjectDeleted(HTTPException):
    """A PR outcome finished the deletion of project *project_id* while a request was reading its rows.

    They went with it, so the request cannot carry on with them; this is its 404.
    """

    def __init__(self, project_id: int):
        super().__init__(status_code=404, detail=PROJECT_DELETED_DETAIL)
        self.project_id = project_id


def _call_off_deletion(db: Session, project, prs_still_open: bool) -> Optional[bool]:
    """The project stays and its files stay in GitHub, but where a merged removal took one off.

    Removal PRs still open become ordinary open PRs. What the merged ones
    removed they recorded when they merged (apply_pr_outcome).
    """
    if prs_still_open:
        _update_project_pr_state(db, project.project_id, "open")
        return None
    _apply_all_prs_resolved_transitions(db, project, False)
    # Whatever the transitions kept, the project must not stay read-only.
    _update_project_pr_state(db, project.project_id, "draft")
    return False


def _settle_deletion(db: Session, project, latest: list):
    """Finish or call off the deletion of *project*, whose open PRs are its removal PRs (*latest*).

    Any of them closed unmerged calls it off at once, even with others still
    open. Once all have merged the project goes, unless the record still holds
    one of its files delivered somewhere a removal did not reach.
    """
    states = {pr.pr_state for pr in latest}
    if not latest or "closed" in states:
        return _call_off_deletion(db, project, "open" in states)
    if "open" in states:
        return None
    if deliveries.project_delivered_anywhere(db, project.project_id):
        return _call_off_deletion(db, project, False)
    delete_project_record(db, project)
    return PROJECT_DELETED


def settle_project_prs(db: Session, project):
    """Move the project's files out of review once none of its PRs is open.

    The one place this is decided: every path that learns a PR outcome, the PR
    refreshes and the project page load all call it. Returns True when the files
    went to synced, False when they went back to committed_locally, None
    when a PR is still open or nothing was waiting, and PROJECT_DELETED when
    the project's removal PRs have all merged and the project is gone.
    """
    if project is None or not _has_files_under_review(db, project):
        return None
    if project.pr_state == PROJECT_DELETING:
        return _settle_deletion(db, project, _latest_campaign_prs(db, project.project_id))
    if db.query(ProjectPullRequest.pr_id).filter(
        ProjectPullRequest.project_id == project.project_id,
        ProjectPullRequest.pr_state == "open",
    ).first():
        return None
    latest = _latest_campaign_prs(db, project.project_id)
    if not latest:
        return None
    all_merged = all(pr.pr_state == "merged" for pr in latest)
    _apply_all_prs_resolved_transitions(db, project, all_merged)
    return all_merged


def settle_project_and_linked(db: Session, project):
    """Settle *project* and the Standard ↔ RWX projects linked to it; returns what settling *project* did.

    An RWX project's reusable workflow goes under review through a caller's
    campaign, so the PRs it waits on belong to the caller. Settling only the
    project being viewed would leave it under review until the caller's own page
    is opened.
    """
    related_ids = [pid for pid in _get_pr_visibility_project_map(db, project) if pid != project.project_id]
    for related in db.query(Project).filter(Project.project_id.in_(related_ids)).all():
        settle_project_prs(db, related)
    return settle_project_prs(db, project)


# Each kind's model, id column and status attribute, for _reopen_removed_files.
_FILE_STATUS = {
    deliveries.WORKFLOW: (Workflow, Workflow.workflow_id, "workflow_status"),
    deliveries.CUSTOM_FILE: (CustomFile, CustomFile.id, "file_status"),
    deliveries.CODEOWNERS: (Codeowners, Codeowners.id, "status"),
}


def _reopen_removed_files(db: Session, removed: list) -> None:
    """Mark each file a merge removed from GitHub, but the project keeps, as a local change.

    *removed* is what deliveries.record_pr_outcome settled. A file queued for
    removal (pending_delete) is going anyway. Any other one was taken off by a
    project deletion, and if that deletion is called off the file is no
    longer synced where its removal merged: it is there to deliver again.
    """
    ids_by_kind: dict = {}
    for kind, file_id in removed:
        ids_by_kind.setdefault(kind, []).append(file_id)
    for kind, file_ids in ids_by_kind.items():
        model, id_column, status = _FILE_STATUS[kind]
        for record in db.query(model).filter(id_column.in_(file_ids)):
            if not getattr(record, "pending_delete", False):
                setattr(record, status, "committed_locally")


def _github_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        print(f"⚠️ Could not parse GitHub timestamp {value!r}")
        return None


def apply_pr_outcome(db: Session, pr, state: str, *, at: Optional[datetime] = None,
                     github_user: Optional[str] = None) -> bool:
    """Record that *pr* merged or closed, and everything that follows from it (_apply_pr_outcome).

    The merge button and GitHub's webhook for that merge can both settle the
    last removal PR at once. Whichever loses finds the project's rows gone:
    the deletion it was finishing is finished, so it says so instead of failing.
    """
    project_id = pr.project_id
    try:
        return _apply_pr_outcome(db, pr, state, at=at, github_user=github_user)
    except (StaleDataError, ObjectDeletedError):
        db.rollback()
        if db.query(Project.project_id).filter(Project.project_id == project_id).first() is None:
            return True
        raise


def _apply_pr_outcome(db: Session, pr, state: str, *, at: Optional[datetime] = None,
                      github_user: Optional[str] = None) -> bool:
    """Record that *pr* merged or closed, and everything that follows from it.

    The merge and close buttons, the webhook and a refresh from GitHub all call
    this, so a merge has the same effects however it is discovered. Each used to
    carry its own copy, and they disagreed: a merge found by refresh cleared no
    drift and ran no rollback action, the webhook never recorded merged_at, and
    only the refresh sent a notification.

    *at* is GitHub's merged_at/closed_at when the caller has it. Without a user
    token (the webhook) a rollback "revert" degrades to "keep", and drift reports
    the divergence instead.

    Returns True when this merge finished the project's deletion. *pr* and the
    project's other rows are gone then, so a caller wanting anything of *pr*
    reads it first.
    """
    if state not in ("merged", "closed") or pr.pr_state == state:
        return False
    previous = pr.pr_state
    pr.pr_state = state
    when = at or datetime.now(timezone.utc)
    if state == "merged" and pr.merged_at is None:
        pr.merged_at = when
    elif state == "closed" and pr.closed_at is None:
        pr.closed_at = when
    # In the same commit as the state: once it is recorded the early return
    # above means this never runs again, so a settlement committed separately
    # could be lost for good. Before the drift clears below, which act on what
    # this PR delivered.
    _reopen_removed_files(db, deliveries.record_pr_outcome(db, pr, state))
    db.commit()
    print(f"✅ Updated PR #{pr.pr_number} in {pr.repo_name} to {state}")

    # Separate from the commit above: a notification failure must never undo
    # the recorded outcome.
    try:
        record_campaign_pr_transition(db, pr, previous, state)
    except Exception as notify_err:
        print(f"⚠️ Error recording campaign PR transition notification: {notify_err}")
        db.rollback()

    project = db.query(Project).filter(Project.project_id == pr.project_id).first()
    if project is None:
        return False
    if state == "merged":
        _clear_drift_for_merged_pr(db, project, pr)
        _create_pr_merged_version_entries(db, project, pr.pr_number, pr.repo_name)
    if settle_project_prs(db, project) == PROJECT_DELETED:
        return True
    if state == "merged":
        # Last, so a rollback's choice about ActionsManager's own copy wins over
        # the synced_with_github stamping above.
        _apply_rollback_campaign_action(db, pr, github_user)
    return False


def _get_pr_visibility_project_map(db: Session, project: Project) -> dict[int, str]:
    """Return project IDs whose PRs are visible from the queried project."""
    project_id_map: dict[int, str] = {project.project_id: project.project_name}

    if project.project_type == "standard":
        linked_rwx_rows = (
            db.query(LinkedReusableWorkflow)
            .filter(LinkedReusableWorkflow.standard_project_id == project.project_id)
            .all()
        )
        rwx_project_ids = {row.rwx_project_id for row in linked_rwx_rows}
        if rwx_project_ids:
            rwx_projects = db.query(Project).filter(
                Project.project_id.in_(rwx_project_ids)
            ).all()
            for rwx_proj in rwx_projects:
                project_id_map[rwx_proj.project_id] = rwx_proj.project_name
    else:
        linked_std_rows = (
            db.query(LinkedReusableWorkflow)
            .filter(LinkedReusableWorkflow.rwx_project_id == project.project_id)
            .all()
        )
        std_project_ids = {row.standard_project_id for row in linked_std_rows}
        if std_project_ids:
            std_projects = db.query(Project).filter(
                Project.project_id.in_(std_project_ids)
            ).all()
            for std_proj in std_projects:
                project_id_map[std_proj.project_id] = std_proj.project_name

    return project_id_map


def _split_workflow_names(workflow_names: Optional[str]) -> List[str]:
    if not workflow_names:
        return []
    return [name.strip() for name in workflow_names.split(",") if name.strip()]


def _derive_campaign_name(prs: List[ProjectPullRequest]) -> str:
    workflows: List[str] = []
    for pr in prs:
        for workflow in _split_workflow_names(pr.workflow_names):
            if workflow not in workflows:
                workflows.append(workflow)
    if len(workflows) == 1:
        return f"Update {workflows[0]}"
    if len(workflows) > 1:
        return f"Update {workflows[0]} + {len(workflows) - 1} more"
    title = next((pr.title for pr in prs if pr.title), None)
    return title or "File rollout"


def _campaign_status_from_counts(open_count: int, merged_count: int, closed_count: int) -> str:
    if open_count > 0:
        return "open"
    if merged_count > 0 and closed_count == 0:
        return "completed"
    if merged_count > 0 and closed_count > 0:
        return "partially_completed"
    if closed_count > 0:
        return "cancelled"
    return "open"


def _campaign_snapshot_response(campaign: Optional[ProjectPRCampaign]) -> dict:
    """Decode a campaign's creation-time snapshot into PRCampaignResponse fields.

    Campaigns created before the snapshot existed, and heuristically grouped
    legacy PR rows, have no record to decode — they fall back to empty, and the
    UI renders only the live-derived view for them.
    """
    # JSON-encoded snapshot columns, with the fallback used when they are NULL.
    json_fields = {
        "target_repos": [], "base_commits": {}, "policy_version": {},
        "branch_protection": {}, "target_pr_urls": {},
    }
    # Plain columns, decoded as-is.
    scalar_fields = ("rollback_of_campaign_id", "rollback_am_action")

    if campaign is None:
        return {**json_fields, **dict.fromkeys(scalar_fields)}

    decoded = {field: getattr(campaign, field, None) for field in scalar_fields}
    for field, fallback in json_fields.items():
        raw = getattr(campaign, field, None)
        try:
            decoded[field] = json.loads(raw) if raw else fallback
        except (ValueError, TypeError):
            decoded[field] = fallback
    return decoded


def _campaign_group_key(pr: ProjectPullRequest) -> tuple:
    """Best-effort campaign key using existing PR fields without new schema."""
    workflow_key = ",".join(sorted(_split_workflow_names(pr.workflow_names)))
    created_day = pr.created_at.date().isoformat() if pr.created_at else ""
    return (pr.project_id, workflow_key, pr.target_branch, created_day)


def _campaign_pr_response(
    pr: ProjectPullRequest,
    project_id_map: dict[int, str],
    queried_project_id: int,
    reusable_workflow_names: set[str],
) -> PRCampaignPRResponse:
    source_project_name = (
        project_id_map.get(pr.project_id)
        if pr.project_id != queried_project_id
        else None
    )
    is_reusable_workflow_pr = any(
        _normalize_reusable_workflow_name(name) in reusable_workflow_names
        for name in _split_workflow_names(pr.workflow_names)
    )
    return PRCampaignPRResponse(
        pr_id=pr.pr_id,
        repo_name=pr.repo_name,
        pr_number=pr.pr_number,
        pr_url=pr.pr_url,
        pr_state=pr.pr_state,
        branch_name=pr.branch_name,
        target_branch=pr.target_branch,
        title=pr.title,
        author=pr.author,
        body=pr.body,
        workflow_names=pr.workflow_names,
        file_names=pr.file_names,
        created_at=pr.created_at.isoformat() if pr.created_at else "",
        updated_at=pr.updated_at.isoformat() if pr.updated_at else "",
        merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
        closed_at=pr.closed_at.isoformat() if pr.closed_at else None,
        source_project_name=source_project_name,
        actor=pr.author,
        is_reusable_workflow_pr=is_reusable_workflow_pr,
        **_pr_action_fields(pr),
    )


@router.get("/api/project-pr-status", responses=_responses(401, 404, 409, 500))
def get_project_pr_status(
    project_name: str,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    refresh_from_github: bool = False
):
    """
    Get the PR status for a project, including list of all PRs and their current states.

    Cross-project visibility
    ------------------------
    Pull requests are surfaced across linked Standard ↔ Reusable-Workflow (RWX)
    project pairs so that users see the full picture from either side:

    * Standard Project queried → also includes PRs created by linked
      RWX projects for workflows owned by those RWX projects.
    * RWX Project queried → also includes PRs created by each Standard Project
      that links to it (for reusable workflows in the RWX repo).

    This ensures that when a Standard project creates a PR for a linked reusable
    workflow, the RWX project shows:
    - Open PR banner
    - Locked workflow state
    - PR in Open PRs list

    Args:
        github_user: GitHub username
        project_name: Project name
        db: Database session
        refresh_from_github: If True, fetches current state from GitHub API and updates database.
                           If False (default), returns cached state from database for fast loading.

    Returns cached database state by default for fast loading. Only fetches from GitHub
    when explicitly requested via refresh_from_github=true.

    A refresh that finds the project's last removal PR merged finishes its
    deletion; the answer is then project_state "deleted" and nothing else.
    """
    github_user = account.github_user
    try:
        if github_user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

        token = user_tokens[github_user]

        project = _find_project_by_name(db, github_user, project_name)
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)
        return _project_pr_status_after_deletions(db, project, refresh_from_github, github_user, token)

    except ProjectDeleted:
        return ProjectPRStatusResponse(project_state=PROJECT_DELETED, pull_requests=[], total_prs=0,
                                       open_prs=0, merged_prs=0, closed_prs=0, locked_workflow_ids=[])
    except HTTPException:
        # Not a server error: the project lookup raises 409 for an ambiguous
        # name (#2240), and the catch-all below would report it as a 500.
        raise
    except Exception as e:
        print(f"❌ Error getting project PR status: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error getting project PR status")


def _project_pr_status_after_deletions(db: Session, project: Project, refresh_from_github: bool,
                                      github_user: str, token: str) -> ProjectPRStatusResponse:
    """_project_pr_status, read again when a refresh finished a linked project's deletion.

    The PRs of linked projects are listed too, and that project's went with it.
    Raises ProjectDeleted when the deletion was *project*'s own.
    """
    project_id = project.project_id
    while True:
        try:
            return _project_pr_status(db, project, refresh_from_github, github_user, token)
        except ProjectDeleted as gone:
            if gone.project_id == project_id:
                raise


def _project_pr_status(db: Session, project: Project, refresh_from_github: bool,
                       github_user: str, token: str) -> ProjectPRStatusResponse:
    # PR status is shared across linked Standard ↔ RWX project pairs.
    project_id_map = _get_pr_visibility_project_map(db, project)

    prs = db.query(ProjectPullRequest).filter(
        ProjectPullRequest.project_id.in_(project_id_map.keys())
    ).all()

    pr_responses = []
    counts = {"open": 0, "merged": 0, "closed": 0}

    for pr in prs:
        pr_state = _resolve_pr_state(pr, refresh_from_github, github_user, token, db)
        if pr_state in counts:
            counts[pr_state] += 1

        # This server-side filter applies only to the returned
        # `pull_requests` list so resolved PRs do not appear in the
        # open PR drawer or expose open-PR actions. Summary counts are
        # still accumulated across all PR states above, and the
        # "Pull Requests Open" banner visibility is determined
        # separately by `project.pr_state`, not by this filter.
        # Merged and closed PRs remain available via the
        # /api/project-pr-campaigns endpoint backing the PR History view.
        if pr_state == "open":
            pr_responses.append(_open_pr_status(pr, project, project_id_map))

    # Each PR the refresh found resolved has already been applied. Settling
    # again covers a project left under_review by PRs that resolved earlier
    # (the frontend sends refresh_from_github for exactly that case).
    if refresh_from_github:
        if settle_project_and_linked(db, project) == PROJECT_DELETED:
            raise ProjectDeleted(project.project_id)
        db.refresh(project)

    return ProjectPRStatusResponse(
        project_state=project.pr_state or "new",
        pull_requests=pr_responses,
        total_prs=len(prs),
        open_prs=counts["open"],
        merged_prs=counts["merged"],
        closed_prs=counts["closed"],
        locked_workflow_ids=_pr_status_locked_workflow_ids(db, project),
    )


def _open_pr_status(pr, project: Project, project_id_map: dict) -> PRStatusResponse:
    # A PR of a linked project is labelled with the project it came from.
    source_project_name = project_id_map.get(pr.project_id) if pr.project_id != project.project_id else None
    return PRStatusResponse(
        repo_name=pr.repo_name,
        pr_number=pr.pr_number,
        pr_url=pr.pr_url,
        pr_state="open",
        branch_name=pr.branch_name,
        target_branch=pr.target_branch,
        created_at=pr.created_at.isoformat() if pr.created_at else "",
        updated_at=pr.updated_at.isoformat() if pr.updated_at else "",
        source_project_name=source_project_name,
        **_pr_action_fields(pr),
    )


def _pr_status_locked_workflow_ids(db: Session, project: Project) -> list:
    """The cross-project lock set, so the frontend can avoid flipping linked
    reusable workflow badges that are still held under_review by an open PR
    campaign in *another* project (a sibling caller or the owning RWX project).
    That campaign is not necessarily visible in this project's ``open_prs``
    count and would otherwise look "resolved".
    """
    candidate_wf_ids: set[int] = set()
    # Linked reusable workflows for a standard project.
    candidate_wf_ids.update(
        wid for (wid,) in
        db.query(Workflow.workflow_id)
        .join(LinkedReusableWorkflow, LinkedReusableWorkflow.workflow_id == Workflow.workflow_id)
        .filter(LinkedReusableWorkflow.standard_project_id == project.project_id)
        .all()
    )
    # Reusable workflows owned by the project (the RWX-project view).
    candidate_wf_ids.update(
        wid for (wid,) in
        db.query(Workflow.workflow_id)
        .join(ProjectWorkflow, Workflow.workflow_id == ProjectWorkflow.workflow_id)
        .filter(
            ProjectWorkflow.project_id == project.project_id,
            Workflow.reusable_workflow.is_(True),
        )
        .all()
    )
    if not candidate_wf_ids:
        return []
    return sorted(_reusable_workflow_ids_locked_by_open_campaign(db, list(candidate_wf_ids)))


# Protected branch names that must never be deleted by ActionsManager.
_PROTECTED_BRANCH_NAMES = frozenset({
    "main", "master", "develop", "development", "staging", "production", "prod",
})
_PROTECTED_BRANCH_PREFIXES = ("release/", "release-", "hotfix/", "hotfix-")

# Regex that matches the *new* unique ActionsManager branch naming convention:
#   actions-manager/<project_code>/<repo_slug>/<short_id>-<base_branch>
# Also matches the *legacy* format:
#   actions-manager/<project_code>-<base_branch>
# Both start with "actions-manager/" so a single prefix check is sufficient for
# the first safety gate; the full regex is used for belt-and-braces validation.
_AM_BRANCH_RE = re.compile(r'^actions-manager/')


def _is_safe_to_delete_am_branch(branch_name: str, target_branch: str) -> tuple:
    """
    Validate that *branch_name* is safe to delete under ActionsManager rules.

    Rules:
    - Branch must start with 'actions-manager/'
    - Branch must not equal target_branch
    - Branch must not be a well-known protected branch name
    - Branch must not start with a protected prefix

    Returns:
        (is_safe: bool, reason: str)  – reason is non-empty when is_safe is False.
    """
    if not _AM_BRANCH_RE.match(branch_name):
        return False, f"Branch '{branch_name}' does not follow ActionsManager naming convention"

    if branch_name == target_branch:
        return False, "Source branch and target branch are the same — refusing to delete"

    # Protect well-known stable branches by checking the full branch_name and the
    # last path segment (base). Because branch_name always starts with
    # "actions-manager/", we also check the path *after* that prefix to catch
    # patterns like "actions-manager/release/1.0" or "actions-manager/develop".
    base = branch_name.split("/")[-1] if "/" in branch_name else branch_name
    # Strip the leading "actions-manager/" so prefix rules apply to the rest of the path
    am_stripped = branch_name[len("actions-manager/"):]

    if base in _PROTECTED_BRANCH_NAMES or branch_name in _PROTECTED_BRANCH_NAMES:
        return False, f"Branch '{branch_name}' is a protected branch — refusing to delete"

    for prefix in _PROTECTED_BRANCH_PREFIXES:
        if am_stripped.startswith(prefix) or base.startswith(prefix):
            return False, f"Branch '{branch_name}' matches protected prefix '{prefix}'"

    return True, ""


def _delete_actions_manager_branch(
    owner: str,
    repo: str,
    branch_name: str,
    target_branch: str,
    github_user: str,
) -> tuple:
    """
    Attempt to delete an ActionsManager-created source branch after a successful merge.

    Args:
        owner: Repository owner.
        repo: Repository name.
        branch_name: The ActionsManager source branch to delete.
        target_branch: The base branch the PR was merged into (used for safety check).
        github_user: Authenticated GitHub user.
        db: Database session.

    Returns:
        (deleted: bool, warning: str or None)
    """
    is_safe, reason = _is_safe_to_delete_am_branch(branch_name, target_branch)
    if not is_safe:
        print(f"⚠️ Skipping branch deletion: {reason}")
        return False, reason

    delete_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{branch_name}"
    try:
        # Use requests directly with the authenticated token
        token = user_tokens.get(github_user)
        if not token:
            msg = "Cannot delete branch: user token not available"
            print(f"⚠️ {msg}")
            return False, msg

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"token {token}",
        }
        del_response = requests.delete(delete_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

        if del_response.status_code == 204:
            print(f"✅ Deleted ActionsManager branch '{branch_name}' in {owner}/{repo}")
            return True, None
        elif del_response.status_code == 422:
            msg = f"Branch '{branch_name}' is protected or cannot be deleted: {del_response.text[:200]}"
            print(f"⚠️ {msg}")
            return False, msg
        elif del_response.status_code == 404:
            # Branch was already deleted (e.g. GitHub auto-delete on merge)
            print(f"📌 Branch '{branch_name}' already gone (404) — treating as deleted")
            return True, None
        else:
            msg = f"GitHub API returned {del_response.status_code} deleting '{branch_name}': {del_response.text[:200]}"
            print(f"⚠️ {msg}")
            return False, msg
    except Exception as exc:
        print(f"❌ Exception while deleting branch '{branch_name}': {exc}")
        msg = "Branch deletion failed due to an unexpected error. Check server logs for details."
        return False, msg


def _merge_pull_request_with_fallback(
    *,
    merge_url: str,
    github_user: str,
    db: Session,
    headers: dict,
    commit_title: str,
):
    """Try supported GitHub merge methods so repos that disable merge commits still work."""
    last_response = None
    attempted_messages = []
    for merge_method in ("merge", "squash", "rebase"):
        merge_payload = {
            "commit_title": commit_title,
            "merge_method": merge_method,
        }
        response = github_put(merge_url, github_user, db, json=merge_payload, headers=headers)
        if response.status_code == 200:
            return response, merge_method, None

        last_response = response
        if response.status_code != 405:
            return response, merge_method, None

        message = _github_error_message(response, f"Pull request cannot be merged with {merge_method}.")
        attempted_messages.append(f"{merge_method}: {message}")

    return last_response, "rebase", "GitHub rejected all merge methods. " + " | ".join(attempted_messages)


def _create_pr_merged_version_entries(db, project, pr_number, repo_name):
    """Create a 'pr_merged' version entry for each non-reusable workflow in the project."""
    try:
        nr_workflows = db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id,
            Workflow.reusable_workflow == False
        ).all()
        for wf in nr_workflows:
            create_workflow_version(
                db,
                wf.workflow_id,
                wf.workflow_yaml,
                metadata={
                    'action': 'pr_merged',
                    'pr_number': pr_number,
                    'repo_name': repo_name,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                },
                commit=False
            )
        db.commit()
        print(f"✅ Created pr_merged version entries for {len(nr_workflows)} workflow(s)")
    except Exception as e:
        print(f"❌ Error creating pr_merged version entries: {str(e)}")
        db.rollback()


def _clear_drift_for_merged_pr(db, project, pr_record) -> None:
    """Clear stored drift for the files a merged PR delivered, of every kind that drifts.

    Scoped to what the PR delivered, on its own repo and branch: a merge only
    proves those files now match GitHub, so clearing project-wide would hide
    real drift elsewhere. A rollback is no exception: its merge delivered the
    blob it carried (deliveries.record_pr_outcome), which GitHub now holds.
    """
    if not pr_record:
        return
    try:
        for kind in (deliveries.WORKFLOW, deliveries.CODEOWNERS, deliveries.CUSTOM_FILE):
            for file_id in deliveries.delivered_by_pr(db, kind, pr_record):
                clear_drift(db, project, kind, file_id, pr_record.repo_name, pr_record.target_branch)
    except Exception as e:
        # The outcome is already committed; a later check corrects the stale rows.
        print(f"❌ Error clearing drift after a merge: {str(e)}")
        db.rollback()


def _apply_rollback_campaign_action(db: Session, pr_record, github_user: Optional[str] = None) -> None:
    """Reconcile ActionsManager's stored workflows when a rollback campaign's PR merges.

    A no-op for every PR that is not part of a rollback campaign. Imported inside
    the function rather than at module scope: campaign_rollback imports this module
    for the delivery helpers, so a top-level import either way would be circular.
    """
    from campaign_rollback import handle_rollback_pr_merged
    handle_rollback_pr_merged(db, pr_record, github_user)


def _handle_merged_pull_request(*, response, project, repo_name, pr_number, owner, repo, github_user, db, merge_method):
    """Persist database state and clean up after a successful PR merge; returns the response dict."""
    merge_data = response.json()
    print(f"✅ Successfully merged PR #{pr_number} in {repo_name}")

    pr_record = db.query(ProjectPullRequest).filter_by(
        project_id=project.project_id,
        repo_name=repo_name,
        pr_number=pr_number
    ).first()
    # Read before the outcome: the last removal PR of a deletion takes its row with the project.
    branch_name, target_branch = (pr_record.branch_name, pr_record.target_branch) if pr_record else (None, None)
    project_deleted = bool(pr_record) and apply_pr_outcome(db, pr_record, "merged", github_user=github_user)

    # Attempt to delete the source branch (best-effort; never fails the merge)
    branch_deleted = False
    branch_delete_warning = None
    if branch_name and target_branch:
        branch_deleted, branch_delete_warning = _delete_actions_manager_branch(
            owner=owner,
            repo=repo,
            branch_name=branch_name,
            target_branch=target_branch,
            github_user=github_user,
        )
    else:
        branch_delete_warning = "PR record or " + BRANCH_INFO_NOT_FOUND
        print(f"⚠️ {branch_delete_warning}")

    return {
        "message": "Pull request merged successfully",
        "pr_number": pr_number,
        "repo_name": repo_name,
        "sha": merge_data.get("sha"),
        "merged": True,
        "merge_method": merge_method,
        "branch_deleted": branch_deleted,
        "branch_delete_warning": branch_delete_warning,
        "project_deleted": project_deleted,
    }


@router.put("/api/merge-pull-request", responses=_responses(400, 401, 403, 404, 409, 500))
def merge_pull_request(
    payload: MergePullRequestRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """
    Merge a pull request for a project.
    Updates the PR state in the database and triggers project state transition if needed.
    """
    try:
        github_user = account.github_user
        project_name = payload.project_name
        repo_name = payload.repo_name
        pr_number = payload.pr_number
        
        # Check authentication
        if github_user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
        
        token = user_tokens[github_user]
        
        # Get project
        project = _find_project_by_name(db, github_user, project_name)
        
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)
        
        # Validate repo_name format
        if "/" not in repo_name:
            raise HTTPException(status_code=400, detail="Invalid repository name format. Expected 'owner/repo'")

        # Merging or closing a removal pull request is how a deletion finishes
        # or is called off, so it stays open to editors while the project is read-only.
        _require_project_editor(db, github_user, project, settling=True)
        _require_recorded_pull_request(db, project, repo_name, pr_number)

        owner, repo = repo_name.split("/", 1)
        
        # Merge PR using GitHub API
        merge_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/merge"
        headers = {
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION,
            "Authorization": f"token {token}"
        }
        
        response, merge_method, merge_error = _merge_pull_request_with_fallback(
            merge_url=merge_url,
            github_user=github_user,
            db=db,
            headers=headers,
            commit_title=f"Merge pull request #{pr_number}",
        )
        
        if response.status_code == 200:
            # Successfully merged
            return _handle_merged_pull_request(
                response=response,
                project=project,
                repo_name=repo_name,
                pr_number=pr_number,
                owner=owner,
                repo=repo,
                github_user=github_user,
                db=db,
                merge_method=merge_method,
            )
        elif response.status_code == 405:
            # PR cannot be merged (conflicts, checks failing, etc.)
            raise HTTPException(
                status_code=400,
                detail=merge_error or _github_error_message(
                    response,
                    "Pull request cannot be merged. Check permissions, branch protection, draft status, "
                    "conflicts, and required checks.",
                )
            )
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail="Pull request not found")
        else:
            # Other errors
            raise HTTPException(
                status_code=response.status_code,
                detail=_github_error_message(response, f"Failed to merge pull request: {response.status_code}")
            )
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error merging pull request: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error merging pull request")


@router.patch("/api/close-pull-request", responses=_responses(400, 401, 403, 404, 409, 500))
def close_pull_request(
    payload: ClosePullRequestRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """
    Close a pull request for a project without merging.
    Updates the PR state in the database and triggers project state transition if needed.
    """
    try:
        github_user = account.github_user
        project_name = payload.project_name
        repo_name = payload.repo_name
        pr_number = payload.pr_number
        
        # Check authentication
        if github_user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
        
        token = user_tokens[github_user]
        
        # Get project
        project = _find_project_by_name(db, github_user, project_name)
        
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)
        
        # Validate repo_name format
        if "/" not in repo_name:
            raise HTTPException(status_code=400, detail="Invalid repository name format. Expected 'owner/repo'")

        # Merging or closing a removal pull request is how a deletion finishes
        # or is called off, so it stays open to editors while the project is read-only.
        _require_project_editor(db, github_user, project, settling=True)
        _require_recorded_pull_request(db, project, repo_name, pr_number)

        owner, repo = repo_name.split("/", 1)
        
        # Close PR using GitHub API
        pr_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
        headers = {
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION,
            "Authorization": f"token {token}"
        }
        
        close_payload = {
            "state": "closed"
        }
        
        response = github_patch(pr_url, github_user, db, json=close_payload, headers=headers)
        
        if response.status_code == 200:
            # Successfully closed
            pr_data = response.json()
            print(f"✅ Successfully closed PR #{pr_number} in {repo_name}")
            
            pr_record = db.query(ProjectPullRequest).filter_by(
                project_id=project.project_id,
                repo_name=repo_name,
                pr_number=pr_number
            ).first()
            if pr_record:
                apply_pr_outcome(db, pr_record, "closed", github_user=github_user)

            return {
                "message": "Pull request closed successfully",
                "pr_number": pr_number,
                "repo_name": repo_name,
                "state": pr_data.get("state"),
                "closed": True
            }
        elif response.status_code == 404:
            raise HTTPException(status_code=404, detail="Pull request not found")
        else:
            # Other errors
            raise HTTPException(
                status_code=response.status_code,
                detail=_github_error_message(response, f"Failed to close pull request: {response.status_code}")
            )
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error closing pull request: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error closing pull request")


@router.get("/api/project-pr-campaigns", responses=_responses(401, 404, 409, 500))
def get_project_pr_campaigns(
    project_name: str,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    refresh_from_github: bool = False
) -> PRCampaignsResponse:
    """
    Return PR Campaigns for a project.

    PRs created since campaign tracking was added are grouped by their unique
    campaign_id — every PR campaign creation run produces a new campaign record,
    so new PRs are never appended to a previous campaign. Legacy PR rows without
    a campaign_id are grouped heuristically by project, workflow set, target
    branch, and creation day so existing PR History data remains visible.
    """
    github_user = account.github_user
    try:
        if github_user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

        token = user_tokens[github_user]
        project = _find_project_by_name(db, github_user, project_name)
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)

        project_id_map = _get_pr_visibility_project_map(db, project)
        visible_projects = db.query(Project).filter(
            Project.project_id.in_(project_id_map.keys())
        ).all()
        # Deferred import: projects.py imports this module at module scope, so
        # importing it back at the top would be circular.
        from projects import _migrate_branch_option
        project_branch_option_map = {
            proj.project_id: _migrate_branch_option(proj.branch_option)
            for proj in visible_projects
        }
        linked_rows = db.query(LinkedReusableWorkflow, Workflow).join(
            Workflow, LinkedReusableWorkflow.workflow_id == Workflow.workflow_id
        ).filter(
            or_(
                LinkedReusableWorkflow.standard_project_id.in_(project_id_map.keys()),
                LinkedReusableWorkflow.rwx_project_id.in_(project_id_map.keys()),
            )
        ).all()
        reusable_workflow_names: set[str] = {
            _normalize_reusable_workflow_name(workflow.workflow_name)
            for _link, workflow in linked_rows
        }
        prs = db.query(ProjectPullRequest).filter(
            ProjectPullRequest.project_id.in_(project_id_map.keys())
        ).order_by(ProjectPullRequest.created_at.desc()).all()

        for pr in prs:
            pr.pr_state = _resolve_pr_state(pr, refresh_from_github, github_user, token, db)

        if refresh_from_github:
            settle_project_and_linked(db, project)

        grouped: dict[tuple, List[ProjectPullRequest]] = {}
        for pr in prs:
            if pr.campaign_id is not None:
                key = (pr.project_id, "campaign", pr.campaign_id)
            else:
                # Legacy rows created before campaign tracking: best-effort grouping.
                key = _campaign_group_key(pr)
            grouped.setdefault(key, []).append(pr)

        tracked_campaign_ids = {pr.campaign_id for pr in prs if pr.campaign_id is not None}
        campaign_records: dict[int, ProjectPRCampaign] = {}
        if tracked_campaign_ids:
            campaign_records = {
                record.campaign_id: record
                for record in db.query(ProjectPRCampaign).filter(
                    ProjectPRCampaign.campaign_id.in_(tracked_campaign_ids)
                ).all()
            }

        campaigns: List[PRCampaignResponse] = []
        pr_items: List[PRCampaignPRResponse] = []
        open_prs = 0
        merged_prs = 0
        closed_prs = 0

        for key, campaign_prs in grouped.items():
            campaign_prs.sort(key=lambda item: item.created_at or datetime.min)
            campaign_project_id = key[0]
            open_count = sum(1 for pr in campaign_prs if pr.pr_state == "open")
            merged_count = sum(1 for pr in campaign_prs if pr.pr_state == "merged")
            closed_count = sum(1 for pr in campaign_prs if pr.pr_state == "closed")
            total_count = len(campaign_prs)
            status = _campaign_status_from_counts(open_count, merged_count, closed_count)
            completed_prs = [pr for pr in campaign_prs if pr.pr_state in ("merged", "closed")]
            completed_at = None
            if status != "open" and completed_prs:
                completed_at_dt = max(
                    (pr.merged_at or pr.closed_at or pr.updated_at)
                    for pr in completed_prs
                    if pr.merged_at or pr.closed_at or pr.updated_at
                )
                completed_at = completed_at_dt.isoformat() if completed_at_dt else None

            target_branches = sorted({pr.target_branch for pr in campaign_prs if pr.target_branch})
            workflow_names = []
            for pr in campaign_prs:
                for workflow in _split_workflow_names(pr.workflow_names):
                    if workflow not in workflow_names:
                        workflow_names.append(workflow)
            repositories = sorted({pr.repo_name for pr in campaign_prs})
            # For open campaigns, surface the under_review custom files for this project.
            # (Custom files have no stored campaign_id; under_review implies the active campaign.)
            custom_file_paths: List[str] = []
            if status == "open":
                custom_file_paths = [
                    cf.file_path for cf in db.query(CustomFile).filter(
                        CustomFile.project_id == campaign_project_id,
                        CustomFile.file_status == "under_review",
                    ).order_by(CustomFile.file_path).all()
                ]
            campaign_items = [
                _campaign_pr_response(pr, project_id_map, project.project_id, reusable_workflow_names)
                for pr in sorted(campaign_prs, key=lambda item: item.updated_at or item.created_at or datetime.min, reverse=True)
            ]
            pr_items.extend(campaign_items)

            created_at_dt = campaign_prs[0].created_at
            updated_at_dt = max(
                (pr.updated_at or pr.created_at)
                for pr in campaign_prs
                if pr.updated_at or pr.created_at
            )
            campaign_record = (
                campaign_records.get(key[2])
                if len(key) == 3 and key[1] == "campaign"
                else None
            )
            snapshot = _campaign_snapshot_response(campaign_record)
            campaign_name = _derive_campaign_name(campaign_prs)
            campaign_description = None
            if campaign_record is not None:
                try:
                    record_campaign_status_transition(
                        db, campaign_project_id, project_id_map.get(campaign_project_id, project.project_name),
                        campaign_record, status, open_count, merged_count, closed_count,
                    )
                except Exception as exc:
                    # Never let a notification race (e.g. a concurrent duplicate
                    # dedup_key insert) turn this read endpoint into a 500.
                    print(f"⚠️ Error recording campaign.completed notification: {exc}")
                    db.rollback()
                campaign_id_str = f"campaign-{campaign_record.campaign_id}"
                created_by = campaign_record.created_by or next(
                    (pr.author for pr in campaign_prs if pr.author), github_user
                )
                created_at_dt = campaign_record.created_at or created_at_dt
                campaign_name = campaign_record.campaign_name or campaign_name
                campaign_description = campaign_record.campaign_description
            elif len(key) == 3 and key[1] == "campaign":
                campaign_id_str = f"campaign-{key[2]}"
                created_by = next((pr.author for pr in campaign_prs if pr.author), github_user)
            else:
                # Legacy rows (created before campaign tracking, campaign_id NULL)
                # are isolated under a distinct "legacy-" id so they can never be
                # confused with — or merged into — tracked campaigns.
                campaign_id_str = "legacy-" + "-".join(
                    str(part).replace("/", "-").replace(",", "-") for part in key
                )
                created_by = next((pr.author for pr in campaign_prs if pr.author), github_user)
            campaigns.append(PRCampaignResponse(
                campaign_id=campaign_id_str,
                campaign_name=campaign_name,
                campaign_description=campaign_description,
                campaign_status=status,
                project_name=project_id_map.get(campaign_project_id, project.project_name),
                project_code=project.project_code if campaign_project_id == project.project_id else None,
                created_by=created_by,
                created_at=created_at_dt.isoformat() if created_at_dt else "",
                updated_at=updated_at_dt.isoformat() if updated_at_dt else "",
                completed_at=completed_at,
                target_branches=target_branches,
                branch_option=project_branch_option_map.get(campaign_project_id),
                workflow_names=workflow_names,
                custom_file_paths=custom_file_paths,
                repositories=repositories,
                open_count=open_count,
                merged_count=merged_count,
                closed_count=closed_count,
                failed_count=0,
                completion_percentage=round(((merged_count + closed_count) / total_count) * 100) if total_count else 0,
                pull_requests=campaign_items,
                **snapshot,
            ))

            open_prs += open_count
            merged_prs += merged_count
            closed_prs += closed_count

        campaigns.sort(key=lambda campaign: campaign.updated_at or campaign.created_at, reverse=True)
        pr_items.sort(key=lambda pr: pr.updated_at or pr.created_at, reverse=True)

        # One deferred commit for every campaign.last_known_status update /
        # notification event queued above, instead of one commit per campaign —
        # avoids expire_on_commit re-fetching objects still needed by the
        # response we just built, and keeps this read endpoint resilient to a
        # notification-write failure (falls back to returning what was already
        # computed in memory).
        try:
            db.commit()
        except Exception as exc:
            print(f"⚠️ Error committing campaign notification state: {exc}")
            db.rollback()

        return PRCampaignsResponse(
            campaigns=campaigns,
            pull_requests=pr_items,
            total_campaigns=len(campaigns),
            active_campaigns=sum(1 for campaign in campaigns if campaign.campaign_status == "open"),
            completed_campaigns=sum(1 for campaign in campaigns if campaign.campaign_status != "open"),
            open_prs=open_prs,
            merged_prs=merged_prs,
            closed_prs=closed_prs,
            repositories_affected=len({pr.repo_name for pr in prs}),
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error fetching PR campaigns: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error fetching PR campaigns")


def _verify_pr_webhook_signature(payload: bytes, signature: str) -> bool:
    """
    Verify GitHub webhook signature using HMAC SHA-256.

    Args:
        payload: Raw request body bytes
        signature: X-Hub-Signature-256 header value

    Returns:
        bool: True only if the signature is valid. An unset
        GITHUB_PR_WEBHOOK_SECRET rejects every request rather than accepting
        it, so an exposed endpoint with no secret configured is inert.
    """
    if not GITHUB_PR_WEBHOOK_SECRET:
        print("❌ GITHUB_PR_WEBHOOK_SECRET is not set — rejecting webhook to prevent unauthenticated state changes.")
        return False

    if not signature:
        print("❌ No X-Hub-Signature-256 header in PR webhook request")
        return False

    if not signature.startswith("sha256="):
        print(f"❌ Invalid signature format in PR webhook: {signature}")
        return False

    expected = signature[7:]
    mac = hmac.new(
        GITHUB_PR_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    )
    return hmac.compare_digest(mac.hexdigest(), expected)


WEBHOOK_DOCS_URL = "https://actionsmanager.io/guides/WEBHOOK_ENDPOINT.html"


def _describe_webhook_reachability(app_url: str) -> Optional[str]:
    """Why GitHub probably cannot reach ``app_url``, or None if it looks fine.

    Deliberately a heuristic on the configured URL rather than a probe: we
    cannot ask GitHub to try, and an outbound self-request would succeed from
    inside the network even when the instance is unreachable from outside —
    which is exactly the case this is meant to catch.
    """
    parsed = urlsplit(app_url)
    hostname = (parsed.hostname or "").strip("[]")

    if not hostname:
        return "No APP_URL is configured, so there is no address to give GitHub."

    if _host_is_loopback(hostname):
        return (
            f"APP_URL is {app_url}, which only resolves on the machine running "
            f"ActionsManager. GitHub cannot deliver to it."
        )

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # A hostname — could well be public; DNS resolution here would only
        # tell us what *this* network sees, so treat it as plausible.
        return None

    if ip.is_private or ip.is_link_local or ip.is_reserved:
        return (
            f"APP_URL is {app_url}, a private address. It is reachable on your "
            f"network but not from GitHub."
        )
    return None


def get_webhook_readiness() -> dict:
    """Whether this instance can receive GitHub webhooks, and what is missing.

    Webhooks are optional — delivery and drift detection call out to GitHub and
    need no inbound access — so this reports status rather than failing
    anything. It exists so the UI can explain *why* an event-driven feature is
    unavailable and link to the setup guide, instead of appearing broken.
    """
    app_url = resolve_app_url()
    unreachable_reason = _describe_webhook_reachability(app_url)
    secret_configured = bool(GITHUB_PR_WEBHOOK_SECRET)

    blockers = []
    if unreachable_reason:
        blockers.append(unreachable_reason)
    if not secret_configured:
        blockers.append(
            "GITHUB_PR_WEBHOOK_SECRET is not set. Until it is, every inbound "
            "webhook is rejected — nothing is exposed, the feature is simply off."
        )

    return {
        "ready": not blockers,
        "public_url_configured": unreachable_reason is None,
        "secret_configured": secret_configured,
        "app_url": app_url,
        "webhook_url": f"{app_url}/webhooks/github" if not unreachable_reason else None,
        "blockers": blockers,
        "docs_url": WEBHOOK_DOCS_URL,
    }


@router.get("/api/webhooks/readiness")
def webhook_readiness():
    """Report whether GitHub can deliver webhooks to this instance.

    Unauthenticated on purpose: it returns only this instance's own
    configuration shape — never the secret itself — and the UI needs it to
    explain an unavailable feature before a user has done anything.
    """
    return get_webhook_readiness()


@router.post("/webhooks/github", responses=_responses(400, 401))
async def github_pr_webhook(request: Request, db: Annotated[Session, Depends(get_db)]):
    """
    Handle GitHub pull_request webhook events.

    When a PR that was created by Actions Manager is closed on GitHub directly:
    - If merged: workflow status → synced_with_github, project state → synced
    - If closed without merge: workflow status → committed_locally, project state → draft

    Configure GitHub to send pull_request events to this endpoint.
    Set GITHUB_PR_WEBHOOK_SECRET to the same secret configured in GitHub for signature verification.
    """
    event_type = request.headers.get("X-GitHub-Event")

    # Only process pull_request events
    if event_type != "pull_request":
        return {"status": "ignored", "reason": f"Event type '{event_type}' not handled"}

    body = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    if not _verify_pr_webhook_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {str(e)}")

    action = payload.get("action")

    # We only care about PRs being closed (merged or not)
    if action != "closed":
        return {"status": "ignored", "reason": f"Action '{action}' not handled"}

    pr_data = payload.get("pull_request", {})
    pr_number = pr_data.get("number")
    merged = pr_data.get("merged", False) or bool(pr_data.get("merged_at"))
    repo_full_name = payload.get("repository", {}).get("full_name")

    if not pr_number or not repo_full_name:
        raise HTTPException(status_code=400, detail="Missing pr number or repository in payload")

    print(f"📥 GitHub PR webhook: PR #{pr_number} in {repo_full_name} closed (merged={merged})")

    # Find the PR record in the database
    pr_record = db.query(ProjectPullRequest).filter_by(
        repo_name=repo_full_name,
        pr_number=pr_number,
    ).first()

    if not pr_record:
        print(f"ℹ️ PR #{pr_number} in {repo_full_name} not tracked by Actions Manager, ignoring")
        return {"status": "ignored", "reason": "PR not tracked by Actions Manager"}

    state = "merged" if merged else "closed"
    at = _github_time(pr_data.get("merged_at") if merged else pr_data.get("closed_at"))
    # Everything returned comes from the payload: a merge that finishes a
    # project's deletion takes pr_record with it.
    project_deleted = apply_pr_outcome(db, pr_record, state, at=at)
    return {"status": "processed", "action": state, "pr_number": pr_number, "repo_name": repo_full_name,
            "project_deleted": project_deleted}


def _fetch_branches_page(branches_url: str, headers: dict, params: dict,
                          owner: str, repo: str, page: int,
                          user: str = None, db: Session = None):
    """Fetch a single page of the branches listing. Returns None on API failure."""
    try:
        if user and db:
            return github_get(branches_url, user, db, headers=headers, params=params)
        return requests.get(branches_url, headers=headers, params=params, timeout=GITHUB_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        print(f"⚠️ Failed to fetch branches for {owner}/{repo} (page {page}): {e}")
        return None


def _collect_head_shas(payload: list, head_shas: dict) -> None:
    # The listing already carries each branch's head commit. Capturing
    # it here is free and lets the recency cache invalidate exactly:
    # if the head has not moved, the branch has not moved.
    for b in payload:
        head_shas[b["name"]] = (b.get("commit") or {}).get("sha")


# Reserved cache key for a repo's branch *listing*, as opposed to one branch.
# '*' is not a legal character in a git ref name, so it can never collide with
# a real branch stored in the same table.
BRANCH_LIST_CACHE_KEY = "*"

# ponytail: not a branch-count limit — real repos exit on page 1. This only
# bounds an upstream that ignores `page` and returns the same page forever,
# which would otherwise consume all host memory.
MAX_BRANCH_PAGES = 50


def _cached_branch_listing(db: Session, repo_name: str, owner: str, repo: str,
                           headers: dict, head_shas: Optional[dict]) -> Optional[List[str]]:
    """Branch listing for a repo, conditional on a stored ETag.

    This was the last chargeable call in a warm drift check: the per-branch
    Trees reads answer 304 and the recency answers are cached, but the listing
    itself was fetched in full every time. It supports ETags (verified against
    the live API), so an unchanged repo now costs nothing at all.

    Returns None when the caller should fall back to the normal paginated
    fetch — no cache available, a multi-page repo, or any failure.
    """
    if db is None or not repo_name:
        return None

    row = _get_tree_cache_row(db, repo_name, BRANCH_LIST_CACHE_KEY)
    request_headers = dict(headers)
    if row and row.etag:
        request_headers["If-None-Match"] = row.etag

    try:
        response = requests.get(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/branches",
            headers=request_headers, params={"per_page": 100, "page": 1},
            timeout=GITHUB_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return None

    if response.status_code == 304 and row and row.sha_map_json:
        cached = json.loads(row.sha_map_json)
        if head_shas is not None:
            head_shas.update(cached)
        print(f"🟢 {repo_name} branch listing unchanged (304, no rate-limit cost)")
        return list(cached.keys())

    if response.status_code != 200:
        return None

    payload = response.json()
    if len(payload) >= 100:
        # Paginated: the stored ETag only covers page 1, so caching it would
        # replay an incomplete list. Rare enough to just not cache.
        return None

    listing = {b["name"]: (b.get("commit") or {}).get("sha") for b in payload}
    if head_shas is not None:
        head_shas.update(listing)
    _store_tree_cache(db, repo_name, BRANCH_LIST_CACHE_KEY, listing, response.headers.get("ETag"))
    return list(listing.keys())


def _fetch_all_branches(owner: str, repo: str, headers: dict,
                        user: str = None, db: Session = None,
                        head_shas: Optional[dict] = None,
                        repo_name: str = None) -> Optional[List[str]]:
    """Fetch all branch names for a repository using pagination. Returns None on API failure.

    Pass a dict as ``head_shas`` to also collect {branch: head_commit_sha} from
    the same response — the return type stays a plain name list so existing
    callers and their mocks are unaffected.

    Pass ``repo_name`` to allow a conditional request against a stored ETag,
    which the drift path does; delivery leaves it unset and always fetches.
    """
    if repo_name:
        cached = _cached_branch_listing(db, repo_name, owner, repo, headers, head_shas)
        if cached is not None:
            return cached

    branches_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/branches"
    all_branches: List[str] = []
    page = 1

    while page <= MAX_BRANCH_PAGES:
        params = {"per_page": 100, "page": page}
        response = _fetch_branches_page(branches_url, headers, params, owner, repo, page, user, db)
        if response is None or response.status_code != 200:
            return None

        payload = response.json()
        page_branches = [b["name"] for b in payload]
        if head_shas is not None:
            _collect_head_shas(payload, head_shas)
        all_branches.extend(page_branches)
        if len(page_branches) < 100:
            break
        page += 1
    else:
        print(f"⚠️ {owner}/{repo} branch listing hit the {MAX_BRANCH_PAGES}-page cap; "
              f"using the first {len(all_branches)} branches")

    return all_branches


def _match_branches_by_pattern(all_branches: List[str], regex_pattern: str) -> List[str]:
    """Match branches using a regex pattern, falling back to exact match on invalid regex."""
    try:
        pattern = re.compile(regex_pattern)
        return [b for b in all_branches if pattern.match(b)]
    except re.error as e:
        print(f"⚠️ Invalid regex pattern '{regex_pattern}', falling back to exact branch name match: {str(e)}")
        return [regex_pattern] if regex_pattern in all_branches else []


def _is_branch_recent(owner: str, repo: str, branch_name: str, cutoff_date: datetime,
                      headers: dict, user: str = None, db: Session = None) -> bool:
    """Return True if the branch has had a commit on or after cutoff_date."""
    commits_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/commits"
    params = {"sha": branch_name, "per_page": 1}
    try:
        if user and db:
            response = github_get(commits_url, user, db, headers=headers, params=params)
        else:
            response = requests.get(commits_url, headers=headers, params=params, timeout=GITHUB_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        print(f"⚠️ Failed to fetch commits for branch '{branch_name}' in {owner}/{repo}: {e}")
        return False

    if response.status_code != 200:
        return False
    commits = response.json()
    if not commits:
        return False

    commit_date = datetime.fromisoformat(commits[0]["commit"]["committer"]["date"].replace('Z', '+00:00'))
    if commit_date >= cutoff_date:
        print(f"✅ Branch '{branch_name}' included (last commit: {commit_date.strftime('%Y-%m-%d')})")
        return True
    print(f"⏭️ Branch '{branch_name}' excluded - stale (last commit: {commit_date.strftime('%Y-%m-%d')}, cutoff: {cutoff_date.strftime('%Y-%m-%d')})")
    return False


def _cached_branch_recency(db: Session, repo_name: str, branch: str, head_sha: Optional[str]):
    """The stored "is this branch recent" answer, if it still applies.

    Only reusable while the branch head is unchanged — then the answer provably
    cannot have changed, so this never wrongly skips a branch that just became
    active (which a time-based cache would).
    """
    if db is None or not repo_name or not head_sha:
        return None
    row = _get_tree_cache_row(db, repo_name, branch)
    if row is None or row.branch_is_recent is None:
        return None
    if row.branch_head_sha != head_sha:
        return None
    return row.branch_is_recent


def _store_branch_recency(db: Session, repo_name: str, branch: str,
                          head_sha: Optional[str], is_recent: bool) -> None:
    if db is None or not repo_name or not head_sha:
        return
    repo = db.query(Repo).filter(Repo.repo_name == repo_name).first()
    if repo is None:
        return
    row = _get_tree_cache_row(db, repo_name, branch)
    if row is None:
        row = WorkflowTreeCache(repo_id=repo.repo_id, branch=branch)
        db.add(row)
    row.branch_is_recent = is_recent
    row.branch_head_sha = head_sha
    db.commit()


def _filter_branches_by_recency(owner: str, repo: str, matched_branches: List[str],
                                branch_max_age_days: int, headers: dict,
                                user: str = None, db: Session = None,
                                repo_name: str = None, head_shas: Optional[dict] = None) -> List[str]:
    """Filter branches to those with a recent commit; returns matched_branches unchanged when no age limit.

    ``repo_name``/``head_shas`` enable the recency cache, which the drift path
    uses: this otherwise costs one API call per matched branch on every check.
    Delivery leaves them unset and always asks GitHub, since it decides where
    we actually write.
    """
    if not branch_max_age_days or branch_max_age_days <= 0:
        return matched_branches

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=branch_max_age_days)
    head_shas = head_shas or {}
    recent = []
    for b in matched_branches:
        head_sha = head_shas.get(b)
        cached = _cached_branch_recency(db, repo_name, b, head_sha)
        if cached is not None:
            if cached:
                recent.append(b)
            continue
        is_recent = _is_branch_recent(owner, repo, b, cutoff_date, headers, user, db)
        _store_branch_recency(db, repo_name, b, head_sha, is_recent)
        if is_recent:
            recent.append(b)
    if not recent:
        print(f"⚠️ No branches with commits in last {branch_max_age_days} days, falling back to default branch")
    return recent


def resolve_branch_config_for_repo(db: Session, project: "Project", repo_name: str,
                                   assoc: "ProjectRepo" = None) -> dict:
    """Resolve the effective branch configuration for ``repo_name`` in ``project``.

    Resolution order:
      1. Repository override (when ``project_repos.branch_config_mode == 'override'``)
      2. Project branch configuration
      3. Hard-coded safe defaults (``"default"``, no pattern, 30 days)

    The repository's GitHub default branch is the ultimate fallback used by
    the lower-level ``_resolve_branches_for_repo`` helper when no pattern
    matches; this resolver only decides which option/pattern/max-age to feed
    into that helper for a given (project, repo) pair.

    Pass an already-loaded ``ProjectRepo`` row as ``assoc`` to skip the
    redundant association lookup (useful when iterating over a project's
    repos).

    Returns a dict with:
        ``branch_option``       – ``"default"`` or ``"pattern"``
        ``branch_regex``        – pattern string (may be empty)
        ``branch_max_age_days`` – integer
        ``using_project_default`` – bool, True when no override is in effect
        ``branch_config_mode``  – ``"inherit"`` or ``"override"`` (raw row value)
    """
    proj_option = project.branch_option or "default"
    proj_regex = project.branch_regex or ""
    proj_max_age = project.branch_max_age_days or 30

    # Migrate legacy values inline so callers always see the current set.
    if proj_option == "all":
        proj_option = "default"
    elif proj_option == "regex":
        proj_option = "pattern"

    if assoc is None:
        repo = db.query(Repo).filter(Repo.repo_name == repo_name.strip()).first()
        if repo is None:
            return {
                "branch_option": proj_option,
                "branch_regex": proj_regex,
                "branch_max_age_days": proj_max_age,
                "using_project_default": True,
                "branch_config_mode": "inherit",
            }
        assoc = (
            db.query(ProjectRepo)
            .filter(
                ProjectRepo.project_id == project.project_id,
                ProjectRepo.repo_id == repo.repo_id,
            )
            .first()
        )

    mode = (getattr(assoc, "branch_config_mode", None) or "inherit") if assoc else "inherit"
    if assoc is None or mode != "override":
        return {
            "branch_option": proj_option,
            "branch_regex": proj_regex,
            "branch_max_age_days": proj_max_age,
            "using_project_default": True,
            "branch_config_mode": mode,
        }

    repo_option = assoc.branch_option or proj_option
    if repo_option == "all":
        repo_option = "default"
    elif repo_option == "regex":
        repo_option = "pattern"

    return {
        "branch_option": repo_option,
        "branch_regex": (assoc.branch_regex if assoc.branch_regex is not None else proj_regex) or "",
        "branch_max_age_days": assoc.branch_max_age_days if assoc.branch_max_age_days is not None else proj_max_age,
        "using_project_default": False,
        "branch_config_mode": "override",
    }


def _resolve_branches_for_repo(owner: str, repo: str, branch_option: str,
                               regex_pattern: str, branch_max_age_days: int,
                               headers: dict, user: str = None, db: Session = None,
                               recency_cache_repo: str = None) -> List[str]:
    """
    Resolve which branches to update based on the branch option.

    Args:
        owner: Repository owner
        repo: Repository name
        branch_option: "default" or "pattern"
        regex_pattern: Regex pattern or exact branch name (used when branch_option is "pattern")
        branch_max_age_days: Filter branches by recency (1-30 days)
        headers: GitHub API headers
        user: GitHub username (optional, for API tracking)
        db: Database session (optional, for API tracking)
        recency_cache_repo: "owner/name" to enable the branch-recency cache.
            Drift passes it (the per-branch commit lookup is its dominant cost);
            delivery leaves it unset and always asks GitHub, since it decides
            where we actually write.

    Returns:
        List[str]: List of branch names to update
    """
    if branch_option != "pattern" or not regex_pattern:
        return [get_default_branch(owner, repo, headers, user, db)]

    head_shas: dict = {}
    all_branches = _fetch_all_branches(
        owner, repo, headers, user, db, head_shas=head_shas, repo_name=recency_cache_repo,
    )
    if all_branches is None:
        return [get_default_branch(owner, repo, headers, user, db)]

    matched_branches = _match_branches_by_pattern(all_branches, regex_pattern)
    if not matched_branches:
        return [get_default_branch(owner, repo, headers, user, db)]

    recent_branches = _filter_branches_by_recency(
        owner, repo, matched_branches, branch_max_age_days, headers, user, db,
        repo_name=recency_cache_repo, head_shas=head_shas,
    )
    if not recent_branches:
        return [get_default_branch(owner, repo, headers, user, db)]

    return recent_branches


def _ensure_workflows_directory_exists(owner: str, repo: str, branch: str, 
                                     headers: dict, user: str = None, db: Session = None) -> bool:
    """
    Ensure the .github/workflows directory exists in the repository.
    If it doesn't exist, create it by adding a .gitkeep file.
    
    Returns:
        bool: True if directory exists or was created successfully, False otherwise
    """
    # First, check if the directory already exists by checking for any file in it
    dir_check_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/.github/workflows?ref={branch}"
    
    if user and db:
        check_response = github_get(dir_check_url, user, db, headers=headers)
    else:
        check_response = requests.get(dir_check_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
    
    # If directory exists (200) or is empty (200 with empty array), we're good
    if check_response.status_code == 200:
        print(f"✅ .github/workflows directory already exists in {owner}/{repo} on {branch}")
        return True
    
    # If we get 404, the directory doesn't exist, so create it
    if check_response.status_code == 404:
        print(f"📁 Creating .github/workflows directory in {owner}/{repo} on {branch}")
        
        # Create the directory by creating a .gitkeep file in it
        gitkeep_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/.github/workflows/.gitkeep"
        gitkeep_content = base64.b64encode(b"").decode()  # Empty file
        
        payload = {
            "message": f"Create .github/workflows directory [skip ci]",
            "content": gitkeep_content,
            "branch": branch
        }
        
        if user and db:
            create_response = github_put(gitkeep_url, user, db, json=payload, headers=headers)
        else:
            create_response = requests.put(gitkeep_url, json=payload, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if create_response.status_code in [200, 201]:
            print(f"✅ Successfully created .github/workflows directory in {owner}/{repo} on {branch}")
            return True
        else:
            print(f"❌ Failed to create .github/workflows directory: {create_response.status_code}")
            print(f"   Response: {create_response.text[:500]}")
            return False
    
    # Unexpected status code
    print(f"⚠️ Unexpected response when checking for .github/workflows: {check_response.status_code}")
    return False


def _check_existing_workflow_content(file_url: str, encoded_content: str, headers: dict, user: str = None, db: Session = None) -> tuple:
    """
    Check if existing workflow content matches the new content.
    
    Returns:
        tuple: (sha_or_none, content_unchanged_boolean)
    """
    if user and db:
        response = github_get(file_url, user, db, headers=headers)
    else:
        response = requests.get(file_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
    
    if response.status_code == 200:
        github_data = response.json()
        sha = github_data.get("sha")
        existing_content = github_data.get("content", "")
        
        # Compare the content - if it's the same, skip the update
        # GitHub returns base64 wrapped at 60 chars with a trailing newline,
        # while b64encode produces one unwrapped line — comparing them verbatim
        # was never equal for a file over ~45 bytes, so "unchanged" never fired.
        content_unchanged = (
            "".join(existing_content.split()) == "".join(encoded_content.split())
        )
        return sha, content_unchanged
    
    return None, False  # File doesn't exist, will be created


def _get_authenticated_headers(user: str, headers: dict) -> Optional[dict]:
    """
    Get headers with authentication token for the user.
    
    Args:
        user: GitHub username
        headers: Base headers to copy
    
    Returns:
        dict with authentication or None if user not authenticated
    """
    from auth import user_tokens
    token = user_tokens.get(user)
    if not token:
        return None
    
    headers_with_auth = headers.copy()
    headers_with_auth["Authorization"] = f"token {token}"
    return headers_with_auth


def _get_reusable_workflow_repo(project: Optional["Project"], user: str, db: Optional[Session]) -> str:
    """
    Determine the target repository for reusable workflows.

    For RWX projects the repo is the project's own selected repository (stored
    via the ``ProjectRepo`` association).  When multiple repos are associated,
    the first one is used — RWX projects are expected to have exactly one repo.

    For standard projects with linked reusable workflows the repo is determined
    from the source RWX project's first repository.

    Falls back to ``{user}/am-reuseable-workflow`` only when no repository can
    be determined from the database, preserving backward compatibility.
    """
    if project and db:
        if project.project_type == "rwx":
            project_repos = db.query(Repo).join(ProjectRepo).filter(
                ProjectRepo.project_id == project.project_id
            ).all()
            if project_repos:
                # RWX projects are expected to have one repo; use the first.
                return project_repos[0].repo_name

        # Standard project — look up the linked RWX project's repo
        linked = db.query(LinkedReusableWorkflow).filter(
            LinkedReusableWorkflow.standard_project_id == project.project_id
        ).first()
        if linked:
            rwx_repos = db.query(Repo).join(ProjectRepo).filter(
                ProjectRepo.project_id == linked.rwx_project_id
            ).all()
            if rwx_repos:
                # Use the first repo from the linked RWX project.
                return rwx_repos[0].repo_name

    # Fallback for backward compatibility
    return f"{user}/am-reuseable-workflow"


def _ensure_reusable_repo_exists(owner: str, repo: str, auth_headers: dict,
                                 user: str = None, db: Session = None) -> tuple:
    """
    Check whether a GitHub repository exists and create it if it does not.

    For org-owned repositories the org creation endpoint is used when the
    authenticated user's account type is ``Organization``.  For personal
    accounts (or when the account type cannot be determined) the user endpoint
    ``POST /user/repos`` is used.

    Auto-creation is only attempted when the *owner* matches the authenticated
    *user*.  If the owner is different (e.g. someone else's account) and the
    repo does not exist, a clear error is returned instead of silently creating
    a repo under the wrong account.

    Returns:
        tuple: (True, None) on success, or (False, error_msg) on failure
    """
    repo_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}"
    check_response = requests.get(repo_url, headers=auth_headers, timeout=GITHUB_TIMEOUT_SECONDS)

    if check_response.status_code == 200:
        print(f"✅ Repository '{owner}/{repo}' already exists")
        return True, None

    if check_response.status_code != 404:
        error_msg = f"Failed to check repository '{owner}/{repo}': {check_response.status_code} - {check_response.text[:500]}"
        print(f"❌ {error_msg}")
        return False, error_msg

    # Safety check: only auto-create when the owner matches the authenticated
    # user so we don't accidentally create a repo under the wrong account.
    if user and owner != user:
        error_msg = (
            f"Repository '{owner}/{repo}' does not exist and cannot be auto-created "
            f"because the owner '{owner}' differs from the authenticated user '{user}'"
        )
        print(f"❌ {error_msg}")
        return False, error_msg

    # Repository does not exist — create it.
    # Determine the correct endpoint (user vs org) via get_github_api_endpoints.
    print(f"🔧 Repository '{owner}/{repo}' not found, creating it...")
    endpoints = (get_github_api_endpoints(owner, db) or {}) if db else {}
    create_url = endpoints.get("repos_create", f"{GITHUB_API_URL}/user/repos")
    create_payload = {
        "name": repo,
        "description": "Actions Manager Reusable Workflows repository",
        "private": True,
        "auto_init": False,  # branches are initialized later via the Contents API
    }
    create_response = requests.post(create_url, json=create_payload, headers=auth_headers, timeout=GITHUB_TIMEOUT_SECONDS)

    if create_response.status_code == 201:
        print(f"✅ Created repository '{owner}/{repo}'")
        return True, None

    error_msg = f"Failed to create repository '{owner}/{repo}': {create_response.status_code} - {create_response.text[:500]}"
    print(f"❌ {error_msg}")
    return False, error_msg


def _initialize_am_branch_in_empty_repo(owner: str, repo: str, am_branch_name: str,
                                        target_branch: str, project_code: str, headers: dict,
                                        user: str = None, db: Session = None,
                                        base_shas: Optional[dict] = None) -> tuple:
    """
    Initialize an empty (no commits) repository by creating an initial commit
    via the Contents API, then creating the AM branch.

    The Contents API (PUT /repos/{owner}/{repo}/contents/{path}) is used instead
    of the Git Data API (blobs/trees/commits) because the latter returns
    ``409 Git Repository is empty`` on repositories that have zero commits.
    The Contents API handles this correctly and creates the default branch
    automatically.

    Also ensures the target branch exists so that a pull request from the AM
    branch to the target branch can be opened afterwards.

    If the repository does not exist at all it is created first via the
    GitHub Repos API.

    Pass a dict as ``base_shas`` to also collect {target_branch: base_commit_sha}
    — the commit the AM branch was cut from. The return type stays a 3-tuple so
    existing callers and their mocks are unaffected.

    Returns:
        tuple: (am_branch_name, created_bool, error_or_none)
            - On success: (am_branch_name, True, None) when the AM branch was newly created.
            - On success: (am_branch_name, False, None) when the AM branch already existed (422).
            - On failure: (None, False, error_msg).
    """
    auth_headers = _get_authenticated_headers(user, headers) if (user and db) else headers
    if user and db and not auth_headers:
        return None, False, f"User {user} not authenticated"

    print(f"🔧 Initializing empty repo '{owner}/{repo}' with AM branch '{am_branch_name}'")

    # Step 0: Ensure the repository exists — create it if it doesn't
    repo_ok, repo_error = _ensure_reusable_repo_exists(owner, repo, auth_headers, user=user, db=db)
    if not repo_ok:
        return None, False, repo_error

    # Step 1: Create an initial file via the Contents API.
    # This works on completely empty repos (unlike the Git Data API) and
    # automatically creates the default branch with the first commit.
    readme_content = "# Actions Manager Reusable Workflows\n\nThis repository is managed by Actions Manager.\n"
    contents_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/README.md"
    contents_payload = {
        "message": f"Initialize repository for Actions Manager (project: {project_code})",
        "content": base64.b64encode(readme_content.encode()).decode(),
    }
    contents_response = requests.put(contents_url, json=contents_payload, headers=auth_headers, timeout=GITHUB_TIMEOUT_SECONDS)

    if contents_response.status_code in (200, 201):
        commit_sha = contents_response.json()["commit"]["sha"]
        print(f"✅ Created initial commit in '{owner}/{repo}' via Contents API (SHA: {commit_sha[:8]})")
    elif contents_response.status_code in (409, 422):
        # File already exists (e.g. repo was previously initialized) — fetch
        # the target branch SHA instead.
        print(f"⚠️ README.md already exists in '{owner}/{repo}', fetching target branch SHA...")
        ref_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{target_branch}"
        ref_response = requests.get(ref_url, headers=auth_headers, timeout=GITHUB_TIMEOUT_SECONDS)
        if ref_response.status_code == 200:
            commit_sha = ref_response.json()["object"]["sha"]
            print(f"✅ Got target branch '{target_branch}' SHA: {commit_sha[:8]}")
        else:
            error_msg = (
                f"Failed to initialize repo: README.md already exists but target branch "
                f"'{target_branch}' not found ({ref_response.status_code})"
            )
            print(f"❌ {error_msg}")
            return None, False, error_msg
    else:
        error_msg = f"Failed to initialize repo (contents): {contents_response.status_code} - {contents_response.text[:500]}"
        print(f"❌ {error_msg}")
        return None, False, error_msg

    if base_shas is not None:
        base_shas[target_branch] = commit_sha

    # Step 2: Ensure the target branch exists.
    # The Contents API creates the repo's *default* branch which is usually the
    # target branch, but we create it explicitly in case they differ.
    ref_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs"
    target_ref_payload = {
        "ref": f"refs/heads/{target_branch}",
        "sha": commit_sha,
    }
    target_ref_response = requests.post(ref_url, json=target_ref_payload, headers=auth_headers, timeout=GITHUB_TIMEOUT_SECONDS)
    if target_ref_response.status_code == 201:
        print(f"✅ Created target branch '{target_branch}' in '{owner}/{repo}'")
    elif target_ref_response.status_code == 422:
        print(f"⚠️ Target branch '{target_branch}' already exists in '{owner}/{repo}'")
    else:
        error_msg = f"Failed to create target branch '{target_branch}': {target_ref_response.status_code} - {target_ref_response.text[:200]}"
        print(f"❌ {error_msg}")
        return None, False, error_msg

    # Step 3: Create the AM branch ref pointing to the same commit
    am_ref_payload = {
        "ref": f"refs/heads/{am_branch_name}",
        "sha": commit_sha,
    }
    am_ref_response = requests.post(ref_url, json=am_ref_payload, headers=auth_headers, timeout=GITHUB_TIMEOUT_SECONDS)
    if am_ref_response.status_code == 201:
        print(f"✅ Initialized empty repo '{owner}/{repo}' with AM branch '{am_branch_name}'")
        return am_branch_name, True, None
    elif am_ref_response.status_code == 422:
        print(f"⚠️ AM branch '{am_branch_name}' already exists during initialization")
        return am_branch_name, False, None
    else:
        error_msg = f"Failed to initialize repo (AM ref): {am_ref_response.status_code} - {am_ref_response.text[:200]}"
        print(f"❌ {error_msg}")
        return None, False, error_msg


def _stamp_target_metadata(results: dict, base_commits: dict, protection: dict) -> None:
    """Record each target's base commit SHA and branch protection on its result dict.

    Written after the fact rather than inline because both are known before the
    result dict exists, and errored targets — which take an early continue —
    belong in the campaign snapshot just as much as successful ones.
    """
    for result_key, result in results.items():
        if isinstance(result, dict):
            result["base_sha"] = base_commits.get(result_key)
            result["branch_protection"] = protection.get(result_key)


def _summarize_branch_protection(payload: dict) -> dict:
    """Reduce GitHub's branch protection payload to the fields we snapshot.

    ``enforce_admins`` keeps GitHub's polarity: True means admins are *not*
    exempt from the rules. ``contexts`` is GitHub's deprecated spelling of the
    required checks, so fall back to the newer ``checks`` list.
    """
    checks_block = payload.get("required_status_checks") or {}
    contexts = checks_block.get("contexts") or [
        check.get("context") for check in (checks_block.get("checks") or [])
    ]
    reviews_block = payload.get("required_pull_request_reviews") or {}
    return {
        "status": "protected",
        "required_reviews": reviews_block.get("required_approving_review_count"),
        "required_status_checks": [c for c in contexts if c],
        "enforce_admins": bool((payload.get("enforce_admins") or {}).get("enabled")),
    }


def _fetch_branch_protection(owner: str, repo: str, branch: str, headers: dict,
                             user: str = None, db: Session = None) -> dict:
    """Snapshot a target branch's protection rules.

    "none" (GitHub's 404 — no protection configured) and "unknown" (the token
    cannot read protection settings) are deliberately distinct: an unprotected
    branch and an unreadable one are different facts about the rollout, and
    collapsing them would make the snapshot lie.

    Never raises — a campaign must not fail because protection could not be read.
    """
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/branches/{branch}/protection"
    try:
        if user and db:
            response = github_get(url, user, db, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        else:
            response = requests.get(url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

        if response.status_code == 200:
            return _summarize_branch_protection(response.json())
        if response.status_code == 404:
            return {"status": "none"}
        return {
            "status": "unknown",
            "error": f"{response.status_code} {response.text[:120]}",
        }
    except Exception as exc:
        print(f"⚠️ Could not read branch protection for {owner}/{repo}@{branch}: {exc}")
        return {"status": "unknown", "error": failure_kind(exc, _SHOWN_GITHUB_ERRORS)}


def _create_or_get_am_branch(owner: str, repo: str, target_branch: str,
                            project_code: str, headers: dict,
                            user: str = None, db: Session = None,
                            base_shas: Optional[dict] = None) -> tuple:
    """
    Create a new unique Actions Manager dedicated branch for a project.
    Branch name format: actions-manager/<project_code>/<repo_slug>/<short_id>-<target_branch>

    A new branch is always created — branches are never reused — to prevent
    stale-branch merge conflicts and confusing PR behaviour across repositories.

    Args:
        owner: Repository owner
        repo: Repository name
        target_branch: Target branch to base the dedicated branch on
        project_code: Project code for naming
        headers: GitHub API headers
        user: GitHub username for authenticated API calls
        db: Database session for authenticated API calls
        base_shas: Optional dict to collect {target_branch: base_commit_sha} — the
            commit the AM branch was cut from. The return type stays a 3-tuple so
            existing callers and their mocks are unaffected.

    Returns:
        tuple: (am_branch_name, created_or_existed_boolean, error_message_or_none)
    """
    # Sanitize components for use in a Git branch name
    sanitized_branch = re.sub(r'[^a-zA-Z0-9._-]', '-', target_branch)
    sanitized_repo = re.sub(r'[^a-zA-Z0-9._-]', '-', repo)
    short_id = uuid.uuid4().hex[:8]
    am_branch_name = (
        f"actions-manager/{project_code.lower()}"
        f"/{sanitized_repo}/{short_id}-{sanitized_branch}"
    )

    # Branch doesn't exist yet (always unique), create it based on the target branch
    # Get the target branch's HEAD SHA
    target_ref_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/ref/heads/{target_branch}"
    
    if user and db:
        target_response = github_get(target_ref_url, user, db, headers=headers)
    else:
        target_response = requests.get(target_ref_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
    
    if target_response.status_code != 200:
        if target_response.status_code == 409:
            # 409 = "Git Repository is empty" — no commits at all.
            # Definitely needs to be bootstrapped with an initial commit.
            print(f"⚠️ Repository '{owner}/{repo}' is empty (409), bootstrapping with initial commit...")
            return _initialize_am_branch_in_empty_repo(
                owner, repo, am_branch_name, target_branch, project_code, headers, user, db,
                base_shas=base_shas
            )
        elif target_response.status_code == 404:
            # 404 can mean (a) the repo is empty with no commits, or (b) the
            # repo has commits but the specific target branch doesn't exist.
            # Disambiguate by checking if the repo has any commits.
            commits_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/commits?per_page=1"
            if user and db:
                commits_response = github_get(commits_url, user, db, headers=headers)
            else:
                commits_response = requests.get(commits_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

            if commits_response.status_code == 200:
                try:
                    commits_data = commits_response.json()
                except (ValueError, Exception):
                    commits_data = []
                if commits_data:
                    # Repo is non-empty but the target branch simply doesn't exist —
                    # return an error instead of mutating the repo.
                    error_msg = (
                        f"Target branch '{target_branch}' does not exist in non-empty "
                        f"repository '{owner}/{repo}'. Please create the branch first."
                    )
                    print(f"❌ {error_msg}")
                    return None, False, error_msg
            
            # Repo is empty (no commits) or commits endpoint returned non-200 — bootstrap it.
            print(f"⚠️ Repository '{owner}/{repo}' has no commits, bootstrapping with initial commit...")
            return _initialize_am_branch_in_empty_repo(
                owner, repo, am_branch_name, target_branch, project_code, headers, user, db,
                base_shas=base_shas
            )
        else:
            error_msg = f"Failed to get target branch '{target_branch}': {target_response.status_code}"
            print(f"❌ {error_msg}")
            return None, False, error_msg
    
    target_sha = target_response.json()["object"]["sha"]
    print(f"📌 Target branch '{target_branch}' HEAD SHA: {target_sha}")
    if base_shas is not None:
        base_shas[target_branch] = target_sha
    
    # Create the new Actions Manager branch
    am_branch_ref = f"heads/{am_branch_name}"
    create_ref_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/refs"
    create_payload = {
        "ref": f"refs/{am_branch_ref}",
        "sha": target_sha
    }
    
    if user and db:
        # Use requests.post directly since github_put is for PUT requests
        headers_with_auth = _get_authenticated_headers(user, headers)
        if not headers_with_auth:
            return None, False, f"User {user} not authenticated"
        create_response = requests.post(create_ref_url, json=create_payload, headers=headers_with_auth, timeout=GITHUB_TIMEOUT_SECONDS)
    else:
        create_response = requests.post(create_ref_url, json=create_payload, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
    
    if create_response.status_code == 201:
        print(f"✅ Created Actions Manager branch '{am_branch_name}' in {owner}/{repo}")
        return am_branch_name, True, None
    elif create_response.status_code == 422:
        # 422 can mean "Reference already exists" (rare race condition with unique names)
        # but also covers invalid ref names, permission errors, etc.
        # Only treat it as success when GitHub explicitly says the ref already exists.
        try:
            error_body = create_response.json()
        except Exception:
            error_body = {}
        gh_message = error_body.get("message", "")
        if "already exists" in gh_message.lower():
            print(f"⚠️ Branch '{am_branch_name}' already exists (422) — treating as success")
            return am_branch_name, False, None
        error_msg = f"Failed to create branch (422): {gh_message or create_response.text[:200]}"
        print(f"❌ {error_msg}")
        return None, False, error_msg
    else:
        error_msg = f"Failed to create branch: {create_response.status_code} - {create_response.text[:200]}"
        print(f"❌ {error_msg}")
        return None, False, error_msg


def _check_existing_pr(owner: str, repo: str, am_branch: str, 
                      target_branch: str, headers: dict,
                      user: str = None, db: Session = None) -> Optional[dict]:
    """
    Check if an open PR already exists from the Actions Manager branch to the target branch.
    
    Args:
        owner: Repository owner
        repo: Repository name
        am_branch: Actions Manager dedicated branch name
        target_branch: Target branch name
        headers: GitHub API headers
        user: GitHub username for authenticated API calls
        db: Database session for authenticated API calls
    
    Returns:
        dict or None: PR data if exists, None otherwise
    """
    # GitHub API requires head in format "owner:branch" or just "branch" for same repo
    head_param = f"{owner}:{am_branch}"
    pulls_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls"
    params = {
        "head": head_param,
        "base": target_branch,
        "state": "open"
    }
    
    if user and db:
        # Manually construct URL with params since github_get doesn't support params parameter
        url_with_params = f"{pulls_url}?head={head_param}&base={target_branch}&state=open"
        response = github_get(url_with_params, user, db, headers=headers)
    else:
        response = requests.get(pulls_url, params=params, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
    
    if response.status_code == 200:
        prs = response.json()
        if prs and len(prs) > 0:
            print(f"✅ Found existing PR #{prs[0]['number']} from '{am_branch}' to '{target_branch}'")
            return prs[0]
    
    print(f"📌 No existing PR found from '{am_branch}' to '{target_branch}'")
    return None


def _campaign_view_url(user: Optional[str], project_name: Optional[str]) -> Optional[str]:
    """Link back to the project's PR Campaigns view.

    None when the deployment has not configured an app URL — the frontend then
    auto-detects from the browser, which the backend cannot do on its behalf,
    and a wrong link is worse than no link.
    """
    if not (auth_module.FRONTEND_URL and user and project_name):
        return None
    return f"{auth_module.FRONTEND_URL}/project/{quote(user)}/{quote(project_name)}"


def _delivered_noun(workflows_committed: List[str]) -> str:
    """What a PR says it updates: its workflows, or its files when it carries no workflow."""
    return "workflows" if workflows_committed else "files"


def _pr_body_heading(project_code: str, campaign_meta: Optional[dict], workflows_committed: List[str]) -> List[str]:
    """Open with the campaign's own name when the user gave it one."""
    name = (campaign_meta or {}).get("name")
    if not name:
        noun = _delivered_noun(workflows_committed)
        return [f"This PR updates ActionsManager {noun} for project **{project_code}**."]
    lines = [f"## {name}", ""]
    description = (campaign_meta or {}).get("description")
    if description:
        lines += [description, ""]
    lines.append(f"Delivered by ActionsManager for project **{project_code}**.")
    return lines


def _pr_body_workflow_table(workflows_committed: List[str], policy: dict) -> List[str]:
    """One row per delivered workflow: which version, and the exact content."""
    if not workflows_committed:
        return []
    lines = ["| Workflow | Version | Content sha256 |", "|---|---|---|"]
    for workflow_name in workflows_committed:
        applied = policy.get(workflow_name) or {}
        version = f"v{applied['version']}" if applied.get("version") is not None else "—"
        digest = f"`{applied['sha256'][:16]}`" if applied.get("sha256") else "—"
        lines.append(f"| {workflow_name} | {version} | {digest} |")
    lines.append("")
    return lines


def _pr_body_delivery_lines(target_branch: str, base_sha: Optional[str],
                            custom_files_result: Optional[dict]) -> List[str]:
    """Where this landed, and what else rode along with the workflows."""
    lines = [f"**Target branch:** `{target_branch}`"]
    if base_sha:
        lines.append(f"**Base commit:** `{base_sha[:7]}`")

    files = list((custom_files_result or {}).get("custom_files_committed") or [])
    codeowners = (custom_files_result or {}).get("codeowners_committed")
    if codeowners:
        files.append(codeowners)
    if files:
        lines.append("**Files:** " + ", ".join(f"`{path}`" for path in files))
    return lines


def _campaign_pr_body(db: Session, project_code: str, user: Optional[str],
                      workflows_committed: List[str], target_branch: str,
                      base_sha: Optional[str], custom_files_result: Optional[dict],
                      campaign_meta: Optional[dict] = None) -> str:
    """Describe what this PR is delivering, in the PR itself.

    The delivered workflow list alone does not say *which* version of each
    workflow went out, so a reviewer looking at the PR months later cannot tell
    what it was supposed to contain. The version and content hash recorded here
    are the same ones the campaign snapshot pins.

    ``campaign_meta`` carries the name and description the user gave the campaign.
    Unlike the campaign id, those come off the request and so are known before
    the campaign row exists — they lead the body when present.
    """
    meta = campaign_meta or {}
    project_name = meta.get("project_name")
    policy = meta.get("policy")
    if project_name is None or policy is None:
        # Direct callers (and tests) that have no precomputed campaign context.
        project = db.query(Project).filter(Project.project_code == project_code).first() if db else None
        project_name = project_name or (project.project_name if project else None)
        if policy is None:
            policy = (
                _build_policy_version(db, project.project_id, workflows_committed)
                if (db and project and workflows_committed) else {}
            )

    lines = _pr_body_heading(project_code, campaign_meta, workflows_committed)
    lines.append("")
    lines += _pr_body_workflow_table(workflows_committed, policy)
    lines += _pr_body_delivery_lines(target_branch, base_sha, custom_files_result)

    campaign_url = _campaign_view_url(user, project_name)
    if campaign_url:
        lines += ["", f"**Campaign:** [PR Campaigns — {project_name}]({campaign_url})"]

    lines += ["", "---", "*This PR was automatically created by ActionsManager.*"]
    return "\n".join(lines) + "\n"


def _github_reason(response) -> str:
    """GitHub's own explanation for a failed call, trimmed for display.

    The useful part is ``message`` plus the ``errors`` list — "No commits between
    main and actions-manager/..." only ever appears there. Callers that report a
    bare status code leave the user with nothing to act on.
    """
    fallback = (response.text or "")[:200]
    try:
        payload = response.json()
    except Exception:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    parts = [payload.get("message") or ""]
    for err in payload.get("errors") or []:
        detail = err.get("message") if isinstance(err, dict) else str(err)
        if detail:
            parts.append(str(detail))
    return " — ".join(p for p in parts if p)[:300] or fallback


def _create_pull_request(owner: str, repo: str, am_branch: str,
                        target_branch: str, project_code: str,
                        workflows_committed: List[str], headers: dict,
                        user: str = None, db: Session = None,
                        body: Optional[str] = None,
                        title: Optional[str] = None) -> tuple:
    """
    Create a new Pull Request from the Actions Manager branch to the target branch.
    
    Args:
        owner: Repository owner
        repo: Repository name
        am_branch: Actions Manager dedicated branch name
        target_branch: Target branch name
        project_code: Project code for PR title
        workflows_committed: List of workflow names committed
        headers: GitHub API headers
        user: GitHub username for authenticated API calls
        db: Database session for authenticated API calls
        body: Prebuilt PR body. Falls back to the plain workflow list when the
            caller has no campaign context to describe.
        title: Prebuilt PR title, for deliveries that are not a workflow update.

    Returns:
        tuple: (pr_data, reason) — pr_data is None on failure and ``reason`` carries
        GitHub's own explanation. "No commits between main and actions-manager/..."
        is the common one and the caller has nothing to show the user without it.
    """
    pr_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls"

    workflows_list = "\n".join([f"- {wf}" for wf in workflows_committed])
    pr_body = body or f"""This PR updates Actions Manager workflows for project **{project_code}**.

## Workflows Updated
{workflows_list}

---
*This PR was automatically created by Actions Manager.*
"""

    pr_payload = {
        "title": title or f"[Actions Manager] Update {project_code} {_delivered_noun(workflows_committed)}",
        "body": pr_body,
        "head": am_branch,
        "base": target_branch
    }
    
    if user and db:
        # Use requests.post directly since github_put is for PUT requests
        headers_with_auth = _get_authenticated_headers(user, headers)
        if not headers_with_auth:
            print(f"❌ User {user} not authenticated")
            return None, f"GitHub user '{user}' is not authenticated"
        response = requests.post(pr_url, json=pr_payload, headers=headers_with_auth, timeout=GITHUB_TIMEOUT_SECONDS)
    else:
        response = requests.post(pr_url, json=pr_payload, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

    if response.status_code == 201:
        pr_data = response.json()
        print(f"✅ Created PR #{pr_data['number']}: {pr_data['html_url']}")
        return pr_data, None

    reason = _github_reason(response)
    print(f"❌ Failed to create PR: {response.status_code} - {reason}")
    return None, f"HTTP {response.status_code}: {reason}"


def _update_workflow_to_github(owner: str, repo: str, workflow: dict, 
                              project_code: str, am_branch: str, 
                              headers: dict, repo_name: str, user: str = None, db: Session = None, use_prefix: bool = True) -> tuple:
    """
    Update a single workflow to GitHub on the Actions Manager dedicated branch.
    Only updates if content has actually changed to prevent blank commits.
    
    Args:
        owner: Repository owner
        repo: Repository name
        workflow: Workflow dict with 'name' and 'content' keys
        project_code: Project code for file naming
        am_branch: Actions Manager dedicated branch name (e.g., 'actions-manager/myapp-main')
        headers: GitHub API headers
        repo_name: Full repository name for logging
        user: GitHub username for authenticated API calls
        db: Database session for authenticated API calls
        use_prefix: Whether to use the AM_{PROJECT_CODE}_ prefix
    
    Returns:
        tuple: (status_code, new_sha_or_none)
    """
    # Validate workflow name is not empty
    workflow_name = workflow.get('name', '').strip()
    if not workflow_name:
        error_msg = f"❌ Workflow name cannot be empty for {repo_name}"
        print(error_msg)
        return 400, None  # Bad Request
    
    # Remove .yml/.yaml extension from workflow name if present to avoid double extensions
    workflow_base_name = workflow_name
    if workflow_base_name.endswith('.yml'):
        workflow_base_name = workflow_base_name[:-4]
    elif workflow_base_name.endswith('.yaml'):
        workflow_base_name = workflow_base_name[:-5]
    
    formatted_name = format_workflow_name(workflow_base_name, project_code, use_prefix)
    encoded_content = base64.b64encode(workflow["content"].encode()).decode()
    path = f".github/workflows/{formatted_name}"
    
    print(f"🔍 _update_workflow_to_github:")
    print(f"   Owner: {owner}, Repo: {repo}")
    print(f"   Workflow name: {workflow.get('name')}")
    print(f"   Formatted name: {formatted_name}")
    print(f"   Path: {path}")
    print(f"   AM Branch: {am_branch}")
    print(f"   User for API calls: {user}")
    
    # Check existing content on the AM branch
    file_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}?ref={am_branch}"
    print(f"🔍 Checking file at: {file_url}")
    
    sha, content_unchanged = _check_existing_workflow_content(file_url, encoded_content, headers, user, db)
    print(f"🔍 File check result - SHA: {sha}, Content unchanged: {content_unchanged}")
    
    if content_unchanged:
        print(f"📌 Workflow content unchanged for {formatted_name} in {repo_name} on {am_branch}, skipping update")
        return 204, sha  # No Content (unchanged)

    # If file doesn't exist (sha is None), ensure .github/workflows directory exists
    if sha is None:
        # Verify the AM branch exists
        branch_check_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/branches/{am_branch}"
        print(f"🔍 Verifying branch exists: {branch_check_url}")
        if user and db:
            branch_response = github_get(branch_check_url, user, db, headers=headers)
        else:
            branch_response = requests.get(branch_check_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if branch_response.status_code == 404:
            print(f"❌ Branch '{am_branch}' does not exist in {owner}/{repo}")
            print(f"   Cannot create workflow file on non-existent branch")
            return 404, None
        elif branch_response.status_code != 200:
            print(f"⚠️ Unexpected status checking branch: {branch_response.status_code}")
            print(f"   Response: {branch_response.text[:500]}")
        else:
            print(f"✅ Branch '{am_branch}' exists in {owner}/{repo}")
        
        if not _ensure_workflows_directory_exists(owner, repo, am_branch, headers, user, db):
            # Failed to create directory, mark as error
            print(f"❌ Failed to ensure .github/workflows directory exists in {repo_name} on {am_branch}")
            return 404, None

    # Content has changed or file doesn't exist, proceed with update
    payload = {
        "message": f"Updating {formatted_name} in {repo_name} for {project_code} [skip ci]",
        "content": encoded_content,
        "branch": am_branch,
    }
    if sha:
        payload["sha"] = sha

    # PUT request should NOT include ?ref={branch} - branch is specified in the payload body
    put_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    print(f"🔍 Sending PUT request to: {put_url}")
    print(f"🔍 Payload has SHA: {sha is not None}")
    print(f"🔍 Payload message: {payload['message']}")
    print(f"🔍 Payload branch: {payload['branch']}")
    
    try:
        if user and db:
            print(f"🔍 Using github_put with user: {user}")
            put_response = github_put(put_url, user, db, json=payload, headers=headers)
        else:
            print(f"🔍 Using requests.put directly")
            put_response = requests.put(put_url, json=payload, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
            
        print(f"🔍 PUT response status: {put_response.status_code}")
        if put_response.status_code not in [200, 201]:
            print(f"❌ PUT failed. Response: {put_response.text[:500]}")
            # Try to parse and provide more helpful error message
            try:
                error_data = put_response.json()
                error_msg = error_data.get('message', 'Unknown error')
                print(f"   GitHub API Error: {error_msg}")
                if put_response.status_code == 404:
                    print(f"   Possible causes:")
                    print(f"   - Repository '{owner}/{repo}' doesn't exist or isn't accessible")
                    print(f"   - Branch '{am_branch}' doesn't exist")
                    print(f"   - OAuth app lacks 'contents: write' permission")
                    print(f"   - Token is invalid or expired")
            except Exception:
                # If response parsing fails, continue with existing error message
                pass
            return put_response.status_code, None
    except Exception as put_error:
        print(f"❌ Exception during PUT request: {str(put_error)}")
        import traceback
        traceback.print_exc()
        raise
    
    # Extract the new SHA from the successful response
    new_sha = None
    if put_response.status_code in [200, 201]:
        response_data = put_response.json()
        new_sha = response_data.get("content", {}).get("sha")
        print(f"✅ Successfully created/updated workflow. New SHA: {new_sha}")
    
    return put_response.status_code, new_sha


def _update_workflow_git_hash(db: Session, workflow_name: str, new_sha: str,
                              project_code: str, is_reusable: bool = False) -> None:
    """Update the workflow's git hash in the database, scoped to one project.

    Workflow names are deliberately not globally unique — two projects may each
    own a workflow called "ci". This previously did an unscoped
    ``filter_by(workflow_name=...).first()``, so committing project A's workflow
    could stamp A's GitHub blob SHA onto project B's row, corrupting B's drift
    baseline and making real drift read as "synchronized".

    Note the name arrives unprefixed in both naming modes: ``use_prefix`` is
    applied only when building the GitHub path, so prefixing does not protect
    against this collision.

    pending_delete rows are excluded because only the upsert path reaches here
    — ``_commit_workflows_to_branch`` sends tombstones to
    ``_remove_on_branch`` and continues — so a tombstone is never the
    row this SHA belongs to. Without the filter, a tombstone sharing a live
    workflow's name can win ``.first()``, and the live row's baseline then never
    updates: real drift on it reads as synchronized.
    """
    if not new_sha:
        return

    db_workflow = (
        db.query(Workflow)
        .join(ProjectWorkflow, ProjectWorkflow.workflow_id == Workflow.workflow_id)
        .join(Project, Project.project_id == ProjectWorkflow.project_id)
        .filter(
            Project.project_code == project_code,
            Workflow.workflow_name == workflow_name,
            Workflow.reusable_workflow == is_reusable,
            Workflow.pending_delete.is_(False),
        )
        .first()
    )
    if db_workflow:
        db_workflow.workflow_git_hash = new_sha
        db.commit()
        print(f"✅ Updated git hash for workflow '{workflow_name}' in {project_code}: {new_sha}")


def _db_update_custom_file_sha(db: Session, file_id: Optional[int], new_sha: Optional[str]) -> None:
    """Persist the blob SHA returned by GitHub after a successful custom file write.

    ``file_id`` is None when the writer is not a CustomFile at all — the campaign
    rollback path reuses this committer for plain paths — and there is nothing to
    stamp in that case.
    """
    if not new_sha or file_id is None:
        return
    cf = db.query(CustomFile).filter_by(id=file_id).first()
    if cf:
        cf.git_hash = new_sha
        db.commit()


def _resolve_effective_target_branches(repo_name: str, owner: str, repo: str, project: "Project", db: Session,
                                        branch_option: str, regex_pattern: str, branch_max_age_days: int,
                                        headers: dict, user: str) -> tuple:
    """Resolve target branches for a repo, honoring a project's per-repo branch override when given.

    Returns (target_branches, error_result) — error_result is non-None (and target_branches is
    None) when branch resolution fails; the caller should return error_result immediately.
    """
    if project is not None:
        cfg = resolve_branch_config_for_repo(db, project, repo_name)
        eff_option = cfg["branch_option"]
        eff_regex = cfg["branch_regex"]
        eff_max_age = cfg["branch_max_age_days"]
        if not cfg["using_project_default"]:
            print(
                f"   🔧 Using repo override for {repo_name}: "
                f"option={eff_option}, regex={eff_regex!r}, max_age={eff_max_age}"
            )
    else:
        eff_option, eff_regex, eff_max_age = branch_option, regex_pattern, branch_max_age_days

    try:
        target_branches = _resolve_branches_for_repo(
            owner, repo, eff_option, eff_regex, eff_max_age, headers, user, db
        )
        print(f"🔍 Resolved target branches for {repo_name}: {target_branches}")
        return target_branches, None
    except ValueError as e:
        print(f"❌ Error resolving branches: {str(e)}")
        return None, {"error": str(e), "status": 400}
    except Exception as e:
        print(f"❌ Unexpected error resolving branches: {str(e)}")
        return None, {"error": f"Failed to resolve branches: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}", "status": 500}


class _DeliveryTarget(NamedTuple):
    """One (repo, branch) a delivery reaches."""
    repo_name: str
    owner: str
    repo: str
    branch: str


def _target_on(repo_name: str, branch: str) -> _DeliveryTarget:
    owner, repo = repo_name.split("/", 1)
    return _DeliveryTarget(repo_name, owner, repo, branch)


class _Removal(NamedTuple):
    """One file to take off a (repo, branch): at *path*, holding the blob *sha* when the record knows it."""
    kind: str
    file_id: int
    name: str
    path: str
    sha: Optional[str] = None


# What GitHub answers a delete whose SHA is not the file's: 409 for another
# blob, 422 for one it cannot use.
_STALE_SHA = (409, 422)


def _send_delete(url: str, path: str, branch: str, sha: Optional[str], project_code: str,
                 headers: dict):
    return requests.delete(
        url,
        headers=headers,
        json={
            "message": f"Delete {path} via ActionsManager [{project_code}] [skip ci]",
            "sha": sha,
            "branch": branch,
        },
        timeout=GITHUB_TIMEOUT_SECONDS,
    )


def _delete_outcome(response, path: str, branch: str) -> tuple:
    if response.status_code in (200, 201):
        print(f"✅ Deleted '{path}' from {branch}")
        return DELETE_DELETED, None
    if response.status_code == 404:
        print(f"📌 '{path}' already absent from {branch}")
        return DELETE_ABSENT, None
    print(f"❌ Failed to delete '{path}' from {branch}: {response.status_code}")
    return DELETE_FAILED, f"{path}: HTTP {response.status_code} - {_github_reason(response)}"


def _delete_path_from_branch(owner: str, repo: str, path: str, branch: str, project_code: str,
                             user: str, db: Session, headers: dict, known_sha: Optional[str] = None) -> tuple:
    """Delete *path* from *branch*. Returns (outcome, error_message).

    ``outcome`` is one of DELETE_DELETED / DELETE_ABSENT / DELETE_FAILED. Absent is
    not an error, but it is not a commit either — callers that go on to open a PR
    must not count it, or they open one against a branch with no diff.

    *known_sha* is the blob the delivery record holds there. The delete is
    sent with it straight away, one call where the record is right; only when
    GitHub refuses it as stale is the file read for its own SHA.
    """
    # Encoded at the URL, plain in messages: the stored path must not be able to
    # reshape the request (a "?" or "#" in it would otherwise start a query or
    # fragment, and "%xx" would be decoded by GitHub).
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}"
    if known_sha:
        response = _send_delete(url, path, branch, known_sha, project_code, headers)
        if response.status_code not in _STALE_SHA:
            return _delete_outcome(response, path, branch)
    sha_resp = github_get(url, user, db, headers=headers, params={"ref": branch})
    if sha_resp.status_code == 404:
        print(f"📌 '{path}' already absent from {branch}")
        return DELETE_ABSENT, None
    if sha_resp.status_code != 200:
        return DELETE_FAILED, f"{path}: HTTP {sha_resp.status_code} fetching SHA"
    response = _send_delete(url, path, branch, sha_resp.json().get("sha"), project_code, headers)
    return _delete_outcome(response, path, branch)


def _record_removal(name: str, result: tuple, committed: list, errors: list) -> str:
    """File a removal's outcome into the campaign's committed/error lists.

    Absent is neither a commit nor a failure — see _commit_custom_files_to_branch
    for why counting it as a commit produces a pull request against a branch with
    no diff.
    """
    outcome, error = result
    if outcome == DELETE_DELETED:
        committed.append(name)
    elif outcome == DELETE_FAILED:
        errors.append(error)
    return outcome


def _remove_on_branch(removal: _Removal, owner: str, repo: str, project_code: str, am_branch: str,
                      headers: dict, user: str, db: Session, committed: list, errors: list) -> str:
    """Take one file off the AM branch, filing and returning the outcome.

    Wrapped like every other iteration of a commit loop: an unwrapped timeout
    here would propagate out of the campaign and abandon the AM branches
    already cut for earlier repos, instead of failing this one target.
    """
    try:
        result = _delete_path_from_branch(owner, repo, removal.path, am_branch, project_code,
                                          user, db, headers, known_sha=removal.sha)
    except Exception as e:
        errors.append(f"{removal.name}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")
        print(f"❌ Exception removing '{removal.path}': {type(e).__name__}")
        return DELETE_FAILED
    return _record_removal(removal.name, result, committed, errors)


def _remove_superseded_copy(entry: dict, target: Optional[tuple], branch: str, written: frozenset,
                            owner: str, repo: str, project_code: str, user: str, db: Session,
                            headers: dict, errors: list) -> Optional[str]:
    """Take off *branch* the copy of *entry*'s file that writing it at ``entry["path"]`` left behind.

    That copy is where the delivery record says the file was delivered on
    *target*, the (repo, target branch) this write delivers to; None means
    there is no record to supersede. Git has no rename: a rename or a move is
    a remove plus an add, so both halves land on the same branch, the pull
    request reads as a rename, and merging it finishes the job. Called only
    after the write succeeded: removing first would leave the file absent if
    the write then failed.

    Never a path the run writes on this target (*written*, lowercased): two
    files swapping names each sit where the other was delivered.

    Returns the outcome, or None when there was nothing to remove. A failure
    is added to *errors*. Absent is not an error: the copy may have been
    removed by hand, or by an earlier campaign that was closed after its
    removal landed.
    """
    if target is None:
        return None
    old_path = deliveries.superseded_path(db, entry["kind"], entry["id"], *target, entry["path"])
    if old_path is None or old_path.lower() in written:
        return None
    try:
        outcome, error = _delete_path_from_branch(owner, repo, old_path, branch, project_code,
                                                  user, db, headers)
    except Exception as e:
        errors.append(f"{old_path}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")
        print(f"❌ Exception removing the superseded copy '{old_path}': {type(e).__name__}")
        return DELETE_FAILED
    if outcome == DELETE_FAILED:
        errors.append(error)
    elif outcome == DELETE_DELETED:
        print(f"✅ Removed '{old_path}' from {branch}: '{entry['path']}' supersedes it")
    return outcome


def _paths_written(workflows: List[dict], custom_files: List[dict], codeowners: Optional[dict],
                   project_code: str, use_prefix: bool) -> frozenset:
    """Every path a campaign writes on each of its targets, lowercased (see _remove_superseded_copy)."""
    paths = [_workflow_entry(w, project_code, use_prefix)["path"] for w in workflows if not w.get("pending_delete")]
    paths += [cf.get("file_path", "") for cf in custom_files if not cf.get("pending_delete")]
    if codeowners:
        paths.append(codeowners["file_path"])
    return frozenset(path.lower() for path in paths)


def _carried_entry(kind: str, file_id, path: str, sha=None, delete: bool = False) -> dict:
    """What one commit put on an AM branch, in the shape ``deliveries.record_pending`` takes."""
    return {"kind": kind, "id": file_id, "path": path, "sha": sha, "delete": delete}


def _workflow_entry(workflow: dict, project_code: str, use_prefix: bool, sha=None, delete: bool = False) -> dict:
    path = _WORKFLOWS_DIR + format_workflow_name(workflow.get('name', ''), project_code, use_prefix)
    return _carried_entry(deliveries.WORKFLOW, workflow.get("id"), path, sha, delete)


def _note_carried(carried: Optional[list], entry: dict) -> None:
    if carried is not None:
        carried.append(entry)


def _note_removal(carried: Optional[list], entry: dict, outcome: str) -> None:
    """A deletion the branch now carries, or one it had nothing to do for."""
    if outcome == DELETE_DELETED:
        _note_carried(carried, entry)
    elif outcome == DELETE_ABSENT:
        _note_carried(carried, {**entry, "absent": True})


def _record_absent_removals(db: Session, carried: list, repo_name: str, target_branch: str) -> list:
    """Record removals the AM branch had nothing to do for, and return the rest.

    The AM branch is cut fresh from the target branch, so a file it lacks is
    not on the target either.
    """
    for entry in carried:
        if entry.get("absent"):
            deliveries.record_removed(db, entry["kind"], entry["id"], repo_name, target_branch)
    db.commit()
    return [entry for entry in carried if not entry.get("absent")]


def _commit_workflows_to_branch(workflows: List[dict], owner: str, repo: str, project_code: str, am_branch: str,
                                 headers: dict, repo_name: str, user: str, db: Session, use_prefix: bool,
                                 carried: Optional[list] = None, target: Optional[tuple] = None,
                                 written: frozenset = frozenset()) -> tuple:
    """Commit each workflow to the AM branch. Returns (workflows_committed, workflow_errors).

    ``carried`` collects what each commit put on the branch, for the PR's
    delivery record. ``target`` is the (repo, target branch) the AM branch
    delivers to and ``written`` every path the run writes there: each write
    removes the delivered copy it supersedes (_remove_superseded_copy).
    """
    workflows_committed = []
    workflow_errors = []
    for workflow in workflows:
        workflow_name = workflow.get('name', 'unknown')
        if workflow.get("pending_delete"):
            entry = _workflow_entry(workflow, project_code, use_prefix, delete=True)
            outcome = _remove_on_branch(
                _Removal(entry["kind"], entry["id"], workflow_name, entry["path"]), owner, repo, project_code,
                am_branch, headers, user, db, workflows_committed, workflow_errors,
            )
            _note_removal(carried, entry, outcome)
            continue
        print(f"\n🔍 Committing workflow '{workflow_name}' to {am_branch}")
        try:
            status_code, new_sha = _update_workflow_to_github(
                owner, repo, workflow, project_code, am_branch,
                headers, repo_name, user, db, use_prefix
            )
            if status_code not in (200, 201, 204):
                workflow_errors.append(f"{workflow_name}: HTTP {status_code}")
                print(f"❌ Failed to commit workflow '{workflow_name}': {status_code}")
                continue
            workflows_committed.append(workflow_name)
            # 204 returns the blob already on the branch.
            entry = _workflow_entry(workflow, project_code, use_prefix, new_sha)
            _note_carried(carried, entry)
            print(f"✅ Committed workflow '{workflow_name}' (status: {status_code})")
            if status_code != 204:
                # An owned reusable workflow travels this path too (#2084), and a
                # project may hold a regular and a reusable row of the same name.
                _update_workflow_git_hash(db, workflow_name, new_sha, project_code,
                                          is_reusable=bool(workflow.get("is_reusable")))
            # After a 204 too: the new path already on this branch still leaves
            # the old one there when a previous campaign was closed after the
            # add landed but before the remove did.
            _remove_superseded_copy(entry, target, am_branch, written, owner, repo, project_code,
                                    user, db, headers, workflow_errors)
        except Exception as e:
            workflow_errors.append(f"{workflow_name}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")
            print(f"❌ Exception committing workflow '{workflow_name}': {str(e)}")
            import traceback
            traceback.print_exc()
    return workflows_committed, workflow_errors


def _commit_reusable_workflows_to_branch(rxworkflows: List[dict], owner: str, repo: str, project_code: str,
                                          am_branch: str, headers: dict, reusable_repo: str, user: str,
                                          db: Session, use_prefix: bool, carried: Optional[list] = None,
                                          target: Optional[tuple] = None,
                                          written: frozenset = frozenset()) -> tuple:
    """Commit each reusable workflow to the AM branch. Returns (workflows_committed, workflow_errors).

    ``carried``, ``target`` and ``written`` are as for _commit_workflows_to_branch.
    """
    workflows_committed = []
    workflow_errors = []
    for workflow in rxworkflows:
        workflow_name = workflow.get('name', '').strip()
        if not workflow_name:
            print("❌ Reusable workflow name cannot be empty")
            workflow_errors.append("<empty-name>: Empty workflow name")
            continue

        print(f"\n🔍 Committing reusable workflow '{workflow_name}' to {am_branch}")
        try:
            status_code, new_sha = _update_workflow_to_github(
                owner, repo, workflow, project_code, am_branch,
                headers, reusable_repo, user, db, use_prefix
            )
            if status_code not in (200, 201, 204):
                workflow_errors.append(f"{workflow_name}: HTTP {status_code}")
                print(f"❌ Failed to commit reusable workflow '{workflow_name}': {status_code}")
                continue
            workflows_committed.append(workflow_name)
            entry = _workflow_entry(workflow, project_code, use_prefix, new_sha)
            _note_carried(carried, entry)
            print(f"✅ Committed reusable workflow '{workflow_name}' (status: {status_code})")
            if status_code != 204:
                _update_workflow_git_hash(db, workflow_name, new_sha, project_code, is_reusable=True)
            _remove_superseded_copy(entry, target, am_branch, written, owner, repo, project_code,
                                    user, db, headers, workflow_errors)
        except Exception as e:
            workflow_errors.append(f"{workflow_name}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")
            print(f"❌ Exception committing reusable workflow '{workflow_name}': {str(e)}")
            import traceback
            traceback.print_exc()
    return workflows_committed, workflow_errors


def _upsert_custom_file_to_am_branch(owner: str, repo: str, cf: dict, am_branch: str, project_code: str,
                                      user: str, db: Session, headers: dict,
                                      carried: Optional[list] = None, target: Optional[tuple] = None,
                                      written: frozenset = frozenset()) -> tuple:
    """Returns (success, error_message). A commit is added to ``carried``.

    A commit removes the delivered copy it supersedes, as for a workflow
    (``target`` and ``written`` are _commit_workflows_to_branch's). Its
    failure is the error of a write that succeeded.

    A synced file (one a campaign carries only to put it back where it
    drifted, or to a repository it never reached) is not written where the
    branch already holds it, as for CODEOWNERS: that target gets no pull request.
    """
    cf_path = cf.get("file_path", "")
    cf_url_path = quote(cf_path, safe="/")
    sha_resp = github_get(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{cf_url_path}",
        user, db,
        headers=headers,
        params={"ref": am_branch},
    )
    existing_sha = sha_resp.json().get("sha") if sha_resp.status_code == 200 else None
    if cf.get("synced") and existing_sha == deliveries.blob_sha(cf["file_content"]):
        print(f"📌 Custom file '{cf_path}' already on {am_branch}")
        return False, None
    put_body: dict = {
        "message": f"Update {cf_path} via ActionsManager [{project_code}] [skip ci]",
        "content": base64.b64encode(cf["file_content"].encode()).decode(),
        "branch": am_branch,
    }
    if existing_sha:
        put_body["sha"] = existing_sha
    put_resp = requests.put(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{cf_url_path}",
        headers=headers,
        json=put_body,
        timeout=GITHUB_TIMEOUT_SECONDS,
    )
    if put_resp.status_code in (200, 201):
        new_sha = put_resp.json().get("content", {}).get("sha")
        _db_update_custom_file_sha(db, cf["id"], new_sha)
        entry = _carried_entry(deliveries.CUSTOM_FILE, cf.get("id"), cf_path, new_sha)
        _note_carried(carried, entry)
        print(f"✅ Committed custom file '{cf_path}' (SHA: {new_sha})")
        errors: list = []
        _remove_superseded_copy(entry, target, am_branch, written, owner, repo, project_code,
                                user, db, headers, errors)
        return True, (errors[0] if errors else None)
    print(f"❌ Failed to commit custom file '{cf_path}': {put_resp.status_code}")
    return False, f"{cf_path}: HTTP {put_resp.status_code}"


def _unresolved(repo_name: str, error: str) -> dict:
    """A repository whose branches could not be resolved, shaped like the per-target results callers return."""
    return {"repo": repo_name, "branch": None, "status": DELETE_FAILED, "error": error}


def _project_delivery_targets(db: Session, project: "Project", user: str, headers: dict,
                              only: Optional[Callable[[str], bool]] = None) -> tuple:
    """Every ``_DeliveryTarget`` ``project`` delivers to, in the repositories *only* accepts when given.

    Branches come from ``_resolve_effective_target_branches`` — the same path a PR
    Campaign uses — so a project delivering to ``release/*`` is never silently
    resolved against the repository's GitHub default branch.

    Returns (targets, failures); ``failures`` are repos whose branches could not be
    resolved, already shaped like the per-target results the caller returns.
    """
    repo_names = [
        r.repo_name
        for r in db.query(Repo)
        .join(ProjectRepo, ProjectRepo.repo_id == Repo.repo_id)
        .filter(ProjectRepo.project_id == project.project_id)
        .all()
        if only is None or only(r.repo_name)
    ]

    targets, failures = [], []
    for repo_name in repo_names:
        if "/" not in (repo_name or ""):
            failures.append(_unresolved(repo_name, f"Invalid repository name '{repo_name}'"))
            continue
        owner, repo = repo_name.split("/", 1)
        branches, branch_error = _resolve_effective_target_branches(
            repo_name, owner, repo, project, db,
            project.branch_option or "default",
            project.branch_regex or "",
            project.branch_max_age_days or 30,
            headers, user,
        )
        if branch_error:
            failures.append(_unresolved(repo_name, branch_error.get("error")))
            continue
        targets.extend(_DeliveryTarget(repo_name, owner, repo, branch) for branch in branches)
    return targets, failures


def _reusable_delivery_target(reusable_repo: str, user: str, headers: dict, db: Session) -> _DeliveryTarget:
    """Where reusable workflows are delivered: *reusable_repo*'s default branch.

    Reusable workflows are centralized, not per-branch.
    """
    owner, repo = reusable_repo.split("/", 1)
    return _DeliveryTarget(reusable_repo, owner, repo, get_default_branch(owner, repo, headers, user, db))


def _removable_repos(db: Session, project: "Project", repo_names: Optional[List[str]]) -> Callable[[str], bool]:
    """Whether a removal may reach a repository: one of the project's own, and of *repo_names* when given.

    Ownership is _project_owns_repo's, the check every deletion makes; the
    delivery record also remembers repositories since removed from the project.
    """
    @functools.lru_cache(maxsize=None)
    def removable(repo_name: str) -> bool:
        return (repo_names is None or repo_name in repo_names) and _project_owns_repo(db, project, repo_name)
    return removable


def _configured_targets(db: Session, project: "Project", file, user: str, headers: dict,
                        removable: Callable[[str], bool]) -> tuple:
    """Where a campaign would deliver *file* now, in the repositories *removable* allows: (targets, failures)."""
    if not (isinstance(file, Workflow) and _delivers_to_reusable_repo(project, file)):
        return _project_delivery_targets(db, project, user, headers, only=removable)
    reusable_repo = _get_reusable_workflow_repo(project, user, db)
    if not removable(reusable_repo):
        return [], []
    try:
        return [_reusable_delivery_target(reusable_repo, user, headers, db)], []
    except Exception as e:
        print(f"❌ Could not resolve the default branch of {reusable_repo}: {type(e).__name__}")
        return [], [_unresolved(reusable_repo, f"Failed to resolve branches: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")]


def _removal_of(project: "Project", file) -> _Removal:
    """*file*, a workflow, custom file or the CODEOWNERS of *project*, as a removal at its current path."""
    if isinstance(file, CustomFile):
        return _Removal(deliveries.CUSTOM_FILE, file.id, file.file_path, file.file_path)
    if isinstance(file, Codeowners):
        path = file.file_path or ".github/CODEOWNERS"
        return _Removal(deliveries.CODEOWNERS, file.id, path, path)
    entry = _workflow_entry({"id": file.workflow_id, "name": file.workflow_name},
                            project.project_code or "", project.use_prefix)
    return _Removal(entry["kind"], entry["id"], file.workflow_name, entry["path"])


def _places_to_remove(db: Session, project: "Project", file, removal: _Removal, user: str, headers: dict,
                      removable: Callable[[str], bool], recorded_only: bool) -> tuple:
    """Where *file* is removed from: ([(target, path, sha)], failures). See _removal_targets."""
    held = deliveries.delivered_targets(db, removal.kind, removal.file_id, removal.path)
    if held:
        return [(_target_on(place.repo_name, place.branch), place.path, place.sha)
                for place in held if removable(place.repo_name)], []
    if recorded_only or not _record_never_saw(db, removal.kind, removal.file_id, file):
        return [], []
    configured, failures = _configured_targets(db, project, file, user, headers, removable)
    return [(target, removal.path, None) for target in configured], failures


def _removal_targets(db: Session, project: "Project", files: list, user: str = "", headers: Optional[dict] = None,
                     repo_names: Optional[List[str]] = None, recorded_only: bool = False) -> tuple:
    """Where removing *files*, workflows, custom files or the CODEOWNERS of *project*, takes each one off.

    Returns ``({_DeliveryTarget: [_Removal]}, failures)``. A file is removed
    from every (repo, branch) the record holds it delivered to, or an open PR
    carries it to, at the path it is at there and with the blob the record
    holds (deliveries.delivered_targets): whatever it is called now, and
    whatever the project delivers to now. Only a file the record has never
    seen, which may predate it (_record_never_saw), falls back to where a
    campaign would deliver it now, at its current path. Either way only in
    the project's own repositories, and in *repo_names* when given.
    ``failures`` are repositories that fallback could not resolve branches
    for, shaped as _project_delivery_targets reports them.

    *recorded_only* leaves out a file the record does not place instead, so
    the answer is the record's alone and no GitHub call is made: *user* and
    *headers* are needed only for the fallback.
    """
    removable = _removable_repos(db, project, repo_names)
    targets: dict = {}
    failures: list = []
    for file in files:
        removal = _removal_of(project, file)
        places, missed = _places_to_remove(db, project, file, removal, user, headers, removable, recorded_only)
        failures += missed
        for target, path, sha in places:
            targets.setdefault(target, []).append(removal._replace(path=path, sha=sha))
    return targets, failures


def _remove_directly(db: Session, project: "Project", target: _DeliveryTarget, removal: _Removal,
                     user: str, headers: dict) -> dict:
    """Commit *removal* straight to *target*'s branch; where the file is gone, record so and clear its drift there."""
    try:
        status, error = _delete_path_from_branch(target.owner, target.repo, removal.path, target.branch,
                                                 (project.project_code or "").upper(), user, db, headers,
                                                 known_sha=removal.sha)
    except Exception as e:
        print(f"❌ Exception deleting '{removal.path}' from {target.repo_name} on {target.branch}: {type(e).__name__}")
        status, error = DELETE_FAILED, f"{removal.path}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}"
    if status != DELETE_FAILED:
        deliveries.record_removed(db, removal.kind, removal.file_id, target.repo_name, target.branch)
        clear_drift(db, project, removal.kind, removal.file_id, target.repo_name, target.branch)
    return {"repo": target.repo_name, "branch": target.branch, "status": status, "error": error}


def _delete_directly(db: Session, project: "Project", targets: dict, user: str, headers: dict) -> list:
    """Commit each of *targets*' removals straight to its branch (see _removal_targets).

    The unreviewed path. No branch is listed, and where the record knows the
    blob nothing is read first: one DELETE per (file, repo, branch). Returns
    ``{"repo", "branch", "status", "error"}`` per removal.
    """
    results = [_remove_directly(db, project, target, removal, user, headers)
               for target, removals in targets.items() for removal in removals]
    db.commit()
    return results


def delete_custom_file_directly(db: Session, project: "Project", cf: CustomFile, user: str) -> dict:
    """Commit the removal of *cf* straight to every branch it is on (_removal_targets).

    This is the unreviewed path. Removal through a pull request goes through a real
    PR Campaign instead (``custom_files._delete_via_campaign``), so those deletions
    are grouped, tracked and merged like every other delivery rather than arriving
    as loose pull requests the user has to find one repository at a time.

    Returns ``{"targets": [...], "removed": bool, "errors": [...]}``. ``removed`` is
    True when nothing is left to remove — every target either had the file taken
    out or never had it — which tells the caller it is safe to drop the record.
    """
    headers = _get_authenticated_headers(user, {"Accept": ACCEPT_HEADER})
    if not headers:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
    targets, failures = _removal_targets(db, project, [cf], user, headers)
    results = failures + _delete_directly(db, project, targets, user, headers)
    errors = [r["error"] for r in results if r["status"] == DELETE_FAILED and r.get("error")]
    return {"targets": results, "removed": not errors, "errors": errors}


def _describe_empty_delivery(workflow_errors: list, custom_file_errors: list) -> str:
    """Why this target received nothing.

    ``NO_WORKFLOWS_COMMITTED`` on its own is wrong as often as it is right — the
    usual cause is a pending-delete file GitHub never had, which is not an error
    and not about workflows.
    """
    problems = [e for e in (workflow_errors or []) + (custom_file_errors or []) if e]
    if problems:
        return f"{NO_WORKFLOWS_COMMITTED}: {'; '.join(problems)}"
    return (
        "Nothing to deliver: every selected file already matches this branch, "
        "or the files marked for deletion are not present in it"
    )


def _commit_custom_files_to_branch(custom_files: List[dict], owner: str, repo: str, am_branch: str,
                                    project_code: str, user: str, db: Session, headers: dict,
                                    carried: Optional[list] = None, target: Optional[tuple] = None,
                                    written: frozenset = frozenset()) -> tuple:
    """Delete or upsert each custom file on the AM branch. Returns (custom_files_committed, custom_file_errors).

    ``carried``, ``target`` and ``written`` are as for _commit_workflows_to_branch.
    """
    custom_files_committed = []
    custom_file_errors = []
    for cf in custom_files:
        cf_path = cf.get("file_path", "")
        try:
            if cf.get("pending_delete"):
                outcome, error = _delete_path_from_branch(
                    owner, repo, cf_path, am_branch, project_code, user, db, headers
                )
                _note_removal(carried, _carried_entry(deliveries.CUSTOM_FILE, cf.get("id"), cf_path, delete=True), outcome)
                # A file that was already gone leaves the branch identical to its
                # base. Counting it as committed is what made the campaign open a
                # PR with no diff and report GitHub's 422 as a bare "Error".
                if outcome == DELETE_ABSENT:
                    continue
                success = outcome != DELETE_FAILED
            else:
                success, error = _upsert_custom_file_to_am_branch(
                    owner, repo, cf, am_branch, project_code, user, db, headers, carried, target, written,
                )
            if success:
                custom_files_committed.append(cf_path)
            if error:
                custom_file_errors.append(error)
        except Exception as e:
            custom_file_errors.append(f"{cf_path}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}")
            print(f"❌ Exception committing custom file '{cf_path}': {str(e)}")
    return custom_files_committed, custom_file_errors


def _update_codeowners_git_hash(db: Session, codeowners: dict, new_sha: Optional[str]) -> None:
    """Make the blob a campaign committed the file's git_hash, as _update_workflow_git_hash does.

    Not when the file was saved again since the campaign read it: that save's
    zeroed hash marks a change this commit does not carry.
    """
    if not new_sha:
        return
    record = db.query(Codeowners).populate_existing().filter(Codeowners.id == codeowners["id"]).first()
    if record is not None and (record.content, record.file_path) == (codeowners["content"], codeowners["file_path"]):
        record.git_hash = new_sha
        db.commit()


def _put_codeowners(codeowners: dict, owner: str, repo: str, am_branch: str, project_code: str,
                    existing_sha: Optional[str], headers: dict) -> tuple:
    """Write the CODEOWNERS at its path on *am_branch*. Returns (new blob SHA, error_message)."""
    co_path = codeowners["file_path"]
    put_body = {
        "message": f"Update {co_path} via ActionsManager [{project_code}] [skip ci]",
        "content": base64.b64encode(codeowners["content"].encode()).decode(),
        "branch": am_branch,
    }
    if existing_sha:
        put_body["sha"] = existing_sha
    put_resp = requests.put(
        f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{quote(co_path, safe='/')}",
        headers=headers,
        json=put_body,
        timeout=GITHUB_TIMEOUT_SECONDS,
    )
    if put_resp.status_code not in (200, 201):
        print(f"❌ Failed to commit CODEOWNERS '{co_path}': {put_resp.status_code}")
        return None, f"{co_path}: HTTP {put_resp.status_code}"
    print(f"✅ Committed CODEOWNERS '{co_path}' to {am_branch}")
    return (put_resp.json().get("content") or {}).get("sha"), None


def _commit_codeowners_to_branch(codeowners: Optional[dict], owner: str, repo: str, am_branch: str,
                                  project_code: str, user: str, db: Session, headers: dict,
                                  carried: Optional[list] = None, target: Optional[tuple] = None,
                                  written: frozenset = frozenset()) -> tuple:
    """Commit the project's CODEOWNERS to the AM branch, so it rides the same PR as workflows/custom files.

    Returns (codeowners_committed_path, error_message) — empty/None when there's
    nothing to commit. A branch that already holds this file at its path has
    nothing to commit unless removing the delivered copy it supersedes does:
    moving the file onto an identical copy is that one change.
    ``carried``, ``target`` and ``written`` are as for _commit_workflows_to_branch.
    """
    if not codeowners:
        return "", None
    co_path = codeowners["file_path"]
    errors: list = []
    try:
        sha_resp = github_get(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{quote(co_path, safe='/')}",
            user, db,
            headers=headers,
            params={"ref": am_branch},
        )
        existing_sha = sha_resp.json().get("sha") if sha_resp.status_code == 200 else None
        already_there = existing_sha == deliveries.blob_sha(codeowners["content"])
        if already_there:
            print(f"📌 CODEOWNERS '{co_path}' already on {am_branch}")
            new_sha = existing_sha
        else:
            new_sha, error = _put_codeowners(codeowners, owner, repo, am_branch, project_code, existing_sha, headers)
            if error:
                return "", error
        entry = _carried_entry(deliveries.CODEOWNERS, codeowners.get("id"), co_path, new_sha)
        removed = _remove_superseded_copy(entry, target, am_branch, written, owner, repo, project_code,
                                          user, db, headers, errors)
        if already_there and removed != DELETE_DELETED:
            return "", (errors[0] if errors else None)
        _update_codeowners_git_hash(db, codeowners, new_sha)
        _note_carried(carried, entry)
        return co_path, (errors[0] if errors else None)
    except Exception as e:
        print(f"❌ Exception committing CODEOWNERS '{co_path}': {type(e).__name__}")
        return "", f"{co_path}: {failure_kind(e, _SHOWN_GITHUB_ERRORS)}"


def _build_pr_error_result(workflows_committed: list, workflow_errors: list, custom_files_result: Optional[dict],
                            am_branch: str, target_branch: str, progress_callback, result_key: str,
                            reason: Optional[str] = None) -> dict:
    # Report what GitHub said, not just that something failed. The bare constant
    # left users with a red "Error" chip and no way to tell a permission problem
    # from a branch that had nothing to merge.
    message = f"{PR_CREATION_FAILED}: {reason}" if reason else PR_CREATION_FAILED
    print(f"❌ Failed to create PR for {am_branch} -> {target_branch}: {reason or 'no reason reported'}")
    result = {
        "status": "error",
        "error": message,
        "workflows_committed": workflows_committed,
        "workflow_errors": workflow_errors,
    }
    if custom_files_result is not None:
        result["custom_files_committed"] = custom_files_result.get("custom_files_committed")
        result["custom_file_errors"] = custom_files_result.get("custom_file_errors")
    if progress_callback:
        progress_callback(result_key, PROGRESS_OPENING_PR, "error", message)
    return result


def _build_pr_success_result(pr: dict, existing_pr: Optional[dict], am_branch: str, workflows_committed: list,
                              workflow_errors: list, custom_files_result: Optional[dict],
                              progress_callback, result_key: str) -> dict:
    print(f"✅ {'Updated existing' if existing_pr else 'Created new'} PR #{pr['number']}: {pr['html_url']}")
    result = {
        "status": "pr_updated" if existing_pr else "pr_created",
        "pr_url": pr['html_url'],
        "pr_number": pr['number'],
        "pr_title": pr.get('title'),
        "pr_author": (pr.get('user') or {}).get('login'),
        "pr_body": pr.get('body'),
        "branch_name": am_branch,
        "workflows_committed": workflows_committed,
    }
    if custom_files_result is not None:
        result["custom_files_committed"] = custom_files_result.get("custom_files_committed")
        result["codeowners_committed"] = custom_files_result.get("codeowners_committed")
    if workflow_errors:
        result["workflow_errors"] = workflow_errors
    if custom_files_result is not None and custom_files_result.get("custom_file_errors"):
        result["custom_file_errors"] = custom_files_result.get("custom_file_errors")
    if progress_callback:
        progress_callback(result_key, PROGRESS_OPENING_PR, "completed")
    return result


def _finalize_pr_result(owner: str, repo: str, am_branch: str, target_branch: str, headers: dict, user: str,
                         db: Session, project_code: str, progress_callback, result_key: str,
                         delivery: Optional[dict] = None) -> dict:
    """Resolve existing-PR-update vs new-PR-create and shape the per-branch result dict.

    Shared by the regular and reusable workflow update flows. ``delivery`` carries
    what this target actually received and the campaign context describing it:
    ``workflows_committed``, ``workflow_errors``, ``deliveries`` (the entries
    ``deliveries.record_pending`` takes), ``base_sha``, ``campaign_meta``,
    an optional ``pr_title``, and ``custom_files_result`` (custom_files_committed/
    custom_file_errors/codeowners_committed). Leave ``custom_files_result`` out —
    the reusable flow's case — to omit those keys from the result entirely.
    """
    delivery = delivery or {}
    workflows_committed = delivery.get("workflows_committed") or []
    workflow_errors = delivery.get("workflow_errors") or []
    custom_files_result = delivery.get("custom_files_result")

    existing_pr = _check_existing_pr(owner, repo, am_branch, target_branch, headers, user, db)
    reason = None
    if existing_pr:
        pr = existing_pr
    else:
        pr, reason = _create_pull_request(
            owner, repo, am_branch, target_branch, project_code, workflows_committed, headers, user, db,
            body=_campaign_pr_body(
                db, project_code, user, workflows_committed, target_branch,
                delivery.get("base_sha"), custom_files_result, delivery.get("campaign_meta"),
            ),
            title=delivery.get("pr_title"),
        )

    if not pr:
        return _build_pr_error_result(
            workflows_committed, workflow_errors, custom_files_result,
            am_branch, target_branch, progress_callback, result_key, reason
        )

    result = _build_pr_success_result(
        pr, existing_pr, am_branch, workflows_committed, workflow_errors,
        custom_files_result, progress_callback, result_key
    )
    result["deliveries"] = delivery.get("deliveries", [])
    return result


class _DeliveryRun:
    """What every target of one delivery shares, and what the targets report back."""

    def __init__(self, project_code: str, headers: dict, user: str, db: Session,
                 progress_callback=None, campaign_meta: Optional[dict] = None):
        self.project_code, self.headers, self.user, self.db = project_code, headers, user, db
        self.progress_callback, self.campaign_meta = progress_callback, campaign_meta
        self.results: dict = {}
        self.base_commits: dict = {}
        self.protection: dict = {}

    def progress(self, result_key: str, step: str, status: str, *error) -> None:
        if self.progress_callback:
            self.progress_callback(result_key, step, status, *error)

    def finish(self) -> dict:
        _stamp_target_metadata(self.results, self.base_commits, self.protection)
        return self.results


def _files_delivery(workflows_committed: list, workflow_errors: list, carried: list,
                    custom_files_committed: list, custom_file_errors: list, codeowners_committed: str = "") -> dict:
    """What one target received, in the shape _finalize_pr_result describes it by."""
    return {
        "workflows_committed": workflows_committed,
        "workflow_errors": workflow_errors,
        "deliveries": carried,
        "custom_files_result": {
            "custom_files_committed": custom_files_committed,
            "custom_file_errors": custom_file_errors,
            "codeowners_committed": codeowners_committed,
        },
    }


def _cut_am_branch(target: _DeliveryTarget, result_key: str, run: _DeliveryRun) -> tuple:
    """Cut *target*'s AM branch and read its protection. Returns (am_branch, created); no branch on failure."""
    branch_shas: dict = {}
    am_branch, branch_created, error_msg = _create_or_get_am_branch(
        target.owner, target.repo, target.branch, run.project_code, run.headers, run.user, run.db,
        base_shas=branch_shas,
    )
    run.base_commits[result_key] = branch_shas.get(target.branch)
    if not am_branch:
        # A 404 here would be the missing repo/branch, not "no protection
        # configured" — recording "none" would assert something about a
        # target this campaign never reached. Skips a wasted call too.
        run.protection[result_key] = {"status": "unknown", "error": error_msg}
        print(f"❌ Failed to create/get AM branch: {error_msg}")
        run.results[result_key] = {"status": "error", "error": error_msg}
        run.progress(result_key, PROGRESS_CREATING_BRANCH, "error", error_msg)
        return None, False
    print(f"{'✅ Created' if branch_created else '📌 Using existing'} AM branch: {am_branch}")
    run.protection[result_key] = _fetch_branch_protection(
        target.owner, target.repo, target.branch, run.headers, run.user, run.db
    )
    return am_branch, branch_created


def _committed_anything(delivery: dict) -> bool:
    files = delivery.get("custom_files_result") or {}
    return bool(delivery["workflows_committed"] or files.get("custom_files_committed")
                or files.get("codeowners_committed"))


def _nothing_delivered(target: _DeliveryTarget, am_branch: str, branch_created: bool, delivery: dict,
                       run: _DeliveryRun, result_key: str) -> dict:
    """The result of a target that received nothing, its AM branch dropped when this run cut it."""
    files = delivery.get("custom_files_result")
    workflow_errors = delivery["workflow_errors"]
    custom_file_errors = files["custom_file_errors"] if files is not None else []
    nothing_committed = _describe_empty_delivery(workflow_errors, custom_file_errors)
    print(f"❌ Nothing was committed to {am_branch}: {nothing_committed}")
    # A branch this run created is byte-identical to its base and can only
    # ever produce an empty PR, so it is dropped rather than left as a dead
    # actions-manager/* ref. Only when this run created it:
    # _create_or_get_am_branch also returns a pre-existing branch (a 422
    # "already exists"), and that one may carry an open PR whose work deleting
    # the ref would close.
    if branch_created:
        _delete_actions_manager_branch(target.owner, target.repo, am_branch, target.branch, run.user)
    result = {"status": "error", "error": nothing_committed, "workflow_errors": workflow_errors}
    if files is not None:
        result["custom_file_errors"] = custom_file_errors
    # Distinguishes "the branch never had it" from "this target failed", which
    # a caller cannot tell from status alone.
    result["nothing_to_deliver"] = not (workflow_errors or custom_file_errors)
    run.progress(result_key, PROGRESS_COMMITTING_FILES, "error", nothing_committed)
    return result


def _deliver_to_target(target: _DeliveryTarget, commit: Callable, run: _DeliveryRun) -> None:
    """Deliver one (repo, branch) on its own AM branch, recording its result on *run*.

    Cuts the AM branch, reads the target's branch protection, commits with
    ``commit(target, am_branch)``, which returns what it committed in
    _files_delivery's shape (``custom_files_result`` left out where the
    delivery carries no custom files), records the removals the branch had
    nothing to do for, drops a branch left empty, and otherwise opens or
    updates the pull request (_finalize_pr_result).
    """
    result_key = f"{target.repo_name} on {target.branch}"
    print(f"\n🔍 Processing target branch: {target.branch}")
    run.progress(result_key, PROGRESS_CREATING_BRANCH, "running")
    am_branch, branch_created = _cut_am_branch(target, result_key, run)
    if not am_branch:
        return
    run.progress(result_key, PROGRESS_COMMITTING_FILES, "running")
    delivery = commit(target, am_branch)
    delivery["deliveries"] = _record_absent_removals(run.db, delivery["deliveries"], target.repo_name, target.branch)
    if not _committed_anything(delivery):
        run.results[result_key] = _nothing_delivered(target, am_branch, branch_created, delivery, run, result_key)
        return
    run.progress(result_key, PROGRESS_OPENING_PR, "running")
    result = _finalize_pr_result(
        target.owner, target.repo, am_branch, target.branch, run.headers, run.user, run.db,
        run.project_code, run.progress_callback, result_key,
        delivery={**delivery, "base_sha": run.base_commits.get(result_key), "campaign_meta": run.campaign_meta},
    )
    if branch_created and _holds_everything_already(result):
        # A workflow the target branch already holds is "committed" unchanged,
        # so only GitHub's refusal shows the branch this run cut carries
        # nothing new: the target is up to date, as a file found already there
        # is, not failed. A restore campaign meets this on every other branch of
        # the repository it puts drift right in.
        result = _nothing_delivered(
            target, am_branch, branch_created, {**delivery, "workflows_committed": []}, run, result_key,
        )
    run.results[result_key] = result


def _holds_everything_already(result: dict) -> bool:
    """Whether GitHub refused a pull request because its branch adds nothing to the target."""
    return result.get("status") == "error" and "No commits between" in (result.get("error") or "")


class _ProjectFiles(NamedTuple):
    """What one campaign commits to every caller-repository target."""
    workflows: List[dict]
    custom_files: List[dict]
    codeowners: Optional[dict]
    use_prefix: bool
    written: frozenset


def _commit_project_files(files: _ProjectFiles, run: _DeliveryRun, target: _DeliveryTarget, am_branch: str) -> dict:
    """Commit the workflows, custom files and CODEOWNERS to the AM branch."""
    where = {"carried": [], "target": (target.repo_name, target.branch), "written": files.written}
    workflows_committed, workflow_errors = _commit_workflows_to_branch(
        files.workflows, target.owner, target.repo, run.project_code, am_branch, run.headers, target.repo_name,
        run.user, run.db, files.use_prefix, **where,
    )
    custom_files_committed, custom_file_errors = _commit_custom_files_to_branch(
        files.custom_files, target.owner, target.repo, am_branch, run.project_code, run.user, run.db, run.headers,
        **where,
    )
    codeowners_committed, codeowners_error = _commit_codeowners_to_branch(
        files.codeowners, target.owner, target.repo, am_branch, run.project_code, run.user, run.db, run.headers,
        **where,
    )
    if codeowners_error:
        custom_file_errors.append(codeowners_error)
    return _files_delivery(workflows_committed, workflow_errors, where["carried"],
                           custom_files_committed, custom_file_errors, codeowners_committed)


def _process_regular_workflows_update(repo_names: List[str], workflows: List[dict],
                                    project_code: str, branch_option: str,
                                    regex_pattern: str, branch_max_age_days: int,
                                    headers: dict, db: Session, user: str = None, use_prefix: bool = True,
                                    project: "Project" = None, progress_callback=None,
                                    custom_files: Optional[List[dict]] = None,
                                    codeowners: Optional[dict] = None,
                                    campaign_meta: Optional[dict] = None) -> dict:
    """
    Process updates for regular workflows across multiple repositories.
    Creates dedicated Actions Manager branches and Pull Requests per (repo, target_branch).

    When ``project`` is provided, the per-repository branch override (if any)
    on ``project_repos`` takes precedence over the supplied ``branch_option``/
    ``regex_pattern``/``branch_max_age_days`` arguments — these arguments are
    used as the project-level fallback only.

    ``custom_files`` (list of dicts with id/file_path/file_content/pending_delete) are
    committed to the same AM branch as workflows so they land in the same PR.

    ``codeowners`` (the project's CODEOWNERS, file_path/content) is committed to
    every AM branch the same way.

    Returns:
        dict: Results with PR URLs and status per repo/branch combination
    """
    custom_files = custom_files or []
    files = _ProjectFiles(workflows, custom_files, codeowners, use_prefix,
                          _paths_written(workflows, custom_files, codeowners, project_code, use_prefix))
    run = _DeliveryRun(project_code, headers, user, db, progress_callback, campaign_meta)
    commit = functools.partial(_commit_project_files, files, run)

    print("🔍 _process_regular_workflows_update starting:")
    print(f"   Repositories: {repo_names}")
    print(f"   Workflows: {[w.get('name') for w in workflows]}")
    print(f"   Custom files: {[cf.get('file_path') for cf in custom_files]}")
    print(f"   CODEOWNERS: {codeowners['file_path'] if codeowners else None}")
    print(f"   Project code: {project_code}")
    print(f"   Use prefix: {use_prefix}")

    for repo_name in repo_names:
        print(f"🔍 Processing repo: {repo_name}")
        owner, repo = repo_name.split("/")
        target_branches, branch_error = _resolve_effective_target_branches(
            repo_name, owner, repo, project, db, branch_option, regex_pattern, branch_max_age_days, headers, user
        )
        if branch_error:
            return branch_error
        for target_branch in target_branches:
            _deliver_to_target(_DeliveryTarget(repo_name, owner, repo, target_branch), commit, run)

    results = run.finish()
    print(f"\n🔍 _process_regular_workflows_update complete. Results: {results}")
    return results


def _commit_reusable_files(rxworkflows: List[dict], use_prefix: bool, run: _DeliveryRun,
                           target: _DeliveryTarget, am_branch: str) -> dict:
    """Commit the reusable workflows to the AM branch."""
    carried: list = []
    workflows_committed, workflow_errors = _commit_reusable_workflows_to_branch(
        rxworkflows, target.owner, target.repo, run.project_code, am_branch, run.headers, target.repo_name,
        run.user, run.db, use_prefix, carried=carried, target=(target.repo_name, target.branch),
        written=_paths_written(rxworkflows, [], None, run.project_code, use_prefix),
    )
    return {"workflows_committed": workflows_committed, "workflow_errors": workflow_errors, "deliveries": carried}


def _process_reusable_workflows_update(rxworkflows: List[dict], user: str,
                                     project_code: str,
                                     regex_pattern: str, branch_max_age_days: int,
                                     headers: dict, db: Session = None,
                                     reusable_repo: str = None, use_prefix: bool = True, progress_callback=None,
                                     campaign_meta: Optional[dict] = None) -> dict:
    """
    Process updates for reusable workflows in the dedicated repository.
    Creates dedicated Actions Manager branches and Pull Requests per target_branch.

    Args:
        reusable_repo: Full repo name (owner/repo) to push reusable workflows to.
                       When None, falls back to ``{user}/am-reuseable-workflow``.
        use_prefix: Whether to use the AM_{PROJECT_CODE}_ prefix for workflow names.

    Returns:
        dict: Results with PR URLs and status per branch combination
    """
    if not reusable_repo:
        reusable_repo = f"{user}/am-reuseable-workflow"
    if "/" not in reusable_repo:
        return {"error": f"Invalid repo name format: '{reusable_repo}'", "status": 400}

    print("🔍 _process_reusable_workflows_update starting:")
    print(f"   Reusable repo: {reusable_repo}")
    print(f"   Workflows: {[w.get('name') for w in rxworkflows]}")
    print(f"   Project code: {project_code}")

    run = _DeliveryRun(project_code, headers, user, db, progress_callback, campaign_meta)
    _deliver_to_target(_reusable_delivery_target(reusable_repo, user, headers, db),
                       functools.partial(_commit_reusable_files, rxworkflows, use_prefix, run), run)
    results = run.finish()
    print(f"\n🔍 _process_reusable_workflows_update complete. Results: {results}")
    return results


def _resolve_effective_rwx_params(
    project: "Project", project_code: str, db: Session
) -> tuple:
    """Resolve effective project code and prefix for reusable workflow updates.

    For standard (caller) projects, uses the owning RWX project's settings.
    Returns (effective_code, effective_prefix).
    """
    if project.project_type != "standard":
        return project_code, project.use_prefix

    linked_rwx = db.query(Project).join(
        LinkedReusableWorkflow, LinkedReusableWorkflow.rwx_project_id == Project.project_id
    ).filter(
        LinkedReusableWorkflow.standard_project_id == project.project_id
    ).first()
    if linked_rwx:
        return linked_rwx.project_code.upper(), linked_rwx.use_prefix
    return project_code, project.use_prefix


_ERR_NO_PROJECT_REPOS_SELECTED = (
    "None of the selected repositories belong to this project. Save the project "
    "to add a newly selected repository, then push again."
)


def _only_project_repos(db: Session, project: "Project", repo_names: list) -> tuple:
    """Split the requested repositories into (this project's, the rest) (#2316).

    Save-and-push commits to every repository it is given, and the list comes
    from the request, so without this an editor of one project could open
    branches and pull requests carrying its workflows in any repository their
    token can write to. A repository outside the project is skipped and
    reported rather than refusing the push, as campaigns skip it
    (_get_filtered_repo_names): the editor sends every ticked repository, a
    newly ticked one included before the project is saved, while the push it
    makes is to the project's own. Ownership is _project_owns_repo's, the
    check deletion uses.
    """
    kept, skipped = [], []
    for name in repo_names:
        cleaned = name.strip() if isinstance(name, str) else ""
        if cleaned and _project_owns_repo(db, project, cleaned):
            kept.append(cleaned)
        else:
            skipped.append(name)
    if skipped:
        print(f"⚠️ update_project_files: skipped {len(skipped)} repository name(s) outside "
              f"project {project.project_id}")
    return kept, skipped


class _PushedWorkflow(BaseModel):
    name: str = ""


class UpdateProjectFilesRequest(BaseModel):
    project_name: str
    repo_names: List[str] = []
    workflows: List[_PushedWorkflow] = []
    rxworkflows: List[_PushedWorkflow] = []
    custom_file_ids: List[int] = []
    include_codeowners: bool = False


def _pushed_names(pushed: List[_PushedWorkflow]) -> Optional[List[str]]:
    names = [w.name.strip() for w in pushed if w.name.strip()]
    return names or None


@router.post("/api/update-project-files", responses=_responses(400, 401, 403, 404, 409, 500))
def update_project_files(
    payload: UpdateProjectFilesRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Push saved project files to the project's pull requests: a PR campaign of just those.

    The editor's "Commit and update PR", for workflows, custom files and the
    project's CODEOWNERS alike. It used to push the request's copy of a
    workflow through a commit path of its own that saved no PR row, so the
    delivery record never learned what the push carried. A campaign records
    it, and delivers from what is stored, which the editor saves first.
    """
    user = account.github_user
    project = _find_project_by_name(db, user, payload.project_name.strip())
    if not project:
        raise HTTPException(status_code=404, detail=PROJECT_ERROR)
    _require_project_editor(db, user, project)
    repo_names, skipped_repos = _only_project_repos(db, project, payload.repo_names)
    names_files = bool(payload.custom_file_ids or payload.include_codeowners)
    # A push carries local changes only. A campaign delivers a custom file it
    # names whatever its status, so a synced one named here (a display-name
    # edit) would otherwise be re-sent over copies changed on GitHub.
    custom_file_ids = [
        cid for (cid,) in db.query(CustomFile.id).filter(
            CustomFile.project_id == project.project_id,
            CustomFile.id.in_(payload.custom_file_ids or []),
            _custom_file_has_local_change(),
        )
    ]
    campaign = CreatePullRequestsRequest(
        project_name=project.project_name,
        project_id=project.project_id,
        selected_repos=repo_names or None,
        # [] keeps a files-only push on the project's repositories without
        # selecting a workflow; None there would select every one.
        selected_workflows=_pushed_names(payload.workflows) or ([] if names_files else None),
        selected_reusable_workflows=_pushed_names(payload.rxworkflows),
        selected_custom_file_ids=custom_file_ids,
        include_codeowners=payload.include_codeowners,
    )
    if campaign.selected_workflows is None and campaign.selected_reusable_workflows is None:
        # A campaign selecting neither delivers every workflow in the project.
        return {"message": "⚠️ Nothing processed - no project files were named", "results": {},
                "skipped_repos": skipped_repos}
    # Every repository it named is someone else's: say so rather than deliver to
    # all of the project's, which is what a campaign does with no selection.
    if not repo_names and _includes_regular_workflows(campaign, project, db):
        return {"error": _ERR_NO_PROJECT_REPOS_SELECTED, "status": 400, "skipped_repos": skipped_repos}
    outcome = create_pull_requests(campaign, BackgroundTasks(), db, github_user=user)
    return {"message": "✅ All workflows updated", "results": outcome["results"],
            "skipped_repos": skipped_repos}



def _authorize_workflow_deletion(db: Session, user: str, project_name: str,
                                 repo_names: List[str]) -> Project:
    """Resolve and authorize the project a workflow deletion targets.

    ``_find_project_by_name`` authorizes; the global ``ilike`` lookup this
    replaced did not, so any signed-in caller could name any project. ilike is
    also the wrong matcher for a name: "_" is a LIKE wildcard and project and
    workflow names routinely contain one.
    """
    project = _find_project_by_name(db, user, project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in database")

    # Deleting a workflow file is a write to the project's repositories.
    _require_project_editor(db, user, project)

    # repo_names comes straight from the request body, so without this the
    # caller chooses which repositories get a file deleted out of them.
    # _require_repo_in_project is deliberately looser — it also accepts the
    # linked RWX project's repo so reusable-workflow drift stays resolvable —
    # and that exception must not extend to deleting files.
    for requested_repo in repo_names:
        _require_owned_repo(db, project, requested_repo)

    return project


def _validated_delete_delivery(data: dict) -> str:
    """The requested delivery, defaulting to direct for callers that predate it."""
    delivery = (data.get("delivery") or DELIVERY_DIRECT).strip()
    if delivery not in VALID_DELETE_DELIVERIES:
        raise HTTPException(
            status_code=400,
            detail=f"delivery must be one of {', '.join(VALID_DELETE_DELIVERIES)}",
        )
    return delivery


def _campaign_found_nothing_to_remove(results: dict) -> bool:
    """True when every target reported no error — the file simply was not there.

    _describe_empty_delivery already separates the two reasons a target receives
    nothing: a real failure, which it lists, and a pending-delete file the branch
    never had, which is not an error. A campaign that opened no pull request for
    the second reason has not failed — there was nothing on GitHub to review the
    removal of.
    """
    if not results:
        return False
    return all(
        isinstance(outcome, dict) and outcome.get("nothing_to_deliver")
        for outcome in results.values()
    )


class _CampaignLabel(NamedTuple):
    """A campaign's name and description, read as a campaign request's are (_campaign_meta)."""
    campaign_name: str
    campaign_description: str


def _commit_removals(targets: dict, run: _DeliveryRun, target: _DeliveryTarget, am_branch: str) -> dict:
    """Take *target*'s removals off its AM branch.

    A CODEOWNERS removal is reported as a campaign reports delivering it, its
    error among the custom files' (_commit_project_files).
    """
    carried: list = []
    committed: dict = {kind: [] for kind in _FILE_STATUS}
    errors: dict = {kind: [] for kind in _FILE_STATUS}
    for removal in targets[target]:
        outcome = _remove_on_branch(removal, target.owner, target.repo, run.project_code, am_branch, run.headers,
                                    run.user, run.db, committed[removal.kind], errors[removal.kind])
        _note_removal(carried, _carried_entry(removal.kind, removal.file_id, removal.path, delete=True), outcome)
    return _files_delivery(committed[deliveries.WORKFLOW], errors[deliveries.WORKFLOW], carried,
                           committed[deliveries.CUSTOM_FILE],
                           errors[deliveries.CUSTOM_FILE] + errors[deliveries.CODEOWNERS],
                           ", ".join(committed[deliveries.CODEOWNERS]))


def _open_removal_prs(db: Session, project: Project, targets: dict, user: str, headers: dict,
                      label: _CampaignLabel) -> dict:
    """Open a pull request on each of *targets* taking its removals off it (see _removal_targets).

    By the steps every campaign delivers a target by (_deliver_to_target), so
    a target the file is already gone from gets no empty pull request. No
    preflight: a removal adds nothing to validate. The results are keyed and
    shaped as create_pull_requests's, for _save_prs_and_update_status.
    """
    names = sorted({r.name for removals in targets.values() for r in removals if r.kind == deliveries.WORKFLOW})
    run = _DeliveryRun(project.project_code, headers, user, db,
                       campaign_meta=_campaign_meta(label, project, db, names))
    commit = functools.partial(_commit_removals, targets, run)
    for target in targets:
        _deliver_to_target(target, commit, run)
    return run.finish()


def _run_removal_campaign(db: Session, project: Project, file, targets: dict, failures: list,
                          user: str, headers: dict) -> dict:
    """The PR Campaign whose only change is removing *file*, a workflow or custom file of *project*.

    *targets* and *failures* are _removal_targets's; a failure is reported
    among the results, keyed by its repository. Returns what
    create_pull_requests returns.
    """
    name = _removal_of(project, file).name
    label = _CampaignLabel(f"Remove {name}", f"Removes {name} from {project.project_name}.")
    results = _open_removal_prs(db, project, targets, user, headers, label)
    results.update({failure["repo"]: {"status": "error", "error": failure["error"]} for failure in failures})
    is_workflow = isinstance(file, Workflow)
    pr_count, campaign_id = _save_prs_and_update_status(
        results, project, [file.workflow_name] if is_workflow else [], [], db, github_user=user,
        custom_file_ids=None if is_workflow else [file.id],
        repo_names=sorted({target.repo_name for target in targets}),
        campaign_name=label.campaign_name, campaign_description=label.campaign_description,
    )
    return {"results": results, "prs_created": pr_count, "campaign_id": campaign_id}


def _project_files(db: Session, project: Project) -> list:
    """Every file *project* owns: its workflows, its reusable ones among them, its custom files and its CODEOWNERS."""
    workflows = db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project.project_id,
    ).order_by(Workflow.workflow_name).all()
    custom = db.query(CustomFile).filter(CustomFile.project_id == project.project_id).order_by(CustomFile.file_path).all()
    codeowners = _project_codeowners(db, project.project_id)
    return [*workflows, *custom, *([codeowners] if codeowners else [])]


def _project_removal_targets(db: Session, project: Project, files: list) -> dict:
    """Where deleting *project* with its files takes each of *files* off: where the record holds it, and only there.

    No fallback for a file the record has never placed (_removal_targets's
    recorded_only), so this never calls GitHub. project_removal_plan names
    those files instead.
    """
    targets, _ = _removal_targets(db, project, files, recorded_only=True)
    return targets


def _described(removal: _Removal) -> dict:
    return {"kind": removal.kind, "name": removal.name, "path": removal.path}


def project_removal_plan(db: Session, project: Project) -> dict:
    """What deleting *project* with its files would remove, read from the delivery record alone: no GitHub call.

    ``removal_targets`` are the (repo, branch) a removal pull request would be
    opened on, each with the files it removes there, at their path there.
    ``unrecorded_files`` may be on GitHub although the record has never placed
    them (_record_never_saw), so no pull request removes them.
    """
    files = _project_files(db, project)
    targets = _project_removal_targets(db, project, files)
    planned = {(r.kind, r.file_id) for removals in targets.values() for r in removals}
    removals = [(file, _removal_of(project, file)) for file in files]
    return {
        "removal_targets": [
            {"repository": target.repo_name, "branch": target.branch, "files": [_described(r) for r in held]}
            for target, held in sorted(targets.items())
        ],
        "unrecorded_files": [
            _described(r) for file, r in removals
            if (r.kind, r.file_id) not in planned and _record_never_saw(db, r.kind, r.file_id, file)
        ],
    }


def _open_prs_holding(db: Session, project: Project) -> list:
    """The open PRs removing *project*'s files must wait for: its own, and other projects' carrying its files."""
    carried = deliveries.open_pr_ids_carrying_project(db, project.project_id)
    return db.query(ProjectPullRequest).filter(
        ProjectPullRequest.pr_state == "open",
        or_(ProjectPullRequest.project_id == project.project_id, ProjectPullRequest.pr_id.in_(carried)),
    ).order_by(ProjectPullRequest.repo_name, ProjectPullRequest.pr_number).all()


def _linked_callers(db: Session, account: Optional[Account], project: Project) -> list:
    """The Caller Workflow Projects linking *project*'s reusable workflows, by name, those *account* may not see as a count."""
    if project.project_type != "rwx":
        return []
    ids = [pid for pid in _get_pr_visibility_project_map(db, project) if pid != project.project_id]
    callers = db.query(Project).filter(Project.project_id.in_(ids)).all()
    names = sorted(p.project_name for p in callers if project_role(db, account, p) is not None)
    hidden = len(callers) - len(names)
    return names + ([f"{hidden} other project(s)"] if hidden else [])


def _repos_out_of_reach(db: Session, project: Project) -> list:
    """Repositories the record holds *project*'s files delivered to that no removal may reach (_removable_repos)."""
    removable = _removable_repos(db, project, None)
    return [name for name in deliveries.project_delivered_repos(db, project.project_id) if not removable(name)]


_KEEP_FILES_INSTEAD = "or delete the project and keep its files"


def project_removal_blocker(db: Session, account: Optional[Account], project: Project) -> Optional[dict]:
    """Why *project* cannot be deleted with its files now, or None. Deleting it keeping them is never refused here.

    Shaped as a 409's detail: ``message`` says why and what to do, and the
    list it is about follows it.

    - An open PR, its own or another project's carrying one of its files:
      its outcome would settle the deletion's (settle_project_prs).
    - Caller projects linking a Reusable Workflow Project: removing its
      reusable workflows breaks their ``uses:`` lines.
    - Files delivered to repositories no longer in the project: no removal may
      reach them, so the deletion would only be called off once the rest
      merged (_settle_deletion).
    """
    open_prs = _open_prs_holding(db, project)
    if open_prs:
        listed = ", ".join(f"{pr.repo_name}#{pr.pr_number}" for pr in open_prs)
        return {"message": f"Merge or close its open pull requests first: {listed}.",
                "open_prs": [{"repo_name": pr.repo_name, "pr_number": pr.pr_number, "pr_url": pr.pr_url,
                              "target_branch": pr.target_branch} for pr in open_prs]}
    callers = _linked_callers(db, account, project)
    if callers:
        return {"message": f"Caller projects use its reusable workflows: {', '.join(callers)}. Removing them would "
                           f"break those projects' workflows. Unlink them first, {_KEEP_FILES_INSTEAD}.",
                "linked_projects": callers}
    out_of_reach = _repos_out_of_reach(db, project)
    if out_of_reach:
        return {"message": f"Some of its files are in repositories no longer in the project: {', '.join(out_of_reach)}. "
                           f"Add them back to remove the files there too, {_KEEP_FILES_INSTEAD}.",
                "repositories": out_of_reach}
    return None


class ProjectDeletionOutcome(NamedTuple):
    """What deleting a project did: deleted it, or left it Deleting while its *prs_created* removal PRs are open.

    *results* are keyed and shaped as create_pull_requests's.
    """
    project_deleted: bool
    pr_state: Optional[str]
    campaign_id: Optional[int]
    prs_created: int
    results: dict


def _deletion_outcome(results: dict, campaign_id: Optional[int] = None, prs_created: int = 0) -> ProjectDeletionOutcome:
    deleting = prs_created > 0
    return ProjectDeletionOutcome(not deleting, PROJECT_DELETING if deleting else None, campaign_id, prs_created,
                                  results)


def _deleted_now(db: Session, project: Project, results: dict) -> ProjectDeletionOutcome:
    delete_project_record(db, project)
    return _deletion_outcome(results)


def start_project_removal(db: Session, project: Project, user: str) -> ProjectDeletionOutcome:
    """Delete *project* with its files: remove them from GitHub by a last PR Campaign, never a direct commit.

    One pull request per (repo, branch) the delivery record holds any of its
    workflows, custom files or CODEOWNERS on, removing each at its path there
    (_project_removal_targets). While they are open the project is Deleting,
    read-only to everyone (authorization.project_role); settle_project_prs
    deletes it once all have merged, and calls the deletion off when one
    closes. With nothing to remove it is deleted now. When no pull request
    could be opened for any other reason it is a 502 and the project is left
    as it was. The caller refuses first what project_removal_blocker refuses.
    """
    headers = _get_authenticated_headers(user, {"Accept": ACCEPT_HEADER})
    if not headers:
        raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
    targets = _project_removal_targets(db, project, _project_files(db, project))
    if not targets:
        return _deleted_now(db, project, {})
    name = project.project_name
    label = _CampaignLabel(f"Delete project {name}", f"Removes the files of {name} from GitHub. Merging every "
                                                     "pull request deletes the project; closing one keeps it.")
    results = _open_removal_prs(db, project, targets, user, headers, label)
    pr_count, campaign_id = _save_prs_and_update_status(
        results, project, [], [], db, github_user=user, repo_names=sorted({t.repo_name for t in targets}),
        campaign_name=label.campaign_name, campaign_description=label.campaign_description,
        project_state=PROJECT_DELETING,
    )
    if pr_count:
        return _deletion_outcome(results, campaign_id, pr_count)
    if _campaign_found_nothing_to_remove(results):
        return _deleted_now(db, project, results)
    raise HTTPException(status_code=502, detail={
        "message": f"No pull request could be opened to remove the files of '{name}', so it was not deleted.",
        "results": results,
    })


def _project_workflow_named(db: Session, project: Project, workflow_name: str):
    """*project*'s workflow called *workflow_name*, matched exactly but for case.

    Never ilike: "_" is a LIKE wildcard and workflow names routinely contain one.
    """
    return db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project.project_id,
        func.lower(Workflow.workflow_name) == workflow_name.lower(),
    ).first()


def _removed_without_campaign(db: Session, project: Project, workflow, message: str,
                              results: Optional[dict] = None) -> dict:
    """Drop *workflow* from *project* when there is no GitHub copy for a pull request to review."""
    workflow_id = workflow.workflow_id
    _detach_workflow_from_project(db, project.project_id, workflow)
    db.commit()
    drop_workflow_drift(db, project, workflow_id)
    return {"message": message, "delivery": DELIVERY_CAMPAIGN, "prs_created": 0, "results": results or {}}


def _removed_as_absent(db: Session, project: Project, workflow, results: Optional[dict] = None) -> dict:
    return _removed_without_campaign(
        db, project, workflow, f"✅ '{workflow.workflow_name}' removed — it was not present in any target branch",
        results,
    )


def _no_removal_pr(db: Session, project: Project, workflow, results: dict) -> dict:
    """A removal campaign opened nothing: removed if no target had the file, else a 502."""
    if _campaign_found_nothing_to_remove(results):
        # Nothing failed — the file is not on any target branch, so there is
        # no removal to review. Erroring here would leave a workflow the user
        # asked to delete both undeleted and unexplained.
        return _removed_as_absent(db, project, workflow, results)
    raise HTTPException(
        status_code=502,
        detail={"message": f"No pull request was opened to remove '{workflow.workflow_name}'", "results": results},
    )


def _delete_workflow_via_campaign(db: Session, project: Project, workflow_name: str, user: str,
                                  headers: dict, repo_names: Optional[List[str]] = None) -> dict:
    """Run a PR Campaign whose only change is removing this workflow.

    Mirrors ``custom_files._delete_via_campaign``: each pull request removes the
    file where the delivery record says it is (_removal_targets), and the
    campaign record links them, so the deletion lands in PR Campaigns with the
    grouping, status and merge every other delivery gets rather than as a
    direct commit nobody reviewed. A Reusable Workflow Project's own reusable
    workflows too, in its repository.

    The row stays until the campaign merges — that is what
    _update_project_workflows_status drops on the synced_with_github transition.
    """
    workflow = _project_workflow_named(db, project, workflow_name)
    if not workflow:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_name}' not found in project '{project.project_name}'",
        )
    if _delivers_to_reusable_repo(project, workflow):
        # The lock a campaign carrying it would meet (_build_reusable_workflow_results).
        _check_reusable_workflow_locks(db, [workflow], project.project_id)

    # A project with no repositories has nowhere to deliver to, so a campaign
    # would have no target. There is no GitHub copy to review the removal of:
    # drop the row, as custom_files._delete_via_campaign does. Erroring here
    # would leave the workflow undeletable for anyone who left the preselected
    # campaign option alone.
    if not db.query(ProjectRepo).filter_by(project_id=project.project_id).first():
        return _removed_without_campaign(
            db, project, workflow, f"✅ '{workflow_name}' removed — this project has no repositories to deliver to",
        )

    # Before the mark below, whose status reads as a delivery made before the
    # record existed. _authorize_workflow_deletion vetted repo_names; none
    # means every project repository.
    targets, failures = _removal_targets(db, project, [workflow], user, headers, repo_names or None)
    if not targets and not failures:
        return _removed_as_absent(db, project, workflow)

    previous_status = workflow.workflow_status
    workflow.pending_delete = True
    workflow.workflow_status = "committed_locally"
    db.commit()

    def _unmark():
        workflow.pending_delete = False
        workflow.workflow_status = previous_status
        db.commit()

    try:
        campaign = _run_removal_campaign(db, project, workflow, targets, failures, user, headers)
    except Exception:
        _unmark()
        raise

    if not campaign["prs_created"]:
        # Leaving the workflow marked for a deletion no pull request will carry
        # out would strand it.
        _unmark()
        return _no_removal_pr(db, project, workflow, campaign["results"])

    return {
        "message": f"✅ PR Campaign opened to remove '{workflow.workflow_name}'",
        "delivery": DELIVERY_CAMPAIGN,
        "campaign_id": campaign["campaign_id"],
        "prs_created": campaign["prs_created"],
        "results": campaign["results"],
    }


def _results_by_repo(outcomes: list, failures: list, repo_names: List[str]) -> dict:
    """``{repo: {"deleted": [branches], "failed": {branch: reason}}}`` for each of *repo_names*.

    The shape the direct workflow deletes report. A repository whose branches
    could not be resolved also carries the ``error``.
    """
    results: dict = {}

    def entry(repo_name: str) -> dict:
        return results.setdefault(repo_name, {"deleted": [], "failed": {}})

    for name in repo_names:
        entry(name)
    for outcome in outcomes:
        if outcome["status"] == DELETE_DELETED:
            entry(outcome["repo"])["deleted"].append(outcome["branch"])
        elif outcome["status"] == DELETE_FAILED:
            entry(outcome["repo"])["failed"][outcome["branch"]] = outcome["error"]
    for failure in failures:
        entry(failure["repo"])["error"] = failure["error"]
    return results


def _delete_workflow_directly(db: Session, project: Project, workflow_name: str, user: str, headers: dict,
                              repo_names: List[str]) -> dict:
    """Commit the removal of *project*'s workflow straight to each branch of *repo_names* it is on."""
    workflow = _project_workflow_named(db, project, workflow_name)
    if workflow is None:
        return _results_by_repo([], [], repo_names)
    targets, failures = _removal_targets(db, project, [workflow], user, headers, repo_names)
    return _results_by_repo(_delete_directly(db, project, targets, user, headers), failures, repo_names)


@router.delete("/api/delete-workflow", responses=_responses(400, 401, 404, 409, 500))
async def delete_workflow(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Deletes a workflow file from the branches the delivery record says it is on (_removal_targets)."""
    try:
        data = await request.json()
        print(f"📌 Debug: Incoming Delete Request: {data}")

        user = account.github_user
        repo_names = data.get("repo_names", [])
        project_name = data.get("project_name", "").strip()

        if not (data.get("workflow_name") or "").strip():
            raise HTTPException(status_code=400, detail=MISSING_WORKFLOW_NAME_DETAIL)
        # This name is interpolated into a GitHub contents path, so it gets the
        # same validation every other path-forming caller applies.
        workflow_name = _validate_workflow_name(data.get("workflow_name"))

        if user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

        headers = {
            "Authorization": f"token {user_tokens[user]}",
            "Accept": ACCEPT_HEADER
        }

        project = _authorize_workflow_deletion(db, user, project_name, repo_names)

        if _validated_delete_delivery(data) == DELIVERY_CAMPAIGN:
            return _delete_workflow_via_campaign(db, project, workflow_name, user, headers, repo_names)

        results = _delete_workflow_directly(db, project, workflow_name, user, headers, repo_names)
        return {"message": "✅ Workflow deletions completed!", "results": results}

    except HTTPException:
        # Re-raise HTTPExceptions as-is (e.g., 401 Unauthorized)
        raise
    except Exception as e:
        print(f"❌ Error deleting workflows: {str(e)}")
        raise HTTPException(status_code=500, detail="Error deleting workflows")


_ERR_REUSABLE_NOT_OURS = (
    "This project does not own the reusable-workflow repository, so the file cannot be "
    "deleted from GitHub here. Remove it from ActionsManager only, or delete it from the "
    "Reusable Workflow Project that owns it."
)


def _require_owns_reusable_repo(db: Session, project: Project, user: str) -> str:
    """Return the reusable-workflow repo, refusing one the project does not own.

    ``_get_reusable_workflow_repo`` returns a *standard* project's linked RWX
    project's repository, or the ``{user}/am-reuseable-workflow`` fallback —
    never one of this project's own. Deleting there removes a file every other
    caller project linking that RWX project depends on, and with use_prefix off
    the path is the bare workflow name, so it is exactly the shared copy.

    #2006 removed caller-owned reusable workflows from the PR modal for this
    same reason ("a campaign cannot open a PR against a repository outside the
    project"). The delete path was left reachable.

    The check is on the resolved repository, not on project_type: an RWX project
    with no repositories of its own resolves to the fallback, which is equally
    not ours to delete from.
    """
    reusable_repo = _get_reusable_workflow_repo(project, user, db)
    if not _project_owns_repo(db, project, reusable_repo):
        raise HTTPException(status_code=409, detail=_ERR_REUSABLE_NOT_OURS)
    return reusable_repo


@router.delete("/api/delete-reusable-workflow", responses=_responses(400, 401, 404, 409, 500))
async def delete_reusable_workflow(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Deletes a reusable workflow from the branches of the reusable-workflow repository it is on."""
    try:
        data = await request.json()
        print(f"📌 Debug: Incoming Delete Reusable Workflow Request: {data}")

        user = account.github_user
        workflow_name = data.get("workflow_name", "").strip()
        project_name = data.get("project_name", "").strip()

        if not workflow_name:
            raise HTTPException(status_code=400, detail=MISSING_WORKFLOW_NAME_DETAIL)

        if user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

        headers = {
            "Authorization": f"token {user_tokens[user]}",
            "Accept": ACCEPT_HEADER
        }

        # See delete_workflow: the previous global ilike lookup authorized nobody.
        project = _find_project_by_name(db, user, project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in database")

        _require_project_editor(db, user, project)
        reusable_repo_name = _require_owns_reusable_repo(db, project, user)

        if "/" not in reusable_repo_name:
            raise HTTPException(status_code=400, detail=f"Invalid reusable repository name: '{reusable_repo_name}'")

        results = _delete_workflow_directly(db, project, workflow_name, user, headers, [reusable_repo_name])
        return {"message": "✅ Reusable workflow deletion completed!", "results": results}

    except HTTPException:
        # Re-raise HTTPExceptions as-is (e.g., 401 Unauthorized)
        raise
    except Exception as e:
        print(f"❌ Error deleting reusable workflow: {str(e)}")
        raise HTTPException(status_code=500, detail="Error deleting reusable workflow")


@router.delete("/api/delete-db-workflow", responses=_responses(400, 401, 404, 409, 500))
async def delete_db_workflow(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """
    Deletes a workflow from the specified project and removes it completely 
    only if no other projects are using it.
    """
    try:
        data = await request.json()
        print(f"📌 Debug: Incoming Delete DB Workflow Request: {data}")

        user = account.github_user
        workflow_name = data.get("workflow_name", "").strip()
        project_name = data.get("project_name", "").strip()

        if not workflow_name:
            raise HTTPException(status_code=400, detail=MISSING_WORKFLOW_NAME_DETAIL)

        if user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)

        # Same unscoped lookup its sibling had: this removes another user's
        # workflow from their project, so it needs the authorizing resolver too.
        project = _find_project_by_name(db, user, project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in database")

        _require_project_editor(db, user, project)

        print(f"📌 Debug: Found Project '{project_name}' with ID {project.project_id}")

        # Scoped to this project, and matched exactly: ilike would treat the "_"
        # that workflow names routinely contain as a wildcard and could delete a
        # neighbouring row.
        workflow = db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id,
            func.lower(Workflow.workflow_name) == workflow_name.lower()
        ).first()
        
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_name}' not found in project '{project_name}'")

        print(f"📌 Debug: Found Workflow '{workflow_name}' with ID {workflow.workflow_id}")
        # Captured up front: the instance is detached after the commit below
        # when the workflow itself gets deleted.
        deleted_workflow_id = workflow.workflow_id

        _detach_workflow_from_project(db, project.project_id, workflow)

        # ✅ Commit changes
        db.commit()
        drop_workflow_drift(db, project, deleted_workflow_id)
        print(f"✅ Successfully removed workflow '{workflow_name}' from project '{project_name}'")

        return {"message": f"✅ Workflow '{workflow_name}' removed from project '{project_name}'!"}

    except HTTPException:
        # Re-raise HTTPExceptions as-is (e.g., 401 Unauthorized)
        raise
    except Exception as e:
        print(f"❌ Error deleting workflow from database: {str(e)}")
        db.rollback()  # Ensure database consistency
        raise HTTPException(status_code=500, detail="Error deleting workflow from database")


@router.post("/api/workflow-templates/generate", responses=_responses(500))
def generate_workflow_templates(request: Request, body: WorkflowTemplateRequest, db: Annotated[Session, Depends(get_db)]):
    """Generate a complete set of reusable workflow templates."""
    auth_module.resolve_authenticated_user(request, db)
    try:
        templates = generate_template_set(
            user_org=body.user_org,
            build_type=body.build_type,
            project_code=body.project_code
        )

        # Format templates for frontend consumption
        formatted_templates = []

        # Standard workflow template
        formatted_templates.append(TemplateResponse(
            template_type="standard",
            content=templates["standard_workflow"],
            name=f"standard-pipeline",
            description=f"Standard pipeline that calls reusable workflow for {body.build_type} projects"
        ))

        # Reusable workflow template
        formatted_templates.append(TemplateResponse(
            template_type="reusable",
            content=templates["reusable_workflow"],
            name=f"main-workflow",
            description=f"Main reusable workflow for {body.build_type} projects"
        ))

        # Build-specific workflow template
        formatted_templates.append(TemplateResponse(
            template_type="build",
            content=templates["build_workflow"],
            name=f"am-{body.build_type}-build",
            description=f"Build-specific workflow for {body.build_type} projects"
        ))

        return {
            "message": "Workflow templates generated successfully",
            "build_type": body.build_type,
            "user_org": body.user_org,
            "project_code": body.project_code,
            "templates": [template.model_dump() for template in formatted_templates]
        }

    except Exception as e:
        print(f"❌ Error generating workflow templates: {str(e)}")
        raise HTTPException(status_code=500, detail="Error generating workflow templates")


@router.get("/api/workflows-count", responses=_responses(409))
async def get_workflows_count(
    project_name: str,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Get the count of regular workflows for a project"""
    user = account.github_user
    try:
        print(f"📌 Getting workflows count for user={user}, project={project_name}")
        
        if user not in user_tokens:
            return {"error": NOT_AUTHENTICATED_DETAIL, "status": 401}
        
        project = _find_project_by_name(db, user, project_name)
        
        if not project:
            return {"error": PROJECT_ERROR, "status": 404}
        
        # Count regular workflows for this project
        workflow_count = count_project_workflows(user, project_name)
        
        print(f"✅ Found {workflow_count} regular workflows for project {project_name}")
        return {"count": workflow_count}
        
    except HTTPException:
        # Not a server error: the project lookup raises 409 for an ambiguous
        # name (#2240), and the catch-all below would report it as a 500.
        raise
    except Exception as e:
        print(f"❌ Error getting workflows count: {str(e)}")
        return {"error": "Error getting workflows count", "status": 500}


# ========== Workflow Version History API Endpoints ==========

class WorkflowVersionResponse(BaseModel):
    version_id: int
    version_number: int
    content: str
    metadata: Optional[str] = None
    created_at: str

class VersionHistoryResponse(BaseModel):
    workflow_id: int
    workflow_name: str
    versions: List[WorkflowVersionResponse]
    total_versions: int

class RestoreVersionRequest(BaseModel):
    project_name: str
    workflow_name: str
    version_id: int


class RenameImpactRequest(BaseModel):
    project_name: str
    workflow_name: str
    new_name: str
    # A project may own a regular *and* a reusable workflow under one name, and
    # create_or_update_workflow scopes every lookup by this. Without it the
    # preview's .first() can answer about the other one — a different delivery
    # repository and a different set of consumers. Optional so existing callers
    # keep working; when omitted, regular wins deterministically.
    is_reusable: Optional[bool] = None


class RenameImpactTarget(BaseModel):
    """One (repo, branch) a rename would have to reconcile.

    ``old_file_present`` / ``new_file_present`` are ``None`` when GitHub could
    not be asked — never ``False``. A failed listing is not the same fact as an
    empty directory, and reading it as one is what previously made every
    workflow in a repo look deleted (issue #1981).

    ``old_path`` is the file the next campaign removes there: where the
    delivery record says the workflow was delivered, unless that is the new
    path. None where it removes none.
    """
    repo: str
    branch: str
    old_file_present: Optional[bool] = None
    new_file_present: Optional[bool] = None
    confirmed_present_at: Optional[str] = None
    old_path: Optional[str] = None


class RenameImpactConsumer(BaseModel):
    project_name: str
    workflow_name: str
    uses_line: str


class RenameImpactResponse(BaseModel):
    classification: str  # never_delivered | delivered | unknown | blocked
    # "unknown" means GitHub could not be asked. A client must not treat it
    # as a safe default — it is precisely the state where nothing is known.
    blocked_reason: Optional[str] = None
    old_filename: str
    new_filename: str
    targets: List[RenameImpactTarget] = []
    consumers: List[RenameImpactConsumer] = []
    overrides: List[str] = []
    warnings: List[str] = []


def _rename_blocker_open_pr(db: Session, project: Project, workflow: Workflow) -> Optional[str]:
    """An open campaign carrying the workflow makes a rename unsafe.

    The campaign's branch holds the file under the old name, so merging it after
    the rename delivers a file no workflow is called any more, and the status
    restore at merge time no longer matches. For a reusable workflow the lock
    is global; for a regular one, this project's own campaigns are what carry it.
    """
    if workflow.reusable_workflow:
        if _reusable_workflow_ids_locked_by_open_campaign(db, [workflow.workflow_id]):
            return ("This reusable workflow is locked by an open PR campaign. "
                    "Merge or close it before renaming.")
        return None

    if deliveries.pending_anywhere(db, deliveries.WORKFLOW, workflow.workflow_id,
                                   pr_project_id=project.project_id):
        return ("An open pull request already carries this workflow. "
                "Merge or close it before renaming.")
    return None


def _rename_blocker_name_collision(db: Session, project: Project, workflow: Workflow,
                                   new_name: str) -> Optional[str]:
    """Another workflow in this project already owns the new name.

    Names are deliberately not globally unique, but two rows sharing a name
    inside one project make ``create_or_update_workflow``'s lookup ambiguous
    and would collide on the same delivered filename.

    Either kind clashes. A regular and a reusable workflow this project owns
    are both delivered to its own repositories (#2084), so they would be one
    file; ``_assert_name_free`` refuses the same thing for a
    new row (#2309).

    Tombstones are included, unlike the read paths. A ``pending_delete`` row
    still owns its name until its campaign lands, and the write path now
    refuses to take it (409) — so omitting them here would have the report call
    a rename safe that the save then rejects.
    """
    clash = (
        db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id,
            func.lower(Workflow.workflow_name) == new_name.lower(),
            Workflow.workflow_id != workflow.workflow_id,
        ).first()
    )
    if clash and clash.pending_delete:
        return (f"'{clash.workflow_name}' is queued for removal from GitHub by an open "
                f"PR campaign. Merge or close that campaign before reusing the name.")
    if clash:
        return f"This project already has a workflow named '{clash.workflow_name}'."
    return None


def _rename_consumers(db: Session, project: Project, workflow: Workflow,
                      user: str) -> List[RenameImpactConsumer]:
    """Caller workflows whose stored YAML references this reusable workflow.

    One entry per caller *workflow* that actually carries the ``uses:``
    reference, not per linking project: LinkedReusableWorkflow is unique on
    (standard_project_id, workflow_id), so counting rows reported "1 caller
    workflow" for a project with five of them and the user would fix one and
    break the rest.

    The reference is rendered from the *owning* project's naming mode and its
    reusable-workflow repository — the caller's own prefix setting and repos are
    not what the line points at (see ``_load_linked_reusable_workflows`` and
    ``_get_reusable_workflow_repo``).
    """
    if not workflow.reusable_workflow:
        return []

    # project_workflows.workflow_id is unique, so the workflow the caller
    # resolved is by definition the owning project — no second lookup needed.
    filename = format_workflow_name(
        workflow.workflow_name, (project.project_code or "").upper(), project.use_prefix
    )
    repo_ref = _get_reusable_workflow_repo(project, user, db) or "<reusable-workflow-repo>"
    reference = f"{repo_ref}/.github/workflows/{filename}"

    consumers: List[RenameImpactConsumer] = []
    caller_project_ids = [
        row.standard_project_id for row in
        db.query(LinkedReusableWorkflow)
        .filter(LinkedReusableWorkflow.workflow_id == workflow.workflow_id).all()
    ]
    for caller_id in caller_project_ids:
        caller = db.query(Project).filter_by(project_id=caller_id).first()
        if caller is None:
            continue
        caller_workflows = (
            db.query(Workflow).join(ProjectWorkflow).filter(
                ProjectWorkflow.project_id == caller_id,
                Workflow.pending_delete.is_(False),
            ).all()
        )
        for caller_workflow in caller_workflows:
            if reference not in (caller_workflow.workflow_yaml or ""):
                continue
            consumers.append(RenameImpactConsumer(
                project_name=caller.project_name,
                workflow_name=caller_workflow.workflow_name,
                uses_line=f"uses: {reference}@<ref>",
            ))
    return consumers


def _rename_blockers(db: Session, project: Project, workflow: Workflow, new_name: str,
                     consumers: List[RenameImpactConsumer], delivered: bool) -> Optional[str]:
    """The first reason this rename must be refused, or None.

    Shared by both callers so they cannot disagree: the read-only report
    disables the action and says why, and ``_assert_rename_allowed`` raises 409
    from ``create_or_update_workflow`` when a client renames anyway. Every
    check here is a database query, which is what lets the save path run it.
    """
    # A capitalisation-only change is not a rename ActionsManager can perform:
    # create_or_update_workflow decides with
    # `original_name.lower() != new_name.lower()`, so "Build" -> "build" is not
    # treated as a rename at all and the stored name never changes. Refusing is
    # honest; warning about consequences of a rename that silently no-ops is not.
    if (workflow.workflow_name.lower() == new_name.lower()
            and workflow.workflow_name != new_name):
        return _CASE_ONLY_RENAME_REFUSAL
    if workflow.pending_delete:
        return "This workflow is queued for removal from GitHub and cannot be renamed."
    if workflow.workflow_status == "under_review":
        return ("This workflow is under review. Merge or close its pull request "
                "before renaming.")

    blocker = _rename_blocker_open_pr(db, project, workflow)
    if blocker:
        return blocker

    blocker = _rename_blocker_name_collision(db, project, workflow, new_name)
    if blocker:
        return blocker

    if consumers and delivered:
        names = ", ".join(sorted({c.project_name for c in consumers}))
        return (f"{len(consumers)} caller workflow(s) in {names} reference this reusable "
                f"workflow by filename. Update their 'uses:' lines first — ActionsManager "
                f"does not rewrite caller YAML.")
    return None


def _branch_listing(db: Session, repo_name: str, owner: str, repo: str, branch: str,
                    token: str, warnings: List[str]) -> Optional[dict]:
    """The workflow-file listing for one (repo, branch), or None if unknown.

    Always revalidates through ``_fetch_tree_using_cache`` rather than replaying
    ``sha_map_json`` directly. Nothing invalidates that row when GitHub changes,
    so replaying it answers from a snapshot of whenever drift last ran — and a
    file added since then is invisible, which on this screen means delivering
    over someone else's workflow. Revalidating costs a round trip and **not**
    rate limit: the request carries If-None-Match and a 304 is free, which is
    the whole reason the ETag is stored.

    Returns None — never an empty dict — when GitHub could not be asked: a
    failed listing is not the same fact as a directory with no workflows, and
    the caller must not read one as the other.
    """
    try:
        return _fetch_tree_using_cache(db, repo_name, owner, repo, branch, token)
    except Exception as exc:
        # Broad by intent: any failure here is "unknown", and one unreachable
        # repository must not sink the whole preview.
        print(f"⚠️ Could not read {repo_name}@{branch}: {exc}")
        # The exception type only — these wrap DB reads and writes as well as
        # the GitHub call, and str(exc) on a SQLAlchemy error carries the
        # statement and its bound parameters into the user's browser.
        warnings.append(f"Could not read {repo_name}@{branch} ({type(exc).__name__})")
        return None


def _rename_impact_targets_for_repo(db: Session, project: Project, repo_row, owner: str,
                                    repo: str, old_filename: str, new_filename: str,
                                    token: str, user: str, headers: dict, confirmed: dict,
                                    warnings: List[str]) -> tuple:
    """One repository's (repo, branch) rows for the impact report.

    Returns ``(rows, assessed)``. ``assessed`` is False when the repository
    could not be evaluated at all, which the caller must not read as "the file
    is not there" — see ``_rename_impact_targets``.

    Branches are resolved the same way delivery resolves them, never from the
    repo's GitHub default branch. Resolved live rather than replayed from the
    last drift check: it records the branches drift last saw, so
    a branch added to the project since then would never be checked and a file
    the rename orphans there would be invisible.
    """
    repo_name = repo_row.repo_name
    try:
        branches = _resolve_drift_branches_for_repo(
            db, project, repo_name, owner, repo, headers, user
        )
    except Exception as exc:
        # Broad by intent, as above: report the repository and keep going,
        # but tell the caller this repository was never assessed.
        print(f"⚠️ Could not resolve branches for {repo_name}: {exc}")
        warnings.append(f"Could not resolve branches for {repo_name} ({type(exc).__name__})")
        return [], False

    rows = [
        _rename_impact_target(repo_name, branch, _branch_listing(db, repo_name, owner, repo, branch, token, warnings),
                              old_filename, new_filename, confirmed.get((repo_row.repo_id, branch or "")))
        for branch in branches
    ]
    return rows, True


def _rename_impact_target(repo_name: str, branch: str, listing: Optional[dict], old_filename: str,
                          new_filename: str, delivered: Optional[tuple]) -> RenameImpactTarget:
    """One (repo, branch) of the report, from its listing and *delivered*, the record's (delivered_at, path) there.

    The old file is the one the next campaign removes there
    (``deliveries.left_behind``, the rule the campaign's own removal applies),
    else the file under the current name.
    """
    stamped, delivered_path = delivered or (None, None)
    old_path = deliveries.left_behind(delivered_path, _WORKFLOWS_DIR + new_filename)
    old_name = old_path.rsplit("/", 1)[-1] if old_path else old_filename
    return RenameImpactTarget(
        repo=repo_name,
        branch=branch,
        old_file_present=None if listing is None else old_name in listing,
        new_file_present=None if listing is None else new_filename in listing,
        confirmed_present_at=_utc_iso(stamped),
        old_path=old_path,
    )


def _reusable_impact_targets(db: Session, project: Project, workflow: Workflow,
                             old_filename: str, new_filename: str, token: str,
                             user: str, headers: dict, confirmed: dict) -> tuple:
    """Targets for a reusable workflow, resolved the way its delivery resolves.

    Mirrors ``_process_reusable_workflows``: one repository from
    ``_get_reusable_workflow_repo`` at that repo's default branch, rather than
    the owning project's ProjectRepo rows and branch config.
    """
    repo_name = _get_reusable_workflow_repo(project, user, db)
    warnings: List[str] = []
    if not repo_name or "/" not in repo_name:
        warnings.append("Could not determine the reusable workflow repository.")
        return [], warnings, [repo_name or "<unknown>"]

    owner, repo = repo_name.split("/", 1)
    try:
        branch = get_default_branch(owner, repo, headers, user, db)
    except Exception as exc:
        # Unknown, not absent — the caller must not read this as "not delivered".
        print(f"⚠️ Could not resolve the default branch for {repo_name}: {exc}")
        warnings.append(f"Could not resolve the default branch for {repo_name} ({type(exc).__name__})")
        return [], warnings, [repo_name]

    listing = _branch_listing(db, repo_name, owner, repo, branch, token, warnings)
    repo_row = db.query(Repo).filter(Repo.repo_name == repo_name).first()
    delivered = confirmed.get((repo_row.repo_id, branch or "")) if repo_row else None
    return [_rename_impact_target(repo_name, branch, listing, old_filename, new_filename, delivered)], warnings, []


def _rename_impact_targets(db: Session, project: Project, workflow: Workflow,
                           old_filename: str, new_filename: str, token: str,
                           user: str) -> tuple:
    """Per (repo, branch) presence of the old and new filenames.

    Returns ``(targets, warnings, unassessed_repos)``. ``unassessed_repos`` names
    the repositories that produced no rows because they could not be evaluated —
    without it those repositories vanish silently and the caller mistakes
    "we never looked" for "nothing is there".
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION,
    }
    confirmed = deliveries.delivered_at_by_target(db, deliveries.WORKFLOW, workflow.workflow_id)

    if workflow.reusable_workflow:
        # Reusable workflows are not delivered to the project's own repositories.
        # Delivery and drift both resolve them through _get_reusable_workflow_repo
        # (see _process_reusable_workflows), which for an RWX project is its own
        # repo, for a caller the linked RWX project's, and otherwise the
        # {user}/am-reuseable-workflow fallback. Scanning ProjectRepo instead
        # produced no rows at all for an RWX project with no ProjectRepo row —
        # and no rows reads as "not delivered", which silently releases the
        # linked-consumer refusal this report exists to apply.
        return _reusable_impact_targets(
            db, project, workflow, old_filename, new_filename, token, user, headers, confirmed
        )
    repo_rows = (
        db.query(Repo).join(ProjectRepo).filter(ProjectRepo.project_id == project.project_id).all()
    )
    targets: List[RenameImpactTarget] = []
    warnings: List[str] = []
    unassessed: List[str] = []
    for repo_row in repo_rows:
        if "/" not in repo_row.repo_name:
            warnings.append(f"Skipped '{repo_row.repo_name}': not in owner/repo form.")
            unassessed.append(repo_row.repo_name)
            continue
        owner, repo = repo_row.repo_name.split("/", 1)
        rows, assessed = _rename_impact_targets_for_repo(
            db, project, repo_row, owner, repo, old_filename, new_filename,
            token, user, headers, confirmed, warnings,
        )
        targets.extend(rows)
        if not assessed:
            unassessed.append(repo_row.repo_name)
    return targets, warnings, unassessed


def _rename_impact_warnings(project: Project,
                            targets: List[RenameImpactTarget]) -> List[str]:
    """Consequences worth stating that are not blockers."""
    warnings: List[str] = []

    if not project.use_prefix:
        # A file already sitting at the new path is not this project's: a
        # workflow of ours owning that name would have been refused as a name
        # collision before we got here. Whether the *old* file is still there
        # is irrelevant — the overwrite happens either way.
        colliding = sorted({t.repo for t in targets if t.new_file_present})
        if colliding:
            warnings.append(
                f"This project does not use the AM_ prefix, and a file with the new name "
                f"already exists in {', '.join(colliding)} that ActionsManager has not "
                f"delivered. Delivering would overwrite it."
            )

    warnings.append(
        "Build metrics recorded under the old filename stay attached to it, so this "
        "workflow's run history will show a break at the rename."
    )
    return warnings


@router.post("/api/workflows/rename-impact", responses=_responses(400, 401, 403, 404, 409, 500))
def workflow_rename_impact(
    payload: RenameImpactRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """What renaming a workflow would do, without doing any of it.

    No project state is changed and no pull request is opened. Not literally
    write-free, though: revalidating a listing refreshes ``workflow_tree_cache``
    and branch-recency rows on this session, the same rows a drift check
    maintains. Renaming
    changes the delivered filename, so the old file stays in every repository
    under its old name unless something removes it — this is what the
    confirmation dialog shows before the user commits to that.

    Cost: the same as one drift check — a branch resolution per repository plus
    one listing per (repo, branch). Those listings are **conditional**: each
    carries ``If-None-Match`` from the stored ETag, and an unchanged branch
    answers 304, which does not count against the rate limit. Presence is
    deliberately not replayed from ``workflow_tree_cache`` without
    revalidating, and branches are deliberately not replayed from the last
    drift check: both are snapshots of whenever drift last ran,
    and answering from them hides a file or a branch added since.
    """
    github_user = account.github_user
    token, project, project_code = _validate_user_and_get_project(
        db, github_user, payload.project_name
    )
    _require_project_editor(db, github_user, project)

    # Both names go through the same normalisation the write path applies
    # (create_or_update_workflow strips the prefix from original_name and
    # new_name alike). Stripping only one made the preview 404 on the displayed
    # "AM_CODE_name" while the rename itself would have found the row.
    old_name = _validate_workflow_name(_strip_duplicated_project_prefix(
        db, project.project_id, payload.workflow_name
    ))
    new_name = _validate_workflow_name(_strip_duplicated_project_prefix(
        db, project.project_id, payload.new_name
    ))

    lookup = db.query(Workflow).join(ProjectWorkflow).filter(
        ProjectWorkflow.project_id == project.project_id,
        func.lower(Workflow.workflow_name) == old_name.lower(),
        Workflow.pending_delete.is_(False),
    )
    if payload.is_reusable is not None:
        lookup = lookup.filter((Workflow.reusable_workflow.is_(True) if payload.is_reusable
             # isnot(True) matches legacy rows where the column is NULL,
             # which is the convention everywhere else in this file. is_(False)
             # missed them, 404ing the preview for a workflow the save can see.
             else Workflow.reusable_workflow.isnot(True)))
    # Ordered so an unscoped request resolves the regular workflow rather than
    # whichever row the database happened to return first.
    workflow = lookup.order_by(Workflow.reusable_workflow).first()
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{old_name}' not found in this project")

    old_filename = format_workflow_name(workflow.workflow_name, project_code, project.use_prefix)
    new_filename = format_workflow_name(new_name, project_code, project.use_prefix)

    targets, target_warnings, unassessed = _rename_impact_targets(
        db, project, workflow, old_filename, new_filename, token, github_user
    )
    consumers = _rename_consumers(db, project, workflow, github_user)

    # "Delivered" is a claim about history: a file seen at the old path now, or
    # a check that once confirmed it there. workflow_git_hash cannot answer it —
    # it is zeroed by any local edit and set to a PR branch SHA by a campaign
    # that has not landed.
    #
    # Three states, not two. `old_file_present` is None when GitHub could not be
    # asked, and None is falsy — so folding it into a boolean would turn an
    # outage into "never delivered" and hand back a rename that should have been
    # refused. Not knowing is its own answer.
    known_delivered = any(
        t.old_file_present is True or t.confirmed_present_at for t in targets
    )
    unknown = bool(unassessed) or any(t.old_file_present is None for t in targets)

    override_repos = sorted({
        name for (name,) in db.query(Repo.repo_name).join(
            RepoWorkflowOverride, RepoWorkflowOverride.repo_id == Repo.repo_id
        ).filter(
            RepoWorkflowOverride.project_id == project.project_id,
            RepoWorkflowOverride.workflow_id == workflow.workflow_id,
        ).all()
    })

    # Blockers see unknown as possibly-delivered, so a GitHub outage cannot
    # quietly release the reusable-workflow refusal.
    blocked_reason = _rename_blockers(
        db, project, workflow, new_name, consumers,
        delivered=known_delivered or unknown,
    )
    if blocked_reason:
        classification = "blocked"
    elif known_delivered:
        classification = "delivered"
    elif unknown:
        classification = "unknown"
    else:
        classification = "never_delivered"

    return RenameImpactResponse(
        classification=classification,
        blocked_reason=blocked_reason,
        old_filename=old_filename,
        new_filename=new_filename,
        targets=targets,
        consumers=consumers,
        overrides=override_repos,
        warnings=target_warnings + _rename_impact_warnings(project, targets),
    )


@router.get("/api/workflows/{workflow_name}/versions", responses=_responses(401, 404, 409, 500))
async def get_workflow_versions(
    workflow_name: str,
    project_name: str,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Get all version history for a specific workflow"""
    user = account.github_user
    try:
        print(f"📌 Getting version history for workflow '{workflow_name}' in project '{project_name}'")
        
        if user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
        
        project = _find_project_by_name(db, user, project_name)
        
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)
        
        # Find workflow within this project
        workflow = db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id,
            Workflow.workflow_name == workflow_name
        ).first()
        
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_name}' not found in project")
        
        # Get all versions for this workflow
        versions = db.query(WorkflowVersion).filter(
            WorkflowVersion.workflow_id == workflow.workflow_id
        ).order_by(WorkflowVersion.version_number.desc()).all()
        
        version_list = [
            WorkflowVersionResponse(
                version_id=v.version_id,
                version_number=v.version_number,
                content=v.content,
                metadata=v.version_metadata,
                created_at=v.created_at.isoformat() if v.created_at else None
            )
            for v in versions
        ]
        
        print(f"✅ Found {len(versions)} versions for workflow '{workflow_name}'")
        
        return VersionHistoryResponse(
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.workflow_name,
            versions=version_list,
            total_versions=len(versions)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error getting workflow versions: {str(e)}")
        raise HTTPException(status_code=500, detail="Error getting workflow versions")


@router.post("/api/workflows/restore-version", responses=_responses(401, 404, 409, 500))
async def restore_workflow_version(
    payload: RestoreVersionRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Restore a workflow to a previous version"""
    github_user = account.github_user
    try:
        print(f"📌 Restoring workflow '{payload.workflow_name}' to version {payload.version_id}")
        
        if github_user not in user_tokens:
            raise HTTPException(status_code=401, detail=NOT_AUTHENTICATED_DETAIL)
        
        project = _find_project_by_name(db, github_user, payload.project_name)
        
        if not project:
            raise HTTPException(status_code=404, detail=PROJECT_ERROR)

        # Restoring overwrites workflow_yaml with a stored version and clears
        # the git hash, so it is a write like any other.
        _require_project_editor(db, github_user, project)

        # Find workflow within this project
        workflow = db.query(Workflow).join(ProjectWorkflow).filter(
            ProjectWorkflow.project_id == project.project_id,
            Workflow.workflow_name == payload.workflow_name
        ).first()
        
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow '{payload.workflow_name}' not found")
        
        # Get the version to restore
        version = db.query(WorkflowVersion).filter(
            WorkflowVersion.version_id == payload.version_id,
            WorkflowVersion.workflow_id == workflow.workflow_id
        ).first()
        
        if not version:
            raise HTTPException(status_code=404, detail=f"Version {payload.version_id} not found")
        
        # Restore the workflow content from this version
        workflow.workflow_yaml = version.content
        workflow.workflow_git_hash = None
        db.commit()
        resolve_drift_for_local_edit(db, project, deliveries.WORKFLOW, workflow.workflow_id, version.content)
        
        # Create a new version entry to track this restoration
        create_workflow_version(
            db,
            workflow.workflow_id,
            version.content,
            metadata={
                'action': 'restore',
                'restored_from_version': version.version_number,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
        )
        
        print(f"✅ Restored workflow '{payload.workflow_name}' to version {version.version_number}")
        
        return {
            "message": f"Workflow restored to version {version.version_number}",
            "workflow_name": workflow.workflow_name,
            "restored_version": version.version_number,
            "restored_content": version.content
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error restoring workflow version: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Error restoring workflow version")
