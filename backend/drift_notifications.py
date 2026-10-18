"""
Drift notification event emission (issue #1793, part of #1789).

A drift check's outcome for each (file, repo, branch) is stored on the
delivery record's row (``deliveries.record_check``), which says whether the
previous check found drift there; a transition writes a deduplicated
notification_event: drift.detected or drift.resolved. A check that could not
complete writes drift.check_failed instead, and nothing per file.

Workflows, CODEOWNERS and custom files go through the same recorder and the
same clears.
"""

import hashlib
from datetime import datetime, timezone
from typing import Iterable, List, NamedTuple, Optional

from sqlalchemy.orm import Session

import deliveries
from models import CustomFile, Project, Repo, RepoWorkflowOverride, Workflow
from notification_dispatch import emit_notification_event

# Stands in for a workflow id in a CODEOWNERS event's dedup key.
CODEOWNERS_SUBJECT = "codeowners"
# Prefixes a custom file's id in its events' dedup keys, so it never collides with a workflow's.
CUSTOM_FILE_SUBJECT = "custom_file"
CODEOWNERS_NAME = "CODEOWNERS"


class DriftObservation(NamedTuple):
    """What a drift check found for one file on one (repo, branch).

    ``detail`` is the check's WorkflowDriftDetail, CodeownersDriftDetail or
    CustomFileDriftDetail: its repo, repo_id and branch say where, the rest
    what it found. ``etag`` is the validator of the read, for a file read on
    its own (CODEOWNERS, a custom file).
    """
    kind: str
    file_id: Optional[int]
    name: str
    detail: object
    etag: object = deliveries.UNSET


def _content_hash(detail) -> str:
    raw = f"{detail.has_drift}:{detail.github_sha or ''}:{detail.actionsmanager_sha or ''}:{detail.message}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _subject(kind: str, file_id):
    """The file in a dedup key: a workflow's id, ``codeowners``, or ``custom_file:<id>``."""
    if kind == deliveries.CODEOWNERS:
        return CODEOWNERS_SUBJECT
    return f"{CUSTOM_FILE_SUBJECT}:{file_id}" if kind == deliveries.CUSTOM_FILE else file_id


def _file_name(db: Session, kind: str, file_id) -> str:
    """The name a drift check gives the file (``DriftObservation.name``): a custom file's is its path."""
    if kind == deliveries.CODEOWNERS:
        return CODEOWNERS_NAME
    if kind == deliveries.CUSTOM_FILE:
        path = db.query(CustomFile.file_path).filter(CustomFile.id == file_id).scalar()
        return path or str(file_id)
    workflow = db.query(Workflow).filter(Workflow.workflow_id == file_id).first()
    return workflow.workflow_name if workflow else str(file_id)


def _dedup_key(event: str, project: Project, row, subject) -> str:
    # The branch is part of every key: drift appearing on a second branch is
    # its own event, not a duplicate of the first branch's.
    return (f"{event}:{project.project_id}:{row.repo_id}:{subject}:"
            f"{row.branch or ''}:{row.content_hash or ''}:{row.drift_cycle_count}")


def _emit_resolved(db: Session, project: Project, row, repo_name: str, subject, name: str) -> None:
    key = _dedup_key("drift.resolved", project, row, subject)
    emit_notification_event(db, project.project_id, "drift.resolved", key, {
        "project_name": project.project_name,
        "repo": repo_name,
        "branch": row.branch or "",
        "workflow_name": name,
    })


def _record(db: Session, project: Project, observation: DriftObservation, now: datetime) -> None:
    """Store one observation and notify its transition, if it is one."""
    detail = observation.detail
    row, was_drifted = deliveries.record_check(
        db, observation.kind, observation.file_id, detail.repo_id, detail.branch or "",
        has_drift=detail.has_drift, deleted=bool(detail.deleted_in_github), github_sha=detail.github_sha,
        content_hash=_content_hash(detail), now=now, etag=observation.etag,
    )
    if row is None or detail.has_drift == was_drifted:
        return
    subject = _subject(observation.kind, observation.file_id)
    if not detail.has_drift:
        _emit_resolved(db, project, row, detail.repo, subject, observation.name)
        return
    key = _dedup_key("drift.detected", project, row, subject)
    emit_notification_event(db, project.project_id, "drift.detected", key, {
        "project_name": project.project_name,
        "repo": detail.repo,
        "branch": row.branch or "",
        "workflow_name": observation.name,
        "message": detail.message,
    })


def _recordable(detail) -> bool:
    # A check that didn't complete says nothing about drift. Persisting it
    # would overwrite a real "drifted" with "clean" and emit drift.resolved for
    # a file that is still drifted — the previous behaviour whenever GitHub
    # rate-limited or the token expired. Without a repo there is no stable
    # identity to persist under.
    return detail.repo_id is not None and not getattr(detail, "check_failed", False)


def record_drift_transitions(db: Session, project: Project, observations: Iterable[DriftObservation]) -> None:
    """Store what a check found for each file on each (repo, branch), of every
    kind alike, notifying each one that changed whether it drifted."""
    now = datetime.now(timezone.utc)
    for observation in observations:
        if _recordable(observation.detail):
            _record(db, project, observation, now)
    db.commit()


