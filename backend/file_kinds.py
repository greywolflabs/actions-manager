"""
The repository files a Managed Files project can manage (#2367), one entry per kind.

A Managed Files project (project_type ``managed_files``) manages exactly one
kind, named by ``projects.file_kind`` and fixed at creation. An entry holds what
differs between kinds and generic code needs; the rest (syntax checks, the
editor) stays with the kind's own code, such as ``codeowners.py``.
"""

import posixpath
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Codeowners

MANAGED_FILES = "managed_files"


@dataclass(frozen=True)
class FileKind:
    label: str
    # Every path GitHub reads the file from, in the order it looks.
    paths: tuple
    # {project_id: path} for those of the given projects that have stored the file.
    file_paths: Callable[[Session, list], dict]


def _codeowners_paths(db: Session, project_ids: list) -> dict:
    return dict(
        db.query(Codeowners.project_id, Codeowners.file_path).filter(Codeowners.project_id.in_(project_ids))
    )


FILE_KINDS = {
    "codeowners": FileKind("CODEOWNERS", (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"), _codeowners_paths),
}


# What a Managed Files project cannot hold, by what a request would add to it.
_NOT_HELD = {
    "workflows": "workflows",
    "custom_files": "custom files",
    "linked_reusable_workflows": "linked reusable workflows",
    "validation_repo": "a validation repository",
}


def kind_for_path(path: Optional[str]) -> Optional[str]:
    """The kind whose file sits at *path*, if GitHub reads one from there.

    Compared in normal form, so "./CODEOWNERS", ".github//CODEOWNERS" or a
    backslash spelling of a kind's path is that path too.
    """
    cleaned = posixpath.normpath((path or "").strip().replace("\\", "/")).lstrip("/")
    return next((kind for kind, definition in FILE_KINDS.items() if cleaned in definition.paths), None)


def _holds_its_kind(project, kind: str) -> bool:
    return project.project_type == MANAGED_FILES and project.file_kind == kind


def project_accepts(project, holds: Optional[str] = None, *, kind: Optional[str] = None,
                    path: Optional[str] = None) -> Optional[str]:
    """Why *project* cannot hold what a request would give it, or None when it can.

    *project* is a Project row or a create payload; only its project_type and
    file_kind are read.

    - *holds* (a key of _NOT_HELD): a Managed Files project holds its one
      file and nothing else.
    - *kind*, or a file at *path*: a kind's file belongs to a Managed Files
      project of that kind and to no other project, so two projects never
      write the one file GitHub reads from a repository.
    """
    if holds is not None and project.project_type == MANAGED_FILES:
        return (
            f"A Managed Files project manages only its {FILE_KINDS[project.file_kind].label} file, "
            f"so it cannot hold {_NOT_HELD[holds]}."
        )
    owned_kind = kind or kind_for_path(path)
    if owned_kind is None or _holds_its_kind(project, owned_kind):
        return None
    label = FILE_KINDS[owned_kind].label
    where = f"{path.strip()} is a {label} file, and {label}" if path else label
    return f"{where} is managed in Managed Files. Manage it from a {label} project there."


def refuse_unless_accepted(project, holds: Optional[str] = None, *, kind: Optional[str] = None,
                           path: Optional[str] = None) -> None:
    """Raise 400 when *project* cannot hold what a request would give it (see project_accepts)."""
    reason = project_accepts(project, holds, kind=kind, path=path)
    if reason:
        raise HTTPException(status_code=400, detail=reason)
