"""
The sweep is the only thing that checks drift without a human clicking.

Before it, drift state changed on exactly three user-initiated requests, so a
project could show "in sync" indefinitely while GitHub had drifted. These tests
are about *which projects get picked and when* — the drift comparison itself is
covered by the ~170 existing drift tests and is mocked out here.

Two failure modes matter more than the rest, both of which would leave the
sweep silently doing nothing:

  * a project whose owner has no token must not advance its own cursor (that
    would fake a check) *and* must not starve the projects behind it.
  * one project raising must not stop the sweep for everyone else.
"""

import contextlib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import Base, Account, DriftSettings, Project  # noqa: E402
import drift_worker  # noqa: E402
from drift_worker import sweep_projects_for_drift  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

STALE = datetime.now(timezone.utc) - timedelta(hours=2)
FRESH = datetime.now(timezone.utc)


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _owner(db, name="alice"):
    user = Account(github_user=name, github_email=f"{name}@e.com", account_type="free")
    db.add(user); db.commit(); db.refresh(user)
    return user


def _project(db, owner, code, last_checked=STALE, failure_count=0, interval=None):
    project = Project(project_name=f"proj-{code}", project_code=code, user_id=owner.user_id,
                      use_prefix=False, branch_option="default",
                      last_drift_check_at=last_checked,
                      drift_check_failure_count=failure_count,
                      drift_check_interval_minutes=interval)
    db.add(project); db.commit(); db.refresh(project)
    return project


def _settings(db, **overrides):
    """Save the single global settings row. Config lives in the DB, not the
    environment, so tests set it the same way the GUI does."""
    values = {"sweep_enabled": True, "recheck_interval_minutes": 15,
              "batch_size": 5, "poll_interval_seconds": 60}
    values.update(overrides)
    settings = DriftSettings(**values)
    db.add(settings); db.commit()
    return settings


@contextlib.contextmanager
def _statements(kind=""):
    """Collect the statements of ``kind`` (a leading keyword) run while the block executes."""
    executed = []

    def record(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(kind):
            executed.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield executed
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _updates():
    return _statements("UPDATE")


@contextlib.contextmanager
def _project_reads():
    """Collect each read of projects run while the block executes, with its parameters."""
    reads = []

    def record(_conn, _cursor, statement, parameters, *_args):
        if "FROM projects" in statement:
            reads.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield reads
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _query_plan(db, statement, parameters):
    if engine.dialect.name != "sqlite":
        pytest.skip("EXPLAIN QUERY PLAN is SQLite's")
    return [row[-1] for row in db.connection().exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)]


def _use_default(db, minutes):
    """Save the workspace default, as PUT /api/drift/settings does."""
    settings = db.query(DriftSettings).order_by(DriftSettings.settings_id).first()
    if settings is None:
        _settings(db, recheck_interval_minutes=minutes)
    else:
        settings.recheck_interval_minutes = minutes
        db.commit()


def _tokens(mapping):
    """Patch the credential store the worker resolves owners through."""
    return patch("auth.user_tokens.get", side_effect=lambda u, d=None: mapping.get(u, d))


class TestOnlyStaleProjectsAreChecked:
    def test_a_stale_project_is_checked(self, db):
        _project(db, _owner(db), "P1")

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", return_value=([], [])) as check:
            assert sweep_projects_for_drift(db) == 1

        assert check.call_count == 1

    def test_a_recently_checked_project_is_skipped(self, db):
        _project(db, _owner(db), "P1", last_checked=FRESH)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0

    def test_never_checked_goes_first(self, db):
        owner = _owner(db)
        _project(db, owner, "OLD", last_checked=STALE)
        _project(db, owner, "NEW", last_checked=None)
        _settings(db, batch_size=1)

        seen = []
        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            sweep_projects_for_drift(db)

        assert seen == ["NEW"]


class TestBatching:
    def test_the_batch_caps_how_many_are_checked(self, db):
        owner = _owner(db)
        for i in range(5):
            _project(db, owner, f"P{i}")
        _settings(db, batch_size=2)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", return_value=([], [])) as check:
            assert sweep_projects_for_drift(db) == 2

        assert check.call_count == 2


class TestAMissingTokenDoesNotBreakTheSweep:
    def test_no_token_means_no_check_and_no_fake_timestamp(self, db):
        """Marking it checked would be the stale-'clean' bug this feature exists
        to prevent."""
        project = _project(db, _owner(db), "P1")

        with _tokens({}), patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0
        db.refresh(project)
        # SQLite drops tzinfo on round-trip, so compare naive to naive.
        assert project.last_drift_check_at.replace(tzinfo=None) == STALE.replace(tzinfo=None)

    def test_the_skip_is_explained_rather_than_silent(self, db):
        """A timestamp that just stops moving reads as a broken feature. The
        reason has to be recorded, without pretending a check happened."""
        project = _project(db, _owner(db), "P1")

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_error_summary == drift_worker.NO_CREDENTIAL_REASON
        assert project.last_drift_check_at.replace(tzinfo=None) == STALE.replace(tzinfo=None)

    def test_a_previous_real_result_is_not_overwritten(self, db):
        """A project that genuinely was drifting still is — a skip must not
        downgrade that to 'unknown' and hide a true positive."""
        project = _project(db, _owner(db), "P1")
        project.drift_status = "drifted"
        project.drift_count = 3
        db.commit()

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_status == "drifted"
        assert project.drift_count == 3

    def test_the_reason_is_written_once_not_every_tick(self, db):
        _project(db, _owner(db), "P1")

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)
            with _updates() as executed:
                sweep_projects_for_drift(db)

        assert executed == []

    def test_a_successful_check_clears_the_reason(self, db):
        """Self-healing: once the owner saves a token the warning must go away
        on its own, not linger and mislead."""
        project = _project(db, _owner(db), "P1")
        project.drift_error_summary = drift_worker.NO_CREDENTIAL_REASON
        db.commit()

        # A real check clears it via _cache_project_drift_summary.
        from workflows import _cache_project_drift_summary
        _cache_project_drift_summary(db, project, "clean", 0, None)

        db.refresh(project)
        assert project.drift_error_summary is None

    def test_a_tokenless_project_does_not_starve_the_others(self, db):
        """It keeps its old cursor, so it would stay at the head of a queue
        ordered by cursor. Only checkable owners' projects are queued."""
        broke = _owner(db, "broke")
        fine = _owner(db, "fine")
        # The tokenless project sorts first (oldest cursor).
        _project(db, broke, "STARVER", last_checked=STALE - timedelta(days=1))
        _project(db, fine, "WANTED", last_checked=STALE)
        _settings(db, batch_size=1)

        seen = []
        with _tokens({"fine": "tok"}), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            assert sweep_projects_for_drift(db) == 1

        assert seen == ["WANTED"]