def recompute_project_drift_summary(db: Session, project: Project) -> None:
    """Refresh the cached projects.drift_* columns from the stored per-file drift.

    The project list renders its drift badge from these columns while the
    project page renders from the delivery record's drift, so the two must not
    diverge.

    ponytail: writes the columns directly rather than reusing workflows.py's
    _cache_project_drift_summary — workflows.py already imports this module, so
    calling back into it would be a circular import.
    """
    drift_count = deliveries.drift_count(db, project.project_id)
    project.drift_status = "drifted" if drift_count else "clean"
    project.drift_count = drift_count
    project.last_drift_check_at = datetime.now(timezone.utc)
    project.drift_error_summary = None
    # This path only runs from a real answer ("drifted" or "clean"), never
    # check_failed, so it must reset the sweep's backoff streak the same way
    # workflows.py's _cache_project_drift_summary does.
    project.drift_check_failure_count = 0
    db.commit()


def _notify_resolved(db: Session, project: Project, kind: str, file_id, rows: List) -> None:
    """drift.resolved for each row a clear just resolved.

    Same dedup shape as a check's, so a resolution recorded here can't
    double-notify with one recorded by a later live check.
    """
    if not rows:
        return
    name = _file_name(db, kind, file_id)
    repo_names = dict(db.query(Repo.repo_id, Repo.repo_name).filter(Repo.repo_id.in_({row.repo_id for row in rows})))
    for row in rows:
        _emit_resolved(db, project, row, repo_names.get(row.repo_id, ""), _subject(kind, file_id), name)


def clear_drift(db: Session, project: Project, kind: str, file_id,
                repo_name: Optional[str] = None, branch: Optional[str] = None) -> int:
    """Mark a file's stored drift resolved, optionally scoped to one repo/branch.

    For actions that resolve drift as a side effect: adopting GitHub's
    version, restoring the managed version, merging a fix PR, syncing. Each
    of them has already recorded the delivery it made. Without this the caches
    keep claiming drift until the next live check, so the banner shows drift
    that is already fixed.

    Clears from known intent rather than re-checking GitHub, so no extra API
    calls. Returns the number of rows cleared.

    ``branch`` matters as much as ``repo_name``: a resolution writes to one
    branch, so clearing every branch's row would report the file clean while
    the branches nobody touched are still drifted.
    """
    repo_id = None
    if repo_name:
        repo = db.query(Repo).filter(Repo.repo_name == repo_name.strip()).first()
        if repo is None:
            # Unknown repo name can't match any row; clearing everything for the
            # file instead would wrongly resolve drift in its other repos.
            recompute_project_drift_summary(db, project)
            return 0
        repo_id = repo.repo_id
    rows = deliveries.resolve_drift(db, kind, file_id, repo_id, branch)
    _notify_resolved(db, project, kind, file_id, rows)
    # Always: a caller may have deleted rows (cascade) rather than flipping
    # them, which changes the project's count too.
    recompute_project_drift_summary(db, project)
    return len(rows)


def _overridden_repo_ids(db: Session, project: Project, kind: str, file_id) -> set:
    if kind != deliveries.WORKFLOW:
        return set()
    return {
        repo_id for (repo_id,) in db.query(RepoWorkflowOverride.repo_id).filter(
            RepoWorkflowOverride.project_id == project.project_id,
            RepoWorkflowOverride.workflow_id == file_id,
        )
    }


def resolve_drift_for_local_edit(db: Session, project: Project, kind: str, file_id, content: str) -> int:
    """Resolve the drift a local edit adopts: where GitHub's copy is exactly the edited content.

    The last check saw that blob there, so ActionsManager now holds what
    GitHub does and the blob counts as delivered. Any other edit leaves drift
    reported: GitHub's copy is still not what was delivered, and a local edit
    is shown by the file's status, never by drift. Repositories with a
    per-repo override are skipped: drift there is judged against the override,
    which the edit does not change. Returns the number of rows resolved.
    """
    # A workflow save strips its content, while GitHub's editor ends a file with
    # a newline: adopting GitHub's copy saves it without that newline, which the
    # check would not count as a difference either.
    content = content or ""
    blobs = {deliveries.blob_sha(content), deliveries.blob_sha(content + "\n")}
    skipped = _overridden_repo_ids(db, project, kind, file_id)
    resolved = []
    for row in deliveries.drifted_rows(db, project.project_id, kind, file_id):
        if row.github_sha in blobs and row.repo_id not in skipped:
            deliveries.record_matched(db, kind, file_id, row.repo_id, row.branch, row.github_sha)
            resolved += deliveries.resolve_drift(db, kind, file_id, row.repo_id, row.branch)
    if not resolved:
        return 0
    _notify_resolved(db, project, kind, file_id, resolved)
    recompute_project_drift_summary(db, project)
    return len(resolved)


def drop_drift(db: Session, project: Project, kind: str, file_id) -> None:
    """Forget stored drift for a file removed from a project, or on its way out.

    Deleting the file's row cascades its record away; this also covers a
    removal that keeps the row, so its drift stops counting towards the
    project. What was delivered stays on the record.
    """
    deliveries.reset_drift(db, kind, file_id, project.project_id)
    db.commit()
    recompute_project_drift_summary(db, project)


def drop_workflow_drift(db: Session, project: Project, workflow_id: int) -> None:
    drop_drift(db, project, deliveries.WORKFLOW, workflow_id)


def record_drift_check_failed(db: Session, project: Project, error: str) -> None:
    """Emit a project-scoped drift.check_failed event, deduplicated on the error content
    so repeated identical failures don't spam but a new failure reason still notifies."""
    content_hash = hashlib.sha256((error or "").encode("utf-8")).hexdigest()[:16]
    dedup_key = f"drift.check_failed:{project.project_id}:{content_hash}"
    emit_notification_event(db, project.project_id, "drift.check_failed", dedup_key, {
        "project_name": project.project_name,
        "error": error,
    })
    db.commit()
