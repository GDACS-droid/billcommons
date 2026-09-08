"""PostgreSQL tests for the evidence-gated TLS repair path."""
from __future__ import annotations

import hashlib
import json
import signal
from pathlib import Path

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from billcommons_ingest import fulltext, tls_repair
from billcommons_ingest.repair_transport import new_repair_fetcher
from billcommons_shared.safe_http import SafeResponse
from billcommons_ingest.queue import enqueue
from billcommons_schema.models import (
    Bill,
    BillDocument,
    BillVersion,
    CorpusUpdateEvidence,
    OfficialRawBlob,
    IngestJob,
    Jurisdiction,
    Session as SessionModel,
    TlsFulltextRepair,
    TlsFulltextRepairAttempt,
)


TLS_ERROR = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "unable to get local issuer certificate (_ssl.c:1010)"
)
NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def _document(
    db,
    *,
    url="https://www.cga.ct.gov/bills/hb1.pdf",
    status=None,
    attempts=15,
    document_id=None,
):
    suffix = uuid.uuid4().hex[:10].upper()
    jurisdiction = Jurisdiction(name="TLS repair test", abbreviation=f"ZQ_TR_{suffix}", classification="state")
    db.add(jurisdiction)
    db.flush()
    session = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026", active=True)
    db.add(session)
    db.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="TLS repair test bill",
    )
    db.add(bill)
    db.flush()
    version = BillVersion(bill_id=bill.id, note="introduced")
    db.add(version)
    db.flush()
    document = BillDocument(
        id=document_id or uuid.uuid4(),
        bill_version_id=version.id,
        url=url,
        fetch_attempts=attempts,
        license_note=status or f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED}",
    )
    db.add(document)
    db.flush()
    return document


def _dead_tls_job(db, document, *, error=TLS_ERROR):
    job = IngestJob(
        kind=fulltext.FETCH_TEXT_KIND,
        payload={"document_id": str(document.id)},
        status="dead",
        attempts=5,
        last_error=error,
    )
    db.add(job)
    db.flush()
    return job


def _seed_one(db, document):
    source = _dead_tls_job(db, document)
    assert tls_repair.seed_candidates(db, now=NOW) == 1
    repair = db.execute(select(TlsFulltextRepair)).scalar_one()
    assert repair.source_dead_job_id == source.id
    return repair


def _reserve_one(db):
    reservation = tls_repair.reserve_one_due_repair(db, now=NOW)
    assert isinstance(reservation, tls_repair.RepairReservation)
    return reservation


def _outcomes(db):
    return list(
        db.execute(
            select(TlsFulltextRepairAttempt.outcome).order_by(
                TlsFulltextRepairAttempt.attempt_number, TlsFulltextRepairAttempt.outcome
            )
        ).scalars()
    )


def test_discovery_requires_exact_historical_tls_evidence_and_repairable_document(db_session):
    eligible = _document(db_session)
    source = _dead_tls_job(db_session, eligible)

    generic = _document(db_session)
    _dead_tls_job(db_session, generic, error="connection refused")
    robots = _document(db_session, status=f"fulltext_status={fulltext.STATUS_ROBOTS_DISALLOWED}")
    _dead_tls_job(db_session, robots)
    wrong_host = _document(db_session, url="https://example.gov/bill.pdf")
    _dead_tls_job(db_session, wrong_host)
    with_text = _document(db_session)
    with_text.extracted_text = "already acquired"
    _dead_tls_job(db_session, with_text)

    candidates = tls_repair.discover_candidates(db_session, limit=10)
    assert candidates == [
        tls_repair.RepairCandidate(
            document_id=eligible.id,
            source_dead_job_id=source.id,
            source_error_sha256=hashlib.sha256(TLS_ERROR.encode("utf-8")).hexdigest(),
        )
    ]


def test_discovery_excludes_preseeded_earlier_row_before_limit(db_session):
    # Fixed IDs make ordering deterministic: the pre-seeded row is first, but
    # it must not consume the one-row planning cap.
    earlier = _document(db_session, document_id=uuid.UUID(int=1))
    later = _document(db_session, document_id=uuid.UUID(int=2))
    _dead_tls_job(db_session, earlier)
    later_source = _dead_tls_job(db_session, later)
    assert tls_repair.seed_candidates(db_session, now=NOW, limit=10) == 2
    db_session.execute(
        TlsFulltextRepair.__table__.delete().where(TlsFulltextRepair.document_id == later.id)
    )
    assert tls_repair.discover_candidates(db_session, limit=1) == [
        tls_repair.RepairCandidate(
            document_id=later.id,
            source_dead_job_id=later_source.id,
            source_error_sha256=hashlib.sha256(TLS_ERROR.encode("utf-8")).hexdigest(),
        )
    ]


