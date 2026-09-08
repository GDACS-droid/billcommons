from __future__ import annotations

import pytest

from billcommons_ingest.official_fl_senate_reconciliation import (
    COMPARATOR_VERSION,
    reconcile_fl_senate_action_content,
)
from billcommons_shared.reconciliation import ReconciliationInputError


SCOPE = {"jurisdiction": "FL", "session": "2025 Regular Session", "bill_id": "HB 7031"}


def event(*, date="2025-04-02", chamber="House", description="Read first time.", row=1, bullet=1, **extra):
    return {
        **SCOPE,
        "date": date,
        "date_precision": "day" if date else "unknown",
        "chamber": chamber,
        "description": description,
        "source_row_position": row,
        "source_bullet_position": bullet,
        **extra,
    }


def test_row_and_bullet_positions_are_evidence_not_content_identity():
    report = reconcile_fl_senate_action_content(
        [event(row=3, bullet=2)], [event(row=99, bullet=7, description=" read  first\t time. ")], scope=SCOPE,
    )

    assert report["comparator_version"] == COMPARATOR_VERSION
    assert report["summary"] == {
        "official_records": 1,
        "local_records": 1,
        "content_agreement": 1,
        "official_only_content": 0,
        "local_only_content": 0,
        "ambiguous_insufficient_evidence": 0,
    }
    item = report["content_agreement"][0]
    assert item["occurrence_proof"] is False
    assert item["official_evidence"][0]["original_evidence"]["source_row_position"] == 3
    assert item["local_evidence"][0]["original_evidence"]["source_row_position"] == 99


def test_repeated_action_content_is_compared_as_a_multiset_not_paired():
    report = reconcile_fl_senate_action_content([event(row=1), event(row=2)], [event(row=7), event(row=8)], scope=SCOPE)
    assert report["content_agreement"][0]["agreement"] == "repeated_content_multiset_agreement"
    assert report["content_agreement"][0]["official_count"] == report["content_agreement"][0]["local_count"] == 2


@pytest.mark.parametrize("field, value", [("session", "2026 Regular Session"), ("bill_id", "HB 7032")])
def test_mixed_exact_scope_is_rejected(field, value):
    altered = event()
    altered[field] = value
    with pytest.raises(ReconciliationInputError, match="mixed jurisdiction, session, or bill scope"):
        reconcile_fl_senate_action_content([event()], [altered], scope=SCOPE)


def test_incomplete_content_remains_ambiguous_and_never_claims_a_missing_action():
    report = reconcile_fl_senate_action_content([event(date=None)], [], scope=SCOPE)
    assert report["summary"]["ambiguous_insufficient_evidence"] == 1
    assert report["ambiguous_insufficient_evidence"][0]["reason"] == "missing_exact_day"
    assert "missing" not in report["interpretation"].casefold()


def test_same_day_and_text_in_different_chambers_are_distinct_content():
    report = reconcile_fl_senate_action_content(
        [event(chamber="House")], [event(chamber="Senate")], scope=SCOPE,
    )
    assert report["summary"]["content_agreement"] == 0
    assert report["summary"]["official_only_content"] == 1
    assert report["summary"]["local_only_content"] == 1


def test_unknown_chamber_stays_ambiguous():
    report = reconcile_fl_senate_action_content([event(chamber=None)], [], scope=SCOPE)
    assert report["ambiguous_insufficient_evidence"][0]["reason"] == "missing_chamber"
