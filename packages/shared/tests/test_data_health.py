"""Pure contract tests for the public data-reliability report.

The report's DB queries receive separate disposable-Postgres coverage in the
CLI smoke command.  These tests keep the safety-critical interpretation and
severity rules deterministic without opening any configured database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from billcommons_shared.data_health import (
    BillEvidence,
    CoverageEvidence,
    JurisdictionEvidence,
    RunEvidence,
    build_report,
    exit_code,
)


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _evidence(**overrides) -> JurisdictionEvidence:
    values = {
        "abbreviation": "AA",
        "name": "Example",
        "cadence_tier": "active",
        "cadence_minutes": 30,
        "bills": BillEvidence(bill_count=10),
        "latest_run": RunEvidence("adapter", "success", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4)),
        "latest_successful_run": RunEvidence(
            "adapter", "success", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4)
        ),
        "latest_api_sync_run": RunEvidence(
            "openstates_api_sync", "success", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4)
        ),
        "latest_successful_api_sync": RunEvidence(
            "openstates_api_sync", "success", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4)
        ),
        "coverage": CoverageEvidence("GREEN", 10, 8),
    }
    values.update(overrides)
    return JurisdictionEvidence(**values)


def test_local_success_is_explicitly_not_laundered_into_official_freshness():
    report = build_report([_evidence()], now=NOW)

    assert report["honesty"]["official_freshness"] == "unverified"
    assert report["jurisdictions"][0]["official_reconciliation"]["state"] == "unavailable"
    assert "not official-source freshness" in report["jurisdictions"][0]["local_ingestion"]["interpretation"]
    assert report["defects"] == []


def test_ledger_orders_operational_failures_by_severity_then_jurisdiction():
    no_success = _evidence(
        abbreviation="ZZ",
        latest_run=RunEvidence("adapter", "failed", NOW - timedelta(minutes=1), NOW),
        latest_successful_run=None,
        latest_api_sync_run=None,
        latest_successful_api_sync=None,
        coverage=CoverageEvidence("BLOCKED", 10, 0),
        dead_api_sync_jobs=2,
    )
    stale_provenance = _evidence(
        abbreviation="AA",
        latest_run=RunEvidence("adapter", "success", NOW - timedelta(hours=2), NOW - timedelta(hours=2)),
        latest_successful_run=RunEvidence("adapter", "success", NOW - timedelta(hours=2), NOW - timedelta(hours=2)),
        latest_api_sync_run=RunEvidence(
            "openstates_api_sync", "success", NOW - timedelta(hours=2), NOW - timedelta(hours=2)
        ),
        latest_successful_api_sync=RunEvidence(
            "openstates_api_sync", "success", NOW - timedelta(hours=2), NOW - timedelta(hours=2)
        ),
        bills=BillEvidence(bill_count=10, missing_parser_version=3, missing_source_url=2),
    )

    report = build_report([no_success, stale_provenance], now=NOW)
    ledger = [(row["severity"], row["jurisdiction"], row["code"]) for row in report["defects"]]

    assert ledger == [
        ("critical", "ZZ", "COVERAGE_BLOCKED"),
        ("critical", "ZZ", "LOCAL_DATA_WITHOUT_SUCCESSFUL_RUN"),
        ("error", "ZZ", "DEAD_API_SYNC_JOBS"),
        ("error", "ZZ", "LATEST_INGESTION_FAILED"),
        ("warning", "AA", "LOCAL_SYNC_OVERDUE"),
        ("warning", "AA", "MISSING_PARSER_PROVENANCE"),
        ("warning", "AA", "MISSING_SOURCE_PROVENANCE"),
        ("warning", "ZZ", "NO_SUCCESSFUL_API_SYNC"),
    ]
    assert exit_code(report, "critical") == 1
    assert exit_code(report, "error") == 1


def test_fail_on_only_fails_at_or_above_its_requested_threshold():
    report = build_report(
        [_evidence(bills=BillEvidence(bill_count=10, missing_parser_version=1))], now=NOW
    )

    assert exit_code(report, None) == 0
    assert exit_code(report, "critical") == 0
    assert exit_code(report, "error") == 0
    assert exit_code(report, "warning") == 1


def test_one_off_success_does_not_hide_an_overdue_incremental_sync():
    report = build_report(
        [
            _evidence(
                latest_successful_run=RunEvidence("repair", "success", NOW, NOW),
                latest_api_sync_run=RunEvidence(
                    "openstates_api_sync", "success", NOW - timedelta(hours=2), NOW - timedelta(hours=2)
                ),
                latest_successful_api_sync=RunEvidence(
                    "openstates_api_sync", "success", NOW - timedelta(hours=2), NOW - timedelta(hours=2)
                ),
            )
        ],
        now=NOW,
    )

    assert [row["code"] for row in report["defects"]] == ["LOCAL_SYNC_OVERDUE"]


def test_pending_sync_beyond_target_is_a_suspected_stall_with_no_recovery_action():
    report = build_report(
        [
            _evidence(
                queued_api_sync_jobs=1,
                oldest_queued_api_sync_at=NOW - timedelta(hours=3),
                latest_run=RunEvidence("repair", "success", NOW, NOW),
                latest_successful_run=RunEvidence("repair", "success", NOW, NOW),
            )
        ],
        now=NOW,
    )

    defect = report["defects"][0]
    assert defect["code"] == "API_SYNC_QUEUED_STALLED_SUSPECTED"
    assert defect["evidence"]["action"] == "inspect; this report performs no automatic recovery"


def test_api_sync_failure_is_reported_even_when_an_unrelated_run_succeeds_later():
    report = build_report(
        [
            _evidence(
                latest_run=RunEvidence("repair", "success", NOW, NOW),
                latest_successful_run=RunEvidence("repair", "success", NOW, NOW),
                latest_api_sync_run=RunEvidence(
                    "openstates_api_sync", "failed", NOW - timedelta(minutes=1), NOW
                ),
            )
        ],
        now=NOW,
    )

    assert [row["code"] for row in report["defects"]] == ["LATEST_API_SYNC_FAILED"]