def test_limit_is_hard_bounded():
    with pytest.raises(ValueError, match="between 1 and 100"):
        tls_repair.discover_candidates(object(), limit=101)


def test_active_normal_job_blocks_planning_and_execution_recheck(db_session, monkeypatch):
    document = _document(db_session)
    _seed_one(db_session, document)
    reservation = _reserve_one(db_session)
    enqueue(db_session, fulltext.FETCH_TEXT_KIND, {"document_id": str(document.id)})

    monkeypatch.setattr(
        fulltext,
        "process_fetch_text_job",
        lambda *_args, **_kwargs: pytest.fail("repair bypassed normal queue admission"),
    )
    assert tls_repair.execute_reserved_repair(
        db_session, reservation, fetcher=object(), rawstore=object(), now=NOW
    ) == "skipped"
    repair = db_session.execute(select(TlsFulltextRepair)).scalar_one()
    assert repair.status == "skipped"
    assert repair.attempts == 1
    assert _outcomes(db_session) == ["admitted", "ineligible"]


def test_committed_admission_blocks_automatic_second_outbound_attempt(db_session):
    document = _document(db_session)
    _seed_one(db_session, document)
    reservation = _reserve_one(db_session)
    db_session.flush()

    repair = db_session.execute(select(TlsFulltextRepair)).scalar_one()
    assert repair.status == "reserved"
    assert repair.reservation_token == reservation.token
    assert repair.attempts == 1
    # A process that dies now leaves the admission reserved; the cycle never
    # takes it over based on age or starts a second request automatically.
    assert tls_repair.reserve_one_due_repair(db_session, now=NOW + timedelta(days=30)) is None
    assert _outcomes(db_session) == ["admitted"]


def test_source_dead_job_hash_and_document_are_revalidated_before_fetch(db_session, monkeypatch):
    document = _document(db_session)
    source = _dead_tls_job(db_session, document)
    assert tls_repair.seed_candidates(db_session, now=NOW) == 1
    reservation = _reserve_one(db_session)
    source.last_error = "connection refused"

    monkeypatch.setattr(
        fulltext,
        "process_fetch_text_job",
        lambda *_args, **_kwargs: pytest.fail("stale source evidence made a request"),
    )
    assert tls_repair.execute_reserved_repair(
        db_session, reservation, fetcher=object(), rawstore=object(), now=NOW
    ) == "skipped"
    assert _outcomes(db_session) == ["admitted", "ineligible"]


def test_document_fetch_failure_preserves_permanent_state_and_uses_cooldown(db_session, monkeypatch):
    document = _document(db_session)
    repair = _seed_one(db_session, document)
    reservation = _reserve_one(db_session)

    def fail(*_args, **_kwargs):
        raise fulltext.DocumentFetchError("still unavailable", document_id=str(document.id))

    monkeypatch.setattr(fulltext, "process_fetch_text_job", fail)
    before_attempt = datetime.now(timezone.utc)
    assert tls_repair.execute_reserved_repair(
        db_session, reservation, fetcher=object(), rawstore=object(), now=NOW
    ) == "failed"
    db_session.flush()

    db_session.refresh(document)
    db_session.refresh(repair)
    assert document.fetch_attempts == fulltext.MAX_FETCH_ATTEMPTS
    assert document.license_note == f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED}"
    assert repair.status == "planned"
    assert repair.attempts == 1
    assert repair.next_attempt_at >= before_attempt + tls_repair.REPAIR_COOLDOWN
    assert _outcomes(db_session) == ["admitted", "document_fetch_error"]


