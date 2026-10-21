"""
CODEOWNERS Management API for ActionsManager

A project owns one CODEOWNERS file, delivered to every repository and branch the
project delivers to by its PR campaigns, the way its workflows and custom files
are. Drift is checked with the project's workflows (``workflows.run_project_drift_check``)
and follows the same rules.

* GET  /api/projects/{project_id}/codeowners   – the project's CODEOWNERS
* PUT  /api/projects/{project_id}/codeowners   – save it locally (Commit Locally)
* GET  /api/repos/{repo_ref}/codeowners        – a repository's copy on GitHub, to import or load

Callers come from the session (#2241), and ownership and RBAC checks run for
the signed-in user with the same helpers as the rest of the application.
"""

import base64
from typing import Annotated, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth as auth_module
from auth import user_tokens
from config import GITHUB_TIMEOUT_SECONDS
from custom_files import _get_project_for_user, _mark_project_draft
from database import get_db
import deliveries
from drift_notifications import resolve_drift_for_local_edit
from file_kinds import FILE_KINDS, refuse_unless_accepted
from models import Codeowners, Project, ProjectRepo, Repo
from workflows import (
    _find_project_by_name,
    _project_codeowners,
    _require_project_editor,
)


router = APIRouter()

GITHUB_API_URL = "https://api.github.com"
ACCEPT_HEADER = "application/vnd.github+json"
X_API_VERSION = "2022-11-28"

# CODEOWNERS' key in file_kinds.FILE_KINDS. Only a Managed Files project of
# this kind holds the file (#2367).
KIND = "codeowners"

# Every location GitHub reads CODEOWNERS from, in the order it looks. The kind
# definition is the one list; the editor and the import read it too.
ALLOWED_PATHS = FILE_KINDS[KIND].paths


# --------------------------------------------------------------------------- #
# Pydantic models                                                             #
# --------------------------------------------------------------------------- #


class CodeownersSaveRequest(BaseModel):
    """Body for saving the project's CODEOWNERS."""
    content: str
    file_path: Optional[str] = ALLOWED_PATHS[0]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _validate_file_path(path: Optional[str]) -> str:
    """Return a validated CODEOWNERS path, defaulting to ``.github/CODEOWNERS``."""
    if not path:
        return ALLOWED_PATHS[0]
    cleaned = path.strip().lstrip("/")
    if cleaned not in ALLOWED_PATHS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file_path; must be one of {ALLOWED_PATHS}",
        )
    return cleaned


def _resolve_repo_in_project(db: Session, project: Project, repo_ref: str) -> Repo:
    """Return the Repo if it belongs to *project*, else raise 404.

    ``repo_ref`` may be either the numeric ``repo_id`` (as a string) or the
    full ``owner/repo`` name.  This dual lookup keeps the URL stable while
    matching how the rest of the application identifies repositories.
    """
    query = (
        db.query(Repo)
        .join(ProjectRepo, ProjectRepo.repo_id == Repo.repo_id)
        .filter(ProjectRepo.project_id == project.project_id)
    )
    if repo_ref.isdigit():
        repo = query.filter(Repo.repo_id == int(repo_ref)).first()
    else:
        repo = query.filter(Repo.repo_name == repo_ref).first()
    if not repo:
        raise HTTPException(
            status_code=404,
            detail=f"Repository {repo_ref!r} not found in project '{project.project_name}'",
        )
    return repo


def _resolve_caller_and_project(db: Session, github_user: str, project_name: str):
    """Resolve the session caller's token and the project, raising HTTPException on failure."""
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail="User not authenticated")
    token = user_tokens[github_user]
    project = _find_project_by_name(db, github_user, project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    return token, project


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION,
    }


def _split_repo(repo_name: str):
    """Split ``owner/repo`` safely."""
    if not repo_name or "/" not in repo_name:
        raise HTTPException(status_code=400, detail=f"Invalid repository name: {repo_name!r}")
    owner, repo = repo_name.split("/", 1)
    return owner.strip(), repo.strip()


def _fetch_github_codeowners(owner: str, repo: str, headers: dict, branch: Optional[str] = None,
                             paths: tuple = ALLOWED_PATHS):
    """
    Fetch CODEOWNERS from GitHub.  Tries *paths* in priority order and returns
    ``(content, sha, path)`` when found, or ``(None, None, None)`` if no
    CODEOWNERS file exists.
    """
    params = {"ref": branch} if branch else None
    for path in paths:
        url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
        resp = requests.get(url, headers=headers, params=params, timeout=GITHUB_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            data = resp.json()
            try:
                content = base64.b64decode(data["content"]).decode("utf-8")
            except Exception:
                content = ""
            return content, data.get("sha"), path
        if resp.status_code == 404:
            continue
        if resp.status_code in (401, 403):
            # Auth issues should not be silently treated as "no CODEOWNERS file".
            raise HTTPException(
                status_code=502,
                detail=(
                    f"GitHub API authorization error ({resp.status_code}) reading {path}. "
                    "The GitHub token may be invalid, expired, or missing required permissions."
                ),
            )
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API error ({resp.status_code}) reading {path}",
        )
    return None, None, None


