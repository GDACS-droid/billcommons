"""Pure contract tests for the public data-reliability report.

The report's DB queries receive separate disposable-Postgres coverage in the
CLI smoke command.  These tests keep the safety-critical interpretation and
severity rules deterministic without opening any configured database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from billcommons_shared.official_source_health import OfficialTargetHealth

from billcommons_shared.data_health import (
    BillEvidence,
    CoverageEvidence,
    JurisdictionEvidence,
    RunEvidence,
    SnapshotBlockerEvidence,
    build_report,
    exit_code,
)


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def test_official_source_failures_join_ledger_with_bounded_evidence():
    targets = tuple(OfficialTargetHealth(
        target_id=f"target-{index}", observation_id=f"observation-{index}",
        state="failed", retrieved_at=NOW, upstream_updated_at=NOW - timedelta(days=2),
        next_check_at=NOW + timedelta(hours=1),
    ) for index in range(8))
    report = build_report([_evidence(official_targets=targets)], now=NOW)
    source_health = report["jurisdictions"][0]["source_health"]["official_sources"]
    assert source_health["target_count"] == 8
    assert source_health["targets_by_state"]["failed"] == 8
    assert len(source_health["samples"]) == 5 and source_health["samples_truncated"]
    defect = next(d for d in report["defects"] if d["code"] == "OFFICIAL_SOURCE_OBSERVATION_FAILED")
    assert defect["evidence"]["target_count"] == 8
    assert len(defect["evidence"]["target_ids"]) == 5
    assert report["honesty"]["official_freshness"] == "unverified"
    assert report["jurisdictions"][0]["official_reconciliation"]["state"] == "unavailable"


def test_observed_and_disabled_targets_do_not_create_operational_failure():
    targets = tuple(OfficialTargetHealth(
        target_id=state, observation_id=state, state=state,
        retrieved_at=NOW, upstream_updated_at=None, next_check_at=NOW,
    ) for state in ("observed", "disabled"))
    report = build_report([_evidence(official_targets=targets)], now=NOW)
    assert not any(d["code"].startswith("OFFICIAL_SOURCE_") for d in report["defects"])
    assert report["honesty"]["official_freshness"] == "unverified"


def test_future_official_observation_fails_error_gate():
    target = OfficialTargetHealth(
        target_id="target", observation_id="observation", state="future_observation",
        retrieved_at=NOW + timedelta(minutes=6), upstream_updated_at=None, next_check_at=NOW,
    )
    report = build_report([_evidence(official_targets=(target,))], now=NOW)
    defect = next(d for d in report["defects"] if d["code"] == "OFFICIAL_SOURCE_FUTURE_OBSERVATION")
    assert defect["severity"] == "error"
    assert exit_code(report, "error") == 1


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


def test_snapshot_blocker_remains_visible_despite_a_newer_success():
    sample = SnapshotBlockerEvidence(
        blocker_id="00000000-0000-0000-0000-000000000001",
        bill_id=None, component="actions", record_cap=1000,
        first_seen_at=NOW - timedelta(days=1), last_seen_at=NOW,
    )
    report = build_report([_evidence(
        active_snapshot_blockers=1,
        snapshot_blockers_without_local_bill=1,
        snapshot_blocker_samples=(sample,),
    )], now=NOW)

    defect, = report["defects"]
    assert defect["code"] == "API_SYNC_SNAPSHOT_BLOCKED"
    assert defect["severity"] == "error"
    assert exit_code(report, "error") == 1
    public = report["jurisdictions"][0]["source_health"]["snapshot_blockers"]
    assert defect["evidence"] == public
    assert public["active_count"] == public["without_local_bill_count"] == 1
    assert public["samples"][0]["bill_id"] is None
    assert public["samples_truncated"] is False
    assert report["honesty"]["official_freshness"] == "unverified"


def test_snapshot_blocker_rendering_bounds_samples_and_preserves_total():
    sample = SnapshotBlockerEvidence(
        blocker_id="00000000-0000-0000-0000-000000000001",
        bill_id=None, component="actions", record_cap=1000,
        first_seen_at=NOW, last_seen_at=NOW,
    )
    report = build_report([_evidence(
        active_snapshot_blockers=9, snapshot_blocker_samples=(sample,) * 9,
    )], now=NOW)
    public = report["jurisdictions"][0]["source_health"]["snapshot_blockers"]
    assert public["active_count"] == 9
    assert len(public["samples"]) == public["sample_limit"] == 5
    assert public["samples_truncated"] is True


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
        bills=BillEvidence(
            bill_count=10, missing_parser_version=3, missing_source_url=2
        ),
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


def test_missing_session_cadence_is_an_explicit_operational_defect():
    report = build_report([_evidence(cadence_tier=None, cadence_minutes=None)], now=NOW)

    assert [row["code"] for row in report["defects"]] == ["MISSING_REFRESH_CONFIGURATION"]
    assert report["jurisdictions"][0]["refresh_target"] == {
        "cadence_tier": None,
        "target_minutes": None,
    }


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
    assert defect["severity"] == "error"
    assert defect["code"] == "API_SYNC_QUEUED_STALLED_SUSPECTED"
    assert defect["evidence"]["action"] == "inspect; this report performs no automatic recovery"
    assert exit_code(report, "error") == 1


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


def test_missing_canonical_jurisdiction_is_an_error_instead_of_being_dropped():
    report = build_report([_evidence(exists=False, abbreviation="CA")], now=NOW)

    assert report["summary"]["jurisdiction_count"] == 1
    assert report["defects"] == [
        {
            "severity": "error",
            "code": "MISSING_JURISDICTION",
            "jurisdiction": "CA",
            "message": "This canonical jurisdiction has no local jurisdiction row.",
            "evidence": {"jurisdiction": "CA"},
        }
    ]
    assert exit_code(report, "error") == 1


def test_empty_local_corpus_is_an_error_without_claiming_source_freshness():
    empty_corpus = build_report([_evidence(bills=BillEvidence())], now=NOW)

    assert [row["code"] for row in empty_corpus["defects"]] == ["EMPTY_CORPUS"]
    assert empty_corpus["honesty"]["official_freshness"] == "unverified"
    assert exit_code(empty_corpus, "error") == 1


def test_future_successful_api_sync_is_an_error_after_small_clock_skew_tolerance():
    future_sync = RunEvidence(
        "openstates_api_sync", "success", NOW + timedelta(minutes=6), NOW + timedelta(minutes=6)
    )
    report = build_report(
        [_evidence(latest_api_sync_run=future_sync, latest_successful_api_sync=future_sync)], now=NOW
    )

    assert report["defects"] == [
        {
            "severity": "error",
            "code": "FUTURE_SUCCESSFUL_API_SYNC_TIME",
            "jurisdiction": "AA",
            "message": "The last successful local API sync timestamp is materially in the future.",
            "evidence": {
                "last_successful_local_api_sync_at": "2026-09-08T12:06:00+00:00",
                "ahead_minutes": 6.0,
                "allowed_clock_skew_minutes": 5,
                "cadence_tier": "active",
            },
        }
    ]
    assert exit_code(report, "error") == 1


def test_successful_api_sync_within_clock_skew_tolerance_does_not_create_a_defect():
    tolerated_sync = RunEvidence(
        "openstates_api_sync", "success", NOW + timedelta(minutes=5), NOW + timedelta(minutes=5)
    )
    report = build_report(
        [_evidence(latest_api_sync_run=tolerated_sync, latest_successful_api_sync=tolerated_sync)], now=NOW
    )

    assert report["defects"] == []


def test_successful_api_sync_without_any_timestamp_is_an_error_not_freshness():
    unknown_time = RunEvidence("openstates_api_sync", "success", None, None)
    report = build_report(
        [_evidence(latest_api_sync_run=unknown_time, latest_successful_api_sync=unknown_time)], now=NOW
    )

    assert [row["code"] for row in report["defects"]] == ["UNKNOWN_SYNC_TIME"]
    assert exit_code(report, "error") == 1
