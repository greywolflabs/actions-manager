"""
Drift reconciliation sweep.

Drift state only ever changed when a human clicked: "Check now", opening a
workflow's drift detail, or resolving a drift. Nothing ran on its own, so a
project could show "in sync" indefinitely while GitHub had drifted. This is the
automatic trigger that was missing.

An in-process asyncio polling loop — no Celery/APScheduler/Redis — started at
FastAPI startup, matching notification_worker.py. Same single-instance
assumption: this app has no multi-replica deployment today, so no row locking.
Two instances would duplicate checks, which wastes rate limit but cannot
corrupt state.

Affordable only because of the conditional-fetch work: an unchanged branch
answers 304, which does not count against the rate limit, and branch recency is
cached by head SHA. Sweeping a quiet project costs roughly one conditional call
per repo, which is why polling every project on a timer is now reasonable when
it would not have been before.

Cadence comes from the database, not the environment: global defaults live in
``drift_settings`` (edited by workspace admins in the GUI) and a project can
override the interval, or opt out entirely, via
``Project.drift_check_interval_minutes``. Everything is re-read per tick, so
changes apply without a restart.

Each project stores when it is next due (``next_drift_check_at``), derived by
next_check_at from its last check, interval and failure streak. Due-ness is
then one indexed comparison, and the backoff curve can be anything Python can
compute. Every ORM write to one of those inputs writes the time in the same
statement, from the locked row (Project's mapper events), and records in
``drift_check_scheduled_minutes`` the interval it used. Each tick first
rewrites any time that row-level scheduling could not settle — a workspace
default changed since, or a row nothing has scheduled yet (reconcile_schedules).
"""

import asyncio
import contextlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Optional

from sqlalchemy import and_, bindparam, case, false, inspect, or_, select, union, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from authorization import PROJECT_DELETING
from models import Account, DriftSettings, Project

DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_RECHECK_INTERVAL_MINUTES = 15
DEFAULT_BATCH_SIZE = 5

# A project stuck returning check_failed is retried less and less often as
# failures accumulate, instead of at the full recheck interval forever. The
# cap is a multiplier rather than an absolute number so it scales with
# whatever recheck interval an admin configures.
BACKOFF_MAX_MULTIPLIER = 32

# Per-project override meaning "never sweep this project".
INTERVAL_OFF = 0


def _backoff_multiplier(failure_count: int) -> int:
    """1x for a project with no recent failures, doubling per failure after
    the first, capped at BACKOFF_MAX_MULTIPLIER. Mirrors notification_worker's
    _backoff_delay shape."""
    return min(2 ** max(failure_count - 1, 0), BACKOFF_MAX_MULTIPLIER)


_DEFAULTS = SimpleNamespace(
    sweep_enabled=True,
    recheck_interval_minutes=DEFAULT_RECHECK_INTERVAL_MINUTES,
    batch_size=DEFAULT_BATCH_SIZE,
    poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
)


def get_settings(db: Session):
    """Global drift settings, or the built-in defaults when nobody has saved any.

    Read fresh on every call rather than cached, so a change made in the GUI
    takes effect on the next sweep instead of at the next restart. That
    includes the session's own copy: populate_existing stops a row loaded
    earlier in a long-lived session from answering with what it said then.
    """
    return (db.query(DriftSettings).order_by(DriftSettings.settings_id)
            .populate_existing().first()) or _DEFAULTS


def next_check_at(last_checked_at: Optional[datetime], interval_minutes: Optional[int],
                  failure_count: int, default_minutes: int) -> Optional[datetime]:
    """When the sweep should next check a project; None means now (never checked).

    Its own interval, or the workspace default, stretched by its failure streak
    and counted from its last check. The sweep only compares the stored result
    with the clock, so it has to be re-derived whenever one of these inputs
    changes: by schedule_flushed_project on every ORM write, and by
    reconcile_schedules for everything else.
    """
    if last_checked_at is None:
        return None
    minutes = _effective_minutes(interval_minutes, default_minutes)
    return last_checked_at + timedelta(minutes=minutes * _backoff_multiplier(failure_count or 0))


