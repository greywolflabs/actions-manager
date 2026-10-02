"""
Strict runtime mode validation for ActionsManager.

This module is the single source of truth for deployment-mode-specific
configuration rules. It is invoked at application startup and **fails closed**
when a deployment is configured in a way that could allow tier enforcement to
be bypassed, or that is unsafe to run in production.

There are two orthogonal axes:

* ``INSTALLATION_MODE`` — which rules apply. This application validates
  ``self-hosted`` (tier comes from a signed JWT ``LICENSE_KEY``). Another
  entrypoint that serves a different mode registers its rules with
  :func:`register_mode`; any other mode refuses to start.
* ``ENVIRONMENT`` — *operational strictness*. Either ``production`` (default,
  fail-closed on unsafe ops settings) or ``development`` (permissive, intended
  for local iteration).

Self-hosted production (``INSTALLATION_MODE=self-hosted``,
``ENVIRONMENT=production``) rules — refuse to start when:

* ``USE_MOCK_RESPONSES=true``.
* ``DEBUG_MODE=true``.
* Tier-override env vars are set.

Self-hosted production deliberately does *not* require PostgreSQL: small
single-tenant deployments may legitimately use SQLite.

Self-hosted development (``INSTALLATION_MODE=self-hosted``,
``ENVIRONMENT=development``) rules — permissive:

* ``LICENSE_KEY`` is allowed.
* SQLite is allowed (no ``DATABASE_URL`` required).
* Mocks and debug mode are tolerated.
* Tier-override env vars are still rejected — the JWT license is the only
  way to upgrade a self-hosted installation, never an env var.

Use :func:`validate_startup_configuration` from ``main.py``. The function
raises :class:`ModeValidationError` on failure, listing every violation so
operators can fix the deployment in one pass.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

import config


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Env vars that must NEVER set the effective tier directly: the JWT license is
# the only valid upgrade path.
TIER_OVERRIDE_ENV_VARS = (
    "ACCOUNT_TYPE",
    "TIER",
    "PLAN",
    "FORCE_TIER",
    "OVERRIDE_TIER",
)

# Operational strictness axis. Production is fail-closed; development is
# permissive.
VALID_ENVIRONMENTS = {"production", "development"}
DEFAULT_ENVIRONMENT = "production"


def get_environment() -> str:
    """
    Resolve the operational environment from the ``ENVIRONMENT`` env var.

    Defaults to ``production`` when unset, empty, or set to an unknown value
    so that misconfiguration always fails closed. The validators emit a
    separate violation describing the unknown value via
    :func:`_collect_environment_violations`.
    """
    raw = os.environ.get("ENVIRONMENT", "").strip().lower()
    if raw in VALID_ENVIRONMENTS:
        return raw
    return DEFAULT_ENVIRONMENT


def _collect_environment_violations() -> Iterable[str]:
    """Yield a violation when ``ENVIRONMENT`` is set to an unknown value."""
    raw = os.environ.get("ENVIRONMENT", "").strip().lower()
    if raw and raw not in VALID_ENVIRONMENTS:
        yield (
            f"Unknown ENVIRONMENT={raw!r}. "
            f"Must be one of: {', '.join(sorted(VALID_ENVIRONMENTS))}."
        )


class ModeValidationError(RuntimeError):
    """Raised when startup configuration violates the active mode's rules."""

    def __init__(self, mode: str, violations: List[str]):
        self.mode = mode
        self.violations = list(violations)
        bullets = "\n".join(f"  - {v}" for v in self.violations)
        super().__init__(
            f"Refusing to start: INSTALLATION_MODE={mode!r} configuration "
            f"violations:\n{bullets}"
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _env(name: str) -> str:
    """Return a stripped environment variable, never ``None``."""
    return os.environ.get(name, "").strip()


def _is_truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _collect_tier_override_violations() -> Iterable[str]:
    for var in TIER_OVERRIDE_ENV_VARS:
        if _env(var):
            yield (
                f"{var} is set. Tier overrides via environment variables are "
                "not allowed; the tier comes from LICENSE_KEY."
            )


# --------------------------------------------------------------------------- #
# APP_URL resolution and validation
# --------------------------------------------------------------------------- #

_LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}