class TestOneBadProjectDoesNotStopTheRest:
    def test_an_exception_is_contained(self, db):
        owner = _owner(db)
        _project(db, owner, "BAD", last_checked=STALE - timedelta(days=1))
        _project(db, owner, "GOOD", last_checked=STALE)

        def flaky(_db, _user, project):
            if project.project_code == "BAD":
                raise RuntimeError("GitHub exploded")
            return ([], [])

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", side_effect=flaky):
            assert sweep_projects_for_drift(db) == 1


class TestBackoffMultiplier:
    """Pure function - covering the shape directly is cheaper than round-tripping
    through the DB for every case. notification_worker.py's _backoff_delay uses
    the same 2**(attempt-1) shape; this mirrors it."""

    def test_no_failures_is_1x(self):
        assert drift_worker._backoff_multiplier(0) == 1

    def test_a_single_failure_does_not_yet_back_off(self):
        assert drift_worker._backoff_multiplier(1) == 1

    def test_doubles_from_the_second_failure(self):
        assert drift_worker._backoff_multiplier(2) == 2
        assert drift_worker._backoff_multiplier(3) == 4
        assert drift_worker._backoff_multiplier(4) == 8

    def test_caps_rather_than_growing_unbounded(self):
        assert drift_worker._backoff_multiplier(6) == drift_worker.BACKOFF_MAX_MULTIPLIER
        assert drift_worker._backoff_multiplier(100) == drift_worker.BACKOFF_MAX_MULTIPLIER


class TestBackoffInTheSweep:
    def test_a_single_failure_is_still_checked_at_the_normal_interval(self, db):
        """Matches TestBackoffMultiplier: the first failure doesn't back off yet."""
        _project(db, _owner(db), "P1", last_checked=STALE, failure_count=1)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", return_value=([], [])) as check:
            assert sweep_projects_for_drift(db) == 1

        assert check.call_count == 1

    def test_a_repeatedly_failing_project_is_skipped_before_its_backoff_window_elapses(self, db):
        """5 failures -> 16x the 15-minute default = 4h. Checked 2h ago (STALE)
        is not due yet, even though it clears the un-backed-off base cutoff."""
        _project(db, _owner(db), "P1", last_checked=STALE, failure_count=5)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0

    def test_a_repeatedly_failing_project_is_checked_once_its_backoff_window_elapses(self, db):
        """5 failures -> 4h backoff window; checked 5h ago clears it."""
        _project(db, _owner(db), "P1", last_checked=STALE - timedelta(hours=3), failure_count=5)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", return_value=([], [])) as check:
            assert sweep_projects_for_drift(db) == 1

        assert check.call_count == 1

    def test_a_backed_off_project_does_not_starve_the_others(self, db):
        """Same non-starvation guarantee as the tokenless case, for backoff.

        STARVER sorts first (older cursor) but 10 failures caps its backoff at
        8h, and it was only checked 3h ago - not due. WANTED, checked 2h ago
        with no failures, is due at the plain 15-minute interval and must
        still be reached despite sorting behind STARVER.
        """
        owner = _owner(db)
        _project(db, owner, "STARVER", last_checked=STALE - timedelta(hours=1), failure_count=10)
        _project(db, owner, "WANTED", last_checked=STALE)
        _settings(db, batch_size=1)

        seen = []
        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            assert sweep_projects_for_drift(db) == 1

        assert seen == ["WANTED"]


class TestDriftCheckFailureCountBookkeeping:
    """_cache_project_drift_summary is what backoff is computed from, so its
    increment/reset behavior is covered directly here alongside the sweep
    tests that depend on it."""

    def test_check_failed_increments_the_counter(self, db):
        from workflows import _cache_project_drift_summary
        project = _project(db, _owner(db), "P1", failure_count=2)

        _cache_project_drift_summary(db, project, "check_failed", 0, "boom")

        db.refresh(project)
        assert project.drift_check_failure_count == 3

    def test_clean_resets_the_counter(self, db):
        from workflows import _cache_project_drift_summary
        project = _project(db, _owner(db), "P1", failure_count=4)

        _cache_project_drift_summary(db, project, "clean", 0, None)

        db.refresh(project)
        assert project.drift_check_failure_count == 0

    def test_drifted_resets_the_counter(self, db):
        """Finding real drift is a successful check, not a failure."""
        from workflows import _cache_project_drift_summary
        project = _project(db, _owner(db), "P1", failure_count=4)

        _cache_project_drift_summary(db, project, "drifted", 2, None)

        db.refresh(project)
        assert project.drift_check_failure_count == 0


class TestEveryWriteReschedulesTheNextCheck:
    """The sweep reads only next_drift_check_at (#2343). A write to one of its
    inputs that left it alone would keep the project on its old schedule with
    nothing visibly wrong, so each write path is pinned here."""

    @staticmethod
    def _gap(project):
        return project.next_drift_check_at - project.last_drift_check_at

    @pytest.mark.parametrize("status, multiplier", [("check_failed", 4), ("clean", 1), ("drifted", 1)])
    def test_a_check_result(self, db, status, multiplier):
        from workflows import _cache_project_drift_summary
        project = _project(db, _owner(db), "P1", failure_count=2)

        _cache_project_drift_summary(db, project, status, 0, None)

        db.refresh(project)
        assert self._gap(project) == timedelta(minutes=15 * multiplier)

    def test_a_resolved_drift(self, db):
        """recompute_project_drift_summary writes the columns itself rather
        than going through _cache_project_drift_summary."""
        from drift_notifications import recompute_project_drift_summary
        project = _project(db, _owner(db), "P1", failure_count=5)

        recompute_project_drift_summary(db, project)

        db.refresh(project)
        assert self._gap(project) == timedelta(minutes=15)

    def test_a_recorded_pause_returns_the_project_to_its_normal_schedule(self, db):
        project = _project(db, _owner(db), "P1", failure_count=6,
                           last_checked=datetime.now(timezone.utc) - timedelta(hours=1))

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert self._gap(project) == timedelta(minutes=15)


