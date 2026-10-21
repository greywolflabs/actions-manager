"""
Database Configuration Module

Handles database connection and session management for ActionsManager.
Supports both PostgreSQL (production) and SQLite (development/self-hosted).
"""

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote_plus
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Define Base for SQLAlchemy models
Base = declarative_base()


@event.listens_for(Engine, "connect")
def _sqlite_enable_foreign_keys(dbapi_connection, connection_record):
    """Turn on SQLite foreign key enforcement for every connection (issue #1811).

    SQLite defaults `foreign_keys` to OFF, which made all 36 ON DELETE CASCADE
    declarations in models.py silent no-ops — deleting a parent left orphaned
    children behind with no error. PostgreSQL enforces natively and is skipped.

    Registered against the Engine *class*, not a single engine instance, so it
    covers every engine in the process — including the ones the test suite
    builds itself — and so it still resolves when database.py is imported with
    create_engine patched out (see tests/test_database_persistence.py).

    The pragma is per-connection, hence "connect" rather than a one-off call.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")


def _postgresql_session_in_utc(dbapi_connection, connection_record):
    """Run a PostgreSQL session in UTC (issue #2350).

    The models' DateTime columns are TIMESTAMP WITHOUT TIME ZONE, and the app writes
    timezone-aware UTC values and now(). PostgreSQL converts both to the
    session's TimeZone before storing them, and a session inherits the
    server's or database's setting. On a server that isn't on UTC, stored
    times were local wall-clock time, while everything that reads them treats
    them as UTC. Drift check times, session expiry and rate-limit resets were
    all off by the server's offset.

    A SET rather than a startup option, because PgBouncer re-applies a
    session's TimeZone but rejects the options parameter. Committed, so a
    later rollback cannot undo it.
    """
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'UTC'")
    dbapi_connection.commit()


# Load PostgreSQL connection details
POSTGRES_USER = os.getenv("POSTGRES_USER", "").strip()
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "").strip()
POSTGRES_DB = os.getenv("POSTGRES_DB", "").strip()
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "").strip()
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "").strip()

# An explicit DATABASE_URL env var always wins over all other settings.
_explicit_db_url = os.getenv("DATABASE_URL", "").strip()

# The canonical persistent data directory for self-hosted deployments.
# All self-hosted SQLite databases and runtime-generated files live here.
SELF_HOSTED_DATA_DIR = Path("/app/data")

# Legacy path used by earlier releases; kept for migration detection only.
_LEGACY_SQLITE_PATH = Path("/app/backend/test.db")


def _resolve_sqlite_url() -> str:
    """
    Return the SQLite DATABASE_URL for the current environment.

    Priority:
    1. INSTALLATION_MODE=self-hosted  → /app/data/actions_manager.db
    2. Otherwise (development/tests)  → ./test.db (relative to CWD)
    """
    installation_mode = os.getenv("INSTALLATION_MODE", "").strip().lower()

    if installation_mode == "self-hosted":
        db_path = SELF_HOSTED_DATA_DIR / "actions_manager.db"
        # Ensure the data directory exists so SQLite can create the file.
        try:
            SELF_HOSTED_DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Non-fatal during import; the directory may already exist or the
            # filesystem may be read-only during unit tests.
            pass
        return f"sqlite:///{db_path}"

    # Development / test fallback
    return "sqlite:///./test.db"


if _explicit_db_url:
    DATABASE_URL = _explicit_db_url
    _db_source = "DATABASE_URL env var"
    # Determine a safe log label for the explicit URL (no credentials).
    _db_log_label = "explicit DATABASE_URL (SQLite)" if "sqlite" in _explicit_db_url.lower() else "explicit DATABASE_URL (PostgreSQL)"
elif all([POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, POSTGRES_HOST, POSTGRES_PORT]):
    safe_password = quote_plus(POSTGRES_PASSWORD)
    DATABASE_URL = (
        f"postgresql://{POSTGRES_USER}:{safe_password}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    )
    _db_source = "PostgreSQL env vars"
    _db_log_label = "PostgreSQL"
else:
    DATABASE_URL = _resolve_sqlite_url()
    _db_source = "SQLite (auto-resolved)"
    # The auto-resolved URL is always a local SQLite path; build the log
    # label from the known, non-sensitive path constant rather than from
    # DATABASE_URL so static-analysis tools do not flag it as credential logging.
    _installation_mode = os.getenv("INSTALLATION_MODE", "").strip().lower()
    if _installation_mode == "self-hosted":
        _db_log_label = f"sqlite (self-hosted): {SELF_HOSTED_DATA_DIR / 'actions_manager.db'}"
    else:
        _db_log_label = "sqlite (development): ./test.db"

# ---------------------------------------------------------------------------
# Log the active database path so operators can confirm persistence is wired
# correctly. Credentials are never included in this output.
# ---------------------------------------------------------------------------
print(f"📂 Database: {_db_log_label} [{_db_source}]")

# Warn if the legacy test.db exists but the active database is elsewhere.
# This helps operators who upgrade from an older release notice that their
# data lives in the old location and may need to be moved.
# Only emit the warning for auto-resolved self-hosted SQLite (not for explicit
# DATABASE_URL overrides where the operator has explicitly chosen a path).
if (
    not _explicit_db_url
    and "sqlite" in DATABASE_URL.lower()
    and _LEGACY_SQLITE_PATH.exists()
    and str(_LEGACY_SQLITE_PATH) not in DATABASE_URL
):
    _new_db_path = SELF_HOSTED_DATA_DIR / "actions_manager.db"
    print(
        f"⚠️  Legacy database detected at {_LEGACY_SQLITE_PATH}. "
        f"The active database is {_new_db_path}. "
        "If you have existing data in the legacy path, copy it to the new "
        "location before starting, or set DATABASE_URL=sqlite:////app/backend/test.db "
        "to keep using the legacy path."
    )

# ---------------------------------------------------------------------------
# Build the SQLAlchemy engine
# ---------------------------------------------------------------------------
if "sqlite" in DATABASE_URL.lower():
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},  # Required for SQLite
        echo=False,
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        pool_pre_ping=True,
        echo=False,
    )

if engine.dialect.name == "postgresql":
    event.listen(engine, "connect", _postgresql_session_in_utc)

# Setup session maker
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Create tables if they don't exist
Base.metadata.create_all(engine)


def serialize_writers(db, lock_id: int) -> None:
    """Let writers that pass the same *lock_id* through one at a time, until this transaction ends.

    For a check-then-write that two requests must never both pass: whoever
    holds the lock reads what the previous holder committed. PostgreSQL takes a
    transaction-scoped advisory lock on *lock_id*. SQLite has no advisory locks
    but runs one writer at a time, so a write that changes nothing takes that
    lock now rather than at the transaction's first real write; it holds back
    every writer, whatever its lock_id.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
    else:
        db.execute(text("UPDATE repos SET repo_id = repo_id WHERE 1 = 0"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()