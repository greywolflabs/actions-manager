"""
Tests for the strict startup mode validator (`backend/mode_validation.py`).

These tests cover the rules from the "Harden Cloud and Self-Hosted Release
Targets" issue:

* Self-hosted allows missing LICENSE_KEY (resolves to Free), missing
  GITHUB_WEBHOOK_SECRET, and SQLite.
* Tier overrides via env vars are rejected so the JWT license remains the
  only upgrade path on self-hosted.
* A mode nothing registered rules for refuses to start.

The cloud rules are tested in tests/cloud/test_mode_validation.py.
"""

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Env vars that should NEVER appear in a clean test run; clear them so the
# host environment cannot influence the result.
_DANGEROUS_VARS = (
    "LICENSE_KEY",
    "ACCOUNT_TYPE",
    "TIER",
    "PLAN",
    "FORCE_TIER",
    "OVERRIDE_TIER",
    "USE_MOCK_RESPONSES",
    "USE_STUBBED_MARKETPLACE_API",
    "DEBUG_MODE",
    "GITHUB_WEBHOOK_SECRET",
    "DATABASE_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ENVIRONMENT",
)


def _apply_env(monkeypatch, env: dict, mode: str = "self-hosted"):
    """Reset dangerous env vars then apply the test scenario."""
    # Set to empty (not delete) so config.py's load_dotenv() — which only
    # fills *unset* vars — cannot resurrect values from a local .env file
    # when we reload the config module below.
    for var in _DANGEROUS_VARS:
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("INSTALLATION_MODE", mode)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Reload config so config.INSTALLATION_MODE is up-to-date.
    import config
    importlib.reload(config)
    import mode_validation
    importlib.reload(mode_validation)
    return mode_validation


# --------------------------------------------------------------------------- #
# Self-hosted mode: permissive defaults
# --------------------------------------------------------------------------- #


class TestSelfHostedPermissiveRules:
    def test_self_hosted_minimal_env_passes(self, monkeypatch):
        # Empty env beyond INSTALLATION_MODE — SQLite, no license, no webhook.
        mv = _apply_env(monkeypatch, {}, mode="self-hosted")
        mv.validate_startup_configuration()

    def test_self_hosted_allows_license_key(self, monkeypatch):
        mv = _apply_env(
            monkeypatch, {"LICENSE_KEY": "any.jwt.token"}, mode="self-hosted"
        )
        mv.validate_startup_configuration()

    def test_self_hosted_does_not_require_github_webhook_secret(self, monkeypatch):
        mv = _apply_env(monkeypatch, {}, mode="self-hosted")
        mv.validate_startup_configuration()

    def test_self_hosted_allows_sqlite(self, monkeypatch):
        # No DATABASE_URL set — SQLite is the documented default.
        mv = _apply_env(monkeypatch, {}, mode="self-hosted")
        mv.validate_startup_configuration()

    @pytest.mark.parametrize(
        "var", ("ACCOUNT_TYPE", "TIER", "PLAN", "FORCE_TIER", "OVERRIDE_TIER")
    )
    def test_self_hosted_refuses_tier_override_env_vars(self, monkeypatch, var):
        # The JWT license is the ONLY upgrade path on self-hosted; env-var
        # tier overrides must be rejected too.
        mv = _apply_env(monkeypatch, {var: "enterprise"}, mode="self-hosted")
        with pytest.raises(mv.ModeValidationError) as exc:
            mv.validate_startup_configuration()
        assert any(var in v and "LICENSE_KEY" in v for v in exc.value.violations)


