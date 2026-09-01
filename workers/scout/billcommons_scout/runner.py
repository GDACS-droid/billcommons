"""DB-backed Scout queue runner; no database transaction spans external I/O."""
from __future__ import annotations

import html
import inspect
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, delete, exists, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from billcommons_schema.models import ApiCustomer, Bill, BillSubject, Jurisdiction, ScoutBrowserSession, ScoutFinding, ScoutJobEvent, ScoutRawBlob, ScoutResearchJob, ScoutSource, Session as LegislativeSession
from billcommons_shared.rawstore import RawStore
from billcommons_shared.safe_http import SafeHttpError, SsrfRejected, new_safe_http_client
from billcommons_shared.scout import (
    BrowserCapture, BrowserRequest, ResearchBrowserProvider, ScoutPolicyError,
    ScoutSettings, browser_required, canonicalize_url, classify_direct_response,
    content_hash, discover_florida_senate_related_documents,
    extract_florida_bill_identifier, summarize_content_change,
    is_pdf_attachment_payload, topical_search_terms,
)
from billcommons_scout.providers import ProviderSessionPersistenceError, SolariProviderError
from billcommons_scout.pdf_extract import extract_pdf_text

Fetcher = Callable[[str], tuple[int, str | None, bytes]]
_INFLIGHT_CLEANUP_LOCK = threading.Lock()
_INFLIGHT_CLEANUPS: set[uuid.UUID] = set()
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SHELL_MARKERS = (
    "sign in", "log in", "login", "maintenance", "temporarily unavailable",
    "enable javascript", "javascript is required", "please enable javascript",
    "loading...", "loading…",
)
_OPERATOR_CANARY_URL = (
    "https://www.leg.state.fl.us/statutes/index.cfm?App_mode=Display_Statute&"
    "Search_String=&URL=0000-0099/0043/Sections/0043.16.html"
)
_OPERATOR_CANARY_MARKERS = (b"43.16", b"Justice Administrative Commission")
_OPERATOR_CANARY_CACHE_KEY = "scout-operator-solari-lifecycle-v1"
_OPERATOR_CANARY_QUERY = "operator solari lifecycle validation"
@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    token: str


def _bounded_call(fn, *args, timeout: float):
    """Return a bounded provider call without a non-daemon executor shutdown.

    Providers are remote I/O and cannot be forcibly cancelled safely from
    Python. The daemon deliberately does not keep a draining worker process
    alive after its caller times out. Browser release uses a dedicated durable
    keepalive below because retrying an outcome-unknown cleanup concurrently
    would be unsafe; this generic helper remains suitable for bounded capture
    and replay probes.
    """
    result: dict[str, object] = {}
    done = threading.Event()

    def invoke() -> None:
        try:
            result["value"] = fn(*args)
        except BaseException as exc:  # propagate provider failures unchanged
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=invoke, name="scout-bounded-provider-call", daemon=True)
    thread.start()
    if not done.wait(timeout):
        raise TimeoutError("provider_call_timeout")
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result.get("value")


def safe_direct_fetch(url: str, *, max_body_bytes: int) -> tuple[int, str | None, bytes]:
    """Pinned-address GET; expose only a rejected redirect's status to routing."""
    try:
        response = new_safe_http_client(max_body_bytes=max_body_bytes).fetch(
            url, method="GET", headers={"User-Agent": "BillCommons-Scout/1.0"}, require_body=True
        )
    except SsrfRejected as exc:
        # The shared transport deliberately refuses to follow redirects. For
        # the narrow browser allowlist, preserve only the status so the router
        # can hand navigation to the browser provider, whose route policy
        # independently admits every redirect target. Other hosts still fail
        # closed in browser_required().
        prefix = "redirect_status_"
        if exc.reason.startswith(prefix):
            try:
                status = int(exc.reason.removeprefix(prefix))
            except ValueError:
                raise exc
            return status, None, b""
        raise
    return response.status, response.headers.get("content-type"), response.body or b""


