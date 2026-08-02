"""Coverage warnings must fire on DEGRADED and can never be masked by a sibling.

Regression test for a silent failure of the honesty machinery itself: severity
was ranked by position in COVERAGE_STATES, where DEGRADED and BLOCKED are
appended AFTER GREEN as terminal fault states. `min(..., key=index)` therefore
scored them as more advanced than GREEN, so a wholly DEGRADED jurisdiction
(Massachusetts, live) produced no warning, and a BLOCKED session was hidden by
any non-BLOCKED sibling row.
"""
from dataclasses import dataclass

from billcommons_mcp.common import COVERAGE_STATES, coverage_severity, worst_status


@dataclass
class _Row:
    status: str


def test_degraded_is_less_trustworthy_than_green():
    assert coverage_severity("DEGRADED") < coverage_severity("GREEN")


def test_blocked_is_the_least_trustworthy_state():
    assert coverage_severity("BLOCKED") == min(
        coverage_severity(s) for s in COVERAGE_STATES
    )


def test_degraded_falls_below_the_metadata_threshold():
    """This is what makes the warning fire at all."""
    assert coverage_severity("DEGRADED") < coverage_severity("METADATA_SEARCHABLE")


def test_degraded_alone_is_the_worst_status():
    assert worst_status([_Row("DEGRADED")]) == "DEGRADED"


def test_green_sibling_cannot_mask_a_degraded_row():
    assert worst_status([_Row("GREEN"), _Row("DEGRADED")]) == "DEGRADED"


def test_green_sibling_cannot_mask_a_blocked_row():
    assert worst_status([_Row("GREEN"), _Row("BLOCKED")]) == "BLOCKED"


def test_blocked_outranks_degraded():
    assert worst_status([_Row("DEGRADED"), _Row("BLOCKED")]) == "BLOCKED"


def test_all_green_is_still_green():
    assert worst_status([_Row("GREEN"), _Row("GREEN")]) == "GREEN"


def test_early_lifecycle_states_still_rank_below_green():
    for s in ("NOT_STARTED", "SOURCE_IDENTIFIED", "BOOTSTRAPPED"):
        assert coverage_severity(s) < coverage_severity("GREEN")


def test_unknown_status_is_treated_as_untrusted():
    """A status we do not recognise must never be assumed safe."""
    assert coverage_severity("SOMETHING_NEW") == 0
    assert worst_status([_Row("GREEN"), _Row("SOMETHING_NEW")]) == "SOMETHING_NEW"