class TestUnservedModes:
    """main:app validates self-hosted only; an entrypoint that serves another
    mode registers that mode's rules (mode_validation.register_mode)."""

    def test_mode_without_registered_rules_refuses_to_start(self, monkeypatch):
        # Otherwise a cloud-mode process started from main:app would come up
        # with the self-hosted tier and sign-in rules.
        mv = _apply_env(monkeypatch, {}, mode="cloud")
        with pytest.raises(mv.ModeValidationError) as exc:
            mv.validate_startup_configuration()
        assert exc.value.violations == [
            "INSTALLATION_MODE='cloud' is not supported by this application. "
            "Use INSTALLATION_MODE=self-hosted."
        ]

    def test_main_app_exits_in_a_mode_without_registered_rules(self, monkeypatch):
        import asyncio
        import main

        _apply_env(monkeypatch, {}, mode="cloud")
        startup = main.startup_event()
        with pytest.raises(SystemExit) as exc:
            asyncio.run(startup)
        assert exc.value.code == 2

    def test_registered_mode_uses_its_rules(self, monkeypatch):
        mv = _apply_env(monkeypatch, {}, mode="self-hosted")
        mv.register_mode("cloud", lambda: ["from the entrypoint"])
        assert mv.get_violations("cloud") == ["from the entrypoint"]


# --------------------------------------------------------------------------- #
# ENVIRONMENT axis: production vs development strictness
# --------------------------------------------------------------------------- #


