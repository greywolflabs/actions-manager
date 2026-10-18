"""
Rulesets API Module for ActionsManager

Handles GitHub repository ruleset management including:
- Uploading ruleset JSON files
- Storing rulesets in database
- Applying rulesets to repositories
- Managing rulesets across projects
"""

import json
from typing import Annotated, List, Optional, Tuple
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Query, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, ValidationError, field_validator
import httpx
from config import GITHUB_TIMEOUT_SECONDS
from error_detail import failure_kind
from database import get_db
from models import Ruleset, Project, ProjectRepo, ProjectRuleset, Account, Repo
import os
import auth as auth_module
from auth import user_tokens
from authorization import project_role
from workflows import (
    _ERR_INSUFFICIENT_PROJECT_ROLE,
    _find_project_by_name,
    require_project_write_access,
    require_repo_write_access,
)


router = APIRouter()

# GitHub API configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
RULESET_NOT_FOUND = "Ruleset not found"

# Which copies of a ruleset a removal destroys.
#
# "project"            — drops only the ActionsManager records; the ruleset stays
#                        enforced on every repository it was applied to.
# "github"             — the inverse: stops enforcing it on those repositories
#                        but keeps the definition here, so it can be applied
#                        again. A branch-protection policy is worth keeping
#                        after you lift it.
# "project_and_github" — both.
#
# "project" is the default because it is what this route always did — a
# parameterless DELETE must not start reaching GitHub.
_SCOPE_PROJECT = "project"
_SCOPE_GITHUB = "github"
_SCOPE_PROJECT_AND_GITHUB = "project_and_github"
_VALID_SCOPES = (_SCOPE_PROJECT, _SCOPE_GITHUB, _SCOPE_PROJECT_AND_GITHUB)
_SCOPES_REACHING_GITHUB = (_SCOPE_GITHUB, _SCOPE_PROJECT_AND_GITHUB)

GITHUB_ACCEPT_JSON = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"


# Past the column sizes PostgreSQL rejects the insert, and the client's mistake
# would come back as a 500 (#2239).
_MAX_RULESET_NAME = Ruleset.__table__.c.ruleset_name.type.length
_MAX_RULESET_DESCRIPTION = Ruleset.__table__.c.description.type.length


class RulesetCreate(BaseModel):
    """Model for creating a new ruleset; upload validates through it too."""
    name: str = Field(min_length=1, max_length=_MAX_RULESET_NAME)
    description: Optional[str] = Field(default=None, max_length=_MAX_RULESET_DESCRIPTION)
    ruleset_json: dict

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("name", "description")
    @classmethod
    def _storable_text(cls, value: Optional[str]) -> Optional[str]:
        # The database drivers refuse these rather than store them: NUL on
        # PostgreSQL, a lone surrogate on any of them.
        if value is None:
            return value
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("contains characters that cannot be stored") from None
        if "\x00" in value:
            raise ValueError("contains characters that cannot be stored")
        return value


# Neither model carries the caller: it comes from the session (#2241). A
# github_user the client still sends is an extra field, and ignored.
class ApplyRulesetRequest(BaseModel):
    """Model for applying ruleset to repositories"""
    repo_names: List[str]


class RulesetSyncStatusRequest(BaseModel):
    """Model for checking ruleset sync status across repositories"""
    repo_names: List[str]


def _validate_uploaded(name, description, ruleset_data: dict) -> None:
    """Reject what the rulesets table cannot store as the client's error.

    The uploaded JSON is free-form, so its name and description arrive with no
    type or size checked. RulesetCreate holds the rules create already applies.
    """
    try:
        RulesetCreate(name=name, description=description, ruleset_json=ruleset_data)
    except ValidationError as e:
        error = e.errors()[0]
        raise HTTPException(status_code=400, detail=f"Invalid ruleset {error['loc'][0]}: {error['msg']}")


