"""PostgreSQL contracts for the durable, non-mutating CA observation worker.

Run these only through the disposable-Postgres harness below.  Each test uses
an outer rollback transaction, while a separate session verifies SKIP LOCKED.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from billcommons_ingest import official_ca_actions as ca_actions
from billcommons_ingest import official_discovery as discovery
from billcommons_ingest import official_observer as observer
from billcommons_schema.models import (
    Bill,
    BillAction,
    Jurisdiction,
    OfficialRawBlob,
    OfficialReconciliationRun,
    OfficialSourceObservation,
    Organization,
    OfficialSourceTarget,
    Session as SessionModel,
)
from billcommons_shared.db import get_engine
from billcommons_shared.normalize import normalize_bill_number

NOW = datetime(2026, 9, 7, 16, tzinfo=timezone.utc)
FL_SOURCE_URL = "https://www.flsenate.gov/Session/Bill/2025/7031"
FL_FIXTURE = Path(__file__).parent / "fixtures" / "fl_senate_detail_2025_7031.html"


def _tsv(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, delimiter="\t", quotechar="`", lineterminator="\n").writerows(rows)
    return stream.getvalue().encode()


def _archive(*, official_bill_id: str = "202520260AB12", history_id: str = "101") -> bytes:
    bill = [official_bill_id, "20252026", "0", "AB", "12"] + [""] * 14
    history = [
        official_bill_id, history_id, "2026-09-01 00:00:00", "Read first time.", "src",
        "2026-09-01 12:00:00", "1", "x", "x", "x", "x", "x", "x",
    ]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("BILL_TBL.dat", _tsv([bill]))
        archive.writestr("BILL_HISTORY_TBL.dat", _tsv([history]))
    return out.getvalue()


def _archive_many(count: int) -> bytes:
    bills = []
    history = []
    for number in range(1, count + 1):
        bill_id = f"202520260AB{number}"
        bills.append([bill_id, "20252026", "0", "AB", str(number)] + [""] * 14)
        history.append([
            bill_id, str(number), "2026-09-01 00:00:00", "Read first time.", "src",
            "2026-09-01 12:00:00", "1", "x", "x", "x", "x", "x", "x",
        ])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("BILL_TBL.dat", _tsv(bills))
        archive.writestr("BILL_HISTORY_TBL.dat", _tsv(history))
    return out.getvalue()


def _captured(raw: bytes):
    return ca_actions.CapturedCaOfficialActionsResponse(
        source_url=ca_actions.ca_delta_url("Mon"),
        raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        retrieved_at=NOW,
        upstream_modified="Sun, 07 Sep 2026 15:30:00 GMT",
    )


def _target(db_session, unique_abbr, *, due: datetime = NOW, scope=None, source_url=None, adapter_name=observer.ADAPTER_NAME):
    jurisdiction = Jurisdiction(name="Test California", abbreviation=unique_abbr("ZZ_CA"), classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    target = OfficialSourceTarget(
        jurisdiction_id=jurisdiction.id,
        adapter_name=adapter_name,
        source_url=source_url or ca_actions.ca_delta_url("Mon"),
        scope=scope if scope is not None else {"day": "Mon", "sessions": ["special1", "20252026 regular"]},
        enabled=True,
        cadence_seconds=300,
        next_check_at=due,
    )
    db_session.add(target)
    db_session.flush()
    # The adapter protects the exact external CA scope; tests use disposable
    # jurisdiction rows, so only their abbreviation needs to be California.
    jurisdiction.abbreviation = "CA"
    db_session.flush()
    return jurisdiction, target


def _fl_target(db_session, unique_abbr, *, due: datetime = NOW, scope=None):
    jurisdiction = Jurisdiction(name="Test Florida", abbreviation=unique_abbr("ZZ_FL"), classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    target = OfficialSourceTarget(
        jurisdiction_id=jurisdiction.id,
        adapter_name=observer.FL_ADAPTER_NAME,
        source_url=FL_SOURCE_URL,
        scope=scope if scope is not None else {
            "jurisdiction": "FL",
            "source_session_year": "2025",
            "source_bill_number": "7031",
            "coverage": "bounded_bill_history",
        },
        enabled=True,
        cadence_seconds=300,
        next_check_at=due,
    )
    db_session.add(target)
    db_session.flush()
    jurisdiction.abbreviation = "FL"
    db_session.flush()
    return jurisdiction, target


def _fl_captured(raw: bytes | None, *, error_class: str | None = None, robots: bytes | None = b"not found", http_status: int | None = 200):
    return discovery.OfficialDiscoveryCapture(
        source_url=FL_SOURCE_URL,
        retrieved_at=NOW,
        http_status=http_status,
        raw_bytes=raw,
        content_type="text/html",
        upstream_modified="Mon, 07 Sep 2026 15:30:00 GMT",
        robots_url="https://www.flsenate.gov/robots.txt",
        robots_status=404 if robots is not None else None,
        robots_bytes=robots,
        error_class=error_class,
    )


def _local_bill(db_session, jurisdiction: Jurisdiction, *, identifier="AB 12") -> Bill:
    session = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier=ca_actions.REGULAR_SESSION_IDENTIFIER,
        classification="regular",
        active=True,
    )
    db_session.add(session)
    db_session.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session.id,
        identifier=identifier,
        identifier_norm=normalize_bill_number(identifier),
        title="Test bill",
    )
    db_session.add(bill)
    db_session.flush()
    return bill


def test_success_records_verified_raw_replayable_diff_and_never_mutates_actions(db_session, unique_abbr, monkeypatch):
    jurisdiction, target = _target(db_session, unique_abbr)
    bill = _local_bill(db_session, jurisdiction)
    local = BillAction(
        bill_id=bill.id,
        description="Read first time.",
        action_date=NOW.date() - timedelta(days=6),
        source_name="older-import",
        upstream_id="ca-history:101",
    )
    ignored = BillAction(
        bill_id=bill.id,
        description="Other source fact",
        action_date=NOW.date(),
        upstream_id="openstates:unrelated",
    )
    db_session.add_all((local, ignored))
    db_session.flush()
    raw = _archive()
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result is not None
    assert (result.status, result.record_count, result.reconciliation_count) == ("succeeded", 1, 1)
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert observation.upstream_updated_at == datetime(2026, 9, 7, 15, 30, tzinfo=timezone.utc)
    assert observation.scope["coverage"] == "delta_only"
    assert db_session.get(OfficialRawBlob, observation.raw_sha256).data == raw
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert run.status == "completed"
    assert run.bill_id == bill.id
    assert run.local_snapshot_sha256 and run.diff_sha256
    report = json.loads(db_session.get(OfficialRawBlob, run.diff_sha256).data)
    assert report["summary"]["content_agreement"] == 1
    assert report["summary"]["local_records"] == 2
    assert report["summary"]["local_only_content"] == 1
    assert db_session.get(BillAction, local.id).description == "Read first time."
    assert db_session.get(BillAction, ignored.id).description == "Other source fact"
    assert target.consecutive_failures == 0
    assert target.next_check_at == NOW + timedelta(seconds=300)
    assert db_session.scalar(text("SHOW statement_timeout")) == "10s"
    assert db_session.scalar(text("SHOW lock_timeout")) == "5s"
    assert db_session.scalar(text("SHOW idle_in_transaction_session_timeout")) in {"240s", "4min"}


def test_malformed_archive_retains_raw_invalid_observation_and_backoff(db_session, unique_abbr, monkeypatch):
    _, target = _target(db_session, unique_abbr)
    raw = b"not a ZIP"
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.status == "invalid"
    assert observation.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert db_session.get(OfficialRawBlob, observation.raw_sha256).data == raw
    assert observation.error_class == "OfficialCaActionsError"
    assert target.consecutive_failures == 1
    assert target.next_check_at == NOW + timedelta(seconds=300)


def test_failed_fetch_stores_no_raw_and_uses_durable_backoff(db_session, unique_abbr, monkeypatch):
    _, target = _target(db_session, unique_abbr)

    def unavailable(day: str):
        raise RuntimeError("never persist upstream message or URL tokens")

    monkeypatch.setattr(observer, "_capture_ca_response", unavailable)
    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "failed"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.raw_sha256 is None
    assert observation.error_class == "RuntimeError"
    assert db_session.scalar(select(func.count()).select_from(OfficialRawBlob)) == 0
    assert target.consecutive_failures == 1
    assert target.next_check_at == NOW + timedelta(seconds=300)


def test_http_503_capture_failure_persists_safe_diagnosis_and_observed_status(
    db_session, unique_abbr, monkeypatch
):
    _, target = _target(db_session, unique_abbr)

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        monkeypatch.setattr(
            observer,
            "_capture_ca_response",
            lambda day: ca_actions.fetch_ca_official_actions_response(day, client=client, retrieved_at=NOW),
        )
        result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "failed"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.raw_sha256 is None
    assert observation.http_status == 503
    assert observation.scope["failure"] == {
        "version": 1,
        "stage": "capture",
        "code": "http_status_unexpected",
        "recommended_action": "retry_as_scheduled",
        "details": {"http_status": 503},
    }
    assert "failure" not in target.scope
    assert db_session.scalar(select(func.count()).select_from(OfficialRawBlob)) == 0


def test_capture_deadline_never_invents_http_200(db_session, unique_abbr, monkeypatch):
    _, _ = _target(db_session, unique_abbr)
    timeout = ca_actions.OfficialCaActionsError(
        "private capture timeout text",
        code="response_deadline_exceeded",
        details={"limit": int(ca_actions.TOTAL_RESPONSE_DEADLINE_SECONDS)},
    )
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: (_ for _ in ()).throw(timeout))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "failed"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.raw_sha256 is None
    assert observation.http_status is None
    assert observation.scope["failure"] == {
        "version": 1,
        "stage": "capture",
        "code": "response_deadline_exceeded",
        "recommended_action": "retry_as_scheduled",
        "details": {"limit": int(ca_actions.TOTAL_RESPONSE_DEADLINE_SECONDS)},
    }


def test_member_count_parse_cap_keeps_raw_and_diagnosis_without_corpus_writes(
    db_session, unique_abbr, monkeypatch
):
    _, target = _target(db_session, unique_abbr)
    stream = io.BytesIO()
    bill = ["202520260AB12", "20252026", "0", "AB", "12"] + [""] * 14
    history = [
        "202520260AB12", "101", "2026-09-01 00:00:00", "Read first time.", "src",
        "2026-09-01 12:00:00", "1", "x", "x", "x", "x", "x", "x",
    ]
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("BILL_TBL.dat", _tsv([bill]))
        archive.writestr("BILL_HISTORY_TBL.dat", _tsv([history]))
        for index in range(255):
            archive.writestr(f"auxiliary-{index}.dat", b"x")
    raw = stream.getvalue()
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert observation.scope["failure"] == {
        "version": 1,
        "stage": "parse",
        "code": "archive_member_count_limit_exceeded",
        "recommended_action": "review_parser_limit",
        "details": {"observed": 257, "limit": ca_actions.MAX_ZIP_MEMBERS},
    }
    assert "failure" not in target.scope
    assert db_session.scalar(select(func.count()).select_from(OfficialRawBlob)) == 1
    assert db_session.scalar(select(func.count()).select_from(OfficialReconciliationRun)) == 0
    assert db_session.scalar(select(func.count()).select_from(BillAction)) == 0


def test_missing_local_mapping_is_partial_and_does_not_guess_a_bill(db_session, unique_abbr, monkeypatch):
    _, target = _target(db_session, unique_abbr)
    raw = _archive()
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "succeeded"
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert run.status == "partial"
    assert run.bill_id is None
    assert run.summary == {"reason": "local_session_missing_or_ambiguous"}
    assert run.local_snapshot_sha256 is None
    assert run.diff_sha256 is None
    assert target.consecutive_failures == 0


def test_invalid_target_is_observed_without_calling_external_capture(db_session, unique_abbr, monkeypatch):
    _, target = _target(
        db_session,
        unique_abbr,
        scope={"day": "Mon", "sessions": ["20252026 regular", "special1"], "extra": True},
    )
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: (_ for _ in ()).throw(AssertionError("should not fetch")))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.status == "invalid"
    assert observation.error_class == "InvalidOfficialTarget"
    assert target.next_check_at == NOW + timedelta(seconds=300)


def test_fl_detail_success_retains_one_bill_snapshot_without_local_mutation(db_session, unique_abbr, monkeypatch):
    _, target = _fl_target(db_session, unique_abbr)
    raw = FL_FIXTURE.read_bytes()
    before_actions = db_session.scalar(select(func.count()).select_from(BillAction))
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and (result.status, result.record_count, result.reconciliation_count) == ("succeeded", 46, 1)
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.adapter_name == observer.FL_ADAPTER_NAME
    assert observation.adapter_version == observer.fl_actions.ADAPTER_VERSION
    assert observation.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert observation.upstream_updated_at == datetime(2026, 9, 7, 15, 30, tzinfo=timezone.utc)
    assert observation.scope["coverage"] == "bounded_bill_history"
    assert observation.scope["semantic_label"] == "observed_bill_history_snapshot"
    assert observation.scope["source_session_year"] == "2025"
    assert observation.scope["source_bill_number"] == "7031"
    assert "local_session" not in observation.scope
    assert "freshness" not in observation.scope
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert run.status == "partial"
    assert run.bill_id is None
    assert run.comparator_version == "fl-senate-action-content-multiset/1"
    assert run.summary == {"reason": "local_fl_regular_session_missing_or_ambiguous"}
    snapshot = json.loads(db_session.get(OfficialRawBlob, observation.scope["parsed_snapshot_sha256"]).data)
    assert snapshot["source_raw_sha256"] == observation.raw_sha256
    assert snapshot["scope"] == {
        "jurisdiction": "FL", "source_session_year": "2025", "source_bill_number": "7031",
        "coverage": "bounded_bill_history",
    }
    assert len(snapshot["actions"]) == 46
    assert snapshot["actions"][-1]["date"] == "2026-06-18"
    assert "occurrence" in snapshot["interpretation"]
    assert db_session.scalar(select(func.count()).select_from(BillAction)) == before_actions
    assert target.consecutive_failures == 0
    assert target.next_check_at == NOW + timedelta(seconds=300)


def _fl_local_bill(db_session, jurisdiction: Jurisdiction, *, session_identifier: str = "2025 Regular Session", classification: str = "regular") -> Bill:
    session = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier=session_identifier,
        classification=classification,
        active=True,
    )
    db_session.add(session)
    db_session.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session.id,
        identifier="HB 7031",
        identifier_norm=normalize_bill_number("HB 7031"),
        title="Taxation",
    )
    db_session.add(bill)
    db_session.flush()
    return bill


def _fl_chamber(db_session, jurisdiction: Jurisdiction, chamber: str) -> Organization:
    expected = {"House": "lower", "Senate": "upper"}
    organization = Organization(jurisdiction_id=jurisdiction.id, name=chamber, classification=expected[chamber])
    db_session.add(organization)
    db_session.flush()
    return organization


def test_fl_exact_regular_session_and_bill_produce_content_comparison(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    bill = _fl_local_bill(db_session, jurisdiction)
    parsed = observer.fl_actions.parse_florida_senate_bill_history(FL_FIXTURE.read_bytes(), source_url=FL_SOURCE_URL)
    action = parsed.actions[0]
    local = BillAction(
        bill_id=bill.id,
        organization_id=_fl_chamber(db_session, jurisdiction, action.chamber).id,
        description=action.description,
        action_date=action.action_date,
        source_name="retained-import",
        upstream_id="unrelated-local-id",
    )
    db_session.add(local)
    db_session.flush()
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(FL_FIXTURE.read_bytes()))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and (result.status, result.record_count, result.reconciliation_count) == ("succeeded", 46, 1)
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert run.status == "completed" and run.bill_id == bill.id
    assert run.official_bill_id == "fl-senate:2025:HB 7031"
    local_fixture = json.loads(db_session.get(OfficialRawBlob, run.local_snapshot_sha256).data)
    report = json.loads(db_session.get(OfficialRawBlob, run.diff_sha256).data)
    assert local_fixture["events"][0]["session"] == "2025 Regular Session"
    assert report["scope"] == {"jurisdiction": "FL", "session": "2025 Regular Session", "bill_id": "HB 7031"}
    assert report["summary"]["content_agreement"] == 1
    assert report["content_agreement"][0]["occurrence_proof"] is False
    assert db_session.get(BillAction, local.id).description == action.description


def test_fl_cs_prefixed_heading_maps_only_to_the_base_bill_in_same_exact_session(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    bill = _fl_local_bill(db_session, jurisdiction)
    raw = FL_FIXTURE.read_bytes().replace(b"HB 7031: Taxation", b"CS/CS/HB 7031: Taxation", 1)
    parsed = observer.fl_actions.parse_florida_senate_bill_history(raw, source_url=FL_SOURCE_URL)
    assert parsed.bill_identifier == "CS/CS/HB 7031"
    assert observer._fl_local_identifier(parsed.bill_identifier) == "HB 7031"
    action = parsed.actions[0]
    db_session.add(BillAction(
        bill_id=bill.id,
        organization_id=_fl_chamber(db_session, jurisdiction, action.chamber).id,
        description=action.description,
        action_date=action.action_date,
    ))
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(raw))

    observer.observe_due_target(db_session, now=NOW)
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert run.status == "completed" and run.bill_id == bill.id
    assert run.official_bill_id == "fl-senate:2025:CS/CS/HB 7031"


def test_fl_source_year_never_substitutes_a_same_number_bill_in_another_session(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    wrong_year_bill = _fl_local_bill(db_session, jurisdiction, session_identifier="2026 Regular Session")
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(FL_FIXTURE.read_bytes()))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.reconciliation_count == 1
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert run.status == "partial" and run.bill_id is None
    assert run.summary == {"reason": "local_fl_regular_session_missing_or_ambiguous"}
    assert db_session.get(Bill, wrong_year_bill.id) is not None


def test_fl_same_day_same_text_in_different_chambers_stays_distinct_content(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    bill = _fl_local_bill(db_session, jurisdiction)
    house = _fl_chamber(db_session, jurisdiction, "House")
    senate = _fl_chamber(db_session, jurisdiction, "Senate")
    # The fixture's first House action is deliberately duplicated locally as
    # Senate content. It must not be counted as an agreement merely by text.
    parsed = observer.fl_actions.parse_florida_senate_bill_history(FL_FIXTURE.read_bytes(), source_url=FL_SOURCE_URL)
    action = parsed.actions[0]
    db_session.add_all((
        BillAction(bill_id=bill.id, organization_id=house.id, description=action.description, action_date=action.action_date),
        BillAction(bill_id=bill.id, organization_id=senate.id, description=action.description, action_date=action.action_date),
    ))
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(FL_FIXTURE.read_bytes()))

    observer.observe_due_target(db_session, now=NOW)
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    report = json.loads(db_session.get(OfficialRawBlob, run.diff_sha256).data)
    same_content = [item for item in report["local_only_content"] if item["content"]["description"] == action.description.casefold()]
    assert len(same_content) == 1
    assert same_content[0]["content"] == {
        "date": action.action_date.isoformat(), "chamber": "Senate", "description": action.description.casefold(),
    }
    assert same_content[0]["official_count"] == 0
    assert same_content[0]["local_count"] == same_content[0]["unmatched_count"] == 1


def test_fl_unknown_local_chamber_is_ambiguous_not_filled_from_bill(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    bill = _fl_local_bill(db_session, jurisdiction)
    parsed = observer.fl_actions.parse_florida_senate_bill_history(FL_FIXTURE.read_bytes(), source_url=FL_SOURCE_URL)
    action = parsed.actions[0]
    db_session.add(BillAction(bill_id=bill.id, description=action.description, action_date=action.action_date))
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(FL_FIXTURE.read_bytes()))

    observer.observe_due_target(db_session, now=NOW)
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    report = json.loads(db_session.get(OfficialRawBlob, run.diff_sha256).data)
    assert report["summary"]["content_agreement"] == 0
    assert any(item["side"] == "local" and item["reason"] == "missing_chamber" for item in report["ambiguous_insufficient_evidence"])


def test_fl_cross_jurisdiction_organization_rejects_comparison(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    bill = _fl_local_bill(db_session, jurisdiction)
    foreign = Jurisdiction(name="Foreign", abbreviation=unique_abbr("ZZ_ORG"), classification="state")
    db_session.add(foreign)
    db_session.flush()
    organization = Organization(jurisdiction_id=foreign.id, name="House", classification="lower")
    db_session.add(organization)
    db_session.flush()
    parsed = observer.fl_actions.parse_florida_senate_bill_history(FL_FIXTURE.read_bytes(), source_url=FL_SOURCE_URL)
    action = parsed.actions[0]
    db_session.add(BillAction(bill_id=bill.id, organization_id=organization.id, description=action.description, action_date=action.action_date))
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(FL_FIXTURE.read_bytes()))

    result = observer.observe_due_target(db_session, now=NOW)
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert result and result.reconciliation_count == 1
    assert run.status == "failed" and run.summary == {"reason": "comparison_failed"}
    assert run.error_class == "ValueError"


def test_fl_comparison_deadline_creates_a_failed_run_without_corpus_mutation(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    bill = _fl_local_bill(db_session, jurisdiction)
    parsed = observer.fl_actions.parse_florida_senate_bill_history(FL_FIXTURE.read_bytes(), source_url=FL_SOURCE_URL)
    action = parsed.actions[0]
    local = BillAction(
        bill_id=bill.id,
        organization_id=_fl_chamber(db_session, jurisdiction, action.chamber).id,
        description=action.description,
        action_date=action.action_date,
    )
    db_session.add(local)
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(FL_FIXTURE.read_bytes()))
    calls = {"count": 0}
    original = observer._require_deadline

    def expire_after_initial_check(started_at):
        calls["count"] += 1
        if calls["count"] >= 5:
            raise observer.ObservationDeadlineExceeded("fixture deadline")
        return original(started_at)

    monkeypatch.setattr(observer, "_require_deadline", expire_after_initial_check)
    result = observer.observe_due_target(db_session, now=NOW)
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    assert result and result.status == "succeeded" and result.reconciliation_count == 1
    assert run.status == "failed" and run.summary == {"reason": "comparison_failed"}
    assert run.error_class == "ObservationDeadlineExceeded"
    assert db_session.get(BillAction, local.id).description == action.description


def test_fl_robots_denial_retains_policy_and_backoff_without_parsing(db_session, unique_abbr, monkeypatch):
    _, target = _fl_target(db_session, unique_abbr)
    policy = b"User-agent: *\nDisallow: /\n"
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(None, error_class="robots_disallowed", robots=policy, http_status=None))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "failed"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.raw_sha256 is None
    assert observation.error_class == "robots_disallowed"
    assert observation.scope["semantic_label"] == "not_established"
    assert observation.scope["robots"]["raw_sha256"] == hashlib.sha256(policy).hexdigest()
    assert observation.scope["failure"]["stage"] == "capture"
    assert target.consecutive_failures == 1
    assert target.next_check_at == NOW + timedelta(seconds=300)
    assert db_session.get(OfficialRawBlob, hashlib.sha256(policy).hexdigest()).data == policy


def test_fl_scope_mismatch_is_invalid_before_capture_and_backs_off(db_session, unique_abbr, monkeypatch):
    _, target = _fl_target(
        db_session,
        unique_abbr,
        scope={"jurisdiction": "FL", "source_session_year": "2025", "source_bill_number": "7031", "coverage": "bounded_bill_history", "extra": True},
    )
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: (_ for _ in ()).throw(AssertionError("scope mismatch must not fetch")))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.error_class == "InvalidOfficialTarget"
    assert observation.adapter_version == observer.fl_actions.ADAPTER_VERSION
    assert observation.scope["semantic_label"] == "not_established"
    assert db_session.scalar(select(func.count()).select_from(OfficialRawBlob)) == 0
    assert target.consecutive_failures == 1
    assert target.next_check_at == NOW + timedelta(seconds=300)


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>not a Florida bill detail page</html>",
        FL_FIXTURE.read_bytes().replace(
            b'<div class="tabbody " id="tabBodyBillHistory">',
            b'<div class="tabbody " id="tabBodyBillHistory"><table><thead></thead><tbody></tbody></table>',
            1,
        ),
        (b"<div>" * observer.fl_actions.MAX_DOM_DEPTH) + FL_FIXTURE.read_bytes() + (b"</div>" * observer.fl_actions.MAX_DOM_DEPTH),
    ],
)
def test_fl_malformed_or_capped_parse_retains_raw_and_never_writes_actions(db_session, unique_abbr, monkeypatch, raw):
    _, target = _fl_target(db_session, unique_abbr)
    before_actions = db_session.scalar(select(func.count()).select_from(BillAction))
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert db_session.get(OfficialRawBlob, observation.raw_sha256).data == raw
    assert observation.error_class == "OfficialFloridaSenateActionsError"
    assert observation.scope["failure"]["stage"] == "parse"
    assert observation.scope["semantic_label"] == "not_established"
    assert "parsed_snapshot_sha256" not in observation.scope
    assert db_session.scalar(select(func.count()).select_from(BillAction)) == before_actions
    assert target.consecutive_failures == 1


def test_fl_empty_robots_policy_is_valid_and_retained_as_zero_byte_evidence(db_session, unique_abbr, monkeypatch):
    _, target = _fl_target(db_session, unique_abbr)
    raw = FL_FIXTURE.read_bytes()
    monkeypatch.setattr(
        observer,
        "_capture_fl_senate_detail",
        lambda source_url: _fl_captured(raw, robots=b""),
    )

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and (result.status, result.record_count) == ("succeeded", 46)
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.scope["semantic_label"] == "observed_bill_history_snapshot"
    assert observation.scope["robots"] == {
        "source_url": "https://www.flsenate.gov/robots.txt",
        "http_status": 404,
        "raw_sha256": None,
        "body_bytes": 0,
    }
    assert db_session.scalar(select(func.count()).select_from(OfficialRawBlob)) == 2
    assert target.consecutive_failures == 0


def test_fl_malformed_robots_retains_independently_valid_page_raw_before_failure(db_session, unique_abbr, monkeypatch):
    _, target = _fl_target(db_session, unique_abbr)
    raw = FL_FIXTURE.read_bytes()
    malformed_robots = b"x" * (observer.MAX_BLOB_BYTES + 1)
    monkeypatch.setattr(
        observer,
        "_capture_fl_senate_detail",
        lambda source_url: _fl_captured(raw, robots=malformed_robots),
    )

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    assert observation.raw_sha256 == raw_sha256
    assert db_session.get(OfficialRawBlob, raw_sha256).data == raw
    assert observation.scope["semantic_label"] == "not_established"
    assert observation.scope["robots"]["raw_sha256"] is None
    assert observation.scope["robots"]["body_bytes"] == len(malformed_robots)
    assert observation.scope["failure"]["stage"] == "capture"
    assert target.consecutive_failures == 1


def test_fl_snapshot_storage_failure_escapes_and_rolls_back_claim(monkeypatch):
    # Keep the seeded target inside an outer transaction. Session commits
    # release savepoints only: no other worker can see an enabled fixture,
    # and the connection rolls everything back even if any assertion fails.
    engine = get_engine()
    raw = FL_FIXTURE.read_bytes()
    monkeypatch.setattr(observer, "_capture_fl_senate_detail", lambda source_url: _fl_captured(raw))
    original_store = observer.store_official_raw_blob
    calls = 0

    def fail_snapshot(db, data, content_type):
        nonlocal calls
        calls += 1
        if content_type == "application/json":
            raise RuntimeError("injected snapshot storage outage")
        return original_store(db, data, content_type)

    monkeypatch.setattr(observer, "store_official_raw_blob", fail_snapshot)
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            with Session(connection, join_transaction_mode="create_savepoint", autoflush=False) as seed:
                jurisdiction = Jurisdiction(name="Atomic Florida", abbreviation="FL", classification="state")
                seed.add(jurisdiction)
                seed.flush()
                target = OfficialSourceTarget(
                    jurisdiction_id=jurisdiction.id,
                    adapter_name=observer.FL_ADAPTER_NAME,
                    source_url=FL_SOURCE_URL,
                    scope={"jurisdiction": "FL", "source_session_year": "2025", "source_bill_number": "7031", "coverage": "bounded_bill_history"},
                    enabled=True, cadence_seconds=300, next_check_at=NOW,
                )
                seed.add(target)
                seed.commit()
                target_id = target.id
            with Session(engine) as unrelated_worker:
                assert unrelated_worker.get(OfficialSourceTarget, target_id) is None
            models = (OfficialSourceObservation, OfficialRawBlob, BillAction)
            before = {model: connection.scalar(select(func.count()).select_from(model)) for model in models}
            with Session(connection, join_transaction_mode="create_savepoint", autoflush=False) as transaction:
                with pytest.raises(RuntimeError, match="injected snapshot storage outage"):
                    observer.observe_due_target(transaction, now=NOW)
                transaction.rollback()
            with Session(connection, join_transaction_mode="create_savepoint", autoflush=False) as check:
                stored = check.get(OfficialSourceTarget, target_id)
                assert calls == 3
                assert stored.consecutive_failures == 0 and stored.next_check_at == NOW
                assert {model: check.scalar(select(func.count()).select_from(model)) for model in models} == before
                assert check.scalar(select(OfficialSourceObservation.id).where(OfficialSourceObservation.target_id == target_id)) is None
        finally:
            outer.rollback()


def test_due_claim_uses_skip_locked_and_no_due_target_returns_none(db_session, unique_abbr):
    # A second connection can only prove SKIP LOCKED after it can see a
    # committed target.  Seed it outside db_session's rollback transaction.
    seed = Session(get_engine(), autoflush=False)
    try:
        jurisdiction = Jurisdiction(name="Lock Test California", abbreviation="CA", classification="state")
        seed.add(jurisdiction)
        seed.flush()
        target = OfficialSourceTarget(
            jurisdiction_id=jurisdiction.id,
            adapter_name=observer.ADAPTER_NAME,
            source_url=ca_actions.ca_delta_url("Mon"),
            scope={"day": "Mon", "sessions": ["20252026 regular", "special1"]},
            enabled=True,
            cadence_seconds=300,
            next_check_at=NOW,
        )
        seed.add(target)
        seed.commit()
        target_id = target.id
        jurisdiction_id = jurisdiction.id
    finally:
        seed.close()
    locker = Session(get_engine(), autoflush=False)
    locker.execute(select(OfficialSourceTarget).where(OfficialSourceTarget.id == target_id).with_for_update())
    other = Session(get_engine(), autoflush=False)
    try:
        assert observer.observe_due_target(other, now=NOW) is None
        other.rollback()
    finally:
        other.close()
        locker.rollback()
        locker.close()
    cleaner = Session(get_engine(), autoflush=False)
    cleaner.execute(
        OfficialSourceTarget.__table__.update()
        .where(OfficialSourceTarget.id == target_id)
        .values(next_check_at=NOW + timedelta(seconds=1))
    )
    cleaner.commit()
    assert observer.observe_due_target(cleaner, now=NOW) is None
    # The disposable harness removes its database after the run; clean this
    # committed seed now so the test also remains repeatable in one database.
    cleaner.execute(delete(OfficialSourceTarget).where(OfficialSourceTarget.id == target_id))
    cleaner.execute(delete(Jurisdiction).where(Jurisdiction.id == jurisdiction_id))
    cleaner.commit()
    cleaner.close()


def test_repeated_unchanged_capture_reuses_content_addressed_evidence(db_session, unique_abbr, monkeypatch):
    jurisdiction, target = _target(db_session, unique_abbr)
    bill = _local_bill(db_session, jurisdiction)
    db_session.add(
        BillAction(
            bill_id=bill.id,
            description="Read first time.",
            action_date=NOW.date() - timedelta(days=6),
            upstream_id="ca-history:101",
        )
    )
    raw = _archive()
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    first = observer.observe_due_target(db_session, now=NOW)
    target.next_check_at = NOW
    db_session.flush()
    second = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert first and second and first.status == second.status == "succeeded"
    assert db_session.scalar(select(func.count()).select_from(OfficialSourceObservation)) == 2
    # Source archive, local fixture, and reconciliation report are unchanged
    # and therefore each occupy one blob despite two observations.
    assert db_session.scalar(select(func.count()).select_from(OfficialRawBlob)) == 3


def test_changed_history_id_agrees_on_content_without_occurrence_proof(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _target(db_session, unique_abbr)
    bill = _local_bill(db_session, jurisdiction)
    db_session.add(
        BillAction(
            bill_id=bill.id,
            description="Read first time.",
            action_date=NOW.date() - timedelta(days=6),
            upstream_id="ca-history:different-id",
        )
    )
    raw = _archive(history_id="101")
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "succeeded"
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    report = json.loads(db_session.get(OfficialRawBlob, run.diff_sha256).data)
    assert report["summary"]["content_agreement"] == 1
    assert report["summary"]["official_only_content"] == 0
    assert report["summary"]["local_only_content"] == 0
    assert report["content_agreement"][0]["occurrence_proof"] is False


def test_nested_scope_values_are_invalid_not_a_worker_crash(db_session, unique_abbr, monkeypatch):
    _, _ = _target(
        db_session,
        unique_abbr,
        scope={"day": "Mon", "sessions": [["20252026 regular"], "special1"]},
    )
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: (_ for _ in ()).throw(AssertionError("must not fetch")))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.adapter_name == observer.ADAPTER_NAME
    assert observation.adapter_version == observer.UNKNOWN_ADAPTER_VERSION


def test_unknown_adapter_identity_is_preserved_on_invalid_target(db_session, unique_abbr, monkeypatch):
    _, _ = _target(db_session, unique_abbr, adapter_name="future-reviewed-adapter")
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: (_ for _ in ()).throw(AssertionError("must not fetch")))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "invalid"
    observation = db_session.execute(select(OfficialSourceObservation)).scalar_one()
    assert observation.adapter_name == "future-reviewed-adapter"
    assert observation.adapter_version == observer.UNKNOWN_ADAPTER_VERSION


def test_storage_failure_propagates_so_the_claim_can_roll_back(db_session, unique_abbr, monkeypatch):
    _, target = _target(db_session, unique_abbr)
    raw = _archive()
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    def storage_outage(db, data, content_type):
        raise RuntimeError("database storage unavailable")

    monkeypatch.setattr(observer, "store_official_raw_blob", storage_outage)
    with pytest.raises(RuntimeError, match="database storage unavailable"):
        observer.observe_due_target(db_session, now=NOW)


def test_comparison_cap_records_durable_continuation(db_session, unique_abbr, monkeypatch):
    _, _ = _target(db_session, unique_abbr)
    raw = b"x"
    captured = _captured(raw)
    bill_ids = tuple(sorted(f"202520260AB{number}" for number in range(1, observer.MAX_RECONCILIATIONS_PER_OBSERVATION + 2)))
    batch = SimpleNamespace(
        source_url=captured.source_url,
        raw_bytes=raw,
        sha256=captured.sha256,
        retrieved_at=NOW,
        upstream_modified=None,
        event_count=0,
        scoped_bill_ids=bill_ids,
        events_by_official_bill_id={},
    )
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: captured)
    monkeypatch.setattr(observer, "_parse_ca_response", lambda captured: batch)

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "continuing"
    assert result.reconciliation_count == observer.MAX_RECONCILIATIONS_PER_OBSERVATION
    runs = list(db_session.execute(select(OfficialReconciliationRun).order_by(OfficialReconciliationRun.official_bill_id)).scalars())
    assert len(runs) == observer.MAX_RECONCILIATIONS_PER_OBSERVATION
    target_scope = db_session.execute(select(OfficialSourceTarget.scope)).scalar_one()
    assert target_scope
    assert target_scope["continuation"]["next_bill_index"] == observer.MAX_RECONCILIATIONS_PER_OBSERVATION


def test_non_ca_local_action_is_retained_as_unpaired_snapshot_evidence(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _target(db_session, unique_abbr)
    bill = _local_bill(db_session, jurisdiction)
    unknown = BillAction(
        bill_id=bill.id,
        description="Unidentified local event",
        action_date=NOW.date(),
        source_name="legacy-import",
        upstream_id="legacy:17",
    )
    db_session.add(unknown)
    raw = _archive()
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: _captured(raw))

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "succeeded"
    run = db_session.execute(select(OfficialReconciliationRun)).scalar_one()
    fixture = json.loads(db_session.get(OfficialRawBlob, run.local_snapshot_sha256).data)
    local_event = fixture["events"][0]
    assert local_event["occurrence_id"] is None
    assert local_event["local_record_id"] == str(unknown.id)
    report = json.loads(db_session.get(OfficialRawBlob, run.diff_sha256).data)
    assert report["summary"]["local_only_content"] == 1


def test_total_deadline_preserves_a_durable_continuation_cursor(db_session, unique_abbr, monkeypatch):
    _, target = _target(db_session, unique_abbr)
    raw = b"x"
    captured = _captured(raw)
    bill_id = "202520260AB1"
    batch = SimpleNamespace(
        source_url=captured.source_url,
        raw_bytes=raw,
        sha256=captured.sha256,
        retrieved_at=NOW,
        upstream_modified=None,
        event_count=0,
        scoped_bill_ids=(bill_id,),
        events_by_official_bill_id={},
    )
    ticks = iter((0.0, 0.0, 0.0, 0.0, observer.OBSERVATION_DEADLINE_SECONDS + 1.0))
    monkeypatch.setattr(observer, "_monotonic", lambda: next(ticks))
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: captured)
    monkeypatch.setattr(observer, "_parse_ca_response", lambda captured: batch)

    result = observer.observe_due_target(db_session, now=NOW)
    db_session.flush()

    assert result and result.status == "continuing" and result.reconciliation_count == 0
    assert db_session.execute(select(func.count()).select_from(OfficialReconciliationRun)).scalar_one() == 0
    assert db_session.execute(select(OfficialSourceTarget.scope)).scalar_one()["continuation"]["next_bill_index"] == 0
    assert target.consecutive_failures == 1
    assert target.next_check_at == NOW + timedelta(seconds=300)


def test_continuation_replay_failure_keeps_old_raw_without_claiming_http_response(
    db_session, unique_abbr, monkeypatch
):
    """A failed replay is a local attempt, not a second source HTTP 200."""
    _, target = _target(db_session, unique_abbr)
    raw = _archive_many(observer.MAX_RECONCILIATIONS_PER_OBSERVATION + 1)
    captures = []
    monkeypatch.setattr(
        observer, "_capture_ca_response", lambda day: (captures.append(day), _captured(raw))[1]
    )

    first = observer.observe_due_target(db_session, now=NOW)
    assert first and first.status == "continuing"
    source = db_session.scalar(select(OfficialSourceObservation).where(
        OfficialSourceObservation.status == "succeeded"
    ))
    assert source.raw_sha256 and source.http_status == 200

    def malformed_replay(*_args, **_kwargs):
        raise ca_actions.OfficialCaActionsError("fixture parser failure")

    monkeypatch.setattr(ca_actions, "parse_ca_official_actions_zip", malformed_replay)
    result = observer.observe_due_target(db_session, now=NOW + timedelta(seconds=1))
    assert result and result.status == "failed"
    failures = list(db_session.scalars(select(OfficialSourceObservation).where(
        OfficialSourceObservation.status == "failed"
    )))
    assert len(failures) == 1
    failure = failures[0]
    assert failure.raw_sha256 == source.raw_sha256
    assert failure.http_status is None
    assert failure.retrieved_at == NOW + timedelta(seconds=1)
    assert captures == ["Mon"]


def test_continuation_replays_exact_archive_after_rollback_without_refetching(monkeypatch):
    """A committed cursor resumes each official bill exactly once after rollback."""

    engine = get_engine()
    seed = Session(engine, autoflush=False)
    try:
        jurisdiction = Jurisdiction(name="Continuation California", abbreviation="CA", classification="state")
        seed.add(jurisdiction)
        seed.flush()
        target = OfficialSourceTarget(
            jurisdiction_id=jurisdiction.id,
            adapter_name=observer.ADAPTER_NAME,
            source_url=ca_actions.ca_delta_url("Mon"),
            scope={"day": "Mon", "sessions": ["20252026 regular", "special1"]},
            enabled=True,
            cadence_seconds=300,
            next_check_at=NOW,
        )
        seed.add(target)
        session = SessionModel(jurisdiction_id=jurisdiction.id, identifier=ca_actions.REGULAR_SESSION_IDENTIFIER)
        seed.add(session)
        seed.flush()
        bill = Bill(
            jurisdiction_id=jurisdiction.id,
            session_id=session.id,
            identifier="AB 1",
            identifier_norm=normalize_bill_number("AB 1"),
            title="Cursor proof bill",
        )
        seed.add(bill)
        seed.flush()
        seed.add(BillAction(
            bill_id=bill.id,
            description="Read first time.",
            action_date=NOW.date() - timedelta(days=6),
            upstream_id="ca-history:1",
        ))
        seed.commit()
        target_id, jurisdiction_id, session_id, bill_id = target.id, jurisdiction.id, session.id, bill.id
    finally:
        seed.close()

    raw = _archive_many(observer.MAX_RECONCILIATIONS_PER_OBSERVATION + 1)
    fetches: list[str] = []
    monkeypatch.setattr(observer, "_capture_ca_response", lambda day: (fetches.append(day), _captured(raw))[1])
    original_partial = observer._add_partial_run
    try:
        first = Session(engine, autoflush=False)
        try:
            result = observer.observe_due_target(first, now=NOW)
            assert result and result.status == "continuing"
            first.commit()
        finally:
            first.close()

        check = Session(engine, autoflush=False)
        try:
            stored_target = check.get(OfficialSourceTarget, target_id)
            source_observation = check.execute(select(OfficialSourceObservation)).scalar_one()
            assert stored_target.scope["continuation"]["next_bill_index"] == observer.MAX_RECONCILIATIONS_PER_OBSERVATION
            assert stored_target.scope["continuation"]["raw_sha256"] == source_observation.raw_sha256
            assert check.get(OfficialRawBlob, source_observation.raw_sha256).data == raw
        finally:
            check.close()

        monkeypatch.setattr(observer, "_add_partial_run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected rollback")))
        failed = Session(engine, autoflush=False)
        try:
            with pytest.raises(RuntimeError, match="injected rollback"):
                observer.observe_due_target(failed, now=NOW + timedelta(seconds=1))
            failed.rollback()
        finally:
            failed.close()
        monkeypatch.setattr(observer, "_add_partial_run", original_partial)

        retry = Session(engine, autoflush=False)
        try:
            stored_target = retry.get(OfficialSourceTarget, target_id)
            assert stored_target.scope["continuation"]["next_bill_index"] == observer.MAX_RECONCILIATIONS_PER_OBSERVATION
            result = observer.observe_due_target(retry, now=NOW + timedelta(seconds=2))
            assert result and result.status == "succeeded" and result.reconciliation_count == 1
            retry.commit()
        finally:
            retry.close()

        verify = Session(engine, autoflush=False)
        try:
            target_after = verify.get(OfficialSourceTarget, target_id)
            runs = list(verify.execute(select(OfficialReconciliationRun)).scalars())
            assert "continuation" not in target_after.scope
            assert len(runs) == observer.MAX_RECONCILIATIONS_PER_OBSERVATION + 1
            assert {run.official_bill_id for run in runs} == {
                f"202520260AB{number}" for number in range(1, observer.MAX_RECONCILIATIONS_PER_OBSERVATION + 2)
            }
            completed = next(run for run in runs if run.official_bill_id == "202520260AB1")
            assert completed.status == "completed"
            assert completed.local_snapshot_sha256 and completed.diff_sha256
            assert verify.get(OfficialRawBlob, completed.local_snapshot_sha256)
            assert verify.get(OfficialRawBlob, completed.diff_sha256)
            assert fetches == ["Mon"]
        finally:
            verify.close()
    finally:
        cleanup = Session(engine, autoflush=False)
        try:
            cleanup.execute(delete(OfficialReconciliationRun).where(OfficialReconciliationRun.observation_id.in_(select(OfficialSourceObservation.id).where(OfficialSourceObservation.target_id == target_id))))
            cleanup.execute(delete(OfficialSourceObservation).where(OfficialSourceObservation.target_id == target_id))
            cleanup.execute(delete(BillAction).where(BillAction.bill_id == bill_id))
            cleanup.execute(delete(Bill).where(Bill.id == bill_id))
            cleanup.execute(delete(SessionModel).where(SessionModel.id == session_id))
            cleanup.execute(delete(OfficialSourceTarget).where(OfficialSourceTarget.id == target_id))
            cleanup.execute(delete(Jurisdiction).where(Jurisdiction.id == jurisdiction_id))
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.mark.parametrize('value', [None, {}, [], {'next_bill_index': True}])
def test_explicit_malformed_continuation_is_rejected(value):
    with pytest.raises(observer.InvalidOfficialTarget):
        observer._continuation_from_scope({'continuation': value})
