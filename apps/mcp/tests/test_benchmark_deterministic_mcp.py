"""MCP-side deterministic benchmark gates (docs/quality/adversarial-benchmark.md).

Structural assertions on the tool layer, matching the numbered questions. Each
is a regression test for something that was live on 2026-08-02.
"""
import inspect

from billcommons_mcp import tools
from billcommons_mcp.common import coverage_severity, worst_status

SRC = inspect.getsource(tools)


class _Row:
    def __init__(self, status):
        self.status = status


# --- 4.1  coverage warning must fire where it was designed to ----------------

def test_q4_1_degraded_jurisdiction_is_not_ranked_above_green():
    """COVERAGE_STATES is lifecycle order; DEGRADED/BLOCKED are appended after
    GREEN. Ranking severity by that list silently disabled the warning."""
    assert coverage_severity("DEGRADED") < coverage_severity("GREEN")
    assert coverage_severity("DEGRADED") < coverage_severity("METADATA_SEARCHABLE")
    assert worst_status([_Row("GREEN"), _Row("DEGRADED")]) == "DEGRADED"


def test_q4_1_blocked_can_never_be_masked_by_a_sibling():
    assert worst_status([_Row("GREEN"), _Row("BLOCKED")]) == "BLOCKED"
    assert worst_status([_Row("DEGRADED"), _Row("BLOCKED")]) == "BLOCKED"


def test_q4_1_unknown_status_is_never_assumed_safe():
    assert coverage_severity("SOMETHING_NEW") == 0


# --- 1.2  search must not report ambiguity as an exact match -----------------

def test_q1_2_bill_number_path_is_ordered_and_limited():
    assert ".order_by(Bill.jurisdiction_id, Bill.session_id, Bill.id)" in SRC
    assert ".limit(limit_n + 1)" in SRC


def test_q1_2_multi_session_match_is_flagged_ambiguous():
    assert '"bill_number_ambiguous"' in SRC
    assert SRC.index('match_type = "bill_number_ambiguous"') < SRC.index(
        'match_type = "bill_number_exact"'
    )


def test_q6_3_truncation_is_explicit():
    assert '"results_truncated"' in SRC


# --- 6.1  derived conclusions are not official record ------------------------

def test_q6_1_evidence_packet_names_its_derived_fields():
    assert '"derived_fields": ["status", "status_date"]' in SRC
    assert "derived_note" in SRC


# --- 4.2  missing data is not the same as a negative finding -----------------

def test_q4_2_empty_hearings_are_not_labelled_official():
    assert "not collected -- Bill Commons has no hearing data" in SRC
    assert "absence_note" in SRC
    assert "It does NOT mean no" in SRC
