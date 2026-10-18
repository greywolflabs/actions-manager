"""
Custom Files API for ActionsManager

Manages workflow-adjacent text files (scripts, action definitions, config
files, etc.) at the project level.  Custom files are deployed to every
repository in a project alongside workflow YAML files and participate in the
same PR Campaign / drift-detection lifecycle.

Endpoints:
  GET    /api/projects/{project_id}/custom-files            – list all
  POST   /api/projects/{project_id}/custom-files            – create
  PUT    /api/projects/{project_id}/custom-files/{file_id}  – update
  DELETE /api/projects/{project_id}/custom-files/{file_id}  – remove from the project, optionally from GitHub too
  POST   /api/projects/{project_id}/custom-files/{file_id}/restore – cancel pending delete
  GET    /api/projects/{project_id}/custom-files/{file_id}/github  – its copy on one (repo, branch), to load

Security: path validation is enforced server-side on every create/update.
File content is never logged.
"""

from typing import Annotated, List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth as auth_module
from auth import user_tokens
from database import get_db
import deliveries
from drift_notifications import drop_drift, resolve_drift_for_local_edit
from models import Account, CustomFile, Project, ProjectRepo
from authorization import has_project_role, project_role

router = APIRouter()


# Which copies of a custom file a delete destroys. "project" removes only the
# ActionsManager record and leaves the committed file in GitHub;
# "project_and_github" also removes it from the repositories, now rather than on
# some later delivery. It stays the default, so a parameterless DELETE reaches
# GitHub — see `delivery` below for how.
_SCOPE_PROJECT = "project"
_SCOPE_PROJECT_AND_GITHUB = "project_and_github"
_VALID_SCOPES = (_SCOPE_PROJECT, _SCOPE_PROJECT_AND_GITHUB)

# How a GitHub-scoped removal reaches the repositories. "campaign" runs a real
# PR Campaign for the deletion, so its pull requests are grouped, tracked and
# merged like every other delivery this product makes; "direct" commits the
# removal straight to each target branch. Neither defers the work — the dialog
# offers to delete from GitHub, so the request has to reach GitHub now.
_DELIVERY_CAMPAIGN = "campaign"
_DELIVERY_DIRECT = "direct"
_VALID_DELIVERIES = (_DELIVERY_CAMPAIGN, _DELIVERY_DIRECT)


# ── Path validation ──────────────────────────────────────────────────────────

_BLOCKED_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".crt", ".cert", ".jks"}


def validate_file_path(path: str) -> Optional[str]:
    """Return an error message string, or None when the path is safe."""
    if not path or not path.strip():
        return "File path is required"
    path = path.strip()
    if path.startswith("/"):
        return "Absolute paths are not allowed"
    # The stored path is interpolated into a GitHub contents URL, where the
    # server percent-decodes it. Without this, "%2e%2e" survives the ".." check
    # below and still reaches GitHub as "..", escaping the contents endpoint.
    if "%" in path:
        return "Percent-encoded characters are not allowed in a file path"
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        return "Path traversal (..) is not allowed"
    if parts[0] == ".git" or ".git" in parts[1:]:
        return ".git/ paths are not allowed"
    basename = parts[-1].lower()
    if basename == ".env" or basename.startswith(".env."):
        return ".env files are not allowed"
    lower = path.lower()
    for ext in _BLOCKED_EXTENSIONS:
        if lower.endswith(ext):
            return f"{ext} files are not allowed (may contain secrets)"
    return None


# ── Auth helpers ─────────────────────────────────────────────────────────────

# Error responses these endpoints can return, declared on each route so they
# appear in the OpenAPI schema (and so generated clients know about them).
# Codes raised inside shared helpers count too - the rule tracks the call.
_ERROR_RESPONSES = {
    400: {"description": "Invalid request"},
    401: {"description": "Not authenticated"},
    403: {"description": "Access denied"},
    404: {"description": "Not found"},
    409: {"description": "Conflicts with an existing custom file"},
    502: {"description": "GitHub rejected part of the request"},
}


