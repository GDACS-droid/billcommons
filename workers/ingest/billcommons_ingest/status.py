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

from billcommons_shared.enrollment import (
    ENROLLED_PENDING_GRACE_DAYS as _ENROLLED_PENDING_GRACE_DAYS,
    enrolled_outcome_is_uncaptured as _enrolled_outcome_is_uncaptured,
)
from datetime import date

# Controlled vocabulary. Deliberately matches what downstream consumers asked
# for, so nobody has to translate ours into theirs.
INTRODUCED = "introduced"
IN_COMMITTEE = "in_committee"
SUBSTITUTED = "substituted"
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
    SUBSTITUTED,
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
LIVE_STATUSES = frozenset(
    {INTRODUCED, IN_COMMITTEE, SUBSTITUTED, PASSED_ONE_CHAMBER, PASSED_BOTH}
)

# Used only to break ties between statuses derived from the SAME date.
_RANK = {
    INTRODUCED: 1,
    IN_COMMITTEE: 2,
    # A substituted print sits below a chamber passage: the survivor print is
    # what actually carries the bill forward, and this rank only matters for
    # same-date tie-breaks before recompute_status_for_bills resolves it via
    # related_bills (see cli.py).
    SUBSTITUTED: 2.5,
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
    # Direction matters: "substituted BY X" means X replaced this bill (this
    # print moves to a new identifier and stays LIVE under it); "substituted
    # FOR X" means THIS bill is the survivor, so it implies nothing about this
    # bill's own status and is deliberately absent from this table.
    (re.compile(r"\bsubstituted\s+by\s+[A-Za-z]{1,3}\s?\d+[\s-]?[A-Za-z]?\b", re.I), SUBSTITUTED),
    (re.compile(r"\bsent\s+to\s+(the\s+)?governor\b", re.I), ENROLLED),
    (re.compile(r"\benrolled\b", re.I), ENROLLED),
    (re.compile(r"\breferred\s+to\b", re.I), IN_COMMITTEE),
    (re.compile(r"\bintroduced\b", re.I), INTRODUCED),
)


@dataclass(frozen=True)
class ActionRow:
    """The fields status derivation reads.

    `organization_id` is optional and defaults to None so every existing
    caller/fixture that only ever passed the first three fields keeps
    working unchanged. It is populated by callers that can join
    `bill_actions.organization_id` (see cli.py) and is used ONLY to detect
    two distinct chambers both recording a `passage` action (R2 -- PASSED_BOTH).
    """

    action_date: date | None
    classification: str | None
    description: str | None
    organization_id: object | None = None


# Classification tokens that represent forward motion on a bill, used only to
# decide whether a stale DEAD (see below) should be demoted. Deliberately a
# broader set than "things that map to a LIVE status today" -- reading-2/
# reading-3 currently map to INTRODUCED, but a second or third reading dated
# after a recorded death is still unambiguous proof the bill kept moving.
_PROGRESS_CLASSIFICATIONS = frozenset(
    {
        "passage",
        "committee-passage",
        "committee-passage-favorable",
        "committee-passage-unfavorable",
        "referral-committee",
        "reading-2",
        "reading-3",
        "executive-receipt",
    }
)


