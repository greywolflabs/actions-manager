"""
Reusable Workflow Detection

Provides a helper function to detect whether a workflow YAML file
is a reusable workflow by checking for the `workflow_call` trigger.
"""

from typing import Optional

import yaml


def is_reusable_workflow_yaml(workflow_yaml: object) -> bool:
    """
    Determine if a workflow YAML string defines a reusable workflow.

    A workflow is considered reusable when its triggers include `workflow_call`.

    Supports common valid YAML shapes:
      - on: workflow_call
      - on:\n  workflow_call:
      - "on":\n  workflow_call:
      - on: [push, workflow_call]
      - on with multiple triggers including workflow_call

    Args:
        workflow_yaml: The raw YAML content of the workflow file.

    Returns:
        True if the workflow contains a workflow_call trigger, False otherwise.
    """
    return declared_reusable(workflow_yaml) is True


def declared_reusable(workflow_yaml: object) -> Optional[bool]:
    """Whether the YAML declares a reusable workflow, or None if it says nothing.

    None when the YAML does not parse or has no ``on:``: a half-typed edit must
    not read as "regular" and change what kind of workflow it is (#2318).
    """
    if not workflow_yaml or not isinstance(workflow_yaml, str):
        return None

    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return None

    if not isinstance(parsed, dict):
        return None

    # The trigger key is "on" (or True in YAML due to boolean parsing)
    triggers = parsed.get("on") or parsed.get(True)

    if triggers is None:
        return None

    # Case: on: workflow_call  (string)
    if isinstance(triggers, str):
        return triggers == "workflow_call"

    # Case: on: [push, workflow_call]  (list)
    if isinstance(triggers, list):
        return "workflow_call" in triggers

    # Case: on:\n  workflow_call:\n  push:  (dict)
    if isinstance(triggers, dict):
        return "workflow_call" in triggers

    return None