def _responses(*codes: int) -> dict:
    """Subset of _ERROR_RESPONSES for a route's `responses=` parameter."""
    return {code: _ERROR_RESPONSES[code] for code in codes}


def _resolve_user(account) -> str:
    """The session's caller (#2241), raising 401 when no GitHub token is held for them."""
    if account.github_user not in user_tokens:
        raise HTTPException(status_code=401, detail="User not authenticated")
    return account.github_user


def _get_project_for_user(db: Session, project_id: int, github_user: str) -> Project:
    """Return the project if the caller may see it, else raise 401/404/403 (see authorization.project_role)."""
    account = db.query(Account).filter_by(github_user=github_user).first()
    if not account:
        raise HTTPException(status_code=401, detail="User not authenticated")

    project = db.query(Project).filter_by(project_id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project_role(db, account, project) is None:
        raise HTTPException(status_code=403, detail="Access denied")
    return project


_ERR_NOT_FOUND = "Custom file not found"
_ERR_NOT_EDITOR = "Editor access to this project is required to change it on GitHub"


def _require_project_editor(db: Session, project: Project, github_user: str) -> None:
    """Require project_editor before a removal is allowed to reach GitHub (see authorization.project_role)."""
    account = db.query(Account).filter_by(github_user=github_user).first()
    if not has_project_role(db, account, project, "project_editor"):
        raise HTTPException(status_code=403, detail=_ERR_NOT_EDITOR)

# ── Project state helper ─────────────────────────────────────────────────────

def _current_pr_state(db: Session, project_id: int) -> Optional[str]:
    """The project's pr_state, for a mutation to hand back.

    Only the project-load endpoint computes it otherwise, so a caller that does
    not return it leaves Create PR Campaign stale until the user refreshes.
    """
    project = db.query(Project).filter_by(project_id=project_id).first()
    return project.pr_state if project else None


def _mark_project_draft(db: Session, project_id: int) -> Optional[str]:
    """Promote project pr_state to 'draft' when a custom file change is saved.

    Mirrors the same promotion that workflow saves perform so that the Create
    PR Campaign button becomes available without touching a workflow first.

    Returns the resulting pr_state so a mutation can hand it back — the value is
    otherwise only computed by the project-load endpoint, and the button stays
    disabled until the user refreshes.
    """
    project = db.query(Project).filter_by(project_id=project_id).first()
    if not project:
        return None
    if project.pr_state in ("new", "synced"):
        project.pr_state = "draft"
        db.commit()
    return project.pr_state


# ── Serialiser ───────────────────────────────────────────────────────────────

def _serialize(cf: CustomFile) -> dict:
    return {
        "id": cf.id,
        "project_id": cf.project_id,
        "display_name": cf.display_name,
        "file_path": cf.file_path,
        "file_content": cf.file_content,
        "git_hash": cf.git_hash,
        "file_status": cf.file_status,
        "pending_delete": cf.pending_delete,
        "last_modified_by": cf.last_modified_by,
        "description": cf.description,
        "created_at": cf.created_at.isoformat() if cf.created_at else None,
        "updated_at": cf.updated_at.isoformat() if cf.updated_at else None,
    }


# ── Request schemas ───────────────────────────────────────────────────────────

class CreateCustomFileRequest(BaseModel):
    display_name: Optional[str] = None
    file_path: str
    file_content: str = ""
    description: Optional[str] = None


class UpdateCustomFileRequest(BaseModel):
    display_name: Optional[str] = None
    file_path: Optional[str] = None
    file_content: Optional[str] = None
    description: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/projects/{project_id}/custom-files", responses=_responses(401, 403, 404))
def list_custom_files(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    user = _resolve_user(account)
    _get_project_for_user(db, project_id, user)
    files = db.query(CustomFile).filter_by(project_id=project_id).all()
    return {"custom_files": [_serialize(f) for f in files]}


@router.post("/api/projects/{project_id}/custom-files", responses=_responses(400, 401, 403, 404, 409))
def create_custom_file(
    project_id: int,
    payload: CreateCustomFileRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    user = _resolve_user(account)
    _project = _get_project_for_user(db, project_id, user)

    # Creating, editing and restoring a custom file all change what this
    # project delivers, so seeing the project is not enough - the resolve
    # above proves only that. delete_custom_file already carried this.
    _require_project_editor(db, _project, user)

    path_error = validate_file_path(payload.file_path)
    if path_error:
        raise HTTPException(status_code=400, detail=path_error)

    existing = db.query(CustomFile).filter_by(
        project_id=project_id, file_path=payload.file_path.strip()
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A custom file already exists at path '{payload.file_path}' in this project",
        )

    cf = CustomFile(
        project_id=project_id,
        display_name=payload.display_name,
        file_path=payload.file_path.strip(),
        file_content=payload.file_content,
        description=payload.description,
        file_status="new",
        last_modified_by=user,
    )
    db.add(cf)
    db.commit()
    db.refresh(cf)
    _mark_project_draft(db, project_id)
    return {"custom_file": _serialize(cf)}


def _move_custom_file(db: Session, cf: CustomFile, project_id: int, file_path: Optional[str]) -> bool:
    """Move *cf* to *file_path* when that is a new path; returns whether it moved."""
    if file_path is None or file_path.strip() == cf.file_path:
        return False
    path_error = validate_file_path(file_path)
    if path_error:
        raise HTTPException(status_code=400, detail=path_error)
    conflict = db.query(CustomFile).filter_by(
        project_id=project_id, file_path=file_path.strip()
    ).filter(CustomFile.id != cf.id).first()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"A custom file already exists at path '{file_path}' in this project",
        )
    cf.file_path = file_path.strip()
    return True


@router.put("/api/projects/{project_id}/custom-files/{file_id}", responses=_responses(400, 401, 403, 404, 409))
def update_custom_file(
    project_id: int,
    file_id: int,
    payload: UpdateCustomFileRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    user = _resolve_user(account)
    _project = _get_project_for_user(db, project_id, user)

    # Creating, editing and restoring a custom file all change what this
    # project delivers, so seeing the project is not enough - the resolve
    # above proves only that. delete_custom_file already carried this.
    _require_project_editor(db, _project, user)

    cf = db.query(CustomFile).filter_by(id=file_id, project_id=project_id).first()
    if not cf:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)

    # Whether the path changed is knowable only before the new one is assigned.
    moved = _move_custom_file(db, cf, project_id, payload.file_path)

    if payload.display_name is not None:
        cf.display_name = payload.display_name
    if payload.description is not None:
        cf.description = payload.description

    content_changed = payload.file_content is not None and payload.file_content != cf.file_content
    if content_changed:
        cf.file_content = payload.file_content

    if content_changed or moved:
        cf.file_status = "committed_locally"
        cf.git_hash = None

    cf.last_modified_by = user
    cf.updated_at = datetime.now(timezone.utc)

    db.commit()
    if content_changed:
        # Resolves only drift whose GitHub copy the edit now is, e.g. Load GitHub version then Commit Locally.
        resolve_drift_for_local_edit(db, _project, deliveries.CUSTOM_FILE, cf.id, cf.file_content)
    db.refresh(cf)
    _mark_project_draft(db, project_id)
    return {"custom_file": _serialize(cf)}


def _hard_delete(db: Session, project: Project, cf: CustomFile, targets: Optional[List[dict]] = None) -> dict:
    project_id = cf.project_id
    drop_drift(db, project, deliveries.CUSTOM_FILE, cf.id)
    db.delete(cf)
    db.commit()
    response = {
        "deleted": True,
        "hard_deleted": True,
        "pr_state": _current_pr_state(db, project_id),
    }
    if targets is not None:
        response["targets"] = targets
    return response


def _campaign_errors(results: dict) -> List[str]:
    """Per-target reasons from a campaign result, labelled by target."""
    reasons = []
    for target, outcome in results.items():
        if not isinstance(outcome, dict) or outcome.get("status") != "error":
            continue
        reasons.append(f"{target}: {outcome.get('error') or 'no reason reported'}")
    return reasons


def _delete_via_campaign(db: Session, project: Project, cf: CustomFile, user: str) -> dict:
    """Run a PR Campaign whose only change is removing this file.

    Each pull request removes the file where the delivery record says it is,
    wherever it was moved since, and a ProjectPRCampaign links them
    (``workflows._run_removal_campaign``). Going through a campaign rather than
    opening loose pull requests is what puts the deletion in the PR Campaigns
    view with the grouping, status and merge that every other delivery gets.

    The row stays until those PRs merge — that is what Restore cancels and what
    the campaign's synced_with_github transition cleans up.
    """
    from workflows import ACCEPT_HEADER, X_API_VERSION, _removal_targets, _run_removal_campaign

    # A project with no repositories has nowhere to deliver to, so a campaign
    # would have no target and fail with "nothing was selected". There is no
    # GitHub copy this project can reach: drop the record.
    if not db.query(ProjectRepo).filter_by(project_id=project.project_id).first():
        return _hard_delete(db, project, cf)

    # _resolve_user saw this token.
    headers = {"Accept": ACCEPT_HEADER, "X-GitHub-Api-Version": X_API_VERSION, "Authorization": f"token {user_tokens[user]}"}
    # Before the mark below: its status reads as a delivery made before the
    # record existed, and a rejection after it would leave the file
    # pending_delete for a campaign that never ran.
    targets, failures = _removal_targets(db, project, [cf], user, headers)
    if not targets and not failures:
        # The record holds no copy anywhere, so there is nothing to review.
        return _hard_delete(db, project, cf)

    previous_status = cf.file_status
    cf.pending_delete = True
    cf.file_status = "committed_locally"
    cf.last_modified_by = user
    cf.updated_at = datetime.now(timezone.utc)
    db.commit()

    def _unmark():
        # Leaving the row marked for a deletion that never got delivered would
        # strand it in "pending delete" — the state this whole change exists to
        # get rid of.
        cf.pending_delete = False
        cf.file_status = previous_status
        db.commit()

    try:
        campaign = _run_removal_campaign(db, project, cf, targets, failures, user, headers)
    except Exception:
        _unmark()
        raise

    # A campaign can open nothing — every target failing branch creation, say.
    # Without this the file would sit pending_delete with no pull request that
    # will ever remove it.
    if not campaign["prs_created"]:
        _unmark()
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"No pull request was opened to remove '{cf.file_path}'",
                "errors": _campaign_errors(campaign["results"]),
                "targets": campaign["results"],
            },
        )

    # The check skips a file on its way out, so drift it found before would stay counted.
    drop_drift(db, project, deliveries.CUSTOM_FILE, cf.id)
    db.refresh(cf)
    return {
        "deleted": False,
        "pending_delete": True,
        "custom_file": _serialize(cf),
        "pr_state": _current_pr_state(db, project.project_id),
        "campaign_id": campaign["campaign_id"],
        "prs_created": campaign["prs_created"],
        "results": campaign["results"],
    }