def test_second_failure_exhausts_lifetime_repair_budget(db_session, monkeypatch):
    document = _document(db_session)
    repair = _seed_one(db_session, document)

    def fail(*_args, **_kwargs):
        raise fulltext.DocumentFetchError("still unavailable", document_id=str(document.id))

    monkeypatch.setattr(fulltext, "process_fetch_text_job", fail)
    first = _reserve_one(db_session)
    assert tls_repair.execute_reserved_repair(
        db_session, first, fetcher=object(), rawstore=object(), now=NOW
    ) == "failed"
    db_session.flush()
    db_session.refresh(repair)
    second = tls_repair.reserve_one_due_repair(db_session, now=repair.next_attempt_at + timedelta(seconds=1))
    assert isinstance(second, tls_repair.RepairReservation)
    assert tls_repair.execute_reserved_repair(
        db_session,
        second,
        fetcher=object(),
        rawstore=object(),
        now=repair.next_attempt_at + timedelta(seconds=1),
    ) == "failed"
    db_session.flush()
    db_session.refresh(repair)
    assert repair.status == "exhausted"
    assert repair.attempts == tls_repair.MAX_REPAIR_ATTEMPTS
    assert _outcomes(db_session) == [
        "admitted",
        "document_fetch_error",
        "admitted",
        "document_fetch_error",
    ]


def test_expired_repair_records_no_outbound_attempt(db_session, monkeypatch):
    document = _document(db_session)
    repair = _seed_one(db_session, document)
    repair.expires_at = NOW

    monkeypatch.setattr(
        fulltext,
        "process_fetch_text_job",
        lambda *_args, **_kwargs: pytest.fail("expired repair made an outbound fetch"),
    )
    assert tls_repair.reserve_one_due_repair(db_session, now=NOW) == "expired"
    assert repair.status == "expired"
    assert repair.attempts == 0
    assert _outcomes(db_session) == []


def test_normal_terminal_no_text_return_is_skipped_not_succeeded(db_session, monkeypatch):
    document = _document(db_session)
    repair = _seed_one(db_session, document)
    reservation = _reserve_one(db_session)

    def terminal(db, document_id, **_kwargs):
        stored = db.get(BillDocument, document_id)
        stored.extracted_text = None
        stored.license_note = f"fulltext_status={fulltext.STATUS_SCANNED_PDF_NO_TEXT}"
        return fulltext.FetchTextResult(
            document_id=str(stored.id), status=fulltext.STATUS_SCANNED_PDF_NO_TEXT
        )

    monkeypatch.setattr(fulltext, "process_fetch_text_job", terminal)
    assert tls_repair.execute_reserved_repair(
        db_session, reservation, fetcher=object(), rawstore=object(), now=NOW
    ) == "skipped"
    assert repair.status == "skipped"
    assert repair.last_outcome == "terminal_no_text"
    assert _outcomes(db_session) == ["admitted", "terminal_no_text"]


def test_success_mutation_and_outcome_rollback_together(db_session, monkeypatch):
    document = _document(db_session)
    repair = _seed_one(db_session, document)
    reservation = _reserve_one(db_session)

    def mutate_then_succeed(db, document_id, **_kwargs):
        stored = db.get(BillDocument, document_id)
        stored.extracted_text = "would have been committed"
        stored.license_note = "fulltext_status=ok"
        return fulltext.FetchTextResult(document_id=str(stored.id), status=fulltext.STATUS_OK, extracted_chars=25)

    monkeypatch.setattr(fulltext, "process_fetch_text_job", mutate_then_succeed)
    monkeypatch.setattr(
        tls_repair,
        "_append_attempt_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    document_id = document.id
    repair_id = repair.id
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        with db_session.begin_nested():
            tls_repair.execute_reserved_repair(
                db_session, reservation, fetcher=object(), rawstore=object(), now=NOW
            )

    document = db_session.get(BillDocument, document_id)
    repair = db_session.get(TlsFulltextRepair, repair_id)
    assert document.extracted_text is None
    assert document.license_note == f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED}"
    assert repair.status == "reserved"
    assert repair.attempts == 1
    assert _outcomes(db_session) == ["admitted"]


