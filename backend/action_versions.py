"""Pinned GitHub Action versions used by generated/prebuilt workflow templates.

Single source of truth for build_detector.py and workflow_templates.py --
bump a version here once instead of hunting through generated YAML strings.
Checked against `gh api repos/<action>/releases/latest`.
"""

# Shared literal only to keep Sonar S1192 quiet; each action still bumps on its own.
_V6_1_0 = "v6.1.0"

ACTION_VERSIONS = {
    "actions/checkout": "v7.0.1",
    "actions/setup-java": _V6_1_0,
    "actions/setup-node": "v7.1.0",
    "actions/setup-python": "v7.1.0",
    "actions/setup-go": "v7.0.0",
    "actions/setup-dotnet": _V6_1_0,
    "actions/cache": _V6_1_0,
}