@router.delete("/api/projects/{project_id}/custom-files/{file_id}", responses=_responses(400, 401, 403, 404, 409, 502))
def delete_custom_file(
    project_id: int,
    file_id: int,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    scope: Annotated[str, Query(description="'project' or 'project_and_github'")] = _SCOPE_PROJECT_AND_GITHUB,
    delivery: Annotated[str, Query(description="'campaign' or 'direct'; only used when scope includes GitHub")] = _DELIVERY_CAMPAIGN,
):
    user = _resolve_user(account)
    project = _get_project_for_user(db, project_id, user)

    if scope not in _VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of {', '.join(_VALID_SCOPES)}",
        )
    if delivery not in _VALID_DELIVERIES:
        raise HTTPException(
            status_code=400,
            detail=f"delivery must be one of {', '.join(_VALID_DELIVERIES)}",
        )

    cf = db.query(CustomFile).filter_by(id=file_id, project_id=project_id).first()
    if not cf:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)

    # Every scope deletes something, and _get_project_for_user lets any full
    # member see every project, so seeing it is not enough for any of them.
    _require_project_editor(db, project, user)

    # A project-scoped removal leaves nothing to deliver, and a file that was
    # created but never saved to GitHub has no copy there to chase, so both drop
    # the row outright without touching GitHub.
    if scope == _SCOPE_PROJECT or cf.file_status == "new":
        return _hard_delete(db, project, cf)

    if delivery == _DELIVERY_CAMPAIGN:
        return _delete_via_campaign(db, project, cf, user)

    # Imported from workflows lazily: workflows.py owns every GitHub helper and
    # imports nothing from here, so a module-level import would only add a cycle
    # risk for one call.
    from workflows import delete_custom_file_directly

    outcome = delete_custom_file_directly(db, project, cf, user)

    if outcome["errors"]:
        # Keep the row. Dropping it here would leave the file in the repositories
        # that failed with nothing in ActionsManager still pointing at it.
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Could not remove '{cf.file_path}' from every repository",
                "errors": outcome["errors"],
                "targets": outcome["targets"],
            },
        )

    return _hard_delete(db, project, cf, outcome["targets"])