class TestEnvironmentAxis:
    """
    The ENVIRONMENT env var is orthogonal to INSTALLATION_MODE:

      * self-hosted + production → strict ops checks (no mocks/debug)
      * self-hosted + development → permissive (intended for local iteration)

    Tier-override env vars are rejected in every cell. The cloud cells are in
    tests/cloud/test_mode_validation.py.
    """

    # ---- self-hosted × production --------------------------------------- #

    def test_self_hosted_production_minimal_env_passes(self, monkeypatch):
        # SQLite + no admin creds + no webhook is fine in self-hosted prod.
        mv = _apply_env(monkeypatch, {"ENVIRONMENT": "production"}, mode="self-hosted")
        mv.validate_startup_configuration()

    def test_self_hosted_production_refuses_use_mock_responses(self, monkeypatch):
        mv = _apply_env(
            monkeypatch,
            {"ENVIRONMENT": "production", "USE_MOCK_RESPONSES": "true"},
            mode="self-hosted",
        )
        with pytest.raises(mv.ModeValidationError) as exc:
            mv.validate_startup_configuration()
        assert any("USE_MOCK_RESPONSES" in v for v in exc.value.violations)

    def test_self_hosted_production_refuses_debug_mode(self, monkeypatch):
        mv = _apply_env(
            monkeypatch,
            {"ENVIRONMENT": "production", "DEBUG_MODE": "true"},
            mode="self-hosted",
        )
        with pytest.raises(mv.ModeValidationError) as exc:
            mv.validate_startup_configuration()
        assert any("DEBUG_MODE" in v for v in exc.value.violations)

    def test_self_hosted_production_ignores_cloud_only_settings(self, monkeypatch):
        # Only the cloud image reads these. backend/.env.example once shipped
        # the admin placeholders, and copying it stopped production starts.
        mv = _apply_env(
            monkeypatch,
            {
                "ENVIRONMENT": "production",
                "USE_STUBBED_MARKETPLACE_API": "true",
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD": "change_this_password",
            },
            mode="self-hosted",
        )
        assert mv.get_violations() == []

    def test_self_hosted_production_does_not_require_postgres(self, monkeypatch):
        # Self-hosted prod legitimately runs on SQLite for small deployments.
        mv = _apply_env(monkeypatch, {"ENVIRONMENT": "production"}, mode="self-hosted")
        mv.validate_startup_configuration()

    def test_self_hosted_production_does_not_require_github_webhook_secret(
        self, monkeypatch
    ):
        # Marketplace doesn't apply to self-hosted; webhook secret is optional.
        mv = _apply_env(monkeypatch, {"ENVIRONMENT": "production"}, mode="self-hosted")
        mv.validate_startup_configuration()

    @pytest.mark.parametrize(
        "var", ("ACCOUNT_TYPE", "TIER", "PLAN", "FORCE_TIER", "OVERRIDE_TIER")
    )
    def test_self_hosted_production_refuses_tier_override_env_vars(
        self, monkeypatch, var
    ):
        mv = _apply_env(
            monkeypatch,
            {"ENVIRONMENT": "production", var: "enterprise"},
            mode="self-hosted",
        )
        with pytest.raises(mv.ModeValidationError) as exc:
            mv.validate_startup_configuration()
        assert any(var in v for v in exc.value.violations)

    # ---- self-hosted × development -------------------------------------- #

    def test_self_hosted_development_allows_mocks_debug_and_default_admin(
        self, monkeypatch
    ):
        mv = _apply_env(
            monkeypatch,
            {
                "ENVIRONMENT": "development",
                "USE_MOCK_RESPONSES": "true",
                "USE_STUBBED_MARKETPLACE_API": "true",
                "DEBUG_MODE": "true",
                "ADMIN_USERNAME": "admin",
                "ADMIN_PASSWORD": "change_this_password",
            },
            mode="self-hosted",
        )
        mv.validate_startup_configuration()

    @pytest.mark.parametrize(
        "var", ("ACCOUNT_TYPE", "TIER", "PLAN", "FORCE_TIER", "OVERRIDE_TIER")
    )
    def test_self_hosted_development_still_refuses_tier_overrides(
        self, monkeypatch, var
    ):
        # The one invariant across every cell: env-var tier overrides are
        # always rejected, no exceptions.
        mv = _apply_env(
            monkeypatch,
            {"ENVIRONMENT": "development", var: "enterprise"},
            mode="self-hosted",
        )
        with pytest.raises(mv.ModeValidationError) as exc:
            mv.validate_startup_configuration()
        assert any(var in v for v in exc.value.violations)

    def test_self_hosted_unknown_environment_emits_violation(self, monkeypatch):
        mv = _apply_env(
            monkeypatch, {"ENVIRONMENT": "qa"}, mode="self-hosted"
        )
        with pytest.raises(mv.ModeValidationError) as exc:
            mv.validate_startup_configuration()
        assert any("Unknown ENVIRONMENT" in v for v in exc.value.violations)

    def test_self_hosted_unknown_environment_falls_back_to_production_strictness(
        self, monkeypatch
    ):
        # Unknown values default to production semantics so misconfiguration
        # fails closed. DEBUG_MODE=true should still be rejected even when
        # the operator typo'd the ENVIRONMENT value.
        mv = _apply_env(
            monkeypatch,
            {"ENVIRONMENT": "qa", "DEBUG_MODE": "true"},
            mode="self-hosted",
        )
        violations = mv.get_violations()
        assert any("Unknown ENVIRONMENT" in v for v in violations)
        assert any("DEBUG_MODE" in v for v in violations)

    # ---- get_environment helper ----------------------------------------- #

    def test_get_environment_defaults_to_production(self, monkeypatch):
        mv = _apply_env(monkeypatch, {}, mode="self-hosted")
        assert mv.get_environment() == "production"

    def test_get_environment_accepts_development(self, monkeypatch):
        mv = _apply_env(
            monkeypatch, {"ENVIRONMENT": "development"}, mode="self-hosted"
        )
        assert mv.get_environment() == "development"

    def test_get_environment_is_case_insensitive(self, monkeypatch):
        mv = _apply_env(
            monkeypatch, {"ENVIRONMENT": "Development"}, mode="self-hosted"
        )
        assert mv.get_environment() == "development"

    def test_get_environment_falls_back_to_production_for_unknown_value(
        self, monkeypatch
    ):
        # Helper itself never raises; fail-closed by returning production.
        mv = _apply_env(
            monkeypatch, {"ENVIRONMENT": "qa"}, mode="self-hosted"
        )
        assert mv.get_environment() == "production"
