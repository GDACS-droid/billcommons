"""Authenticated, owner-scoped API for the Scout durable research queue.

This router deliberately imports shared contracts and schema models only.  The
Scout worker and provider modules are not present in the API container.
"""
from __future__ import annotations

import uuid
import inspect
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
    ScoutMonitor,
    ScoutMonitorRun,
    ScoutRawBlob,
    ScoutResearchJob,
    ScoutSource,
)
from billcommons_shared.scout import (
    CALIFORNIA,
    ScoutPolicyError,
    ScoutSettings,
    extract_california_bill_query,
    normalize_jurisdiction,
    normalize_query,
    scout_cache_key,
    scout_cache_namespace,
)
from billcommons_shared.scout_admission import ScoutAdmissionError, admit_scout_job, customer_is_admitted
from billcommons_shared.scout_monitors import is_operator_strategy, source_snapshot


router = APIRouter(prefix="/scout", tags=["scout"])

_PLATFORM_ADMISSION_LOCK_KEY = 81_420_902
_sqlite_platform_admission_lock = threading.RLock()


class CreateScoutJob(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    jurisdiction: str = Field(default="FL", min_length=2, max_length=2)


class SaveScoutMonitor(BaseModel):
    cadence_seconds: int = Field(default=6 * 60 * 60, ge=6 * 60 * 60, le=7 * 24 * 60 * 60)


class UpdateScoutMonitor(BaseModel):
    active: bool | None = None
    cadence_seconds: int | None = Field(default=None, ge=6 * 60 * 60, le=7 * 24 * 60 * 60)


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
        if jurisdiction == CALIFORNIA and extract_california_bill_query(body.query) is None:
            raise ScoutPolicyError("invalid_california_retained_query")
    except ScoutPolicyError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail={"code": "invalid_scout_request", "message": str(exc)}) from exc
    key = scout_cache_key(
        body.query,
        jurisdiction,
        freshness_bucket=scout_cache_namespace(jurisdiction),
    )
    try:
        admission = admit_scout_job(
            db, customer, original_query=body.query, normalized_query=normalized,
            jurisdiction=jurisdiction, cache_key=key, settings=settings,
        )
    except ScoutAdmissionError as exc:
        raise too_many_requests(exc.code, exc.message, exc.retry_after) from exc
    if admission.created:
        db.commit()
        db.refresh(admission.job)
    else:
        response.status_code = 200
    response.headers["Cache-Control"] = "no-store"
    payload = {"coalesced": admission.coalesced, "job": _job_payload(db, admission.job)}
    if admission.cached:
        payload.update({"cached": True, "cache_hit": True})
    return payload


def _monitor_for_owner(db: Session, customer: ApiCustomer, monitor_id: uuid.UUID, *, lock: bool = False) -> ScoutMonitor:
    stmt = select(ScoutMonitor).where(ScoutMonitor.id == monitor_id, ScoutMonitor.customer_id == customer.id)
    if lock:
        stmt = stmt.with_for_update()
    monitor = db.execute(stmt).scalar_one_or_none()
    if monitor is None:
        raise not_found("scout_monitor_not_found", "Scout monitor was not found.")
    return monitor


def _monitor_payload(monitor: ScoutMonitor) -> dict:
    return {
        "id": str(monitor.id),
        "query": monitor.original_query,
        "jurisdiction": monitor.jurisdiction,
        "cadence_seconds": monitor.cadence_seconds,
        "active": monitor.active,
        "next_run_at": monitor.next_run_at.isoformat() if monitor.next_run_at else None,
        "consecutive_deferrals": monitor.consecutive_deferrals,
        "last_completed_run_id": str(monitor.last_completed_run_id) if monitor.last_completed_run_id else None,
        "created_at": monitor.created_at.isoformat() if monitor.created_at else None,
        "updated_at": monitor.updated_at.isoformat() if monitor.updated_at else None,
    }


