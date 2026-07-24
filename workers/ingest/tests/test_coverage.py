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
from billcommons_schema.models import Bill, Jurisdiction, JurisdictionCoverage, Session as SessionModel


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


def test_write_coverage_report_writes_valid_json(tmp_path):
    import json

    report = {"generated_at": "2026-07-23T00:00:00Z", "jurisdiction_count": 1, "rows": []}
    output_path = tmp_path / "nested" / "coverage-latest.json"
    write_coverage_report(report, output_path)

    assert output_path.exists()
    loaded = json.loads(output_path.read_text())
    assert loaded == report
