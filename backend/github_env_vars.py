import re

import httpx
from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from urllib.parse import quote
from typing import Annotated
from database import get_db
import auth as auth_module
from auth import user_tokens
from workflows import (
    _caller_can_see_project, _find_project_by_name, _project_owns_repo, _responses,
    require_project_write_access, require_repo_write_access,
)
from models import Project, ProjectEnvVar, ProjectRepo, Repo, WorkspaceMember

router = APIRouter()
GITHUB_API_URL = "https://api.github.com"
ACCEPT_HEADER = "application/vnd.github+json"
X_API_VERSION = "2022-11-28"
AUTH_STRING = "User not authenticated"
ERROR_CODE = "No repositories provided"
INVALID_REPO_NAMES = "repo_names must be a list of 'owner/repo' names"
INVALID_REPO_NAME = "repo_name must be 'owner/repo'"
INVALID_ENVIRONMENT_NAME = "Missing or invalid environment name"
# The most each listing returns per page: GitHub caps variables at 30.
VARIABLES_PAGE = 30
ENVIRONMENTS_PAGE = 100
_VALID_KEY = re.compile(r"\w+", re.ASCII)
_REPO_PART = re.compile(r"[A-Za-z0-9_.-]+")

# What each kind of route raises, for its `responses=`.
_NAMED_PROJECT = {409: {"description": "More than one project has this name"}}
PROJECT_WRITE_RESPONSES = {**_responses(400, 403, 404), **_NAMED_PROJECT}
PROJECT_READ_RESPONSES = {**_responses(400), **_NAMED_PROJECT}
REPO_WRITE_RESPONSES = _responses(400, 403, 404)
REPO_READ_RESPONSES = _responses(400, 403)

async def count_project_env_vars(user: str, project_code: str, repo_names: list, use_prefix: bool = True, project_id: int = None, db: Session = None) -> int:
    """Helper function to count environment variables for a project across repositories"""
    if user not in user_tokens:
        return 0
    
    # If prefix is disabled, count from local database
    if not use_prefix and project_id is not None and db is not None:
        count = db.query(ProjectEnvVar).filter(
            ProjectEnvVar.project_id == project_id
        ).count()
        return count
    
    # For prefixed env vars, count from GitHub
    token = user_tokens[user]
    headers = {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION
    }
    
    unique_env_vars = set()
    
    async with httpx.AsyncClient() as client:
        for repo_name in repo_names:
            try:
                variables = await _fetch_all_github_variables(client, repo_name, headers) or []
                unique_env_vars.update(
                    var["name"] for var in variables if var["name"].startswith(f"AM_{project_code}_")
                )
            except Exception as e:
                print(f"❌ Error counting env vars for {repo_name}: {str(e)}")
    
    return len(unique_env_vars)


async def count_project_environments(user: str, repo_names: list) -> int:
    """Helper function to count deployment environments for a project across repositories"""
    if user not in user_tokens:
        return 0
    
    token = user_tokens[user]
    headers = {
        "Authorization": f"token {token}",
        "Accept":  ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION
    }
    
    unique_environments = set()
    
    async with httpx.AsyncClient() as client:
        for repo_name in repo_names:
            try:
                environments, _status = await _list_environments(client, repo_name, headers)
                unique_environments.update(env["name"] for env in environments or [])
            except Exception as e:
                print(f"❌ Error counting environments for {repo_name}: {str(e)}")
    
    return len(unique_environments)


async def _list_all(client, url: str, key: str, per_page: int, headers: dict) -> tuple[list | None, int]:
    """Every item of a paged GitHub listing, with the status that ended it.

    Pages until total_count is reached rather than stopping on a short page,
    so a page cap lower than expected cannot end the listing early. The items
    are None when GitHub refuses a page.
    """
    items = []
    page = 1
    while True:
        response = await client.get(f"{url}?per_page={per_page}&page={page}", headers=headers)
        if response.status_code != 200:
            return None, response.status_code
        body = response.json()
        batch = body.get(key, [])
        items.extend(batch)
        if not batch or len(items) >= body.get("total_count", len(items)):
            return items, response.status_code
        page += 1


async def _list_environments(client, repo_name: str, headers: dict) -> tuple[list | None, int]:
    return await _list_all(client, _repo_url(repo_name, "environments"), "environments", ENVIRONMENTS_PAGE, headers)


def _is_unsafe_part(value: str) -> bool:
    """Empty, '.' or '..' anywhere between slashes."""
    return any(piece in ("", ".", "..") for piece in value.split("/"))


