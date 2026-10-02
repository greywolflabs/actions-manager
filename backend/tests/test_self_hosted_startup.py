"""
Regression tests for self-hosted startup reliability.

Validates that backend/main.py never imports the cloud package
(backend/cloud/: the admin panel and the Marketplace webhooks), in either
installation mode, and serves none of its routes. The self-hosted image and the
public export both drop backend/cloud/ entirely, so any import of it from core
would fail with:
  ERROR: Error loading ASGI app. Could not import module "main".
The cloud image mounts those routes through cloud/app.py instead.
"""

import importlib
import sys
import os
import types
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_cloud_module(name: str) -> bool:
    return name.partition(".")[0] == "cloud"


def _reload_main_without_cloud_modules(installation_mode: str):
    """
    Reload the main module with the cloud package unimportable, simulating
    the self-hosted image and the public export, where backend/cloud/ does not
    exist. Any import of it from main fails the reload.

    Returns the reloaded main module so callers can inspect app routes.
    """
    with patch.dict(os.environ, {"INSTALLATION_MODE": installation_mode}):
        # Ensure config picks up the patched env var
        if "config" in sys.modules:
            config_mod = sys.modules["config"]
            original_mode = getattr(config_mod, "INSTALLATION_MODE", None)
            config_mod.INSTALLATION_MODE = installation_mode
        else:
            original_mode = None

        # Remove cached modules so the import runs from scratch
        originals = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "main" or _is_cloud_module(name)
        }

        class _BlockCloudModules(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if _is_cloud_module(fullname):
                    raise ModuleNotFoundError(f"No module named '{fullname}'")
                return None

        blocker = _BlockCloudModules()
        sys.meta_path.insert(0, blocker)

        try:
            import main as main_mod
            return main_mod
        finally:
            sys.meta_path.remove(blocker)
            # Later tests patch these modules by name and reuse main's app, so
            # put back the very objects they were already bound to.
            sys.modules.update(originals)
            # Restore config mode
            if original_mode is not None and "config" in sys.modules:
                sys.modules["config"].INSTALLATION_MODE = original_mode


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSelfHostedStartup:
    """Validates that main.py starts cleanly without the cloud package."""

    @pytest.mark.parametrize("installation_mode", ["self-hosted", "cloud"])
    def test_main_imports_without_cloud_package(self, installation_mode):
        """
        main.py must import with backend/cloud/ absent in both modes: core never
        imports the cloud package, even when INSTALLATION_MODE=cloud.
        """
        main_mod = _reload_main_without_cloud_modules(installation_mode)
        assert hasattr(main_mod, "app"), "main.py must expose a FastAPI 'app' object"
        assert not hasattr(main_mod, "admin")
        assert not hasattr(main_mod, "marketplace_webhooks")

    @pytest.mark.parametrize("installation_mode", ["self-hosted", "cloud"])
    def test_core_app_serves_no_cloud_routes(self, installation_mode):
        """
        /admin/* and /webhooks/marketplace are only mounted by cloud/app.py, so
        the core app must not serve them in either mode. (app.routes can't show
        this: FastAPI no longer flattens included routers into it.)
        """
        main_mod = _reload_main_without_cloud_modules(installation_mode)
        client = TestClient(main_mod.app)
        assert client.get("/admin/users").status_code == 404
        assert client.post("/webhooks/marketplace", json={}).status_code == 404

    def test_main_py_file_exists(self):
        """
        Regression guard: backend/main.py must be present in the source tree.
        This catches accidental deletion or mis-naming of the entry point.
        """
        backend_dir = os.path.join(os.path.dirname(__file__), "..")
        main_path = os.path.join(backend_dir, "main.py")
        assert os.path.isfile(main_path), (
            f"backend/main.py is missing at {os.path.abspath(main_path)}. "
            "Uvicorn requires this file as its entry point."
        )

    def test_main_import_exposes_app(self):
        """
        The 'main' module must expose a 'app' attribute that Uvicorn can bind to
        via 'uvicorn main:app'.
        """
        # Use the already-imported module from the test suite's sys.modules
        # (or import fresh if needed)
        if "main" not in sys.modules:
            import main  # noqa: F401
        main_mod = sys.modules["main"]
        assert hasattr(main_mod, "app"), (
            "main.py must define 'app' for 'uvicorn main:app' to work"
        )


def _tables_after_import(module, tmp_path, installation_mode):
    import contextlib
    import sqlite3
    import subprocess

    db = tmp_path / "fresh.db"
    env = {**os.environ, "INSTALLATION_MODE": installation_mode, "DATABASE_URL": f"sqlite:///{db}"}
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=backend, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    with contextlib.closing(sqlite3.connect(db)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_self_hosted_database_gets_no_cloud_only_table(tmp_path):
    # #2184: the Marketplace webhook-events model lives in backend/cloud/, so a
    # fresh self-hosted database is created without its table. Run in a
    # subprocess: in this process the cloud tests may already have imported it.
    tables = _tables_after_import("main", tmp_path, "self-hosted")
    assert "accounts" in tables
    assert "marketplace_webhook_events" not in tables
