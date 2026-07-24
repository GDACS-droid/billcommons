"""Tests for billcommons_ingest.scheduler cadence logic + enqueue pass.

Business intent per docs/SPEC.md "Refresh targets": active/special sessions
must sync far more often than dormant ones, and a jurisdiction whose last
sync is still "fresh" for its tier must NOT be re-enqueued (that's the
entire point of the idempotent due-check -- a bug here either burns the
shared Open States API quota on redundant syncs, or silently stops
refreshing an active legislature).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from billcommons_ingest import queue as queue_mod
from billcommons_ingest.scheduler import (
    CADENCE_TIER_ACTIVE,
    CADENCE_TIER_DORMANT,
    CADENCE_TIER_RECENTLY_ADJOURNED,
    CADENCE_TIER_YEAR_ROUND,
    API_SYNC_KIND,
    SCHEDULE_PASS_ADVISORY_LOCK_KEY,
    cadence_minutes,
    cadence_tier,
    is_due,
    plan_schedule,
    run_schedule_pass,
)
from billcommons_schema.models import IngestJob, Jurisdiction, Session as SessionModel
from billcommons_shared.db import get_engine

# Fixed instant for pure (no-DB) cadence/is_due tests, where any timestamp
# works equally well.
_NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _real_now() -> datetime:
    """Fresh wall-clock `now()` for DB-backed tests. Several of those tests
    round-trip through `IngestJob.created_at`, a real DB server-side
    timestamp (see TimestampMixin) -- comparing it against a *fixed* fake
    `_NOW` computed once at import time is flaky in a full suite run: each
    test in this file is separated by real wall-clock time (WAN DB latency
    across ~90s+ of preceding tests), so by the time a later test executes,
    the actual clock has drifted arbitrarily far from any constant chosen
    at import time, making `later - real_created_at` go negative. Fetching
    `now()` fresh at the point of use keeps the comparison anchored to
    the real DB writes it's actually being compared against."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pure cadence-tier classification
# ---------------------------------------------------------------------------


def test_active_session_with_end_date_is_active_tier():
    tier = cadence_tier(active=True, end_date=_NOW.date() + timedelta(days=30), now=_NOW)
    assert tier == CADENCE_TIER_ACTIVE
    assert cadence_minutes(tier) == 30


def test_active_session_with_no_end_date_is_year_round():
    # Mirrors DC's real registry row: active=True, expected_adjournment=null.
    tier = cadence_tier(active=True, end_date=None, now=_NOW)
    assert tier == CADENCE_TIER_YEAR_ROUND
    assert cadence_minutes(tier) == 60


def test_inactive_session_recently_ended_is_recently_adjourned():
    tier = cadence_tier(active=False, end_date=_NOW.date() - timedelta(days=10), now=_NOW)
    assert tier == CADENCE_TIER_RECENTLY_ADJOURNED
    assert cadence_minutes(tier) == 24 * 60


def test_inactive_session_ended_long_ago_is_dormant():
    tier = cadence_tier(active=False, end_date=_NOW.date() - timedelta(days=200), now=_NOW)
    assert tier == CADENCE_TIER_DORMANT
    assert cadence_minutes(tier) == 7 * 24 * 60


def test_inactive_session_with_no_end_date_is_dormant():
    # No-2026-regular-session states (e.g. MT/TX/NV/ND) -- nothing to key a
    # "recently adjourned" window off of, so this falls to the safest
    # (least-frequent) tier rather than guessing.
    tier = cadence_tier(active=False, end_date=None, now=_NOW)
    assert tier == CADENCE_TIER_DORMANT


# ---------------------------------------------------------------------------
# is_due (fake clock, no DB)
# ---------------------------------------------------------------------------


def test_never_enqueued_is_always_due():
    assert is_due(last_enqueued_at=None, tier=CADENCE_TIER_ACTIVE, now=_NOW) is True


def test_active_tier_not_due_before_30_minutes():
    last = _NOW - timedelta(minutes=10)
    assert is_due(last_enqueued_at=last, tier=CADENCE_TIER_ACTIVE, now=_NOW) is False


def test_active_tier_due_after_30_minutes():
    last = _NOW - timedelta(minutes=31)
    assert is_due(last_enqueued_at=last, tier=CADENCE_TIER_ACTIVE, now=_NOW) is True


def test_dormant_tier_not_due_after_one_day():
    # A dormant jurisdiction synced yesterday must NOT be due again (weekly
    # cadence) -- this is the exact bug class ("everything looks due because
    # we forgot to check the tier's own interval") the pure function guards.
    last = _NOW - timedelta(days=1)
    assert is_due(last_enqueued_at=last, tier=CADENCE_TIER_DORMANT, now=_NOW) is False


def test_dormant_tier_due_after_one_week():
    last = _NOW - timedelta(days=8)
    assert is_due(last_enqueued_at=last, tier=CADENCE_TIER_DORMANT, now=_NOW) is True


# ---------------------------------------------------------------------------
# DB-backed plan_schedule / run_schedule_pass
# ---------------------------------------------------------------------------


def _make_jurisdiction_with_session(db_session, *, active, end_date, abbr=None):
    if abbr is None:
        abbr = f"ZQ_SCHED_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="Scheduler Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier="2026 Session",
        active=active,
        end_date=end_date,
    )
    db_session.add(session_row)
    db_session.flush()
    return jurisdiction, session_row


