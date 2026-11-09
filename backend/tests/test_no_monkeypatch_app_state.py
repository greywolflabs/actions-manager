"""
pytest's monkeypatch must not patch app.state: use unittest.mock.patch.object.

Since pytest 9.2 its setattr takes the value to restore from the target's
__dict__. Starlette's State keeps its values in _state, so undo finds nothing
there and deletes the attribute: one patched middleware_db_factory took every
later test on the worker down with it (#2454).
"""

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PATCHES_APP_STATE = re.compile(r"\w\.setattr\(\s*(?:\w+\.)*app\.state\b")


def test_no_test_monkeypatches_app_state():
    offenders = [
        f"{path.relative_to(TESTS_DIR)}:{number}"
        for path in sorted(TESTS_DIR.rglob("*.py"))
        if path != Path(__file__).resolve()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if PATCHES_APP_STATE.search(line)
    ]

    assert offenders == [], "patch app.state with unittest.mock.patch.object instead: " + ", ".join(offenders)
