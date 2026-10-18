"""
What ActionsManager delivered to each (file, repo, branch), and what an open
pull request carries there.

The only module that reads or writes ``file_deliveries``. Each of these used to
stand in for that record, and each was wrong somewhere: the per-file hash (the
blob of whatever branch was last written, a PR branch included), the file's
status (one value for every repository), the drift state's
``confirmed_present_at`` (only what a drift check happened to see), and the name
lists on ``project_pull_requests`` (per repository, never per branch, so an open
PR to one branch hid drift on the others).

A file is one of three kinds, named by the constants below, and ``file_id`` is
that file's own row id. A row with nothing delivered and nothing pending is
kept: it says the record has tracked this file there, which is how
``has_any_record`` tells a file the record knows was never delivered from one
delivered before the record existed. Writers flush; the caller commits.

Each row also holds what the last drift check saw there (``record_check``):
drift is read against this record, so the two live together.
"""

import hashlib
from datetime import datetime, timezone
from typing import Iterable, NamedTuple, Optional

from sqlalchemy import false
from sqlalchemy.orm import Session

from models import Codeowners, CustomFile, FileDelivery, ProjectPullRequest, ProjectWorkflow, Repo

WORKFLOW = "workflow"
CUSTOM_FILE = "custom_file"
CODEOWNERS = "codeowners"

_FILE_COLUMN = {
    WORKFLOW: "workflow_id",
    CUSTOM_FILE: "custom_file_id",
    CODEOWNERS: "codeowners_id",
}

# A record_check argument left out, as opposed to one passed as None.
UNSET = object()

# Whether the PR a row's pending change rides is still open. A pending row
# never outlives its PR being open, whoever reads it.
_PR_OPEN = ProjectPullRequest.pr_state == "open"


class Target(NamedTuple):
    """One row of the record, and whether an open PR carries a change there."""
    row: FileDelivery
    pending: bool


class Delivered(NamedTuple):
    """Where the record holds a file on one (repo, branch): its path there, and the blob it holds."""
    repo_name: str
    branch: str
    path: str
    sha: Optional[str]


