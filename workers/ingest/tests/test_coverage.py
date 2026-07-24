"""Tests for coverage.recompute_all_coverage / build_coverage_report.

Business intent: coverage rows must accurately reflect the DB (not just be
static), the state machine must only ever advance forward from counts (never
regress a row a validation run already pushed to GREEN/DEGRADED/BLOCKED),
and the exported JSON report shape matches what SPEC's public coverage
matrix requires (jurisdiction, session, bill count, full-text %, status).
"""
from __future__ import annotations

import uuid

from billcommons_ingest.coverage import (
    _next_status_for_counts,
    build_coverage_report,
    recompute_all_coverage,
    write_coverage_report,
)
from billcommons_schema.models import (
    Bill,
    BillDocument,
    BillVersion,
    Jurisdiction,
    JurisdictionCoverage,
    Session as SessionModel,
)


def _make_jurisdiction_with_session(db_session, abbr=None):
    if abbr is None:
        abbr = f"ZQ_COV_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="Coverage Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db_session.add(session_row)
    db_session.flush()
    return jurisdiction, session_row


def test_next_status_advances_on_bill_count():
    assert _next_status_for_counts("SOURCE_IDENTIFIED", 0, 0) == "SOURCE_IDENTIFIED"
    assert _next_status_for_counts("SOURCE_IDENTIFIED", 5, 0) == "METADATA_SEARCHABLE"
    assert _next_status_for_counts("SOURCE_IDENTIFIED", 5, 2) == "FULL_TEXT_SEARCHABLE"


def test_next_status_never_regresses_terminal_states():
    # A jurisdiction already marked GREEN/BLOCKED by a validation run must
    # never be silently demoted just because a recompute pass ran with a
    # transient zero count.
    assert _next_status_for_counts("GREEN", 0, 0) == "GREEN"
    assert _next_status_for_counts("BLOCKED", 5, 5) == "BLOCKED"
    assert _next_status_for_counts("VALIDATING", 5, 0) == "VALIDATING"


def test_recompute_coverage_row_updates_bill_count(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_session(db_session)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id, session_id=session_row.id, status="SOURCE_IDENTIFIED"
    )
    db_session.add(coverage)
    db_session.flush()

    for i in range(3):
        db_session.add(
            Bill(
                jurisdiction_id=jurisdiction.id,
                session_id=session_row.id,
                identifier=f"HB {i}",
                identifier_norm=f"HB {i}",
                title=f"Bill {i}",
            )
        )
    db_session.flush()

    recompute_all_coverage(db_session)
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.bill_count == 3
    assert coverage.status == "METADATA_SEARCHABLE"
    assert coverage.last_success_at is not None


def test_build_coverage_report_shape(db_session):
    abbr = f"ZQ_REPORT_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction, session_row = _make_jurisdiction_with_session(db_session, abbr=abbr)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id, session_id=session_row.id, status="SOURCE_IDENTIFIED"
    )
    db_session.add(coverage)
    db_session.flush()

    report = build_coverage_report(db_session)
    assert "generated_at" in report
    assert "jurisdiction_count" in report
    assert "rows" in report

    row = next(r for r in report["rows"] if r["jurisdiction"] == abbr)
    assert row["jurisdiction_name"] == "Coverage Test State"
    assert row["session"] == "2026 Session"
    assert row["status"] == "SOURCE_IDENTIFIED"
    assert row["bill_count"] == 0
    assert row["full_text_pct"] == 0.0


def test_periodic_recompute_advances_metadata_searchable_to_full_text_searchable(db_session):
    """Business intent: the periodic recompute pass in cmd_worker exists
    specifically to catch a jurisdiction's full_text_count going stale as
    the fulltext crawl lands extracted_text over time -- if this regresses,
    a jurisdiction with real full text sitting in bill_documents would never
    advance past METADATA_SEARCHABLE on its own (the exact bug this whole
    feature fixes)."""
    jurisdiction, session_row = _make_jurisdiction_with_session(db_session)
    coverage = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id, session_id=session_row.id, status="METADATA_SEARCHABLE", bill_count=1
    )
    db_session.add(coverage)

    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="Bill 1",
    )
    db_session.add(bill)
    db_session.flush()

    version = BillVersion(bill_id=bill.id)
    db_session.add(version)
    db_session.flush()

    document = BillDocument(bill_version_id=version.id, extracted_text="the full text landed")
    db_session.add(document)
    db_session.flush()

    recompute_all_coverage(db_session)
    db_session.flush()
    db_session.refresh(coverage)

    assert coverage.full_text_count == 1
    assert coverage.status == "FULL_TEXT_SEARCHABLE"


def test_periodic_recompute_does_not_touch_green_or_degraded_rows(db_session):
    """Business intent: recompute is a cheap, count-based pass that runs
    every 20 minutes unattended -- it must never silently demote a row a
    validation run already promoted to GREEN or demoted to DEGRADED, even
    though the row it's re-scanning has zero bills/documents attached in
    this test (a transient-looking zero count must not regress a terminal
    status)."""
    jurisdiction, session_row = _make_jurisdiction_with_session(db_session)
    green = JurisdictionCoverage(
        jurisdiction_id=jurisdiction.id, session_id=session_row.id, status="GREEN", bill_count=5, full_text_count=5
    )
    db_session.add(green)

    jurisdiction2, session_row2 = _make_jurisdiction_with_session(db_session)
    degraded = JurisdictionCoverage(
        jurisdiction_id=jurisdiction2.id, session_id=session_row2.id, status="DEGRADED", bill_count=5
    )
    db_session.add(degraded)
    db_session.flush()

    recompute_all_coverage(db_session)
    db_session.flush()
    db_session.refresh(green)
    db_session.refresh(degraded)

    assert green.status == "GREEN"
    assert degraded.status == "DEGRADED"


def test_write_coverage_report_writes_valid_json(tmp_path):
    import json

    report = {"generated_at": "2026-07-23T00:00:00Z", "jurisdiction_count": 1, "rows": []}
    output_path = tmp_path / "nested" / "coverage-latest.json"
    write_coverage_report(report, output_path)

    assert output_path.exists()
    loaded = json.loads(output_path.read_text())
    assert loaded == report
