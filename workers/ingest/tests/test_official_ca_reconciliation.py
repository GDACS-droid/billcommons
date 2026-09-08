from __future__ import annotations

import pytest

from billcommons_ingest.official_ca_reconciliation import (
    COMPARATOR_VERSION,
    reconcile_ca_action_content,
)
from billcommons_shared.reconciliation import MAX_EVENTS_PER_SIDE, ReconciliationInputError


def event(*, history_id="old", sequence="1", date="2026-09-01", description="Read  first time.", **extra):
    return {
        "jurisdiction": "CA", "session": "2025-2026 Regular Session", "bill_id": "202520260AB12",
        "date": date, "date_precision": "day" if date else "unknown", "description": description,
        "occurrence_id": f"ca-history:{history_id}", "source_identity": f"ca-history:{history_id}",
        "action_sequence": sequence, **extra,
    }


def test_history_ids_and_sequences_are_evidence_not_content_identity():
    report = reconcile_ca_action_content(
        [event(history_id="old-1", sequence="7")], [event(history_id="new-9", sequence="2", description=" read first\t time. ")]
    )

    assert report["comparator_version"] == COMPARATOR_VERSION
    assert report["summary"] == {"official_records": 1, "local_records": 1, "content_agreement": 1,
                                 "official_only_content": 0, "local_only_content": 0,
                                 "ambiguous_insufficient_evidence": 0}
    item = report["content_agreement"][0]
    assert item["agreement"] == "unique_content_agreement"
    assert item["occurrence_proof"] is False
    assert item["official_evidence"][0]["original_evidence"]["occurrence_id"] == "ca-history:old-1"
    assert item["local_evidence"][0]["original_evidence"]["action_sequence"] == "2"


def test_repeated_identical_content_is_counted_not_paired_as_occurrences():
    official = [event(history_id="a"), event(history_id="b")]
    local = [event(history_id="x"), event(history_id="y")]
    report = reconcile_ca_action_content(official, local)

    item = report["content_agreement"][0]
    assert item["agreement"] == "repeated_content_multiset_agreement"
    assert item["official_count"] == item["local_count"] == 2
    assert item["occurrence_proof"] is False


def test_unequal_multiplicity_is_content_only_evidence_without_a_proposed_change():
    report = reconcile_ca_action_content([event(history_id="a"), event(history_id="b")], [event(history_id="x")])

    assert report["content_agreement"] == []
    assert report["summary"]["official_only_content"] == 1
    item = report["official_only_content"][0]
    assert item["official_count"] == 2 and item["local_count"] == 1 and item["unmatched_count"] == 1


@pytest.mark.parametrize("event_data, reason", [
    (event(date=None), "missing_exact_day"),
    (event(description=""), "missing_description"),
    (event(date="2026-09"), "missing_exact_day"),
])
def test_unknown_or_imprecise_content_evidence_stays_ambiguous(event_data, reason):
    report = reconcile_ca_action_content([event_data], [])

    assert report["summary"]["ambiguous_insufficient_evidence"] == 1
    assert report["ambiguous_insufficient_evidence"][0]["reason"] == reason


@pytest.mark.parametrize("field, value", [("session", "Other Session"), ("bill_id", "202520260AB13")])
def test_conflicting_scope_is_rejected(field, value):
    altered = event(history_id="other")
    altered[field] = value
    with pytest.raises(ReconciliationInputError, match="mixed jurisdiction, session, or bill scope"):
        reconcile_ca_action_content([event()], [altered])


def test_non_ca_jurisdiction_is_rejected():
    altered = event()
    altered["jurisdiction"] = "NY"
    with pytest.raises(ReconciliationInputError, match="requires jurisdiction 'CA'"):
        reconcile_ca_action_content([altered], [])


def test_missing_scope_and_too_many_events_fail_closed():
    missing = event()
    missing.pop("session")
    with pytest.raises(ReconciliationInputError, match="required for scope"):
        reconcile_ca_action_content([missing], [])
    with pytest.raises(ReconciliationInputError, match="event safety cap"):
        reconcile_ca_action_content([event() for _ in range(MAX_EVENTS_PER_SIDE + 1)], [])


def test_empty_histories_require_explicit_consistent_bill_scope():
    scope = {k: event()[k] for k in ('jurisdiction', 'session', 'bill_id')}
    report = reconcile_ca_action_content([], [], scope=scope)
    assert report['summary']['official_records'] == report['summary']['local_records'] == 0
    with pytest.raises(ReconciliationInputError, match='scope evidence'):
        reconcile_ca_action_content([], [])
    with pytest.raises(ReconciliationInputError, match='mixed jurisdiction'):
        reconcile_ca_action_content([event()], [], scope={**scope, 'bill_id': '202520260AB13'})


def test_report_is_deterministic_under_input_permutations():
    official = [event(history_id='a'), event(history_id='b', date='2026-09-02'), event(history_id='c', date=None)]
    local = [event(history_id='d'), event(history_id='e', date='2026-09-03')]
    assert reconcile_ca_action_content(official, local) == reconcile_ca_action_content(official[::-1], local[::-1])


def test_record_and_output_size_caps_fail_closed(monkeypatch):
    from billcommons_ingest import official_ca_reconciliation as comparator
    with pytest.raises(ReconciliationInputError, match='evidence safety cap'):
        reconcile_ca_action_content([event(extra='x' * comparator.MAX_EVENT_BYTES)], [])
    monkeypatch.setattr(comparator, 'MAX_REPORT_BYTES', 100)
    with pytest.raises(ReconciliationInputError, match='report exceeds'):
        reconcile_ca_action_content([event()], [event()])
