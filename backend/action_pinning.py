"""
Pin or bump one action across workflows (#2046): the textual rewrite of a
``uses:`` value, and the one GitHub call that turns a tag into its commit SHA.

A workflow is stored and delivered as text, so the rewrite splices the new
value into the stored text instead of dumping parsed YAML, which would
reformat every other line and drop its comments. PyYAML's node marks say where
each ``uses`` value sits; only that range, and a trailing comment on its line,
is ever replaced.
"""

import re
from typing import Any, Iterator, Optional
from urllib.parse import quote

import requests
import yaml
from fastapi import HTTPException

from action_usage import FULL_SHA, VERSION_TAG, classify_reference
from error_detail import failure_kind
from github_api_tracker import RateLimitExceeded, github_get
from workflows import ACCEPT_HEADER, GITHUB_API_URL, X_API_VERSION

# What follows a value on its line when nothing but a comment does.
_LINE_TAIL = re.compile(r"([ \t]*)(#[^\r]*)?(\r?)")
# A tag or branch name, safe to put in a URL path.
_SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _key(node: Any) -> Optional[str]:
    """A mapping key's text, or None when the key is not a plain scalar."""
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _job_uses_nodes(root: Any) -> Iterator[yaml.ScalarNode]:
    """The ``uses`` scalars of every job and step, as parse_uses_references reads them."""
    if not isinstance(root, yaml.MappingNode):
        return
    jobs = next((v for k, v in root.value if _key(k) == "jobs"), None)
    for _, job in jobs.value if isinstance(jobs, yaml.MappingNode) else []:
        if not isinstance(job, yaml.MappingNode):
            continue
        for key, value in job.value:
            name = _key(key)
            if name == "uses" and isinstance(value, yaml.ScalarNode):
                yield value
            elif name == "steps" and isinstance(value, yaml.SequenceNode):
                yield from _step_uses_nodes(value)


def _step_uses_nodes(steps: yaml.SequenceNode) -> Iterator[yaml.ScalarNode]:
    for step in steps.value:
        if not isinstance(step, yaml.MappingNode):
            continue
        for key, value in step.value:
            if _key(key) == "uses" and isinstance(value, yaml.ScalarNode):
                yield value


def _new_tail(rest: str, comment: Optional[str]) -> str:
    """What follows the value on its line once it is pinned.

    A comment, or nothing, is all that follows a value that ends its line: then
    the comment names the version pinned to. A stale version comment (``# v3``)
    is dropped when there is no new one, and any other comment is kept. When
    something else follows (a flow mapping's next key) the line is left alone,
    since a comment there would swallow it.
    """
    match = _LINE_TAIL.fullmatch(rest)
    if match is None:
        return rest
    gap, existing, carriage_return = match.groups()
    if comment:
        return f"{gap or ' '}# {comment}{carriage_return}"
    if existing and VERSION_TAG.fullmatch(existing[1:].strip()):
        return carriage_return
    return rest


def _edit_for(text: str, node: yaml.ScalarNode, action: str, sha: str, comment: Optional[str]):
    """(start, end, replacement) for one ``uses`` value, or None when it is not *action* or is already right."""
    reference = classify_reference(node.value)
    if reference is None or reference.action is None or reference.action.lower() != action.lower():
        return None
    start, end = node.start_mark.index, node.end_mark.index
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    name = node.value.strip().rpartition("@")[0]
    quote_mark = node.style if node.style in ("'", '"') else ""
    value = f"{quote_mark}{name}@{sha}{quote_mark}"
    tail = _new_tail(text[end:line_end], comment)
    if text[start:line_end] == value + tail:
        return None
    return start, line_end, value + tail