class TestReconcilingSchedules:
    """Each tick first recomputes every stored time it can't trust, from the
    committed row, before selecting anything (#2343)."""

    def _reconcile(self, db, default_minutes=None):
        if default_minutes is not None:
            _use_default(db, default_minutes)
        return drift_worker.reconcile_schedules(db)

    def test_a_new_default_reaches_inheriting_projects_before_anything_is_selected(self, db):
        project = _project(db, _owner(db), "P1", last_checked=STALE)
        self._reconcile(db)
        _settings(db, recheck_interval_minutes=1440)

        with _tokens({"alice": "tok"}), patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0
        db.refresh(project)
        assert project.next_drift_check_at - project.last_drift_check_at == timedelta(minutes=1440)
        assert project.drift_check_scheduled_minutes == 1440

    def test_a_new_default_leaves_projects_with_their_own_interval_alone(self, db):
        owner = _owner(db)
        _project(db, owner, "INHERITS")
        _project(db, owner, "PINNED", interval=1440)
        self._reconcile(db)

        assert self._reconcile(db, 60) == 1

    def test_nothing_is_written_once_every_time_is_trusted(self, db):
        _project(db, _owner(db), "P1")
        self._reconcile(db)

        with _updates() as executed:
            assert self._reconcile(db) == 0

        assert executed == []

    def test_every_untrusted_time_is_written_in_one_statement(self, db):
        owner = _owner(db)
        for code in ("A", "B", "C"):
            _project(db, owner, code)

        with _updates() as executed:
            assert self._reconcile(db, 60) == 3

        assert len(executed) == 1

    def test_a_large_reconcile_is_written_a_chunk_per_transaction(self, db):
        """A new default moves every inheriting project at once; one transaction
        would hold all of their rows, and every check of them would wait."""
        owner = _owner(db)
        projects = [_project(db, owner, f"P{i}") for i in range(5)]
        _settings(db, recheck_interval_minutes=60)

        with patch.object(drift_worker, "WRITE_CHUNK", 2), _updates() as executed, \
             patch.object(drift_worker.Session, "commit", autospec=True, side_effect=Session.commit) as commit:
            drift_worker._reconcile(db)

        assert len(executed) == 3
        assert commit.call_count == 3
        for project in projects:
            db.refresh(project)
            assert project.drift_check_scheduled_minutes == 60

    def test_the_count_is_of_rows_actually_written(self, db):
        """A compare-and-set that a concurrent write beat is not reported as done."""
        _project(db, _owner(db), "P1")
        _settings(db, recheck_interval_minutes=60)

        real_next_check_at = drift_worker.next_check_at

        def a_check_lands_first(*args):
            db.execute(Project.__table__.update().values(drift_check_failure_count=7))
            return real_next_check_at(*args)

        with patch.object(drift_worker, "next_check_at", side_effect=a_check_lands_first):
            written = drift_worker.reconcile_schedules(db)

        assert written == 0

    def test_a_row_scheduled_with_a_default_saved_mid_reconcile_is_not_moved_back(self, db):
        """The rows were picked with the default as it stood (15), but a check
        then scheduled this one with a default saved meanwhile (1440). The
        default is read again after the rows, so it is left as the check wrote it."""
        project = _project(db, _owner(db), "P1")
        _use_default(db, 1440)
        project.drift_check_failure_count = 1
        db.commit()
        db.refresh(project)
        scheduled_at = project.next_drift_check_at

        with patch.object(drift_worker, "saved_default_minutes", side_effect=[15, 1440]):
            written = drift_worker.reconcile_schedules(db)

        assert written == 0
        db.refresh(project)
        assert project.next_drift_check_at == scheduled_at
        assert project.drift_check_scheduled_minutes == 1440

    def test_a_last_check_stored_in_another_text_format_is_still_scheduled(self, db):
        """SQLite keeps timestamps as text. Something other than this app may
        write one without microseconds; the row must not be skipped forever."""
        project = _project(db, _owner(db), "P1")
        db.execute(text("UPDATE projects SET last_drift_check_at = '2030-01-01 12:00:00', "
                        "next_drift_check_at = NULL, drift_check_scheduled_minutes = NULL"))
        db.commit()

        assert self._reconcile(db) == 1

        db.refresh(project)
        assert project.next_drift_check_at == datetime(2030, 1, 1, 12, 15)

    def test_each_tick_finds_the_untrusted_times_through_indexes(self, db):
        """Every tick runs this read, and on an up-to-date table it finds
        nothing: it must look that up, not walk every project to filter it.
        Ordering and limiting it made PostgreSQL do exactly that, which SQLite's
        planner does not reproduce, so the statement itself is checked for both.
        Each branch must also be an index lookup, the inheriting ones on the
        interval and the marker together, so projects with their own interval
        are never read."""
        owner = _owner(db)
        _project(db, owner, "INHERITS")
        _project(db, owner, "PINNED", interval=1440)

        with _project_reads() as reads:
            assert drift_worker._untrusted_schedules(db) == []

        (statement, parameters), = reads
        assert "ORDER BY" not in statement
        assert "LIMIT" not in statement
        plan = _query_plan(db, statement, parameters)
        assert not [step for step in plan if step.startswith("SCAN") or "PRIMARY KEY" in step], plan
        for bound in "<>":
            assert any(f"drift_check_interval_minutes=? AND drift_check_scheduled_minutes{bound}?" in step
                       for step in plan), plan

    def test_reconciling_is_not_an_edit(self, db):
        """The dashboard orders projects by updated_at."""
        project = _project(db, _owner(db), "P1")
        project.updated_at = datetime(2025, 6, 1, 9, 0)
        db.commit()

        self._reconcile(db, 60)

        db.refresh(project)
        assert project.updated_at == datetime(2025, 6, 1, 9, 0)

    def test_the_first_tick_records_every_pause_in_one_statement(self, db):
        owner = _owner(db)
        for code in ("A", "B", "C"):
            _project(db, owner, code, failure_count=4)

        with _tokens({}), patch("workflows.run_project_drift_check"), _updates() as executed:
            sweep_projects_for_drift(db)

        assert len([sql for sql in executed if "drift_error_summary" in sql]) == 1

    def test_many_pauses_are_written_a_chunk_per_transaction(self, db):
        owner = _owner(db)
        projects = [_project(db, owner, f"P{i}", failure_count=4) for i in range(5)]

        with _tokens({}), patch("workflows.run_project_drift_check"), \
             patch.object(drift_worker, "WRITE_CHUNK", 2), _updates() as executed:
            sweep_projects_for_drift(db)

        assert len([sql for sql in executed if "drift_error_summary" in sql]) == 3
        for project in projects:
            db.refresh(project)
            assert project.drift_error_summary == drift_worker.NO_CREDENTIAL_REASON

    def test_a_check_leaves_nothing_for_the_next_tick_to_rewrite(self, db):
        """Row-level scheduling is trusted, so the sweep does not write each
        checked project a second time."""
        from workflows import _cache_project_drift_summary
        project = _project(db, _owner(db), "P1", failure_count=2)

        _cache_project_drift_summary(db, project, "check_failed", 0, "boom")

        assert self._reconcile(db) == 0

    def test_never_checked_and_switched_off_projects_are_settled_once(self, db):
        owner = _owner(db)
        _project(db, owner, "NEW", last_checked=None)
        _project(db, owner, "OFF", interval=drift_worker.INTERVAL_OFF)
        db.execute(Project.__table__.update().values(drift_check_scheduled_minutes=None))
        db.commit()

        assert self._reconcile(db) == 2
        with _updates() as executed:
            assert self._reconcile(db) == 0
        assert executed == []

    def test_a_failed_reconcile_does_not_stop_the_checks(self, db):
        """One unreadable row must not stop every project being checked. The
        stored times are used as they stand."""
        _project(db, _owner(db), "P1")

        with _tokens({"alice": "tok"}), patch("workflows.run_project_drift_check", return_value=([], [])), \
             patch.object(drift_worker, "reconcile_schedules", side_effect=RuntimeError("database is locked")):
            assert sweep_projects_for_drift(db) == 1

    def test_a_project_reconcile_could_not_schedule_is_still_checked(self, db):
        """Checked, but with no time yet, and reconciling fails on every tick:
        it is due rather than never selected again."""
        _project(db, _owner(db), "P1", last_checked=FRESH)
        db.execute(Project.__table__.update().values(next_drift_check_at=None, drift_check_scheduled_minutes=None))
        db.commit()

        with _tokens({"alice": "tok"}), patch("workflows.run_project_drift_check", return_value=([], [])), \
             patch.object(drift_worker, "reconcile_schedules", side_effect=RuntimeError("database is locked")):
            assert sweep_projects_for_drift(db) == 1

    def test_saving_a_default_does_no_work_on_projects(self, db):
        """However many projects inherit it: they are rescheduled by the sweep."""
        _project(db, _owner(db), "P1")

        with _statements() as executed:
            _save_default(db, 60)

        assert not [sql for sql in executed if "projects" in sql]