def _tx_document(db, *, url="ftp://ftp.legis.state.tx.us/bills/89R/witlistbill/html/senate_bills/SB00001_SB00099/SB00001S.HTM", document_id=None):
    document = _document(db, url=url, status=f"fulltext_status={fulltext.STATUS_UNSUPPORTED_REDIRECT_SCHEME}", attempts=0, document_id=document_id)
    bill = db.scalar(select(Bill).join(BillVersion, BillVersion.bill_id == Bill.id).where(BillVersion.id == document.bill_version_id))
    jurisdiction = db.get(Jurisdiction, bill.jurisdiction_id)
    existing_tx = db.scalar(select(Jurisdiction).where(Jurisdiction.abbreviation == "TX"))
    if existing_tx is None:
        jurisdiction.abbreviation = "TX"
    else:
        bill.jurisdiction_id = existing_tx.id
        session = db.get(SessionModel, bill.session_id)
        session.jurisdiction_id = existing_tx.id
        session.identifier = f"2026-{session.id}"
    db.flush()
    return document


def _enable_tx_witness(monkeypatch):
    from billcommons_ingest import url_resolvers
    monkeypatch.setattr(url_resolvers, "tx_ftp_tlodocs_candidate", lambda url: "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/SB00001.htm", raising=False)


def test_tx_witness_requires_exact_tx_status_url_and_latest_dead_source(db_session, monkeypatch):
    _enable_tx_witness(monkeypatch)
    eligible = _tx_document(db_session)
    source = _dead_tls_job(db_session, eligible, error="fulltext_status=unsupported_redirect_scheme")
    assert tls_repair.discover_tx_candidates(db_session) == [
        tls_repair.RepairCandidate(eligible.id, source.id, hashlib.sha256(source.last_error.encode()).hexdigest())
    ]
    assert tls_repair.seed_tx_candidates(db_session, now=NOW) == 1
    assert tls_repair.seed_tx_candidates(db_session, now=NOW) == 0


def test_tx_flat_witness_path_remains_seedable(db_session):
    document = _tx_document(
        db_session,
        url="ftp://ftp.legis.state.tx.us/bills/89R/witlistbill/html/SB00001S.HTM",
    )
    source = _dead_tls_job(db_session, document, error="unsupported_redirect_scheme")

    assert tls_repair.discover_tx_candidates(db_session) == [
        tls_repair.RepairCandidate(
            document.id,
            source.id,
            hashlib.sha256(source.last_error.encode()).hexdigest(),
        )
    ]


def test_tx_source_drift_revalidates_before_fetch(db_session, monkeypatch):
    _enable_tx_witness(monkeypatch)
    document = _tx_document(db_session)
    source = _dead_tls_job(db_session, document, error="fulltext_status=unsupported_redirect_scheme")
    assert tls_repair.seed_tx_candidates(db_session, now=NOW) == 1
    reservation = tls_repair.reserve_one_due_repair(db_session, now=NOW, reason=tls_repair.TX_REPAIR_REASON)
    source.last_error = "changed"
    monkeypatch.setattr(fulltext, "process_fetch_text_job", lambda *_a, **_k: pytest.fail("stale TX source fetched"))
    assert tls_repair.execute_reserved_repair(db_session, reservation, fetcher=object(), rawstore=object(), now=NOW) == "skipped"


def test_tx_success_uses_shared_fulltext_tail_and_ledger(db_session, monkeypatch):
    _enable_tx_witness(monkeypatch)
    document = _tx_document(db_session)
    _dead_tls_job(db_session, document, error="fulltext_status=unsupported_redirect_scheme")
    assert tls_repair.seed_tx_candidates(db_session, now=NOW) == 1
    reservation = tls_repair.reserve_one_due_repair(db_session, now=NOW, reason=tls_repair.TX_REPAIR_REASON)
    def success(db, document_id, **_kwargs):
        stored = db.get(BillDocument, document_id)
        stored.extracted_text = "official witness text"
        stored.license_note = "fulltext_status=ok url_resolver=tx_ftp_tlodocs"
        return fulltext.FetchTextResult(document_id=str(stored.id), status=fulltext.STATUS_OK, extracted_chars=21)
    monkeypatch.setattr(fulltext, "process_fetch_text_job", success)
    assert tls_repair.execute_reserved_repair(db_session, reservation, fetcher=object(), rawstore=object(), now=NOW) == "succeeded"
    repair = db_session.scalar(select(TlsFulltextRepair).where(TlsFulltextRepair.reason == tls_repair.TX_REPAIR_REASON))
    assert repair.status == "succeeded"
    assert _outcomes(db_session)[-2:] == ["admitted", "succeeded"]


