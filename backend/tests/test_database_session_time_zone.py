"""
Every PostgreSQL session runs in UTC (#2350).

The models' DateTime columns are TIMESTAMP WITHOUT TIME ZONE, and the app writes
timezone-aware UTC values and now(). PostgreSQL converts both to the session's
TimeZone before storing them, so a server that isn't on UTC stored local
wall-clock time that every reader takes for UTC. CI runs on SQLite, so the
listener runs against a stand-in DBAPI connection, and its wiring is checked
on real, unconnected engines.
"""
import importlib
import os
import sys
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database  # noqa: E402


class _Connection:
    def __init__(self, fail_with=None):
        self.fail_with = fail_with
        self.calls = []

    def cursor(self):
        connection = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                connection.calls.append("close")
                return False

            def execute(self, statement):
                if connection.fail_with:
                    raise connection.fail_with
                connection.calls.append(statement)

        return _Cursor()

    def commit(self):
        self.calls.append("commit")


def test_the_session_is_set_to_utc_and_committed():
    """Committed, so a later rollback cannot undo it."""
    connection = _Connection()

    database._postgresql_session_in_utc(connection, None)

    assert connection.calls == ["SET TIME ZONE 'UTC'", "close", "commit"]


def test_a_failure_is_reported_as_it_happened():
    lost = ConnectionError("server closed the connection unexpectedly")
    connection = _Connection(fail_with=lost)

    with pytest.raises(ConnectionError) as raised:
        database._postgresql_session_in_utc(connection, None)

    assert raised.value is lost
    assert connection.calls == ["close"]


@pytest.fixture
def database_for():
    """database.py imported afresh for a DATABASE_URL alone; the original module is put back,
    and the class-level listener each import adds is taken off again."""
    original = sys.modules.get("database")
    loaded = []

    def load(url):
        sys.modules.pop("database", None)
        with patch.dict(os.environ, {"DATABASE_URL": url}, clear=True), patch("sqlalchemy.MetaData.create_all"):
            module = importlib.import_module("database")
        loaded.append(module)
        return module

    yield load
    for module in loaded:
        event.remove(Engine, "connect", module._sqlite_enable_foreign_keys)
        module.engine.dispose()
    if original is not None:
        sys.modules["database"] = original


def test_the_postgresql_engine_runs_its_sessions_in_utc(database_for):
    module = database_for("postgresql+psycopg2://user:secret@db.example.com:5432/actions")

    assert event.contains(module.engine, "connect", module._postgresql_session_in_utc)


def test_the_sqlite_engine_is_left_alone(database_for):
    """SQLite has no SET TIME ZONE: running it would fail every connection."""
    module = database_for("sqlite://")

    assert not event.contains(module.engine, "connect", module._postgresql_session_in_utc)