def _file_database(tmp_path, timeout=5):
    """A second connection that really contends for locks: the in-memory
    database shares one connection, so it cannot show a race."""
    file_engine = create_engine(f"sqlite:///{tmp_path / 'drift.db'}", connect_args={"timeout": timeout})
    Base.metadata.create_all(bind=file_engine)
    return file_engine, sessionmaker(autocommit=False, autoflush=False, bind=file_engine)


def _save_default(session, minutes):
    """PUT /api/drift/settings, called directly."""
    from drift_settings import DriftSettingsSchema, update_drift_settings
    update_drift_settings(DriftSettingsSchema(sweep_enabled=True, recheck_interval_minutes=minutes,
                                              batch_size=5, poll_interval_seconds=60), session, None)


def _record_a_check(session, project_id, at):
    from workflows import _cache_project_drift_summary
    with patch("workflows.datetime") as clock:
        clock.now.return_value = at
        _cache_project_drift_summary(session, session.get(Project, project_id), "clean", 0, None)


def _reconcile_with_a_write_in_between(db, make_session, write):
    """Reconcile, with ``write`` committing from another session after the rows are read."""
    real_next_check_at = drift_worker.next_check_at
    interleaved = []

    def the_write_commits_first(*args):
        if not interleaved:
            interleaved.append(True)
            other = make_session()
            try:
                write(other)
            finally:
                other.close()
        return real_next_check_at(*args)

    with patch.object(drift_worker, "next_check_at", side_effect=the_write_commits_first):
        drift_worker.reconcile_schedules(db)
        db.commit()
    assert interleaved
    db.expire_all()


class TestAStoredTimeIsAlwaysRecomputedFromTheCommittedRow:
    """Nothing serialises a request's write with the sweep or with a save. A
    time written from stale inputs is marked, and recomputed (#2343)."""

    def test_a_write_from_a_stale_copy_of_the_project_is_scheduled_from_the_row(self, tmp_path):
        """The request loaded the project before a check committed, so its copy
        holds the old last check and failure streak. The time is computed from
        the locked row instead."""
        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project_id = _project(db, _owner(db), "P1", failure_count=5).project_id
            request = make_session()
            stale = request.get(Project, project_id)  # loaded now, as a request would have
            other = make_session()
            try:
                _record_a_check(other, project_id, datetime(2030, 1, 1, 12, 0))
            finally:
                other.close()
            stale.drift_check_interval_minutes = 30
            request.commit()
            request.close()

            db.expire_all()
            assert db.get(Project, project_id).next_drift_check_at == datetime(2030, 1, 1, 12, 30)
        finally:
            db.close()
            file_engine.dispose()

    def test_a_check_committing_mid_reconcile_is_not_overwritten(self, tmp_path):
        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project_id = _project(db, _owner(db), "P1").project_id
            _save_default(db, 60)

            _reconcile_with_a_write_in_between(
                db, make_session, lambda other: _record_a_check(other, project_id, datetime(2030, 1, 1, 12, 0)))

            assert db.get(Project, project_id).next_drift_check_at == datetime(2030, 1, 1, 13, 0)
        finally:
            db.close()
            file_engine.dispose()

    def test_a_check_scheduled_with_the_previous_default_is_not_overwritten(self, tmp_path):
        """The check read the default just before the new one was saved, so the
        marker it leaves is the one reconcile read: only the time shows the
        write. The next pass moves it to the new default, from the new check."""
        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project_id = _project(db, _owner(db), "P1").project_id
            _save_default(db, 60)
            checked_at = datetime(2030, 1, 1, 12, 0)

            def a_check_with_the_previous_default(other):
                with patch.object(drift_worker, "saved_default_minutes", return_value=15):
                    _record_a_check(other, project_id, checked_at)

            _reconcile_with_a_write_in_between(db, make_session, a_check_with_the_previous_default)
            assert db.get(Project, project_id).next_drift_check_at == checked_at + timedelta(minutes=15)

            drift_worker.reconcile_schedules(db)
            db.commit()
            db.expire_all()
            assert db.get(Project, project_id).next_drift_check_at == checked_at + timedelta(minutes=60)
        finally:
            db.close()
            file_engine.dispose()

    def test_an_interval_pinned_mid_reconcile_is_not_overwritten(self, tmp_path):
        """Pinned at the interval the row's time was already computed with, so
        the time and marker are unchanged: only the interval shows the write."""
        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project_id = _project(db, _owner(db), "P1").project_id
            _save_default(db, 60)

            def pin_at_15(other):
                other.get(Project, project_id).drift_check_interval_minutes = 15
                other.commit()

            _reconcile_with_a_write_in_between(db, make_session, pin_at_15)

            pinned = db.get(Project, project_id)
            assert pinned.next_drift_check_at - pinned.last_drift_check_at == timedelta(minutes=15)
            assert pinned.drift_check_scheduled_minutes == 15
        finally:
            db.close()
            file_engine.dispose()

    def test_an_unchanged_value_is_still_written_when_the_row_holds_another(self, tmp_path):
        """The request's copy holds the time and marker a 15-minute default
        gave. Meanwhile the default became 60 and the row was rescheduled. The
        request pins the interval at 15, which computes the same time and marker
        as its copy: unless they are sent anyway, the row keeps the 60-minute
        time, now marked as matching a 15-minute interval it never used."""
        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project_id = _project(db, _owner(db), "P1").project_id
            request = make_session()
            copy = request.get(Project, project_id)
            _save_default(db, 60)
            drift_worker.reconcile_schedules(db)
            db.commit()

            copy.drift_check_interval_minutes = 15
            request.commit()
            request.close()

            db.expire_all()
            pinned = db.get(Project, project_id)
            assert pinned.next_drift_check_at - pinned.last_drift_check_at == timedelta(minutes=15)
            assert pinned.drift_check_scheduled_minutes == 15
        finally:
            db.close()
            file_engine.dispose()

    def test_a_default_saved_mid_tick_reaches_the_pauses_that_tick(self, tmp_path):
        """Paused projects are never checked, so only the pause and reconciling
        move them. The pause reads the default after its rows, as reconciling
        does, rather than the one the tick started with."""
        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project_id = _project(db, _owner(db), "P1", failure_count=3).project_id
            real_owners = drift_worker._owners_by_credential

            def an_admin_saves_during_the_lookup(session, owner_ids):
                found = real_owners(session, owner_ids)
                admin = make_session()
                try:
                    _save_default(admin, 240)
                finally:
                    admin.close()
                return found

            with _tokens({}), patch("workflows.run_project_drift_check"), \
                 patch.object(drift_worker, "_owners_by_credential", side_effect=an_admin_saves_during_the_lookup):
                sweep_projects_for_drift(db)

            db.expire_all()
            paused = db.get(Project, project_id)
            assert paused.drift_error_summary == drift_worker.NO_CREDENTIAL_REASON
            assert paused.next_drift_check_at - paused.last_drift_check_at == timedelta(minutes=240)
        finally:
            db.close()
            file_engine.dispose()