def _repo_url(repo_name: str, *segments: str) -> str:
    """https://api.github.com/repos/{owner}/{repo}/..., every part percent-encoded.

    Every GitHub URL this module calls is built here. httpx resolves '.' and
    '..' and ends the path at '?' or '#', so a value interpolated raw could
    reach another endpoint: an environment named '..' turned
    DELETE .../environments/.. into DELETE /repos/{owner}/{repo}, which
    deletes the repository. Encoding keeps each value inside its segment, and
    an empty or dot part is refused outright, '/'-separated ones included, in
    case GitHub decodes %2F before it resolves the path.
    """
    owner_repo = (repo_name or "").strip().split("/")
    parts = [*owner_repo, *segments]
    if len(owner_repo) != 2 or any(_is_unsafe_part(part) for part in parts):
        raise HTTPException(status_code=400, detail=f"Invalid name in a GitHub path: {'/'.join(parts)!r}")
    return "/".join([GITHUB_API_URL, "repos", *(quote(part, safe="") for part in parts)])


def _is_repo_name(name) -> bool:
    """'owner/repo' as GitHub spells it, with no '.' or '..' part."""
    if not isinstance(name, str):
        return False
    parts = name.strip().split("/")
    return len(parts) == 2 and all(_REPO_PART.fullmatch(part) and part not in (".", "..") for part in parts)


def _validate_repo_names(repo_names) -> list:
    """The body's repo_names, or a 400 before any of them reaches a check or a URL.

    An empty list stays valid: in no-prefix mode it updates only the names the
    project stores, without a call to GitHub.
    """
    if not isinstance(repo_names, list) or not all(_is_repo_name(name) for name in repo_names):
        raise HTTPException(status_code=400, detail=INVALID_REPO_NAMES)
    return [name.strip() for name in repo_names]