def _effective_minutes(interval_minutes: Optional[int], default_minutes: int) -> int:
    return default_minutes if interval_minutes is None else interval_minutes


SCHEDULE_INPUTS = ("last_drift_check_at", "drift_check_failure_count", "drift_check_interval_minutes")

# Most project rows a bulk schedule write touches in one transaction.
WRITE_CHUNK = 500


def saved_default_minutes(connection) -> int:
    c = DriftSettings.__table__.c
    saved = connection.execute(select(c.recheck_interval_minutes).order_by(c.settings_id).limit(1)).scalar()
    return saved or DEFAULT_RECHECK_INTERVAL_MINUTES


def _locked_inputs(connection, project_id: int):
    """The row's committed schedule inputs, locked until this transaction ends.

    PostgreSQL locks the row (FOR UPDATE). SQLite has no row locks or
    SELECT ... FOR UPDATE; it serialises writers on one database lock, which a
    no-op UPDATE takes before the read.
    """
    c = Project.__table__.c
    if connection.dialect.name == "sqlite":
        connection.execute(update(Project.__table__).where(false()).values(project_id=c.project_id))
    return connection.execute(
        select(c.last_drift_check_at, c.drift_check_failure_count, c.drift_check_interval_minutes)
        .where(c.project_id == project_id).with_for_update()
    ).one_or_none()


def schedule_flushed_project(connection, project: Project) -> None:
    """Schedule a project as it is inserted, or in the same UPDATE as a change to one of its schedule inputs.

    Called from Project's before_insert/before_update mapper events (models.py),
    so no ORM writer — an assignment, a constructor argument, setattr — can
    change an input and leave the old time behind. The in-memory project can
    hold a stale copy of an input this write did not touch, so the row is
    locked and those are read from it: the time written is the one the row
    will actually hold. The marker records the interval it was computed with;
    a default saved at the same moment differs from it, and reconcile_schedules
    redoes the row. Both columns are always sent, since an unchanged value is
    otherwise left out of the UPDATE while the database may hold another.
    """
    state = inspect(project)
    changed = {name for name in SCHEDULE_INPUTS if state.attrs[name].history.has_changes()}
    if not changed and state.has_identity:
        return
    if state.has_identity:
        # Only what this write changed comes from the project: reading the
        # rest from it would refresh an expired instance just to discard it.
        current = _locked_inputs(connection, state.identity[0])
        if current is None:
            return  # deleted meanwhile; the ORM's own stale-row check reports it
        inputs = {name: getattr(project, name) if name in changed else current._mapping[name]
                  for name in SCHEDULE_INPUTS}
    else:
        inputs = {name: getattr(project, name) for name in SCHEDULE_INPUTS}
    default_minutes = saved_default_minutes(connection)
    project.next_drift_check_at = next_check_at(
        inputs["last_drift_check_at"], inputs["drift_check_interval_minutes"],
        inputs["drift_check_failure_count"], default_minutes)
    project.drift_check_scheduled_minutes = _effective_minutes(inputs["drift_check_interval_minutes"],
                                                               default_minutes)
    flag_modified(project, "next_drift_check_at")
    flag_modified(project, "drift_check_scheduled_minutes")


def _compare_and_set(**values):
    """UPDATE one project by id, only while the row is as it was read.

    Bound per row as b_id, b_was_next, b_was_marker, b_streak and b_interval.
    Every write to a schedule input also rewrites the time and the marker, so
    comparing those catches a change to the last check without comparing its
    timestamp, which SQLite holds as text: a value written in another format
    by something else would never match, and the row would never be written.
    """
    c = Project.__table__.c
    return (
        update(Project.__table__)
        .where(c.project_id == bindparam("b_id"),
               c.next_drift_check_at.is_not_distinct_from(bindparam("b_was_next")),
               c.drift_check_scheduled_minutes.is_not_distinct_from(bindparam("b_was_marker")),
               c.drift_check_failure_count == bindparam("b_streak"),
               c.drift_check_interval_minutes.is_not_distinct_from(bindparam("b_interval")))
        .values(**values)
    )