@router.post(
    "/api/rulesets/upload",
    responses={
        400: {"description": "Not a JSON object, or a name or description that cannot be stored"},
        401: {"description": "Not signed in"},
        403: {"description": "Names another user, or cannot edit the project"},
        404: {"description": "Project not found"},
        409: {"description": "More than one project has this name"},
        500: {"description": "Error uploading ruleset"},
    },
)
async def upload_ruleset_file(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    file: Annotated[UploadFile, File()],
    project_name: Annotated[str, Form()],
):
    """Upload a ruleset JSON file and store it in the database"""

    # Storing a ruleset against a project is a write to that project.
    project = require_project_write_access(db, request, account.github_user, project_name)

    if not file.filename.endswith('.json'):
        raise HTTPException(status_code=400, detail="File must be a JSON file")

    # From bytes, json detects a BOM and UTF-16/32 itself. Everything it raises
    # on bad input is a ValueError (undecodable bytes, malformed JSON, a number
    # past the digit limit) or, for deep nesting, a RecursionError.
    try:
        ruleset_data = json.loads(await file.read())
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Invalid JSON file")

    try:
        # Validate that this looks like a GitHub ruleset
        if not isinstance(ruleset_data, dict):
            raise HTTPException(status_code=400, detail="Invalid ruleset format: must be a JSON object")
        
        # Extract name from ruleset or use filename
        ruleset_name = ruleset_data.get('name', file.filename.removesuffix('.json'))
        # Cut to fit: a long filename is not the client's mistake to fix.
        description = ruleset_data.get('description', f"Imported from {file.filename}"[:_MAX_RULESET_DESCRIPTION])

        _validate_uploaded(ruleset_name, description, ruleset_data)

        # Create new ruleset
        new_ruleset = Ruleset(
            ruleset_name=ruleset_name,
            ruleset_json=json.dumps(ruleset_data),
            description=description,
            user_id=account.user_id
        )
        
        db.add(new_ruleset)
        db.flush()
        
        # Attach to the project the gate resolved. Looking it up again by owner
        # and exact name missed editors, admins and other casings, and stored
        # the ruleset with no project while still reporting success (#2063).
        # One commit for both rows, so a failed link cannot leave it unattached.
        db.add(ProjectRuleset(project_id=project.project_id, ruleset_id=new_ruleset.ruleset_id))
        db.commit()
        
        return {
            "success": True,
            "message": f"Ruleset '{ruleset_name}' uploaded successfully",
            "ruleset_id": new_ruleset.ruleset_id,
            "ruleset_name": ruleset_name
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions (like 404) without modification
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error uploading ruleset: {e}")
        raise HTTPException(status_code=500, detail="Error uploading ruleset")


@router.post(
    "/api/rulesets/create",
    responses={
        401: {"description": "Not signed in"},
        403: {"description": "Names another user, or cannot edit the project"},
        404: {"description": "Project not found"},
        409: {"description": "More than one project has this name"},
        500: {"description": "Error creating ruleset"},
    },
)
async def create_ruleset(
    request: Request,
    ruleset_data: RulesetCreate,
    db: Annotated[Session, Depends(get_db)],
    user_account: auth_module.CurrentAccount,
    project_name: str = None,
):
    """Create a new ruleset from JSON data"""

    # The project gate only runs when a project is named: this route can also
    # create a ruleset with no project. When a project is named, attaching to
    # it is a write to it.
    project = None
    if project_name:
        project = require_project_write_access(db, request, user_account.github_user, project_name)

    try:
        # Create new ruleset
        new_ruleset = Ruleset(
            ruleset_name=ruleset_data.name,
            ruleset_json=json.dumps(ruleset_data.ruleset_json),
            description=ruleset_data.description,
            user_id=user_account.user_id
        )
        
        db.add(new_ruleset)
        db.flush()
        
        if project:
            db.add(ProjectRuleset(project_id=project.project_id, ruleset_id=new_ruleset.ruleset_id))
        db.commit()
        
        return {
            "success": True,
            "message": f"Ruleset '{ruleset_data.name}' created successfully",
            "ruleset_id": new_ruleset.ruleset_id
        }
        
    except Exception as e:
        db.rollback()
        print(f"❌ Error creating ruleset: {e}")
        raise HTTPException(status_code=500, detail="Error creating ruleset")


@router.get(
    "/api/rulesets/{project_name}",
    responses={
        404: {"description": "Project not found"},
        409: {"description": "More than one project has this name"},
        500: {"description": "Error fetching rulesets"},
    },
)
async def get_project_rulesets(
    project_name: str,
    account: auth_module.CurrentAccount,
    db: Annotated[Session, Depends(get_db)]
):
    """Get all rulesets for a specific project"""
    
    try:
        # Resolve by access rather than ownership. The owner-scoped lookup this
        # used to do meant the panel only ever loaded for the account that owns
        # the project: every other caller - including a workspace admin on a
        # project they did not create - got a 404 that the UI shows as
        # "Error loading rulesets".
        project = _find_project_by_name(db, account.github_user, project_name)
        
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        
        # Get project rulesets
        rulesets = db.query(Ruleset).join(ProjectRuleset).filter(
            ProjectRuleset.project_id == project.project_id
        ).all()
        
        return {
            "success": True,
            "rulesets": [
                {
                    "ruleset_id": ruleset.ruleset_id,
                    "ruleset_name": ruleset.ruleset_name,
                    "description": ruleset.description,
                    "created_at": ruleset.created_at.isoformat(),
                    "updated_at": ruleset.updated_at.isoformat(),
                    "ruleset_json": json.loads(ruleset.ruleset_json)
                }
                for ruleset in rulesets
            ]
        }
        
    except HTTPException:
        # Without this the handler below swallows this route's own 404 and
        # re-reports it as a 500 carrying the original detail, which is what
        # the UI was showing as "Error loading rulesets".
        raise
    except Exception as e:
        print(f"❌ Error fetching rulesets: {e}")
        raise HTTPException(status_code=500, detail="Error fetching rulesets")


def _github_name(ruleset: Ruleset) -> str:
    """The name GitHub knows the ruleset by: its JSON's own, else the listed one.

    Apply, sync status and removal all look a ruleset up on GitHub by this, so
    they must agree on it, or the copy one creates is invisible to the others
    (#2238). A null or blank name in the JSON counts as none.
    """
    return json.loads(ruleset.ruleset_json).get("name") or ruleset.ruleset_name


@router.post(
    "/api/rulesets/{ruleset_id}/apply",
    responses={
        401: {"description": "Not signed in"},
        403: {"description": "Names another user, or cannot edit the ruleset's projects or a target repository"},
        404: {"description": "Ruleset or repository not found, or the ruleset is on no project the caller can see"},
        500: {"description": "Error applying ruleset"},
    },
)
async def apply_ruleset_to_repos(
    ruleset_id: int,
    request_data: ApplyRulesetRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Apply a ruleset to specified repositories"""

    # The caller picks whose GitHub token gets used, so it comes from the
    # session: a name in the body would let a member apply rulesets under
    # someone else's identity.
    caller = account.github_user
    ruleset = _ruleset_for_caller(db, account, ruleset_id, edit=True)
    # Applying writes the ruleset into those repositories. They carry no
    # project_name, so authorization comes from the projects the repositories
    # belong to.
    for _repo in request_data.repo_names:
        require_repo_write_access(db, request, caller, _repo)

    try:
        # Parse ruleset JSON
        ruleset_data = json.loads(ruleset.ruleset_json)
        ruleset_data["name"] = _github_name(ruleset)
        
        results = []
        errors = []
        
        # Apply ruleset to each repository
        for repo_name in request_data.repo_names:
            try:
                result = await apply_ruleset_to_repo(
                    caller,
                    repo_name, 
                    ruleset_data
                )
                if result:
                    results.append(f"✅ Applied ruleset '{ruleset.ruleset_name}' to {repo_name}")
                else:
                    errors.append(f"❌ Failed to apply ruleset to {repo_name}")
            except Exception as e:
                kind = failure_kind(e)
                print(f"❌ {kind} applying ruleset to {repo_name}: {e}")
                errors.append(f"❌ Error applying ruleset to {repo_name}: {kind}")
        
        return {
            "success": len(errors) == 0,
            "message": f"Attempted to apply ruleset '{ruleset.ruleset_name}' to {len(request_data.repo_names)} repositories",
            "results": results,
            "errors": errors,
            "applied_count": len(results),
            "error_count": len(errors)
        }
        
    except Exception as e:
        print(f"❌ Error applying ruleset: {e}")
        raise HTTPException(status_code=500, detail="Error applying ruleset")


@router.post(
    "/api/rulesets/{ruleset_id}/sync-status",
    responses={
        401: {"description": "Not authenticated with GitHub"},
        403: {"description": "Names another user"},
        404: {"description": "Ruleset not found, or on no project the caller can see"},
        500: {"description": "Error checking ruleset sync status"},
    },
)
async def check_ruleset_sync_status(
    ruleset_id: int,
    request_data: RulesetSyncStatusRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """Check if a ruleset exists across all specified repositories"""

    # As for apply: the caller selects whose token reads the repositories.
    caller = account.github_user
    ruleset = _ruleset_for_caller(db, account, ruleset_id, edit=False)

    try:
        ruleset_name = _github_name(ruleset)
        
        # Check if user is authenticated with GitHub
        if caller not in user_tokens:
            raise HTTPException(status_code=401, detail="User not authenticated with GitHub")
        
        # Get user's GitHub token
        token = user_tokens[caller]
        headers = {
            "Authorization": f"token {token}",
            "Accept": GITHUB_ACCEPT_JSON,
            "X-GitHub-Api-Version": GITHUB_API_VERSION
        }
        
        repo_statuses = {}
        missing_repos = []
        
        async with httpx.AsyncClient() as client:
            # Check each repository for the ruleset
            for repo_name in request_data.repo_names:
                if '/' not in repo_name:
                    # Skip invalid repo names
                    continue
                    
                owner, repo = repo_name.split('/', 1)
                
                try:
                    # Get repository rulesets
                    api_url = f"https://api.github.com/repos/{owner}/{repo}/rulesets"
                    response = await client.get(api_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
                    
                    if response.status_code == 200:
                        rulesets = response.json()
                        
                        # Check if any ruleset matches our name
                        ruleset_found = False
                        for existing_ruleset in rulesets:
                            if existing_ruleset.get("name") == ruleset_name:
                                ruleset_found = True
                                break
                        
                        if ruleset_found:
                            repo_statuses[repo_name] = {
                                "status": "exists",
                                "message": f"Ruleset '{ruleset_name}' exists in repository"
                            }
                        else:
                            repo_statuses[repo_name] = {
                                "status": "not_found",
                                "message": f"Ruleset '{ruleset_name}' not found in repository"
                            }
                            missing_repos.append(repo_name)
                            
                    elif response.status_code == 403:
                        repo_statuses[repo_name] = {
                            "status": "permission_denied",
                            "message": "Permission denied - need admin access to check rulesets"
                        }
                        missing_repos.append(repo_name)  # Treat as missing since we can't verify
                        
                    elif response.status_code == 404:
                        repo_statuses[repo_name] = {
                            "status": "repo_not_found",
                            "message": "Repository not found"
                        }
                        missing_repos.append(repo_name)
                        
                    else:
                        repo_statuses[repo_name] = {
                            "status": "error",
                            "message": f"Error checking repository: HTTP {response.status_code}"
                        }
                        missing_repos.append(repo_name)
                        
                except Exception as e:
                    kind = failure_kind(e)
                    print(f"❌ {kind} checking rulesets on {repo_name}: {e}")
                    repo_statuses[repo_name] = {
                        "status": "error",
                        "message": f"Error checking repository: {kind}"
                    }
                    missing_repos.append(repo_name)
        
        is_synced = len(missing_repos) == 0
        
        return {
            "success": True,
            "ruleset_name": ruleset_name,
            "is_synced": is_synced,
            "missing_repos": missing_repos,
            "repo_statuses": repo_statuses,
            "total_repos": len(request_data.repo_names),
            "synced_repos": len(request_data.repo_names) - len(missing_repos)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error checking ruleset sync status: {e}")
        raise HTTPException(status_code=500, detail="Error checking ruleset sync status")


def _require_ruleset_access(db: Session, account: Account, ruleset: Ruleset, *, edit: bool) -> None:
    """Raise unless the caller may act on *ruleset*, through the projects it is attached to.

    The panel lists every ruleset attached to the project, so access has to be
    decided the same way: whoever can see its project can read it, and
    whoever can edit it can apply or remove it. Who added it stops mattering,
    or an admin's upload is out of the owner's reach and a ruleset whose
    uploader lost access can never be removed (#2236).

    Editing needs every attached project, not just one: removal drops the
    ruleset from all of them and, for the GitHub scopes, deletes it from all
    of their repositories. (No route attaches a ruleset to a second project
    today; this keeps that from becoming a way into someone else's.)

    An unattached ruleset (created without a project, or whose project was
    deleted) has no project to decide by, so it stays with the account that
    added it rather than becoming everyone's.
    """
    projects = (
        db.query(Project)
        .join(ProjectRuleset, ProjectRuleset.project_id == Project.project_id)
        .filter(ProjectRuleset.ruleset_id == ruleset.ruleset_id)
        .all()
    )
    if not projects:
        if ruleset.user_id != account.user_id:
            raise HTTPException(status_code=404, detail=RULESET_NOT_FOUND)
        return

    roles = [project_role(db, account, project) for project in projects]
    # Same 404 as a missing ruleset for one on projects the caller cannot see.
    if not any(roles):
        raise HTTPException(status_code=404, detail=RULESET_NOT_FOUND)
    if edit and not all(role in ("project_admin", "project_editor") for role in roles):
        raise HTTPException(status_code=403, detail=_ERR_INSUFFICIENT_PROJECT_ROLE)


def _ruleset_for_caller(db: Session, account: Account, ruleset_id: int, *, edit: bool) -> Ruleset:
    """Load a ruleset the caller may read, or with *edit* apply and remove."""
    ruleset = db.query(Ruleset).filter(Ruleset.ruleset_id == ruleset_id).first()
    if not ruleset:
        raise HTTPException(status_code=404, detail=RULESET_NOT_FOUND)
    _require_ruleset_access(db, account, ruleset, edit=edit)
    return ruleset


def _repos_targeted_by_ruleset(db: Session, ruleset_id: int) -> List[str]:
    """Every repository the ruleset's projects target — where a GitHub copy can be."""
    rows = (
        db.query(Repo.repo_name)
        .join(ProjectRepo, ProjectRepo.repo_id == Repo.repo_id)
        .join(ProjectRuleset, ProjectRuleset.project_id == ProjectRepo.project_id)
        .filter(ProjectRuleset.ruleset_id == ruleset_id)
        .distinct()
        .all()
    )
    return [name for (name,) in rows]


async def _delete_repo_ruleset(
    client: httpx.AsyncClient,
    headers: dict,
    github_user: str,
    repo_name: str,
    ruleset_name: str,
) -> Tuple[bool, Optional[str]]:
    """Delete one repository's copy of *ruleset_name*.

    Returns ``(deleted, error)``. ``(False, None)`` means the repository has no
    ruleset by that name — never applied, or already removed by hand — which is
    an acceptable end state, not a failure.

    The GitHub-side id is looked up by name rather than stored: nothing has ever
    recorded it, so a new column would be NULL for every ruleset applied before
    this existed — exactly the leftovers worth deleting.
    """
    owner, repo = repo_name.split('/', 1) if '/' in repo_name else (github_user, repo_name)
    api_base = f"https://api.github.com/repos/{owner}/{repo}/rulesets"

    listing = await client.get(
        api_base,
        headers=headers,
        # includes_parents defaults to TRUE, which mixes the organization and
        # enterprise rulesets inherited by this repository into the listing.
        # Matching by name across that could hand back an org ruleset's id, and
        # DELETE on this repo-scoped path cannot delete one — it 404s, which
        # below counts as removed. The row would then be dropped with the
        # repository's own ruleset still enforced: exactly the leftover #2011 is
        # about. It also keeps the single page below sufficient, since a
        # repository's own rulesets do not reach 100.
        params={"per_page": 100, "includes_parents": "false"},
        timeout=GITHUB_TIMEOUT_SECONDS,
    )
    if listing.status_code != 200:
        return False, f"could not list rulesets (HTTP {listing.status_code})"

    target_id = next(
        (item.get("id") for item in listing.json() if item.get("name") == ruleset_name),
        None,
    )
    if target_id is None:
        return False, None

    response = await client.delete(
        f"{api_base}/{target_id}", headers=headers, timeout=GITHUB_TIMEOUT_SECONDS
    )
    # 404: the id came from this repository's own rulesets moments ago, so a
    # miss now means something else deleted it in between. Same end state.
    if response.status_code in (204, 404):
        return True, None
    return False, f"could not delete ruleset (HTTP {response.status_code})"


async def _delete_ruleset_from_repos(
    github_user: str, repo_names: List[str], ruleset_name: str
) -> Tuple[List[str], List[str]]:
    """Delete *ruleset_name* from every repository. Returns (removed_from, errors)."""
    if github_user not in user_tokens:
        raise HTTPException(status_code=401, detail="User not authenticated with GitHub")

    headers = {
        "Authorization": f"token {user_tokens[github_user]}",
        "Accept": GITHUB_ACCEPT_JSON,
        "X-GitHub-Api-Version": GITHUB_API_VERSION
    }

    removed_from: List[str] = []
    errors: List[str] = []

    async with httpx.AsyncClient() as client:
        for repo_name in repo_names:
            try:
                deleted, error = await _delete_repo_ruleset(
                    client, headers, github_user, repo_name, ruleset_name
                )
            except httpx.RequestError as e:
                print(f"❌ Network error removing ruleset from {repo_name}: {e}")
                deleted, error = False, "network error"

            if error:
                errors.append(f"{repo_name}: {error}")
            elif deleted:
                removed_from.append(repo_name)

    return removed_from, errors


async def _remove_ruleset_from_github(
    db: Session, ruleset: Ruleset, github_user: str
) -> List[str]:
    """Delete the ruleset from every repository its projects target.

    Raises 502 on any failure, which keeps the caller's rows in place: dropping
    them would leave a ruleset enforcing branch protection with nothing in
    ActionsManager still pointing at it — the whole point of the GitHub option.
    """
    github_name = _github_name(ruleset)

    removed_from, errors = await _delete_ruleset_from_repos(
        github_user, _repos_targeted_by_ruleset(db, ruleset.ruleset_id), github_name
    )

    if errors:
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Could not remove ruleset '{github_name}' from every repository",
                "errors": errors,
                "removed_from_repos": removed_from,
            },
        )

    return removed_from


@router.delete(
    "/api/rulesets/{ruleset_id}",
    # SonarQube S8415 wants every status this route raises declared. 403 comes
    # from the session and project checks, 400 from an unknown scope, 401/502
    # from the GitHub half below, 404 from the ruleset lookup, and 500 from the
    # catch-all.
    responses={
        400: {"description": "Unknown removal scope"},
        401: {"description": "Not authenticated with GitHub"},
        403: {"description": "Names another user, or cannot edit the ruleset's projects"},
        404: {"description": "Ruleset not found, or on no project the caller can see"},
        500: {"description": "Error deleting ruleset"},
        502: {"description": "GitHub rejected part of the removal"},
    },
)
async def delete_ruleset(
    ruleset_id: int,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
    scope: Annotated[str, Query(description="'project', 'github' or 'project_and_github'")] = _SCOPE_PROJECT,
):
    """Delete a ruleset from ActionsManager, and optionally from GitHub too"""

    # Deleting detaches the ruleset from its projects and, for the GitHub
    # scopes, removes it from their repositories.
    ruleset = _ruleset_for_caller(db, account, ruleset_id, edit=True)

    if scope not in _VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of {', '.join(_VALID_SCOPES)}",
        )

    try:
        ruleset_name = ruleset.ruleset_name

        removed_from: List[str] = []
        if scope in _SCOPES_REACHING_GITHUB:
            removed_from = await _remove_ruleset_from_github(db, ruleset, account.github_user)

        # A GitHub-only removal lifts the policy without forgetting it: the rows
        # stay so the same ruleset can be applied again from the same project.
        if scope == _SCOPE_GITHUB:
            return {
                "success": True,
                # Claiming a removal that touched no repository reads as done
                # when nothing happened — a project whose repositories were
                # never saved resolves no targets at all.
                "message": (
                    f"Ruleset '{ruleset_name}' removed from GitHub"
                    if removed_from
                    else f"Ruleset '{ruleset_name}' was not applied to any of this project's repositories, so nothing was removed"
                ),
                "removed_from_repos": removed_from
            }

        # Delete associated project relationships
        db.query(ProjectRuleset).filter(ProjectRuleset.ruleset_id == ruleset_id).delete()
        
        # Delete the ruleset
        db.delete(ruleset)
        db.commit()
        
        return {
            "success": True,
            "message": f"Ruleset '{ruleset_name}' deleted successfully",
            "removed_from_repos": removed_from
        }
        
    except HTTPException:
        # Without this, the catch-all below turns the GitHub half's 401 and 502
        # into a 500.
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error deleting ruleset: {e}")
        raise HTTPException(status_code=500, detail="Error deleting ruleset")


async def apply_ruleset_to_repo(github_user: str, repo_name: str, ruleset_data: dict) -> bool:
    """Apply a ruleset to a specific repository using GitHub API"""
    
    try:
        # Check if user is authenticated
        if github_user not in user_tokens:
            print(f"❌ User {github_user} is not authenticated")
            return False
        
        # Get user's GitHub token
        token = user_tokens[github_user]
        
        # Prepare headers for GitHub API
        headers = {
            "Authorization": f"token {token}",
            "Accept": GITHUB_ACCEPT_JSON,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Content-Type": "application/json"
        }
        
        # Split repo_name into owner/repo format
        if '/' in repo_name:
            owner, repo = repo_name.split('/', 1)
        else:
            # If no slash, assume the user owns the repo
            owner = github_user
            repo = repo_name
        
        # Transform the exported ruleset data to the format required by GitHub API
        # The exported format includes metadata that we need to strip for creation
        api_ruleset_data = {
            "name": ruleset_data["name"],
            "target": ruleset_data.get("target", "branch"),
            "enforcement": ruleset_data.get("enforcement", "active"),
            "rules": ruleset_data.get("rules", []),
            "conditions": ruleset_data.get("conditions", {}),
            "bypass_actors": ruleset_data.get("bypass_actors", [])
        }
        
        # Remove any fields that are read-only or not needed for creation
        # The exported format includes 'id' and 'source_type' which are not needed for creation
        if "id" in api_ruleset_data:
            del api_ruleset_data["id"]
        if "source_type" in api_ruleset_data:
            del api_ruleset_data["source_type"]
        
        # GitHub API endpoint for creating repository rulesets
        api_url = f"https://api.github.com/repos/{owner}/{repo}/rulesets"
        
        print(f"📋 Applying ruleset '{api_ruleset_data['name']}' to {owner}/{repo}")
        print(f"📋 API URL: {api_url}")
        print(f"📋 Ruleset data: {json.dumps(api_ruleset_data, indent=2)}")
        
        # Make the API request to create the ruleset
        async with httpx.AsyncClient() as client:
            response = await client.post(
                api_url,
                headers=headers,
                json=api_ruleset_data,
                timeout=GITHUB_TIMEOUT_SECONDS
            )
        
            print(f"📋 GitHub API response status: {response.status_code}")
        
            if response.status_code == 201:
                # Success - ruleset created
                response_data = response.json()
                print(f"✅ Successfully applied ruleset to {owner}/{repo}. Created ruleset ID: {response_data.get('id')}")
                return True
            elif response.status_code == 422:
                # Validation error - likely the ruleset already exists or has invalid data
                error_data = response.json()
                print(f"⚠️ Validation error applying ruleset to {owner}/{repo}: {error_data.get('message', 'Unknown error')}")
                print(f"📋 Error details: {json.dumps(error_data, indent=2)}")
                return False
            elif response.status_code == 403:
                # Permission denied - user doesn't have admin rights to the repo
                print(f"❌ Permission denied applying ruleset to {owner}/{repo}. User needs admin access to the repository.")
                return False
            elif response.status_code == 404:
                # Repository not found
                print(f"❌ Repository {owner}/{repo} not found")
                return False
            else:
                # Other error
                error_data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {"message": response.text}
                print(f"❌ Error applying ruleset to {owner}/{repo}: HTTP {response.status_code}")
                print(f"📋 Error details: {json.dumps(error_data, indent=2)}")
                return False
        
    except httpx.RequestError as e:
        print(f"❌ Network error applying ruleset to {repo_name}: {str(e)}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error applying ruleset to {repo_name}: {str(e)}")
        return False
