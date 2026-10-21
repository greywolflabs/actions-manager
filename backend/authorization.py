"""
Authorization module for ActionsManager

Provides:
- get_current_user: FastAPI dependency to resolve the authenticated user from the request
- require_role: Factory that returns a dependency enforcing minimum workspace role
- ROLE_HIERARCHY: Ordered role levels for permission comparison

Roles (highest → lowest):
  admin  →  member  →  read_only
"""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db as _get_db, serialize_writers
from models import Account, Project, WorkspaceMember, ProjectMembership, SEED_ACCOUNT_GITHUB_USER

# Ordered from lowest to highest privilege
ROLE_HIERARCHY = ["read_only", "member", "admin"]
VALID_ROLES = set(ROLE_HIERARCHY)

# Valid project-level roles
PROJECT_ROLES = {"project_editor", "project_viewer"}


# Two callers decide something important from this one answer: who becomes
# workspace admin on first login, and whether an unauthenticated first-boot
# restore is still allowed. They must never drift apart — the restore endpoint
# is safe to leave open precisely *because* its window is the same window in
# which the next person to sign in would be made admin anyway.
FIRST_MEMBER_LOCK_ID = 1465


def lock_first_member_decision(db: Session) -> None:
    """Serialize the count-then-write, so two concurrent callers can't both
    observe an empty workspace and both act on it."""
    serialize_writers(db, FIRST_MEMBER_LOCK_ID)


def workspace_is_uninitialized(db: Session) -> bool:
    """True when nobody has ever signed in.

    The seed account is created by a migration before first boot, so counting it
    would make an untouched installation look occupied — which is exactly the
    bug migrate_revoke_seed_workspace_membership.py exists to repair.
    """
    member_count = (
        db.query(WorkspaceMember)
        .join(Account, Account.user_id == WorkspaceMember.user_id)
        .filter(Account.github_user != SEED_ACCOUNT_GITHUB_USER)
        .count()
    )
    return member_count == 0



def get_current_user(
    request: Request,
    db: Session = Depends(_get_db),
) -> Account:
    """
    Resolve the currently authenticated user from a server-issued session token.

    X-GitHub-User is never trusted for identity; it is accepted only as an
    optional consistency check by the auth session resolver.

    Returns the Account row or raises 401.
    """
    from auth import resolve_authenticated_user, set_request_user

    user = resolve_authenticated_user(request, db)
    set_request_user(user.github_user)
    return user


def get_current_member(
    current_user: Account = Depends(get_current_user),
    db: Session = Depends(_get_db),
) -> WorkspaceMember:
    """
    Resolve the workspace membership for the current user.
    Returns WorkspaceMember or raises 403 if no membership exists.
    """
    member = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.user_id == current_user.user_id)
        .first()
    )
    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a workspace member",
        )
    if member.workspace_role == "co_admin":
        # Transitional safety for pre-migration data: normalize deprecated role in-place.
        member.workspace_role = "admin"
        db.commit()
        db.refresh(member)
    return member


def _role_level(role: str) -> int:
    """Return the numeric privilege level for a role."""
    try:
        return ROLE_HIERARCHY.index(role)
    except ValueError:
        return -1


def require_role(minimum_role: str):
    """
    Factory: returns a FastAPI dependency that ensures the caller has
    *at least* the given workspace role.

    Usage in an endpoint::

        @router.post("/api/projects/")
        def create_project(
            ...,
            _auth: WorkspaceMember = Depends(require_role("admin")),
        ):
            ...
    """
    if minimum_role not in VALID_ROLES:
        raise ValueError(
            f"Invalid minimum_role '{minimum_role}'. Must be one of: {', '.join(ROLE_HIERARCHY)}"
        )
    min_level = _role_level(minimum_role)

    def _check(member: WorkspaceMember = Depends(get_current_member)) -> WorkspaceMember:
        if _role_level(member.workspace_role) < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {minimum_role}",
            )
        return member

    return _check


def is_project_admin(member: WorkspaceMember) -> bool:
    """Return True if the workspace role grants automatic project_admin access
    across all projects, without requiring explicit ProjectMembership records.

    Only admin roles receive implicit full project access.
    Members must have explicit ProjectMembership records (project_editor or
    project_viewer) to access individual projects.
    """
    # Transitional runtime safety: treat any remaining deprecated co_admin rows as admin.
    # Once all environments have run migrate_co_admin_to_admin.py, this fallback can be removed.
    return member.workspace_role in ("admin", "co_admin")


def get_project_membership(
    db: Session, user_id: int, project_id: int
) -> Optional[ProjectMembership]:
    """Look up the project membership for a user, or return None."""
    return (
        db.query(ProjectMembership)
        .filter(
            ProjectMembership.user_id == user_id,
            ProjectMembership.project_id == project_id,
        )
        .first()
    )