def _row_as_read(row) -> dict:
    return {"b_id": row.project_id, "b_was_next": row.next_drift_check_at,
            "b_was_marker": row.drift_check_scheduled_minutes,
            "b_streak": row.drift_check_failure_count, "b_interval": row.drift_check_interval_minutes}


def _untrusted_schedules(db) -> list[int]:
    """Ids of the projects whose stored time can't be trusted.

    That is a row nothing has scheduled yet, or one inheriting a default other
    than the one its time was computed with. Neither ordered nor limited: with
    ORDER BY project_id LIMIT, PostgreSQL takes the interval and the marker as
    independent, expects thousands of matches where an up-to-date table has
    none, and walks the primary key past every project on every tick. Without
    them, each branch is a lookup in its own index (the marker, and the
    interval with the marker), so a tick with nothing to do reads nothing.
    """
    c = Project.__table__.c
    marker, inheriting = c.drift_check_scheduled_minutes, c.drift_check_interval_minutes.is_(None)
    default_minutes = saved_default_minutes(db)
    return db.execute(select(c.project_id).where(or_(
        marker.is_(None),
        # Two ranges rather than "!=", which an index cannot serve.
        and_(inheriting, marker < default_minutes),
        and_(inheriting, marker > default_minutes),
    ))).scalars().all()


def reconcile_schedules(db) -> int:
    """Rewrite every stored time that can't be trusted. Returns how many rows were written.

    Committed every WRITE_CHUNK rows, so no transaction holds more: a new
    default moves every inheriting project at once, which on PostgreSQL is
    seconds of row locks in a single transaction at a few tens of thousands.
    """
    project_ids = _untrusted_schedules(db)
    written = 0
    for start in range(0, len(project_ids), WRITE_CHUNK):
        written += _reconcile_chunk(db, project_ids[start:start + WRITE_CHUNK])
        db.commit()
    return written


def _reconcile_chunk(db, project_ids: list[int]) -> int:
    """Recompute these projects' times from their committed rows; returns how many were written.

    The default is read after the rows. A marker on them was written with a
    default committed before they were read, so the one used here is at least
    as new: a row a check has just scheduled with a freshly saved default is
    never moved back to an older one. Each row is written only while it is as
    it was read, so a write landing in between is not overwritten. updated_at
    is kept: rescheduling is not an edit, and the dashboard orders projects by it.
    """
    c = Project.__table__.c
    rows = db.execute(
        select(c.project_id, c.last_drift_check_at, c.drift_check_interval_minutes,
               c.drift_check_failure_count, c.next_drift_check_at, c.drift_check_scheduled_minutes)
        .where(c.project_id.in_(project_ids))
        # The UPDATE below locks the rows in this order: primary-key order, as
        # every other multi-row writer takes them, so two writers can't each
        # hold a row the other is waiting for. Keep `moved` in it.
        .order_by(c.project_id)
    ).all()
    default_minutes = saved_default_minutes(db)
    moved = []
    for row in rows:
        scheduled = next_check_at(row.last_drift_check_at, row.drift_check_interval_minutes,
                                  row.drift_check_failure_count, default_minutes)
        interval = _effective_minutes(row.drift_check_interval_minutes, default_minutes)
        if (scheduled, interval) != (row.next_drift_check_at, row.drift_check_scheduled_minutes):
            moved.append({**_row_as_read(row), "b_next": scheduled, "b_marker": interval})
    if not moved:
        return 0
    return db.execute(_compare_and_set(next_drift_check_at=bindparam("b_next"),
                                       drift_check_scheduled_minutes=bindparam("b_marker"),
                                       updated_at=c.updated_at),
                      moved).rowcount


def _schedulable():
    # A project being deleted is only waiting for its removal PRs: nothing to
    # check, so its checks would only spend rate limit.
    return and_(Project.pr_state != PROJECT_DELETING,
                or_(Project.drift_check_interval_minutes.is_(None),
                    Project.drift_check_interval_minutes != INTERVAL_OFF))