class TestEveryOrmWriteReschedules:
    """Project's mapper events reschedule whatever wrote the input, so a new
    call site cannot forget to."""

    @pytest.mark.parametrize("write", [
        lambda p, at: setattr(p, "last_drift_check_at", at),
        lambda p, at: p.__setattr__("drift_check_failure_count", 3),
        lambda p, at: setattr(p, "drift_check_interval_minutes", 60),
    ], ids=["last_check", "streak", "interval"])
    def test_an_assignment(self, db, write):
        project = _project(db, _owner(db), "P1")
        write(project, datetime(2030, 1, 1, 12, 0))
        db.commit()

        db.refresh(project)
        assert project.next_drift_check_at == drift_worker.next_check_at(
            project.last_drift_check_at, project.drift_check_interval_minutes,
            project.drift_check_failure_count, 15)
        assert project.drift_check_scheduled_minutes == drift_worker._effective_minutes(
            project.drift_check_interval_minutes, 15)

    def test_a_new_project_built_already_checked(self, db):
        project = Project(project_name="built", project_code="BUILT", user_id=_owner(db).user_id,
                          use_prefix=False, branch_option="default",
                          last_drift_check_at=datetime(2030, 1, 1, 12, 0), drift_check_failure_count=2)
        db.add(project)
        db.commit()

        db.refresh(project)
        assert project.next_drift_check_at == datetime(2030, 1, 1, 12, 30)

    def test_a_new_project_is_scheduled_as_it_is_created(self, db):
        """Never checked, so due now; marked, so the next tick has nothing to redo."""
        project = Project(project_name="new", project_code="NEW", user_id=_owner(db).user_id,
                          use_prefix=False, branch_option="default")
        db.add(project)
        db.commit()

        db.refresh(project)
        assert (project.next_drift_check_at, project.drift_check_scheduled_minutes) == (
            None, drift_worker.DEFAULT_RECHECK_INTERVAL_MINUTES)
        assert drift_worker._untrusted_schedules(db) == []

    def test_a_project_deleted_meanwhile_is_reported_as_the_orm_reports_it(self, tmp_path):
        """Not as an error from inside the schedule hook."""
        from sqlalchemy.orm.exc import StaleDataError

        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project_id = _project(db, _owner(db), "P1").project_id
            request = make_session()
            copy = request.get(Project, project_id)
            other = make_session()
            other.delete(other.get(Project, project_id))
            other.commit()
            other.close()

            copy.last_drift_check_at = datetime(2030, 1, 1, 12, 0)
            with pytest.raises(StaleDataError):
                request.commit()
            request.close()
        finally:
            db.close()
            file_engine.dispose()

    def test_a_write_that_touches_no_input_leaves_the_schedule_alone(self, db):
        project = _project(db, _owner(db), "P1")
        drift_worker.reconcile_schedules(db)
        db.commit()
        scheduled = project.next_drift_check_at
        project.project_name = "renamed"

        with _updates() as executed:
            db.commit()

        assert len([sql for sql in executed if sql.startswith("UPDATE projects")]) == 1
        db.refresh(project)
        assert project.next_drift_check_at == scheduled
        assert project.drift_check_scheduled_minutes == 15


class TestAPauseNeverOverwritesAFresherCheck:
    def test_a_pause_moves_updated_at_as_a_check_result_does(self, db):
        """It changes what the project shows; only rescheduling is not an edit."""
        project = _project(db, _owner(db), "P1", failure_count=3)
        project.updated_at = datetime(2025, 6, 1, 9, 0)
        db.commit()

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_error_summary == drift_worker.NO_CREDENTIAL_REASON
        assert project.updated_at > datetime(2025, 6, 1, 9, 0)

    def test_a_status_set_after_the_rows_were_read_is_kept(self, db):
        """The status is worked out as the row is written, not as it was read."""
        project = _project(db, _owner(db), "P1", failure_count=3)
        project.drift_status = "check_failed"
        db.commit()
        real_saved_default = drift_worker.saved_default_minutes

        def a_verdict_lands_first(connection):
            db.execute(Project.__table__.update().values(drift_status="drifted"))
            return real_saved_default(connection)

        with _tokens({}), patch("workflows.run_project_drift_check"), \
             patch.object(drift_worker, "saved_default_minutes", side_effect=a_verdict_lands_first):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_error_summary == drift_worker.NO_CREDENTIAL_REASON
        assert project.drift_status == "drifted"

    def test_a_check_committing_mid_record_is_left_alone(self, tmp_path):
        """A check can commit after the pause read its rows. Each row is
        written only while the last check it was read with still holds."""
        file_engine, make_session = _file_database(tmp_path)
        db = make_session()
        try:
            project = _project(db, _owner(db), "P1", failure_count=3)
            project_id = project.project_id
            drift_worker.reconcile_schedules(db)
            db.commit()
            real_next_check_at = drift_worker.next_check_at
            interleaved = []

            def a_check_commits_first(*args):
                if not interleaved:
                    interleaved.append(True)
                    other = make_session()
                    try:
                        _record_a_check(other, project_id, datetime(2030, 1, 1, 12, 0))
                    finally:
                        other.close()
                return real_next_check_at(*args)

            with _tokens({}), patch("workflows.run_project_drift_check"), \
                 patch.object(drift_worker, "next_check_at", side_effect=a_check_commits_first):
                sweep_projects_for_drift(db)

            db.expire_all()
            fresh = db.get(Project, project_id)
            assert (fresh.drift_status, fresh.drift_error_summary) == ("clean", None)
        finally:
            db.close()
            file_engine.dispose()

    def test_a_project_checked_since_the_tick_began_is_left_alone(self, db):
        """That check is a fresher answer than this tick's credential lookup."""
        tick = datetime.now(timezone.utc)
        project = _project(db, _owner(db), "P1", last_checked=tick + timedelta(seconds=1))
        project.drift_status = "clean"
        project.next_drift_check_at = None
        db.commit()

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db, now=tick)

        db.refresh(project)
        assert project.drift_error_summary is None