def _is_progress_classification(classification: str | None) -> bool:
    if not classification:
        return False
    return any(
        token.strip() in _PROGRESS_CLASSIFICATIONS for token in classification.split(",")
    )


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

    ONE exception to "an outcome, once reached, is what the bill is": DEAD.
    Carryover jurisdictions (NY chief among them) file "DIED IN [CHAMBER]" at
    the close of year one of a two-year session purely as an end-of-year
    bookkeeping artifact -- the bill is very much still alive and often goes
    on to pass both chambers in year two. Treating that the same as a real
    death would report bills that later got enacted as dead forever. So: if
    the winning terminal entry is DEAD (never VETOED/ENACTED/WITHDRAWN -- those
    are real, deliberate acts, not clerical carryover noise) and the record
    also contains unambiguous forward motion dated strictly AFTER it, the
    stale DEAD is dropped and the bill is re-derived as if that entry had
    never been recorded. Same-date "progress" does not count -- an action
    filed the same day as the death is noise, not proof of survival.
    """
    derived: list[tuple[date | None, str]] = []
    for action in actions:
        status = status_for_action(action)
        if status is not None:
            derived.append((action.action_date, status))

    if not derived:
        return None

    progress_dates = [
        action.action_date
        for action in actions
        if action.action_date is not None
        and (
            _is_progress_classification(action.classification)
            or status_for_action(action) in (PASSED_ONE_CHAMBER, PASSED_BOTH)
        )
    ]

    def sort_key(entry: tuple[date | None, str]) -> tuple[date, int]:
        action_date, status = entry
        return (action_date or date.min, _RANK[status])

    remaining = list(derived)
    result: str | None = None
    while remaining:
        terminal = [entry for entry in remaining if entry[1] in TERMINAL_STATUSES]
        pool = terminal or remaining
        winner = max(pool, key=sort_key)
        if winner[1] == DEAD:
            winner_date = winner[0] or date.min
            if any(pd > winner_date for pd in progress_dates):
                # Stale carryover DEAD: drop this one entry and re-resolve
                # from what remains, so an earlier real terminal outcome (or
                # the non-terminal pool, if nothing else concluded the bill)
                # decides instead.
                remaining.remove(winner)
                continue
        result = winner[1]
        break

    # R2 -- two distinct chambers each recording a `passage` action outrank a
    # single chamber's passage. Only ever upgrades PASSED_ONE_CHAMBER: a bill
    # that concluded some other way (terminal, or a later committee referral)
    # is reported as that, not silently overwritten.
    if result == PASSED_ONE_CHAMBER:
        chambers = {
            action.organization_id
            for action in actions
            if action.organization_id is not None
            and action.classification
            and "passage" in {tok.strip() for tok in action.classification.split(",")}
        }
        if len(chambers) >= 2:
            result = PASSED_BOTH

    return result


# `substituted by <ID>` regex, capturing the survivor's identifier. Kept
# separate from `_TEXT_PATTERNS` (which only ever answers "what status does
# this action imply", never "which OTHER bill does it name") because
# resolving the survivor is a cross-bill lookup that belongs to the caller
# (see cli.py `recompute_status_for_bills`), not to single-action derivation.
_SUBSTITUTED_BY_RE = re.compile(
    r"\bsubstituted\s+by\s+([A-Za-z]{1,3}\s?\d+[\s-]?[A-Za-z]?)\b", re.I
)
# "substituted FOR X" means this bill is the survivor -- it must never be
# read as naming a survivor of ITS OWN.
_SUBSTITUTED_FOR_RE = re.compile(r"\bsubstituted\s+for\b", re.I)


def substitution_target(description: str | None) -> str | None:
    """The OTHER bill's identifier if `description` says this bill was
    substituted BY it, else None.

    Direction matters: "substituted by A10008C" names a survivor this bill
    should defer to; "substituted for A10008C" names a bill THIS one
    replaced, which says nothing about this bill's own fate. The identifier
    is returned normalized (`normalize_bill_number`) so callers can compare
    it directly against `bills.identifier_norm`.
    """
    if not description:
        return None
    if _SUBSTITUTED_FOR_RE.search(description):
        return None
    match = _SUBSTITUTED_BY_RE.search(description)
    if not match:
        return None
    from billcommons_shared.normalize import normalize_bill_number

    try:
        return normalize_bill_number(match.group(1))
    except ValueError:
        return None


# A single uppercase letter trailing a digit is NY's print/amendment version
# (e.g. "A 10008C"), never part of the bill's identity -- the corpus stores
# the bill as "A 10008". Survivor lookups need both forms, exact first.
_TRAILING_PRINT_VERSION_RE = re.compile(r"\d[A-Z]$")


def substitution_lookup_candidates(identifier: str) -> list[str]:
    """Identifiers to try, in order, when resolving `identifier` against
    `bills.identifier_norm`. Always includes `identifier` itself; if it ends
    in a digit followed by one uppercase letter (an NY print version), also
    includes that identifier with the trailing letter stripped."""
    candidates = [identifier]
    if _TRAILING_PRINT_VERSION_RE.search(identifier):
        candidates.append(identifier[:-1])
    return candidates


# Re-exported here so ingest callers keep importing it from status.
ENROLLED_PENDING_GRACE_DAYS = _ENROLLED_PENDING_GRACE_DAYS
enrolled_outcome_is_uncaptured = _enrolled_outcome_is_uncaptured


def apply_session_outcome(
    status: str | None,
    session_end_date: date | None,
    today: date | None = None,
    session_active: bool = False,
    session_has_recent_activity: bool = False,
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

    And never applied while the SOURCE still calls the session active.
    `sessions.end_date` is populated from Open States' `expected_adjournment`
    (registry.py) -- an ESTIMATE, not a recorded sine die. When a chamber sits
    past its expected date, that estimate silently becomes a past date and the
    calendar alone marks every live bill in the session dead.

    Not hypothetical: on 2026-08-02 this had killed 26,165 bills across four
    jurisdictions the source still flagged active, including 18,343 in
    Massachusetts, whose 194th General Court runs to the end of the year and
    whose expected adjournment had passed two days earlier. Those 18,343 were
    19.3% of the entire national died_on_adjournment figure this project
    publishes as its flagship finding.

    `active` is upstream's statement of fact; `expected_adjournment` is
    upstream's guess. When they disagree, the fact wins and we assert nothing.
    """
    if status is not None and status not in LIVE_STATUSES:
        return status
    if session_end_date is None:
        return status
    if session_end_date >= (today or date.today()):
        return status

    # Past the predicted adjournment. What the two upstream signals say now
    # decides, and they do not always agree.
    if not session_active:
        # Source says the session is over and the date agrees. Reached for
        # status=None too: whatever stage it got to, the session closed without
        # it becoming law, and nothing further can happen to it.
        return DIED_ON_ADJOURNMENT

    # CONTRADICTION: the source calls the session active while its own
    # predicted adjournment has passed. Neither field is authoritative here --
    # `expected_adjournment` is a guess, and `active` is demonstrably sticky
    # (Virginia sat at active=true with no filed action for over three months).
    if session_has_recent_activity:
        # A chamber filing paper this month has not adjourned. Affirmative
        # evidence of life, so the bill's own record stands.
        return status

    # Contradiction with no corroboration either way. Silence cannot prove
    # adjournment -- a recess looks identical -- and the source's active flag
    # cannot prove life once it has gone stale. Asserting death here would
    # recreate exactly the unsupported inference this function got wrong;
    # asserting life would be the same mistake pointed the other way.
    #
    # So: assert nothing. This is the same doctrine that already leaves ~5% of
    # bills unclassified rather than guessing at a state's wording.
    return None