# (owning project, file id) for each kind. A workflow's owner is the project
# whose ProjectWorkflow holds it, which for a linked reusable workflow is the
# Reusable Workflow Project, never the caller that delivered it.
_OWNER = {
    WORKFLOW: (ProjectWorkflow.project_id, ProjectWorkflow.workflow_id),
    CUSTOM_FILE: (CustomFile.project_id, CustomFile.id),
    CODEOWNERS: (Codeowners.project_id, Codeowners.id),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _repo_id(db: Session, repo_name: str, create: bool = False):
    name = (repo_name or "").strip()
    repo = db.query(Repo).filter(Repo.repo_name == name).first()
    if repo is None and create and name:
        # The {user}/am-reuseable-workflow fallback may have no row yet. Never a
        # ProjectRepo: recording a delivery does not add a repository to a project.
        repo = Repo(repo_name=name)
        db.add(repo)
        db.flush()
    return repo.repo_id if repo else None


def _rows(db: Session, kind: str, file_id):
    query = db.query(FileDelivery)
    if file_id is None:
        # Compared as IS NULL, a missing id would match every row of the other kinds.
        return query.filter(false())
    return query.filter(getattr(FileDelivery, _FILE_COLUMN[kind]) == file_id)


def _row(db: Session, kind: str, file_id, repo_id, branch: str, create: bool = True):
    """The row for one (file, repo, branch), created if *create* and the file has an owner."""
    branch = branch or ""
    row = _rows(db, kind, file_id).filter(
        FileDelivery.repo_id == repo_id, FileDelivery.branch == branch,
    ).first()
    if row is not None or not create or file_id is None or repo_id is None:
        return row
    project_column, id_column = _OWNER[kind]
    project_id = db.query(project_column).filter(id_column == file_id).scalar()
    if project_id is None:
        return None
    row = FileDelivery(project_id=project_id, repo_id=repo_id, branch=branch,
                       **{_FILE_COLUMN[kind]: file_id})
    db.add(row)
    db.flush()
    return row


def _clear_pending(row: FileDelivery) -> None:
    row.pending_sha = row.pending_path = row.pending_pr_id = None
    row.pending_delete = False


def _file_of(row: FileDelivery) -> tuple:
    """(kind, file id) of *row*; ck_file_delivery_one_file says exactly one is set."""
    return next((kind, getattr(row, column)) for kind, column in _FILE_COLUMN.items()
                if getattr(row, column) is not None)


def blob_sha(text: str) -> str:
    """The SHA-1 git (and the GitHub contents API) gives a file with this content."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


# --------------------------------------------------------------------------- #
# Writers                                                                     #
# --------------------------------------------------------------------------- #


def record_pending(db: Session, pr_row: ProjectPullRequest, entries: Iterable[dict]) -> None:
    """Note what an open PR carries to its (repo, target branch).

    ``entries`` are what a campaign committed to the PR's branch, each
    ``{"kind", "id", "path", "sha", "delete"}``. An entry for a file nothing
    owns any more, or with no id, is skipped.
    """
    entries = list(entries or [])
    if not entries:
        return
    repo_id = _repo_id(db, pr_row.repo_name, create=True)
    for entry in entries:
        row = _row(db, entry["kind"], entry.get("id"), repo_id, pr_row.target_branch)
        if row is None:
            continue
        row.pending_delete = bool(entry.get("delete"))
        row.pending_sha = None if row.pending_delete else entry.get("sha")
        row.pending_path = entry.get("path")
        row.pending_pr_id = pr_row.pr_id
    db.flush()


def _settle_merge(db: Session, row: FileDelivery, pr_row: ProjectPullRequest) -> None:
    if row.pending_delete:
        db.delete(row)
        return
    row.delivered_sha, row.delivered_path = row.pending_sha, row.pending_path
    row.delivered_at = pr_row.merged_at or _now()
    row.delivered_pr_id = pr_row.pr_id
    _clear_pending(row)


def record_pr_outcome(db: Session, pr_row: ProjectPullRequest, state: str) -> list:
    """Settle what *pr_row* carried, once it has merged or closed.

    A merge delivers what it carried, or removes the file where it carried a
    deletion. A close leaves the branch as it was. Returns ``(kind, file_id)``
    for each file the merge removed from its (repo, branch).
    """
    removed = []
    if state not in ("merged", "closed"):
        return removed
    for row in db.query(FileDelivery).filter(FileDelivery.pending_pr_id == pr_row.pr_id).all():
        if state != "merged":
            _clear_pending(row)
            continue
        if row.pending_delete:
            removed.append(_file_of(row))
        _settle_merge(db, row, pr_row)
    db.flush()
    return removed


def record_delivered(db: Session, kind: str, file_id, repo_name: str, branch: str,
                     path, sha) -> None:
    """A direct push or an adopt left this content on (repo, branch).

    An open PR's pending change is kept: the PR still carries it.
    """
    row = _row(db, kind, file_id, _repo_id(db, repo_name, create=True), branch)
    if row is None:
        return
    row.delivered_sha, row.delivered_path = sha, path
    row.delivered_at = _now()
    row.delivered_pr_id = None
    db.flush()


def record_matched(db: Session, kind: str, file_id, repo_id, branch: str, sha, path=None):
    """GitHub holds exactly ActionsManager's content on (repo, branch), so *sha* is what is delivered there.

    *path* is where the check read it, when known. Returns the row, or None
    for a file nothing owns.
    """
    row = _row(db, kind, file_id, repo_id, branch)
    if row is None:
        return None
    row.delivered_sha = sha
    if path is not None:
        row.delivered_path = path
    if row.delivered_at is None:
        row.delivered_at = _now()
    db.flush()
    return row


def record_check(db: Session, kind: str, file_id, repo_id, branch: str, *, has_drift: bool, deleted: bool,
                 github_sha, content_hash, now: datetime, etag=UNSET) -> tuple:
    """Store what a drift check found on (repo, branch); returns (row, whether it was drifted before).

    A clean-to-drifted transition starts a new drift cycle. *etag* is stored
    only when passed: a read GitHub answered with 304 keeps the one it matched.

    A (repo, branch) with no row gets one only when there is something to
    keep: drift, or an ETag for the next read. A check therefore never adds a
    workflow's row (see has_any_record): a workflow drifts only where the
    record already holds its delivery, or outside its project, where a check
    records nothing.
    """
    row = _row(db, kind, file_id, repo_id, branch, create=has_drift or etag not in (UNSET, None))
    if row is None:
        return None, False
    was_drifted = bool(row.has_drift)
    if has_drift and not was_drifted:
        row.drift_cycle_count = (row.drift_cycle_count or 0) + 1
    row.has_drift, row.deleted_in_github = has_drift, deleted
    row.github_sha, row.content_hash, row.last_checked_at = github_sha, content_hash, now
    if etag is not UNSET:
        row.etag = etag
    db.flush()
    return row, was_drifted


def resolve_drift(db: Session, kind: str, file_id, repo_id=None, branch=None) -> list:
    """Mark this file's drifted rows clean, scoped to *repo_id* and *branch* when given; returns them."""
    query = _rows(db, kind, file_id).filter(FileDelivery.has_drift.is_(True))
    if repo_id is not None:
        query = query.filter(FileDelivery.repo_id == repo_id)
    if branch is not None:
        query = query.filter(FileDelivery.branch == branch)
    rows = query.all()
    now = _now()
    for row in rows:
        row.has_drift = row.deleted_in_github = False
        row.last_checked_at = now
    db.flush()
    return rows


def reset_drift(db: Session, kind: str, file_id, project_id: int) -> None:
    """Forget what drift checks saw of this file in *project_id*'s rows; what was delivered stays.

    The cycle count stays too: it keeps a later drift's notification from
    colliding with an earlier one's dedup key.
    """
    for row in _rows(db, kind, file_id).filter(FileDelivery.project_id == project_id):
        row.has_drift = row.deleted_in_github = False
        row.github_sha = row.etag = row.content_hash = None
    db.flush()


def record_removed(db: Session, kind: str, file_id, repo_name: str, branch: str) -> None:
    """The file is not on (repo, branch) any more, or never was.

    An open PR's pending change is kept: merging it still lands there.
    """
    row = _rows(db, kind, file_id).filter(
        FileDelivery.repo_id == _repo_id(db, repo_name), FileDelivery.branch == (branch or ""),
    ).first()
    if row is None:
        return
    row.delivered_sha = row.delivered_path = row.delivered_at = row.delivered_pr_id = None
    db.flush()


# --------------------------------------------------------------------------- #
# Readers                                                                     #
# --------------------------------------------------------------------------- #


def path_on(row, current_path):
    """Where a file is on the (repo, branch) of the record's *row*: where it was delivered there, else *current_path*.

    Moving or renaming a file locally is an edit, which never makes drift: its
    copy is read, and a delivery that writes the new path removes it, where
    the record says it was delivered. *row* is None where the record has none.
    """
    if row is not None and row.delivered_path:
        return row.delivered_path
    return current_path


def left_behind(old_path, new_path):
    """*old_path* when writing the file at *new_path* leaves that copy behind, else None.

    Compared case-insensitively: renaming ``ci`` to ``ci2`` and then ``CI``
    writes a path that differs from the delivered one only in case, and if
    GitHub folds case there, removing the "old" one deletes the file just
    written. An orphan is the cheaper mistake.
    """
    if not old_path or old_path.lower() == (new_path or "").lower():
        return None
    return old_path


def _at(db: Session, kind: str, file_id, repo_name: str, branch: str):
    return _rows(db, kind, file_id).join(Repo, Repo.repo_id == FileDelivery.repo_id).filter(
        Repo.repo_name == repo_name, FileDelivery.branch == (branch or ""),
    )


def _delivered(query):
    return query.filter(FileDelivery.delivered_at.isnot(None))


def _pending(query):
    return query.join(
        ProjectPullRequest, ProjectPullRequest.pr_id == FileDelivery.pending_pr_id,
    ).filter(_PR_OPEN)


def _drifted(db: Session, project_id: int):
    return db.query(FileDelivery).filter(FileDelivery.project_id == project_id, FileDelivery.has_drift.is_(True))


def delivered_here(db: Session, kind: str, file_id, repo_name: str, branch: str) -> bool:
    return _delivered(_at(db, kind, file_id, repo_name, branch)).first() is not None


def delivered_path(db: Session, kind: str, file_id, repo_name: str, branch: str):
    """The path the record says this file was delivered to on (repo, branch), or None."""
    return path_on(_delivered(_at(db, kind, file_id, repo_name, branch)).first(), None)


def superseded_path(db: Session, kind: str, file_id, repo_name: str, branch: str, new_path: str):
    """The delivered copy that writing this file at *new_path* on (repo, branch) leaves behind, or None.

    Only where the record holds a delivery there: a file never delivered to
    (repo, branch) left nothing there to remove, whatever it was called.
    """
    return left_behind(delivered_path(db, kind, file_id, repo_name, branch), new_path)


def pending_here(db: Session, kind: str, file_id, repo_name: str, branch: str) -> bool:
    """Whether an open PR carries a change to this file on exactly this (repo, branch)."""
    return _pending(_at(db, kind, file_id, repo_name, branch)).first() is not None


def delivered_anywhere(db: Session, kind: str, file_id) -> bool:
    return _delivered(_rows(db, kind, file_id)).first() is not None


def project_delivered_anywhere(db: Session, project_id: int) -> bool:
    """Whether the record holds any file of *project_id* delivered anywhere."""
    return _delivered(db.query(FileDelivery).filter(FileDelivery.project_id == project_id)).first() is not None


def project_delivered_repos(db: Session, project_id: int) -> list:
    """Names of the repositories the record holds any file of *project_id* delivered to, sorted."""
    query = _delivered(db.query(Repo.repo_name).join(FileDelivery, FileDelivery.repo_id == Repo.repo_id)
                       .filter(FileDelivery.project_id == project_id))
    return [name for (name,) in query.distinct().order_by(Repo.repo_name)]


def open_pr_ids_carrying_project(db: Session, project_id: int) -> set:
    """Ids of the open PRs, of any project, carrying a change to a file of *project_id*."""
    query = _pending(db.query(FileDelivery).filter(FileDelivery.project_id == project_id))
    return {pr_id for (pr_id,) in query.with_entities(FileDelivery.pending_pr_id).distinct()}


def pending_anywhere(db: Session, kind: str, file_id, pr_project_id=None) -> bool:
    """Whether any open PR carries a change to this file, or one of *pr_project_id*'s PRs."""
    query = _pending(_rows(db, kind, file_id))
    if pr_project_id is not None:
        query = query.filter(ProjectPullRequest.project_id == pr_project_id)
    return query.first() is not None


def has_any_record(db: Session, kind: str, file_id) -> bool:
    """Whether the record has tracked this file anywhere, delivered or not.

    Asked through ``workflows._record_never_saw``, for a workflow (which
    ``_may_be_delivered_unrecorded`` acts on only for a repository outside
    the workflow's project) and for a removal. Drift checks never change this
    answer for a workflow. A check records drift only in the project's own
    repositories, since only there does its result carry a repo id, and it
    adds a row only for drift; a workflow drifts only where its delivery is
    already on the record, because outside the record it is "not delivered
    yet" in the project's own repositories. A custom file is read only where
    the record already has a row.
    """
    return _rows(db, kind, file_id).first() is not None


def open_prs_carrying(db: Session, kind: str, file_id) -> list:
    """The open PRs carrying a change to this file, oldest first."""
    return (
        _pending(_rows(db, kind, file_id))
        .with_entities(ProjectPullRequest)
        .order_by(ProjectPullRequest.pr_id)
        .all()
    )


def workflow_ids_with_open_pending(db: Session, workflow_ids) -> set:
    """The subset of *workflow_ids* an open PR carries a change to, from any project."""
    if not workflow_ids:
        return set()
    query = db.query(FileDelivery).filter(FileDelivery.workflow_id.in_(list(workflow_ids)))
    return {wid for (wid,) in _pending(query).with_entities(FileDelivery.workflow_id).distinct()}


def reached_repo_ids(db: Session, workflow_ids) -> set:
    """Repos where any of *workflow_ids* is delivered, or carried by an open PR, on any branch."""
    if not workflow_ids:
        return set()
    query = db.query(FileDelivery).filter(FileDelivery.workflow_id.in_(list(workflow_ids)))
    delivered = {rid for (rid,) in _delivered(query).with_entities(FileDelivery.repo_id).distinct()}
    pending = {rid for (rid,) in _pending(query).with_entities(FileDelivery.repo_id).distinct()}
    return delivered | pending


def delivered_at_by_target(db: Session, kind: str, file_id) -> dict:
    """``{(repo_id, branch): (delivered_at, delivered path or None)}`` for every place this file is delivered."""
    return {(row.repo_id, row.branch): (row.delivered_at, path_on(row, None))
            for row in _delivered(_rows(db, kind, file_id))}


def _held(row: FileDelivery, repo_name: str, pr_open, current_path: str):
    if row.delivered_at is not None:
        return Delivered(repo_name, row.branch, path_on(row, current_path), row.delivered_sha)
    if pr_open and not row.pending_delete:
        return Delivered(repo_name, row.branch, row.pending_path or current_path, row.pending_sha)
    return None


def delivered_targets(db: Session, kind: str, file_id, current_path: str) -> list:
    """Every (repo, branch) the record holds this file delivered to, or an open PR carries it to, as ``Delivered``.

    Each at the path the file is at there: where it was delivered
    (``path_on``), else where the PR carries it, else *current_path*, which
    the record names when it predates recorded paths. A PR carrying only its
    removal holds nothing there to remove. Ordered by repository and branch.
    """
    query = _rows(db, kind, file_id).join(Repo, Repo.repo_id == FileDelivery.repo_id).outerjoin(
        ProjectPullRequest, ProjectPullRequest.pr_id == FileDelivery.pending_pr_id,
    ).add_columns(Repo.repo_name, _PR_OPEN).order_by(Repo.repo_name, FileDelivery.branch)
    held = (_held(row, repo_name, pr_open, current_path) for row, repo_name, pr_open in query)
    return [place for place in held if place is not None]


def delivered_by_pr(db: Session, kind: str, pr_row: ProjectPullRequest) -> list:
    """Ids of the files of *pr_row*'s own project that merging it delivered."""
    column = getattr(FileDelivery, _FILE_COLUMN[kind])
    return [
        file_id for (file_id,) in db.query(column).filter(
            FileDelivery.delivered_pr_id == pr_row.pr_id,
            FileDelivery.project_id == pr_row.project_id,
            column.isnot(None),
        )
    ]


def targets_for(db: Session, project_id: int) -> dict:
    """Every row of *project_id*'s files, keyed ``(kind, file_id, repo_id, branch)``.

    One query for a whole drift check, in place of one per (file, repo,
    branch). Each value is a ``Target``.
    """
    query = db.query(FileDelivery, _PR_OPEN).outerjoin(
        ProjectPullRequest, ProjectPullRequest.pr_id == FileDelivery.pending_pr_id,
    ).filter(FileDelivery.project_id == project_id)
    return {(*_file_of(row), row.repo_id, row.branch): Target(row, bool(is_open)) for row, is_open in query}


def drifted_rows(db: Session, project_id: int, kind=None, file_id=None) -> list:
    """The rows of *project_id*'s files the last check found drifted, by repository and branch.

    Of one kind when *kind* is given, and of one file when *file_id* is too.
    """
    query = _drifted(db, project_id)
    if kind is not None:
        column = getattr(FileDelivery, _FILE_COLUMN[kind])
        query = query.filter(column.isnot(None) if file_id is None else column == file_id)
    return query.join(Repo, Repo.repo_id == FileDelivery.repo_id).order_by(Repo.repo_name, FileDelivery.branch).all()


def drift_count(db: Session, project_id: int) -> int:
    """How many (file, repo, branch) of *project_id* the last check found drifted."""
    return _drifted(db, project_id).count()