class TestAnOwnersProjectsAreFoundThroughTheOwnerIndex:
    """Every tick looks up the projects of the owners it can check and of the
    ones it pauses. Without an index on the owner, each lookup read every
    project of every owner (#2346)."""

    INDEX = "ix_projects_user_id_next_drift_check_at"

    def _steps_on_the_owner_index(self, db, statement, parameters):
        plan = _query_plan(db, statement, parameters)
        steps = [step for step in plan if self.INDEX in step]
        assert steps, plan
        return steps

    def test_only_the_due_projects_of_a_checkable_owner_are_read(self, db):
        """Through both columns: with the owner alone, SQLite reads and sorts
        every project of the owner, which for a single-owner install is all of
        them, on every tick."""
        owner = _owner(db)
        _project(db, owner, "P1")

        with _project_reads() as reads:
            drift_worker._projects_to_check(db, datetime.now(timezone.utc), [owner.user_id], 5)

        assert len(reads) == 2
        for statement, parameters in reads:
            steps = self._steps_on_the_owner_index(db, statement, parameters)
            assert any("next_drift_check_at" in step.split(self.INDEX)[1] for step in steps), steps

    def test_the_projects_of_a_paused_owner(self, db):
        owner = _owner(db)
        _project(db, owner, "P1", failure_count=3)

        with _project_reads() as reads:
            drift_worker._record_paused_projects(
                db, {owner.user_id: drift_worker.NO_CREDENTIAL_REASON}, datetime.now(timezone.utc))

        assert reads
        for statement, parameters in reads:
            self._steps_on_the_owner_index(db, statement, parameters)


class TestTheMostOverdueProjectGoesFirst:
    def test_a_short_interval_far_past_due_beats_a_long_one_just_due(self, db):
        """Ordering by last check would pick the daily project, checked a day
        ago, over the 15-minute one checked 2h ago that is far more overdue."""
        owner = _owner(db)
        _project(db, owner, "DAILY", interval=1440,
                 last_checked=datetime.now(timezone.utc) - timedelta(days=1, minutes=5))
        _project(db, owner, "FAST", interval=15, last_checked=STALE)
        _settings(db, batch_size=1)

        seen = []
        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            sweep_projects_for_drift(db)

        assert seen == ["FAST"]


class TestTheKillSwitch:
    def test_disabled_means_nothing_is_checked(self, db):
        _project(db, _owner(db), "P1")
        _settings(db, sweep_enabled=False)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0

    def test_enabled_when_nothing_has_been_saved(self, db):
        """No settings row is the state of every install that has never opened
        the settings page — it must behave exactly as the old defaults did."""
        settings = drift_worker.get_settings(db)

        assert settings.sweep_enabled is True
        assert settings.recheck_interval_minutes == 15
        assert settings.batch_size == 5
        assert settings.poll_interval_seconds == 60


class TestPerProjectInterval:
    """One project off, one on a long interval, one on a short one — the point
    of moving this config into the GUI."""

    def test_a_project_set_to_off_is_never_checked(self, db):
        _project(db, _owner(db), "OFF", interval=drift_worker.INTERVAL_OFF,
                 last_checked=STALE - timedelta(days=7))

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0

    def test_off_does_not_starve_the_projects_behind_it(self, db):
        """An off project sorts to the head of the queue (oldest check first),
        so excluding it must not cost the next project its slot."""
        owner = _owner(db)
        _project(db, owner, "OFF", interval=drift_worker.INTERVAL_OFF,
                 last_checked=STALE - timedelta(days=7))
        _project(db, owner, "WANTED", last_checked=STALE)
        _settings(db, batch_size=1)

        seen = []
        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            assert sweep_projects_for_drift(db) == 1

        assert seen == ["WANTED"]

    def test_a_longer_project_interval_defers_a_check_the_global_would_allow(self, db):
        """Checked 2h ago: due at the 15-minute global default, not due on the
        project's own daily cadence."""
        _project(db, _owner(db), "DAILY", interval=1440, last_checked=STALE)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0

    def test_a_project_past_its_own_longer_interval_is_checked(self, db):
        _project(db, _owner(db), "DAILY", interval=1440,
                 last_checked=datetime.now(timezone.utc) - timedelta(days=2))

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", return_value=([], [])) as check:
            assert sweep_projects_for_drift(db) == 1

        assert check.call_count == 1

    def test_a_shorter_project_interval_beats_a_long_global_default(self, db):
        """The SQL pre-filter is bounded by the shortest interval in play, so a
        project checking more often than the global default is still reached."""
        _project(db, _owner(db), "FAST", interval=15,
                 last_checked=datetime.now(timezone.utc) - timedelta(minutes=30))
        _settings(db, recheck_interval_minutes=1440)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", return_value=([], [])) as check:
            assert sweep_projects_for_drift(db) == 1

        assert check.call_count == 1

    def test_no_override_inherits_the_global_default(self, db):
        _project(db, _owner(db), "P1", interval=None, last_checked=STALE)
        _settings(db, recheck_interval_minutes=1440)

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0

    def test_many_not_due_projects_do_not_starve_a_due_one(self, db):
        """The candidate window is capped, so "due" has to be decided in SQL.

        Sixty daily projects checked 2h ago are not due, but they sort ahead of
        a 15-minute project checked 30m ago that is. If they can occupy
        candidate slots, the due project is never reached and the tick does no
        work at all — the same starvation an "off" project would cause.
        """
        owner = _owner(db)
        for i in range(60):
            _project(db, owner, f"D{i}", interval=1440,
                     last_checked=datetime.now(timezone.utc) - timedelta(hours=2))
        _project(db, owner, "DUE", interval=15,
                 last_checked=datetime.now(timezone.utc) - timedelta(minutes=30))
        _settings(db, batch_size=5)

        seen = []
        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            assert sweep_projects_for_drift(db) == 1

        assert seen == ["DUE"]

    def test_backoff_still_applies_on_top_of_a_project_interval(self, db):
        """Two consecutive failures double the wait — on the project's own
        30-minute cadence, not the global default."""
        _project(db, _owner(db), "P1", interval=30, failure_count=2,
                 last_checked=datetime.now(timezone.utc) - timedelta(minutes=45))

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0


