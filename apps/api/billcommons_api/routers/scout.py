"""Authenticated, owner-scoped API for the Scout durable research queue.

This router deliberately imports shared contracts and schema models only.  The
Scout worker and provider modules are not present in the API container.
"""
from __future__ import annotations

import uuid
import inspect
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from urllib.parse import urlsplit

from billcommons_api.deps import get_db
from billcommons_api.errors import not_found, too_many_requests
from billcommons_api.routers.account import _check_origin, _require_session
from billcommons_schema.models import (
    ApiCustomer,
    ScoutBrowserSession,
    ScoutFinding,
    ScoutJobEvent,
    ScoutRawBlob,
    ScoutResearchJob,
    ScoutSource,
)
from billcommons_shared.scout import ScoutPolicyError, ScoutSettings, normalize_jurisdiction, normalize_query, scout_cache_key

router = APIRouter(prefix="/scout", tags=["scout"])

_PLATFORM_ADMISSION_LOCK_KEY = 81_420_902
_sqlite_platform_admission_lock = threading.RLock()


class CreateScoutJob(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    jurisdiction: str = Field(default="FL", min_length=2, max_length=2)


def _enabled() -> ScoutSettings:
    return ScoutSettings.from_env()


def _require_enabled() -> ScoutSettings:
    settings = _enabled()
    if not settings.enabled:
        # A disabled dark-launch surface must not advertise itself or queue work.
        raise not_found("scout_disabled", "Scout is not enabled.")
    return settings


def _require_canary(customer: ApiCustomer, settings: ScoutSettings) -> None:
    """Admit new work only for the configured private-canary cohort.

    Existing owner-scoped jobs remain readable/cancelable if a customer is
    later removed from the cohort; rollout controls must not strand retained
    evidence or weaken ownership checks.
    """
    if settings.canary_emails and customer.email.strip().casefold() not in settings.canary_emails:
        raise not_found("scout_not_available", "Scout is not available for this account.")
    if not settings.canary_emails and not settings.allow_public_rollout:
        # Enabling the worker/API flag alone must never accidentally expose an
        # unbounded all-account rollout. Public expansion is a separate,
        # deliberate capacity/cost decision.
        raise not_found("scout_canary_not_configured", "Scout is not available for this account.")


def _job_for_owner(db: Session, customer: ApiCustomer, job_id: uuid.UUID, *, lock: bool = False) -> ScoutResearchJob:
    stmt = select(ScoutResearchJob).where(
        ScoutResearchJob.id == job_id, ScoutResearchJob.customer_id == customer.id
    )
    if lock:
        stmt = stmt.with_for_update()
    job = db.execute(stmt).scalar_one_or_none()
    if job is None:
        # 404 avoids confirming that another account owns a guessed UUID.
        raise not_found("scout_job_not_found", "Scout job was not found.")
    return job


def _browser_limit_seconds(job: ScoutResearchJob, name: str, fallback: int) -> int:
    limits = job.limits or {}
    value = limits.get(name, fallback)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback


def _has_persisted_browser_cleanup_limit(job: ScoutResearchJob) -> bool:
    value = (job.limits or {}).get("browser_cleanup_seconds")
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _browser_session_reservation_ms(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    """Bound one provider session: drive plus provider and runner cleanup."""
    wall_seconds = _browser_limit_seconds(job, "browser_wall_seconds", settings.browser_wall_seconds)
    cleanup_seconds = _browser_limit_seconds(job, "browser_cleanup_seconds", settings.browser_cleanup_seconds)
    return (wall_seconds + 2 * cleanup_seconds) * 1000


def _browser_cleanup_reservation_ms(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    return _browser_limit_seconds(job, "browser_cleanup_seconds", settings.browser_cleanup_seconds) * 1000


def _browser_reservation_ms(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    """Return the browser capacity durably reserved for one nonterminal job.

    A Scout job can issue at most ``max_external_requests`` browser captures,
    each bounded by its wall-clock limit.  New jobs persist that bound so a
    later settings change cannot alter an already-admitted reservation.  The
    fallback keeps pre-reservation jobs conservative during a rolling deploy.
    """
    if not _has_persisted_browser_cleanup_limit(job):
        # Legacy rows were admitted before cleanup was persisted. The worker
        # may use its local validated setting to finish them, but API admission
        # must not assume a mutable rollout value: hold the entire daily cap so
        # no new browser work can overlap that unbounded legacy cleanup.
        return settings.per_customer_daily_browser_seconds * 1000
    limits = job.limits or {}
    max_requests = limits.get("max_external_requests", settings.max_external_requests)
    if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests <= 0:
        max_requests = settings.max_external_requests
    reconciled = max_requests * _browser_session_reservation_ms(job, settings)
    reservation = limits.get("daily_browser_reservation_ms")
    if isinstance(reservation, int) and not isinstance(reservation, bool) and reservation > 0:
        # Never let a stale/corrupt aggregate lower the execution bounds
        # frozen alongside it; a larger historical reservation stays held.
        return max(reservation, reconciled)
    return reconciled


@contextmanager
def _platform_admission_lock(db: Session):
    """Serialize platform-wide check-and-create decisions.

    PostgreSQL owns the production lock inside the caller's transaction. The
    in-process lock only supplies equivalent deterministic behavior for the
    SQLite unit-test path; it is never relied on between production replicas.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PLATFORM_ADMISSION_LOCK_KEY})
        yield
        return
    with _sqlite_platform_admission_lock:
        yield


def _browser_budget_totals(
    db: Session,
    settings: ScoutSettings,
    day_start: datetime,
    *,
    customer_id: uuid.UUID | None = None,
) -> tuple[list[ScoutResearchJob], int, int]:
    """Return active jobs plus actual and durably reserved browser milliseconds."""
    job_scope = [] if customer_id is None else [ScoutResearchJob.customer_id == customer_id]
    active_jobs = list(
        db.scalars(
            select(ScoutResearchJob).where(
                *job_scope,
                ScoutResearchJob.status.in_(("queued", "running")),
            )
        ).all()
    )
    reserved_job_ids = {job.id for job in active_jobs}
    live_session_stmt = (
        select(ScoutBrowserSession.job_id)
        .join(ScoutResearchJob, ScoutResearchJob.id == ScoutBrowserSession.job_id)
        .where(
            *job_scope,
            ScoutBrowserSession.status.in_(("starting", "running", "cleanup_failed", "reaping")),
        )
    )
    reserved_job_ids.update(db.scalars(live_session_stmt).all())

    terminal_stmt = (
        select(ScoutBrowserSession, ScoutResearchJob)
        .join(ScoutResearchJob, ScoutResearchJob.id == ScoutBrowserSession.job_id)
        .where(
            *job_scope,
            or_(
                ScoutBrowserSession.created_at >= day_start,
                ScoutBrowserSession.released_at >= day_start,
                and_(
                    ScoutBrowserSession.provider_session_id.is_not(None),
                    ScoutBrowserSession.runtime_ms.is_(None),
                ),
            ),
        )
    )
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
    reserved_browser_ms = sum(
        _browser_reservation_ms(job, settings)
        for job in db.scalars(
            select(ScoutResearchJob).where(ScoutResearchJob.id.in_(reserved_job_ids))
        ).all()
    ) if reserved_job_ids else 0
    return active_jobs, daily_browser_ms, reserved_browser_ms


def _rawstore_reservation_bytes(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    """Bound future immutable evidence for a queued/running job.

    Legacy jobs did not persist all limits, so retain the current process
    ceilings rather than treating missing JSON fields as free capacity.
    """
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


def _job_payload(db: Session, job: ScoutResearchJob) -> dict:
    findings = db.scalar(select(func.count()).select_from(ScoutFinding).where(ScoutFinding.job_id == job.id))
    browser_sessions = list(
        db.execute(select(ScoutBrowserSession).where(ScoutBrowserSession.job_id == job.id)).scalars()
    )
    usage = dict(job.usage or {})
    # Older jobs may have browser-session ledger rows without the aggregate
    # heartbeat field.  A provider ID proves the paid session actually started;
    # an unstarted reservation must not be reported as a zero-request session.
    if "browser_routed_requests" not in usage:
        started_sessions = [session for session in browser_sessions if session.provider_session_id]
        if started_sessions:
            usage["browser_routed_requests"] = sum(session.routed_requests for session in started_sessions)
    fresh_until = job.fresh_until
    if fresh_until is not None and fresh_until.tzinfo is None:
        # SQLite test/dev rows do not round-trip timezone info.  Stored Scout
        # timestamps are UTC, so compare them on the same basis as Postgres.
        fresh_until = fresh_until.replace(tzinfo=timezone.utc)
    payload = {
        "id": str(job.id),
        "query": job.original_query,
        "jurisdiction": job.jurisdiction,
        "status": job.status,
        "partial_success": job.partial_success,
        "error_class": job.error_class,
        "cancel_version": job.cancel_version,
        "strategy": (job.strategy or {}).get("mode", "structured_first"),
        "strategy_detail": job.strategy or {},
        "cache_status": "fresh" if fresh_until and fresh_until > datetime.now(timezone.utc) else "miss",
        "usage": {
            "external_requests": int(usage.get("external_requests", usage.get("direct_requests", 0))),
            "browser_sessions": int(usage.get("browser_sessions", 0)),
            "browser_pages": int(usage.get("browser_pages", 0)),
            "browser_actions": int(usage.get("browser_actions", 0)),
        },
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "finding_count": findings or 0,
    }
    if "browser_routed_requests" in usage:
        payload["usage"]["browser_routed_requests"] = int(usage["browser_routed_requests"])
    sources = {
        source.id: source
        for source in db.execute(select(ScoutSource).where(ScoutSource.job_id == job.id)).scalars()
    }
    prior_ids = {source.prior_source_id for source in sources.values() if source.prior_source_id}
    prior_sources: dict[uuid.UUID, tuple[ScoutSource, ScoutResearchJob]] = {}
    if prior_ids:
        # The descriptor is inspectable provenance, but only when the prior
        # source belongs to this same authenticated customer. A malformed or
        # legacy cross-tenant pointer remains opaque.
        prior_sources = {
            prior_source.id: (prior_source, prior_job)
            for prior_source, prior_job in db.execute(
                select(ScoutSource, ScoutResearchJob)
                .join(ScoutResearchJob, ScoutResearchJob.id == ScoutSource.job_id)
                .where(
                    ScoutSource.id.in_(prior_ids),
                    ScoutResearchJob.customer_id == job.customer_id,
                )
            ).all()
        }

    def prior_descriptor(source: ScoutSource) -> dict | None:
        prior = prior_sources.get(source.prior_source_id)
        if prior is None:
            return None
        prior_source, prior_job = prior
        return {
            "job_id": str(prior_job.id),
            "canonical_url": prior_source.canonical_url,
            "content_hash": prior_source.content_hash,
            "retrieved_at": prior_source.retrieved_at.isoformat() if prior_source.retrieved_at else None,
        }
    payload["events"] = [
        {"id": str(event.id), "kind": event.kind, "message": event.kind.replace("_", " "), "detail": event.detail, "created_at": event.created_at.isoformat()}
        for event in db.execute(
            select(ScoutJobEvent).where(ScoutJobEvent.job_id == job.id).order_by(ScoutJobEvent.created_at)
        ).scalars()
    ]
    payload["sources"] = [
        {"id": str(source.id), "url": source.canonical_url, "title": source.title, "domain": urlsplit(source.canonical_url).hostname, "official_domain": urlsplit(source.canonical_url).hostname, "official": source.official,
         "mechanism": source.retrieval_mechanism, "status": source.http_status,
         "type": source.mime_type, "mime_type": source.mime_type, "content_hash": source.content_hash,
         "prior_source_id": str(source.prior_source_id) if source.prior_source_id else None,
         "prior_source": prior_descriptor(source),
         "change_kind": source.change_kind, "change_summary": source.change_summary,
         "retrieved_at": source.retrieved_at.isoformat() if source.retrieved_at else None}
        for source in sources.values()
    ]
    payload["findings"] = [
        {"id": str(finding.id), "title": finding.title, "what_happened": finding.what_happened,
         "why_it_matters": finding.why_it_matters, "excerpt": finding.excerpt,
         "excerpt_hash": finding.excerpt_hash, "confidence": finding.confidence, "source_id": str(finding.source_id),
         "relevant_date": finding.relevant_date.isoformat() if finding.relevant_date else None,
         "bill_id": str(finding.bill_id) if finding.bill_id else None,
         "source_url": sources.get(finding.source_id).canonical_url if finding.source_id in sources else None}
        for finding in db.execute(select(ScoutFinding).where(ScoutFinding.job_id == job.id)).scalars()
    ]
    payload["browser_sessions"] = [
        {"id": str(session.id), "status": session.status, "pages": session.pages,
         "actions": session.actions, "runtime_ms": session.runtime_ms,
         "routed_requests": session.routed_requests,
         "replay_available": session.status == "released" and bool(session.replay_url)}
        for session in browser_sessions
    ]
    return payload


@router.post("/jobs", status_code=201)
def create_job(
    body: CreateScoutJob,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    settings = _require_enabled()
    _check_origin(request)
    customer = _require_session(request, db)
    _require_canary(customer, settings)
    try:
        jurisdiction = normalize_jurisdiction(body.jurisdiction)
        normalized = normalize_query(body.query, max_chars=settings.max_query_chars)
    except ScoutPolicyError as exc:
        # The API validation contract need not disclose policy implementation.
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail={"code": "invalid_scout_request", "message": str(exc)}) from exc
    key = scout_cache_key(body.query, jurisdiction)

    # Serializes a customer's quota/check-and-create decision on Postgres.
    # The partial cache-key index remains the authority for equivalent jobs.
    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer.id).with_for_update())

    # An equivalent request is allowed to coalesce even when the owner has
    # exhausted their unrelated active-job budget.
    existing = db.execute(
        select(ScoutResearchJob).where(
            ScoutResearchJob.customer_id == customer.id,
            ScoutResearchJob.cache_key == key,
            ScoutResearchJob.status.in_(("queued", "running")),
        )
    ).scalar_one_or_none()
    if existing is not None:
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return {"coalesced": True, "job": _job_payload(db, existing)}
    fresh = db.execute(
        select(ScoutResearchJob).where(
            ScoutResearchJob.customer_id == customer.id,
            ScoutResearchJob.cache_key == key,
            ScoutResearchJob.status.in_(("completed", "partial")),
            ScoutResearchJob.fresh_until > datetime.now(timezone.utc),
        ).order_by(ScoutResearchJob.completed_at.desc()).limit(1)
    ).scalar_one_or_none()
    if fresh is not None:
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return {"coalesced": True, "cached": True, "cache_hit": True, "job": _job_payload(db, fresh)}

    # The PostgreSQL advisory xact lock makes platform-wide aggregates a
    # check-and-create decision rather than a best-effort observation. The
    # customer row lock above keeps same-owner cache/coalescing race-safe
    # without charging those free reads against the global lock.
    with _platform_admission_lock(db):
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        active_jobs, daily_browser_ms, reserved_browser_ms = _browser_budget_totals(
            db, settings, day_start, customer_id=customer.id
        )
        active_count = len(active_jobs)
        platform_active, platform_daily_browser_ms, platform_reserved_browser_ms = _browser_budget_totals(
            db, settings, day_start
        )
        daily_jobs = db.scalar(
            select(func.count()).select_from(ScoutResearchJob).where(
                ScoutResearchJob.customer_id == customer.id,
                ScoutResearchJob.created_at >= day_start,
            )
        ) or 0
        platform_daily_jobs = db.scalar(
            select(func.count()).select_from(ScoutResearchJob).where(
                ScoutResearchJob.created_at >= day_start,
            )
        ) or 0
        new_reservation_ms = settings.max_external_requests * (
            settings.browser_wall_seconds + 2 * settings.browser_cleanup_seconds
        ) * 1000
        new_rawstore_reservation = settings.max_external_requests * settings.max_direct_bytes
        if len(platform_active) >= settings.platform_max_active_jobs:
            raise too_many_requests("scout_platform_active_job_limit", "Scout is at platform capacity.", 60)
        retained_rawstore_bytes = _retained_rawstore_bytes(db)
        active_rawstore_reservations = sum(
            _rawstore_reservation_bytes(job, settings) for job in platform_active
        )
        if (
            retained_rawstore_bytes + active_rawstore_reservations + new_rawstore_reservation
            > settings.max_retained_rawstore_bytes
        ):
            raise too_many_requests(
                "scout_rawstore_capacity_limit", "Scout evidence capacity reached.", 3600
            )
        if platform_daily_jobs >= settings.platform_max_daily_jobs:
            raise too_many_requests("scout_platform_daily_job_limit", "Scout daily platform capacity reached.", 3600)
        if (
            platform_daily_browser_ms + platform_reserved_browser_ms + new_reservation_ms
            > settings.platform_max_daily_browser_seconds * 1000
        ):
            raise too_many_requests("scout_platform_daily_browser_limit", "Scout browser capacity reached.", 3600)
        if daily_jobs >= settings.per_customer_daily_jobs:
            raise too_many_requests("scout_daily_job_limit", "Daily Scout job limit reached.", 3600)
        if daily_browser_ms + reserved_browser_ms + new_reservation_ms > settings.per_customer_daily_browser_seconds * 1000:
            raise too_many_requests("scout_daily_browser_limit", "Daily Scout browser budget reached.", 3600)
        if active_count >= settings.per_customer_active_jobs:
            raise too_many_requests("scout_active_job_limit", "Too many active Scout jobs.", 60)

        job = ScoutResearchJob(
            customer_id=customer.id,
            original_query=body.query.strip(),
            normalized_query=normalized,
            jurisdiction=jurisdiction,
            cache_key=key,
            strategy={"adapter": "florida_p0", "mode": "structured_first"},
            limits={
                "max_pages": settings.max_pages,
                "max_actions": settings.max_actions,
                "max_external_requests": settings.max_external_requests,
                "max_related_documents": settings.max_related_documents,
                "max_direct_bytes": settings.max_direct_bytes,
                "max_pdf_pages": settings.max_pdf_pages,
                "max_pdf_text_chars": settings.max_pdf_text_chars,
                "max_pdf_extract_seconds": settings.max_pdf_extract_seconds,
                "max_pdf_extract_memory_bytes": settings.max_pdf_extract_memory_bytes,
                "max_pdf_extract_cpu_seconds": settings.max_pdf_extract_cpu_seconds,
                "max_routed_requests": settings.max_browser_routed_requests,
                "max_retries": settings.max_retries,
                "daily_jobs": settings.per_customer_daily_jobs,
                "daily_browser_seconds": settings.per_customer_daily_browser_seconds,
                "browser_wall_seconds": settings.browser_wall_seconds,
                "browser_cleanup_seconds": settings.browser_cleanup_seconds,
                "daily_browser_reservation_ms": new_reservation_ms,
            },
            usage={},
        )
        db.add(job)
        try:
            db.flush()
        except IntegrityError:
            # The partial unique index is the race-safe authority. Query it
            # only after rollback; do not solve concurrent submits in memory.
            db.rollback()
            existing = db.execute(
                select(ScoutResearchJob).where(
                    ScoutResearchJob.customer_id == customer.id,
                    ScoutResearchJob.cache_key == key,
                    ScoutResearchJob.status.in_(("queued", "running")),
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            response.status_code = 200
            response.headers["Cache-Control"] = "no-store"
            return {"coalesced": True, "job": _job_payload(db, existing)}
        db.commit()
    db.refresh(job)
    response.headers["Cache-Control"] = "no-store"
    return {"coalesced": False, "job": _job_payload(db, job)}


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, request: Request, response: Response, db: Session = Depends(get_db)):
    customer = _require_session(request, db)
    response.headers["Cache-Control"] = "no-store"
    return _job_payload(db, _job_for_owner(db, customer, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, request: Request, response: Response, db: Session = Depends(get_db)):
    _check_origin(request)
    customer = _require_session(request, db)
    # Serialize cancellation so concurrent requests cannot append duplicate
    # terminal events for the same job.
    job = _job_for_owner(db, customer, job_id, lock=True)
    if job.status in {"queued", "running"}:
        job.cancel_version += 1
        # Make cancellation terminal immediately. A claimant may finish its
        # current network operation, but its token/status fence then prevents
        # any heartbeat, RawStore provenance write, finding, or finish write.
        job.status = "canceled"
        job.completed_at = datetime.now(timezone.utc)
        db.add(ScoutJobEvent(job_id=job.id, kind="finished", detail={"status": "canceled", "error_class": None}))
        db.commit()
        db.refresh(job)
    response.headers["Cache-Control"] = "no-store"
    return _job_payload(db, job)


@router.get("/jobs/{job_id}/evidence")
def get_evidence(job_id: uuid.UUID, request: Request, response: Response, db: Session = Depends(get_db)):
    customer = _require_session(request, db)
    _job_for_owner(db, customer, job_id)
    sources = {
        source.id: source
        for source in db.execute(select(ScoutSource).where(ScoutSource.job_id == job_id)).scalars()
    }
    findings = []
    for finding in db.execute(select(ScoutFinding).where(ScoutFinding.job_id == job_id)).scalars():
        source = sources.get(finding.source_id)
        findings.append({
            "id": str(finding.id), "title": finding.title,
            "what_happened": finding.what_happened, "why_it_matters": finding.why_it_matters,
            "excerpt": finding.excerpt, "excerpt_hash": finding.excerpt_hash,
            "confidence": finding.confidence, "source_url": source.canonical_url if source else None,
            "source_hash": source.content_hash if source else None,
        })
    response.headers["Cache-Control"] = "no-store"
    return {"job_id": str(job_id), "findings": findings}


@router.get("/jobs/{job_id}/browser-sessions/{session_id}/replay")
def get_replay(job_id: uuid.UUID, session_id: uuid.UUID, request: Request, response: Response, db: Session = Depends(get_db)):
    customer = _require_session(request, db)
    _job_for_owner(db, customer, job_id)
    session = db.execute(
        select(ScoutBrowserSession).where(ScoutBrowserSession.id == session_id, ScoutBrowserSession.job_id == job_id)
    ).scalar_one_or_none()
    if session is None:
        raise not_found("scout_browser_session_not_found", "Scout browser session was not found.")
    response.headers["Cache-Control"] = "no-store"
    return {"available": session.status == "released" and bool(session.replay_url), "replay_url": session.replay_url if session.status == "released" else None}