def _valid_environment_name(name) -> str | None:
    """The stripped name, or None when GitHub would not take it or it is a dot segment."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name if len(name) <= 255 and not _is_unsafe_part(name) else None


def _require_project_repos(db: Session, project: Project, repo_names: list) -> None:
    """Every repository written to must be one of the project's own.

    Write access to one project would otherwise reach any repository the
    caller's token can write to, and in no-prefix mode a variable name is not
    scoped to the project at all. Refused rather than skipped, so a panel
    never reports a change it did not make: a repository ticked but not
    saved yet joins the project when the project is saved.
    """
    for repo_name in repo_names:
        if not _project_owns_repo(db, project, repo_name):
            raise HTTPException(
                status_code=400,
                detail=f"Repository '{repo_name}' is not part of project '{project.project_name}' yet: save the project first",
            )


def _require_valid_key(key) -> str:
    """A variable name GitHub accepts, before it goes into a URL path.

    The same rule as the panel's, so nothing it allows is refused.
    """
    if not isinstance(key, str) or not _VALID_KEY.fullmatch(key.strip()):
        raise HTTPException(
            status_code=400, detail=f"Invalid variable name {key!r}: use letters, digits and underscores",
        )
    return key.strip()


def _require_repos_visible(db: Session, account, repo_names: list) -> None:
    """A read_only member reads environments only on their projects' repositories.

    Admins and full members see every project and may start a new one from any
    repository, so a repository ticked before the project is saved must still
    show its environments. A read_only member can do neither: the projects they
    were granted are all they see.
    """
    member = db.query(WorkspaceMember).filter(WorkspaceMember.user_id == account.user_id).first()
    if member and member.workspace_role != "read_only":
        return
    for repo_name in repo_names:
        linked = (
            db.query(Project)
            .join(ProjectRepo, ProjectRepo.project_id == Project.project_id)
            .join(Repo, Repo.repo_id == ProjectRepo.repo_id)
            .filter(Repo.repo_name == repo_name)
            .all()
        )
        if not any(_caller_can_see_project(db, account.github_user, project) for project in linked):
            raise HTTPException(status_code=403, detail=f"Not authorized for repository '{repo_name}'")


def _project_name(data: dict) -> str:
    return str(data.get("project_name") or "").strip()


def _validate_request_data(data: dict) -> tuple[list, list, str]:
    """Extract and validate request data"""
    repo_names = _validate_repo_names(data.get("repo_names"))
    env_vars = data.get("env") if isinstance(data.get("env"), list) else []
    return repo_names, env_vars, _project_name(data)


def _get_auth_headers(user: str) -> dict:
    """Get authentication headers for GitHub API"""
    if user not in user_tokens:
        return {}
    
    token = user_tokens[user]
    return {
        "Authorization": f"token {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": X_API_VERSION
    }


def _format_env_var_key(original_key: str, project_code: str, use_prefix: bool = True) -> str:
    """Format environment variable key with or without project prefix.

    Every variable URL is built from what this returns, so the name is
    checked here: a name that could leave its path segment never reaches one.
    """
    original_key = _require_valid_key(original_key)
    project_code = project_code.upper()
    prefix = f"AM_{project_code}_"
    
    if not use_prefix:
        # No prefix mode - just use the key as-is (uppercased)
        return original_key.upper()
    
    # Prefix mode
    if original_key.upper().startswith(prefix):
        # Key already has prefix, strip it first
        clean_key = original_key[len(prefix):]
        print(f"🔧 Stripped existing prefix from '{original_key}' -> '{clean_key}'")
    else:
        clean_key = original_key
    
    return f"AM_{project_code}_{clean_key.upper()}"


async def _check_variable_exists_in_repos(formatted_key: str, repo_names: list, headers: dict) -> bool:
    """Check if variable exists in any of the specified repositories"""
    async with httpx.AsyncClient() as client:
        for repo_name in repo_names:
            check_var_response = await client.get(_repo_url(repo_name, "actions", "variables", formatted_key), headers=headers)
            if check_var_response.status_code == 200:
                return True
    return False


def _validate_sync_environment_request(data: dict) -> tuple[str, list, str]:
    """Extract and validate sync environment request data"""
    repo_names = _validate_repo_names(data.get("repo_names"))
    environment_name = _valid_environment_name(data.get("environment_name"))
    if environment_name is None:
        raise HTTPException(status_code=400, detail=INVALID_ENVIRONMENT_NAME)
    return _project_name(data), repo_names, environment_name


async def _check_free_account_environment_limits(user: str, repo_names: list, new_envs_count: int, user_obj) -> dict:
    """Check if syncing would create more environments than the account may have.

    *new_envs_count* is how many repositories the sync will create it in,
    from the probe _find_existing_environments already made. Counts nothing
    when the active tier rules do not limit environments for this account.
    """
    from tier_service import environment_limit, environment_sync_limit_message

    limit = environment_limit(user_obj)
    if limit is None:
        return {}

    current_count = await count_project_environments(user, repo_names)

    if current_count + new_envs_count > limit:
        return {
            "error": environment_sync_limit_message(limit, current_count, new_envs_count),
            "status": 403,
            "current_count": current_count,
            "limit": limit,
        }
    return {}


async def _find_existing_environments(repo_names: list, environment_name: str, client: httpx.AsyncClient, headers: dict) -> dict:
    """What each repository answered when asked for the environment by name.

    True where it exists. A repository missing from the result answered 404,
    and only those are created: the create sends an empty configuration, so
    taking any other answer for "missing" would clear the reviewers and
    branch policy of an environment that is there. Those keep the status
    GitHub gave, and are left alone.
    """
    existing_environments = {}
    for repo_name in repo_names:
        response = await client.get(_repo_url(repo_name, "environments", environment_name), headers=headers)
        if response.status_code == 200:
            existing_environments[repo_name] = True
        elif response.status_code != 404:
            existing_environments[repo_name] = response.status_code

    return existing_environments


async def _create_missing_environments(repo_names: list, environment_name: str, existing_environments: dict, client: httpx.AsyncClient, headers: dict) -> tuple[dict, int]:
    """Create environment in repositories that don't have it"""
    results = {}
    created_count = 0
    
    for repo_name in repo_names:
        if repo_name not in existing_environments:
            create_env_url = _repo_url(repo_name, "environments", environment_name)
            
            # Create with minimal configuration (can be extended later)
            env_data = {
                "wait_timer": 0,
                "reviewers": [],
                "deployment_branch_policy": None
            }
            
            response = await client.put(create_env_url, json=env_data, headers=headers)
            
            if response.status_code in [200, 201]:
                results[repo_name] = "created"
                created_count += 1
            else:
                results[repo_name] = f"failed ({response.status_code})"
        elif existing_environments[repo_name] is True:
            results[repo_name] = "already exists"
        else:
            results[repo_name] = f"could not check ({existing_environments[repo_name]})"

    return results, created_count


async def _get_truly_new_variables(env_vars: list, project_code: str, repo_names: list, headers: dict, use_prefix: bool = True) -> list:
    """Identify which environment variables are truly new (don't exist in any repo)"""
    truly_new_vars = []
    for env_var in env_vars:
        original_key = env_var.get("key", "").strip()
        if not original_key:
            continue
            
        formatted_key = _format_env_var_key(original_key, project_code, use_prefix)
        variable_exists = await _check_variable_exists_in_repos(formatted_key, repo_names, headers)
        
        if not variable_exists:
            truly_new_vars.append(env_var)
    
    return truly_new_vars


