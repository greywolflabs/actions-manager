"""Seed the delivery record the way production writes it.

The "delivered here?", "is an open PR carrying it here?" and "did the last
check find drift here?" readers read ``file_deliveries``. A test that builds a
ProjectPullRequest row by hand, or wants a check's result without running one,
also needs what production records alongside it. Everything goes through
``deliveries`` itself, so it writes exactly what production would.
"""

from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import patch

import deliveries
from models import Codeowners, CustomFile, Workflow

_KINDS = {Workflow: deliveries.WORKFLOW, CustomFile: deliveries.CUSTOM_FILE, Codeowners: deliveries.CODEOWNERS}


def _entry(file) -> dict:
    return {"kind": _KINDS[type(file)], "id": file.id if not isinstance(file, Workflow) else file.workflow_id}


def carried(*files) -> list:
    """The ``deliveries`` a campaign result carries for *files*, for a mocked commit chain."""
    return [{**_entry(file), "path": None, "sha": None, "delete": False} for file in files]


def removed(*files) -> list:
    """The ``deliveries`` a removal PR's result carries for *files*: each taken off its branch."""
    return [{**entry, "delete": True} for entry in carried(*files)]


def record_pr_carries(db, pr, *files) -> None:
    """Record that the open PR *pr* carries *files* (Workflow, CustomFile or
    Codeowners rows) to its repo and target branch, as a campaign does."""
    db.flush()
    deliveries.record_pending(db, pr, [_entry(file) for file in files])
    db.commit()


def record_pr_merged(db, pr, *files) -> None:
    """Record that *pr* carried *files* and merged, as a campaign and its merge do."""
    record_pr_carries(db, pr, *files)
    deliveries.record_pr_outcome(db, pr, "merged")
    db.commit()


def record_delivered(db, file, repo_name: str, branch: str, sha=None, at=None, path=None) -> None:
    """Record that *file* was delivered to (repo, branch), as a merge or push
    does, optionally as of *at* and at *path*."""
    db.flush()
    entry = _entry(file)
    with patch.object(deliveries, "_now", return_value=at) if at else nullcontext():
        deliveries.record_delivered(db, entry["kind"], entry["id"], repo_name, branch, path, sha)
    db.commit()


def record_drift(db, file, repo_name: str, branch: str = "main", *, has_drift: bool = True,
                 deleted: bool = False, github_sha="b" * 40, content_hash="h", at=None) -> None:
    """Record what a drift check found for *file* on (repo, branch), as the check's
    recorder does: drifted by default, its GitHub copy *github_sha* (none when
    *deleted*), checked *at* (now by default)."""
    db.flush()
    entry = _entry(file)
    deliveries.record_check(
        db, entry["kind"], entry["id"], deliveries._repo_id(db, repo_name, create=True), branch,
        has_drift=has_drift, deleted=deleted, github_sha=None if deleted else github_sha,
        content_hash=content_hash, now=at or datetime.now(timezone.utc),
    )
    db.commit()
