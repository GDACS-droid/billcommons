"""Postgres-backed job queue over `ingest_jobs`.

Workers claim jobs with `SELECT ... FOR UPDATE SKIP LOCKED` so multiple
worker processes can poll the same table concurrently without contending on
row locks or double-processing a job. Failed jobs get exponential backoff
via `run_after`; after `attempts` exceeds the cap the job is moved to the
terminal `dead` status (dead-letter) instead of being retried forever.

Status lifecycle: queued -> running -> done
                              -> failed (requeued w/ backoff, attempts < cap)
                              -> dead (attempts >= cap)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from billcommons_schema.models import IngestJob

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 60


def enqueue(
    db: Session,
    kind: str,
    payload: dict | None = None,
    *,
    run_after: datetime | None = None,
) -> IngestJob:
    """Insert a new queued job. Caller is responsible for committing."""
    job = IngestJob(
        kind=kind,
        payload=payload or {},
        status="queued",
        run_after=run_after or datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()
    return job


def claim_job(db: Session, worker_id: str) -> IngestJob | None:
    """Atomically claim the oldest eligible queued job for `worker_id`.

    Uses `FOR UPDATE SKIP LOCKED` so concurrent workers never block on each
    other or double-claim the same row. Returns None if nothing is eligible.
    Caller must commit (or rollback) the transaction this ran in.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(IngestJob)
        .where(IngestJob.status == "queued", IngestJob.run_after <= now)
        .order_by(IngestJob.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.execute(stmt).scalar_one_or_none()
    if job is None:
        return None

    job.status = "running"
    job.locked_by = worker_id
    job.locked_at = now
    job.attempts = job.attempts + 1
    db.flush()
    return job


def complete_job(db: Session, job: IngestJob) -> None:
    """Mark a claimed job as done. Caller must commit."""
    job.status = "done"
    job.locked_by = None
    job.locked_at = None
    job.last_error = None
    db.flush()


def fail_job(
    db: Session,
    job: IngestJob,
    error: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
) -> None:
    """Mark a claimed job as failed.

    If `attempts` (already incremented by `claim_job`) is still under
    `max_attempts`, the job is requeued with exponential backoff
    (`base_backoff_seconds * 2**(attempts-1)`). Otherwise it is moved to the
    terminal `dead` (dead-letter) status and left for manual inspection.
    Caller must commit.
    """
    job.last_error = error[:4000] if error else error
    job.locked_by = None
    job.locked_at = None

    if job.attempts >= max_attempts:
        job.status = "dead"
    else:
        job.status = "queued"
        delay = base_backoff_seconds * (2 ** (job.attempts - 1))
        job.run_after = datetime.now(timezone.utc) + timedelta(seconds=delay)
    db.flush()


def dead_letter_job(db: Session, job: IngestJob, error: str) -> None:
    """Force a job straight to the dead-letter state regardless of attempts
    (e.g. for unrecoverable/validation errors that retrying won't fix)."""
    job.status = "dead"
    job.last_error = error[:4000] if error else error
    job.locked_by = None
    job.locked_at = None
    db.flush()


def queue_depth(db: Session, status: str = "queued") -> int:
    """Count of jobs currently in `status` (default: queued-and-eligible-or-not)."""
    stmt = select(IngestJob).where(IngestJob.status == status)
    return len(db.execute(stmt).scalars().all())