def resolve_app_url() -> str:
    """
    Return the effective APP_URL after the priority chain.

    Priority: APP_URL > VITE_APP_URL > default ``http://localhost:8080``.
    Trailing slashes are stripped.
    """
    url = _env("APP_URL") or _env("VITE_APP_URL")
    if not url:
        return "http://localhost:8080"
    return url.rstrip("/")


def _host_is_loopback(hostname: str) -> bool:
    """Return True when *hostname* refers to the local machine."""
    if not hostname:
        return False
    # Strip brackets from IPv6 (e.g. "[::1]")
    bare = hostname.strip("[]").lower()
    return bare in _LOCALHOST_NAMES


def _collect_app_url_violations() -> Iterable[str]:
    """
    Validate the resolved APP_URL scheme and HTTP-security policy.

    Yields violations (never raises directly) so that all problems can be
    collected in a single startup pass.
    """
    url = resolve_app_url()
    parsed = urlsplit(url)

    if parsed.scheme not in ("http", "https"):
        yield (
            f"APP_URL={url!r} uses unsupported scheme {parsed.scheme!r}. "
            f"Only http:// and https:// are allowed."
        )
        return

    if parsed.scheme == "https":
        return

    # scheme == "http" — allowed for loopback without opt-in
    hostname = parsed.hostname or ""
    if _host_is_loopback(hostname):
        return

    # Non-loopback HTTP — require ALLOW_INSECURE_HTTP=true
    if not _is_truthy(_env("ALLOW_INSECURE_HTTP")):
        yield (
            f"APP_URL={url!r} is non-local plain HTTP. This is insecure — "
            f"tokens and credentials would be transmitted in cleartext. "
            f"Either switch to https:// or set ALLOW_INSECURE_HTTP=true "
            f"to allow it."
        )


def _validate_self_hosted() -> List[str]:
    """Return the list of self-hosted-mode violations (empty when valid)."""
    violations: List[str] = []
    # Tier-overrides are forbidden in every environment because the JWT
    # license is the only valid upgrade path on self-hosted.
    violations.extend(_collect_tier_override_violations())
    # Surface a clear violation when ENVIRONMENT is set to an unknown value.
    violations.extend(_collect_environment_violations())

    # APP_URL scheme / insecure-HTTP validation applies in every environment.
    violations.extend(_collect_app_url_violations())

    environment = get_environment()
    if environment == "production":
        if _is_truthy(_env("USE_MOCK_RESPONSES")):
            violations.append(
                "USE_MOCK_RESPONSES=true is not permitted with "
                "ENVIRONMENT=production. Set ENVIRONMENT=development for local "
                "iteration."
            )
        if _is_truthy(_env("DEBUG_MODE")):
            violations.append(
                "DEBUG_MODE=true is not permitted with ENVIRONMENT=production. "
                "Set DEBUG_MODE=false or ENVIRONMENT=development."
            )

    return violations


_VALIDATORS: Dict[str, Callable[[], List[str]]] = {"self-hosted": _validate_self_hosted}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def register_mode(mode: str, validator: Callable[[], List[str]]) -> None:
    """Validate ``INSTALLATION_MODE=<mode>`` with *validator* (an entrypoint that serves it)."""
    _VALIDATORS[mode] = validator


def get_violations(mode: Optional[str] = None) -> List[str]:
    """
    Return the list of configuration violations for the given mode (defaults
    to the active installation mode read from the environment). Empty list
    means valid.
    """
    if mode is None:
        # Re-read from the environment rather than relying on the cached
        # ``config.INSTALLATION_MODE`` module constant. This keeps the
        # validator robust against test pollution and against module-load
        # order issues at startup.
        active_mode = config.get_installation_mode()
    else:
        active_mode = mode.lower()
    validator = _VALIDATORS.get(active_mode)
    if validator is None:
        # Fail closed: a mode nothing registered rules for would otherwise
        # run with the self-hosted tier and sign-in rules.
        return [
            f"INSTALLATION_MODE={active_mode!r} is not supported by this "
            f"application. Use INSTALLATION_MODE=self-hosted."
        ]
    return validator()


def validate_startup_configuration(mode: Optional[str] = None) -> None:
    """
    Validate the active deployment mode's configuration.

    Raises :class:`ModeValidationError` listing every violation when the
    configuration is invalid. Returns silently when valid.
    """
    if mode is None:
        active_mode = config.get_installation_mode()
    else:
        active_mode = mode.lower()
    violations = get_violations(active_mode)
    if violations:
        raise ModeValidationError(active_mode, violations)