@router.post("/api/projects/{project_id}/custom-files/{file_id}/restore", responses=_responses(401, 403, 404))
def restore_custom_file(
    project_id: int,
    file_id: int,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    user = _resolve_user(account)
    _project = _get_project_for_user(db, project_id, user)

    # Creating, editing and restoring a custom file all change what this
    # project delivers, so seeing the project is not enough - the resolve
    # above proves only that. delete_custom_file already carried this.
    _require_project_editor(db, _project, user)

    cf = db.query(CustomFile).filter_by(id=file_id, project_id=project_id).first()
    if not cf:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)

    cf.pending_delete = False
    cf.last_modified_by = user
    cf.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cf)
    return {"custom_file": _serialize(cf)}


@router.get("/api/projects/{project_id}/custom-files/{file_id}/github", responses=_responses(401, 403, 404, 502))
def get_custom_file_on_github(
    project_id: int,
    file_id: int,
    repo: Annotated[str, Query(description="The repository's owner/name or repo_id; one of the project's")],
    branch: Annotated[str, Query(description="The branch to read, e.g. the one a drift row names")],
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """GitHub's copy of a custom file on one (repo, branch), to load into the editor.

    Read where its drift check reads it, so Load GitHub version shows the copy
    that drifted. One contents call with the caller's token; seeing the
    project is enough, as reading its stored files is.
    """
    # Lazily: codeowners and workflows both import this module.
    from codeowners import _resolve_repo_in_project
    from workflows import DriftCheckUnavailable, custom_file_on_github

    user = _resolve_user(account)
    project = _get_project_for_user(db, project_id, user)
    cf = db.query(CustomFile).filter_by(id=file_id, project_id=project_id).first()
    if not cf:
        raise HTTPException(status_code=404, detail=_ERR_NOT_FOUND)
    target_repo = _resolve_repo_in_project(db, project, repo)
    try:
        found, path = custom_file_on_github(db, cf, target_repo, branch, user_tokens[user])
    except DriftCheckUnavailable as e:
        raise HTTPException(status_code=502, detail=f"GitHub could not be read (HTTP {e.status_code})") from e
    return {
        "repo_id": target_repo.repo_id,
        "repo_name": target_repo.repo_name,
        "branch": branch,
        "github": {
            "exists": found is not None,
            "content": found["content"] if found else None,
            "sha": found["sha"] if found else None,
            "path": path,
        },
    }