def _unscheduled():
    """Never checked, or checked without a time yet because reconciliation could
    not write one: either way due now, so a failing reconcile delays no project."""
    return Project.next_drift_check_at.is_(None)


def _due(now: datetime):
    """Projects due a check: unscheduled, or past their stored next check."""
    return and_(_schedulable(), or_(_unscheduled(), Project.next_drift_check_at <= now))


def _projects_to_check(db: Session, now: datetime, owner_ids: list[int], limit: int) -> list[Project]:
    """Due projects of owners who can be checked, unscheduled first, then most overdue.

    Owners who can't be checked are excluded, so every row returned is a check
    to run. Nothing can sit at the head of the queue without being checked and
    starve the projects behind it, however many there are (#2058). Two reads
    rather than one ORDER BY on "IS NULL", which no index serves. Both read
    only the listed owners' unscheduled or due projects, through
    (user_id, next_drift_check_at): in next-check order for one owner, sorted
    for several.
    """
    if not owner_ids:
        return []
    candidates = db.query(Project).filter(_schedulable(), Project.user_id.in_(owner_ids))
    unscheduled = (candidates.filter(_unscheduled())
                   .order_by(Project.project_id).limit(limit).all())
    if len(unscheduled) >= limit:
        return unscheduled
    return unscheduled + (candidates.filter(Project.next_drift_check_at <= now)
                          .order_by(Project.next_drift_check_at)
                          .limit(limit - len(unscheduled)).all())


# Shown to the user beside the last-checked time. Without it a skipped project
# is indistinguishable from a forgotten one: the timestamp simply stops moving
# and nothing says why.
NO_CREDENTIAL_REASON = (
    "Automatic drift checks are paused: this project's owner has no saved "
    "GitHub token. Sign out and sign back in to store one, or save a personal "
    "access token. Check Now still works."
)

# Distinct from the above on purpose: a token *is* stored, so "no saved token"
# would send the owner looking for a missing credential that is right there.
UNREADABLE_CREDENTIAL_REASON = (
    "Automatic drift checks are paused: this project's owner has a saved "
    "GitHub token that cannot be decrypted, which usually means SECRET_KEY "
    "changed since it was saved. Check that SECRET_KEY survives container "
    "updates, then save the personal access token again to re-encrypt it "
    "under the current key."
)


def _owner_credential(owner: Account) -> tuple[Optional[str], Optional[str]]:
    """The owner's username, or None plus why their projects cannot be checked.

    Exactly one of the two is set: a username with no reason, or a reason with
    no username. The caller still defaults the reason rather than asserting it,
    so a future branch that forgets one cannot write a null over a real one.

    Resolved through the normal credential store, which prefers a saved PAT and
    falls back to an in-memory OAuth session token. A worker has no request
    context, so the store's per-request identity guard does not apply here. The
    token itself is discarded — the drift check resolves it again by username;
    this only answers "is this project checkable at all".
    """
    from auth import user_tokens

    if not owner.github_user:
        return None, NO_CREDENTIAL_REASON
    username = owner.github_user
    if user_tokens.get(username):
        return username, None
    if user_tokens.has_unreadable_saved_token(username):
        return None, UNREADABLE_CREDENTIAL_REASON
    return None, NO_CREDENTIAL_REASON


def _owners_awaiting_a_credential(now: datetime):
    """Owners whose credential this tick needs: of due projects, and of failing ones.

    A failing project inside its backoff window isn't due, but a lost
    credential first shows up as failing checks, and its pause has to replace
    that failure now rather than once the backoff runs out (#2058).

    Two selects rather than one OR, so each is served by its own index
    (next_drift_check_at, drift_check_failure_count) instead of the OR forcing
    a scan of every project on every tick.
    """
    return union(
        select(Project.user_id).where(_due(now)),
        select(Project.user_id).where(_schedulable(), Project.drift_check_failure_count > 0),
    )


