from __future__ import annotations

import json

import pytest

from billcommons_shared.reconciliation import (
    MAX_EVENTS_PER_SIDE,
    MAX_EVENT_BYTES,
    ReconciliationInputError,
    main,
    reconcile_events,
)


@pytest.mark.parametrize("precision", [[], {}, 5, False])
def test_invalid_precision_type_is_controlled(precision):
    with pytest.raises(ReconciliationInputError):
        reconcile_events([event("id", date_precision=precision)], [])


def test_non_finite_fixture_json_is_rejected(tmp_path, capsys):
    official = tmp_path / "official.json"
    local = tmp_path / "local.json"
    official.write_text('[{"occurrence_id":"id","value":NaN}]')
    local.write_text('[]')
    assert main(["--official", str(official), "--local", str(local)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_reconciliation_input"


def event(occurrence_id: str | None, **overrides):
    value = {
        "occurrence_id": occurrence_id,
        "event_type": "committee_referral",
        "description": "Referred to the Committee on Rules",
        "date": "2026-01-15",
        "chamber": "lower",
        "stage": "committee",
        "source_url": "https://legislature.example/bill/17/actions",
        "raw_capture": {"source": "recorded fixture", "ordinal": overrides.pop("ordinal", 0)},
    }
    value.update(overrides)
    return value


def test_reconciliation_is_order_independent_and_preserves_distinct_duplicate_occurrences():
    first = event("official:action:100", ordinal=1)
    second = event("official:action:101", ordinal=2)
    report = reconcile_events({"events": [first, second]}, {"events": [second, first]})

    assert report["summary"] == {
        "official_records": 2,
        "local_records": 2,
        "matched": 2,
        "missing_from_local": 0,
        "local_only_not_deletion": 0,
        "mismatched_evidence": 0,
        "uncertain_evidence": 0,
        "ambiguous_identities": 0,
    }
    assert [item["identity"]["value"] for item in report["matched"]] == [
        "official:action:100",
        "official:action:101",
    ]
    assert report["matched"][0]["official"]["raw_evidence"] == first


def test_source_identity_is_a_supported_explicit_identity_when_occurrence_id_is_unavailable():
    official = event(None, source_identity="ca-leginfo:AB-17:action:892")
    local = event(None, source_identity="ca-leginfo:AB-17:action:892")
    report = reconcile_events([official], [local])

    assert report["summary"]["matched"] == 1
    assert report["matched"][0]["identity"] == {
        "kind": "source_identity",
        "value": "ca-leginfo:AB-17:action:892",
    }


def test_explicit_identifiers_and_source_urls_remain_case_sensitive_evidence():
    official = event("official:Action:100", source_url="https://legislature.example/Actions")
    local = event("official:Action:100", source_url="https://legislature.example/actions")
    report = reconcile_events([official], [local])

    assert report["summary"]["mismatched_evidence"] == 1
    assert report["mismatched_evidence"][0]["differences"] == [
        {
            "field": "source_url",
            "official": "https://legislature.example/Actions",
            "local": "https://legislature.example/actions",
        }
    ]


def test_trailing_source_url_slash_is_preserved_as_evidence():
    official = event("official:Action:100", source_url="https://legislature.example/Actions/")
    local = event("official:Action:100", source_url="https://legislature.example/Actions")
    report = reconcile_events([official], [local])

    assert report["mismatched_evidence"][0]["differences"][0]["field"] == "source_url"


def test_duplicate_explicit_identity_is_ambiguous_not_paired_by_text_or_position():
    first = event("official:action:100", ordinal=1)
    second = event("official:action:100", ordinal=2)
    report = reconcile_events([first, second], [first])

    assert report["summary"]["ambiguous_identities"] == 1
    ambiguity = report["ambiguous_identities"][0]
    assert ambiguity["reason"] == "duplicate_explicit_identity"
    assert len(ambiguity["official"]) == 2
    assert ambiguity["local"][0]["raw_evidence"] == first
    assert report["summary"]["matched"] == 0


def test_missing_and_local_only_are_reported_without_a_deletion_instruction():
    official_only = event("official:action:100")
    local_only = event("official:action:200")
    report = reconcile_events([official_only], [local_only])

    assert report["missing_from_local"][0]["raw_evidence"] == official_only
    assert report["local_only_not_deletion"][0]["raw_evidence"] == local_only
    assert "delete" not in _canonical_report(report["local_only_not_deletion"]).casefold()


def test_explicit_identity_exposes_mismatched_evidence_but_month_precision_is_compatible_with_day():
    official = event("official:action:100", description="Referred to Rules", date="2026-01")
    local = event("official:action:100", description="Referred to Finance", date="2026-01-15")
    report = reconcile_events([official], [local])

    mismatch = report["mismatched_evidence"][0]
    assert mismatch["differences"] == [
        {"field": "description", "official": "referred to rules", "local": "referred to finance"}
    ]
    assert all(item["field"] != "date" for item in mismatch["differences"])


def test_missing_official_field_is_uncertain_not_a_proven_difference():
    official = event("official:action:100", description=None)
    local = event("official:action:100", description="Referred to Rules")
    report = reconcile_events([official], [local])

    assert report["summary"]["mismatched_evidence"] == 0
    assert report["summary"]["uncertain_evidence"] == 1
    assert report["uncertain_evidence"][0]["uncertain_fields"][0]["field"] == "description"


def test_missing_core_date_and_description_on_both_sides_are_uncertain_not_matched():
    official = event("official:action:100", description=None, date=None)
    local = event("official:action:100", description=None, date=None)
    report = reconcile_events([official], [local])

    assert report["summary"]["matched"] == 0
    assert report["summary"]["uncertain_evidence"] == 1
    assert {item["field"] for item in report["uncertain_evidence"][0]["uncertain_fields"]} == {
        "date",
        "description",
    }


def test_overlapping_imprecise_dates_are_uncertain_unless_value_and_precision_match():
    official = event("official:action:100", date="2026")
    local = event("official:action:100", date="2026-01-15")
    report = reconcile_events([official], [local])

    assert report["summary"]["matched"] == 0
    assert report["summary"]["uncertain_evidence"] == 1
    assert report["uncertain_evidence"][0]["uncertain_fields"] == [
        {
            "field": "date",
            "official": "2026",
            "official_precision": "year",
            "local": "2026-01-15",
            "local_precision": "day",
            "reason": "overlapping_imprecise_dates",
        }
    ]


def test_scope_fields_prevent_same_identifier_from_matching_a_different_record():
    official = event("shared-action", jurisdiction="CA", session="2025-2026", bill_id="AB 17")
    local = event("shared-action", jurisdiction="NV", session="2025-2026", bill_id="AB 17")
    report = reconcile_events([official], [local])

    assert report["summary"]["mismatched_evidence"] == 1
    assert report["mismatched_evidence"][0]["differences"] == [
        {"field": "jurisdiction", "official": "ca", "local": "nv"}
    ]


def test_missing_scope_on_one_side_is_uncertain_not_a_proven_match():
    official = event("shared-action", source_namespace="official-ca")
    local = event("shared-action")
    report = reconcile_events([official], [local])

    assert report["summary"]["matched"] == 0
    assert report["summary"]["uncertain_evidence"] == 1
    assert report["uncertain_evidence"][0]["uncertain_fields"][0]["field"] == "source_namespace"


def test_records_without_explicit_identity_are_not_merged_even_when_text_matches():
    official = event(None)
    local = event(None)
    report = reconcile_events([official], [local])

    assert report["summary"]["matched"] == 0
    assert report["summary"]["ambiguous_identities"] == 2
    assert {item["reason"] for item in report["ambiguous_identities"]} == {"missing_explicit_identity"}


@pytest.mark.parametrize(
    "fixture",
    [
        [{"occurrence_id": "x", "date": "2026-02-31"}],
        [{"occurrence_id": "x", "date": "0000"}],
        [{"occurrence_id": "x", "date": "not-a-date"}],
        [{"occurrence_id": 17}],
        {"events": "not an array"},
    ],
)
def test_malformed_fixture_fails_closed(fixture):
    with pytest.raises(ReconciliationInputError):
        reconcile_events(fixture, [])


def test_size_caps_fail_closed_before_a_fixture_can_make_reconciliation_unbounded():
    oversized_event = event("official:action:100", description="x" * MAX_EVENT_BYTES)
    with pytest.raises(ReconciliationInputError, match="evidence safety cap"):
        reconcile_events([oversized_event], [])

    with pytest.raises(ReconciliationInputError, match="event safety cap"):
        reconcile_events([event(f"official:action:{index}") for index in range(MAX_EVENTS_PER_SIDE + 1)], [])


def test_cli_reads_only_named_fixtures_and_returns_machine_readable_report(tmp_path, capsys):
    official_path = tmp_path / "official.json"
    local_path = tmp_path / "local.json"
    official_path.write_text(json.dumps([event("official:action:100")]), encoding="utf-8")
    local_path.write_text(json.dumps([event("official:action:100")]), encoding="utf-8")

    assert main(["--official", str(official_path), "--local", str(local_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["matched"] == 1


def _canonical_report(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
