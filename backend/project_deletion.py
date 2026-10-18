"""
Project Deletion API Module

Deleting a project either keeps its files in GitHub or removes them by a last
PR Campaign (workflows.start_project_removal). Separately, and at once, it can
delete the project's secrets, variables and deployment environments, which no
pull request can carry. The summary lists both before the user chooses.
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import Annotated, List, Dict, Any, Optional, Set
import requests
from pydantic import BaseModel

from database import get_db
import auth as auth_module
from auth import user_tokens
from models import Project, Account, Repo, Workflow, ProjectRepo, ProjectWorkflow, ProjectSecret, ProjectEnvVar
from authorization import PROJECT_DELETING, find_project_by_name, has_project_role
from workflows import (
    GITHUB_TIMEOUT_SECONDS,
    ProjectDeletionOutcome,
    _deleted_now,
    _responses,
    project_hint,
    project_removal_blocker,
    project_removal_plan,
    start_project_removal,
)
from error_detail import failure_kind

router = APIRouter()

_DEBUG_NO_PREFIX_LOOKUP = "🔍 Debug: Using database lookup for no_prefix project"

GITHUB_API_URL = "https://api.github.com"


class ProjectDeletionSummary(BaseModel):
    """What deleting a project would affect.

    ``removal_targets`` and ``unrecorded_files`` are workflows.project_removal_plan's,
    read from the database alone. ``removal_blocked_reason`` says why the project
    cannot be deleted with its files now (workflows.project_removal_blocker).
    """
    project_name: str
    project_code: str
    workflows: List[Dict[str, Any]]
    reusable_workflows: List[Dict[str, Any]]
    secrets: List[Dict[str, Any]]
    environment_variables: List[Dict[str, Any]]
    deployment_environments: List[Dict[str, Any]]
    removal_targets: List[Dict[str, Any]] = []
    unrecorded_files: List[Dict[str, Any]] = []
    removal_blocked_reason: Optional[str] = None

class DeleteProjectRequest(BaseModel):
    """Request model for project deletion.

    ``remove_files`` removes the project's files from GitHub by pull request
    before it goes. ``delete_github_resources`` deletes its secrets, variables
    and, unless ``delete_deployment_environments`` is False, its deployment
    environments, directly and at once.
    """
    project_name: str
    remove_files: bool = False
    delete_github_resources: bool = False
    delete_deployment_environments: bool = True


ALREADY_DELETING = "This project is already being deleted: merge or close its removal pull requests."


def _github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def _tracked_names_for_project(project: Optional[Project], db: Optional[Session], model, name_attr: str) -> Optional[Set[str]]:
    """DB-tracked names for a no-prefix project, or None to signal prefix matching should be used instead."""
    if not (project and db and not project.use_prefix):
        return None
    print(_DEBUG_NO_PREFIX_LOOKUP)
    rows = db.query(model).filter(model.project_id == project.project_id).all()
    tracked_names = {getattr(row, name_attr) for row in rows}
    print(f"🔍 Debug: Tracked names in database: {tracked_names}")
    return tracked_names


def _belongs_to_project(name: str, tracked_names: Optional[Set[str]], project_prefix: str) -> bool:
    """Whether a GitHub secret/variable name belongs to the project, per tracked-names-or-prefix rule shared by every fetch/delete function below."""
    if tracked_names is not None:
        return name in tracked_names
    return bool(project_prefix) and name.startswith(project_prefix)


def _fetch_repository_secrets(repo_name: str, headers: Dict[str, str], project_prefix: str, project: Project = None, db: Session = None) -> List[Dict[str, Any]]:
    """Fetch repository secrets that match the project prefix or are tracked in database."""
    secrets = []
    secrets_url = f"{GITHUB_API_URL}/repos/{repo_name}/actions/secrets"
    
    try:
        response = requests.get(secrets_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        print(f"🔍 Debug: Repository secrets API response: {response.status_code}")
        
        if response.status_code == 200:
            secrets_data = response.json()
            all_secrets = secrets_data.get('secrets', [])
            print(f"🔍 Debug: Found {len(all_secrets)} total repository secrets")
            
            if all_secrets:
                print(f"🔍 Debug: All secret names: {[s['name'] for s in all_secrets]}")

                tracked_names = _tracked_names_for_project(project, db, ProjectSecret, "secret_name")
                matching_secrets = [s for s in all_secrets if _belongs_to_project(s["name"], tracked_names, project_prefix)]

                print(f"🔍 Debug: Secrets matching project: {len(matching_secrets)}")
                
                if matching_secrets:
                    print(f"🔍 Debug: Matching secret names: {[s['name'] for s in matching_secrets]}")
                
                for secret in matching_secrets:
                    secrets.append({
                        "name": secret["name"],
                        "repository": repo_name,
                        "created_at": secret.get("created_at"),
                        "updated_at": secret.get("updated_at")
                    })
                    print(f"✅ Added repository secret: {secret['name']}")
            else:
                print("🔍 Debug: No repository secrets found")
        else:
            print(f"❌ Repository secrets API failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Error fetching repository secrets for {repo_name}: {str(e)}")
    
    return secrets


def _fetch_repository_variables(repo_name: str, headers: Dict[str, str], project_prefix: str, project: Project = None, db: Session = None) -> List[Dict[str, Any]]:
    """Fetch repository variables that match the project prefix or are tracked in database."""
    variables = []
    variables_url = f"{GITHUB_API_URL}/repos/{repo_name}/actions/variables"
    
    try:
        response = requests.get(variables_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        print(f"🔍 Debug: Repository variables API response: {response.status_code}")
        
        if response.status_code == 200:
            variables_data = response.json()
            all_variables = variables_data.get('variables', [])
            print(f"🔍 Debug: Found {len(all_variables)} total repository variables")
            
            if all_variables:
                print(f"🔍 Debug: All repository variable names: {[v['name'] for v in all_variables]}")

                tracked_names = _tracked_names_for_project(project, db, ProjectEnvVar, "env_var_name")
                matching_variables = [v for v in all_variables if _belongs_to_project(v["name"], tracked_names, project_prefix)]

                print(f"🔍 Debug: Repository variables matching project: {len(matching_variables)}")
                
                if matching_variables:
                    print(f"🔍 Debug: Matching repository variable names: {[v['name'] for v in matching_variables]}")
                
                for variable in matching_variables:
                    variables.append({
                        "name": variable["name"],
                        "value": variable.get("value", ""),
                        "repository": repo_name,
                        "environment": "repository",
                        "created_at": variable.get("created_at"),
                        "updated_at": variable.get("updated_at")
                    })
                    print(f"✅ Added repository variable: {variable['name']}")
            else:
                print("🔍 Debug: No repository variables found")
        else:
            print(f"❌ Repository variables API failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Error fetching repository variables for {repo_name}: {str(e)}")
    
    return variables


def _fetch_environment_secrets(repo_name: str, env_name: str, headers: Dict[str, str], project_prefix: str, project: Project = None, db: Session = None) -> List[Dict[str, Any]]:
    """Fetch environment secrets that match the project prefix or are tracked in database."""
    secrets = []
    env_secrets_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments/{env_name}/secrets"
    
    try:
        response = requests.get(env_secrets_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if response.status_code == 200:
            env_secrets_data = response.json()
            all_env_secrets = env_secrets_data.get('secrets', [])
            print(f"🔍 Debug: Found {len(all_env_secrets)} environment secrets in '{env_name}'")
            
            if all_env_secrets:
                print(f"🔍 Debug: Secret names in '{env_name}': {[s['name'] for s in all_env_secrets]}")

                tracked_names = _tracked_names_for_project(project, db, ProjectSecret, "secret_name")
                for secret in all_env_secrets:
                    if not _belongs_to_project(secret["name"], tracked_names, project_prefix):
                        continue
                    secrets.append({
                        "name": secret["name"],
                        "repository": repo_name,
                        "environment": env_name,
                        "created_at": secret.get("created_at"),
                        "updated_at": secret.get("updated_at")
                    })
                    print(f"✅ Added environment secret: {secret['name']}")
        else:
            print(f"❌ Environment secrets API failed for '{env_name}': {response.status_code}")
    except Exception as e:
        print(f"❌ Error fetching environment secrets for {repo_name}/{env_name}: {str(e)}")
    
    return secrets


def _fetch_environment_variables(repo_name: str, env_name: str, headers: Dict[str, str], project_prefix: str, project: Project = None, db: Session = None) -> List[Dict[str, Any]]:
    """Fetch environment variables that match the project prefix or are tracked in database."""
    variables = []
    env_vars_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments/{env_name}/variables"
    
    try:
        response = requests.get(env_vars_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if response.status_code == 200:
            env_vars_data = response.json()
            all_env_vars = env_vars_data.get('variables', [])
            print(f"🔍 Debug: Found {len(all_env_vars)} environment variables in '{env_name}'")
            
            if all_env_vars:
                print(f"🔍 Debug: Variable names in '{env_name}': {[v['name'] for v in all_env_vars]}")

                tracked_names = _tracked_names_for_project(project, db, ProjectEnvVar, "env_var_name")
                for var in all_env_vars:
                    if not _belongs_to_project(var["name"], tracked_names, project_prefix):
                        continue
                    variables.append({
                        "name": var["name"],
                        "value": var["value"],
                        "repository": repo_name,
                        "environment": env_name,
                        "created_at": var.get("created_at"),
                        "updated_at": var.get("updated_at")
                    })
                    print(f"✅ Added environment variable: {var['name']}")
        else:
            print(f"❌ Environment variables API failed for '{env_name}': {response.status_code}")
    except Exception as e:
        print(f"❌ Error fetching environment variables for {repo_name}/{env_name}: {str(e)}")
    
    return variables


def _fetch_deployment_environments(repo_name: str, headers: Dict[str, str]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch deployment environments and their associated secrets/variables."""
    environments = []
    all_env_secrets = []
    all_env_variables = []
    
    environments_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments"
    
    try:
        response = requests.get(environments_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        print(f"🔍 Debug: Environments API response: {response.status_code}")
        
        if response.status_code == 200:
            environments_data = response.json()
            all_environments = environments_data.get('environments', [])
            print(f"🔍 Debug: Found {len(all_environments)} total environments")
            
            if all_environments:
                print(f"🔍 Debug: All environment names: {[e['name'] for e in all_environments]}")
                print(f"🔍 Debug: Adding all {len(all_environments)} deployment environments (no prefix filtering)")
                
                for env in all_environments:
                    print(f"\n🔍 Debug: Processing environment '{env['name']}' (deployment environments don't use prefix)")
                    
                    environments.append({
                        "name": env["name"],
                        "repository": repo_name,
                        "created_at": env.get("created_at"),
                        "updated_at": env.get("updated_at")
                    })
                    print(f"✅ Added deployment environment: {env['name']}")
            else:
                print("🔍 Debug: No environments found")
        else:
            print(f"❌ Environments API failed: {response.status_code}")
    except Exception as e:
        print(f"❌ Error fetching environments for {repo_name}: {str(e)}")
    
    return environments, all_env_secrets, all_env_variables


def _process_single_environment(repo_name: str, env_name: str, headers: Dict[str, str], project_prefix: str, project: Project = None, db: Session = None) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Process secrets and variables for a single environment."""
    env_secrets = _fetch_environment_secrets(repo_name, env_name, headers, project_prefix, project, db)
    env_variables = _fetch_environment_variables(repo_name, env_name, headers, project_prefix, project, db)
    return env_secrets, env_variables


def _validate_repository_access(repo_name: str, headers: Dict[str, str]) -> bool:
    """Test repository accessibility and log access status."""
    repo_url = f"{GITHUB_API_URL}/repos/{repo_name}"
    
    try:
        response = requests.get(repo_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        print(f"🔍 Debug: Repository '{repo_name}' accessibility: {response.status_code}")
        
        if response.status_code != 200:
            if response.status_code == 404:
                print(f"❌ Repository '{repo_name}' not found or no access")
            elif response.status_code == 401:
                print(f"❌ Authentication failed for repository '{repo_name}'")
            elif response.status_code == 403:
                print(f"❌ Access forbidden for repository '{repo_name}'")
            return False
        return True
    except Exception as e:
        print(f"❌ Error validating repository access for {repo_name}: {str(e)}")
        return False


def _process_repository_resources(repo_name: str, headers: Dict[str, str], project_prefix: str, project: Project = None, db: Session = None) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Process all GitHub resources for a single repository."""
    print(f"\n🔍 Debug: Processing repository: {repo_name}")
    
    # Validate repository access first
    if not _validate_repository_access(repo_name, headers):
        return [], [], []
    
    # Collect repository-level resources
    repo_secrets = _fetch_repository_secrets(repo_name, headers, project_prefix, project, db)
    repo_variables = _fetch_repository_variables(repo_name, headers, project_prefix, project, db)
    
    # Collect deployment environments and their resources
    environments, _, _ = _fetch_deployment_environments(repo_name, headers)
    
    # Process environment-specific resources
    all_env_secrets = []
    all_env_variables = []
    
    for env in environments:
        env_secrets, env_variables = _process_single_environment(
            repo_name, env["name"], headers, project_prefix, project, db
        )
        all_env_secrets.extend(env_secrets)
        all_env_variables.extend(env_variables)
    
    # Combine all resources
    all_secrets = repo_secrets + all_env_secrets
    all_variables = repo_variables + all_env_variables
    
    return all_secrets, all_variables, environments


def _delete_repository_secrets(repo_name: str, headers: Dict[str, str], project_prefix: str, deletion_results: Dict[str, Any], project: Project = None, db: Session = None) -> None:
    """Delete repository secrets that match the project prefix or are tracked in database."""
    secrets_url = f"{GITHUB_API_URL}/repos/{repo_name}/actions/secrets"
    
    try:
        secrets_response = requests.get(secrets_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if secrets_response.status_code == 200:
            secrets_data = secrets_response.json()
            tracked_names = _tracked_names_for_project(project, db, ProjectSecret, "secret_name")

            for secret in secrets_data.get("secrets", []):
                if not _belongs_to_project(secret["name"], tracked_names, project_prefix):
                    continue
                delete_secret_url = f"{GITHUB_API_URL}/repos/{repo_name}/actions/secrets/{secret['name']}"
                delete_response = requests.delete(delete_secret_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

                if delete_response.status_code == 204:
                    deletion_results["github_resources_deleted"].append(f"Repository Secret: {secret['name']} from {repo_name}")
                else:
                    deletion_results["errors"].append(f"Failed to delete repository secret {secret['name']} from {repo_name}")
    except Exception as e:
        print(f"❌ Error deleting repository secrets from {repo_name}: {e}")
        deletion_results["errors"].append(f"Error deleting repository secrets from {repo_name}: {failure_kind(e)}")


def _delete_repository_variables(repo_name: str, headers: Dict[str, str], project_prefix: str, deletion_results: Dict[str, Any], project: Project = None, db: Session = None) -> None:
    """Delete repository variables that match the project prefix or are tracked in database."""
    variables_url = f"{GITHUB_API_URL}/repos/{repo_name}/actions/variables"
    
    try:
        variables_response = requests.get(variables_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if variables_response.status_code == 200:
            variables_data = variables_response.json()
            tracked_names = _tracked_names_for_project(project, db, ProjectEnvVar, "env_var_name")

            for variable in variables_data.get("variables", []):
                if not _belongs_to_project(variable["name"], tracked_names, project_prefix):
                    continue
                delete_variable_url = f"{GITHUB_API_URL}/repos/{repo_name}/actions/variables/{variable['name']}"
                delete_response = requests.delete(delete_variable_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

                if delete_response.status_code == 204:
                    deletion_results["github_resources_deleted"].append(f"Repository Variable: {variable['name']} from {repo_name}")
                else:
                    deletion_results["errors"].append(f"Failed to delete repository variable {variable['name']} from {repo_name}")
    except Exception as e:
        print(f"❌ Error deleting repository variables from {repo_name}: {e}")
        deletion_results["errors"].append(f"Error deleting repository variables from {repo_name}: {failure_kind(e)}")


def _delete_deployment_environments(repo_name: str, headers: Dict[str, str], deletion_results: Dict[str, Any]) -> None:
    """Delete all deployment environments for the repository."""
    environments_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments"
    
    try:
        env_response = requests.get(environments_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if env_response.status_code == 200:
            environments_data = env_response.json()
            for env in environments_data.get("environments", []):
                delete_env_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments/{env['name']}"
                delete_response = requests.delete(delete_env_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
                
                if delete_response.status_code == 204:
                    deletion_results["github_resources_deleted"].append(f"Deployment Environment: {env['name']} from {repo_name}")
                else:
                    deletion_results["errors"].append(f"Failed to delete deployment environment {env['name']} from {repo_name}")
    except Exception as e:
        print(f"❌ Error deleting deployment environments from {repo_name}: {e}")
        deletion_results["errors"].append(f"Error deleting deployment environments from {repo_name}: {failure_kind(e)}")


def _delete_environment_secrets(repo_name: str, headers: Dict[str, str], project_prefix: str, deletion_results: Dict[str, Any], project: Project = None, db: Session = None) -> None:
    """Delete environment-scoped secrets that match the project prefix or are tracked in database."""
    environments_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments"
    
    try:
        env_response = requests.get(environments_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
        
        if env_response.status_code == 200:
            environments_data = env_response.json()
            tracked_names = _tracked_names_for_project(project, db, ProjectSecret, "secret_name")

            for env in environments_data.get("environments", []):
                env_secrets_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments/{env['name']}/secrets"
                env_secrets_response = requests.get(env_secrets_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

                if env_secrets_response.status_code != 200:
                    continue

                env_secrets_data = env_secrets_response.json()
                for secret in env_secrets_data.get("secrets", []):
                    if not _belongs_to_project(secret["name"], tracked_names, project_prefix):
                        continue
                    delete_env_secret_url = f"{GITHUB_API_URL}/repos/{repo_name}/environments/{env['name']}/secrets/{secret['name']}"
                    delete_response = requests.delete(delete_env_secret_url, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)

                    if delete_response.status_code == 204:
                        deletion_results["github_resources_deleted"].append(f"Environment Secret: {secret['name']} from {repo_name}/{env['name']}")
                    else:
                        deletion_results["errors"].append(f"Failed to delete environment secret {secret['name']} from {repo_name}/{env['name']}")
    except Exception as e:
        print(f"❌ Error deleting environment secrets from {repo_name}: {e}")
        deletion_results["errors"].append(f"Error deleting environment secrets from {repo_name}: {failure_kind(e)}")


def _delete_github_resources_for_repository(repo_name: str, headers: Dict[str, str], project_prefix: str, deletion_results: Dict[str, Any], project: Project = None, db: Session = None, delete_deployment_environments: bool = True) -> None:
    """Delete the project's secrets, variables and deployment environments in one repository.

    Never its files: those are removed only by pull request (workflows.start_project_removal).
    """
    try:
        print(f"🔍 Processing repository: {repo_name}")

        # Delete repository secrets with project prefix or tracked in database
        _delete_repository_secrets(repo_name, headers, project_prefix, deletion_results, project, db)

        # Delete repository variables with project prefix or tracked in database
        _delete_repository_variables(repo_name, headers, project_prefix, deletion_results, project, db)

        # Delete ALL deployment environments (they don't use project prefix), unless the user opted to keep them
        if delete_deployment_environments:
            _delete_deployment_environments(repo_name, headers, deletion_results)

        # Delete environment-scoped secrets with project prefix or tracked in database
        _delete_environment_secrets(repo_name, headers, project_prefix, deletion_results, project, db)

    except Exception as e:
        print(f"❌ Error deleting GitHub resources from {repo_name}: {e}")
        deletion_results["errors"].append(f"Error deleting GitHub resources from {repo_name}: {failure_kind(e)}")


def _delete_all_github_resources(repo_names: List[str], headers: Dict[str, str], project_code: str, deletion_results: Dict[str, Any], project: Project = None, db: Session = None, delete_deployment_environments: bool = True) -> None:
    """Delete GitHub resources across all repositories for the project."""
    # Only use prefix if project.use_prefix is True
    project_prefix = f"AM_{project_code}_" if (project and project.use_prefix) else ""

    # Delete GitHub resources for each repository
    for repo_name in repo_names:
        _delete_github_resources_for_repository(repo_name, headers, project_prefix, deletion_results, project, db, delete_deployment_environments)


def _debug_resource_summary(github_secrets: List[Dict[str, Any]], github_env_vars: List[Dict[str, Any]], 
                           github_environments: List[Dict[str, Any]], project_prefix: str) -> None:
    """Log resource discovery summary."""
    prefix_matched_count = len(github_secrets) + len(github_env_vars) + len(github_environments)
    
    print(f"\n🔍 Debug: Resource summary:")
    print(f"  - Prefix-matched resources: {prefix_matched_count}")
    print(f"✅ Only showing resources that match project prefix '{project_prefix}'")


def _build_deletion_summary(project: Project, workflows: List[Dict[str, Any]], reusable_workflows: List[Dict[str, Any]], 
                           github_secrets: List[Dict[str, Any]], github_env_vars: List[Dict[str, Any]], 
                           github_environments: List[Dict[str, Any]], removal: Dict[str, Any]) -> ProjectDeletionSummary:
    """Build the final deletion summary response."""
    print(f"🔍 Debug: Final summary:")
    print(f"  - Workflows: {len(workflows)}")
    print(f"  - Reusable workflows: {len(reusable_workflows)}")
    print(f"  - GitHub secrets: {len(github_secrets)}")
    print(f"  - Environment variables: {len(github_env_vars)}")
    print(f"  - Deployment environments: {len(github_environments)}")

    return ProjectDeletionSummary(
        project_name=project.project_name,
        project_code=project.project_code,
        workflows=workflows,
        reusable_workflows=reusable_workflows,
        secrets=github_secrets,
        environment_variables=github_env_vars,
        deployment_environments=github_environments,
        **removal,
    )


def _get_project_and_user(project_name: str, github_user: str, db: Session) -> tuple[Account, Project]:
    """The caller's account and the project to delete. Deletion is owner-only.

    The page may show another owner's project of this name (#2240). Deleting
    the caller's own instead would destroy one they are not looking at, so it
    is the page's project or nothing.
    """
    user = db.query(Account).filter(Account.github_user == github_user.strip()).first()
    if not user:
        raise HTTPException(status_code=404, detail="GitHub user not found")

    project = find_project_by_name(db, user, project_name, hint=project_hint())
    if not project or project.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Project not found or access denied")
    return user, project


def _get_project_repositories(project: Project, db: Session) -> List[str]:
    """Get repository names associated with the project."""
    return [
        db.query(Repo.repo_name).filter(Repo.repo_id == pr.repo_id).scalar()
        for pr in db.query(ProjectRepo).filter(ProjectRepo.project_id == project.project_id).all()
    ]


def _get_project_workflows(project: Project, db: Session) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Get and categorize workflows associated with the project."""
    all_workflows = db.query(Workflow) \
        .join(ProjectWorkflow, Workflow.workflow_id == ProjectWorkflow.workflow_id) \
        .filter(ProjectWorkflow.project_id == project.project_id) \
        .all()

    workflows = []
    reusable_workflows = []

    for w in all_workflows:
        workflow_info = {
            "name": w.workflow_name,
            "is_reusable": w.reusable_workflow,
            "created_at": w.created_at.isoformat() if w.created_at else None,
            "updated_at": w.updated_at.isoformat() if w.updated_at else None
        }
        
        if w.reusable_workflow:
            reusable_workflows.append(workflow_info)
        else:
            workflows.append(workflow_info)
    
    return workflows, reusable_workflows


def _log_missing_repos_debug(project: Project, db: Session) -> None:
    """Diagnostic output when a project has no repositories, to help debug data-association issues."""
    print("⚠️ DEBUG: No repositories found for this project - GitHub API calls will be skipped")
    print("🔍 DEBUG: Checking project-repository associations...")
    project_repos = db.query(ProjectRepo).filter(ProjectRepo.project_id == project.project_id).all()
    print(f"🔍 DEBUG: Found {len(project_repos)} project-repo associations")
    for pr in project_repos:
        repo = db.query(Repo).filter(Repo.repo_id == pr.repo_id).first()
        print(f"🔍 DEBUG: Repo ID {pr.repo_id} -> {repo.repo_name if repo else 'NOT FOUND'}")


def _collect_github_resources_for_deletion_summary(
    project: Project, github_user: str, repo_names: List[str], db: Session
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch GitHub secrets/env vars/environments across every project repo, tolerating per-repo failures."""
    github_secrets: List[Dict[str, Any]] = []
    github_env_vars: List[Dict[str, Any]] = []
    github_environments: List[Dict[str, Any]] = []

    if not (github_user in user_tokens and repo_names):
        print("🔍 Debug: Skipping GitHub API calls - Missing token or repos")
        print(f"  - github_user in user_tokens: {github_user in user_tokens if github_user else False}")
        print(f"  - repo_names: {repo_names}")
        return github_secrets, github_env_vars, github_environments

    headers = _github_headers(user_tokens[github_user])
    project_prefix = f"AM_{project.project_code}_" if project.use_prefix else ""
    print(f"🔍 Debug: Starting GitHub API calls, use_prefix={project.use_prefix}, prefix='{project_prefix}'")

    for repo_name in repo_names:
        try:
            repo_secrets, repo_env_vars, repo_environments = _process_repository_resources(
                repo_name, headers, project_prefix, project, db
            )
            github_secrets.extend(repo_secrets)
            github_env_vars.extend(repo_env_vars)
            github_environments.extend(repo_environments)
        except Exception as e:
            print(f"Warning: Could not fetch GitHub resources for {repo_name}: {str(e)}")

    _debug_resource_summary(github_secrets, github_env_vars, github_environments, project_prefix)
    return github_secrets, github_env_vars, github_environments


@router.get("/projects/{project_name}/deletion-summary", responses=_responses(401, 403, 404, 500))
async def get_project_deletion_summary(
    project_name: str,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """
    Get a summary of all resources that would be affected when deleting a project.

    The files a removal would take off GitHub, and whether it may, come from
    the database alone. Secrets, variables and environments are read from GitHub.
    """
    github_user = account.github_user
    try:
        # Get user and project from database
        user, project = _get_project_and_user(project_name, github_user, db)

        # Get repositories and workflows from database
        repo_names = _get_project_repositories(project, db)
        workflows, reusable_workflows = _get_project_workflows(project, db)

        print(f"🔍 Debug: github_user='{github_user}', repo_names={repo_names}, project_code='{project.project_code}'")

        if not repo_names:
            _log_missing_repos_debug(project, db)

        github_secrets, github_env_vars, github_environments = _collect_github_resources_for_deletion_summary(
            project, github_user, repo_names, db
        )
        blocker = project_removal_blocker(db, user, project)
        removal = {**project_removal_plan(db, project),
                   "removal_blocked_reason": blocker["message"] if blocker else None}

        return _build_deletion_summary(
            project, workflows, reusable_workflows, 
            github_secrets, github_env_vars, github_environments, removal
        )

    except HTTPException:
        # The lookup's 404 is the answer, not a server error.
        raise
    except Exception as e:
        print(f"Error getting project deletion summary: {str(e)}")
        raise HTTPException(status_code=500, detail="Error retrieving project deletion summary")


def _require_deletable(db: Session, user: Account, project: Project, remove_files: bool) -> None:
    """Refuse a deletion that cannot go ahead.

    409 when the project is already being deleted, which its removal pull
    requests finish or call off, and when its files cannot be removed now
    (workflows.project_removal_blocker). 403 without editor access.
    """
    if project.pr_state == PROJECT_DELETING:
        raise HTTPException(status_code=409, detail=ALREADY_DELETING)
    if not has_project_role(db, user, project, "project_editor"):
        raise HTTPException(status_code=403, detail="Insufficient project permissions. Required: project_editor")
    blocker = project_removal_blocker(db, user, project) if remove_files else None
    if blocker:
        raise HTTPException(status_code=409, detail=blocker)


def _delete_secrets_and_variables(project: Project, github_user: str, db: Session, deletion_results: Dict[str, Any],
                                  delete_deployment_environments: bool) -> None:
    """Delete the project's secrets, variables and deployment environments directly: no pull request can carry them."""
    token = user_tokens.get(github_user)
    if not token:
        return
    _delete_all_github_resources(_get_project_repositories(project, db), _github_headers(token), project.project_code,
                                 deletion_results, project, db, delete_deployment_environments)


def _delete_or_start_removal(db: Session, project: Project, github_user: str, remove_files: bool,
                             deletion_results: Dict[str, Any]) -> ProjectDeletionOutcome:
    """Delete *project* now keeping its files, or start removing them (workflows.start_project_removal)."""
    if not remove_files:
        return _deleted_now(db, project, {})
    try:
        return start_project_removal(db, project, github_user)
    except HTTPException as e:
        # Secrets deleted before the removal failed stay deleted: say which.
        if e.status_code == 502:
            e.detail = {**e.detail, "details": deletion_results}
        raise


def _failed(result) -> bool:
    return isinstance(result, dict) and result.get("status") == "error" and not result.get("nothing_to_deliver")


def _deletion_response(outcome: ProjectDeletionOutcome, deletion_results: Dict[str, Any]) -> Dict[str, Any]:
    failures = [f"{key}: {result.get('error')}" for key, result in outcome.results.items() if _failed(result)]
    message = ("✅ Project deletion completed" if outcome.project_deleted
               else "Project is being deleted: it goes once its pull requests have all merged")
    details = {**deletion_results, "project_deleted": outcome.project_deleted,
               "errors": deletion_results["errors"] + failures}
    return {"message": message, **outcome._asdict(), "details": details}


@router.delete("/projects/{project_name}/enhanced", responses=_responses(401, 403, 404, 409, 500, 502))
async def delete_project_enhanced(
    project_name: str,
    request: DeleteProjectRequest,
    db: Annotated[Session, Depends(get_db)],
    account: auth_module.CurrentAccount,
):
    """
    Delete a project, keeping its files in GitHub or removing them by pull request.

    Owner-only: the project is looked up among the session caller's own (#2241).
    With ``remove_files`` the project is Deleting until its removal pull
    requests merge, unless there was nothing to remove (``project_deleted``).
    Secrets, variables and environments, when asked for, are deleted first and
    at once, and stay deleted if the deletion is called off.
    """
    github_user = account.github_user
    try:
        user, project = _get_project_and_user(project_name, github_user, db)
        _require_deletable(db, user, project, request.remove_files)
        deletion_results = {"github_resources_deleted": [], "errors": []}
        if request.delete_github_resources:
            _delete_secrets_and_variables(project, github_user, db, deletion_results,
                                          request.delete_deployment_environments)
        outcome = _delete_or_start_removal(db, project, github_user, request.remove_files, deletion_results)
        return _deletion_response(outcome, deletion_results)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        print(f"Error in enhanced project deletion: {str(e)}")
        raise HTTPException(status_code=500, detail="Error deleting project")

    finally:
        db.close()
