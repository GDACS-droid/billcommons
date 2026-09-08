"""Explicit, bounded repair for historical missing-intermediate full-text failures.

This is intentionally separate from the ordinary fetch queue.  It admits only
historically dead ``fetch_text`` jobs whose exact TLS signature was retained,
for five reviewed hosts.  Planning is read-only by default.  An outbound
repair must first commit an immutable admission record and a fencing token;
a crash after admission never silently spends an unbounded number of requests.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import signal
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Callable
from urllib.parse import urlsplit

from sqlalchemy import Text, cast, exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from billcommons_ingest import fulltext
from billcommons_schema.models import (
    Bill,
    BillDocument,
    BillVersion,
    IngestJob,
    Jurisdiction,
    TlsFulltextRepair,
    TlsFulltextRepairAttempt,
)
from billcommons_shared.db import get_session
from billcommons_shared.rawstore import FilesystemRawStore, RawStore


REPAIR_REASON = "missing_tls_intermediate"
REMEDIATION_VERSION = "tls-intermediate-aia-v1"
TX_REPAIR_REASON = "tx_ftp_witness_url"
TX_REMEDIATION_VERSION = "tx-ftp-tlodocs-v1"
MAX_REPAIR_ATTEMPTS = 2
MAX_CYCLE_LIMIT = 100
DEFAULT_CYCLE_LIMIT = 20
REPAIR_COOLDOWN = timedelta(hours=24)
REPAIR_LIFETIME = timedelta(days=14)
FETCH_TEXT_KIND = fulltext.FETCH_TEXT_KIND
DB_STATEMENT_TIMEOUT_MS = 5_000
DB_LOCK_TIMEOUT_MS = 1_000
ATTEMPT_WALL_TIMEOUT_SECONDS = 90

# These hosts were independently verified to recover only after their public
# issuer certificate was supplied. Membership is still insufficient on its
# own: the source dead job must retain both exact TLS error markers below.
REVIEWED_TLS_HOSTS = frozenset(
    {
        "www.cga.ct.gov",
        "www.legislature.mi.gov",
        "www.legislature.ms.gov",
        "www.legislature.ohio.gov",
        "legislature.vermont.gov",
    }
)
_MISSING_ISSUER_MARKER = "unable to get local issuer certificate"
_CERT_VERIFY_MARKER = "certificate_verify_failed"


class RepairAttemptTimeout(RuntimeError):
    """The bounded repair invocation exceeded its total wall-time allowance."""


@dataclass(frozen=True)
class RepairCandidate:
    document_id: uuid.UUID
    source_dead_job_id: uuid.UUID
    source_error_sha256: str


@dataclass(frozen=True)
class RepairReservation:
    repair_id: uuid.UUID
    token: uuid.UUID


@dataclass(frozen=True)
class RepairCycleResult:
    planned: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    expired: int = 0


def _utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("repair timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_CYCLE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_CYCLE_LIMIT}")
    return limit


def _set_db_timeouts(db: Session) -> None:
    """Bound every short planning/claim statement and lock wait.

    The fetch transaction is separately bounded by ``_attempt_deadline``;
    setting an idle-in-transaction timeout here would kill a deliberate,
    bounded network request before its shared full-text persistence tail can
    commit atomically.
    """
    db.execute(text(f"SET LOCAL statement_timeout = '{DB_STATEMENT_TIMEOUT_MS}ms'"))
    db.execute(text(f"SET LOCAL lock_timeout = '{DB_LOCK_TIMEOUT_MS}ms'"))


def _status(note: str | None) -> str | None:
    if not note or not note.startswith("fulltext_status="):
        return None
    return note[len("fulltext_status=") :].split(" ", 1)[0]


def _valid_reviewed_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in REVIEWED_TLS_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def _reviewed_url_sql() -> object:
    """Safe SQL prefilter matching the URL shapes accepted by the parser."""
    predicates = []
    for host in sorted(REVIEWED_TLS_HOSTS):
        origin = f"https://{host}"
        predicates.extend(
            (
                BillDocument.url == origin,
                BillDocument.url.like(f"{origin}/%"),
                BillDocument.url.like(f"{origin}?%"),
                BillDocument.url.like(f"{origin}#%"),
            )
        )
    return or_(*predicates)


def _is_exact_missing_issuer_error(error: str | None) -> bool:
    if not error:
        return False
    normalized = error.casefold()
    return _CERT_VERIFY_MARKER in normalized and _MISSING_ISSUER_MARKER in normalized


def _tx_witness_candidate(url: str | None) -> str | None:
    """Return only the reviewed TX resolver output for the three evidence eras."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "ftp"
        or parsed.hostname != "ftp.legis.state.tx.us"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 21)
        or not parsed.path.startswith(("/bills/89R/witlistbill/html/", "/bills/891/witlistbill/html/", "/bills/892/witlistbill/html/"))
    ):
        return None
    # The resolver is deliberately owned separately; this repair merely uses
    # its pure reviewed candidate as a witness, never inventing a TX URL.
    from billcommons_ingest import url_resolvers

    resolver = getattr(url_resolvers, "tx_ftp_tlodocs_candidate", None)
    if not callable(resolver):
        return None
    candidate = resolver(url)
    return candidate if isinstance(candidate, str) and candidate else None


