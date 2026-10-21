"""
The Managed Files section (#2367): projects that each manage one repository
file, such as CODEOWNERS, across a set of repositories.

Its projects are listed by GET /api/projects/?section=managed_files. This
module answers what the section's page needs on top of that list.
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from auth import CurrentAccount
from database import get_db
from file_kinds import FILE_KINDS, MANAGED_FILES
from projects import (
    OWNER_REPO_NAME,
    _require_project_access,
    managed_files_holders,
    owned_projects_by_type,
    shown_holders,
)
from tier_service import describe_plan

router = APIRouter()

class TakenReposRequest(BaseModel):
    repo_names: list[str] = Field(max_length=1000)
    # The project being edited: its own repositories are not taken from it.
    exclude_project_id: Optional[int] = None

    @field_validator("repo_names")
    @classmethod
    def _owner_repo_names(cls, names: list[str]) -> list[str]:
        for name in names:
            if not OWNER_REPO_NAME.fullmatch(name):
                raise ValueError("repo_names must use owner/repo format")
        return names


@router.get("/api/managed-files", responses={401: {"description": "Authentication required"}})
def get_managed_files(
    db: Annotated[Session, Depends(get_db)],
    account: CurrentAccount,
):
    """The file kinds a project can manage, and the caller's place against the section's limit.

    ``owned_count`` is what the create check counts, and ``limit`` and
    ``limit_message`` come from the plan, so the page shows the limit the
    server enforces. ``limit`` is null when the plan has none.
    """
    plan = describe_plan(account)
    return {
        "kinds": [
            {"kind": kind, "label": definition.label, "paths": list(definition.paths)}
            for kind, definition in FILE_KINDS.items()
        ],
        "owned_count": owned_projects_by_type(db, account.user_id).get(MANAGED_FILES, 0),
        "limit": plan["limits"]["managed_files_projects"],
        "limit_message": plan["limit_messages"].get("managed_files_projects"),
    }


@router.post(
    "/api/managed-files/{kind}/taken-repos",
    responses={
        401: {"description": "Authentication required"},
        404: {"description": "Unknown file kind, or a project to exclude the caller cannot see"},
        422: {"description": "repo_names must be at most 1000 names in owner/repo format"},
    },
)
def get_taken_repos(
    kind: str,
    payload: TakenReposRequest,
    db: Annotated[Session, Depends(get_db)],
    account: CurrentAccount,
):
    """Which of *repo_names* a Managed Files project of *kind* already holds, from the database only.

    Answers only for the names asked, so it never lists another project's
    repositories. A holder the caller cannot see comes back with a null id and
    name. Names a project holds are keys of ``taken``; the rest are left out.
    """
    if kind not in FILE_KINDS:
        raise HTTPException(status_code=404, detail=f"Unknown file kind '{kind}'")
    if payload.exclude_project_id is not None:
        _require_project_access(db, payload.exclude_project_id, account)
    holders = managed_files_holders(db, kind, payload.repo_names, exclude_project_id=payload.exclude_project_id)
    shown = shown_holders(db, account, holders.values())
    return {
        "taken": {
            name: shown[holders[name.lower()].project_id]
            for name in payload.repo_names
            if name.lower() in holders
        }
    }