def _serialize_codeowners(record: Optional[Codeowners]) -> Optional[dict]:
    if not record:
        return None
    return {
        "id": record.id,
        "project_id": record.project_id,
        "content": record.content,
        "file_path": record.file_path,
        "git_hash": record.git_hash,
        "status": record.status,
        "last_modified_by": record.last_modified_by,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def _validate_codeowners_syntax(content: str) -> list:
    """
    Minimal best-effort CODEOWNERS validator.  Returns a list of warning
    strings.  Lines that are blank or comments are ignored; every other
    line must contain at least one owner token starting with ``@`` or
    looking like an email address.
    """
    warnings = []
    for idx, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            warnings.append(f"Line {idx}: missing owner(s) for pattern '{parts[0]}'")
            continue
        owners = parts[1:]
        for owner in owners:
            # Tighten the email check: at least one character on either side of '@'.
            looks_like_email = "@" in owner and 0 < owner.index("@") < len(owner) - 1
            if not (owner.startswith("@") or looks_like_email):
                warnings.append(
                    f"Line {idx}: owner '{owner}' is neither a @user/@team handle nor an email"
                )
    return warnings


def _save(db: Session, project: Project, payload: CodeownersSaveRequest, file_path: str, user: str) -> tuple:
    """Write the save to the project's record; returns (record, whether it changed)."""
    record = _project_codeowners(db, project.project_id)
    if record is None:
        record = Codeowners(project_id=project.project_id, status="new")
        db.add(record)
    elif (record.content, record.file_path) == (payload.content, file_path):
        return record, False
    else:
        # Under review too: the editor unlocks it, and Commit and Update PR
        # then adds this save to the open pull request.
        record.status = "committed_locally"
    record.content = payload.content
    record.file_path = file_path
    # As for a workflow: no blob SHA for content GitHub has not been given.
    record.git_hash = None
    record.last_modified_by = user
    db.commit()
    return record, True


# --------------------------------------------------------------------------- #
# Endpoints                                                                   #
# --------------------------------------------------------------------------- #


@router.get("/api/projects/{project_id}/codeowners", responses={
    401: {"description": "Not authenticated"},
    403: {"description": "No access to this project"},
    404: {"description": "Project not found"},
})
def get_project_codeowners(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """The project's CODEOWNERS file, or null when it has none (database only, no GitHub calls)."""
    _get_project_for_user(db, project_id, account.github_user)
    return {"codeowners": _serialize_codeowners(_project_codeowners(db, project_id))}


@router.put("/api/projects/{project_id}/codeowners", responses={
    400: {"description": "Invalid file path, or a project that is not a CODEOWNERS project"},
    401: {"description": "Not authenticated"},
    403: {"description": "Not a project editor"},
    404: {"description": "Project not found"},
})
def save_project_codeowners(
    project_id: int,
    payload: CodeownersSaveRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Save the project's CODEOWNERS locally; a PR campaign delivers it.

    A change is a local edit, exactly as for a workflow: the file is
    committed_locally with its hash cleared. Drift the last check recorded
    stays, unless the edit makes the file exactly GitHub's copy there.
    """
    user = account.github_user
    project = _get_project_for_user(db, project_id, user)
    _require_project_editor(db, user, project)
    refuse_unless_accepted(project, kind=KIND)
    file_path = _validate_file_path(payload.file_path)

    record, changed = _save(db, project, payload, file_path, user)
    if changed:
        resolve_drift_for_local_edit(db, project, deliveries.CODEOWNERS, record.id, record.content)
        # Create Pull Requests needs a draft project; without this a
        # CODEOWNERS-only edit could not be delivered until a workflow changed.
        _mark_project_draft(db, project.project_id)
    db.refresh(record)
    db.refresh(project)

    return {
        "success": True,
        "message": "CODEOWNERS saved" if changed else "No changes to save",
        "codeowners": _serialize_codeowners(record),
        "validation_warnings": _validate_codeowners_syntax(payload.content),
        "pr_state": project.pr_state,
    }


@router.get("/api/repos/{repo_ref:path}/codeowners", responses={
    400: {"description": "Invalid file path or repository name, or a project that is not a CODEOWNERS project"},
    409: {"description": "More than one project has this name"},
    502: {"description": "GitHub could not be read"},
})
def get_codeowners(
    repo_ref: str,
    project_name: str,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    branch: Optional[str] = None,
    path: Optional[str] = None,
):
    """
    A repository's CODEOWNERS on GitHub, alongside the project's own: to import
    a repository's file into the project, or to load the version a branch drifted to.

    ``branch`` defaults to the repository's default branch, and ``path`` to
    the first of the standard locations that exists.
    ``repo_ref`` is either the integer ``repo_id`` or the full ``owner/repo`` name.
    """
    token, project = _resolve_caller_and_project(db, account.github_user, project_name)
    refuse_unless_accepted(project, kind=KIND)
    repo = _resolve_repo_in_project(db, project, repo_ref)
    owner, repo_short = _split_repo(repo.repo_name)
    paths = (_validate_file_path(path),) if path else ALLOWED_PATHS

    github_content, github_sha, github_path = _fetch_github_codeowners(
        owner, repo_short, _gh_headers(token), branch, paths
    )

    return {
        "success": True,
        "repo_id": repo.repo_id,
        "repo_name": repo.repo_name,
        "branch": branch,
        "github": {
            "exists": github_content is not None,
            "content": github_content,
            "sha": github_sha,
            "path": github_path,
        },
        "local": _serialize_codeowners(_project_codeowners(db, project.project_id)),
    }