def _is_tx_redirect_error(error: str | None) -> bool:
    return bool(error and fulltext.STATUS_UNSUPPORTED_REDIRECT_SCHEME in error)


def _active_normal_fetch_job_exists(db: Session, document_id: object) -> bool:
    return bool(
        db.execute(
            select(
                exists().where(
                    IngestJob.kind == FETCH_TEXT_KIND,
                    IngestJob.status.in_(("queued", "running")),
                    IngestJob.payload["document_id"].astext == cast(document_id, Text),
                )
            )
        ).scalar()
    )


def _document_remains_repairable(
    db: Session, document: BillDocument, *, reason: str = REPAIR_REASON
) -> bool:
    """Recheck all mutable eligibility while holding the document row lock."""
    common = (
        document.extracted_text is None
        and not _active_normal_fetch_job_exists(db, document.id)
    )
    if reason == REPAIR_REASON:
        return (
            common
            and _valid_reviewed_url(document.url)
            and _status(document.license_note) == fulltext.STATUS_PERMANENTLY_FAILED
            and (document.fetch_attempts or 0) >= fulltext.MAX_FETCH_ATTEMPTS
        )
    if reason == TX_REPAIR_REASON:
        jurisdiction = db.scalar(
            select(Jurisdiction.abbreviation)
            .join(Bill, Bill.jurisdiction_id == Jurisdiction.id)
            .join(BillVersion, BillVersion.bill_id == Bill.id)
            .where(BillVersion.id == document.bill_version_id)
        )
        return (
            common
            and jurisdiction == "TX"
            and _tx_witness_candidate(document.url) is not None
            and _status(document.license_note) == fulltext.STATUS_UNSUPPORTED_REDIRECT_SCHEME
            and (document.fetch_attempts or 0) == 0
        )
    return False


def _matching_dead_jobs():
    """One latest exact TLS dead job per document, before a bounded scan."""
    error = func.lower(IngestJob.last_error)
    document_id = IngestJob.payload["document_id"].astext.label("document_id")
    return (
        select(
            IngestJob.id.label("source_dead_job_id"),
            document_id,
            IngestJob.last_error.label("last_error"),
            func.row_number()
            .over(
                partition_by=document_id,
                order_by=(IngestJob.created_at.desc(), IngestJob.id.desc()),
            )
            .label("latest_rank"),
        )
        .where(
            IngestJob.kind == FETCH_TEXT_KIND,
            IngestJob.status == "dead",
            error.contains(_CERT_VERIFY_MARKER),
            error.contains(_MISSING_ISSUER_MARKER),
        )
        .subquery()
    )


