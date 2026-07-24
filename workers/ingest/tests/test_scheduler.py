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

import pytest
from sqlalchemy import select, text

from billcommons_ingest import queue as queue_mod
from billcommons_ingest import scheduler as scheduler_mod
from billcommons_ingest.scheduler import (
    CADENCE_TIER_ACTIVE,
    CADENCE_TIER_DORMANT,
    CADENCE_TIER_RECENTLY_ADJOURNED,
    CADENCE_TIER_YEAR_ROUND,
    API_SYNC_KIND,
    SCHEDULE_PASS_ADVISORY_LOCK_KEY,
    VALIDATE_ENQUEUE_ADVISORY_LOCK_KEY,
    VALIDATE_KIND,
    cadence_minutes,
    cadence_tier,
    enqueue_validation_jobs,
    is_due,
    plan_schedule,
    plan_validation,
    run_schedule_pass,
)
from billcommons_schema.models import IngestJob, Jurisdiction, JurisdictionCoverage, Session as SessionModel
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


def test_run_schedule_pass_raising_enqueue_does_not_leak_the_lock(db_session, monkeypatch):
    """Regression for Finding 3: the OLD implementation used a
    session-scoped `pg_try_advisory_lock` + a `finally`-block
    `pg_advisory_unlock` on the SAME session. If `queue_mod.enqueue` raised
    mid-pass, the transaction would abort -- and the `finally` block's own
    unlock statement, issued on that now-aborted transaction, would itself
    fail (Postgres refuses further statements until rollback), leaking the
    session-level lock on the pooled connection FOREVER. Every subsequent
    `run_schedule_pass` reusing that connection would then silently return
    `[]`, a total silent scheduling stall.

    Proves the fix (xact-scoped lock, auto-released on commit OR rollback,
    no manual unlock needed): force `queue_mod.enqueue` to raise on the
    first pass, catch it, roll back (mirroring cli.py's real error-handling
    contract for a raising DB call), and confirm a SECOND, completely fresh
    session can still acquire the lock and run a pass -- i.e. nothing was
    left behind holding it.
    """
    now = _real_now()
    jurisdiction, _ = _make_jurisdiction_with_session(db_session, active=True, end_date=now.date())
    db_session.flush()

    real_enqueue = scheduler_mod.queue_mod.enqueue
    call_count = {"n": 0}

    def _raising_once_enqueue(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated DB error mid-enqueue")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(scheduler_mod.queue_mod, "enqueue", _raising_once_enqueue)

    with pytest.raises(RuntimeError, match="simulated DB error"):
        run_schedule_pass(db_session, now=now)

    # Mirrors the real caller contract (cli.py's cmd_schedule_refresh /
    # cmd_worker both db.rollback() on any exception) -- this is what an
    # xact-scoped lock needs to release; the OLD session-scoped lock would
    # need an explicit (and, in this exact failure path, unreachable) unlock
    # call instead.
    db_session.rollback()

    engine = get_engine()
    fresh_connection = engine.connect()
    try:
        from sqlalchemy.orm import Session as OrmSession

        fresh_trans = fresh_connection.begin()
        fresh_session = OrmSession(bind=fresh_connection)
        try:
            second_jurisdiction, _ = _make_jurisdiction_with_session(
                fresh_session, active=True, end_date=now.date()
            )
            fresh_session.flush()
            enqueued = run_schedule_pass(fresh_session, now=now)
            assert second_jurisdiction.abbreviation in enqueued, (
                "a fresh session must still be able to acquire the advisory lock and "
                "run a pass after a prior pass raised -- the lock must not have leaked"
            )
        finally:
            fresh_session.close()
            fresh_trans.rollback()
    finally:
        fresh_connection.close()


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


# ---------------------------------------------------------------------------
# Validation scheduler: plan_validation / enqueue_validation_jobs
# ---------------------------------------------------------------------------


def _make_coverage_jurisdiction(db_session, *, status, bill_count=5, full_text_count=0, last_attempt_at=None, abbr=None):
    if abbr is None:
        abbr = f"ZQ_VALSCHED_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="Validate Scheduler Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db_session.add(session_row)
    db_session.flush()
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        status=status,
        bill_count=bill_count,
        full_text_count=full_text_count,
        last_attempt_at=last_attempt_at,
    )
    db_session.add(coverage)
    db_session.flush()
    return jurisdiction, coverage


def test_plan_validation_prioritizes_full_text_searchable_over_degraded_and_others(db_session):
    """Business intent: a jurisdiction that's ready to be promoted to GREEN
    (FULL_TEXT_SEARCHABLE) must be validated before anything else -- that's
    the highest-value thing the scheduler can spend a validate job on. A
    stale DEGRADED row (past the recheck window) comes next, and anything
    else still in play comes last."""
    now = _real_now()
    ready, _ = _make_coverage_jurisdiction(db_session, status="FULL_TEXT_SEARCHABLE", last_attempt_at=now)
    stale_degraded, _ = _make_coverage_jurisdiction(
        db_session, status="DEGRADED", last_attempt_at=now - timedelta(hours=7)
    )
    other, _ = _make_coverage_jurisdiction(db_session, status="VALIDATING", last_attempt_at=now - timedelta(hours=1))
    db_session.flush()

    # batch is deliberately large: this suite runs against the shared live
    # DB, which already carries dozens of real DEGRADED/VALIDATING rows that
    # would otherwise crowd our three fixtures out of a small batch cap
    # before we can even observe their relative order.
    candidates = plan_validation(db_session, batch=500, degraded_recheck_age_hours=6, now=now)
    abbrs_by_priority = [c.jurisdiction_abbr for c in candidates]

    ready_idx = abbrs_by_priority.index(ready.abbreviation)
    degraded_idx = abbrs_by_priority.index(stale_degraded.abbreviation)
    other_idx = abbrs_by_priority.index(other.abbreviation)
    assert ready_idx < degraded_idx < other_idx


