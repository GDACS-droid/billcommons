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

# Used only to break ties between statuses derived from the SAME date.
_RANK = {
    INTRODUCED: 1,
    IN_COMMITTEE: 2,
    PASSED_ONE_CHAMBER: 3,
    PASSED_BOTH: 4,
    ENROLLED: 5,
    # Below the affirmative endings: adjournment is never derived from an
    # action, so it can only ever apply where nothing else concluded the bill.
    DIED_ON_ADJOURNMENT: 5.5,
    WITHDRAWN: 6,
    DEAD: 7,
    VETOED: 8,
    # Enactment outranks a veto on the same day: an override is recorded as
    # both, and the bill is law.
    ENACTED: 9,
}

# Open States action classifications -> status. A single action may carry
# several comma-separated classifications; the highest-ranked one wins.
_CLASSIFICATION_STATUS = {
    "became-law": ENACTED,
    "executive-signature": ENACTED,
    "executive-veto": VETOED,
    "executive-veto-line-item": VETOED,
    "withdrawal": WITHDRAWN,
    "failure": DEAD,
    "committee-failure": DEAD,
    "enrolled": ENROLLED,
    "executive-receipt": ENROLLED,
    "passage": PASSED_ONE_CHAMBER,
    "committee-passage": IN_COMMITTEE,
    "committee-passage-favorable": IN_COMMITTEE,
    "committee-passage-unfavorable": IN_COMMITTEE,
    "referral-committee": IN_COMMITTEE,
    "introduction": INTRODUCED,
    "filing": INTRODUCED,
    "reading-1": INTRODUCED,
    "reading-2": INTRODUCED,
    "reading-3": INTRODUCED,
}

# Text fallback, applied ONLY when an action has no classification.
#
# Every pattern here is anchored on wording that states actually use and that
# cannot plausibly mean anything else. Deliberately omitted: bare "passed",
# "reported", "amended" -- too easy to match a motion that FAILED to do the
# thing ("motion to withdraw failed"), and a wrong terminal status is far
# costlier than a missing one.
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Enactment first: "veto overridden" must read as law, not as a veto.
    (re.compile(r"\bveto\s+overr(idden|ide)\b", re.I), ENACTED),
    (re.compile(r"\boverr(idden|ode)\s+.{0,20}\bveto\b", re.I), ENACTED),
    (re.compile(r"\bchaptered\b", re.I), ENACTED),
    (re.compile(r"\bbecame\s+law\b", re.I), ENACTED),
    (re.compile(r"\b(signed|approved)\s+by\s+(the\s+)?governor\b", re.I), ENACTED),
    (re.compile(r"^act\s+\d+", re.I), ENACTED),
    (re.compile(r"\bact\s+\d+,\s*\d{2}/\d{2}/\d{4}", re.I), ENACTED),
    (re.compile(r"\bvetoed\s+by\s+(the\s+)?governor\b", re.I), VETOED),
    (re.compile(r"^vetoed\b", re.I), VETOED),
    (re.compile(r"\bdied\s+(in|on|pursuant)\b", re.I), DEAD),
    (re.compile(r"\bfailed\s+to\s+pass\b", re.I), DEAD),
    (re.compile(r"\b(indefinitely\s+postponed|postponed\s+indefinitely)\b", re.I), DEAD),
    (re.compile(r"\bsession\s+sine\s+die\b", re.I), DEAD),
    (re.compile(r"\bwithdrawn\s+by\s+(the\s+)?(author|sponsor|patron)\b", re.I), WITHDRAWN),
    (re.compile(r"^withdrawn\b", re.I), WITHDRAWN),
    (re.compile(r"\bsent\s+to\s+(the\s+)?governor\b", re.I), ENROLLED),
    (re.compile(r"\benrolled\b", re.I), ENROLLED),
    (re.compile(r"\breferred\s+to\b", re.I), IN_COMMITTEE),
    (re.compile(r"\bintroduced\b", re.I), INTRODUCED),
)


@dataclass(frozen=True)
class ActionRow:
    """The only three fields status derivation reads."""

    action_date: date | None
    classification: str | None
    description: str | None


def _status_from_classification(classification: str | None) -> str | None:
    if not classification:
        return None
    best: str | None = None
    for token in classification.split(","):
        mapped = _CLASSIFICATION_STATUS.get(token.strip())
        if mapped and (best is None or _RANK[mapped] > _RANK[best]):
            best = mapped
    return best


def _status_from_text(description: str | None) -> str | None:
    if not description:
        return None
    for pattern, status in _TEXT_PATTERNS:
        if pattern.search(description):
            return status
    return None


def status_for_action(action: ActionRow) -> str | None:
    """Status implied by one action, or None if it implies nothing.

    Classification wins outright when present -- it is structured upstream data
    and beats guessing from prose. The text fallback exists only for the 42% of
    actions that arrive unclassified.
    """
    from_class = _status_from_classification(action.classification)
    if from_class is not None:
        return from_class
    return _status_from_text(action.description)


def derive_status(actions: list[ActionRow]) -> str | None:
    """Normalized status for a bill, or None when the record does not say.

    An OUTCOME (enacted/vetoed/dead/withdrawn), once reached, is what the bill
    is -- later procedural filings cannot demote it back to "in committee". So
    terminal statuses are considered first, and among them the latest-dated one
    wins, which is what makes "died, then revived and enacted" and "vetoed,
    then overridden" both come out right. Only if nothing terminal was ever
    recorded does the newest procedural stage stand.

    Undated actions sort oldest: an action with no date cannot be shown to
    supersede one that has a date, so it must not win by accident.
    """
    derived: list[tuple[date | None, str]] = []
    for action in actions:
        status = status_for_action(action)
        if status is not None:
            derived.append((action.action_date, status))

    if not derived:
        return None

    def sort_key(entry: tuple[date | None, str]) -> tuple[date, int]:
        action_date, status = entry
        return (action_date or date.min, _RANK[status])

    terminal = [entry for entry in derived if entry[1] in TERMINAL_STATUSES]
    pool = terminal or derived
    return max(pool, key=sort_key)[1]


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