def discover_candidates(db: Session, *, limit: int = DEFAULT_CYCLE_LIMIT) -> list[RepairCandidate]:
    """Read a bounded, fully prefiltered set of repair candidates.

    Every stable exclusion is in SQL before the limit so already-seeded,
    duplicate, ineligible, or normally queued rows cannot starve later work.
    Python repeats the exact checks before writing as the correctness boundary.
    """
    limit = _bounded_limit(limit)
    _set_db_timeouts(db)
    dead_jobs = _matching_dead_jobs()
    active_normal = exists().where(
        IngestJob.kind == FETCH_TEXT_KIND,
        IngestJob.status.in_(("queued", "running")),
        IngestJob.payload["document_id"].astext == cast(BillDocument.id, Text),
    )
    existing_plan = exists().where(
        TlsFulltextRepair.document_id == BillDocument.id,
        TlsFulltextRepair.reason == REPAIR_REASON,
        TlsFulltextRepair.remediation_version == REMEDIATION_VERSION,
    )
    stmt = (
        select(
            BillDocument.id,
            dead_jobs.c.source_dead_job_id,
            dead_jobs.c.last_error,
        )
        .join(dead_jobs, dead_jobs.c.document_id == cast(BillDocument.id, Text))
        .where(
            dead_jobs.c.latest_rank == 1,
            BillDocument.extracted_text.is_(None),
            fulltext.license_note_matches_status(
                BillDocument.license_note, (fulltext.STATUS_PERMANENTLY_FAILED,)
            ),
            BillDocument.fetch_attempts >= fulltext.MAX_FETCH_ATTEMPTS,
            _reviewed_url_sql(),
            ~active_normal,
            ~existing_plan,
        )
        .order_by(BillDocument.id)
        .limit(limit)
    )
    candidates: list[RepairCandidate] = []
    for document_id, job_id, error in db.execute(stmt):
        # Keep the pure predicate so collation differences or a changed SQL
        # expression cannot broaden the historical TLS admission contract.
        if _is_exact_missing_issuer_error(error):
            candidates.append(
                RepairCandidate(
                    document_id=document_id,
                    source_dead_job_id=job_id,
                    source_error_sha256=hashlib.sha256(error.encode("utf-8")).hexdigest(),
                )
            )
    return candidates


def discover_tx_candidates(db: Session, *, limit: int = DEFAULT_CYCLE_LIMIT) -> list[RepairCandidate]:
    """Find only the historically recorded TX FTP redirect failures.

    The table name is historical TLS compatibility storage; this function is
    intentionally an explicit second admission path, not a policy framework.
    """
    limit = _bounded_limit(limit)
    _set_db_timeouts(db)
    document_id = IngestJob.payload["document_id"].astext.label("document_id")
    dead = (
        select(
            IngestJob.id.label("source_dead_job_id"), document_id,
            IngestJob.last_error.label("last_error"),
            func.row_number().over(
                partition_by=document_id,
                order_by=(IngestJob.created_at.desc(), IngestJob.id.desc()),
            ).label("latest_rank"),
        )
        .where(
            IngestJob.kind == FETCH_TEXT_KIND,
            IngestJob.status == "dead",
            func.lower(IngestJob.last_error).contains(fulltext.STATUS_UNSUPPORTED_REDIRECT_SCHEME),
        ).subquery()
    )
    active = exists().where(
        IngestJob.kind == FETCH_TEXT_KIND,
        IngestJob.status.in_(("queued", "running")),
        IngestJob.payload["document_id"].astext == cast(BillDocument.id, Text),
    )
    existing = exists().where(
        TlsFulltextRepair.document_id == BillDocument.id,
        TlsFulltextRepair.reason == TX_REPAIR_REASON,
        TlsFulltextRepair.remediation_version == TX_REMEDIATION_VERSION,
    )
    stmt = (
        select(BillDocument.id, dead.c.source_dead_job_id, dead.c.last_error)
        .join(dead, dead.c.document_id == cast(BillDocument.id, Text))
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .join(Bill, Bill.id == BillVersion.bill_id)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .where(
            dead.c.latest_rank == 1,
            Jurisdiction.abbreviation == "TX",
            BillDocument.extracted_text.is_(None),
            BillDocument.fetch_attempts == 0,
            fulltext.license_note_matches_status(
                BillDocument.license_note, (fulltext.STATUS_UNSUPPORTED_REDIRECT_SCHEME,)
            ),
            BillDocument.url.like("ftp://ftp.legis.state.tx.us/%"),
            ~active, ~existing,
        ).order_by(BillDocument.id).limit(limit)
    )
    return [
        RepairCandidate(document_id=row[0], source_dead_job_id=row[1],
                        source_error_sha256=hashlib.sha256(row[2].encode("utf-8")).hexdigest())
        for row in db.execute(stmt)
        if _is_tx_redirect_error(row[2])
        and _tx_witness_candidate(db.get(BillDocument, row[0]).url) is not None
    ]