def _owners_by_credential(db: Session, owner_ids) -> tuple[dict[int, str], dict[int, str]]:
    """The selected owners, split into checkable (id -> username) and paused (id -> reason).

    A credential belongs to the owner, not the project, so it is resolved once
    per owner per tick rather than once per project. An owner whose lookup
    raises lands in neither: their projects are left exactly as they were and
    retried next tick, rather than the error ending the tick for everyone.
    """
    owners = db.query(Account).filter(Account.user_id.in_(owner_ids)).all()
    checkable, paused = {}, {}
    for owner in owners:
        try:
            username, reason = _owner_credential(owner)
        except Exception as exc:  # noqa: BLE001 - one bad owner must not stop the sweep
            print(f"⚠️ Drift sweep could not resolve a credential for account {owner.user_id}: {exc}",
                  file=sys.stderr, flush=True)
            continue
        if username is None:
            paused[owner.user_id] = reason or NO_CREDENTIAL_REASON
        else:
            checkable[owner.user_id] = username
    return checkable, paused


def _record_paused_projects(db: Session, paused: dict[int, str], now: datetime) -> None:
    """Say why a paused owner's projects are passed over, without pretending they were checked.

    ``last_drift_check_at`` is left alone on purpose: advancing it would make an
    unchecked project read as freshly verified.

    ``drift_status`` keeps a real verdict — a project that genuinely was
    drifting still is — but ``check_failed`` is not a verdict. It means a check
    could not reach one, which is the same answer a skip gives without spending
    the call. Leaving it would keep every project reading "Needs Attention" for
    a check that is no longer even attempted, which is the #2050 symptom
    surviving its own fix on upgrade. It becomes "unknown" ("Not checked"), and
    the reason says why. updated_at moves, as it does for a check result: what
    the project shows has changed.

    The failure streak goes with it, so the project returns to its normal
    schedule: backoff spaces out failing *checks*, and a skip is not one. That
    is also why every project of the owner is covered, not only the due ones —
    one inside its backoff window is the one whose stale failure most needs
    replacing (#2058).

    Only rows that don't show the reason yet are read, and they are written in
    batched UPDATEs of WRITE_CHUNK rows, each committed on its own: the first
    tick after SECRET_KEY changes can cover every project of an owner, and must
    not hold all of their rows at once. Later ticks write nothing.

    A project checked since this tick began is left alone: that check is a
    fresher answer than the credential lookup this tick made before it. Each
    row is also written only while it is as it was read (_compare_and_set), so
    a check committing in between is never overwritten; the next tick looks at
    it again. The default is read after the rows, as reconcile_schedules
    does, so a row it just moved to a new default is not moved back.
    """
    if not paused:
        return
    owners_by_reason: dict[str, list[int]] = {}
    for owner_id, reason in paused.items():
        owners_by_reason.setdefault(reason, []).append(owner_id)
    not_yet_recorded = or_(*(
        and_(Project.user_id.in_(owner_ids),
             or_(Project.drift_status == "check_failed",
                 Project.drift_check_failure_count != 0,
                 Project.drift_error_summary.is_(None),
                 Project.drift_error_summary != reason))
        for reason, owner_ids in owners_by_reason.items()
    ))
    not_checked_this_tick = or_(Project.last_drift_check_at.is_(None), Project.last_drift_check_at < now)
    after_id = 0
    while after_id is not None:
        try:
            after_id = _record_pause_chunk(db, paused, after_id,
                                           and_(_schedulable(), not_yet_recorded, not_checked_this_tick))
            db.commit()
        except Exception as exc:  # noqa: BLE001 - a failed write must not stop the checks
            print(f"⚠️ Drift sweep could not record paused projects: {exc}", file=sys.stderr, flush=True)
            db.rollback()
            return


