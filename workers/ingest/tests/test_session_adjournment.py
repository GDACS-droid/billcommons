"""A bill whose session adjourned without passing it is dead.

This is the most common way a bill actually ends, and it is the one ending that
leaves NO trace in the action record -- nothing is filed at sine die, the bill
just stops. Reading actions alone, `derive_status` reports the last thing that
happened ("passed one chamber") and a consumer reads that as momentum.

Measured before the fix: 54,547 bills -- 26% of the corpus -- sat at a live
status in a session that had already adjourned. A reviewer building a tracker
put three of them in an "ADVANCEMENT / higher priority for monitoring" bucket.
All three were dead.

The tests that matter most here are the ones asserting the rule does NOT fire.
Over-applying it would invent deaths, which is worse than the bug it fixes.
"""
from __future__ import annotations

from datetime import date

import pytest

from billcommons_ingest import status

TODAY = date(2026, 7, 27)
ADJOURNED = date(2026, 5, 15)
STILL_SITTING = date(2026, 12, 31)


@pytest.mark.parametrize("live", sorted(status.LIVE_STATUSES))
def test_live_statuses_die_when_the_session_has_ended(live):
    assert status.apply_session_outcome(live, ADJOURNED, TODAY) == status.DIED_ON_ADJOURNMENT


@pytest.mark.parametrize("live", sorted(status.LIVE_STATUSES))
def test_live_statuses_survive_while_the_session_sits(live):
    assert status.apply_session_outcome(live, STILL_SITTING, TODAY) == live


def test_an_unknown_stage_in_an_adjourned_session_is_still_dead():
    """Null status means we could not tell what stage it reached -- not that
    the bill might still pass. The session closed without it becoming law."""
    assert status.apply_session_outcome(None, ADJOURNED, TODAY) == status.DIED_ON_ADJOURNMENT


def test_enrolled_survives_adjournment():
    """The single most important negative case. A bill on the governor's desk
    outlives sine die by design -- HI SB 2135's session adjourned 2026-05-08
    and it was signed into law on 2026-07-07, two months later. Calling that
    dead is the same error as calling a dead bill alive, pointed the other
    way."""
    assert status.apply_session_outcome(status.ENROLLED, date(2026, 5, 8), TODAY) == status.ENROLLED


@pytest.mark.parametrize(
    "terminal", [status.ENACTED, status.VETOED, status.DEAD, status.WITHDRAWN]
)
def test_an_outcome_already_reached_is_never_overwritten(terminal):
    """Adjournment explains bills that never concluded. One that DID conclude
    keeps its real ending -- 'died in committee' is more informative than
    'died on adjournment' and must not be flattened into it."""
    assert status.apply_session_outcome(terminal, ADJOURNED, TODAY) == terminal


@pytest.mark.parametrize("live", sorted(status.LIVE_STATUSES))
def test_an_unknown_end_date_changes_nothing(live):
    """Load-bearing, not laziness. The sessions with no recorded end date are
    overwhelmingly two-year carryover biennia (NY, NJ, IL, MN, WI, DC) where a
    bill pending at the end of year one genuinely rolls into year two.
    Defaulting to 'dead' there would invent deaths across six states."""
    assert status.apply_session_outcome(live, None, TODAY) == live


def test_the_session_end_day_itself_is_not_yet_over():
    """Sine die day is still a legislative day; bills pass on it."""
    assert status.apply_session_outcome(status.IN_COMMITTEE, TODAY, TODAY) == status.IN_COMMITTEE
    assert (
        status.apply_session_outcome(status.IN_COMMITTEE, date(2026, 7, 26), TODAY)
        == status.DIED_ON_ADJOURNMENT
    )


def test_died_on_adjournment_is_terminal_and_in_the_vocabulary():
    assert status.DIED_ON_ADJOURNMENT in status.ALL_STATUSES
    assert status.DIED_ON_ADJOURNMENT in status.TERMINAL_STATUSES
    # It must never be derivable from an action -- it is a fact about the
    # calendar, and letting prose produce it would make it unfalsifiable.
    assert status.DIED_ON_ADJOURNMENT not in status._CLASSIFICATION_STATUS.values()
    assert status.DIED_ON_ADJOURNMENT not in [s for _, s in status._TEXT_PATTERNS]


def test_it_is_idempotent():
    """The sweep re-runs every cycle over an overlapping set; a second pass
    must be a no-op rather than churning updated_at and re-announcing."""
    once = status.apply_session_outcome(status.IN_COMMITTEE, ADJOURNED, TODAY)
    assert status.apply_session_outcome(once, ADJOURNED, TODAY) == once