class TestAnUndecryptableSavedTokenIsNotACredential:
    """#2050: after a container update every project showed "Needs Attention".

    A saved PAT that will not decrypt (SECRET_KEY changed) resolved to the
    sentinel string, which the store handed out as an ordinary token. Every
    GitHub read then 401'd, every (workflow, repo) pair came back check_failed,
    and every project cached check_failed at once. These tests drive the real
    credential store rather than patching it, because the bug lived there.
    """

    @pytest.fixture
    def sentinel_owner(self, db):
        from auth import INVALID_SAVED_TOKEN_SENTINEL, user_tokens

        owner = _owner(db, "alice")
        user_tokens.clear()
        user_tokens._pat_cache["alice"] = (INVALID_SAVED_TOKEN_SENTINEL, float("inf"))
        try:
            yield owner
        finally:
            user_tokens.clear()
            user_tokens.invalidate_pat("alice")

    def test_the_project_is_skipped_rather_than_checked_with_a_garbage_token(
        self, db, sentinel_owner
    ):
        """Running the check anyway is what produced check_failed everywhere."""
        _project(db, sentinel_owner, "P1")

        with patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0

        assert check.call_count == 0

    def test_a_clean_project_is_not_flipped_to_check_failed(self, db, sentinel_owner):
        """The reported symptom. Running the check with the sentinel 401s every
        read, so the whole project caches check_failed — which the UI shows as
        "Needs Attention". The mock stands in for that, so the test fails if the
        sweep ever decides to run the check again."""
        project = _project(db, sentinel_owner, "P1")
        project.drift_status = "clean"
        db.commit()

        def every_read_401s(session, _username, proj):
            from workflows import _cache_project_drift_summary
            _cache_project_drift_summary(session, proj, "check_failed", 0, "401")
            return ([], [])

        with patch("workflows.run_project_drift_check", side_effect=every_read_401s):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_status == "clean"

    def test_the_badge_the_bug_left_behind_is_cleared(self, db, sentinel_owner):
        """The fix is worthless on upgrade if it only stops *new* false badges.

        Every affected project is already sitting on a check_failed written by
        the bug, which the UI renders as "Needs Attention". A skip knows no
        check is being attempted, so that non-verdict becomes "unknown"
        ("Not checked") instead of accusing the project indefinitely.
        """
        project = _project(db, sentinel_owner, "P1")
        project.drift_status = "check_failed"
        db.commit()

        with patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_status == "unknown"

    def test_a_real_verdict_is_still_not_touched(self, db, sentinel_owner):
        """Only the non-verdict is cleared: drift that was really found stands."""
        project = _project(db, sentinel_owner, "P1")
        project.drift_status = "drifted"
        project.drift_count = 3
        db.commit()

        with patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_status == "drifted"
        assert project.drift_count == 3

    def test_the_inflated_backoff_streak_is_reset(self, db, sentinel_owner):
        """Pre-fix, every doomed check incremented the streak, so it hit the 32x
        cap within ~1.5h. A skip is not a failed check, so carrying that streak
        would keep throttling a project that is not being checked at all — and
        would still be throttling it after the credential is repaired.

        The cursor is well inside the 8h window (32 x 15min) on purpose: a skip
        is not held back by failure backoff (#2058).
        """
        project = _project(db, sentinel_owner, "P1", failure_count=6,
                           last_checked=datetime.now(timezone.utc) - timedelta(hours=1))
        project.drift_status = "check_failed"
        db.commit()

        with patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_check_failure_count == 0
        assert project.drift_status == "unknown"
        assert project.drift_error_summary == drift_worker.UNREADABLE_CREDENTIAL_REASON

    def test_the_reason_names_the_unreadable_token_not_a_missing_one(
        self, db, sentinel_owner
    ):
        """A token *is* stored, so "no saved GitHub token" sends the owner
        looking for a credential that is sitting right there."""
        project = _project(db, sentinel_owner, "P1")

        with patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_error_summary == drift_worker.UNREADABLE_CREDENTIAL_REASON
        assert project.drift_error_summary != drift_worker.NO_CREDENTIAL_REASON


class TestARecordedSkipCannotKillTheTick:
    def test_a_failed_skip_does_not_stop_the_other_projects(self, db):
        """Recording a pause writes, and a write can fail (SQLite "database is
        locked"). Unguarded, that took the whole sweep down with it, on the
        first tick after a deploy when every affected project writes at once."""
        broke = _owner(db, "broke")
        fine = _owner(db, "fine")
        _project(db, broke, "SKIPPED", last_checked=STALE - timedelta(days=1))
        _project(db, fine, "WANTED", last_checked=STALE)

        seen = []
        with _tokens({"fine": "tok"}), \
             patch.object(drift_worker, "_record_pause_chunk", side_effect=RuntimeError("database is locked")), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            assert sweep_projects_for_drift(db) == 1

        assert seen == ["WANTED"]


class TestPausedAndBackedOffProjectsCannotStarveTheQueue:
    """#2058. Candidates used to be taken oldest-first through a fixed window
    (batch x 10) and filtered afterwards. A project that couldn't be checked
    never advanced its cursor and one in failure backoff wasn't due, yet both
    sorted first, so enough of either filled the window and nothing was checked.
    Now the query returns only checks to run."""

    def _wanted_is_checked(self, db, tokens):
        seen = []
        with _tokens(tokens), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            assert sweep_projects_for_drift(db) == 1
        return seen

    def test_more_paused_projects_than_the_old_window_do_not_starve_another_owner(self, db):
        broke = _owner(db, "broke")
        fine = _owner(db, "fine")
        for i in range(11):
            _project(db, broke, f"PAUSED{i}", last_checked=STALE - timedelta(days=1, minutes=i))
        _project(db, fine, "WANTED", last_checked=STALE)
        _settings(db, batch_size=1)

        assert self._wanted_is_checked(db, {"fine": "tok"}) == ["WANTED"]

    def test_more_backed_off_projects_than_the_old_window_do_not_starve_a_due_one(self, db):
        owner = _owner(db)
        for i in range(11):
            _project(db, owner, f"BACKOFF{i}", failure_count=10,
                     last_checked=STALE - timedelta(hours=1, minutes=i))
        _project(db, owner, "WANTED", last_checked=STALE)
        _settings(db, batch_size=1)

        assert self._wanted_is_checked(db, {"alice": "tok"}) == ["WANTED"]