def _monitor_run_payload(run: ScoutMonitorRun, job: ScoutResearchJob | None) -> dict:
    return {
        "id": str(run.id),
        "job_id": str(run.job_id) if run.job_id else None,
        "baseline_run_id": str(run.baseline_run_id) if run.baseline_run_id else None,
        "status": run.status,
        "execution_mode": run.execution_mode,
        "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error_class": run.error_class,
        "source_snapshot": run.source_snapshot or {},
        "change_summary": run.change_summary or {},
        "job": _job_payload_for_monitor(job) if job is not None else None,
    }


def _job_payload_for_monitor(job: ScoutResearchJob) -> dict:
    """A bounded run reference; detailed evidence remains owner-readable by job ID."""
    return {
        "id": str(job.id), "status": job.status, "query": job.original_query,
        "jurisdiction": job.jurisdiction, "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _validate_monitor_cadence(cadence_seconds: int, settings: ScoutSettings) -> None:
    if not settings.monitor_min_cadence_seconds <= cadence_seconds <= settings.monitor_max_cadence_seconds:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_monitor_cadence", "message": "Monitor cadence is outside the configured safe range."},
        )


@router.post("/jobs/{job_id}/monitor", status_code=201)
def save_monitor(
    job_id: uuid.UUID,
    body: SaveScoutMonitor,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    settings = _require_enabled()
    _check_origin(request)
    customer = _require_session(request, db)
    _require_canary(customer, settings)
    _validate_monitor_cadence(body.cadence_seconds, settings)
    # Follow the admission lock ordering: owner row first, then the selected
    # terminal job. This makes a concurrent duplicate save deterministic.
    db.execute(select(ApiCustomer.id).where(ApiCustomer.id == customer.id).with_for_update())
    job = _job_for_owner(db, customer, job_id, lock=True)
    if job.status not in {"completed", "partial"} or is_operator_strategy(job):
        raise HTTPException(status_code=422, detail={"code": "invalid_monitor_baseline", "message": "Save a completed Scout result with user-visible evidence."})
    evidence_count = db.scalar(select(func.count()).select_from(ScoutFinding).where(ScoutFinding.job_id == job.id)) or 0
    if evidence_count < 1:
        raise HTTPException(status_code=422, detail={"code": "monitor_baseline_missing_evidence", "message": "Save a Scout result that retained evidence."})
    existing = db.execute(select(ScoutMonitor).where(
        ScoutMonitor.customer_id == customer.id,
        ScoutMonitor.normalized_query == job.normalized_query,
        ScoutMonitor.jurisdiction == job.jurisdiction,
    )).scalar_one_or_none()
    if existing is not None:
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return {"created": False, "monitor": _monitor_payload(existing)}
    count = db.scalar(select(func.count()).select_from(ScoutMonitor).where(ScoutMonitor.customer_id == customer.id)) or 0
    if count >= settings.max_saved_monitors_per_customer:
        raise too_many_requests("scout_monitor_limit", "Saved Scout monitor limit reached.", 3600)
    now = datetime.now(timezone.utc)
    monitor = ScoutMonitor(
        customer_id=customer.id,
        original_query=job.original_query,
        normalized_query=job.normalized_query,
        jurisdiction=job.jurisdiction,
        cache_key=job.cache_key,
        cadence_seconds=body.cadence_seconds,
        active=True,
        next_run_at=now + timedelta(seconds=body.cadence_seconds),
    )
    db.add(monitor)
    db.flush()
    try:
        snapshot = source_snapshot(db, job.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc), "message": "Baseline evidence is too large to monitor safely."}) from exc
    baseline = ScoutMonitorRun(
        monitor_id=monitor.id,
        job_id=job.id,
        status="baseline",
        execution_mode="baseline",
        scheduled_for=now,
        completed_at=now,
        source_snapshot=snapshot,
        change_summary={
            "baseline": True,
            "comparison_complete": job.status == "completed",
            "absence_evaluated": False,
            "observed_source_count": len(snapshot["sources"]),
        },
    )
    db.add(baseline)
    db.flush()
    monitor.last_completed_run_id = baseline.id
    db.commit()
    db.refresh(monitor)
    response.headers["Cache-Control"] = "no-store"
    return {"created": True, "monitor": _monitor_payload(monitor)}


