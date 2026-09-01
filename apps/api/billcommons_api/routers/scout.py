"""Authenticated, owner-scoped API for the Scout durable research queue.

This router deliberately imports shared contracts and schema models only.  The
Scout worker and provider modules are not present in the API container.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
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
    ScoutResearchJob,
    ScoutSource,
)
from billcommons_shared.scout import ScoutPolicyError, ScoutSettings, normalize_jurisdiction, normalize_query, scout_cache_key

router = APIRouter(prefix="/scout", tags=["scout"])


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


def _browser_reservation_ms(job: ScoutResearchJob, settings: ScoutSettings) -> int:
    """Return the browser capacity durably reserved for one nonterminal job.

    A Scout job can issue at most ``max_external_requests`` browser captures,
    each bounded by its wall-clock limit.  New jobs persist that bound so a
    later settings change cannot alter an already-admitted reservation.  The
    fallback keeps pre-reservation jobs conservative during a rolling deploy.
    """
    limits = job.limits or {}
    reservation = limits.get("daily_browser_reservation_ms")
    if isinstance(reservation, int) and reservation >= 0:
        return reservation
    max_requests = limits.get("max_external_requests", settings.max_external_requests)
    wall_seconds = limits.get("browser_wall_seconds", settings.browser_wall_seconds)
    if not isinstance(max_requests, int) or max_requests < 0:
        max_requests = settings.max_external_requests
    if not isinstance(wall_seconds, int) or wall_seconds < 0:
        wall_seconds = settings.browser_wall_seconds
    return max_requests * wall_seconds * 1000


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

    active_jobs = list(
        db.execute(select(ScoutResearchJob).where(
            ScoutResearchJob.customer_id == customer.id,
            ScoutResearchJob.status.in_(("queued", "running")),
        )).scalars()
    )
    active_count = len(active_jobs)
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

    # Durable calendar-day budgets prevent a customer from serially draining
    # browser credits as soon as each two-job active batch completes. Cached or
    # coalesced research above remains free to reuse after the cap.
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_jobs = db.scalar(
        select(func.count()).select_from(ScoutResearchJob).where(
            ScoutResearchJob.customer_id == customer.id,
            ScoutResearchJob.created_at >= day_start,
        )
    ) or 0
    if daily_jobs >= settings.per_customer_daily_jobs:
        raise too_many_requests("scout_daily_job_limit", "Daily Scout job limit reached.", 3600)
    # Active jobs reserve their maximum possible browser spend before any
    # provider session exists.  Without this durable reservation, serialized
    # creates can each observe the same runtime ledger and over-admit work.
    # A canceled/terminal job retains its reservation while a live provider
    # slot exists, then its actual daily runtime replaces that reservation.
    reserved_job_ids = {job.id for job in active_jobs}
    reserved_job_ids.update(
        db.scalars(
            select(ScoutBrowserSession.job_id)
            .join(ScoutResearchJob, ScoutResearchJob.id == ScoutBrowserSession.job_id)
            .where(
                ScoutResearchJob.customer_id == customer.id,
                ScoutBrowserSession.status.in_(("starting", "running", "cleanup_failed", "reaping")),
            )
        ).all()
    )
    daily_browser_ms = db.scalar(
        select(func.coalesce(func.sum(ScoutBrowserSession.runtime_ms), 0))
        .join(ScoutResearchJob, ScoutResearchJob.id == ScoutBrowserSession.job_id)
        .where(
            ScoutResearchJob.customer_id == customer.id,
            ScoutBrowserSession.created_at >= day_start,
            ScoutResearchJob.id.not_in(reserved_job_ids),
        )
    ) or 0
    reserved_browser_ms = sum(
        _browser_reservation_ms(job, settings)
        for job in db.scalars(select(ScoutResearchJob).where(ScoutResearchJob.id.in_(reserved_job_ids))).all()
    )
    if daily_browser_ms + reserved_browser_ms + (
        settings.max_external_requests * settings.browser_wall_seconds * 1000
    ) > settings.per_customer_daily_browser_seconds * 1000:
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
        limits={"max_pages": settings.max_pages, "max_actions": settings.max_actions, "max_external_requests": settings.max_external_requests, "max_routed_requests": settings.max_browser_routed_requests, "max_retries": settings.max_retries, "daily_jobs": settings.per_customer_daily_jobs, "daily_browser_seconds": settings.per_customer_daily_browser_seconds, "browser_wall_seconds": settings.browser_wall_seconds, "daily_browser_reservation_ms": settings.max_external_requests * settings.browser_wall_seconds * 1000},
        usage={},
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        # The partial unique index is the race-safe authority.  Query it only
        # after rollback; do not try to solve concurrent submits in memory.
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