def check_project_access(
    db: Session, member: WorkspaceMember, project_id: int
) -> Optional[str]:
    """
    Check whether a workspace member can access a project.

    Returns the effective project role string:
      - "project_admin"   for admin workspace members (full access)
      - "project_editor"  for explicit project_editor membership
      - "project_viewer"  for explicit project_viewer membership
      - None              if the user has no access

    Does NOT raise — callers decide how to handle None.
    """
    if is_project_admin(member):
        return "project_admin"

    pm = get_project_membership(db, member.user_id, project_id)
    if pm:
        return pm.project_role

    # A full workspace member belongs to the workspace, and this application is
    # single-workspace, so every project in it is theirs to see. A
    # ProjectMembership row is what raises them to project_editor on one of
    # them, not what makes the project visible. read_only members stay on
    # explicit grants: for them this table is the only way in.
    if member.workspace_role == "member":
        return "project_viewer"
    return None


# ---------------------------------------------------------------------------
# Project access: the one place it is decided. Every lookup and gate delegates
# here; a second copy of these rules is how the copies came to disagree.
# ---------------------------------------------------------------------------

PROJECT_ROLE_LEVEL = {"project_viewer": 0, "project_editor": 1, "project_admin": 2}

# Project.pr_state while the pull requests removing its files from GitHub are
# open. The project goes when they have all merged; closing one calls it off.
PROJECT_DELETING = "deleting"


def project_role(db: Session, account: Optional[Account], project: Optional[Project], *,
                 settling: bool = False) -> Optional[str]:
    """The caller's role on *project*, or None when they may not see it.

    Workspace admins hold project_admin. The owner edits their own project with
    no membership row. Otherwise the member rule in check_project_access
    applies. A read_only member never edits, owner or not: they see their own
    project and the ones granted to them, and WriteProtectionMiddleware refuses
    their writes before any route as well.

    A project being deleted is read-only to everyone, admins included: its
    removal pull requests are the last change it makes. *settling* is for
    merging or closing one of them, which finishes the deletion or calls it
    off, so it takes the role the caller would otherwise hold.
    """
    role = _role_ignoring_deletion(db, account, project)
    if role is not None and not settling and project.pr_state == PROJECT_DELETING:
        return "project_viewer"
    return role


def _role_ignoring_deletion(db: Session, account: Optional[Account], project: Optional[Project]) -> Optional[str]:
    if account is None or project is None:
        return None
    member = db.query(WorkspaceMember).filter(WorkspaceMember.user_id == account.user_id).first()
    if member is not None and is_project_admin(member):
        return "project_admin"
    read_only = member is not None and member.workspace_role == "read_only"
    if project.user_id == account.user_id:
        return "project_viewer" if read_only else "project_editor"
    role = check_project_access(db, member, project.project_id) if member is not None else None
    return "project_viewer" if role and read_only else role


def has_project_role(db: Session, account: Optional[Account], project: Optional[Project], minimum: str, *,
                     settling: bool = False) -> bool:
    role = project_role(db, account, project, settling=settling)
    return role is not None and PROJECT_ROLE_LEVEL[role] >= PROJECT_ROLE_LEVEL[minimum]


def ambiguous_project_name_detail(project_name: str) -> str:
    return (
        f"More than one project is named '{project_name.strip()}'. "
        "Open it from the projects list, or rename one so they can be told apart."
    )


def _single(projects: List[Project], project_name: str) -> Optional[Project]:
    if len(projects) > 1:
        raise HTTPException(status_code=409, detail=ambiguous_project_name_detail(project_name))
    return projects[0] if projects else None


def find_project_by_name(
    db: Session,
    account: Optional[Account],
    project_name: str,
    *,
    owner_name: Optional[str] = None,
    hint: Optional[int] = None,
) -> Optional[Project]:
    """The project *project_name* means for *account*, among those it may see.

    Names are unique per owner only (#2240), so a name can match several
    projects. In order: the page's own project (*hint*, from X-Project-Id), the
    named owner's, the caller's own, one the caller holds a grant on, then the
    only one left. Several left is a 409 rather than a guess.
    """
    name = (project_name or "").strip()
    if account is None or not name:
        return None
    # The exact match covers names SQLite's lower() leaves alone (non-ASCII).
    candidates = (
        db.query(Project)
        .filter(or_(Project.project_name == name, func.lower(Project.project_name) == name.lower()))
        .order_by(Project.project_id)
        .all()
    )
    visible = [p for p in candidates if project_role(db, account, p) is not None]
    if not visible:
        return None

    hinted = [p for p in visible if hint and p.project_id == hint]
    if hinted:
        return hinted[0]
    if owner_name:
        owner = db.query(Account).filter(Account.github_user == owner_name.strip()).first()
        owned = [p for p in visible if owner is not None and p.user_id == owner.user_id]
        if owned:
            return owned[0]
    own = [p for p in visible if p.user_id == account.user_id]
    if own:
        return own[0]
    granted_ids = {
        pm.project_id
        for pm in db.query(ProjectMembership).filter(
            ProjectMembership.user_id == account.user_id,
            ProjectMembership.project_id.in_([p.project_id for p in visible]),
        )
    }
    granted = [p for p in visible if p.project_id in granted_ids]
    if granted:
        return _single(granted, project_name)
    return _single(visible, project_name)
