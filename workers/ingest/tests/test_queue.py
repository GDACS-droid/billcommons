"""Tests for the ingest_jobs queue (SKIP LOCKED claim, backoff, dead-letter).

Business intent: two workers must never both claim the same job (that's the
entire reason FOR UPDATE SKIP LOCKED exists over a naive SELECT), backoff
must actually grow between attempts, and attempts must eventually terminate
in the `dead` status rather than retry forever.

NOTE: this suite runs against a real, shared, live-schema Postgres DB (see
conftest.py) that the production worker is concurrently claiming/completing
real ingest_jobs rows against. `claim_job` without a `kind` filter claims the
OLDEST eligible queued job in the whole table -- against a live worker that
is exactly as likely to be an unrelated production job as this test's own
fixture row, which is what made these tests flaky (asserting on `claimed.id`
or `job.status` when a completely different, real job got claimed instead).
Every test here scopes `enqueue`/`claim_job` to a fresh `unique_kind()` so it
can only ever see/claim its own fixture jobs, regardless of what else the
shared DB is doing at the same time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from billcommons_ingest.queue import claim_job, complete_job, dead_letter_job, enqueue, fail_job


def test_enqueue_and_claim_roundtrip(db_session, unique_kind):
    kind = unique_kind()
    job = enqueue(db_session, kind, {"state": "NC"})
    db_session.flush()

    claimed = claim_job(db_session, "worker-1", kind=kind)
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.locked_by == "worker-1"
    assert claimed.attempts == 1


def test_claim_ignores_jobs_not_yet_due(db_session, unique_kind):
    kind = unique_kind()
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    enqueue(db_session, kind, {"state": "NC"}, run_after=future)
    db_session.flush()

    claimed = claim_job(db_session, "worker-1", kind=kind)
    assert claimed is None


def test_claim_never_returns_an_excluded_kind(db_session, unique_kind):
    """An excluded kind is unclaimable even when it is due and eligible.

    Business intent: an api_sync job runs many rate-limited Open States calls
    while the crawl loop holds its DB session, which stalls every fetch_text
    behind it (the `idle in transaction` freeze). Claim ordering is purely by
    run_after with no kind preference, so a due api_sync WOULD otherwise win
    the claim; `exclude_kinds` is what keeps it out of the crawl loop.

    Scoped to its own `kind` (see module docstring) so it can never claim --
    or take a row lock on -- a real production job on the shared live DB.
    """
    excluded = unique_kind()
    overdue = datetime.now(timezone.utc) - timedelta(hours=1)
    enqueue(db_session, excluded, {"state": "NC"}, run_after=overdue)
    db_session.flush()

    # The exclusion check must run first: it claims nothing, so the job is
    # still queued for the eligibility check below.
    assert (
        claim_job(db_session, "worker-1", kind=excluded, exclude_kinds=(excluded,))
        is None
    )
    # ...and the job really was claimable all along, so the None above is the
    # exclusion doing the work and not an unrelated ineligibility.
    assert claim_job(db_session, "worker-1", kind=excluded) is not None


def test_crawl_worker_excludes_api_sync(db_session, unique_kind):
    """The production constant, not just the mechanism, must list api_sync."""
    from billcommons_ingest.cli import CRAWL_WORKER_EXCLUDED_KINDS
    from billcommons_ingest.scheduler import API_SYNC_KIND

    assert API_SYNC_KIND in CRAWL_WORKER_EXCLUDED_KINDS


def test_claim_skips_already_running_jobs(db_session, unique_kind):
    """Simulates the SKIP LOCKED guarantee at the application-state level:
    a job already in `running` status must not be claimable again by a
    different worker even without an active row lock in this single-session
    test (the real lock semantics are exercised by concurrent connections in
    production; here we assert the status-filter half of the contract)."""
    kind = unique_kind()
    job = enqueue(db_session, kind, {"state": "NC"})
    db_session.flush()
    claim_job(db_session, "worker-1", kind=kind)
    db_session.flush()

    second_claim = claim_job(db_session, "worker-2", kind=kind)
    assert second_claim is None, "a job already running must not be claimed again"


def test_complete_job_marks_done(db_session, unique_kind):
    kind = unique_kind()
    enqueue(db_session, kind)
    db_session.flush()
    job = claim_job(db_session, "worker-1", kind=kind)
    complete_job(db_session, job)
    db_session.flush()

    assert job.status == "done"
    assert job.locked_by is None


def test_fail_job_requeues_with_growing_backoff(db_session, unique_kind):
    kind = unique_kind()
    enqueue(db_session, kind, {"state": "NC"})
    db_session.flush()

    job = claim_job(db_session, "worker-1", kind=kind)
    before_fail_run_after = job.run_after
    fail_job(db_session, job, "boom", base_backoff_seconds=10)
    db_session.flush()

    assert job.status == "queued"
    assert job.run_after > before_fail_run_after
    first_delay = (job.run_after - datetime.now(timezone.utc)).total_seconds()

    # Second attempt: backoff should have grown (exponential).
    job.run_after = datetime.now(timezone.utc)  # make it claimable again
    db_session.flush()
    job2 = claim_job(db_session, "worker-1", kind=kind)
    assert job2.id == job.id
    assert job2.attempts == 2
    fail_job(db_session, job2, "boom again", base_backoff_seconds=10)
    db_session.flush()
    second_delay = (job2.run_after - datetime.now(timezone.utc)).total_seconds()

    assert second_delay > first_delay, "backoff must grow between successive failures"


def test_fail_job_dead_letters_after_max_attempts(db_session, unique_kind):
    kind = unique_kind()
    job = enqueue(db_session, kind, {"state": "NC"})
    db_session.flush()

    for _ in range(5):
        job.run_after = datetime.now(timezone.utc)
        db_session.flush()
        claimed = claim_job(db_session, "worker-1", kind=kind)
        fail_job(db_session, claimed, "still failing", max_attempts=5, base_backoff_seconds=0)
        db_session.flush()

    assert job.status == "dead", "job must be dead-lettered once attempts reach the cap"


def test_dead_letter_job_is_immediate(db_session, unique_kind):
    kind = unique_kind()
    job = enqueue(db_session, kind, {"state": "NC"})
    db_session.flush()
    claimed = claim_job(db_session, "worker-1", kind=kind)
    dead_letter_job(db_session, claimed, "unrecoverable")
    db_session.flush()

    assert claimed.status == "dead"
    assert claimed.last_error == "unrecoverable"


def test_count_claimable_ignores_jobs_parked_on_backoff(db_session, unique_kind):
    """The crawl's top-up gate asks "am I about to run out of work?" -- and a
    job whose backoff puts it minutes in the future is NOT work available now.
    Counting it anyway is what let a queue full of failing-and-backing-off jobs
    read as a healthy backlog, so the top-up never fired and the crawl sat at
    zero throughput with a full-looking queue.
    """
    from billcommons_ingest.queue import count_claimable, count_queued

    kind = unique_kind()
    ready = enqueue(db_session, kind, {"n": 1})
    parked = enqueue(db_session, kind, {"n": 2})
    parked.run_after = datetime.now(timezone.utc) + timedelta(minutes=30)
    db_session.flush()

    assert count_queued(db_session, kind) == 2, "both rows are queued"
    assert count_claimable(db_session, kind) == 1, (
        "only the job whose run_after has arrived is real, available backlog"
    )
    assert ready.status == "queued" and parked.status == "queued"


def test_permanently_failing_job_dies_even_though_the_claim_is_rolled_back(unique_kind):
    """A job that always fails must reach the dead-letter cap.

    This walks the worker's REAL failure path: claim in one session, roll that
    session back (which is what discards the claim's uncommitted attempts
    increment), then record the failure in a fresh session via
    record_job_failure. If the post-claim attempt count isn't carried across
    that boundary, every retry re-reads attempts=0, the job requeues forever,
    and -- because it stays `queued` -- it also keeps the queue above the
    top-up floor so no new work is ever enqueued. That combination took the
    crawl to exactly zero throughput on 2026-07-25 while the worker looked
    busy. Uses committing sessions (not the rollback fixture) because the
    session boundary is the thing under test.
    """
    from billcommons_ingest.cli import record_job_failure
    from billcommons_schema.models import IngestJob
    from billcommons_shared.db import get_session

    kind = unique_kind()
    setup = get_session()
    try:
        job = enqueue(setup, kind, {"doc": "always-fails"})
        setup.commit()
        job_id = job.id
    finally:
        setup.close()

    try:
        for _ in range(10):
            claiming = get_session()
            try:
                claimed = claim_job(claiming, "worker-under-test", kind=kind)
                if claimed is None:
                    break
                # Read the count while the uncommitted increment is still live,
                # exactly as the worker loop does.
                claimed_attempts = claimed.attempts
                claiming.rollback()
            finally:
                claiming.close()

            record_job_failure(
                job_id,
                IngestJob,
                claimed_attempts=claimed_attempts,
                error="permanent failure",
                session_factory=get_session,
            )

            # Skip past the backoff so the next iteration can claim it.
            bump = get_session()
            try:
                row = bump.get(IngestJob, job_id)
                if row.status != "queued":
                    break
                row.run_after = datetime.now(timezone.utc)
                bump.commit()
            finally:
                bump.close()

        check = get_session()
        try:
            final = check.get(IngestJob, job_id)
            assert final.status == "dead", (
                f"a job that always fails must dead-letter, got status={final.status!r} "
                f"attempts={final.attempts} -- if attempts is 0 the claim's increment "
                f"was lost across the rollback and this job would retry forever"
            )
            assert final.attempts >= 5
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            row = cleanup.get(IngestJob, job_id)
            if row is not None:
                cleanup.delete(row)
                cleanup.commit()
        finally:
            cleanup.close()