def test_plan_validation_skips_degraded_row_still_within_recheck_window(db_session):
    """A DEGRADED jurisdiction re-checked an hour ago must NOT be re-queued
    again immediately (within the 6h recheck window) -- otherwise the
    scheduler would burn validate-job capacity re-hammering the same
    jurisdiction's official site instead of spreading coverage across all
    51."""
    now = _real_now()
    fresh_degraded, _ = _make_coverage_jurisdiction(
        db_session, status="DEGRADED", last_attempt_at=now - timedelta(hours=1)
    )
    db_session.flush()

    candidates = plan_validation(db_session, batch=10, degraded_recheck_age_hours=6, now=now)
    assert fresh_degraded.abbreviation not in [c.jurisdiction_abbr for c in candidates]


def test_plan_validation_skips_green_and_zero_bill_count_rows(db_session):
    now = _real_now()
    green, _ = _make_coverage_jurisdiction(db_session, status="GREEN", last_attempt_at=now - timedelta(days=30))
    empty, _ = _make_coverage_jurisdiction(db_session, status="FULL_TEXT_SEARCHABLE", bill_count=0)
    db_session.flush()

    candidates = plan_validation(db_session, batch=10, now=now)
    abbrs = [c.jurisdiction_abbr for c in candidates]
    assert green.abbreviation not in abbrs
    assert empty.abbreviation not in abbrs


def test_enqueue_validation_jobs_dedupes_against_existing_queued_validate_job(db_session):
    """The whole point of the WHERE NOT EXISTS dedupe: a jurisdiction that
    already has a queued/running validate job must not get a second one
    enqueued on top of it (would double-hit the production search API +
    official site for the same jurisdiction)."""
    now = _real_now()
    ready, _ = _make_coverage_jurisdiction(db_session, status="FULL_TEXT_SEARCHABLE")
    db_session.flush()

    queue_mod.enqueue(db_session, VALIDATE_KIND, {"jurisdiction": ready.abbreviation})
    db_session.flush()

    enqueued = enqueue_validation_jobs(db_session, batch=10, now=now)
    assert ready.abbreviation not in enqueued

    jobs = db_session.execute(
        select(IngestJob).where(
            IngestJob.kind == VALIDATE_KIND,
            IngestJob.payload["jurisdiction"].astext == ready.abbreviation,
        )
    ).scalars().all()
    assert len(jobs) == 1


def test_enqueue_validation_jobs_respects_batch_size(db_session):
    now = _real_now()
    made = [
        _make_coverage_jurisdiction(db_session, status="FULL_TEXT_SEARCHABLE", last_attempt_at=now)[0]
        for _ in range(5)
    ]
    db_session.flush()

    enqueued = enqueue_validation_jobs(db_session, batch=2, now=now)
    assert len(enqueued) == 2
    assert set(enqueued).issubset({j.abbreviation for j in made})


def test_enqueue_validation_jobs_skips_when_another_pass_holds_the_advisory_lock(db_session):
    """Mirrors run_schedule_pass's own advisory-lock regression test: a
    concurrent validation-enqueue pass holding
    VALIDATE_ENQUEUE_ADVISORY_LOCK_KEY must make this pass enqueue NOTHING
    rather than racing it (same read-then-insert gap as run_schedule_pass,
    just for `validate` jobs instead of `api_sync`)."""
    now = _real_now()
    ready, _ = _make_coverage_jurisdiction(db_session, status="FULL_TEXT_SEARCHABLE")
    db_session.flush()

    engine = get_engine()
    blocking_connection = engine.connect()
    try:
        acquired = blocking_connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": VALIDATE_ENQUEUE_ADVISORY_LOCK_KEY}
        ).scalar_one()
        assert acquired is True, "sanity check: the blocking connection must actually hold the lock"

        enqueued = enqueue_validation_jobs(db_session, batch=10, now=now)
        assert enqueued == [], (
            "enqueue_validation_jobs must skip the whole pass when it cannot acquire its "
            "advisory lock, rather than racing the concurrent holder's own pass"
        )

        jobs = db_session.execute(
            select(IngestJob).where(
                IngestJob.kind == VALIDATE_KIND,
                IngestJob.payload["jurisdiction"].astext == ready.abbreviation,
            )
        ).scalars().all()
        assert jobs == [], "no validate job must have been enqueued while the lock was held elsewhere"
    finally:
        blocking_connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": VALIDATE_ENQUEUE_ADVISORY_LOCK_KEY}
        )
        blocking_connection.close()