def test_plan_schedule_classifies_active_jurisdiction_as_due(db_session):
    jurisdiction, _ = _make_jurisdiction_with_session(db_session, active=True, end_date=None)
    now = _real_now()
    decisions = plan_schedule(db_session, now=now)
    decision = next(d for d in decisions if d.jurisdiction_abbr == jurisdiction.abbreviation)
    assert decision.tier == CADENCE_TIER_YEAR_ROUND
    assert decision.due is True
    assert decision.last_enqueued_at is None


def test_plan_schedule_skips_jurisdiction_with_no_sessions(db_session):
    abbr = f"ZQ_NOSESS_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="No Session State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()

    decisions = plan_schedule(db_session, now=_real_now())
    assert all(d.jurisdiction_abbr != abbr for d in decisions)


def test_run_schedule_pass_enqueues_api_sync_job(db_session):
    now = _real_now()
    jurisdiction, _ = _make_jurisdiction_with_session(db_session, active=True, end_date=now.date())
    db_session.flush()

    enqueued = run_schedule_pass(db_session, now=now)
    assert jurisdiction.abbreviation in enqueued

    job = db_session.execute(
        select(IngestJob).where(
            IngestJob.kind == API_SYNC_KIND,
            IngestJob.payload["state"].astext == jurisdiction.abbreviation,
        )
    ).scalar_one()
    assert job.status == "queued"


def test_run_schedule_pass_does_not_double_enqueue_when_not_due(db_session):
    now = _real_now()
    jurisdiction, _ = _make_jurisdiction_with_session(db_session, active=True, end_date=now.date())
    db_session.flush()

    first_pass = run_schedule_pass(db_session, now=now)
    assert jurisdiction.abbreviation in first_pass

    # Re-running immediately (well within the 30-min active cadence) must
    # not enqueue a second job for the same state -- this is the whole
    # point of checking `last_enqueued_at` against the tier interval rather
    # than blindly enqueueing every pass.
    second_pass = run_schedule_pass(db_session, now=now + timedelta(minutes=5))
    assert jurisdiction.abbreviation not in second_pass


def test_run_schedule_pass_reenqueues_once_active_cadence_elapses(db_session):
    now = _real_now()
    jurisdiction, _ = _make_jurisdiction_with_session(db_session, active=True, end_date=now.date())
    db_session.flush()

    run_schedule_pass(db_session, now=now)
    later = _real_now() + timedelta(minutes=31)
    second_pass = run_schedule_pass(db_session, now=later)
    assert jurisdiction.abbreviation in second_pass


def test_run_schedule_pass_skips_when_another_pass_holds_the_advisory_lock(db_session):
    """Regression for Finding D: `run_schedule_pass` is a read-then-insert
    (plan_schedule reads the latest ingest_jobs row per state, THEN the
    caller loops inserting) with a real gap in between -- two concurrent
    callers (two worker processes' periodic schedule-refresh passes, or a
    worker's pass racing a manual `schedule-refresh` CLI run) could both
    read "not yet enqueued" for the same jurisdiction and both insert an
    api_sync job, burning the shared Open States API quota on a duplicate
    sync.

    Simulates a genuinely concurrent second holder using a SEPARATE, real
    Postgres connection (session-level advisory locks are tied to the
    backend connection, not a SQLAlchemy ORM transaction/savepoint, so this
    can't be faked with just `db_session`): that second connection acquires
    the SAME advisory lock key `run_schedule_pass` uses and holds it, then
    `run_schedule_pass` on `db_session` must find the lock unavailable and
    return an empty list WITHOUT enqueueing anything -- proving the
    read-then-insert gap can no longer be raced.
    """
    now = _real_now()
    jurisdiction, _ = _make_jurisdiction_with_session(db_session, active=True, end_date=now.date())
    db_session.flush()

    engine = get_engine()
    blocking_connection = engine.connect()
    try:
        acquired = blocking_connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEDULE_PASS_ADVISORY_LOCK_KEY}
        ).scalar_one()
        assert acquired is True, "sanity check: the blocking connection must actually hold the lock"

        enqueued = run_schedule_pass(db_session, now=now)
        assert enqueued == [], (
            "run_schedule_pass must skip the whole pass (enqueue nothing) when it cannot "
            "acquire the advisory lock, rather than racing the concurrent holder's own pass"
        )

        jobs = db_session.execute(
            select(IngestJob).where(
                IngestJob.kind == API_SYNC_KIND,
                IngestJob.payload["state"].astext == jurisdiction.abbreviation,
            )
        ).scalars().all()
        assert jobs == [], "no api_sync job must have been enqueued while the lock was held elsewhere"
    finally:
        blocking_connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEDULE_PASS_ADVISORY_LOCK_KEY}
        )
        blocking_connection.close()


def test_run_schedule_pass_ignores_dead_jobs_for_due_check(db_session):
    """A `dead` (exhausted-retries) api_sync job must not count as 'already
    scheduled' -- otherwise a permanently-broken sync for one state would
    silently stop that state from ever being re-scheduled."""
    now = _real_now()
    jurisdiction, _ = _make_jurisdiction_with_session(db_session, active=True, end_date=now.date())
    db_session.flush()

    job = queue_mod.enqueue(db_session, API_SYNC_KIND, {"state": jurisdiction.abbreviation})
    job.status = "dead"
    job.created_at = now - timedelta(minutes=1)
    db_session.flush()

    decisions = plan_schedule(db_session, now=now)
    decision = next(d for d in decisions if d.jurisdiction_abbr == jurisdiction.abbreviation)
    assert decision.due is True
    assert decision.last_enqueued_at is None