def test_tx_invalid_earlier_path_does_not_starve_valid_candidate(db_session):
    invalid = _tx_document(db_session, url="ftp://ftp.legis.state.tx.us/bills/90R/witlistbill/html/SB1.htm", document_id=uuid.UUID(int=1))
    wrong_bucket = _tx_document(
        db_session,
        url="ftp://ftp.legis.state.tx.us/bills/89R/witlistbill/html/house_bills/SB00001_SB00099/SB00001S.HTM",
        document_id=uuid.UUID(int=2),
    )
    out_of_range = _tx_document(
        db_session,
        url="ftp://ftp.legis.state.tx.us/bills/89R/witlistbill/html/house_bills/HB00500_HB00599/HB00499H.htm",
        document_id=uuid.UUID(int=3),
    )
    valid = _tx_document(db_session, document_id=uuid.UUID(int=4))
    _dead_tls_job(db_session, invalid, error="unsupported_redirect_scheme")
    _dead_tls_job(db_session, wrong_bucket, error="unsupported_redirect_scheme")
    _dead_tls_job(db_session, out_of_range, error="unsupported_redirect_scheme")
    source = _dead_tls_job(db_session, valid, error="unsupported_redirect_scheme")
    candidates = tls_repair.discover_tx_candidates(db_session, limit=1)
    assert [(row.document_id, row.source_dead_job_id) for row in candidates] == [(valid.id, source.id)]


def test_repair_deadline_escapes_generic_extraction_error_handler():
    swallowed = False
    with pytest.raises(tls_repair.RepairAttemptTimeout):
        with tls_repair._attempt_deadline(seconds=5):
            try:
                signal.raise_signal(signal.SIGALRM)
            except Exception:
                swallowed = True
    assert not swallowed
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_tx_repair_real_resolver_extraction_and_retained_evidence(db_session, rawstore):
    source_url = "ftp://ftp.legis.state.tx.us/bills/89R/witlistbill/html/house_bills/HB00500_HB00599/HB00576H.htm"
    target_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    document = _tx_document(db_session, url=source_url)
    source = _dead_tls_job(db_session, document, error="unsupported_redirect_scheme")
    body = (Path(__file__).parent / "fixtures/tx_witness_89r_HB00576H.html").read_bytes()
    assert hashlib.sha256(body).hexdigest() == "f7ae367343adc5c6835d43f81302453c626ebb46633147b165af9a07c6de4880"
    requested = []
    class WireClient:
        def fetch(self, url, *, method, headers, require_body):
            assert method == "GET" and require_body
            requested.append(url)
            if url == "https://capitol.texas.gov/robots.txt":
                return SafeResponse(200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n")
            assert url == target_url
            return SafeResponse(200, {"content-type": "text/html"}, body)
    client = WireClient()
    fetcher = new_repair_fetcher(document_client=client, robots_client=client)
    assert tls_repair.seed_tx_candidates(db_session, now=NOW) == 1
    reservation = tls_repair.reserve_one_due_repair(db_session, now=NOW, reason=tls_repair.TX_REPAIR_REASON)
    assert tls_repair.execute_reserved_repair(db_session, reservation, fetcher=fetcher, rawstore=rawstore, now=NOW) == "succeeded"
    assert requested == ["https://capitol.texas.gov/robots.txt", target_url]
    assert document.url == source_url
    assert document.extracted_text and "HB 576" in document.extracted_text
    assert "url_resolver=tx_ftp_tlodocs" in document.license_note
    evidence = db_session.scalar(select(CorpusUpdateEvidence).where(CorpusUpdateEvidence.request_scope["document_id"].astext == str(document.id)))
    assert evidence.request_scope["resolver"] == "tx_ftp_tlodocs"
    assert db_session.get(OfficialRawBlob, evidence.response_sha256).data == body
    before = json.loads(db_session.get(OfficialRawBlob, evidence.before_snapshot_sha256).data)
    after = json.loads(db_session.get(OfficialRawBlob, evidence.after_snapshot_sha256).data)
    assert before["document"]["extracted_text"] is None
    assert before["document"]["url"] == source_url
    assert after["document"]["url"] == source_url
    assert after["document"]["extracted_text"] == document.extracted_text
    assert source.status == "dead" and source.last_error == "unsupported_redirect_scheme"
    assert _outcomes(db_session) == ["admitted", "succeeded"]