async def _check_free_account_limits(user_obj, user: str, project_code: str, repo_names: list, env_vars: list, headers: dict, use_prefix: bool = True, project_id: int = None, db: Session = None) -> dict:
    """Check if account would exceed env-var limits with new variables.

    Counts nothing when the active tier rules do not limit environment
    variables for this account.
    """
    from tier_service import env_var_limit, env_var_limit_message

    limit = env_var_limit(user_obj)
    if limit is None:
        return {}

    current_count = await count_project_env_vars(user, project_code, repo_names, use_prefix, project_id, db)
    truly_new_vars = await _get_truly_new_variables(env_vars, project_code, repo_names, headers, use_prefix)
    new_env_vars_count = len(truly_new_vars)
    
    if current_count + new_env_vars_count > limit:
        return {
            "error": env_var_limit_message(limit, current_count, new_env_vars_count),
            "status": 403,
            "current_count": current_count,
            "limit": limit
        }
    
    return {}


async def _update_or_create_variable(repo_name: str, formatted_key: str, value: str, headers: dict) -> int:
    """Update existing variable or create new one, return status code"""
    var_url = _repo_url(repo_name, "actions", "variables", formatted_key)

    async with httpx.AsyncClient() as client:
        # Check if the variable exists
        check_var_response = await client.get(var_url, headers=headers)

        if check_var_response.status_code == 200:
            # Variable exists, update it using PATCH
            update_payload = {"value": value}
            update_response = await client.patch(var_url, json=update_payload, headers=headers)
            return update_response.status_code
        else:
            # Variable does NOT exist, create it using POST
            create_var_url = _repo_url(repo_name, "actions", "variables")
            create_payload = {"name": formatted_key, "value": value}
            create_response = await client.post(create_var_url, json=create_payload, headers=headers)
            return create_response.status_code

def _store_env_var_names_in_db(env_vars: list, project: "Project", db: Session) -> None:
    """Store env var names (without values) in the DB when prefix is disabled."""
    try:
        keys = {env_var["key"].strip().upper() for env_var in env_vars if env_var["key"].strip()}
        if not keys:
            return
        existing_names = {
            row.env_var_name
            for row in db.query(ProjectEnvVar.env_var_name).filter(
                ProjectEnvVar.project_id == project.project_id,
                ProjectEnvVar.env_var_name.in_(keys)
            ).all()
        }
        new_records = [
            ProjectEnvVar(project_id=project.project_id, env_var_name=key)
            for key in keys
            if key not in existing_names
        ]
        if new_records:
            db.add_all(new_records)
        db.commit()
        print(f"✅ Stored {len(env_vars)} env var names locally")
    except SQLAlchemyError as e:
        db.rollback()
        print(f"⚠️ Warning: Could not store env var names: {str(e)}")


