"""
Pinning an action (#2046): the textual rewrite of `uses:`, and tag to SHA.

The rewrite splices the stored text, so everything outside the values and
trailing comments it changes must stay byte-identical, and a `uses` in a
comment or a `run:` block is never touched.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi import HTTPException

from action_pinning import changed_lines, latest_release, resolve_ref_to_sha, rewrite_uses
from github_api_tracker import RateLimitExceeded

NEW_SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"
OLD_SHA = "08c6903cd8c0fde910a37f88322edcfb5dd907a8"
ACTION = "actions/checkout"


def _steps(*lines: str) -> str:
    body = "\n".join(lines)
    return f"name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n{body}\n"


def _pin(text: str, comment="v5.0.0", action=ACTION, sha=NEW_SHA):
    return rewrite_uses(text, action, sha, comment)


def test_a_tag_is_pinned_to_the_sha_with_the_version_as_a_comment():
    text = _steps("      - uses: actions/checkout@v4")

    new, count = _pin(text)

    assert (new, count) == (_steps(f"      - uses: actions/checkout@{NEW_SHA} # v5.0.0"), 1)


def test_pinning_the_same_sha_again_changes_nothing():
    new, _ = _pin(_steps("      - uses: actions/checkout@v4"))

    assert _pin(new) == (new, 0)


def test_a_sha_with_the_right_comment_already_is_the_pin():
    text = _steps(f"      - uses: actions/checkout@{NEW_SHA}   # v5.0.0")

    assert _pin(text) == (text, 0)


def test_bumping_from_one_sha_to_another_replaces_the_old_comment_instead_of_adding_one():
    text = _steps(f"      - uses: actions/checkout@{OLD_SHA} # v4.2.2")

    new, count = _pin(text)

    assert (new, count) == (_steps(f"      - uses: actions/checkout@{NEW_SHA} # v5.0.0"), 1)


def test_every_occurrence_is_pinned_at_any_indent_and_only_those_lines_change():
    text = (
        "name: ci\non: push\njobs:\n"
        "  one:\n    steps:\n      - uses: actions/checkout@v3\n      - run: make\n"
        "  two:\n    steps:\n          - uses: actions/checkout@main\n          - uses: actions/cache@v4\n"
    )

    new, count = _pin(text)

    assert count == 2
    assert [line["line"] for line in changed_lines(text, new)] == [6, 10]
    assert "actions/cache@v4" in new


def test_a_uses_in_a_comment_or_a_run_block_is_not_rewritten():
    text = _steps(
        "      # - uses: actions/checkout@v3",
        "      - run: |",
        "          echo 'uses: actions/checkout@v3'",
        "      - name: note uses: actions/checkout@v3",
        "        run: echo hi",
    )

    assert _pin(text) == (text, 0)


def test_the_rest_of_the_file_is_byte_identical():
    text = (
        "# top comment\r\nname:   ci\r\non:\r\n  push:\r\n    branches: [ main ]\r\n\r\njobs:\r\n"
        "  build:\r\n    runs-on: ubuntu-latest\r\n    steps:\r\n"
        "      -   uses: actions/checkout@v4   # old\r\n"
        "      - run: |\r\n          echo   spaced\r\n\r\n"
    )

    new, count = _pin(text)

    assert count == 1
    assert new == text.replace(
        "-   uses: actions/checkout@v4   # old\r\n", f"-   uses: actions/checkout@{NEW_SHA}   # v5.0.0\r\n"
    )


def test_a_quoted_value_keeps_its_quotes():
    text = _steps('      - uses: "actions/checkout@v4"', "      - uses: 'actions/checkout@v3' # three")

    new, _ = _pin(text)

    assert new == _steps(
        f'      - uses: "actions/checkout@{NEW_SHA}" # v5.0.0', f"      - uses: 'actions/checkout@{NEW_SHA}' # v5.0.0",
    )


def test_a_value_in_a_flow_mapping_is_pinned_without_a_comment_that_would_swallow_the_rest():
    text = _steps("      - {uses: actions/checkout@v4, with: {fetch-depth: 0}}")

    new, count = _pin(text)

    assert (new, count) == (_steps(f"      - {{uses: actions/checkout@{NEW_SHA}, with: {{fetch-depth: 0}}}}"), 1)


def test_the_path_into_the_action_is_kept():
    text = _steps("      - uses: github/codeql-action/init@v3", "      - uses: github/codeql-action/analyze@v3 # v3")

    new, count = _pin(text, comment="v4.1.0", action="github/codeql-action", sha=NEW_SHA)

    assert count == 2
    assert f"github/codeql-action/init@{NEW_SHA} # v4.1.0" in new
    assert f"github/codeql-action/analyze@{NEW_SHA} # v4.1.0" in new


def test_a_reusable_workflow_call_at_job_level_is_pinned():
    text = "on: push\njobs:\n  call:\n    uses: octo/shared/.github/workflows/build.yml@main\n    with:\n      x: 1\n"

    new, count = _pin(text, action="octo/shared", comment="v2")

    assert (new, count) == (
        f"on: push\njobs:\n  call:\n    uses: octo/shared/.github/workflows/build.yml@{NEW_SHA} # v2\n    with:\n      x: 1\n",
        1,
    )


def test_the_action_is_matched_by_owner_and_repo_without_regard_to_case_and_never_by_prefix():
    text = _steps("      - uses: Actions/Checkout@v4", "      - uses: actions/checkout-extra@v1", "      - uses: actions/checkout2@v1")

    new, count = _pin(text)

    assert count == 1
    assert "actions/checkout-extra@v1" in new
    assert "actions/checkout2@v1" in new


def test_without_a_comment_a_stale_version_comment_goes_and_any_other_comment_stays():
    text = _steps("      - uses: actions/checkout@v4 # v4", "      - uses: actions/checkout@v3 # keep this")

    new, _ = _pin(text, comment=None)

    assert new == _steps(f"      - uses: actions/checkout@{NEW_SHA}", f"      - uses: actions/checkout@{NEW_SHA} # keep this")


@pytest.mark.parametrize("text", ["", "   \n", "name: only\n", "jobs: [unclosed\n  - {", "jobs:\n  a: nope\n", "- a\n- list\n", None])
def test_text_without_a_pinnable_uses_comes_back_unchanged(text):
    assert _pin(text) == (text, 0)


def test_changed_lines_name_the_line_and_both_sides():
    assert changed_lines("a\nb\nc", "a\nB\nc") == [{"line": 2, "before": "b", "after": "B"}]


def _response(status, payload=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {} if payload is None else payload
    return response


def _resolve(response=None, side_effect=None, ref="v5.0.0"):
    with patch("action_pinning.github_get", return_value=response, side_effect=side_effect) as get:
        return resolve_ref_to_sha(ACTION, ref, "token", MagicMock(), "alice"), get


def test_a_tag_resolves_to_its_commit_with_one_call():
    sha, get = _resolve(_response(200, {"sha": NEW_SHA.upper()}))

    assert sha == NEW_SHA
    assert get.call_count == 1
    assert get.call_args.args[0] == "https://api.github.com/repos/actions/checkout/commits/v5.0.0"
    assert get.call_args.args[1] == "alice"


def test_a_branch_with_a_slash_is_kept_in_the_path():
    _, get = _resolve(_response(200, {"sha": NEW_SHA}), ref="release/v5")

    assert get.call_args.args[0].endswith("/commits/release/v5")


def test_a_full_sha_is_used_as_it_is_with_no_call():
    sha, get = _resolve(ref=NEW_SHA.upper())

    assert (sha, get.call_count) == (NEW_SHA, 0)


@pytest.mark.parametrize("ref", ["../x", "a b", "-flag", "v1?x=1", "a/../b", "v1#frag"])
def test_a_ref_that_is_not_a_name_is_refused_before_any_call(ref):
    db = MagicMock()
    with patch("action_pinning.github_get") as get:
        with pytest.raises(HTTPException) as refused:
            resolve_ref_to_sha(ACTION, ref, "token", db, "alice")

    assert (refused.value.status_code, get.call_count) == (400, 0)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, 404), (422, 404), (401, 401), (403, 403), (500, 502), (301, 502)],
)
def test_github_answers_map_to_the_status_the_caller_can_act_on(status, expected):
    answer = _response(status)
    with pytest.raises(HTTPException) as refused:
        _resolve(answer)

    assert refused.value.status_code == expected


def test_an_answer_without_a_commit_sha_is_a_bad_gateway():
    answer = _response(200, {"sha": "not-a-sha"})
    with pytest.raises(HTTPException) as refused:
        _resolve(answer)

    assert refused.value.status_code == 502


def test_the_rate_limit_is_a_429():
    over = RateLimitExceeded("over")
    with pytest.raises(HTTPException) as refused:
        _resolve(side_effect=over)

    assert refused.value.status_code == 429


def test_a_network_failure_is_a_bad_gateway():
    down = requests.ConnectionError("down")
    with pytest.raises(HTTPException) as refused:
        _resolve(side_effect=down)

    assert refused.value.status_code == 502


def _latest(*answers, side_effect=None):
    with patch("action_pinning.github_get", side_effect=side_effect or list(answers)) as get:
        return latest_release(ACTION, "token", MagicMock(), "alice"), get


def _urls(get) -> list:
    return [call.args[0].removeprefix("https://api.github.com/repos/actions/checkout/") for call in get.call_args_list]


def _tag(name: str, sha: str = NEW_SHA) -> dict:
    return {"name": name, "commit": {"sha": sha}}


def test_the_latest_release_is_looked_up_and_its_tag_resolved_to_a_commit():
    found, get = _latest(_response(200, {"tag_name": "v5.0.0"}), _response(200, {"sha": NEW_SHA}))

    assert found == ("v5.0.0", NEW_SHA)
    assert _urls(get) == ["releases/latest", "commits/v5.0.0"]


def test_an_action_without_releases_defaults_to_its_highest_stable_version_tag():
    tags = [_tag("v4.2.2", OLD_SHA), _tag("v5.0.1"), _tag("v5.0.2-rc1", OLD_SHA), _tag("nightly", OLD_SHA), _tag("v5")]

    found, get = _latest(_response(404), _response(200, tags))

    assert found == ("v5.0.1", NEW_SHA)
    assert _urls(get) == ["releases/latest", "tags?per_page=100"]


def test_version_tags_are_ordered_by_number_not_by_text():
    found, _ = _latest(_response(404), _response(200, [_tag("v9.9.9", OLD_SHA), _tag("v10.0.0")]))

    assert found == ("v10.0.0", NEW_SHA)


@pytest.mark.parametrize("tags", [[], [_tag("nightly"), _tag("v1.0.0-beta")], [{"name": "v1.0.0", "commit": {"sha": "x"}}]])
def test_an_action_with_nothing_to_default_to_says_to_enter_a_ref(tags):
    answers = (_response(404), _response(200, tags))
    with pytest.raises(HTTPException) as refused:
        _latest(*answers)

    assert refused.value.status_code == 404
    assert "Enter a tag, branch or commit SHA" in refused.value.detail


def test_a_release_without_a_tag_name_is_a_bad_gateway():
    answer = _response(200, {"tag_name": ""})
    with pytest.raises(HTTPException) as refused:
        _latest(answer)

    assert refused.value.status_code == 502


@pytest.mark.parametrize(("status", "expected"), [(401, 401), (403, 403), (500, 502)])
def test_a_refused_release_lookup_maps_to_the_status_the_caller_can_act_on(status, expected):
    answer = _response(status)
    with pytest.raises(HTTPException) as refused:
        _latest(answer)

    assert refused.value.status_code == expected


def test_a_missing_repository_is_a_404_from_the_tag_fallback():
    answers = (_response(404), _response(404))
    with pytest.raises(HTTPException) as refused:
        _latest(*answers)

    assert refused.value.status_code == 404


def test_the_latest_lookup_stops_at_the_rate_limit():
    over = RateLimitExceeded("over")
    with pytest.raises(HTTPException) as refused:
        _latest(side_effect=over)

    assert refused.value.status_code == 429