def rewrite_uses(workflow_yaml: Any, action: str, sha: str, comment: Optional[str] = None) -> tuple[str, int]:
    """*workflow_yaml* with every ``uses`` of *action* pinned to *sha*, and how many were changed.

    *action* is ``owner/repo``; a path into it (``github/codeql-action/init``)
    and whichever ref it was on are matched. The result is byte-identical to
    the input outside the values and trailing comments that changed. A value
    already at *sha* with the right comment is not a change, so pinning twice
    is a no-op. Text that is not a workflow, or does not parse, comes back as is.
    """
    if not isinstance(workflow_yaml, str) or not workflow_yaml.strip():
        return workflow_yaml, 0
    try:
        root = yaml.compose(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml, 0
    edits = [e for node in _job_uses_nodes(root) if (e := _edit_for(workflow_yaml, node, action, sha, comment))]
    text = workflow_yaml
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, len(edits)


def changed_lines(before: str, after: str) -> list[dict]:
    """The lines a rewrite changed, as ``{line, before, after}``. A rewrite never adds or removes a line."""
    return [
        {"line": number, "before": old, "after": new}
        for number, (old, new) in enumerate(zip(before.split("\n"), after.split("\n")), start=1)
        if old != new
    ]


def _repo_url(action: str, path: str) -> str:
    return f"{GITHUB_API_URL}/repos/{action}/{path}"


def _get(url: str, token: str, db, username: str):
    """One GitHub read as *username*; the rate limit and a network failure become the HTTP errors a caller can act on."""
    headers = {"Accept": ACCEPT_HEADER, "X-GitHub-Api-Version": X_API_VERSION, "Authorization": f"token {token}"}
    try:
        return github_get(url, username, db, headers=headers)
    except RateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=f"GitHub API rate limit reached: {exc}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {failure_kind(exc)}.") from exc


def _refuse(response: Any, action: str, subject: str) -> None:
    """Raise the HTTP error for a GitHub answer that is not a 200."""
    if response.status_code in (404, 422):
        raise HTTPException(status_code=404, detail=f"{subject} was not found on {action}.")
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="GitHub refused the saved credentials. Sign in again.")
    if response.status_code == 403:
        raise HTTPException(status_code=403, detail=f"GitHub refused access to {action}.")
    raise HTTPException(status_code=502, detail=f"GitHub answered {response.status_code} for {subject} on {action}.")


def _commit_sha(response: Any, action: str, ref: str) -> str:
    if response.status_code != 200:
        _refuse(response, action, ref)
    sha = str(response.json().get("sha", ""))
    if not FULL_SHA.fullmatch(sha):
        raise HTTPException(status_code=502, detail=f"GitHub did not return a commit for {ref} on {action}.")
    return sha.lower()


def _safe_ref(ref: str) -> str:
    if not _SAFE_REF.fullmatch(ref) or ".." in ref:
        raise HTTPException(status_code=400, detail="That is not a tag, branch or commit SHA.")
    return ref


def resolve_ref_to_sha(action: str, ref: str, token: str, db, username: str) -> str:
    """The commit SHA *ref* (a tag or branch) is on in *action*, with one GitHub call.

    A full SHA is returned as it is, with no call. The commits endpoint
    dereferences an annotated tag to its commit, which a lightweight tag read
    would not.
    """
    if FULL_SHA.fullmatch(ref):
        return ref.lower()
    url = _repo_url(action, f"commits/{quote(_safe_ref(ref), safe='/')}")
    return _commit_sha(_get(url, token, db, username), action, ref)


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", tag))


def _newest_stable_tag(action: str, token: str, db, username: str) -> tuple[str, str]:
    """(tag, commit SHA) of the highest stable version tag, for an action that publishes no releases."""
    response = _get(_repo_url(action, "tags?per_page=100"), token, db, username)
    if response.status_code != 200:
        _refuse(response, action, "Its tags")
    stable = [
        (tag["name"], str(tag.get("commit", {}).get("sha", "")))
        for tag in response.json()
        if isinstance(tag, dict) and VERSION_TAG.fullmatch(str(tag.get("name", ""))) and "-" not in tag["name"]
    ]
    stable = [(name, sha) for name, sha in stable if FULL_SHA.fullmatch(sha)]
    if not stable:
        raise HTTPException(
            status_code=404,
            detail=f"{action} has no release or version tag to default to. Enter a tag, branch or commit SHA.",
        )
    name, sha = max(stable, key=lambda found: _version_key(found[0]))
    return name, sha.lower()


def latest_release(action: str, token: str, db, username: str) -> tuple[str, str]:
    """(tag, commit SHA) of the action's newest published release.

    GitHub's "latest release" leaves out drafts and prereleases, which is the
    default wanted here. An action that publishes no releases falls back to its
    newest stable version tag. Pinning the head of a branch is never a default:
    that is code nobody reviewed.
    """
    response = _get(_repo_url(action, "releases/latest"), token, db, username)
    if response.status_code == 404:
        return _newest_stable_tag(action, token, db, username)
    if response.status_code != 200:
        _refuse(response, action, "Its latest release")
    tag = str(response.json().get("tag_name", ""))
    if not tag:
        raise HTTPException(status_code=502, detail=f"GitHub did not name the latest release of {action}.")
    return tag, resolve_ref_to_sha(action, tag, token, db, username)