@router.post("/api/update-env-vars", responses=PROJECT_WRITE_RESPONSES)
async def update_env_vars(request: Request, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Updates GitHub repository environment variables"""
    try:
        # Validate request data
        data = await request.json()
        repo_names, env_vars, project_name = _validate_request_data(data)
        # The caller selects whose GitHub token is used, so it comes from the
        # session (#2241); a "user" the body still carries is ignored. Write
        # access is required: this route changes configuration in the
        # project's repositories.
        user = account.github_user
        project = require_project_write_access(db, request, user, project_name)
        _require_project_repos(db, project, repo_names)
        # Blank rows are dropped, as the panel does; anything else is formatted,
        # and so checked, before the limit check or any write.
        env_vars = [
            env_var for env_var in env_vars
            if not isinstance(env_var.get("key"), str) or env_var["key"].strip()
        ]

        # Check authentication
        headers = _get_auth_headers(user)
        if not headers:
            return {"error": AUTH_STRING, "status": 401}

        project_code = project.project_code.upper()
        use_prefix = project.use_prefix  # Get prefix setting from project
        print(f"📌 Debug: Using Project Code: {project_code}, Use Prefix: {use_prefix}")
        formatted = [
            (_format_env_var_key(env_var.get("key"), project_code, use_prefix), env_var["value"].strip())
            for env_var in env_vars
        ]

        # Check free account limits
        limit_error = await _check_free_account_limits(account, user, project_code, repo_names, env_vars, headers, use_prefix, project.project_id, db)
        if limit_error:
            return limit_error

        # Update variables in all repositories
        results = {}
        for repo_name in repo_names:
            for formatted_key, value in formatted:
                status_code = await _update_or_create_variable(repo_name, formatted_key, value, headers)
                results[f"{repo_name}/env/{formatted_key}"] = status_code

        # If prefix is disabled, store env var names locally (without values)
        if not use_prefix:
            _store_env_var_names_in_db(env_vars, project, db)

        return {"message": "✅ GitHub Repository Variables updated!", "results": results}
    
    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error updating env vars: {str(e)}")
        return {"error": "Error updating env vars", "status": 500}


async def _fetch_all_github_variables(client, repo_name: str, headers: dict) -> list | None:
    """Fetches all GitHub Actions variables for a repo with pagination. Returns None on error."""
    variables, _status = await _list_all(
        client, _repo_url(repo_name, "actions", "variables"), "variables", VARIABLES_PAGE, headers,
    )
    return variables


def _get_prefix_mode_vars(all_env_vars: list, repo_name: str, project_code: str) -> list:
    """Collects env vars matching the AM_<PROJECT_CODE>_ prefix, with the values the listing carries."""
    prefix = f"AM_{project_code}_"
    project_env_vars = [
        {"env_key": var["name"], "value": var.get("value", "N/A"), "repo": repo_name}
        for var in all_env_vars
        if var["name"].startswith(prefix)
    ]
    print(f"✅ [PREFIX MODE] Found {len(project_env_vars)} variables with prefix {prefix}")
    return project_env_vars


def _get_no_prefix_mode_vars(all_env_vars: list, repo_name: str, project, db) -> list:
    """Retrieves env var names from DB and keeps those GitHub lists, with their values."""
    db_env_vars = db.query(ProjectEnvVar).filter(ProjectEnvVar.project_id == project.project_id).all()
    env_var_names_from_db = {ev.env_var_name for ev in db_env_vars}
    values = {var["name"]: var.get("value", "N/A") for var in all_env_vars}

    project_env_vars = [
        {"env_key": name, "value": values[name], "repo": repo_name}
        for name in env_var_names_from_db
        if name in values
    ]

    print(f"✅ [NO PREFIX MODE] Found {len(project_env_vars)} variables from database (out of {len(env_var_names_from_db)} stored)")
    return project_env_vars


@router.get("/api/get-env-vars", responses=PROJECT_READ_RESPONSES)
async def get_env_vars(repo_name: str, project_name: str, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Fetches GitHub environment variables for a project.
    - If use_prefix=True: Filters environment variables matching 'AM_<PROJECT_CODE>_*' format
    - If use_prefix=False: Retrieves env var names from ProjectEnvVar table and verifies they exist in GitHub
    """
    try:
        # The session's caller, whose token reads the repository (#2241).
        user = account.github_user
        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        token = user_tokens[user]
        headers = {
            "Authorization": f"token {token}",
            "Accept":  ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION
        }

        project = _find_project_by_name(db, user, project_name)
        if not project:
            print(f"❌ Project '{project_name}' not found in DB")
            return {"error": f"Project '{project_name}' not found in database", "status": 404}

        project_code = project.project_code.upper()
        use_prefix = project.use_prefix
        print(f"📌 Found Project Code (Uppercase): {project_code}, use_prefix: {use_prefix}")

        if not project_code:
            return {"error": "Project code not found", "status": 400}

        # A repository ticked but not saved yet is not the project's: it answers
        # with an error body, as an unsaved project does, so the panel lists
        # nothing for it rather than failing every repository's fetch.
        if not _project_owns_repo(db, project, repo_name):
            return {"error": f"Repository '{repo_name}' is not part of project '{project_name}'", "status": 400}

        async with httpx.AsyncClient() as client:
            all_env_vars = await _fetch_all_github_variables(client, repo_name, headers)
        if all_env_vars is None:
            return {"error": f"Failed to fetch environment variables from GitHub for {repo_name}"}

        if use_prefix:
            project_env_vars = _get_prefix_mode_vars(all_env_vars, repo_name, project_code)
        else:
            project_env_vars = _get_no_prefix_mode_vars(all_env_vars, repo_name, project, db)

        print(f"📌 Debug: Returning {len(project_env_vars)} environment variables for {repo_name}")

        return {"env_vars": project_env_vars}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error in /api/get-env-vars: {str(e)}")
        return {"error": "Error getting env vars"}


@router.delete("/api/delete-env-vars", responses=PROJECT_WRITE_RESPONSES)
async def delete_env_vars(request: Request, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Deletes GitHub repository environment variables"""
    try:
        data = await request.json()
        user = account.github_user
        project_name = _project_name(data)
        repo_names = _validate_repo_names(data.get("repo_names"))
        env_vars = data.get("env") if isinstance(data.get("env"), list) else []

        # The session's caller selects whose GitHub token is used. Write access
        # to the project is required because this route changes configuration
        # in its repositories.
        project = require_project_write_access(db, request, user, project_name)
        _require_project_repos(db, project, repo_names)

        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        token = user_tokens[user]
        headers = {
            "Authorization": f"token {token}",
            "Accept":  ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION
        }

        project_code = project.project_code.upper()
        use_prefix = project.use_prefix
        formatted_keys = [_format_env_var_key(env_var.get("env_key"), project_code, use_prefix) for env_var in env_vars]

        results = {}
        async with httpx.AsyncClient() as client:
            for repo_name in repo_names:
                for formatted_key in formatted_keys:
                    delete_var_url = _repo_url(repo_name, "actions", "variables", formatted_key)

                    delete_response = await client.delete(delete_var_url, headers=headers)

                    results[f"{repo_name}/env/{formatted_key}"] = delete_response.status_code

        # If prefix is disabled, remove env var names from local database
        if not use_prefix:
            keys_to_delete = {env_var["env_key"].strip().upper() for env_var in env_vars if env_var["env_key"].strip()}
            if keys_to_delete:
                try:
                    db.query(ProjectEnvVar).filter(
                        ProjectEnvVar.project_id == project.project_id,
                        ProjectEnvVar.env_var_name.in_(keys_to_delete)
                    ).delete(synchronize_session=False)
                    db.commit()
                except SQLAlchemyError as e:
                    db.rollback()
                    print(f"⚠️ Warning: Could not remove env var names from DB: {str(e)}")

        return {"message": "✅ GitHub Repository Variables deleted!", "results": results}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Exception: {str(e)}")
        return {"error": "Error deleting env vars", "status": 500}


async def _sync_env_var_to_repo(client, repo_name: str, formatted_key: str, env_value: str, headers: dict):
    """Check if a variable exists in a repo and create it if missing."""
    check_response = await client.get(_repo_url(repo_name, "actions", "variables", formatted_key), headers=headers)

    if check_response.status_code == 404:
        create_var_url = _repo_url(repo_name, "actions", "variables")
        create_payload = {"name": formatted_key, "value": env_value}
        create_response = await client.post(create_var_url, json=create_payload, headers=headers)
        if create_response.status_code == 201:
            print(f"✅ Created env var '{formatted_key}' in {repo_name}")
        else:
            print(f"❌ Failed to create env var '{formatted_key}' in {repo_name}: {create_response.status_code}")
        return create_response.status_code

    print(f"ℹ️ Env var '{formatted_key}' already exists in {repo_name}")
    return "already_exists"


@router.post("/api/sync-env-var", responses=PROJECT_WRITE_RESPONSES)
async def sync_env_var(request: Request, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Syncs a specific environment variable to all repositories that don't have it"""
    try:
        data = await request.json()
        user = account.github_user
        project_name = _project_name(data)
        repo_names = _validate_repo_names(data.get("repo_names"))
        env_key = _require_valid_key(data.get("env_key"))

        # The session's caller selects whose GitHub token is used. Write access
        # to the project is required because this route changes configuration
        # in its repositories.
        project = require_project_write_access(db, request, user, project_name)
        _require_project_repos(db, project, repo_names)

        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        token = user_tokens[user]
        headers = {
            "Authorization": f"token {token}",
            "Accept":  ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION
        }

        # Allow sync for free accounts since they're not creating new variables, just syncing existing ones

        project_code = project.project_code.upper()
        use_prefix = project.use_prefix
        
        formatted_key = _format_env_var_key(env_key, project_code, use_prefix)

        # Find a repository that has this variable to get its value
        env_value = None
        source_repo = None
        
        async with httpx.AsyncClient() as client:
            for repo_name in repo_names:
                check_response = await client.get(_repo_url(repo_name, "actions", "variables", formatted_key), headers=headers)
                
                if check_response.status_code == 200:
                    var_data = check_response.json()
                    env_value = var_data.get("value")
                    source_repo = repo_name
                    break

            if not env_value:
                return {"error": f"Environment variable '{env_key}' not found in any repository", "status": 404}

            print(f"📌 Syncing env var '{env_key}' with value from {source_repo} to missing repos")

            # Sync to repositories that don't have it
            results = {}
            for repo_name in repo_names:
                result = await _sync_env_var_to_repo(client, repo_name, formatted_key, env_value, headers)
                results[f"{repo_name}/env/{formatted_key}"] = result

        return {"message": f"✅ Environment variable '{env_key}' synced!", "results": results}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error syncing env var: {str(e)}")
        return {"error": "Error syncing env var", "status": 500}


async def _check_environment_limit(user: str, repo_name: str, environment_exists: bool, user_obj):
    """Check tier-based environment creation limits. Returns an error dict or None."""
    from tier_service import environment_limit, environment_limit_message

    limit = environment_limit(user_obj)
    if limit is None:
        return None

    repo_list = [repo_name] if repo_name else []
    current_count = await count_project_environments(user, repo_list)
    if not environment_exists and current_count >= limit:
        return {
            "error": environment_limit_message(limit, current_count),
            "status": 403,
            "current_count": current_count,
            "limit": limit,
        }
    return None


@router.post("/api/create-environment", responses=REPO_WRITE_RESPONSES)
async def create_environment(request: Request, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Creates a new deploy environment in a GitHub repository."""
    try:
        data = await request.json()
        user = account.github_user
        repo_name = data.get("repo_name")
        environment_name = data.get("environment_name")

        # Refused with a real 400, before any write: the panel's create loop
        # treats only a rejected request as a failure.
        if not isinstance(repo_name, str) or not repo_name.strip():
            raise HTTPException(status_code=400, detail="Missing or invalid repository name")
        if not _is_repo_name(repo_name):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid repository name format: '{repo_name}'. Expected format: 'owner/repo'",
            )

        repo_name = repo_name.strip()
        if isinstance(environment_name, str) and len(environment_name.strip()) > 255:
            raise HTTPException(status_code=400, detail="Environment name is too long")
        environment_name = _valid_environment_name(environment_name)
        if environment_name is None:
            raise HTTPException(status_code=400, detail=INVALID_ENVIRONMENT_NAME)

        # No project_name on this route - an environment is created on a
        # repository. A caller may configure a repository belonging to a
        # project they can edit; GitHub's own permissions were the only
        # check before, and those are not ActionsManager's role model.
        require_repo_write_access(db, request, user, repo_name)

        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        token = user_tokens[user]
        headers = {
            "Authorization": f"token {token}",
            "Accept":  ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION
        }

        env_url = _repo_url(repo_name, "environments", environment_name)

        async with httpx.AsyncClient() as client:
            check_response = await client.get(env_url, headers=headers)
            environment_exists = check_response.status_code == 200

            limit_error = await _check_environment_limit(user, repo_name, environment_exists, account)
            if limit_error:
                return limit_error

            if environment_exists:
                return {
                    "message": f"Environment '{environment_name}' already exists - no changes made.",
                    "created": False
                }

            response = await client.put(env_url, headers=headers, json={})
            if response.status_code in [200, 201]:
                return {"message": f"Environment '{environment_name}' created successfully.", "created": True}
            else:
                return {"error": response.json(), "status": response.status_code}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error creating environment: {str(e)}")
        return {"error": "Error creating environment", "status": 500}


@router.get("/api/get-environments", responses=REPO_READ_RESPONSES)
async def get_environments(repo_name: str, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Fetches all deploy environments for a GitHub repository."""
    try:
        user = account.github_user
        if not _is_repo_name(repo_name):
            raise HTTPException(status_code=400, detail=INVALID_REPO_NAME)
        repo_name = repo_name.strip()
        _require_repos_visible(db, account, [repo_name])
        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        token = user_tokens[user]
        headers = {
            "Authorization": f"token {token}",
            "Accept":  ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION
        }

        async with httpx.AsyncClient() as client:
            environments, status = await _list_environments(client, repo_name, headers)
        if environments is None:
            return {"error": f"Failed to fetch environments for {repo_name}", "status": status}
        return {"environments": environments}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error fetching environments: {str(e)}")
        return {"error": "Error fetching environments", "status": 500}


@router.delete("/api/delete-environment", responses=REPO_WRITE_RESPONSES)
async def delete_environment(request: Request, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Deletes a deployment environment from GitHub repositories."""
    try:
        data = await request.json()
        user = account.github_user
        repo_names = data.get("repo_names", [])
        environment_name = data.get("environment_name", "")

        if not isinstance(repo_names, list) or len(repo_names) == 0:
            raise HTTPException(status_code=400, detail=ERROR_CODE)
        repo_names = _validate_repo_names(repo_names)
        environment_name = _valid_environment_name(environment_name)
        if environment_name is None:
            raise HTTPException(status_code=400, detail=INVALID_ENVIRONMENT_NAME)

        # As create-environment: repo-scoped, so authorization comes from the
        # projects those repositories belong to.
        for _repo in repo_names:
            require_repo_write_access(db, request, user, _repo)

        print(f"Request received to delete environment: {environment_name}")
        print(f"User: {user}, Repositories: {repo_names}")

        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        token = user_tokens[user]
        headers = {
            "Authorization": f"token {token}",
            "Accept":  ACCEPT_HEADER,
            "X-GitHub-Api-Version": X_API_VERSION
        }

        results = {}
        async with httpx.AsyncClient() as client:
            for repo_name in repo_names:
                delete_env_url = _repo_url(repo_name, "environments", environment_name)

                print(f"Sending DELETE request to: {delete_env_url}")
                response = await client.delete(delete_env_url, headers=headers)
                print(f"Response for {repo_name}: {response.status_code}, {response.text}")

                results[f"{repo_name}/environment/{environment_name}"] = response.status_code

        return {"message": "✅ Deployment environments deleted!", "results": results}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Exception: {str(e)}")
        return {"error": "Error deleting environment", "status": 500}


@router.post("/api/sync-environment", responses=PROJECT_WRITE_RESPONSES)
async def sync_environment(request: Request, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Syncs a deployment environment to all repositories that don't have it"""
    try:
        data = await request.json()
        project_name, repo_names, environment_name = _validate_sync_environment_request(data)
        # As update-env-vars: the session's caller, and a write to the
        # project's repositories.
        user = account.github_user
        project = require_project_write_access(db, request, user, project_name)
        _require_project_repos(db, project, repo_names)

        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        headers = _get_auth_headers(user)

        async with httpx.AsyncClient() as client:
            existing_environments = await _find_existing_environments(repo_names, environment_name, client, headers)
            missing = [repo for repo in repo_names if repo not in existing_environments]

            limit_check = await _check_free_account_environment_limits(user, repo_names, len(missing), account)
            if limit_check:
                return limit_check

            if True not in existing_environments.values():
                unchecked = [repo for repo, answer in existing_environments.items() if answer is not True]
                if unchecked:
                    return {
                        "error": f"Could not check environment '{environment_name}' in {', '.join(unchecked)}",
                        "status": 502,
                    }
                return {"error": f"Environment '{environment_name}' not found in any repository", "status": 404}

            # Create environment in repositories that don't have it
            results, created_count = await _create_missing_environments(repo_names, environment_name, existing_environments, client, headers)

        return {
            "message": f"✅ Environment '{environment_name}' synced! Created in {created_count} repositories.",
            "results": results,
        }

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error syncing environment: {str(e)}")
        return {"error": "Error syncing environment", "status": 500}


@router.get("/api/env-vars-count", responses=PROJECT_READ_RESPONSES)
async def get_env_vars_count(project_name: str, repo_names: str, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Get the count of environment variables for a project"""
    try:
        user = account.github_user
        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        # Parse repo_names parameter (comma-separated string)
        repo_list = [repo.strip() for repo in repo_names.split(",") if repo.strip()]
        
        if not repo_list:
            return {"error": ERROR_CODE, "status": 400}

        # Get project code
        project = _find_project_by_name(db, user, project_name)
        if not project:
            return {"error": f"Project '{project_name}' not found in database", "status": 404}

        project_code = project.project_code.upper()

        # Only the project's own repositories hold its variables; one ticked
        # but not saved yet adds nothing. A no-prefix project is counted from
        # the names it stores, since its variables carry no prefix to match.
        repo_list = [repo for repo in repo_list if _project_owns_repo(db, project, repo)]
        count = await count_project_env_vars(
            user, project_code, repo_list, project.use_prefix, project.project_id, db,
        )
        
        return {"count": count}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error getting env vars count: {str(e)}")
        return {"error": "Error getting env vars count", "status": 500}


@router.get("/api/environments-count", responses=REPO_READ_RESPONSES)
async def get_environments_count(repo_names: str, db: Annotated[Session, Depends(get_db)], account: auth_module.CurrentAccount):
    """Get the count of deployment environments for a project"""
    try:
        user = account.github_user
        if user not in user_tokens:
            return {"error": AUTH_STRING, "status": 401}

        # Parse repo_names parameter (comma-separated string)
        repo_list = [repo.strip() for repo in repo_names.split(",") if repo.strip()]
        
        if not repo_list:
            return {"error": ERROR_CODE, "status": 400}

        if not all(_is_repo_name(repo) for repo in repo_list):
            raise HTTPException(status_code=400, detail=INVALID_REPO_NAMES)
        _require_repos_visible(db, account, repo_list)
        count = await count_project_environments(user, repo_list)
        
        return {"count": count}

    except HTTPException:
        # A deliberate status - the authorization refusal above, or a 404 -
        # must reach the client as that status. The handler below turns
        # anything it catches into a 200 carrying an error body, which is a
        # refused write reported as success.
        raise
    except Exception as e:
        print(f"❌ Error getting environments count: {str(e)}")
        return {"error": "Error getting environments count", "status": 500}