@router.get("/monitors")
def list_monitors(request: Request, response: Response, db: Session = Depends(get_db)):
    customer = _require_session(request, db)
    monitors = db.scalars(select(ScoutMonitor).where(ScoutMonitor.customer_id == customer.id).order_by(ScoutMonitor.created_at.desc())).all()
    response.headers["Cache-Control"] = "no-store"
    return {"monitors": [_monitor_payload(monitor) for monitor in monitors]}


@router.get("/monitors/{monitor_id}/runs")
def list_monitor_runs(
    monitor_id: uuid.UUID, request: Request, response: Response,
    limit: int = Query(default=50, ge=1, le=100), cursor: uuid.UUID | None = None,
    db: Session = Depends(get_db),
):
    customer = _require_session(request, db)
    monitor = _monitor_for_owner(db, customer, monitor_id)
    stmt = select(ScoutMonitorRun, ScoutResearchJob).outerjoin(
        ScoutResearchJob, ScoutResearchJob.id == ScoutMonitorRun.job_id
    ).where(ScoutMonitorRun.monitor_id == monitor.id)
    if cursor is not None:
        anchor = db.execute(select(ScoutMonitorRun).where(
            ScoutMonitorRun.id == cursor, ScoutMonitorRun.monitor_id == monitor.id
        )).scalar_one_or_none()
        if anchor is None:
            raise HTTPException(status_code=422, detail={"code": "invalid_monitor_cursor", "message": "Monitor history cursor is invalid."})
        stmt = stmt.where(or_(
            ScoutMonitorRun.scheduled_for < anchor.scheduled_for,
            and_(ScoutMonitorRun.scheduled_for == anchor.scheduled_for, ScoutMonitorRun.id < anchor.id),
        ))
    rows = list(db.execute(stmt.order_by(
        ScoutMonitorRun.scheduled_for.desc(), ScoutMonitorRun.id.desc()
    ).limit(limit + 1)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    # Resume after the final row returned.  Using the first unseen row as the
    # cursor would make the strict "older than cursor" predicate skip it.
    next_cursor = str(rows[-1][0].id) if has_more else None
    response.headers["Cache-Control"] = "no-store"
    return {"monitor": _monitor_payload(monitor), "runs": [
        _monitor_run_payload(run, job) for run, job in rows
    ], "next_cursor": next_cursor}


@router.patch("/monitors/{monitor_id}")
def update_monitor(
    monitor_id: uuid.UUID,
    body: UpdateScoutMonitor,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    if body.active is None and body.cadence_seconds is None:
        raise HTTPException(status_code=422, detail={"code": "empty_monitor_update", "message": "Supply active or cadence_seconds."})
    _check_origin(request)
    customer = _require_session(request, db)
    monitor = _monitor_for_owner(db, customer, monitor_id, lock=True)
    settings: ScoutSettings | None = None
    if body.active is True:
        settings = _require_enabled()
        _require_canary(customer, settings)
    if body.cadence_seconds is not None:
        settings = settings or _enabled()
        _validate_monitor_cadence(body.cadence_seconds, settings)
        monitor.cadence_seconds = body.cadence_seconds
    if body.active is not None:
        monitor.active = body.active
        if body.active:
            monitor.consecutive_deferrals = 0
            monitor.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=monitor.cadence_seconds)
    elif body.cadence_seconds is not None and monitor.active:
        monitor.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=monitor.cadence_seconds)
    db.commit()
    db.refresh(monitor)
    response.headers["Cache-Control"] = "no-store"
    return {"monitor": _monitor_payload(monitor)}


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
        for run in db.scalars(select(ScoutMonitorRun).where(
            ScoutMonitorRun.job_id == job.id, ScoutMonitorRun.status == "queued"
        )).all():
            run.status = "canceled"
            run.completed_at = job.completed_at
            run.error_class = None
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
