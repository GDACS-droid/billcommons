"""Derive a normalized bill status from the official action record.

`bills.status` was null for all 209,612 bills, which forced every consumer to
regex `latest_action_text` themselves. This module fills it from the actions we
already store.

Why this is not a one-liner over `bill_actions.classification`:

    KS SB 499   2026-04-10  [failure]  "Died in Committee"
    MS SB 2693  2026-02-03  [None]     "Died In Committee"

Identical events, different states, and 685,120 of ~1.64M actions (42%) carry
no classification at all. Deriving from classification alone would read MS
SB 2693's earlier `referral-committee` and report it IN_COMMITTEE -- calling a
dead bill alive, which is the one error a legislative source must not make.

So: classification is the primary signal, a deliberately narrow text fallback
covers unclassified actions, and anything neither recognizes yields **None**.
"Not determined" is an honest answer; a confident wrong one is not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from billcommons_shared.enrollment import (
    ENROLLED_PENDING_GRACE_DAYS as _ENROLLED_PENDING_GRACE_DAYS,
)
from billcommons_shared.enrollment import (
    enrolled_outcome_is_uncaptured as _enrolled_outcome_is_uncaptured,
)

# Controlled vocabulary. Deliberately matches what downstream consumers asked
# for, so nobody has to translate ours into theirs.
INTRODUCED = "introduced"
IN_COMMITTEE = "in_committee"
PASSED_ONE_CHAMBER = "passed_one_chamber"
PASSED_BOTH = "passed_both"
ENROLLED = "enrolled"
ENACTED = "enacted"
VETOED = "vetoed"
DEAD = "dead"
WITHDRAWN = "withdrawn"
DIED_ON_ADJOURNMENT = "died_on_adjournment"

ALL_STATUSES = (
    INTRODUCED,
    IN_COMMITTEE,
    PASSED_ONE_CHAMBER,
    PASSED_BOTH,
    ENROLLED,
    ENACTED,
    VETOED,
    DEAD,
    WITHDRAWN,
    DIED_ON_ADJOURNMENT,
)

# A bill that reaches one of these has an OUTCOME; procedural noise filed
# afterwards must not drag it back to an earlier stage.
TERMINAL_STATUSES = frozenset({ENACTED, VETOED, DEAD, WITHDRAWN, DIED_ON_ADJOURNMENT})

# Statuses that mean "still in play", i.e. the bill needs the session to
# continue in order to go anywhere. When the session adjourns, these die.
#
# ENROLLED is deliberately NOT here: a bill already on the governor's desk
# survives sine die, and executives routinely sign for weeks afterwards (HI
# SB 2135 adjourned 2026-05-08 and was signed 2026-07-07). Marking those dead
# would be the same error in the opposite direction.
LIVE_STATUSES = frozenset({INTRODUCED, IN_COMMITTEE, PASSED_ONE_CHAMBER, PASSED_BOTH})

# The ENROLLED grace window lives in billcommons_shared.enrollment because the
# API needs it too and ships in a container that has no access to this package.
# Re-exported here so ingest callers keep importing it from status.
ENROLLED_PENDING_GRACE_DAYS = _ENROLLED_PENDING_GRACE_DAYS
enrolled_outcome_is_uncaptured = _enrolled_outcome_is_uncaptured


def apply_session_outcome(
    status: str | None, session_end_date: date | None, today: date | None = None
) -> str | None:
    """Fold the session's fate into a bill's action-derived status.

    A bill's own action record cannot express the most common way a bill
    actually ends. Nothing is filed when a session adjourns -- the bill simply
    stops, mid-committee, forever. Reading only the actions, `derive_status`
    therefore reports the last thing that HAPPENED ("passed one chamber") and
    a consumer reasonably reads that as momentum. Measured on this corpus:
    54,547 bills, 26% of everything, sat at a live status in a session that had
    already adjourned. That is the single largest source of false "still alive"
    here, and "which of my bills are dead" is the question this field exists to
    answer.

    Applied only when the end date is KNOWN and PAST. An unknown end date
    yields the action-derived status unchanged -- and that is load-bearing,
    not laziness: the sessions missing an end date are overwhelmingly
    two-year carryover biennia (NY, NJ, IL, MN, WI, DC) where a bill pending
    at the end of year one is genuinely still alive and rolls into year two.
    Guessing there would invent deaths rather than report them.

    Never overrides a status the bill's own record establishes. Enactment,
    veto, withdrawal and an explicit death all outrank adjournment, and
    ENROLLED is excluded from LIVE_STATUSES because a bill on the governor's
    desk survives sine die by design.
    """
    if session_end_date is None:
        return status
    if status is not None and status not in LIVE_STATUSES:
        return status
    if session_end_date >= (today or date.today()):
        return status
    # Reached for status=None too: whatever stage it got to, the session
    # closed without it becoming law, and nothing further can happen to it.
    return DIED_ON_ADJOURNMENT