def _record_pause_chunk(db: Session, paused: dict[int, str], after_id: int, unrecorded) -> Optional[int]:
    """Record one chunk of pauses; returns the last project id read, or None once done."""
    rows = (
        db.query(Project.project_id, Project.user_id, Project.last_drift_check_at,
                 Project.drift_check_failure_count, Project.drift_check_interval_minutes,
                 Project.next_drift_check_at, Project.drift_check_scheduled_minutes)
        .filter(unrecorded, Project.project_id > after_id)
        .order_by(Project.project_id)
        .limit(WRITE_CHUNK)
        .all()
    )
    if rows:
        default_minutes = saved_default_minutes(db)
        db.execute(_compare_and_set(
            # From the row as it is written, not as read: a status set meanwhile stays.
            drift_status=case((Project.drift_status == "check_failed", "unknown"), else_=Project.drift_status),
            drift_check_failure_count=0,
            drift_error_summary=bindparam("b_reason"), next_drift_check_at=bindparam("b_next"),
            drift_check_scheduled_minutes=bindparam("b_marker")), [{
                **_row_as_read(row),
                "b_reason": paused[row.user_id],
                "b_next": next_check_at(row.last_drift_check_at, row.drift_check_interval_minutes, 0,
                                        default_minutes),
                "b_marker": _effective_minutes(row.drift_check_interval_minutes, default_minutes),
            } for row in rows])
    return rows[-1].project_id if len(rows) == WRITE_CHUNK else None


def _reconcile(db: Session) -> None:
    """Bring stored times up to date before anything is selected from them.

    A failure is reported and the tick goes on with the times as stored, so
    one unreadable row cannot stop every project being checked. Nothing is
    lost: a row still waiting for its time is due (_unscheduled), and one left
    on an old default is off by at most that default, once.
    """
    try:
        reconcile_schedules(db)
    except Exception as exc:  # noqa: BLE001 - reported; the tick goes on with the times as stored
        print(f"⚠️ Drift sweep could not reconcile schedules: {exc}", file=sys.stderr, flush=True)
        db.rollback()


def sweep_projects_for_drift(db: Session, now: Optional[datetime] = None) -> int:
    """Re-check up to one batch of stale projects. Returns how many were checked."""
    settings = get_settings(db)
    if not settings.sweep_enabled:
        return 0

    from workflows import run_project_drift_check

    now = now or datetime.now(timezone.utc)
    _reconcile(db)
    checkable, paused = _owners_by_credential(db, _owners_awaiting_a_credential(now))
    _record_paused_projects(db, paused, now)

    checked = 0
    for project in _projects_to_check(db, now, list(checkable), settings.batch_size):
        try:
            run_project_drift_check(db, checkable[project.user_id], project)
            checked += 1
        except Exception as exc:  # noqa: BLE001 - one bad project must not stop the sweep
            print(f"⚠️ Drift sweep failed for project {project.project_id}: {exc}",
                  file=sys.stderr, flush=True)
            db.rollback()

    return checked


async def drift_worker_loop(
    session_factory: Callable[[], Session],
    poll_interval_seconds: Optional[int] = None,
) -> None:
    """Sleep-then-poll loop. Sleeps first so a task cancelled shortly after
    startup (e.g. a short-lived test lifespan) never touches the database."""
    loop = asyncio.get_event_loop()
    interval = poll_interval_seconds or DEFAULT_POLL_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(interval)
        try:
            db = session_factory()
            try:
                # The drift check does blocking `requests` I/O; running it
                # directly here would stall every other request sharing this
                # process's event loop.
                await loop.run_in_executor(None, sweep_projects_for_drift, db)
                # Re-read each tick so an admin changing the cadence in the GUI
                # doesn't have to wait for a restart. An explicit argument
                # (tests) still wins over whatever is stored. The first sleep
                # uses the default, because reading it earlier would touch the
                # database before the loop is known to be staying alive.
                if poll_interval_seconds is None:
                    interval = get_settings(db).poll_interval_seconds
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 - a bad iteration must never kill the worker
            print(f"⚠️ Drift sweep iteration failed: {exc}", file=sys.stderr, flush=True)


def start_drift_worker(session_factory: Callable[[], Session]) -> asyncio.Task:
    return asyncio.create_task(drift_worker_loop(session_factory))


async def stop_drift_worker(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
