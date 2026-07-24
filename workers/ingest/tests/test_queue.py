"""Tests for the ingest_jobs queue (SKIP LOCKED claim, backoff, dead-letter).

Business intent: two workers must never both claim the same job (that's the
entire reason FOR UPDATE SKIP LOCKED exists over a naive SELECT), backoff
must actually grow between attempts, and attempts must eventually terminate
in the `dead` status rather than retry forever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from billcommons_ingest.queue import claim_job, complete_job, dead_letter_job, enqueue, fail_job
from billcommons_schema.models import IngestJob


def test_enqueue_and_claim_roundtrip(db_session):
    job = enqueue(db_session, "bootstrap", {"state": "NC"})
    db_session.flush()

    claimed = claim_job(db_session, "worker-1")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.locked_by == "worker-1"
    assert claimed.attempts == 1


def test_claim_ignores_jobs_not_yet_due(db_session):
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    enqueue(db_session, "bootstrap", {"state": "NC"}, run_after=future)
    db_session.flush()

    claimed = claim_job(db_session, "worker-1")
    assert claimed is None


def test_claim_skips_already_running_jobs(db_session):
    """Simulates the SKIP LOCKED guarantee at the application-state level:
    a job already in `running` status must not be claimable again by a
    different worker even without an active row lock in this single-session
    test (the real lock semantics are exercised by concurrent connections in
    production; here we assert the status-filter half of the contract)."""
    job = enqueue(db_session, "bootstrap", {"state": "NC"})
    db_session.flush()
    claim_job(db_session, "worker-1")
    db_session.flush()

    second_claim = claim_job(db_session, "worker-2")
    assert second_claim is None, "a job already running must not be claimed again"


def test_complete_job_marks_done(db_session):
    enqueue(db_session, "recompute-coverage")
    db_session.flush()
    job = claim_job(db_session, "worker-1")
    complete_job(db_session, job)
    db_session.flush()

    assert job.status == "done"
    assert job.locked_by is None


def test_fail_job_requeues_with_growing_backoff(db_session):
    enqueue(db_session, "bootstrap", {"state": "NC"})
    db_session.flush()

    job = claim_job(db_session, "worker-1")
    before_fail_run_after = job.run_after
    fail_job(db_session, job, "boom", base_backoff_seconds=10)
    db_session.flush()

    assert job.status == "queued"
    assert job.run_after > before_fail_run_after
    first_delay = (job.run_after - datetime.now(timezone.utc)).total_seconds()

    # Second attempt: backoff should have grown (exponential).
    job.run_after = datetime.now(timezone.utc)  # make it claimable again
    db_session.flush()
    job2 = claim_job(db_session, "worker-1")
    assert job2.id == job.id
    assert job2.attempts == 2
    fail_job(db_session, job2, "boom again", base_backoff_seconds=10)
    db_session.flush()
    second_delay = (job2.run_after - datetime.now(timezone.utc)).total_seconds()

    assert second_delay > first_delay, "backoff must grow between successive failures"


def test_fail_job_dead_letters_after_max_attempts(db_session):
    job = enqueue(db_session, "bootstrap", {"state": "NC"})
    db_session.flush()

    for _ in range(5):
        job.run_after = datetime.now(timezone.utc)
        db_session.flush()
        claimed = claim_job(db_session, "worker-1")
        fail_job(db_session, claimed, "still failing", max_attempts=5, base_backoff_seconds=0)
        db_session.flush()

    assert job.status == "dead", "job must be dead-lettered once attempts reach the cap"


def test_dead_letter_job_is_immediate(db_session):
    job = enqueue(db_session, "bootstrap", {"state": "NC"})
    db_session.flush()
    claimed = claim_job(db_session, "worker-1")
    dead_letter_job(db_session, claimed, "unrecoverable")
    db_session.flush()

    assert claimed.status == "dead"
    assert claimed.last_error == "unrecoverable"