class TestARepairedCredentialIsPickedUpPromptly:
    def test_a_paused_project_in_a_backoff_window_is_checked_once_its_credential_returns(self, db):
        """#2058: the first tick records the pause and resets the streak despite
        the 8h backoff; once the owner can be checked again, the next tick
        checks it rather than waiting the rest of that window out."""
        owner = _owner(db)
        project = _project(db, owner, "P1", failure_count=6,
                           last_checked=datetime.now(timezone.utc) - timedelta(hours=1))
        project.drift_status = "check_failed"
        db.commit()

        with _tokens({}), patch("workflows.run_project_drift_check") as check:
            assert sweep_projects_for_drift(db) == 0
        assert check.call_count == 0
        db.refresh(project)
        assert project.drift_status == "unknown"
        assert project.drift_check_failure_count == 0

        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check", return_value=([], [])) as check:
            assert sweep_projects_for_drift(db) == 1
        assert [c.args[2].project_code for c in check.call_args_list] == ["P1"]


class TestEachOwnersCredentialIsResolvedOncePerTick:
    def test_an_owner_with_several_due_projects_is_looked_up_once(self, db):
        """A credential belongs to the owner. Resolving it per project repeated
        the same lookup, and a decrypt on a cache miss, for every one of them."""
        owner = _owner(db)
        for code in ("A", "B", "C"):
            _project(db, owner, code)

        lookups = []
        with patch("auth.user_tokens.get", side_effect=lambda u, d=None: lookups.append(u) or "tok"), \
             patch("workflows.run_project_drift_check", return_value=([], [])):
            assert sweep_projects_for_drift(db) == 3

        assert lookups == ["alice"]


class TestTheStoredTimeMatchesTheBackoffMultiplier:
    @pytest.mark.parametrize("failures", range(9))
    def test_due_exactly_once_the_stretched_interval_has_passed(self, db, failures):
        """Every streak, including both sides of the cap, has to come due on
        the boundary _backoff_multiplier gives."""
        now = datetime.now(timezone.utc)
        window = timedelta(minutes=15 * drift_worker._backoff_multiplier(failures))
        owner = _owner(db)
        _project(db, owner, "JUST_DUE", failure_count=failures,
                 last_checked=now - window - timedelta(seconds=30))
        _project(db, owner, "NOT_YET", failure_count=failures,
                 last_checked=now - window + timedelta(seconds=30))

        seen = []
        with _tokens({"alice": "tok"}), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            sweep_projects_for_drift(db, now=now)

        assert seen == ["JUST_DUE"]


class TestOneOwnersCredentialLookupCannotKillTheTick:
    def test_the_other_owners_are_still_checked_and_the_failing_one_is_left_alone(self, db):
        """Credentials are resolved for every owner before any check runs, so an
        unguarded error there ended the tick before a single project was checked."""
        broke = _owner(db, "broke")
        fine = _owner(db, "fine")
        stuck = _project(db, broke, "STUCK", last_checked=STALE - timedelta(days=1))
        stuck.drift_status = "check_failed"
        db.commit()
        _project(db, fine, "WANTED", last_checked=STALE)

        def lookup(username, default=None):
            if username == "broke":
                raise RuntimeError("credential store unavailable")
            return "tok"

        seen = []
        with patch("auth.user_tokens.get", side_effect=lookup), \
             patch("workflows.run_project_drift_check",
                   side_effect=lambda d, u, p: seen.append(p.project_code) or ([], [])):
            assert sweep_projects_for_drift(db) == 1

        assert seen == ["WANTED"]
        db.refresh(stuck)
        assert stuck.drift_status == "check_failed"
        assert stuck.drift_error_summary is None


class TestARecordedPauseIsNotRevisited:
    def test_a_changed_reason_is_still_recorded(self, db):
        """The reason is part of what "already recorded" means, so a pause that
        changes cause is rewritten rather than filtered out as done."""
        project = _project(db, _owner(db), "P1")

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)
            with patch("auth.user_tokens.has_unreadable_saved_token", return_value=True):
                sweep_projects_for_drift(db)

        db.refresh(project)
        assert project.drift_error_summary == drift_worker.UNREADABLE_CREDENTIAL_REASON


class TestEveryPauseIsRecordedInOneTick:
    def test_the_batch_size_does_not_limit_how_many_pauses_are_recorded(self, db):
        """The batch caps GitHub checks. Recording a pause spends no call, so a
        project past the first batch must not keep showing its last failure."""
        owner = _owner(db)
        projects = [_project(db, owner, f"P{i}", last_checked=STALE - timedelta(minutes=i)) for i in range(3)]
        for project in projects:
            project.drift_status = "check_failed"
        db.commit()
        _settings(db, batch_size=1)

        with _tokens({}), patch("workflows.run_project_drift_check"):
            sweep_projects_for_drift(db)

        for project in projects:
            db.refresh(project)
            assert (project.drift_status, project.drift_error_summary) == (
                "unknown", drift_worker.NO_CREDENTIAL_REASON)


class TestNothingToRecordReleasesTheWriteLock:
    def test_other_writers_are_not_locked_out_while_the_checks_run(self, tmp_path):
        """On SQLite an UPDATE that matches nothing still opens a write
        transaction. Left open through the checks, every API write in the
        meantime fails with "database is locked". Needs a file database: the
        in-memory one shares a single connection, so it cannot show the lock."""
        import sqlite3

        path = tmp_path / "drift.db"
        file_engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(bind=file_engine)
        db = sessionmaker(autocommit=False, autoflush=False, bind=file_engine)()
        try:
            broke = _owner(db, "broke")
            fine = _owner(db, "fine")
            _project(db, broke, "PAUSED", last_checked=STALE - timedelta(days=1))
            _project(db, fine, "WANTED", last_checked=STALE)

            def write_from_another_connection(_db, _user, _project):
                other = sqlite3.connect(path, timeout=0)
                try:
                    other.execute("UPDATE projects SET project_name = project_name")
                    other.commit()
                finally:
                    other.close()
                return [], []

            with _tokens({"fine": "tok"}), patch("workflows.run_project_drift_check"):
                sweep_projects_for_drift(db)  # records the pause
            with _tokens({"fine": "tok"}), \
                 patch("workflows.run_project_drift_check", side_effect=write_from_another_connection):
                assert sweep_projects_for_drift(db) == 1
        finally:
            db.close()
            file_engine.dispose()
