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
