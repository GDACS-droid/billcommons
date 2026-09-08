"""PostgreSQL coverage-selection checks for the data-health report.

These use the ordinary ingestion `db_session` rollback fixture.  They prove
the report against the actual JSON, DISTINCT ON, and partial-null coverage
semantics rather than a mocked query result.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from billcommons_schema.models import (
    Bill,
    IngestJob,
    IngestionRun,
    Jurisdiction,
    JurisdictionCoverage,
    Session as SessionModel,
)
from billcommons_shared.data_health import collect_report


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _seed_current_session(db_session, abbreviation: str):
    jurisdiction = Jurisdiction(
        name="Data health test jurisdiction", abbreviation=abbreviation, classification="state"
    )
    db_session.add(jurisdiction)
    db_session.flush()
    session = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier="current-2026",
        active=True,
        start_date=date(2026, 1, 1),
        end_date=NOW.date(),
    )
    db_session.add(session)
    db_session.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="Data health test bill",
        source_name="adapter",
        source_url="https://example.invalid/source",
        parser_version="adapter/1",
        retrieved_at=NOW,
    )
    db_session.add(bill)
    db_session.flush()
    return jurisdiction, session


def _report_row(report, abbreviation):
    return next(row for row in report["jurisdictions"] if row["jurisdiction"] == abbreviation)


def test_session_coverage_and_source_specific_sync_health_use_real_postgres_rows(
    db_session,
):
    jurisdiction, session = _seed_current_session(db_session, "AL")
    db_session.add(
        JurisdictionCoverage(
            jurisdiction_id=jurisdiction.id,
            session_id=session.id,
            status="GREEN",
            bill_count=1,
            full_text_count=1,
        )
    )
    # A newer repair is deliberately unrelated to the scheduled API source.
    db_session.add_all(
        [
            IngestionRun(
                jurisdiction_id=jurisdiction.id,
                source_name="openstates_api_sync",
                status="success",
                started_at=None,
                finished_at=None,
            ),
            IngestionRun(
                jurisdiction_id=jurisdiction.id,
                source_name="openstates_api_sync",
                status="success",
                started_at=NOW - timedelta(minutes=6),
                finished_at=NOW - timedelta(minutes=5),
            ),
            IngestionRun(
                jurisdiction_id=jurisdiction.id,
                source_name="repair",
                status="success",
                started_at=NOW - timedelta(minutes=2),
                finished_at=NOW - timedelta(minutes=1),
            ),
            IngestJob(
                kind="api_sync",
                payload={"state": jurisdiction.abbreviation},
                status="queued",
                attempts=1,
                run_after=NOW - timedelta(hours=3),
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW - timedelta(hours=3),
            ),
            IngestJob(
                kind="api_sync",
                payload={"state": jurisdiction.abbreviation},
                status="running",
                attempts=1,
                run_after=NOW - timedelta(hours=3),
                locked_at=None,
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW - timedelta(hours=3),
            ),
        ]
    )
    db_session.flush()

    report = collect_report(db_session, now=NOW)
    row = _report_row(report, jurisdiction.abbreviation)
    codes = [defect["code"] for defect in report["defects"] if defect["jurisdiction"] == jurisdiction.abbreviation]

    assert row["coverage"]["scope"] == "session"
    assert row["coverage"]["session_identifier"] == session.identifier
    assert row["local_ingestion"]["last_run"]["source_name"] == "repair"
    assert row["local_ingestion"]["last_successful_api_sync"]["source_name"] == "openstates_api_sync"
    assert row["source_health"]["oldest_queued_api_sync_at"] is not None
    assert row["source_health"]["oldest_running_api_sync_at"] is not None
    assert "MISSING_JURISDICTION_COVERAGE" not in codes
    assert "API_SYNC_QUEUED_STALLED_SUSPECTED" in codes
    assert "API_SYNC_RUNNING_STALLED_SUSPECTED" in codes


def test_session_coverage_is_current_view_while_degraded_aggregate_is_a_single_scoped_signal(
    db_session,
):
    jurisdiction, session = _seed_current_session(db_session, "AK")
    db_session.add_all(
        [
            JurisdictionCoverage(
                jurisdiction_id=jurisdiction.id,
                status="DEGRADED",
                bill_count=5,
                full_text_count=2,
            ),
            JurisdictionCoverage(
                jurisdiction_id=jurisdiction.id,
                session_id=session.id,
                status="GREEN",
                bill_count=1,
                full_text_count=1,
            ),
        ]
    )
    db_session.flush()

    report = collect_report(db_session, now=NOW)
    row = _report_row(report, jurisdiction.abbreviation)
    coverage_codes = [
        defect["code"] for defect in report["defects"] if defect["jurisdiction"] == jurisdiction.abbreviation
    ]

    assert row["coverage"]["scope"] == "session"
    assert row["coverage"]["status"] == "GREEN"
    assert row["additional_jurisdiction_coverage_signal"]["scope"] == "jurisdiction"
    assert row["additional_jurisdiction_coverage_signal"]["status"] == "DEGRADED"
    assert "COVERAGE_DEGRADED" not in coverage_codes
    assert coverage_codes.count("JURISDICTION_COVERAGE_DEGRADED") == 1


def test_public_report_excludes_stray_jurisdictions_and_flags_missing_session_refresh(
    db_session,
):
    public = Jurisdiction(name="Configured public jurisdiction", abbreviation="AZ", classification="state")
    case_variant = Jurisdiction(name="Lowercase case variant", abbreviation="ca", classification="state")
    stray = Jurisdiction(name="Unsupported fixture", abbreviation="ZZ_DATA_HEALTH", classification="state")
    db_session.add_all((public, case_variant, stray))
    db_session.flush()

    report = collect_report(db_session, now=NOW)

    assert len(report["jurisdictions"]) == 51
    assert {row["jurisdiction"] for row in report["jurisdictions"]} == {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
        "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
        "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
        "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    }
    assert {"ZZ_DATA_HEALTH", "ca"}.isdisjoint(
        {row["jurisdiction"] for row in report["jurisdictions"]}
    )
    by_jurisdiction = {
        code: [defect["code"] for defect in report["defects"] if defect["jurisdiction"] == code]
        for code in ("AZ", "CA", "AL")
    }
    assert by_jurisdiction["AZ"] == ["MISSING_REFRESH_CONFIGURATION"]
    assert by_jurisdiction["CA"] == ["MISSING_JURISDICTION"]
    assert by_jurisdiction["AL"] == ["MISSING_JURISDICTION"]


def test_configured_session_without_corpus_or_coverage_is_an_error(db_session):
    jurisdiction = Jurisdiction(name="Empty local corpus", abbreviation="CA", classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    db_session.add(
        SessionModel(
            jurisdiction_id=jurisdiction.id,
            identifier="current-2026",
            active=True,
            start_date=date(2026, 1, 1),
            end_date=NOW.date(),
        )
    )
    db_session.flush()

    report = collect_report(db_session, now=NOW)
    defects = [
        (defect["severity"], defect["code"])
        for defect in report["defects"]
        if defect["jurisdiction"] == "CA"
    ]

    assert defects == [
        ("error", "EMPTY_CORPUS"),
        ("error", "MISSING_JURISDICTION_COVERAGE"),
    ]


def test_unknown_api_sync_time_and_future_queued_job_are_not_silently_healthy(db_session):
    jurisdiction, session = _seed_current_session(db_session, "CO")
    db_session.add(
        JurisdictionCoverage(
            jurisdiction_id=jurisdiction.id,
            session_id=session.id,
            status="GREEN",
            bill_count=1,
            full_text_count=1,
        )
    )
    db_session.add_all(
        [
            IngestionRun(
                jurisdiction_id=jurisdiction.id,
                source_name="openstates_api_sync",
                status="success",
                started_at=None,
                finished_at=None,
            ),
            IngestJob(
                kind="api_sync",
                payload={"state": "co"},
                status="queued",
                attempts=1,
                created_at=NOW - timedelta(hours=3),
                updated_at=NOW - timedelta(hours=3),
                run_after=NOW + timedelta(hours=3),
            ),
        ]
    )
    db_session.flush()

    report = collect_report(db_session, now=NOW)
    row = _report_row(report, "CO")
    codes = [defect["code"] for defect in report["defects"] if defect["jurisdiction"] == "CO"]

    assert row["source_health"]["queued_api_sync_jobs"] == 1
    assert row["source_health"]["oldest_queued_api_sync_at"] is None
    assert codes == ["UNKNOWN_SYNC_TIME"]


def test_selected_session_query_ignores_historical_coverage_rows(db_session):
    jurisdiction, current = _seed_current_session(db_session, "CT")
    historical = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier="historical-2025",
        active=False,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 9, 1),
    )
    db_session.add(historical)
    db_session.flush()
    db_session.add_all(
        [
            JurisdictionCoverage(
                jurisdiction_id=jurisdiction.id,
                session_id=current.id,
                status="GREEN",
                bill_count=1,
                full_text_count=1,
            ),
            JurisdictionCoverage(
                jurisdiction_id=jurisdiction.id,
                session_id=historical.id,
                status="BLOCKED",
                bill_count=1,
                full_text_count=0,
            ),
        ]
    )
    db_session.flush()

    report = collect_report(db_session, now=NOW)
    row = _report_row(report, "CT")
    codes = [defect["code"] for defect in report["defects"] if defect["jurisdiction"] == "CT"]

    assert row["coverage"]["session_identifier"] == current.identifier
    assert row["additional_jurisdiction_coverage_signal"] is None
    assert "COVERAGE_BLOCKED" not in codes


@pytest.mark.parametrize('status,expected', [
    ('success', 'UNKNOWN_SYNC_TIME'), ('failed', 'LATEST_API_SYNC_FAILED'),
])
def test_newer_undated_run_is_not_hidden_by_dated_history(db_session, status, expected):
    jurisdiction, _ = _seed_current_session(db_session, 'CO')
    db_session.add_all([
        IngestionRun(jurisdiction_id=jurisdiction.id, source_name='openstates_api_sync',
            status='success', started_at=NOW - timedelta(minutes=10),
            finished_at=NOW - timedelta(minutes=5), created_at=NOW - timedelta(minutes=10)),
        IngestionRun(jurisdiction_id=jurisdiction.id, source_name='openstates_api_sync',
            status=status, started_at=None, finished_at=None, created_at=NOW),
    ])
    db_session.flush()
    report = collect_report(db_session, now=NOW)
    row = _report_row(report, 'CO')
    assert row['local_ingestion']['last_api_sync']['status'] == status
    assert row['local_ingestion']['last_api_sync']['finished_at'] is None
    assert expected in {d['code'] for d in report['defects'] if d['jurisdiction'] == 'CO'}
