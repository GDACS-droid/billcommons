"""One durable Scout admission path shared by the API and Scout scheduler.

Callers authenticate and apply rollout policy.  This module owns the customer
row lock, cache/coalescing lookup, platform advisory lock, and all quota and
reservation decisions before it ever constructs a queued job.
"""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from billcommons_schema.models import (
    ApiCustomer,
    ScoutBrowserSession,
    ScoutRawBlob,
    ScoutResearchJob,
)
from billcommons_shared.scout import CALIFORNIA, ScoutSettings

_PLATFORM_ADMISSION_LOCK_KEY = 81_420_902
_sqlite_platform_admission_lock = threading.RLock()


class ScoutAdmissionError(RuntimeError):
    """A stable, non-sensitive refusal suitable for API or scheduler handling."""

    def __init__(self, code: str, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class ScoutAdmission:
    job: ScoutResearchJob
    created: bool
    coalesced: bool
    cached: bool


def customer_is_admitted(customer: ApiCustomer, settings: ScoutSettings) -> bool:
    """Whether a customer remains within the current controlled rollout."""
    if settings.canary_emails:
        return customer.email.strip().casefold() in settings.canary_emails
    return settings.allow_public_rollout


@contextmanager
def _platform_admission_lock(db: Session):
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PLATFORM_ADMISSION_LOCK_KEY})
        yield
        return
    with _sqlite_platform_admission_lock:
        yield


def _browser_limit_seconds(job: ScoutResearchJob, name: str, fallback: int) -> int:
    value = (job.limits or {}).get(name, fallback)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback


