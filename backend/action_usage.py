"""
Action usage inventory (#2045): which actions the managed workflows run, and
at which refs.

Everything is derived on read from the YAML the workflows already store. There
is no table behind it and no GitHub API call. The parser is separate from the
endpoint so the campaign that pins or bumps an action (#2046) can reuse it.
"""

import re
from dataclasses import asdict, dataclass, replace
from typing import Annotated, Any, Optional

import yaml
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import CurrentAccount
from authorization import has_project_role, project_role
from database import get_db
from models import Account, Project, ProjectRepo, ProjectWorkflow, Repo, RepoWorkflowOverride, Workflow
from workflows import format_workflow_name

router = APIRouter()

SHA = "sha"
TAG = "tag"
BRANCH = "branch"
LOCAL = "local"
DOCKER = "docker"

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
# v4, 1.2.3, v5.0.0-rc1: what a release tag looks like. Without asking GitHub
# this is the most the name can tell, so anything else is read as a branch.
VERSION_TAG = re.compile(r"v?\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class ActionReference:
    """One ``uses:`` value. Local and docker references have no owner or repo."""

    uses: str
    kind: str
    owner: Optional[str] = None
    repo: Optional[str] = None
    path: str = ""
    ref: Optional[str] = None

    @property
    def action(self) -> Optional[str]:
        return f"{self.owner}/{self.repo}" if self.owner and self.repo else None

    @property
    def pinned(self) -> bool:
        """Whether an org policy enforcing SHA pinning would accept it. Local and docker references are not its business."""
        return self.kind in (SHA, LOCAL, DOCKER)


def _ref_kind(ref: str) -> str:
    if FULL_SHA.fullmatch(ref):
        return SHA
    return TAG if VERSION_TAG.fullmatch(ref) else BRANCH


def classify_reference(uses: str) -> Optional[ActionReference]:
    """Split a ``uses:`` value, or None when it is not one GitHub would accept.

    ``owner/repo[/path]@ref`` is an action or, with a path to a workflow file,
    a reusable workflow call. ``./path`` is local and ``docker://image`` is a
    container; both are recorded, never flagged as unpinned.
    """
    value = uses.strip()
    if value.startswith("."):
        return ActionReference(value, LOCAL)
    if value.startswith("docker://"):
        return ActionReference(value, DOCKER)
    name, separator, ref = value.rpartition("@")
    parts = name.split("/")
    if not separator or not ref or len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return ActionReference(value, _ref_kind(ref), parts[0], parts[1], "/".join(parts[2:]), ref)


def _jobs(workflow_yaml: Any) -> dict:
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return {}
    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return {}
    jobs = parsed.get("jobs") if isinstance(parsed, dict) else None
    return jobs if isinstance(jobs, dict) else {}


def _job_uses_values(job: Any) -> list:
    """A job's own ``uses`` (a reusable workflow call) and those of its steps."""
    if not isinstance(job, dict):
        return []
    steps = job.get("steps")
    values = [job.get("uses")]
    if isinstance(steps, list):
        values.extend(step.get("uses") for step in steps if isinstance(step, dict))
    return values


def parse_uses_references(workflow_yaml: Any) -> list[ActionReference]:
    """Every ``uses:`` in the workflow's jobs and steps, in file order.

    Read from the parsed YAML, so a ``uses`` in a comment or a ``run:`` block
    is not one. Malformed YAML, or a shape that is not jobs and steps, gives [].
    """
    references = []
    for job in _jobs(workflow_yaml).values():
        for value in _job_uses_values(job):
            reference = classify_reference(value) if isinstance(value, str) else None
            if reference is not None:
                references.append(reference)
    return references


def _distinct_references(references: list[ActionReference]) -> dict:
    """{(action key, ref): (a reference, the paths it is used at)}, so a workflow using
    one action five times counts once."""
    distinct: dict = {}
    for reference in references:
        key = ((reference.action or reference.kind).lower(), reference.ref or reference.uses)
        _, paths = distinct.setdefault(key, (reference, set()))
        if reference.path:
            paths.add(reference.path)
    return distinct


@dataclass
class _Usage:
    """A workflow on one ref of an action, and the repositories that run it."""

    project_id: int
    project_name: str
    workflow_id: int
    workflow_name: str
    workflow_file: str
    workflow_status: str
    reusable: bool
    project_type: str
    editable: bool
    repos: list[str]
    override: bool
    paths: list[str]


@dataclass
class _Version:
    ref: str
    kind: str
    pinned: bool
    usages: list[_Usage]


@dataclass
class _Action:
    action: str
    versions: dict[str, _Version]


def _version_response(version: _Version) -> dict:
    return {
        "ref": version.ref,
        "kind": version.kind,
        "pinned": version.pinned,
        "workflow_count": len({u.workflow_id for u in version.usages}),
        "repo_count": len({repo for u in version.usages for repo in u.repos}),
        "usages": [asdict(u) for u in version.usages],
    }


def _action_response(entry: _Action) -> dict:
    versions = sorted(
        (_version_response(v) for v in entry.versions.values()),
        key=lambda v: (v["pinned"], -v["workflow_count"], v["ref"]),
    )
    usages = [u for v in entry.versions.values() for u in v.usages]
    unpinned = [u for v in entry.versions.values() if not v.pinned for u in v.usages]
    return {
        "action": entry.action,
        "versions": versions,
        "workflow_count": len({u.workflow_id for u in usages}),
        "unpinned_workflow_count": len({u.workflow_id for u in unpinned}),
    }


class _Inventory:
    """Usages gathered per action and per ref, then shaped for the response."""

    def __init__(self) -> None:
        self.actions: dict[str, _Action] = {}
        self.other = {LOCAL: 0, DOCKER: 0}
        self.workflow_ids: set[int] = set()
        self.unpinned_workflow_ids: set[int] = set()

    def add(self, usage: _Usage, references: list[ActionReference]) -> None:
        self.workflow_ids.add(usage.workflow_id)
        for reference, paths in _distinct_references(references).values():
            if reference.action is None:
                self.other[reference.kind] += 1
                continue
            entry = self.actions.setdefault(reference.action.lower(), _Action(reference.action, {}))
            version = entry.versions.setdefault(
                reference.ref, _Version(reference.ref, reference.kind, reference.pinned, [])
            )
            version.usages.append(replace(usage, paths=sorted(paths)))
            if not reference.pinned:
                self.unpinned_workflow_ids.add(usage.workflow_id)

    def to_response(self) -> dict:
        actions = sorted(
            (_action_response(entry) for entry in self.actions.values()),
            key=lambda entry: (-entry["unpinned_workflow_count"], entry["action"].lower()),
        )
        return {
            "actions": actions,
            "other": self.other,
            "summary": {
                "action_count": len(actions),
                "workflows_read": len(self.workflow_ids),
                "unpinned_workflows": len(self.unpinned_workflow_ids),
            },
        }


def visible_projects(db: Session, account: Account) -> list[Project]:
    # Not _project_list_query: for an account with no workspace row it lists every
    # project, and project_role is the one rule for who sees what.
    return [p for p in db.query(Project).all() if project_role(db, account, p) is not None]


def repo_names_by_project(db: Session, project_ids: list[int]) -> dict[int, list[str]]:
    names: dict[int, list[str]] = {}
    rows = (
        db.query(ProjectRepo.project_id, Repo.repo_name)
        .join(Repo, Repo.repo_id == ProjectRepo.repo_id)
        .filter(ProjectRepo.project_id.in_(project_ids))
        .order_by(Repo.repo_name)
        .all()
    )
    for project_id, repo_name in rows:
        names.setdefault(project_id, []).append(repo_name)
    return names


def overrides_by_workflow(db: Session, project_ids: list[int]) -> dict[int, list]:
    """{workflow id: [(repo name, the repo's own YAML)]}."""
    overrides: dict[int, list] = {}
    rows = (
        db.query(RepoWorkflowOverride.workflow_id, Repo.repo_name, RepoWorkflowOverride.workflow_yaml)
        .join(Repo, Repo.repo_id == RepoWorkflowOverride.repo_id)
        .filter(RepoWorkflowOverride.project_id.in_(project_ids))
        .order_by(Repo.repo_name)
        .all()
    )
    for workflow_id, repo_name, workflow_yaml in rows:
        overrides.setdefault(workflow_id, []).append((repo_name, workflow_yaml))
    return overrides


def project_workflows(db: Session, project_ids: list[int]) -> list:
    """[(project id, Workflow)] for the projects' workflows, except those a campaign is removing."""
    return (
        db.query(ProjectWorkflow.project_id, Workflow)
        .join(Workflow, Workflow.workflow_id == ProjectWorkflow.workflow_id)
        .filter(ProjectWorkflow.project_id.in_(project_ids), Workflow.pending_delete.is_(False))
        .order_by(ProjectWorkflow.project_id, Workflow.workflow_name)
        .all()
    )


def _usage_row(project: Project, workflow: Workflow, repos: list[str], override: bool, editable: bool) -> _Usage:
    return _Usage(
        project_id=project.project_id,
        project_name=project.project_name,
        workflow_id=workflow.workflow_id,
        workflow_name=workflow.workflow_name,
        workflow_file=format_workflow_name(workflow.workflow_name, project.project_code, project.use_prefix),
        workflow_status=workflow.workflow_status,
        reusable=bool(workflow.reusable_workflow),
        project_type=project.project_type or "standard",
        editable=editable,
        repos=repos,
        override=override,
        paths=[],
    )


@router.get("/api/action-usage", responses={401: {"description": "Authentication required"}})
def get_action_usage(
    db: Annotated[Session, Depends(get_db)],
    account: CurrentAccount,
):
    """The actions the caller's visible workflows use, grouped by action and then by ref.

    Each ref lists the workflows on it and the repositories they reach: the
    project's repositories, except those with their own copy of the workflow
    (a per-repository override), which are counted for what that copy uses.
    ``pinned`` is false for a tag or a branch, the refs an org policy
    enforcing SHA pinning rejects; the tag/branch split is read from the name.
    Workflows a campaign is removing are left out. ``summary`` counts the
    workflows read, and ``other`` the local and docker references, which are
    never flagged.
    """
    projects = {p.project_id: p for p in visible_projects(db, account)}
    inventory = _Inventory()
    if not projects:
        return inventory.to_response()
    project_ids = list(projects)
    repos = repo_names_by_project(db, project_ids)
    overrides = overrides_by_workflow(db, project_ids)
    editable = {pid: has_project_role(db, account, p, "project_editor") for pid, p in projects.items()}
    rows = project_workflows(db, project_ids)
    for project_id, workflow in rows:
        project = projects[project_id]
        own = overrides.get(workflow.workflow_id, [])
        overridden = {name for name, _ in own}
        base_repos = [name for name in repos.get(project_id, []) if name not in overridden]
        if base_repos or not own:
            inventory.add(
                _usage_row(project, workflow, base_repos, False, editable[project_id]), parse_uses_references(workflow.workflow_yaml)
            )
        for repo_name, override_yaml in own:
            inventory.add(
                _usage_row(project, workflow, [repo_name], True, editable[project_id]), parse_uses_references(override_yaml)
            )
    return inventory.to_response()
