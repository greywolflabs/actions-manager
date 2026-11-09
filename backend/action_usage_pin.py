"""
Update the actions the managed workflows use (#2046), from the Action Usage
inventory: one workflow at a time, or every workflow that uses an action.

Updating means writing the new version into the workflow's ``uses:`` and saving
it in the project through the editor's own path, ``create_or_update_workflow``
(status ``committed_locally``), exactly as Commit Locally does. It never opens a
pull request: delivering the change is the project's Create Pull Requests, when
the user chooses.

A workflow with an open pull request (``under_review``) is not changed. A
repository with its own copy of a workflow (an override) keeps it: the update
changes the project's copy, which an override replaces for that repository.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

import workflows as wf
from action_pinning import changed_lines, latest_release, resolve_ref_to_sha, rewrite_uses
from action_usage import FULL_SHA, overrides_by_workflow, project_workflows, repo_names_by_project, visible_projects
from auth import CurrentAccount, user_tokens
from authorization import has_project_role, project_role
from database import get_db
from file_kinds import refuse_unless_accepted
from models import Project, ProjectWorkflow, Workflow
from projects import OWNER_REPO_NAME

router = APIRouter()

UNDER_REVIEW = "under_review"
BLOCKED = "Under review: it has an open pull request. Close or merge that first."

_PIN_ERRORS = wf._responses(400, 401, 403, 404, 409, 429, 500, 502)


class _PinFields(BaseModel):
    action: str = Field(max_length=200)
    sha: str
    comment: Optional[str] = Field(default=None, max_length=100)

    @field_validator("action")
    @classmethod
    def _owner_repo(cls, value: str) -> str:
        if not OWNER_REPO_NAME.fullmatch(value):
            raise ValueError("action must use owner/repo format")
        return value

    @field_validator("sha")
    @classmethod
    def _full_sha(cls, value: str) -> str:
        if not FULL_SHA.fullmatch(value):
            raise ValueError("sha must be a full 40-character commit SHA")
        return value.lower()

    @field_validator("comment")
    @classmethod
    def _one_line(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and re.search(r"[\r\n]", value):
            raise ValueError("comment must be one line")
        return value or None


class ApplyPinRequest(_PinFields):
    project_id: int = Field(gt=0)


class UpdateWorkflowRequest(_PinFields):
    workflow_id: int = Field(gt=0)
    # False returns the updated YAML and saves nothing, for the editor to load as an unsaved edit.
    commit: bool


@dataclass
class _Pin:
    action: str
    sha: str
    comment: Optional[str]


@dataclass
class _WorkflowChange:
    workflow_id: int
    workflow_name: str
    workflow_file: str
    reusable: bool
    lines: list[dict]
    # Why it is not changed, when it is not: it has an open pull request.
    blocked: Optional[str] = None


@dataclass
class _ProjectChange:
    project_id: int
    project_name: str
    project_type: str
    editable: bool
    # Repositories with their own copy of a changed workflow, which keep it.
    skipped_repos: list[str]
    workflows: list[_WorkflowChange] = field(default_factory=list)


@dataclass
class _ProjectPlan:
    """What pinning does in one project: the response, and the rows and text to save."""

    change: _ProjectChange
    edits: list[tuple[Workflow, str]] = field(default_factory=list)


def _token(account) -> str:
    if account.github_user not in user_tokens:
        raise HTTPException(status_code=401, detail=wf.NOT_AUTHENTICATED_DETAIL)
    return user_tokens[account.github_user]


def _plan_project(project: Project, workflows: list[Workflow], repos: list[str], overrides: dict,
                  pin: _Pin, editable: bool) -> Optional[_ProjectPlan]:
    """The update in one project, or None when no workflow of it uses *action* at another ref."""
    changed = []
    for workflow in workflows:
        new_yaml, count = rewrite_uses(workflow.workflow_yaml, pin.action, pin.sha, pin.comment)
        if count:
            changed.append((workflow, new_yaml))
    if not changed:
        return None
    overridden = {name for workflow, _ in changed for name, _ in overrides.get(workflow.workflow_id, [])}
    change = _ProjectChange(
        project_id=project.project_id,
        project_name=project.project_name,
        project_type=project.project_type or "standard",
        editable=editable,
        skipped_repos=[name for name in repos if name in overridden],
        workflows=[
            _WorkflowChange(
                workflow_id=workflow.workflow_id,
                workflow_name=workflow.workflow_name,
                workflow_file=wf.format_workflow_name(workflow.workflow_name, project.project_code, project.use_prefix),
                reusable=bool(workflow.reusable_workflow),
                lines=changed_lines(workflow.workflow_yaml, new_yaml),
                blocked=BLOCKED if workflow.workflow_status == UNDER_REVIEW else None,
            )
            for workflow, new_yaml in changed
        ],
    )
    return _ProjectPlan(change, [(w, y) for w, y in changed if w.workflow_status != UNDER_REVIEW])


def _plans(db: Session, account, projects: dict[int, Project], pin: _Pin) -> list[_ProjectPlan]:
    if not projects:
        return []
    project_ids = list(projects)
    repos = repo_names_by_project(db, project_ids)
    overrides = overrides_by_workflow(db, project_ids)
    by_project: dict[int, list[Workflow]] = {}
    for project_id, workflow in project_workflows(db, project_ids):
        by_project.setdefault(project_id, []).append(workflow)
    plans = (
        _plan_project(
            project, by_project.get(project_id, []), repos.get(project_id, []), overrides, pin,
            has_project_role(db, account, project, "project_editor"),
        )
        for project_id, project in projects.items()
    )
    return [plan for plan in plans if plan is not None]


def _version(action: str, ref: str, sha: str) -> dict:
    """A version of *action*: the ref asked for or found, the commit it is on, and the comment written beside it."""
    return {"action": action, "ref": ref, "sha": sha, "comment": _default_comment(ref)}


def _default_comment(ref: str) -> Optional[str]:
    """The version a pin is written beside: the tag or branch asked for, none for a bare SHA."""
    return None if FULL_SHA.fullmatch(ref) else ref


@router.get("/api/action-usage/preview", responses=_PIN_ERRORS)
def preview_pin(
    action: Annotated[str, Query(pattern=f"^{OWNER_REPO_NAME.pattern}$", max_length=200)],
    db: Annotated[Session, Depends(get_db)],
    account: CurrentAccount,
    ref: Annotated[Optional[str], Query(min_length=1, max_length=200)] = None,
):
    """What pinning *action* to *ref* would change in every project the caller can see.

    Without *ref* it is the action's latest release (``source`` says which was
    used), so the page can open already showing the change worth making. A read,
    so a viewer can preview (the write-protection middleware refuses a read-only
    member's POST); only apply needs an editor. The tag or branch is resolved
    to its commit SHA once, with the caller's own GitHub token. Only the
    changed lines are returned. Projects the caller cannot edit are listed with
    ``editable`` false, so the page can say so.
    """
    token = _token(account)
    source = "requested" if ref else "latest release"
    if ref is None:
        ref, sha = latest_release(action, token, db, account.github_user)
    else:
        sha = resolve_ref_to_sha(action, ref, token, db, account.github_user)
    version = _version(action, ref, sha)
    plans = _plans(db, account, {p.project_id: p for p in visible_projects(db, account)}, _Pin(action, sha, version["comment"]))
    return {**version, "source": source, "projects": [asdict(plan.change) for plan in plans]}


def _mark_project_draft(db: Session, project: Project, github_user: str) -> None:
    """What Commit Locally does to the project once workflows are saved: it has changes to deliver."""
    project.last_modified_by = github_user or None
    db.commit()
    if project.pr_state in ("new", "synced"):
        wf._update_project_pr_state(db, project.project_id, "draft")
        db.refresh(project)


def _editable_project(db: Session, account, project_id: int) -> Project:
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if project is None or project_role(db, account, project) is None:
        raise HTTPException(status_code=404, detail=wf.PROJECT_ERROR)
    wf._require_project_editor(db, account.github_user, project)
    refuse_unless_accepted(project, "workflows")
    return project


@router.get("/api/action-usage/latest", responses=_PIN_ERRORS)
def get_latest_version(
    action: Annotated[str, Query(pattern=f"^{OWNER_REPO_NAME.pattern}$", max_length=200)],
    db: Annotated[Session, Depends(get_db)],
    account: CurrentAccount,
):
    """The version an update goes to by default: *action*'s latest release, and the commit it is on.

    Two GitHub calls with the caller's own token (the release, then its tag). The page asks once per
    action, when its workflows are listed, so each can say what it would be updated to.
    """
    ref, sha = latest_release(action, _token(account), db, account.github_user)
    return _version(action, ref, sha)


@router.post("/api/action-usage/update-workflow", responses=_PIN_ERRORS)
def update_workflow(
    payload: UpdateWorkflowRequest,
    db: Annotated[Session, Depends(get_db)],
    account: CurrentAccount,
):
    """Update one workflow's use of *action* to *sha*.

    With ``commit`` it is saved in the project as Commit Locally saves it, and the project is left with
    changes to deliver. Without it nothing is saved: the updated YAML comes back for the editor to load
    as an unsaved edit. The change is worked out from the stored workflow, never taken from the client.
    A workflow with an open pull request is not committed (409).
    """
    row = db.query(Workflow, Project).join(
        ProjectWorkflow, ProjectWorkflow.workflow_id == Workflow.workflow_id
    ).join(Project, Project.project_id == ProjectWorkflow.project_id).filter(
        Workflow.workflow_id == payload.workflow_id, Workflow.pending_delete.is_(False),
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    workflow, project = row
    _editable_project(db, account, project.project_id)
    new_yaml, count = rewrite_uses(workflow.workflow_yaml, payload.action, payload.sha, payload.comment)
    if count == 0:
        raise HTTPException(status_code=400, detail=f"This workflow does not use {payload.action} at another version.")
    result = {
        "workflow_id": workflow.workflow_id,
        "workflow_name": workflow.workflow_name,
        "workflow_file": wf.format_workflow_name(workflow.workflow_name, project.project_code, project.use_prefix),
        "project_id": project.project_id,
        "project_name": project.project_name,
        "reusable": bool(workflow.reusable_workflow),
        "changed": count,
    }
    if not payload.commit:
        return {**result, "content": new_yaml}
    if workflow.workflow_status == UNDER_REVIEW:
        raise HTTPException(status_code=409, detail=BLOCKED)
    saved = wf.create_or_update_workflow(
        db, wf.WorkflowSchema(name=workflow.workflow_name, content=new_yaml), project.project_id,
        is_reusable=bool(workflow.reusable_workflow), last_modified_by=account.github_user,
    )
    _mark_project_draft(db, project, account.github_user)
    return {**result, "pr_state": project.pr_state, "workflow_status": saved.workflow_status}


@router.post("/api/action-usage/apply", responses=_PIN_ERRORS)
def apply_pin(
    payload: ApplyPinRequest,
    db: Annotated[Session, Depends(get_db)],
    account: CurrentAccount,
):
    """Update *action* to *sha* in every workflow of one project that uses it, committing each locally.

    No pull request is opened. A workflow with an open pull request is left as it is. The change is
    worked out again here from the stored workflows, never taken from the client.
    """
    project = _editable_project(db, account, payload.project_id)
    plans = _plans(db, account, {project.project_id: project}, _Pin(payload.action, payload.sha, payload.comment))
    if not plans:
        raise HTTPException(status_code=400, detail="No workflow in this project uses that action at another version.")
    plan = plans[0]
    if not plan.edits:
        raise HTTPException(status_code=409, detail=BLOCKED)
    for workflow, new_yaml in plan.edits:
        wf.create_or_update_workflow(
            db, wf.WorkflowSchema(name=workflow.workflow_name, content=new_yaml), project.project_id,
            is_reusable=bool(workflow.reusable_workflow), last_modified_by=account.github_user,
        )
    _mark_project_draft(db, project, account.github_user)
    return {
        "updated": len(plan.edits),
        "workflows": [workflow.workflow_name for workflow, _ in plan.edits],
        "pr_state": project.pr_state,
    }