def _has_persisted_browser_cleanup_limit(job: ScoutResearchJob) -> bool:
    value = (job.limits or {}).get("browser_cleanup_seconds")
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _browser_session_reservation_ms(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    return (
        _browser_limit_seconds(job, "browser_wall_seconds", settings.browser_wall_seconds)
        + 2 * _browser_limit_seconds(job, "browser_cleanup_seconds", settings.browser_cleanup_seconds)
    ) * 1000


def _browser_cleanup_reservation_ms(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    return _browser_limit_seconds(job, "browser_cleanup_seconds", settings.browser_cleanup_seconds) * 1000


def _browser_reservation_ms(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    if not _has_persisted_browser_cleanup_limit(job):
        return settings.per_customer_daily_browser_seconds * 1000
    limits = job.limits or {}
    max_requests = limits.get("max_external_requests", settings.max_external_requests)
    if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests <= 0:
        max_requests = settings.max_external_requests
    reconciled = max_requests * _browser_session_reservation_ms(job, settings)
    reservation = limits.get("daily_browser_reservation_ms")
    if isinstance(reservation, int) and not isinstance(reservation, bool) and reservation > 0:
        return max(reservation, reconciled)
    return reconciled


def _browser_budget_totals(
    db: Session,
    settings: ScoutSettings,
    day_start: datetime,
    *,
    customer_id: uuid.UUID | None = None,
) -> tuple[list[ScoutResearchJob], int, int]:
    job_scope = [] if customer_id is None else [ScoutResearchJob.customer_id == customer_id]
    active_jobs = list(db.scalars(select(ScoutResearchJob).where(
        *job_scope, ScoutResearchJob.status.in_(("queued", "running"))
    )).all())
    reserved_job_ids = {job.id for job in active_jobs}
    live_session_stmt = select(ScoutBrowserSession.job_id).join(
        ScoutResearchJob, ScoutResearchJob.id == ScoutBrowserSession.job_id
    ).where(*job_scope, ScoutBrowserSession.status.in_(("starting", "running", "cleanup_failed", "reaping")))
    reserved_job_ids.update(db.scalars(live_session_stmt).all())
    terminal_stmt = select(ScoutBrowserSession, ScoutResearchJob).join(
        ScoutResearchJob, ScoutResearchJob.id == ScoutBrowserSession.job_id
    ).where(*job_scope, or_(
        ScoutBrowserSession.created_at >= day_start,
        ScoutBrowserSession.released_at >= day_start,
        and_(ScoutBrowserSession.provider_session_id.is_not(None), ScoutBrowserSession.runtime_ms.is_(None)),
    ))
    if reserved_job_ids:
        terminal_stmt = terminal_stmt.where(ScoutResearchJob.id.not_in(reserved_job_ids))
    terminal_sessions = db.execute(terminal_stmt).all()
    daily_browser_ms = sum(
        settings.per_customer_daily_browser_seconds * 1000
        if session.provider_session_id and not _has_persisted_browser_cleanup_limit(job)
        else session.runtime_ms + _browser_cleanup_reservation_ms(job, settings)
        if session.runtime_ms is not None and session.provider_session_id
        else _browser_session_reservation_ms(job, settings)
        if session.provider_session_id
        else _browser_session_reservation_ms(job, settings)
        if (session.error_class or "").startswith("create_outcome_unknown")
        else 0
        for session, job in terminal_sessions
    )
    reserved_browser_ms = sum(_browser_reservation_ms(job, settings) for job in db.scalars(
        select(ScoutResearchJob).where(ScoutResearchJob.id.in_(reserved_job_ids))
    ).all()) if reserved_job_ids else 0
    return active_jobs, daily_browser_ms, reserved_browser_ms


def _rawstore_reservation_bytes(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    limits = job.limits or {}
    requests = limits.get("max_external_requests", settings.max_external_requests)
    direct_bytes = limits.get("max_direct_bytes", settings.max_direct_bytes)
    if not isinstance(requests, int) or isinstance(requests, bool) or requests <= 0:
        requests = settings.max_external_requests
    if not isinstance(direct_bytes, int) or isinstance(direct_bytes, bool) or direct_bytes <= 0:
        direct_bytes = settings.max_direct_bytes
    return requests * direct_bytes


def _retained_rawstore_bytes(db: Session) -> int:
    size = func.octet_length(ScoutRawBlob.data)
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        size = func.length(ScoutRawBlob.data)
    return int(db.scalar(select(func.coalesce(func.sum(size), 0))) or 0)


def _limits(settings: ScoutSettings) -> dict:
    reservation = settings.max_external_requests * (
        settings.browser_wall_seconds + 2 * settings.browser_cleanup_seconds
    ) * 1000
    return {
        "max_pages": settings.max_pages,
        "max_actions": settings.max_actions,
        "max_external_requests": settings.max_external_requests,
        "max_related_documents": settings.max_related_documents,
        "max_related_vote_records": settings.max_related_vote_records,
        "max_direct_bytes": settings.max_direct_bytes,
        "max_pdf_pages": settings.max_pdf_pages,
        "max_pdf_text_chars": settings.max_pdf_text_chars,
        "max_pdf_extract_seconds": settings.max_pdf_extract_seconds,
        "max_pdf_extract_memory_bytes": settings.max_pdf_extract_memory_bytes,
        "max_pdf_extract_cpu_seconds": settings.max_pdf_extract_cpu_seconds,
        "max_ca_parse_seconds": settings.max_ca_parse_seconds,
        "max_routed_requests": settings.max_browser_routed_requests,
        "max_retries": settings.max_retries,
        "daily_jobs": settings.per_customer_daily_jobs,
        "daily_browser_seconds": settings.per_customer_daily_browser_seconds,
        "browser_wall_seconds": settings.browser_wall_seconds,
        "browser_cleanup_seconds": settings.browser_cleanup_seconds,
        "daily_browser_reservation_ms": reservation,
    }


def admit_scout_job(
    db: Session,
    customer: ApiCustomer,
    *,
    original_query: str,
    normalized_query: str,
    jurisdiction: str,
    cache_key: str,
    settings: ScoutSettings,
) -> ScoutAdmission:
    """Coalesce/cache or atomically admit one new Scout job without committing.

    The caller owns the transaction boundary so a scheduler can journal its run
    association with the admission.  This is the sole supported path for new
    non-operator Scout queue rows.
    """
    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer.id).with_for_update())
    existing = db.execute(select(ScoutResearchJob).where(
        ScoutResearchJob.customer_id == customer.id,
        ScoutResearchJob.cache_key == cache_key,
        ScoutResearchJob.status.in_(("queued", "running")),
    )).scalar_one_or_none()
    if existing is not None:
        return ScoutAdmission(existing, created=False, coalesced=True, cached=False)
    now = datetime.now(timezone.utc)
    fresh = db.execute(select(ScoutResearchJob).where(
        ScoutResearchJob.customer_id == customer.id,
        ScoutResearchJob.cache_key == cache_key,
        ScoutResearchJob.status.in_(("completed", "partial")),
        ScoutResearchJob.fresh_until > now,
    ).order_by(ScoutResearchJob.completed_at.desc()).limit(1)).scalar_one_or_none()
    if fresh is not None:
        return ScoutAdmission(fresh, created=False, coalesced=True, cached=True)

    with _platform_admission_lock(db):
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        active_jobs, daily_browser_ms, reserved_browser_ms = _browser_budget_totals(
            db, settings, day_start, customer_id=customer.id
        )
        platform_active, platform_daily_browser_ms, platform_reserved_browser_ms = _browser_budget_totals(
            db, settings, day_start
        )
        daily_jobs = db.scalar(select(func.count()).select_from(ScoutResearchJob).where(
            ScoutResearchJob.customer_id == customer.id, ScoutResearchJob.created_at >= day_start
        )) or 0
        platform_daily_jobs = db.scalar(select(func.count()).select_from(ScoutResearchJob).where(
            ScoutResearchJob.created_at >= day_start
        )) or 0
        new_reservation_ms = settings.max_external_requests * (
            settings.browser_wall_seconds + 2 * settings.browser_cleanup_seconds
        ) * 1000
        new_rawstore_reservation = settings.max_external_requests * settings.max_direct_bytes
        if len(platform_active) >= settings.platform_max_active_jobs:
            raise ScoutAdmissionError("scout_platform_active_job_limit", "Scout is at platform capacity.", 60)
        retained_rawstore_bytes = _retained_rawstore_bytes(db)
        active_rawstore_reservations = sum(_rawstore_reservation_bytes(job, settings) for job in platform_active)
        if retained_rawstore_bytes + active_rawstore_reservations + new_rawstore_reservation > settings.max_retained_rawstore_bytes:
            raise ScoutAdmissionError("scout_rawstore_capacity_limit", "Scout evidence capacity reached.", 3600)
        if platform_daily_jobs >= settings.platform_max_daily_jobs:
            raise ScoutAdmissionError("scout_platform_daily_job_limit", "Scout daily platform capacity reached.", 3600)
        if platform_daily_browser_ms + platform_reserved_browser_ms + new_reservation_ms > settings.platform_max_daily_browser_seconds * 1000:
            raise ScoutAdmissionError("scout_platform_daily_browser_limit", "Scout browser capacity reached.", 3600)
        if daily_jobs >= settings.per_customer_daily_jobs:
            raise ScoutAdmissionError("scout_daily_job_limit", "Daily Scout job limit reached.", 3600)
        if daily_browser_ms + reserved_browser_ms + new_reservation_ms > settings.per_customer_daily_browser_seconds * 1000:
            raise ScoutAdmissionError("scout_daily_browser_limit", "Daily Scout browser budget reached.", 3600)
        if len(active_jobs) >= settings.per_customer_active_jobs:
            raise ScoutAdmissionError("scout_active_job_limit", "Too many active Scout jobs.", 60)
        is_california_retained = jurisdiction == CALIFORNIA
        job = ScoutResearchJob(
            customer_id=customer.id,
            original_query=original_query.strip(),
            normalized_query=normalized_query,
            jurisdiction=jurisdiction,
            cache_key=cache_key,
            strategy=(
                {"adapter": "california_retained_p0", "mode": "retained_official_archive"}
                if is_california_retained
                else {"adapter": "florida_p0", "mode": "structured_first"}
            ),
            limits=_limits(settings),
            usage={},
        )
        try:
            with db.begin_nested():
                db.add(job)
                db.flush()
        except IntegrityError:
            existing = db.execute(select(ScoutResearchJob).where(
                ScoutResearchJob.customer_id == customer.id,
                ScoutResearchJob.cache_key == cache_key,
                ScoutResearchJob.status.in_(("queued", "running")),
            )).scalar_one_or_none()
            if existing is None:
                raise
            return ScoutAdmission(existing, created=False, coalesced=True, cached=False)
    return ScoutAdmission(job, created=True, coalesced=False, cached=False)