def seed_candidates(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_CYCLE_LIMIT,
) -> int:
    """Persist each newly eligible repair plan once; caller commits."""
    current = _utc(now)
    created = 0
    for candidate in discover_candidates(db, limit=limit):
        document = db.get(BillDocument, candidate.document_id, with_for_update=True)
        if document is None or not _document_remains_repairable(db, document):
            continue
        existing = db.execute(
            select(TlsFulltextRepair)
            .where(
                TlsFulltextRepair.document_id == candidate.document_id,
                TlsFulltextRepair.reason == REPAIR_REASON,
                TlsFulltextRepair.remediation_version == REMEDIATION_VERSION,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            continue
        repair = TlsFulltextRepair(
            document_id=candidate.document_id,
            reason=REPAIR_REASON,
            remediation_version=REMEDIATION_VERSION,
            source_dead_job_id=candidate.source_dead_job_id,
            source_error_sha256=candidate.source_error_sha256,
            status="planned",
            attempts=0,
            max_attempts=MAX_REPAIR_ATTEMPTS,
            next_attempt_at=current,
            expires_at=current + REPAIR_LIFETIME,
        )
        try:
            with db.begin_nested():
                db.add(repair)
                db.flush()
        except IntegrityError:
            # The unique constraint is the cross-process duplicate gate. Do
            # not turn any non-unique storage failure into a benign duplicate.
            duplicate = db.execute(
                select(TlsFulltextRepair.id).where(
                    TlsFulltextRepair.document_id == candidate.document_id,
                    TlsFulltextRepair.reason == REPAIR_REASON,
                    TlsFulltextRepair.remediation_version == REMEDIATION_VERSION,
                )
            ).scalar_one_or_none()
            if duplicate is None:
                raise
        else:
            created += 1
    return created


def seed_tx_candidates(db: Session, *, now: datetime | None = None, limit: int = DEFAULT_CYCLE_LIMIT) -> int:
    """Persist TX witness plans once; callers still own the outer commit."""
    current = _utc(now)
    created = 0
    for candidate in discover_tx_candidates(db, limit=limit):
        document = db.get(BillDocument, candidate.document_id, with_for_update=True)
        if document is None or not _document_remains_repairable(db, document, reason=TX_REPAIR_REASON):
            continue
        repair = TlsFulltextRepair(
            document_id=document.id, reason=TX_REPAIR_REASON,
            remediation_version=TX_REMEDIATION_VERSION,
            source_dead_job_id=candidate.source_dead_job_id,
            source_error_sha256=candidate.source_error_sha256,
            status="planned", attempts=0, max_attempts=MAX_REPAIR_ATTEMPTS,
            next_attempt_at=current, expires_at=current + REPAIR_LIFETIME,
        )
        try:
            with db.begin_nested():
                db.add(repair); db.flush()
        except IntegrityError:
            if db.scalar(select(TlsFulltextRepair.id).where(
                TlsFulltextRepair.document_id == document.id,
                TlsFulltextRepair.reason == TX_REPAIR_REASON,
                TlsFulltextRepair.remediation_version == TX_REMEDIATION_VERSION,
            )) is None:
                raise
        else:
            created += 1
    return created


def _append_attempt_event(
    db: Session,
    repair: TlsFulltextRepair,
    *,
    outcome: str,
    attempt_number: int,
    at: datetime,
    status_before: str | None,
    status_after: str | None,
) -> None:
    """Append rather than overwrite durable admission/outcome evidence."""
    db.add(
        TlsFulltextRepairAttempt(
            repair_id=repair.id,
            attempt_number=attempt_number,
            outcome=outcome,
            document_status_before=status_before,
            document_status_after=status_after,
            started_at=at,
            finished_at=at,
        )
    )


def _complete_without_fetch(
    db: Session,
    repair: TlsFulltextRepair,
    *,
    status: str,
    outcome: str,
    current: datetime,
) -> str:
    repair.status = status
    repair.completed_at = current
    repair.last_outcome = outcome
    return "expired" if status == "expired" else "skipped"


def reserve_one_due_repair(
    db: Session, *, now: datetime | None = None, reason: str = REPAIR_REASON
) -> RepairReservation | str | None:
    """Commit-safe admission before any outbound request.

    A reservation consumes one of the two allowed repair attempts and remains
    ``reserved`` after a crash. There is deliberately no age-based reclaim or
    secondary worker takeover: an operator can inspect the immutable admitted
    event before deciding how to handle a proven process failure.
    """
    current = _utc(now)
    _set_db_timeouts(db)
    repair = db.execute(
        select(TlsFulltextRepair)
        .where(
            TlsFulltextRepair.status == "planned",
            TlsFulltextRepair.next_attempt_at <= current,
            TlsFulltextRepair.reason == reason,
        )
        .order_by(TlsFulltextRepair.next_attempt_at, TlsFulltextRepair.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if repair is None:
        return None
    if current >= repair.expires_at:
        return _complete_without_fetch(
            db, repair, status="expired", outcome="expired", current=current
        )
    if repair.attempts >= repair.max_attempts:
        return _complete_without_fetch(
            db, repair, status="exhausted", outcome="exhausted", current=current
        )

    token = uuid.uuid4()
    repair.attempts += 1
    repair.status = "reserved"
    repair.reservation_token = token
    repair.reserved_at = current
    repair.last_outcome = "admitted"
    _append_attempt_event(
        db,
        repair,
        outcome="admitted",
        attempt_number=repair.attempts,
        at=current,
        status_before=None,
        status_after=None,
    )
    db.flush()
    return RepairReservation(repair_id=repair.id, token=token)


def _source_evidence_still_matches(db: Session, repair: TlsFulltextRepair) -> bool:
    """Check that the immutable repair record still names the same dead job."""
    source = db.get(IngestJob, repair.source_dead_job_id)
    return bool(
        source is not None
        and source.kind == FETCH_TEXT_KIND
        and source.status == "dead"
        and source.payload.get("document_id") == str(repair.document_id)
        and source.last_error
        and hmac.compare_digest(
            hashlib.sha256(source.last_error.encode("utf-8")).hexdigest(), repair.source_error_sha256
        )
        and (
            _is_exact_missing_issuer_error(source.last_error)
            if repair.reason == REPAIR_REASON
            else repair.reason == TX_REPAIR_REASON and _is_tx_redirect_error(source.last_error)
        )
    )


@contextmanager
def _attempt_deadline(seconds: int = ATTEMPT_WALL_TIMEOUT_SECONDS) -> Iterator[None]:
    """Interrupt a synchronous fetch in the worker's main thread at a hard cap."""
    if threading.current_thread() is not threading.main_thread():
        # A threaded caller cannot safely install a process-wide alarm. Fail
        # closed before outbound I/O instead of silently losing the wall cap.
        raise RepairAttemptTimeout("repair deadline unavailable outside main thread")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)

    def expire(_signum, _frame) -> None:
        raise RepairAttemptTimeout("repair attempt deadline exceeded")

    signal.signal(signal.SIGALRM, expire)
    started = monotonic()
    try:
        yield
        if monotonic() - started > seconds:
            raise RepairAttemptTimeout("repair attempt deadline exceeded")
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def execute_reserved_repair(
    db: Session,
    reservation: RepairReservation,
    *,
    fetcher: fulltext.FullTextFetcher,
    rawstore: RawStore,
    now: datetime | None = None,
) -> str:
    """Execute one committed reservation and append an immutable outcome.

    The fencing token prevents any code path from finalizing a reservation it
    did not admit. No generic queue row is touched or reaped.
    """
    current = _utc(now)
    _set_db_timeouts(db)
    repair = db.execute(
        select(TlsFulltextRepair)
        .where(
            TlsFulltextRepair.id == reservation.repair_id,
            TlsFulltextRepair.status == "reserved",
            TlsFulltextRepair.reservation_token == reservation.token,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if repair is None:
        return "skipped"

    document = db.get(BillDocument, repair.document_id, with_for_update=True)
    before_status = _status(document.license_note) if document is not None else None
    approved_record = (
        (repair.reason == REPAIR_REASON and repair.remediation_version == REMEDIATION_VERSION)
        or (repair.reason == TX_REPAIR_REASON and repair.remediation_version == TX_REMEDIATION_VERSION)
    )
    if (
        not approved_record
        or document is None
        or not _document_remains_repairable(db, document, reason=repair.reason)
        or not _source_evidence_still_matches(db, repair)
    ):
        repair.status = "skipped"
        repair.completed_at = current
        repair.last_outcome = "ineligible"
        _append_attempt_event(
            db,
            repair,
            outcome="ineligible",
            attempt_number=repair.attempts,
            at=current,
            status_before=before_status,
            status_after=before_status,
        )
        return "skipped"

    try:
        # A full-text failure must not silently overwrite the original
        # permanent status/budget. The savepoint is rolled back on every
        # exception, while the durable reservation and outcome remain in the
        # outer transaction.
        with db.begin_nested():
            with _attempt_deadline():
                result = fulltext.process_fetch_text_job(
                    db, str(document.id), fetcher=fetcher, rawstore=rawstore
                )
    except fulltext.DocumentFetchError:
        db.refresh(document)
        finished_at = _utc()
        _append_attempt_event(
            db,
            repair,
            outcome="document_fetch_error",
            attempt_number=repair.attempts,
            at=finished_at,
            status_before=before_status,
            status_after=_status(document.license_note),
        )
        repair.last_outcome = "document_fetch_error"
        if repair.attempts >= repair.max_attempts or finished_at >= repair.expires_at:
            repair.status = "exhausted"
            repair.completed_at = finished_at
        else:
            repair.status = "planned"
            repair.reservation_token = None
            repair.reserved_at = None
            repair.next_attempt_at = finished_at + REPAIR_COOLDOWN
        return "failed"
    except fulltext.UnfetchableDocument:
        db.refresh(document)
        finished_at = _utc()
        _append_attempt_event(
            db,
            repair,
            outcome="unfetchable",
            attempt_number=repair.attempts,
            at=finished_at,
            status_before=before_status,
            status_after=_status(document.license_note),
        )
        repair.status = "skipped"
        repair.completed_at = finished_at
        repair.last_outcome = "unfetchable"
        return "skipped"
    except Exception:
        db.refresh(document)
        finished_at = _utc()
        _append_attempt_event(
            db,
            repair,
            outcome="unexpected",
            attempt_number=repair.attempts,
            at=finished_at,
            status_before=before_status,
            status_after=_status(document.license_note),
        )
        repair.status = "exhausted"
        repair.completed_at = finished_at
        repair.last_outcome = "unexpected"
        return "failed"

    finished_at = _utc()
    status_after = _status(document.license_note)
    # A normal return merely means bytes were processed; it can truthfully
    # represent scanned/empty/unsupported terminal content. Treat it as a
    # successful repair only when shared full-text persistence produced text
    # and a success status.
    if not document.extracted_text or result.status not in fulltext.SUCCESS_STATUSES:
        _append_attempt_event(
            db,
            repair,
            outcome="terminal_no_text",
            attempt_number=repair.attempts,
            at=finished_at,
            status_before=before_status,
            status_after=status_after,
        )
        repair.status = "skipped"
        repair.completed_at = finished_at
        repair.last_outcome = "terminal_no_text"
        return "skipped"

    _append_attempt_event(
        db,
        repair,
        outcome="succeeded",
        attempt_number=repair.attempts,
        at=finished_at,
        status_before=before_status,
        status_after=status_after,
    )
    repair.status = "succeeded"
    repair.completed_at = finished_at
    repair.last_outcome = "succeeded"
    return "succeeded"


def run_due_repairs(
    session_factory: Callable[[], Session] = get_session,
    *,
    fetcher: fulltext.FullTextFetcher | None = None,
    rawstore: RawStore | None = None,
    limit: int = DEFAULT_CYCLE_LIMIT,
    reason: str = REPAIR_REASON,
) -> RepairCycleResult:
    """Reserve, commit, then run at most ``limit`` bounded repairs."""
    limit = _bounded_limit(limit)
    active_fetcher = fetcher or fulltext.FullTextFetcher()
    active_rawstore = rawstore or FilesystemRawStore()
    counts = {"succeeded": 0, "failed": 0, "skipped": 0, "expired": 0}
    for _ in range(limit):
        reserve_db = session_factory()
        try:
            reservation = reserve_one_due_repair(reserve_db, reason=reason)
            reserve_db.commit()
        except Exception:
            reserve_db.rollback()
            raise
        finally:
            reserve_db.close()
        if reservation is None:
            break
        if isinstance(reservation, str):
            counts[reservation] += 1
            continue

        execute_db = session_factory()
        try:
            outcome = execute_reserved_repair(
                execute_db, reservation, fetcher=active_fetcher, rawstore=active_rawstore
            )
            execute_db.commit()
            counts[outcome] += 1
        except Exception:
            # The already committed reservation remains reserved, so a crash
            # after external I/O cannot result in an automatic extra request.
            execute_db.rollback()
            raise
        finally:
            execute_db.close()
    return RepairCycleResult(planned=0, **counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="persist newly discovered repair plans")
    parser.add_argument(
        "--enable",
        action="store_true",
        help="execute due repair plans; requires --apply in this invocation",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CYCLE_LIMIT,
        help=f"bounded plan/run limit, 1-{MAX_CYCLE_LIMIT} (default: {DEFAULT_CYCLE_LIMIT})",
    )
    args = parser.parse_args(argv)
    if args.enable and not args.apply:
        parser.error("--enable requires --apply")
    try:
        limit = _bounded_limit(args.limit)
    except ValueError as exc:
        parser.error(str(exc))

    db = get_session()
    try:
        if not args.apply:
            # The default inspection path cannot mutate, even if a future
            # helper changes its implementation accidentally.
            db.execute(text("SET TRANSACTION READ ONLY"))
            candidates = discover_candidates(db, limit=limit)
            print(f"tls-repair: {len(candidates)} eligible historical TLS repair candidate(s)")
            db.rollback()
            return 0
        created = seed_candidates(db, limit=limit)
        db.commit()
        print(f"tls-repair: recorded {created} new repair plan(s)")
    except SQLAlchemyError:
        db.rollback()
        print("tls-repair: database operation failed", flush=True)
        return 1
    except Exception:
        db.rollback()
        print("tls-repair: repair planning failed", flush=True)
        return 1
    finally:
        db.close()

    if args.enable:
        try:
            result = run_due_repairs(limit=limit)
        except (SQLAlchemyError, RepairAttemptTimeout):
            print("tls-repair: repair cycle failed", flush=True)
            return 1
        except Exception:
            print("tls-repair: repair cycle failed", flush=True)
            return 1
        print(
            "tls-repair: "
            f"succeeded={result.succeeded} failed={result.failed} "
            f"skipped={result.skipped} expired={result.expired}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