class ScoutRunner:
    def __init__(self, sessions: sessionmaker[Session], rawstore: RawStore, provider: ResearchBrowserProvider, *, settings: ScoutSettings | None = None, fetcher: Fetcher | None = None) -> None:
        self.sessions = sessions
        self.rawstore = rawstore
        self.provider = provider
        self.settings = settings or ScoutSettings.from_env()
        self.fetcher = fetcher

    def _direct_fetch(self, url: str, *, max_body_bytes: int) -> tuple[int, str | None, bytes]:
        """Use the job's frozen byte ceiling for production transport.

        Test fixture fetchers retain the narrow legacy callable shape; their
        returned bytes are checked against the same frozen ceiling immediately
        after retrieval.
        """
        if self.fetcher is not None:
            return self.fetcher(url)
        return safe_direct_fetch(url, max_body_bytes=max_body_bytes)

    def claim_next(self, worker_id: str) -> Claim | None:
        """Claim one queued/expired job using SKIP LOCKED where supported."""
        now = datetime.now(timezone.utc)
        with self.sessions() as db:
            stmt = (
                select(ScoutResearchJob)
                .where(or_(ScoutResearchJob.status == "queued", (ScoutResearchJob.status == "running") & (ScoutResearchJob.lease_expires_at < now)))
                .order_by(ScoutResearchJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = db.execute(stmt).scalar_one_or_none()
            if job is None:
                return None
            if job.status == "running":
                if job.retry_count >= self._job_limit(job, "max_retries", self.settings.max_retries):
                    # An expired lease may be malformed (no token) after a
                    # process crash; it still must not bypass its persisted
                    # retry budget just because normal fencing is impossible.
                    job.status = "failed"
                    job.error_class = "retry_exhausted"
                    job.completed_at = now
                    job.lease_expires_at = None
                    job.claim_owner = None
                    job.claim_token = None
                    db.add(ScoutJobEvent(job_id=job.id, kind="finished", detail={"status": "failed", "error_class": "retry_exhausted"}))
                    db.commit()
                    return None
                job.retry_count += 1
            job.status = "running"
            job.claim_owner = worker_id
            job.claim_token = secrets.token_urlsafe(24)
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=self.settings.lease_seconds)
            db.add(ScoutJobEvent(job_id=job.id, kind="claimed", detail={"attempt": job.retry_count + 1}))
            db.commit()
            return Claim(job.id, job.claim_token)

    @staticmethod
    def _job_limit(job: ScoutResearchJob, name: str, fallback: int) -> int:
        """Use the request-time budget, with a safe fallback for legacy rows."""
        value = (job.limits or {}).get(name, fallback)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback

    def run_once(self, worker_id: str) -> bool:
        claim = self.claim_next(worker_id)
        if claim is None:
            return False
        self.process(claim.job_id, claim.token)
        return True

    def run_operator_lifecycle_canary(self, customer_id: uuid.UUID) -> str:
        """Run one durable, non-user-facing Solari lifecycle validation.

        The command-line entrypoint owns rollout and account admission.  This
        method deliberately accepts only an already-verified customer ID and
        writes a fixed, one-shot job that never creates a user finding.  Its
        deterministic cache key makes a completed, failed, or partially
        cleaned-up validation non-repeatable without a deliberate code change.
        That keeps an operator retry from becoming an unbounded browser-spend
        path or a source of misleading research results.
        """
        token = secrets.token_urlsafe(24)
        with self.sessions() as db:
            # A customer with no prior Scout jobs is valid. Lock the actual
            # account row rather than relying on a job row for serialization.
            account = db.execute(
                select(ApiCustomer).where(ApiCustomer.id == customer_id).with_for_update()
            ).scalar_one_or_none()
            if account is None:
                return "missing_customer"
            existing = db.execute(
                select(ScoutResearchJob).where(
                    ScoutResearchJob.customer_id == customer_id,
                    ScoutResearchJob.cache_key == _OPERATOR_CANARY_CACHE_KEY,
                ).order_by(ScoutResearchJob.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return "already_running" if existing.status in {"queued", "running"} else "already_terminal"
            limits = {
                "max_pages": 1,
                "max_actions": 1,
                "max_external_requests": 1,
                "max_direct_bytes": self.settings.max_direct_bytes,
                "max_routed_requests": min(20, self.settings.max_browser_routed_requests),
                "max_retries": 0,
                "browser_wall_seconds": self.settings.browser_wall_seconds,
                "browser_cleanup_seconds": self.settings.browser_cleanup_seconds,
                "daily_browser_reservation_ms": (
                    self.settings.browser_wall_seconds
                    + 2 * self.settings.browser_cleanup_seconds
                ) * 1000,
            }
            now = datetime.now(timezone.utc)
            job = ScoutResearchJob(
                customer_id=customer_id,
                original_query=_OPERATOR_CANARY_QUERY,
                normalized_query=_OPERATOR_CANARY_QUERY,
                jurisdiction="FL",
                cache_key=_OPERATOR_CANARY_CACHE_KEY,
                status="running",
                claim_owner="operator-solari-lifecycle",
                claim_token=token,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=self.settings.lease_seconds),
                strategy={
                    "mode": "operator_lifecycle_validation",
                    "router": "forced_browser_validation",
                    "user_finding": False,
                },
                limits=limits,
                usage={},
            )
            db.add(job)
            db.flush()
            db.add(ScoutJobEvent(
                job_id=job.id,
                kind="operator_lifecycle_canary_started",
                detail={"mode": "forced_browser_validation"},
            ))
            db.commit()
            job_id = job.id

        captured, reason = self._browser_capture(
            job_id,
            token,
            None,
            "Official Florida statute — Scout lifecycle validation",
            None,
            {},
            _OPERATOR_CANARY_URL,
            create_finding=False,
            required_markers=_OPERATOR_CANARY_MARKERS,
        )
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return "fence_lost"
            session = db.execute(
                select(ScoutBrowserSession)
                .where(ScoutBrowserSession.job_id == job_id)
                .order_by(ScoutBrowserSession.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if captured and session is not None and session.status == "released":
                db.add(ScoutJobEvent(
                    job_id=job_id,
                    kind="operator_lifecycle_canary_complete",
                    detail={"source_retained": True, "session_released": True},
                ))
                self._finish(db, job, token, "completed", None, False)
                return "completed"
            error_class = (
                "browser_cleanup_failed" if captured else reason or "browser_lifecycle_validation_failed"
            )
            db.add(ScoutJobEvent(
                job_id=job_id,
                kind="operator_lifecycle_canary_incomplete",
                detail={"source_retained": bool(captured), "session_released": False},
            ))
            self._finish(db, job, token, "partial" if captured else "failed", error_class, bool(captured))
            return "partial" if captured else "failed"

    def _canceled(self, db: Session, job: ScoutResearchJob, version: int, token: str) -> bool:
        db.refresh(job)
        return job.status != "running" or job.claim_token != token or job.cancel_version != version

    @staticmethod
    def _fenced(db: Session, job_id: uuid.UUID, token: str) -> ScoutResearchJob | None:
        """Lock and validate the current claim before a durable mutation.

        The lease predicate is part of the fence, rather than merely a claim
        eligibility check.  That prevents a worker whose lease elapsed while
        it was doing external I/O from promoting staged bytes or finishing a
        job before another worker has happened to reclaim it.
        """
        now = datetime.now(timezone.utc)
        return db.execute(select(ScoutResearchJob).where(
            ScoutResearchJob.id == job_id,
            ScoutResearchJob.status == "running",
            ScoutResearchJob.claim_token == token,
            ScoutResearchJob.lease_expires_at.is_not(None),
            ScoutResearchJob.lease_expires_at > now,
        ).with_for_update()).scalar_one_or_none()

    def _candidates(self, db: Session, job: ScoutResearchJob) -> list[tuple]:
        identifier = extract_florida_bill_identifier(job.original_query)
        stmt = select(
            Bill.source_url, Bill.id, Bill.title, Bill.status, Bill.identifier,
            Bill.identifier_norm, Bill.latest_action_text, Bill.latest_action_date,
            Bill.status_date, LegislativeSession.identifier, LegislativeSession.name,
            LegislativeSession.active, LegislativeSession.start_date, LegislativeSession.end_date,
        ).join(
            Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id
        ).join(
            LegislativeSession, LegislativeSession.id == Bill.session_id
        ).where(
            Jurisdiction.abbreviation == "FL",
            Bill.source_url.is_not(None),
        )
        if identifier is not None:
            # Identifiers are unique only within a legislative session.  A
            # direct lookup must therefore select one current/newest session,
            # never investigate two different HB/SB records as one bill.
            rows = db.execute(
                stmt.where(Bill.identifier_norm == identifier).order_by(
                    LegislativeSession.active.desc(),
                    LegislativeSession.end_date.desc().nulls_last(),
                    LegislativeSession.start_date.desc().nulls_last(),
                    Bill.updated_at.desc(),
                ).limit(1)
            ).all()
        else:
            # Structured corpus search for topical Florida requests.  This is
            # deliberately a bounded AND-of-terms query over Bill rows, never
            # a web-search/crawl fallback; the adapter still receives only
            # retained official URLs from the matching structured records.
            terms = topical_search_terms(job.original_query)
            if not terms:
                return []
            stmt = stmt.where(and_(*(
                or_(
                    Bill.title.ilike(f"%{term}%"),
                    Bill.description.ilike(f"%{term}%"),
                    exists(select(BillSubject.id).where(
                        BillSubject.bill_id == Bill.id,
                        BillSubject.subject.ilike(f"%{term}%"),
                    )),
                )
                for term in terms
            )))
            # Preserve topical breadth, but do not return two session versions
            # of the same identifier.  Fetch a small overage before the
            # in-memory de-duplication so the five visible candidates remain
            # distinct when old sessions are present.
            rows = db.execute(stmt.order_by(
                LegislativeSession.active.desc(),
                LegislativeSession.end_date.desc().nulls_last(),
                LegislativeSession.start_date.desc().nulls_last(),
                Bill.updated_at.desc(),
            ).limit(25)).all()

        candidates = []
        seen_identifiers: set[str] = set()
        for url, bill_id, title, status, bill_identifier, identifier_norm, action, action_date, status_date, session_identifier, session_name, session_active, session_start, session_end in rows:
            if not url or identifier_norm in seen_identifiers:
                continue
            seen_identifiers.add(identifier_norm)
            candidates.append((url, bill_id, title, status, {
                "identifier": bill_identifier,
                "latest_action": action,
                "latest_action_date": action_date,
                "status_date": status_date,
                "session_identifier": session_identifier,
                "session_name": session_name,
                "session_active": session_active,
                "session_start": session_start,
                "session_end": session_end,
            }))
            if len(candidates) == 5:
                break
        return candidates

    def _has_structured_match_without_source(self, db: Session, job: ScoutResearchJob) -> bool:
        """Distinguish missing official provenance from an unsupported query.

        Scout does not emit an evidence-free finding, but a structured Bill
        Commons record whose ``source_url`` is absent is still a corpus hit.
        This bounded existence query lets the terminal state say exactly that
        instead of falsely reporting the request as unsupported.
        """
        identifier = extract_florida_bill_identifier(job.original_query)
        stmt = select(Bill.id).join(
            Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id
        ).where(
            Jurisdiction.abbreviation == "FL",
            Bill.source_url.is_(None),
        )
        if identifier is not None:
            stmt = stmt.where(Bill.identifier_norm == identifier)
        else:
            terms = topical_search_terms(job.original_query)
            if not terms:
                return False
            stmt = stmt.where(and_(*(
                or_(
                    Bill.title.ilike(f"%{term}%"),
                    Bill.description.ilike(f"%{term}%"),
                    exists(select(BillSubject.id).where(
                        BillSubject.bill_id == Bill.id,
                        BillSubject.subject.ilike(f"%{term}%"),
                    )),
                )
                for term in terms
            )))
        return db.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def process(self, job_id: uuid.UUID, claim_token: str | None = None) -> None:
        """Perform slow I/O after the claim transaction has committed."""
        with self.sessions() as db:
            initial = db.get(ScoutResearchJob, job_id)
            if initial is None or initial.status != "running":
                return
            token = claim_token or initial.claim_token
            if not token:
                return
            job = self._fenced(db, job_id, token)
            if job is None:
                return
            cancel_version = job.cancel_version
            candidates = self._candidates(db, job)
            if not candidates:
                if self._has_structured_match_without_source(db, job):
                    db.add(ScoutJobEvent(
                        job_id=job.id,
                        kind="structured_source_missing",
                        detail={"reason": "official_source_url_unavailable"},
                    ))
                    self._finish(db, job, token, "partial", "official_source_missing", False)
                else:
                    self._finish(db, job, token, "partial", "unsupported_query", False)
                return
            strategy = dict(job.strategy or {})
            strategy["candidate_count"] = len(candidates)
            strategy["structured_lookup"] = "identifier" if extract_florida_bill_identifier(job.original_query) else "title_terms"
            job.strategy = strategy
            db.add(ScoutJobEvent(job_id=job.id, kind="structured_candidates", detail={"count": len(candidates)}))
            db.commit()

        successes = 0
        failures = 0
        request_limit_reached = False
        routed_request_limit_reached = False
        browser_create_outcome_unknown = False
        seen_related_urls: set[str] = set()
        for url, bill_id, bill_title, bill_status, metadata in candidates:
            with self.sessions() as db:
                job = self._fenced(db, job_id, token)
                if job is None or self._canceled(db, job, cancel_version, token):
                    if job is not None and job.status != "canceled":
                        self._finish(db, job, token, "canceled", None, successes > 0)
                    return
            try:
                canonical = canonicalize_url(url)
                last_error: Exception | None = None
                with self.sessions() as db:
                    active_job = self._fenced(db, job_id, token)
                    if active_job is None:
                        return
                    max_retries = self._job_limit(active_job, "max_retries", self.settings.max_retries)
                    max_direct_bytes = self._job_limit(
                        active_job, "max_direct_bytes", self.settings.max_direct_bytes
                    )
                for attempt in range(max_retries + 1):
                    if not self._reserve_external_attempt(job_id, token):
                        request_limit_reached = True
                        break
                    try:
                        status, mime, body = self._direct_fetch(canonical, max_body_bytes=max_direct_bytes)
                        break
                    except SafeHttpError as exc:
                        last_error = exc
                        if attempt == max_retries:
                            raise
                else:  # pragma: no cover - loop always breaks or raises
                    raise last_error or RuntimeError("direct_fetch_failed")
                if request_limit_reached:
                    break
                if not self._heartbeat(job_id, token):
                    return
                if len(body) > max_direct_bytes:
                    raise RuntimeError("direct_body_too_large")
                mode = classify_direct_response(status, mime, body)
                # The response has crossed the direct-retrieval boundary.
                # This is intentionally recorded before extraction; a malformed
                # document is still a real retrieval, not a fake success.
                with self.sessions() as db:
                    if self._fenced(db, job_id, token) is None:
                        return
                    db.add(ScoutJobEvent(job_id=job_id, kind="direct_retrieval", detail={
                        "status": status,
                        "mime_type": (mime or "").split(";", 1)[0].lower(),
                    }))
                    db.commit()
            except (ScoutPolicyError, SafeHttpError):
                failures += 1
                self._record_failed_source(job_id, token, url, "direct", None, None)
                continue
            except Exception:
                failures += 1
                self._record_failed_source(job_id, token, url, "direct", None, None)
                continue
            if mode == "usable":
                if self._persist_capture(job_id, token, bill_id, bill_title, bill_status, metadata, canonical, "direct", status, mime, body):
                    successes += 1
                    related_successes, related_failures, related_limit = self._inspect_florida_related_documents(
                        job_id,
                        token,
                        cancel_version,
                        bill_id,
                        bill_title,
                        bill_status,
                        metadata,
                        canonical,
                        body,
                        seen_related_urls,
                    )
                    successes += related_successes
                    failures += related_failures
                    request_limit_reached = request_limit_reached or related_limit
                else:
                    failures += 1
            elif mode == "browser_required" and browser_required(canonical, status=status, body=body):
                captured, reason = self._browser_capture(job_id, token, bill_id, bill_title, bill_status, metadata, canonical)
                if captured:
                    successes += 1
                else:
                    failures += 1
                    request_limit_reached = request_limit_reached or reason == "external_request_limit"
                    routed_request_limit_reached = routed_request_limit_reached or reason == "browser_routed_request_limit"
                    browser_create_outcome_unknown = (
                        browser_create_outcome_unknown
                        or reason == "browser_create_outcome_unknown"
                    )
            else:
                failures += 1
                self._record_failed_source(job_id, token, canonical, "direct", status, mime)
            if request_limit_reached or browser_create_outcome_unknown:
                break

        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return
            if request_limit_reached:
                # A persisted request-budget denial means there may be
                # unexamined candidates.  Even when earlier candidates
                # succeeded, that is a truthful partial result, never a
                # completed search.
                self._finish(db, job, token, "partial", "external_request_limit", successes > 0)
            elif routed_request_limit_reached:
                self._finish(db, job, token, "partial", "browser_routed_request_limit", successes > 0)
            elif browser_create_outcome_unknown:
                self._finish(
                    db,
                    job,
                    token,
                    "partial",
                    "browser_create_outcome_unknown",
                    successes > 0,
                )
            elif successes and failures:
                self._finish(db, job, token, "partial", None, True)
            elif successes:
                self._finish(db, job, token, "completed", None, False)
            else:
                self._finish(db, job, token, "partial", "no_usable_source", False)

    def _inspect_florida_related_documents(
        self,
        job_id: uuid.UUID,
        token: str,
        cancel_version: int,
        bill_id: uuid.UUID | None,
        bill_title: str,
        bill_status: str | None,
        metadata: dict,
        parent_url: str,
        parent_body: bytes,
        seen_urls: set[str],
    ) -> tuple[int, int, bool]:
        """Persist a small set of official Senate attachments without crawling.

        The parent page was already retrieved and verified as an official bill
        source. Related links remain independently URL-admitted, budgeted,
        fetched, and persisted so a broken attachment leaves the primary
        finding usable while producing a truthful partial job outcome.
        """
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None or self._canceled(db, job, cancel_version, token):
                return 0, 0, False
            maximum = self._job_limit(job, "max_related_documents", self.settings.max_related_documents)
            max_direct_bytes = self._job_limit(job, "max_direct_bytes", self.settings.max_direct_bytes)
        related = discover_florida_senate_related_documents(
            parent_url, parent_body, maximum=maximum, max_html_bytes=max_direct_bytes
        )
        if related:
            with self.sessions() as db:
                if self._fenced(db, job_id, token) is not None:
                    db.add(ScoutJobEvent(job_id=job_id, kind="related_sources_discovered", detail={"count": len(related)}))
                    db.commit()

        successes = 0
        failures = 0
        for document in related:
            if document.canonical_url in seen_urls:
                continue
            seen_urls.add(document.canonical_url)
            with self.sessions() as db:
                job = self._fenced(db, job_id, token)
                if job is None or self._canceled(db, job, cancel_version, token):
                    return successes, failures, False
                max_retries = self._job_limit(job, "max_retries", self.settings.max_retries)
                max_direct_bytes = self._job_limit(job, "max_direct_bytes", self.settings.max_direct_bytes)
            try:
                for attempt in range(max_retries + 1):
                    if not self._reserve_external_attempt(job_id, token):
                        return successes, failures, True
                    try:
                        status, mime, body = self._direct_fetch(
                            document.canonical_url, max_body_bytes=max_direct_bytes
                        )
                        break
                    except SafeHttpError:
                        if attempt == max_retries:
                            raise
                else:  # pragma: no cover - every branch breaks, returns, or raises
                    raise RuntimeError("related_direct_fetch_failed")
                if not self._heartbeat(job_id, token):
                    return successes, failures, False
                if len(body) > max_direct_bytes:
                    raise RuntimeError("direct_body_too_large")
                mode = classify_direct_response(status, mime, body)
                with self.sessions() as db:
                    if self._fenced(db, job_id, token) is None:
                        return successes, failures, False
                    db.add(ScoutJobEvent(job_id=job_id, kind="direct_retrieval", detail={
                        "status": status,
                        "mime_type": (mime or "").split(";", 1)[0].lower(),
                        "related_document": document.artifact_type,
                    }))
                    db.commit()
                # Florida Senate attachments use ordinary direct retrieval. A
                # shell/redirect is a failed attachment, not an excuse to
                # escalate into a costly generic browser route.
                if mode != "usable" or not is_pdf_attachment_payload(mime, body):
                    self._record_failed_source(job_id, token, document.canonical_url, "direct", status, mime)
                    failures += 1
                    continue
                related_metadata = dict(metadata)
                related_metadata["related_artifact_type"] = document.artifact_type
                source_id = self._persist_capture(
                    job_id,
                    token,
                    bill_id,
                    bill_title,
                    bill_status,
                    related_metadata,
                    document.canonical_url,
                    "direct",
                    status,
                    mime,
                    body,
                )
                if source_id is None:
                    failures += 1
                else:
                    successes += 1
            except (ScoutPolicyError, SafeHttpError):
                self._record_failed_source(job_id, token, document.canonical_url, "direct", None, None)
                failures += 1
            except Exception:
                self._record_failed_source(job_id, token, document.canonical_url, "direct", None, None)
                failures += 1
        return successes, failures, False

    def _record_failed_source(self, job_id: uuid.UUID, token: str, url: str, mechanism: str, status: int | None, mime: str | None) -> None:
        try:
            safe_url = canonicalize_url(url)
        except ScoutPolicyError:
            safe_url = None
        with self.sessions() as db:
            if self._fenced(db, job_id, token) is None:
                return
            # A rejected URL is hostile input, not provenance. Never persist it
            # in a field the API/UI may render as an external link. Safe
            # official URLs still retain a failed observation for diagnostics.
            if safe_url is not None:
                db.add(ScoutSource(
                    job_id=job_id,
                    canonical_url=safe_url,
                    official=False,
                    retrieval_mechanism=mechanism,
                    http_status=status,
                    mime_type=mime,
                ))
            db.add(ScoutJobEvent(job_id=job_id, kind="source_failed", detail={"mechanism": mechanism, "status": status}))
            db.commit()

    def _exact_raw_ref(self, job_id: uuid.UUID, token: str, url: str, digest: str) -> str | None:
        """Find a tenant-local matching blob reference without touching RawStore.

        Exact bytes are reusable storage, but never authority to copy a prior
        finding: the current structured candidate controls the claim.
        """
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return None
            exact = db.execute(
                select(ScoutSource.raw_ref)
                .join(ScoutResearchJob, ScoutResearchJob.id == ScoutSource.job_id)
                .where(
                    ScoutResearchJob.customer_id == job.customer_id,
                    ScoutSource.canonical_url == url,
                    ScoutSource.official.is_(True),
                    ScoutSource.content_hash == digest,
                    ScoutSource.raw_ref.is_not(None),
                )
                .order_by(ScoutSource.retrieved_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            return exact

    @staticmethod
    def _latest_observation_for_customer(
        db: Session, customer_id: uuid.UUID, url: str,
    ) -> tuple[uuid.UUID, str | None, str | None] | None:
        """Read the newest official observation for one tenant and URL."""
        prior = db.execute(
            select(ScoutSource.id, ScoutSource.content_hash, ScoutSource.raw_ref)
            .join(ScoutResearchJob, ScoutResearchJob.id == ScoutSource.job_id)
            .where(
                ScoutResearchJob.customer_id == customer_id,
                ScoutSource.canonical_url == url,
                ScoutSource.official.is_(True),
                ScoutSource.content_hash.is_not(None),
            )
            .order_by(ScoutSource.retrieved_at.desc(), ScoutSource.id.desc())
            .limit(1)
        ).one_or_none()
        return tuple(prior) if prior is not None else None

    @staticmethod
    def _lock_source_history(db: Session, customer_id: uuid.UUID, url: str) -> None:
        """Serialize PostgreSQL finalization for a tenant-local canonical URL."""
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            # A transaction-scoped lock avoids a new durable lock table and is
            # released before any RawStore operation.  The tenant is part of
            # the key so identical public URLs never cross customer bounds.
            db.execute(text(
                "SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"
            ), {"scope": f"scout-source:{customer_id}:{url}"})

    def _latest_observation(
        self, job_id: uuid.UUID, token: str, url: str
    ) -> tuple[uuid.UUID, str | None, str | None] | None:
        """Return the newest tenant-local URL observation, regardless of hash.

        Hash filtering here would make A→B→A look unchanged against an older
        A. RawStore reads deliberately happen after this DB transaction closes.
        """
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return None
            return self._latest_observation_for_customer(db, job.customer_id, url)

    def _persist_capture(
        self,
        job_id: uuid.UUID,
        token: str,
        bill_id: uuid.UUID | None,
        bill_title: str,
        bill_status: str | None,
        metadata: dict,
        url: str,
        mechanism: str,
        status: int | None,
        mime: str | None,
        body: bytes,
        *,
        create_finding: bool = True,
    ) -> uuid.UUID | None:
        try:
            with self.sessions() as db:
                active_job = self._fenced(db, job_id, token)
                if active_job is None:
                    return None
                max_direct_bytes = self._job_limit(
                    active_job, "max_direct_bytes", self.settings.max_direct_bytes
                )
                max_pdf_pages = self._job_limit(active_job, "max_pdf_pages", self.settings.max_pdf_pages)
                max_pdf_text_chars = self._job_limit(
                    active_job, "max_pdf_text_chars", self.settings.max_pdf_text_chars
                )
                max_pdf_extract_seconds = self._job_limit(
                    active_job,
                    "max_pdf_extract_seconds",
                    self.settings.max_pdf_extract_seconds,
                )
                max_pdf_extract_memory_bytes = self._job_limit(
                    active_job,
                    "max_pdf_extract_memory_bytes",
                    self.settings.max_pdf_extract_memory_bytes,
                )
                max_pdf_extract_cpu_seconds = self._job_limit(
                    active_job,
                    "max_pdf_extract_cpu_seconds",
                    self.settings.max_pdf_extract_cpu_seconds,
                )
            if len(body) > max_direct_bytes:
                raise RuntimeError("direct_body_too_large")
            digest = content_hash(body)
            exact_raw_ref = self._exact_raw_ref(job_id, token, url, digest)
            if exact_raw_ref:
                try:
                    if not self.rawstore.exists(exact_raw_ref) or content_hash(self.rawstore.get(exact_raw_ref)) != digest:
                        exact_raw_ref = None
                except Exception:
                    exact_raw_ref = None
            mime_base = (mime or "").split(";", 1)[0].lower()
            related_artifact_type = metadata.get("related_artifact_type")
            if related_artifact_type not in {"committee analysis", "amendment"}:
                related_artifact_type = None
            evidence: tuple[str, int, int] | None = None
            if create_finding:
                if mime_base == "application/pdf":
                    text = extract_pdf_text(
                        body,
                        max_pages=max_pdf_pages,
                        max_text_chars=max_pdf_text_chars,
                        timeout_seconds=max_pdf_extract_seconds,
                        memory_limit_bytes=max_pdf_extract_memory_bytes,
                        cpu_limit_seconds=max_pdf_extract_cpu_seconds,
                    )
                else:
                    text = html.unescape(_TAG_RE.sub(" ", body.decode("utf-8", "replace")))[:max_pdf_text_chars].strip()
                evidence = (
                    self._related_evidence_excerpt(text, metadata)
                    if related_artifact_type
                    else self._evidence_excerpt(text, metadata, bill_status)
                )
                if evidence is None:
                    self._record_failed_source(job_id, token, url, mechanism, status, mime)
                    return None
        except Exception:
            self._record_failed_source(job_id, token, url, mechanism, status, mime)
            return None
        try:
            # Commit an explicitly unverified stage first.  A cancellation
            # after RawStore.put therefore leaves a durable reference to the
            # byte sequence, not an untracked RawStore orphan.
            with self.sessions() as db:
                if self._fenced(db, job_id, token) is None:
                    return None
                if self._fenced(db, job_id, token) is None:
                    return None
                stage = ScoutSource(job_id=job_id, canonical_url=url, official=False, retrieval_mechanism="staged", http_status=status, mime_type=mime)
                db.add(stage)
                db.commit()
                stage_id = stage.id
            try:
                raw_ref = exact_raw_ref or self.rawstore.put(body, {"source_url": url, "mechanism": mechanism})
            except Exception:
                with self.sessions() as db:
                    stage = db.get(ScoutSource, stage_id)
                    if stage is not None:
                        db.delete(stage)
                        db.commit()
                raise
            # RawStore reads must remain outside transactions.  A concurrent
            # finalizer can advance the URL history between this read and the
            # final write, so the PostgreSQL finalization lock below rechecks
            # the predecessor and retries with the new immediate predecessor.
            while True:
                prior = self._latest_observation(job_id, token, url)
                prior_id: uuid.UUID | None = None
                prior_bytes: bytes | None = None
                if prior is not None:
                    prior_id, prior_digest, prior_raw_ref = prior
                    if prior_digest == digest:
                        prior_bytes = body
                    elif prior_raw_ref:
                        try:
                            prior_bytes = self.rawstore.get(prior_raw_ref)
                        except Exception:
                            # A missing historic blob must not be represented as
                            # unchanged/cosmetic merely because hashes differ.
                            prior_bytes = None
                change = summarize_content_change(prior_bytes, body) if prior_id is not None else None
                with self.sessions() as db:
                    current_job = self._fenced(db, job_id, token)
                    if current_job is None:
                        stage = db.get(ScoutSource, stage_id)
                        if stage is not None:
                            stage.raw_ref = raw_ref
                            stage.content_hash = digest
                            stage.document_hash = digest
                            db.commit()
                        return None
                    self._lock_source_history(db, current_job.customer_id, url)
                    if self._latest_observation_for_customer(db, current_job.customer_id, url) != prior:
                        # The lock-holder committed a newer observation while
                        # RawStore was read.  Recompute its comparison outside
                        # this transaction, then revalidate again.
                        continue
                    # Same URL/hash within this job is idempotent. A changed
                    # version retains the immediately preceding tenant-local version.
                    same = db.execute(select(ScoutSource.id).where(ScoutSource.job_id == job_id, ScoutSource.canonical_url == url, ScoutSource.content_hash == digest)).scalar_one_or_none()
                    if same is not None and same != stage_id:
                        db.delete(db.get(ScoutSource, stage_id))
                        db.commit()
                        return same
                    source = db.get(ScoutSource, stage_id)
                    if source is None:  # pragma: no cover - stage is committed above
                        return None
                    identifier = str(metadata.get("identifier") or bill_title)
                    source.title = (
                        f"{identifier}: Official Florida Senate {related_artifact_type}"
                        if create_finding and related_artifact_type else bill_title
                    )
                    source.official = True
                    source.retrieval_mechanism = "reused" if exact_raw_ref else mechanism
                    source.content_hash = digest
                    source.document_hash = digest
                    source.raw_ref = raw_ref
                    source.prior_source_id = prior_id
                    source.change_kind = change.kind if change else None
                    source.change_summary = change.summary if change else None
                    # Server defaults on older SQLite/PostgreSQL configurations can
                    # collapse multiple observations into one second. Persist the
                    # actual finalization instant so URL history has a stable newest
                    # observation for A→B→A comparisons.
                    source.retrieved_at = datetime.now(timezone.utc)
                    action = metadata.get("latest_action")
                    action_date = metadata.get("latest_action_date") or metadata.get("status_date")
                    if create_finding and related_artifact_type:
                        assert evidence is not None
                        excerpt, excerpt_start, excerpt_end = evidence
                        finding = ScoutFinding(
                            job_id=job_id,
                            source_id=source.id,
                            title=f"{identifier}: Official Florida Senate {related_artifact_type}",
                            what_happened=(
                                f"Official Florida Senate {related_artifact_type} retrieved for {identifier}."
                            ),
                            why_it_matters=(
                                "This bill-scoped primary document was discovered from the official Senate bill page "
                                "and retained as evidence."
                            ),
                            relevant_date=None,
                            excerpt=excerpt,
                            excerpt_hash=content_hash(excerpt.encode()),
                            excerpt_start=excerpt_start,
                            excerpt_end=excerpt_end,
                            confidence="high",
                            extractor_version="scout-p0-2-related",
                            bill_id=bill_id,
                        )
                    elif create_finding:
                        assert evidence is not None
                        excerpt, excerpt_start, excerpt_end = evidence
                        development = f"Latest structured action{f' ({action_date.isoformat()})' if isinstance(action_date, date) else ''}: {action}" if action else f"Structured Florida status: {bill_status or 'unreported'}"
                        # _evidence_excerpt refuses a finding unless this exact
                        # displayed window supports both the identifier and action.
                        finding = ScoutFinding(job_id=job_id, source_id=source.id, title=f"{identifier}: {bill_title}", what_happened=development, why_it_matters="The structured development is paired with retained official source bytes.", relevant_date=action_date if isinstance(action_date, date) else None, excerpt=excerpt, excerpt_hash=content_hash(excerpt.encode()), excerpt_start=excerpt_start, excerpt_end=excerpt_end, confidence="high", extractor_version="scout-p0-1", bill_id=bill_id)
                    if create_finding:
                        db.add(finding)
                    persisted_mechanism = "reused" if exact_raw_ref else mechanism
                    db.add(ScoutJobEvent(job_id=job_id, kind="document_inspected", detail={"mechanism": persisted_mechanism, "mime_type": mime_base}))
                    db.add(ScoutJobEvent(job_id=job_id, kind="source_persisted", detail={"mechanism": persisted_mechanism, "change_kind": change.kind if change else None}))
                    if create_finding:
                        db.add(ScoutJobEvent(job_id=job_id, kind="finding_persisted", detail={"mechanism": persisted_mechanism}))
                    db.commit()
                    return source.id
        except Exception:
            # A database/provenance failure is isolated to this source; the
            # outer loop can still publish a truthful partial outcome.
            return None

    @staticmethod
    def _evidence_excerpt(text: str, metadata: dict, bill_status: str | None, *, maximum: int = 500) -> tuple[str, int, int] | None:
        """Return one bounded displayed window supporting the claimed finding."""
        display_text = _SPACE_RE.sub(" ", text).strip()
        normalized = display_text.casefold()
        if not normalized:
            return None
        identifier = _SPACE_RE.sub(" ", str(metadata.get("identifier") or "")).strip().casefold()
        action = _SPACE_RE.sub(" ", str(metadata.get("latest_action") or bill_status or "")).strip().casefold()
        if not identifier or not action:
            return None
        identifier_positions = [match.start() for match in re.finditer(re.escape(identifier), normalized)]
        action_positions = [match.start() for match in re.finditer(re.escape(action), normalized)]
        if not identifier_positions or not action_positions:
            return None
        identifier_start, action_start = min(
            ((left, right) for left in identifier_positions for right in action_positions),
            key=lambda pair: abs(pair[0] - pair[1]),
        )
        support_start = min(identifier_start, action_start)
        support_end = max(identifier_start + len(identifier), action_start + len(action))
        if support_end - support_start > maximum:
            return None
        start = max(0, support_start - (maximum - (support_end - support_start)) // 2)
        end = min(len(normalized), start + maximum)
        start = max(0, end - maximum)
        excerpt = display_text[start:end]
        if identifier not in excerpt.casefold() or action not in excerpt.casefold():  # defensive: preserve the display invariant.
            return None
        return excerpt, start, end

    @staticmethod
    def _related_evidence_excerpt(text: str, metadata: dict, *, maximum: int = 500) -> tuple[str, int, int] | None:
        """Require the linked bill identifier in an adjacent primary document.

        The artifact category is constrained by the canonical Senate URL, and
        the finding makes no claim about its substantive contents. The excerpt
        therefore only needs to prove that this retained document identifies
        the same bill as the page from which it was discovered.
        """
        display_text = _SPACE_RE.sub(" ", text).strip()
        identifier = _SPACE_RE.sub(" ", str(metadata.get("identifier") or "")).strip()
        if not display_text or not identifier:
            return None
        position = display_text.casefold().find(identifier.casefold())
        if position < 0:
            return None
        start = max(0, position - (maximum - len(identifier)) // 2)
        end = min(len(display_text), start + maximum)
        start = max(0, end - maximum)
        excerpt = display_text[start:end]
        if identifier.casefold() not in excerpt.casefold():
            return None
        return excerpt, start, end

    def _browser_capture(
        self,
        job_id: uuid.UUID,
        token: str,
        bill_id: uuid.UUID | None,
        bill_title: str,
        bill_status: str | None,
        metadata: dict,
        url: str,
        *,
        create_finding: bool = True,
        required_markers: tuple[bytes, ...] = (),
    ) -> tuple[bool, str | None]:
        with self.sessions() as db:
            active_job = self._fenced(db, job_id, token)
            if active_job is None:
                return False, None
            max_pages = self._job_limit(active_job, "max_pages", self.settings.max_pages)
            max_actions = self._job_limit(active_job, "max_actions", self.settings.max_actions)
            browser_wall_seconds = self._job_limit(
                active_job, "browser_wall_seconds", self.settings.browser_wall_seconds
            )
            browser_cleanup_seconds = self._job_limit(
                active_job, "browser_cleanup_seconds", self.settings.browser_cleanup_seconds
            )
        # The global slot and its external-request reservation are one locked
        # transaction. A full browser cap must not consume request budget, and
        # a consumed request must always have a durable zero-usage slot.
        session_id, reservation_reason = self._reserve_browser_slot_with_reason(job_id, token)
        if session_id is None:
            return False, reservation_reason
        capture: BrowserCapture | None = None
        started: float | None = None
        provider_id: str | None = None
        durable_provider_id = False
        create_outcome_unknown = False

        def on_started(created_provider_id: str) -> None:
            nonlocal provider_id, durable_provider_id
            # This transaction couples the durable provider ID and usage
            # increment. A failed callback is propagated with the opaque ID;
            # this runner still owns the one remote release in its finally.
            self._record_browser_started(session_id, job_id, token, created_provider_id)
            provider_id = created_provider_id
            durable_provider_id = True

        try:
            started = time.monotonic()
            capture = self.provider.capture(
                BrowserRequest(url=url, max_pages=max_pages, max_actions=max_actions, wall_seconds=browser_wall_seconds, max_bytes=self._job_limit(active_job, "max_direct_bytes", self.settings.max_direct_bytes), max_routed_requests=self._job_limit(active_job, "max_routed_requests", self.settings.max_browser_routed_requests), cleanup_seconds=browser_cleanup_seconds),
                on_started=on_started,
            )
            # Provider implementations must call on_started immediately after
            # create. Keep defensive support for a malformed implementation
            # that returned an ID but skipped the callback.
            if provider_id is None:
                self._record_browser_started(session_id, job_id, token, capture.provider_session_id)
                provider_id = capture.provider_session_id
                durable_provider_id = True
            if not self._heartbeat(job_id, token):
                return False, None
            max_routed_requests = self._job_limit(
                active_job, "max_routed_requests", self.settings.max_browser_routed_requests
            )
            if (
                capture.pages > max_pages
                or capture.actions > max_actions
                or capture.routed_requests > max_routed_requests
            ):
                if capture.routed_requests > max_routed_requests:
                    self._record_routed_request_limit(
                        session_id, job_id, token, capture.routed_requests, max_routed_requests,
                    )
                raise RuntimeError("browser_limit_exceeded")
            if required_markers and not all(marker in capture.body for marker in required_markers):
                raise RuntimeError("browser_unexpected_content")
            final_url = canonicalize_url(capture.url)
            source_id = self._persist_capture(
                job_id, token, bill_id, bill_title, bill_status, metadata,
                final_url, "browser", 200, capture.mime_type, capture.body,
                create_finding=create_finding,
            )
            if source_id is None:
                return False, None
            with self.sessions() as db:
                session = db.get(ScoutBrowserSession, session_id)
                if session is not None:
                    session.pages = capture.pages
                    session.actions = capture.actions
                    session.source_id = source_id
                    session.runtime_ms = int((time.monotonic() - started) * 1000)
                    session.routed_requests = capture.routed_requests
                    db.commit()
            self._heartbeat(job_id, token, "browser_pages", capture.pages)
            self._heartbeat(job_id, token, "browser_actions", capture.actions)
            self._heartbeat(job_id, token, "browser_routed_requests", capture.routed_requests)
            return True, None
        except ProviderSessionPersistenceError as exc:
            # Retry only the durable ledger write; if DB recovered we can
            # record and release the true provider ID exactly once. Never log
            # the opaque value.
            provider_id = exc.provider_session_id
            durable_provider_id = self._recover_browser_session(session_id, job_id, token, provider_id)
            if not durable_provider_id and self._browser_slot_abandoned(session_id):
                # The reaper already declared this ID-less slot terminal. The
                # The callback was rejected after the reaper terminalized the
                # slot. We still hold a real provider ID, so retain it for the
                # untracked one-shot release below without reviving the slot.
                durable_provider_id = False
            return False, None
        except SolariProviderError as exc:
            if exc.phase == "create" and provider_id is None:
                # A one-shot create response may be lost after the provider
                # accepted it. There is no ID to release or reconcile, so
                # retain a truthful ledger state and charge the full frozen
                # session reservation instead of claiming nothing started.
                create_outcome_unknown = True
            return False, "browser_create_outcome_unknown" if create_outcome_unknown else None
        except Exception:
            return False, "browser_routed_request_limit" if capture is not None and capture.routed_requests > self._job_limit(active_job, "max_routed_requests", self.settings.max_browser_routed_requests) else None
        finally:
            # Failed/partial provider runs still consume paid time. Persist it
            # before cleanup so daily spend accounting cannot ignore failures.
            if started is not None:
                try:
                    with self.sessions() as db:
                        session = db.get(ScoutBrowserSession, session_id)
                        if session is not None:
                            session.runtime_ms = max(session.runtime_ms or 0, int((time.monotonic() - started) * 1000))
                            if capture is not None:
                                session.pages = max(session.pages, capture.pages)
                                session.actions = max(session.actions, capture.actions)
                                session.routed_requests = max(session.routed_requests, capture.routed_requests)
                            db.commit()
                except Exception:
                    # Do not let best-effort runtime telemetry prevent the
                    # provider-ID finalization/release path below.
                    pass
            if capture is not None:
                provider_id = capture.provider_session_id
            if provider_id and durable_provider_id:
                self._release_browser_session(session_id, provider_id, cleanup_seconds=browser_cleanup_seconds)
            elif provider_id:
                # Callback persistence may have failed because the DB was
                # transiently unavailable. The provider ID means this slot is
                # never safe to delete: attempt a second idempotent release,
                # then finalize the pre-reserved slot without reusing the
                # failing callback transaction.
                released, replay = self._release_untracked_provider(
                    provider_id, cleanup_seconds=browser_cleanup_seconds
                )
                self._finalize_untracked_browser_slot(session_id, job_id, provider_id, released, replay)
            elif create_outcome_unknown:
                self._mark_unknown_browser_creation(session_id, job_id)
            else:
                self._discard_unstarted_browser_slot(session_id)

    def _reserve_browser_slot(self, job_id: uuid.UUID, token: str) -> uuid.UUID | None:
        """Compatibility wrapper for callers that only need a reservation."""
        session_id, _reason = self._reserve_browser_slot_with_reason(job_id, token)
        return session_id

    def _reserve_browser_slot_with_reason(self, job_id: uuid.UUID, token: str) -> tuple[uuid.UUID | None, str | None]:
        """Reserve one durable zero-usage slot under the global browser lock."""
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return None, None
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(81420901)"))
            active = db.query(ScoutBrowserSession).filter(
                ScoutBrowserSession.status.in_(("starting", "running", "cleanup_failed", "reaping"))
            ).count()
            if active >= self.settings.max_concurrent_browser_sessions:
                db.add(ScoutJobEvent(job_id=job_id, kind="browser_skipped", detail={"reason": "global_limit"}))
                db.commit()
                return None, "global_limit"
            usage = dict(job.usage or {})
            if int(usage.get("external_requests", 0)) >= self._job_limit(
                job, "max_external_requests", self.settings.max_external_requests
            ):
                db.add(ScoutJobEvent(job_id=job_id, kind="browser_skipped", detail={"reason": "external_request_limit"}))
                db.commit()
                return None, "external_request_limit"
            usage["external_requests"] = int(usage.get("external_requests", 0)) + 1
            job.usage = usage
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + timedelta(seconds=self.settings.lease_seconds)
            session = ScoutBrowserSession(job_id=job_id, provider=self.provider.__class__.__name__, status="starting")
            db.add(session)
            db.commit()
            db.refresh(session)
            return session.id, None

    def _record_routed_request_limit(
        self, session_id: uuid.UUID, job_id: uuid.UUID, token: str, used: int, limit: int,
    ) -> None:
        """Durably retain the provider-reported routed-request overrun."""
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            session = db.get(ScoutBrowserSession, session_id)
            if job is None or session is None or session.job_id != job_id:
                return
            session.routed_requests = max(session.routed_requests, used)
            usage = dict(job.usage or {})
            usage["browser_routed_requests"] = int(usage.get("browser_routed_requests", 0)) + used
            job.usage = usage
            db.add(ScoutJobEvent(job_id=job_id, kind="browser_routed_request_limit_reached", detail={
                "limit": limit, "used": used,
            }))
            db.commit()

    def _record_browser_started(self, session_id: uuid.UUID, job_id: uuid.UUID, token: str, provider_id: str) -> None:
        """Atomically persist a real provider ID and its browser-session usage."""
        if not provider_id:
            raise RuntimeError("missing_provider_session_id")
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                raise RuntimeError("browser_job_not_fenced")
            session = db.get(ScoutBrowserSession, session_id)
            if session is None or session.job_id != job_id:
                raise RuntimeError("browser_slot_not_found")
            if session.provider_session_id is None:
                # A reaper can terminalize an ID-less reservation once its
                # owner is no longer live.  A delayed provider callback must
                # fail back into the provider's cleanup path, never turn that
                # abandoned capacity back into a live session.
                lease_expires_at = job.lease_expires_at
                if (
                    session.status != "starting"
                    or (
                        lease_expires_at is not None
                        and (lease_expires_at.replace(tzinfo=timezone.utc) if lease_expires_at.tzinfo is None else lease_expires_at)
                        <= datetime.now(timezone.utc)
                    )
                ):
                    raise RuntimeError("browser_slot_no_longer_startable")
                session.provider_session_id = provider_id
                session.status = "running"
                usage = dict(job.usage or {})
                usage["browser_sessions"] = int(usage.get("browser_sessions", 0)) + 1
                job.usage = usage
                db.add(ScoutJobEvent(job_id=job_id, kind="browser_started", detail={"provider": session.provider}))
            elif session.provider_session_id != provider_id:
                raise RuntimeError("provider_session_id_conflict")
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + timedelta(seconds=self.settings.lease_seconds)
            db.commit()

    def _recover_browser_session(self, session_id: uuid.UUID, job_id: uuid.UUID, token: str, provider_id: str) -> bool:
        """Best-effort durable recovery after a callback transaction failure."""
        try:
            self._record_browser_started(session_id, job_id, token, provider_id)
            return True
        except Exception:
            return False

    def _discard_unstarted_browser_slot(self, session_id: uuid.UUID) -> None:
        """Terminalize an ID-less reservation without consuming browser capacity."""
        with self.sessions() as db:
            session = db.get(ScoutBrowserSession, session_id)
            if session is not None and session.provider_session_id is None and session.status == "starting":
                session.status = "abandoned"
                session.error_class = "abandoned_before_provider_id"
                session.released_at = datetime.now(timezone.utc)
                db.commit()

    def _mark_unknown_browser_creation(
        self, session_id: uuid.UUID, job_id: uuid.UUID
    ) -> None:
        """Record an unreconcilable one-shot create outcome without lying.

        Solari does not expose create idempotency keys or list-by-request. A
        lost successful response therefore has no releasable ID. The API cost
        ledger treats this marker as a full session reservation for the day.
        """
        with self.sessions() as db:
            session = db.get(ScoutBrowserSession, session_id)
            if (
                session is None
                or session.job_id != job_id
                or session.provider_session_id is not None
            ):
                return
            # Keep this row globally live for the full frozen drive + cleanup
            # horizon. The provider may have accepted the create even though
            # Scout never received an ID; immediately freeing the slot could
            # exceed the configured cloud-browser concurrency ceiling.
            session.status = "cleanup_failed"
            session.error_class = "create_outcome_unknown"
            session.cleanup_attempted_at = datetime.now(timezone.utc)
            db.add(ScoutJobEvent(
                job_id=job_id,
                kind="browser_create_outcome_unknown",
                detail={"accounting": "full_session_reservation"},
            ))
            db.commit()

    def _browser_slot_abandoned(self, session_id: uuid.UUID) -> bool:
        with self.sessions() as db:
            session = db.get(ScoutBrowserSession, session_id)
            return session is not None and session.status == "abandoned"

    def _provider_release(self, provider_id: str, cleanup_seconds: int) -> str | None:
        """Call either provider release contract once, decided before I/O."""
        release = self.provider.release
        try:
            parameters = inspect.signature(release).parameters.values()
            supports_cleanup = any(
                (
                    parameter.name == "cleanup_seconds"
                    and parameter.kind in {
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    }
                )
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_cleanup = False
        if supports_cleanup:
            return release(provider_id, cleanup_seconds=cleanup_seconds)
        return release(provider_id)

    def _release_untracked_provider(
        self, provider_id: str, *, cleanup_seconds: int
    ) -> tuple[bool, str | None]:
        """One bounded cleanup retry for a provider ID whose callback failed."""
        try:
            replay = _bounded_call(
                self._provider_release, provider_id, cleanup_seconds, timeout=cleanup_seconds
            )
        except TimeoutError:
            return False, None
        except Exception:
            return False, None
        return True, replay

    def _finalize_untracked_browser_slot(
        self,
        session_id: uuid.UUID,
        job_id: uuid.UUID,
        provider_id: str,
        released: bool,
        replay: str | None,
    ) -> bool:
        """Finalize a pre-reserved slot after callback persistence failed.

        This intentionally avoids the claim-fenced callback helper: the
        provider existed, so accounting/release truth remains required even if
        the original worker claim was canceled or the callback DB transaction
        failed. If this transaction cannot commit, leave ``starting`` intact
        so the global cap continues to protect spend until a reaper can act.
        """
        try:
            with self.sessions() as db:
                session = db.get(ScoutBrowserSession, session_id)
                job = db.get(ScoutResearchJob, job_id)
                if session is None or job is None or session.job_id != job_id:
                    return False
                if session.provider_session_id is None:
                    # A late callback for a reaped ID-less slot is released by
                    # the caller before this finalization. Do not recreate
                    # ledger usage or consume capacity after abandonment.
                    if session.status != "starting":
                        return False
                    session.provider_session_id = provider_id
                    usage = dict(job.usage or {})
                    usage["browser_sessions"] = int(usage.get("browser_sessions", 0)) + 1
                    job.usage = usage
                elif session.provider_session_id != provider_id:
                    return False
                if released:
                    session.status = "released"
                    session.replay_url = replay
                    session.error_class = None if replay else "replay_pending:0"
                    session.released_at = datetime.now(timezone.utc)
                else:
                    session.status = "cleanup_failed"
                    session.error_class = "cleanup_failed"
                db.commit()
                return True
        except Exception:
            return False

    def _claim_browser_cleanup(
        self, session_id: uuid.UUID, provider_id: str | None, *, cleanup_seconds: int | None = None
    ) -> str | None:
        """Claim one persisted provider ID for cleanup before external I/O.

        Both workers and reapers use this transition.  ``reaping`` is the
        durable ownership token for a single release attempt; a later retry is
        permitted only after that attempt records ``cleanup_failed`` (or a
        killed cleaner becomes stale).
        """
        now = datetime.now(timezone.utc)
        with self.sessions() as db:
            session = db.execute(select(ScoutBrowserSession).where(
                ScoutBrowserSession.id == session_id,
            ).with_for_update()).scalar_one_or_none()
            if session is None or not session.provider_session_id:
                return None
            if provider_id is not None and session.provider_session_id != provider_id:
                return None
            attempted_at = session.cleanup_attempted_at
            if attempted_at is not None and attempted_at.tzinfo is None:
                attempted_at = attempted_at.replace(tzinfo=timezone.utc)
            cleanup_seconds = cleanup_seconds or self.settings.browser_cleanup_seconds
            stale_reaping = session.status == "reaping" and (
                attempted_at is None
                or attempted_at < now - timedelta(seconds=cleanup_seconds)
            )
            if session.status not in {"starting", "running", "cleanup_failed"} and not stale_reaping:
                return None
            session.status = "reaping"
            session.cleanup_attempted_at = now
            db.commit()
            return session.provider_session_id

    def _touch_browser_cleanup(self, session_id: uuid.UUID, provider_id: str) -> bool:
        """Keep an outcome-unknown release non-retryable while its call lives."""
        with self.sessions() as db:
            session = db.execute(select(ScoutBrowserSession).where(
                ScoutBrowserSession.id == session_id,
            ).with_for_update()).scalar_one_or_none()
            if session is None or session.status != "reaping" or session.provider_session_id != provider_id:
                return False
            session.cleanup_attempted_at = datetime.now(timezone.utc)
            # Once the bounded release wait timed out, retain that explicit
            # classification while its daemon call keeps the admission hold.
            if session.error_class != "cleanup_timeout_inflight":
                session.error_class = "cleanup_outcome_unknown"
            db.commit()
            return True

    def _complete_browser_cleanup(
        self, session_id: uuid.UUID, provider_id: str, *, replay: str | None = None,
        error_class: str | None = None,
    ) -> bool:
        """Settle the cleanup claim only after the provider call has returned."""
        with self.sessions() as db:
            session = db.execute(select(ScoutBrowserSession).where(
                ScoutBrowserSession.id == session_id,
            ).with_for_update()).scalar_one_or_none()
            if session is None or session.status != "reaping" or session.provider_session_id != provider_id:
                return False
            if error_class is not None:
                session.status = "cleanup_failed"
                session.error_class = error_class
                session.cleanup_attempted_at = datetime.now(timezone.utc)
            else:
                session.status = "released"
                session.replay_url = replay
                session.error_class = None if replay else "replay_pending:0"
                session.released_at = datetime.now(timezone.utc)
            db.commit()
            return error_class is None

    def _release_browser_session(
        self, session_id: uuid.UUID, provider_id: str | None, *, cleanup_seconds: int | None = None
    ) -> bool:
        """Release only after winning and continuously owning the durable claim.

        Python cannot kill a provider call safely. If the local wait expires,
        a daemon keepalive leaves the row ``reaping`` until that exact call
        returns. Another process may retry only after the keepalive disappears
        (process death) or the call returns an actual failure.
        """
        # A process-local registry closes the remaining gap when a durable
        # keepalive write fails but another runner in this worker process can
        # still reach the database. Process death clears the registry; the
        # durable stale-`reaping` path then recovers the orphan.
        with _INFLIGHT_CLEANUP_LOCK:
            if session_id in _INFLIGHT_CLEANUPS:
                return False
            _INFLIGHT_CLEANUPS.add(session_id)
        cleanup_seconds = cleanup_seconds or self.settings.browser_cleanup_seconds
        try:
            provider_id = self._claim_browser_cleanup(
                session_id, provider_id, cleanup_seconds=cleanup_seconds
            )
        except Exception:
            with _INFLIGHT_CLEANUP_LOCK:
                _INFLIGHT_CLEANUPS.discard(session_id)
            raise
        if not provider_id:
            with _INFLIGHT_CLEANUP_LOCK:
                _INFLIGHT_CLEANUPS.discard(session_id)
            return False
        done = threading.Event()
        outcome = {"released": False}
        interval = max(0.05, min(1.0, cleanup_seconds / 3))

        def keepalive() -> None:
            while not done.wait(interval):
                try:
                    owned = self._touch_browser_cleanup(session_id, provider_id)
                except Exception:
                    # A transient ledger outage also prevents a healthy reaper
                    # from claiming. Keep trying so ownership refreshes as soon
                    # as the database returns while the provider call is alive.
                    continue
                if not owned:
                    return

        def release() -> None:
            try:
                replay = self._provider_release(provider_id, cleanup_seconds)
            except BaseException:
                self._complete_browser_cleanup(
                    session_id, provider_id, error_class="cleanup_failed"
                )
            else:
                outcome["released"] = self._complete_browser_cleanup(
                    session_id, provider_id, replay=replay
                )
            finally:
                with _INFLIGHT_CLEANUP_LOCK:
                    _INFLIGHT_CLEANUPS.discard(session_id)
                done.set()

        threading.Thread(
            target=keepalive, name="scout-cleanup-keepalive", daemon=True
        ).start()
        threading.Thread(
            target=release, name="scout-provider-release", daemon=True
        ).start()
        if not done.wait(cleanup_seconds):
            # Refresh after the boundary so an immediate reaper cannot call
            # the provider while this outcome-unknown call is still alive.
            try:
                self._touch_browser_cleanup(session_id, provider_id)
                with self.sessions() as db:
                    session = db.get(ScoutBrowserSession, session_id)
                    if session is not None and session.status == "reaping":
                        session.error_class = "cleanup_timeout_inflight"
                        db.commit()
            except Exception:
                pass
            return False
        return bool(outcome["released"])

    def reap_sessions(self) -> int:
        """Claim eligible orphan cleanup atomically, then release outside DB I/O.

        Fresh running sessions belong to their worker until that job is
        canceled/terminal or its lease expires.  `reaping` is recoverable after
        the cleanup timeout, so a killed reaper never permanently consumes cap.
        """
        ids: list[tuple[uuid.UUID, str, int]] = []
        replay_ids: list[tuple[uuid.UUID, str, int]] = []
        now = datetime.now(timezone.utc)
        with self.sessions() as db:
            rows = db.execute(
                select(ScoutBrowserSession, ScoutResearchJob)
                .join(ScoutResearchJob, ScoutResearchJob.id == ScoutBrowserSession.job_id)
                .where(ScoutBrowserSession.status.in_(("starting", "running", "cleanup_failed", "reaping")))
                .with_for_update(skip_locked=True)
            ).all()
            for row, job in rows:
                lease_expires_at = job.lease_expires_at
                if lease_expires_at is not None and lease_expires_at.tzinfo is None:
                    lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
                cleanup_attempted_at = row.cleanup_attempted_at
                if cleanup_attempted_at is not None and cleanup_attempted_at.tzinfo is None:
                    cleanup_attempted_at = cleanup_attempted_at.replace(tzinfo=timezone.utc)
                terminal_or_expired = job.status in {"completed", "partial", "failed", "canceled"} or (
                    lease_expires_at is not None and lease_expires_at < now
                )
                cleanup_seconds = self._job_limit(
                    job, "browser_cleanup_seconds", self.settings.browser_cleanup_seconds
                )
                stale_reaping = row.status == "reaping" and (
                    cleanup_attempted_at is None
                    or cleanup_attempted_at < now - timedelta(seconds=cleanup_seconds)
                )
                eligible = row.status == "cleanup_failed" or (row.status in {"starting", "running"} and terminal_or_expired) or stale_reaping
                if not eligible:
                    continue
                if not row.provider_session_id:
                    if row.error_class == "create_outcome_unknown":
                        # Anchor the conservative hold when the create became
                        # outcome-unknown, not when the local slot was first
                        # reserved. A slow create may consume nearly the whole
                        # drive timeout before its accepted response is lost.
                        hold_started_at = row.cleanup_attempted_at or row.created_at
                        if hold_started_at.tzinfo is None:
                            hold_started_at = hold_started_at.replace(tzinfo=timezone.utc)
                        unknown_hold_seconds = self._job_limit(
                            job,
                            "browser_wall_seconds",
                            self.settings.browser_wall_seconds,
                        ) + cleanup_seconds
                        if hold_started_at + timedelta(seconds=unknown_hold_seconds) > now:
                            continue
                    # No remote resource was ever identified. A stale/dead
                    # reservation must stop consuming the global cap, but is
                    # not falsely reported as released.
                    row.status = "abandoned"
                    row.error_class = (
                        "create_outcome_unknown_expired"
                        if row.error_class == "create_outcome_unknown"
                        else "abandoned_without_provider_id"
                    )
                    row.cleanup_attempted_at = now
                    row.released_at = now
                    continue
                ids.append((row.id, row.provider_session_id, cleanup_seconds))
            for row in db.execute(select(ScoutBrowserSession).where(
                ScoutBrowserSession.status == "released", ScoutBrowserSession.replay_url.is_(None)
            )).scalars():
                attempts = self._replay_attempts(row.error_class)
                released_at = row.released_at or row.created_at
                if released_at.tzinfo is None:
                    released_at = released_at.replace(tzinfo=timezone.utc)
                deadline = released_at + timedelta(seconds=self.settings.replay_probe_window_seconds)
                if row.provider_session_id and attempts < self.settings.replay_probe_attempts and now <= deadline:
                    replay_ids.append((row.id, row.provider_session_id, attempts))
                elif row.error_class != "replay_unavailable":
                    row.error_class = "replay_unavailable"
            db.commit()
        released = 0
        for session_id, provider_id, cleanup_seconds in ids:
            released += int(
                self._release_browser_session(
                    session_id, provider_id, cleanup_seconds=cleanup_seconds
                )
            )
        for session_id, provider_id, attempts in replay_ids:
            self._probe_replay(session_id, provider_id, attempts)
        return released + len(replay_ids)

    def reap_staging_blobs(self) -> dict[str, int]:
        """Delete expired unverified stages and old unreferenced raw bytes.

        Final evidence remains immutable. Only terminal jobs' unverified
        staging rows age out, and a raw blob is deleted only when it is older
        than the same safety window and no Scout source references its hash.
        The age check protects the short interval between a concurrent
        content-addressed put and attaching its reference to the staged row.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.settings.staging_retention_seconds
        )
        terminal_statuses = ("completed", "partial", "failed", "canceled")
        with self.sessions() as db:
            stale_stages = select(ScoutSource.id).join(
                ScoutResearchJob, ScoutResearchJob.id == ScoutSource.job_id
            ).where(
                ScoutSource.retrieval_mechanism == "staged",
                ScoutSource.official.is_(False),
                ScoutSource.retrieved_at < cutoff,
                ScoutResearchJob.status.in_(terminal_statuses),
            )
            source_result = db.execute(
                delete(ScoutSource).where(ScoutSource.id.in_(stale_stages))
            )
            referenced = exists(
                select(ScoutSource.id).where(
                    ScoutSource.raw_ref == ScoutRawBlob.sha256
                )
            )
            # A stage is committed before RawStore.put. An identical old
            # orphan may therefore be returned by put just before this worker
            # attaches its hash. Treat any fresh unresolved stage as a short
            # global GC barrier. Crashed barriers age out with the same cutoff;
            # active finalizers cannot lose bytes between put and attachment.
            unresolved_fresh_stage = exists(
                select(ScoutSource.id).where(
                    ScoutSource.retrieval_mechanism == "staged",
                    ScoutSource.raw_ref.is_(None),
                    ScoutSource.retrieved_at >= cutoff,
                )
            )
            blob_result = db.execute(
                delete(ScoutRawBlob).where(
                    ScoutRawBlob.created_at < cutoff,
                    ~referenced,
                    ~unresolved_fresh_stage,
                )
            )
            db.commit()
            return {
                "staged_sources": max(0, source_result.rowcount or 0),
                "raw_blobs": max(0, blob_result.rowcount or 0),
            }

    @staticmethod
    def _replay_attempts(error_class: str | None) -> int:
        if error_class and error_class.startswith("replay_pending:"):
            try:
                return int(error_class.rsplit(":", 1)[1])
            except ValueError:
                pass
        return 0

    def _probe_replay(self, session_id: uuid.UUID, provider_id: str, attempts: int) -> None:
        try:
            try:
                replay = _bounded_call(self.provider.probe_replay, provider_id, timeout=self.settings.browser_cleanup_seconds)
            except TimeoutError:
                replay = None
            with self.sessions() as db:
                session = db.get(ScoutBrowserSession, session_id)
                if session is not None and session.status == "released" and session.replay_url is None:
                    session.replay_url = replay
                    session.error_class = None if replay else f"replay_pending:{attempts + 1}"
                    db.commit()
        except Exception:
            with self.sessions() as db:
                session = db.get(ScoutBrowserSession, session_id)
                if session is not None and session.status == "released" and session.replay_url is None:
                    session.error_class = f"replay_pending:{attempts + 1}"
                    db.commit()

    def _mark_browser_running(self, session_id: uuid.UUID, provider_id: str) -> None:
        with self.sessions() as db:
            session = db.get(ScoutBrowserSession, session_id)
            if session is not None:
                session.status = "running"
                session.provider_session_id = provider_id
                db.commit()

    def _heartbeat(self, job_id: uuid.UUID, token: str, usage_key: str | None = None, usage_increment: int = 1) -> bool:
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is not None:
                job.heartbeat_at = datetime.now(timezone.utc)
                job.lease_expires_at = job.heartbeat_at + timedelta(seconds=self.settings.lease_seconds)
                if usage_key:
                    usage = dict(job.usage or {})
                    usage[usage_key] = int(usage.get(usage_key, 0)) + usage_increment
                    job.usage = usage
                db.commit()
                return True
        return False

    def rollback_reconcile(self) -> dict[str, int]:
        """Safe rollback operator action, intentionally available while dark.

        It only terminalizes queued jobs and running jobs whose lease is
        already expired; a fresh claim retains ownership and is never killed.
        """
        reaped = self.reap_sessions()
        now = datetime.now(timezone.utc)
        terminalized = 0
        with self.sessions() as db:
            jobs = db.execute(
                select(ScoutResearchJob)
                .where(or_(ScoutResearchJob.status == "queued", and_(ScoutResearchJob.status == "running", ScoutResearchJob.lease_expires_at < now)))
                .with_for_update(skip_locked=True)
            ).scalars()
            for job in jobs:
                job.status = "failed"
                job.error_class = "rolled_back"
                job.completed_at = now
                job.lease_expires_at = None
                job.claim_owner = None
                job.claim_token = None
                db.add(ScoutJobEvent(job_id=job.id, kind="finished", detail={"status": "failed", "error_class": "rolled_back"}))
                terminalized += 1
            db.commit()
        return {"reaped": reaped, "terminalized": terminalized}

    def _reserve_external_attempt(self, job_id: uuid.UUID, token: str) -> bool:
        """Reserve one source retrieval before issuing it; never exceed the persisted cap."""
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return False
            usage = dict(job.usage or {})
            if int(usage.get("external_requests", 0)) >= self._job_limit(job, "max_external_requests", self.settings.max_external_requests):
                db.add(ScoutJobEvent(job_id=job_id, kind="external_request_limit_reached", detail={
                    "limit": self._job_limit(job, "max_external_requests", self.settings.max_external_requests),
                }))
                db.commit()
                return False
            usage["external_requests"] = int(usage.get("external_requests", 0)) + 1
            job.usage = usage
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + timedelta(seconds=self.settings.lease_seconds)
            db.commit()
            return True

    def _finish(self, db: Session, job: ScoutResearchJob, token: str, status: str, error_class: str | None, partial: bool) -> None:
        if job.status != "running" or job.claim_token != token:
            return
        job.status = status
        job.error_class = error_class
        job.partial_success = partial
        job.completed_at = datetime.now(timezone.utc)
        job.lease_expires_at = None
        job.claim_token = None
        if status in {"completed", "partial"}:
            job.fresh_until = datetime.now(timezone.utc) + timedelta(seconds=self.settings.cache_ttl_seconds)
        db.add(ScoutJobEvent(job_id=job.id, kind="finished", detail={"status": status, "error_class": error_class}))
        db.commit()
