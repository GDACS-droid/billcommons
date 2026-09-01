"""DB-backed Scout queue runner; no database transaction spans external I/O."""
from __future__ import annotations

import re
import secrets
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from billcommons_schema.models import Bill, BillSubject, Jurisdiction, ScoutBrowserSession, ScoutFinding, ScoutJobEvent, ScoutResearchJob, ScoutSource, Session as LegislativeSession
from billcommons_shared.rawstore import RawStore
from billcommons_shared.safe_http import SafeHttpError, SsrfRejected, new_safe_http_client
from billcommons_shared.scout import (
    BrowserCapture, BrowserRequest, ResearchBrowserProvider, ScoutPolicyError,
    ScoutSettings, browser_required, canonicalize_url, classify_direct_response,
    content_hash, extract_florida_bill_identifier, topical_search_terms,
)
from billcommons_scout.providers import ProviderSessionPersistenceError

Fetcher = Callable[[str], tuple[int, str | None, bytes]]
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SHELL_MARKERS = (
    "sign in", "log in", "login", "maintenance", "temporarily unavailable",
    "enable javascript", "javascript is required", "please enable javascript",
    "loading...", "loading…",
)
@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    token: str


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
        self.fetcher = fetcher or (lambda url: safe_direct_fetch(url, max_body_bytes=self.settings.max_direct_bytes))

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

    def _canceled(self, db: Session, job: ScoutResearchJob, version: int, token: str) -> bool:
        db.refresh(job)
        return job.status != "running" or job.claim_token != token or job.cancel_version != version

    @staticmethod
    def _fenced(db: Session, job_id: uuid.UUID, token: str) -> ScoutResearchJob | None:
        return db.execute(select(ScoutResearchJob).where(
            ScoutResearchJob.id == job_id,
            ScoutResearchJob.status == "running",
            ScoutResearchJob.claim_token == token,
        )).scalar_one_or_none()

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
        ).where(Jurisdiction.abbreviation == "FL")
        if identifier is not None:
            # Identifiers are unique only within a legislative session.  A
            # direct lookup must therefore select one current/newest session,
            # never investigate two different HB/SB records as one bill.
            rows = db.execute(
                stmt.where(Bill.identifier_norm == identifier).order_by(
                    LegislativeSession.active.desc(),
                    LegislativeSession.end_date.desc(),
                    LegislativeSession.start_date.desc(),
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
                LegislativeSession.end_date.desc(),
                LegislativeSession.start_date.desc(),
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

    def process(self, job_id: uuid.UUID, claim_token: str | None = None) -> None:
        """Perform slow I/O after the claim transaction has committed."""
        with self.sessions() as db:
            job = db.get(ScoutResearchJob, job_id)
            if job is None or job.status != "running":
                return
            token = claim_token or job.claim_token
            if not token or job.claim_token != token:
                return
            cancel_version = job.cancel_version
            candidates = self._candidates(db, job)
            if not candidates:
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
                for attempt in range(max_retries + 1):
                    if not self._reserve_external_attempt(job_id, token):
                        request_limit_reached = True
                        break
                    try:
                        status, mime, body = self.fetcher(canonical)
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
                if len(body) > self.settings.max_direct_bytes:
                    raise RuntimeError("direct_body_too_large")
                mode = classify_direct_response(status, mime, body)
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
                else:
                    failures += 1
            elif mode == "browser_required" and browser_required(canonical, status=status, body=body):
                if self._browser_capture(job_id, token, bill_id, bill_title, bill_status, metadata, canonical):
                    successes += 1
                else:
                    failures += 1
            else:
                failures += 1
                self._record_failed_source(job_id, token, canonical, "direct", status, mime)
            if request_limit_reached:
                break

        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return
            if successes and failures:
                self._finish(db, job, token, "partial", None, True)
            elif successes:
                self._finish(db, job, token, "completed", None, False)
            else:
                self._finish(db, job, token, "partial", "no_usable_source", False)

    def _record_failed_source(self, job_id: uuid.UUID, token: str, url: str, mechanism: str, status: int | None, mime: str | None) -> None:
        with self.sessions() as db:
            if self._fenced(db, job_id, token) is None:
                return
            db.add(ScoutSource(job_id=job_id, canonical_url=url[:4000], official=False, retrieval_mechanism=mechanism, http_status=status, mime_type=mime))
            db.add(ScoutJobEvent(job_id=job_id, kind="source_failed", detail={"mechanism": mechanism, "status": status}))
            db.commit()

    def _persist_capture(self, job_id: uuid.UUID, token: str, bill_id: uuid.UUID | None, bill_title: str, bill_status: str | None, metadata: dict, url: str, mechanism: str, status: int | None, mime: str | None, body: bytes) -> uuid.UUID | None:
        try:
            digest = content_hash(body)
            mime_base = (mime or "").split(";", 1)[0].lower()
            if mime_base == "application/pdf":
                from io import BytesIO
                from pypdf import PdfReader
                reader = PdfReader(BytesIO(body))
                if len(reader.pages) > self.settings.max_pdf_pages:
                    raise RuntimeError("pdf_page_limit")
                parts: list[str] = []
                remaining = self.settings.max_pdf_text_chars
                for page in reader.pages:
                    part = (page.extract_text() or "")[:remaining]
                    parts.append(part)
                    remaining -= len(part)
                    if remaining <= 0:
                        break
                text = " ".join(parts).strip()
            else:
                text = _TAG_RE.sub(" ", body.decode("utf-8", "replace"))[:self.settings.max_pdf_text_chars].strip()
            evidence = self._evidence_excerpt(text, metadata, bill_status)
            if evidence is None:
                self._record_failed_source(job_id, token, url, mechanism, status, mime)
                return None
            excerpt, excerpt_start, excerpt_end = evidence
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
                stage = ScoutSource(job_id=job_id, canonical_url=url, official=False, retrieval_mechanism="staged", http_status=status, mime_type=mime)
                db.add(stage)
                db.commit()
                stage_id = stage.id
            try:
                raw_ref = self.rawstore.put(body, {"source_url": url, "mechanism": mechanism})
            except Exception:
                with self.sessions() as db:
                    stage = db.get(ScoutSource, stage_id)
                    if stage is not None:
                        db.delete(stage)
                        db.commit()
                raise
            with self.sessions() as db:
                if self._fenced(db, job_id, token) is None:
                    stage = db.get(ScoutSource, stage_id)
                    if stage is not None:
                        stage.raw_ref = raw_ref
                        stage.content_hash = digest
                        stage.document_hash = digest
                        db.commit()
                    return None
                # Same URL/hash within this job is idempotent. A changed
                # version retains the immediately preceding version link.
                same = db.execute(select(ScoutSource.id).where(ScoutSource.job_id == job_id, ScoutSource.canonical_url == url, ScoutSource.content_hash == digest)).scalar_one_or_none()
                if same is not None and same != stage_id:
                    db.delete(db.get(ScoutSource, stage_id))
                    db.commit()
                    return same
                current_job = self._fenced(db, job_id, token)
                if current_job is None:
                    return None
                prior = db.execute(
                    select(ScoutSource)
                    .join(ScoutResearchJob, ScoutResearchJob.id == ScoutSource.job_id)
                    .where(
                        ScoutResearchJob.customer_id == current_job.customer_id,
                        ScoutSource.canonical_url == url,
                        ScoutSource.content_hash.is_not(None),
                        ScoutSource.content_hash != digest,
                    )
                    .order_by(ScoutSource.retrieved_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                source = db.get(ScoutSource, stage_id)
                if source is None:  # pragma: no cover - stage is committed above
                    return None
                source.title = bill_title
                source.official = True
                source.retrieval_mechanism = mechanism
                source.content_hash = digest
                source.document_hash = digest
                source.raw_ref = raw_ref
                source.prior_source_id = prior.id if prior else None
                identifier = str(metadata.get("identifier") or bill_title)
                action = metadata.get("latest_action")
                action_date = metadata.get("latest_action_date") or metadata.get("status_date")
                development = f"Latest structured action{f' ({action_date.isoformat()})' if isinstance(action_date, date) else ''}: {action}" if action else f"Structured Florida status: {bill_status or 'unreported'}"
                # _evidence_excerpt refuses a finding unless this exact
                # displayed window supports both the identifier and action.
                db.add(ScoutFinding(job_id=job_id, source_id=source.id, title=f"{identifier}: {bill_title}", what_happened=development, why_it_matters="The structured development is paired with retained official source bytes.", relevant_date=action_date if isinstance(action_date, date) else None, excerpt=excerpt, excerpt_hash=content_hash(excerpt.encode()), excerpt_start=excerpt_start, excerpt_end=excerpt_end, confidence="high", extractor_version="scout-p0-1", bill_id=bill_id))
                db.add(ScoutJobEvent(job_id=job_id, kind="source_persisted", detail={"mechanism": mechanism}))
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
        if not normalized or any(marker in normalized for marker in _SHELL_MARKERS):
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

    def _browser_capture(self, job_id: uuid.UUID, token: str, bill_id: uuid.UUID | None, bill_title: str, bill_status: str | None, metadata: dict, url: str) -> bool:
        with self.sessions() as db:
            active_job = self._fenced(db, job_id, token)
            if active_job is None:
                return False
            max_pages = self._job_limit(active_job, "max_pages", self.settings.max_pages)
            max_actions = self._job_limit(active_job, "max_actions", self.settings.max_actions)
        # The global slot and its external-request reservation are one locked
        # transaction. A full browser cap must not consume request budget, and
        # a consumed request must always have a durable zero-usage slot.
        session_id = self._reserve_browser_slot(job_id, token)
        if session_id is None:
            return False
        capture: BrowserCapture | None = None
        started: float | None = None
        provider_id: str | None = None
        durable_provider_id = False

        def on_started(created_provider_id: str) -> None:
            nonlocal provider_id, durable_provider_id
            # This transaction couples the durable provider ID and usage
            # increment. A failed callback is propagated to the provider,
            # which self-cleans before returning control to us.
            self._record_browser_started(session_id, job_id, token, created_provider_id)
            provider_id = created_provider_id
            durable_provider_id = True

        try:
            started = time.monotonic()
            capture = self.provider.capture(
                BrowserRequest(url=url, max_pages=max_pages, max_actions=max_actions, wall_seconds=self.settings.browser_wall_seconds, max_bytes=self.settings.max_direct_bytes),
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
                return False
            if capture.pages > max_pages or capture.actions > max_actions:
                raise RuntimeError("browser_limit_exceeded")
            final_url = canonicalize_url(capture.url)
            source_id = self._persist_capture(job_id, token, bill_id, bill_title, bill_status, metadata, final_url, "browser", 200, capture.mime_type, capture.body)
            if source_id is None:
                return False
            with self.sessions() as db:
                session = db.get(ScoutBrowserSession, session_id)
                if session is not None:
                    session.pages = capture.pages
                    session.actions = capture.actions
                    session.source_id = source_id
                    session.runtime_ms = int((time.monotonic() - started) * 1000)
                    db.commit()
            self._heartbeat(job_id, token, "browser_pages", capture.pages)
            self._heartbeat(job_id, token, "browser_actions", capture.actions)
            return True
        except ProviderSessionPersistenceError as exc:
            # The provider has already self-cleaned. Retry only the durable
            # ledger write; if DB recovered we can record and idempotently
            # release the true provider ID. Never log the opaque value.
            provider_id = exc.provider_session_id
            durable_provider_id = self._recover_browser_session(session_id, job_id, token, provider_id)
            return False
        except Exception:
            return False
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
                            db.commit()
                except Exception:
                    # Do not let best-effort runtime telemetry prevent the
                    # provider-ID finalization/release path below.
                    pass
            if capture is not None:
                provider_id = capture.provider_session_id
            if provider_id and durable_provider_id:
                self._release_browser_session(session_id, provider_id)
            elif provider_id:
                # Callback persistence may have failed because the DB was
                # transiently unavailable. The provider ID means this slot is
                # never safe to delete: attempt a second idempotent release,
                # then finalize the pre-reserved slot without reusing the
                # failing callback transaction.
                released, replay = self._release_untracked_provider(provider_id)
                self._finalize_untracked_browser_slot(session_id, job_id, provider_id, released, replay)
            else:
                self._discard_unstarted_browser_slot(session_id)

    def _reserve_browser_slot(self, job_id: uuid.UUID, token: str) -> uuid.UUID | None:
        """Reserve one durable zero-usage slot under the global browser lock."""
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return None
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(81420901)"))
            active = db.query(ScoutBrowserSession).filter(
                ScoutBrowserSession.status.in_(("starting", "running", "cleanup_failed"))
            ).count()
            if active >= self.settings.max_concurrent_browser_sessions:
                db.add(ScoutJobEvent(job_id=job_id, kind="browser_skipped", detail={"reason": "global_limit"}))
                db.commit()
                return None
            usage = dict(job.usage or {})
            if int(usage.get("external_requests", 0)) >= self._job_limit(
                job, "max_external_requests", self.settings.max_external_requests
            ):
                db.add(ScoutJobEvent(job_id=job_id, kind="browser_skipped", detail={"reason": "external_request_limit"}))
                db.commit()
                return None
            usage["external_requests"] = int(usage.get("external_requests", 0)) + 1
            job.usage = usage
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + timedelta(seconds=self.settings.lease_seconds)
            session = ScoutBrowserSession(job_id=job_id, provider=self.provider.__class__.__name__, status="starting")
            db.add(session)
            db.commit()
            db.refresh(session)
            return session.id

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
        """Remove a reservation that never obtained a provider session ID."""
        with self.sessions() as db:
            session = db.get(ScoutBrowserSession, session_id)
            if session is not None and session.provider_session_id is None and session.status == "starting":
                db.delete(session)
                db.commit()

    def _release_untracked_provider(self, provider_id: str) -> tuple[bool, str | None]:
        """One bounded cleanup retry for a provider ID whose callback failed."""
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.provider.release, provider_id)
        try:
            replay = future.result(timeout=self.settings.browser_cleanup_seconds)
        except TimeoutError:
            pool.shutdown(wait=False, cancel_futures=True)
            return False, None
        except Exception:
            pool.shutdown(wait=True)
            return False, None
        else:
            pool.shutdown(wait=True)
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

    def _release_browser_session(self, session_id: uuid.UUID, provider_id: str | None) -> None:
        try:
            if not provider_id:
                with self.sessions() as db:
                    session = db.get(ScoutBrowserSession, session_id)
                    provider_id = session.provider_session_id if session is not None else None
            if not provider_id:
                self._mark_cleanup_failed(session_id, "missing_provider_id")
                return
            replay = None
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(self.provider.release, provider_id)
            try:
                replay = future.result(timeout=self.settings.browser_cleanup_seconds)
            except TimeoutError:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)
            with self.sessions() as db:
                session = db.get(ScoutBrowserSession, session_id)
                if session is not None:
                    session.status = "released"
                    session.replay_url = replay
                    session.error_class = None if replay else "replay_pending:0"
                    session.released_at = datetime.now(timezone.utc)
                    db.commit()
        except TimeoutError:
            self._mark_cleanup_failed(session_id, "cleanup_timeout")
        except Exception:
            self._mark_cleanup_failed(session_id, "cleanup_failed")

    def _mark_cleanup_failed(self, session_id: uuid.UUID, error_class: str) -> None:
        with self.sessions() as db:
            session = db.get(ScoutBrowserSession, session_id)
            if session is not None:
                session.status = "cleanup_failed"
                session.error_class = error_class
                db.commit()

    def reap_sessions(self) -> int:
        """Independent retry path for sessions left by process interruption/cleanup timeout."""
        ids: list[tuple[uuid.UUID, str | None]] = []
        replay_ids: list[tuple[uuid.UUID, str, int]] = []
        now = datetime.now(timezone.utc)
        with self.sessions() as db:
            ids = [(row.id, row.provider_session_id) for row in db.execute(select(ScoutBrowserSession).where(or_(
                ScoutBrowserSession.status.in_(("starting", "running", "cleanup_failed")),
            ))).scalars()]
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
        for session_id, provider_id in ids:
            if provider_id:
                self._release_browser_session(session_id, provider_id)
            else:
                # A process died before the provider reported an ID.  There is
                # nothing safe to release remotely, so preserve the truthful
                # failure ledger instead of claiming a release occurred.
                self._mark_cleanup_failed(session_id, "orphaned_start")
        for session_id, provider_id, attempts in replay_ids:
            self._probe_replay(session_id, provider_id, attempts)
        return len(ids) + len(replay_ids)

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
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(self.provider.probe_replay, provider_id)
            try:
                replay = future.result(timeout=self.settings.browser_cleanup_seconds)
            except TimeoutError:
                pool.shutdown(wait=False, cancel_futures=True)
                replay = None
            else:
                pool.shutdown(wait=True)
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

    def _reserve_external_attempt(self, job_id: uuid.UUID, token: str) -> bool:
        """Reserve one source retrieval before issuing it; never exceed the persisted cap."""
        with self.sessions() as db:
            job = self._fenced(db, job_id, token)
            if job is None:
                return False
            usage = dict(job.usage or {})
            if int(usage.get("external_requests", 0)) >= self._job_limit(job, "max_external_requests", self.settings.max_external_requests):
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
