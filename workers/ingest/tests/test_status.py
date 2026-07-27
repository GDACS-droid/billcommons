"""Status derivation.

These tests exist because a wrong status is worse than no status: a consumer
building P0 alerts on "is this bill still alive?" acts on what we say. Each
case below is a way we could be confidently wrong.
"""
from __future__ import annotations

from datetime import date

from billcommons_ingest.status import (
    DEAD,
    ENACTED,
    ENROLLED,
    IN_COMMITTEE,
    INTRODUCED,
    PASSED_ONE_CHAMBER,
    VETOED,
    WITHDRAWN,
    ActionRow,
    derive_status,
)


def A(d, classification=None, description=None):
    return ActionRow(action_date=d, classification=classification, description=description)


def test_unclassified_died_in_committee_is_dead_not_in_committee():
    """The case that motivated the text fallback.

    MS SB 2693's "Died In Committee" carries NO classification, while the
    identical KS event is classified `failure`. Reading classifications alone
    would fall back to the earlier referral and report the bill as alive in
    committee. A dead bill reported as live is the worst failure mode here.
    """
    actions = [
        A(date(2026, 1, 19), "referral-committee", "Referred To Judiciary, Division A"),
        A(date(2026, 2, 3), None, "Died In Committee"),
    ]
    assert derive_status(actions) == DEAD


def test_classified_failure_is_dead():
    actions = [
        A(date(2026, 2, 9), "introduction", "Introduced"),
        A(date(2026, 2, 10), "referral-committee", "Referred to Senate Committee"),
        A(date(2026, 4, 10), "failure", "Died in Committee"),
    ]
    assert derive_status(actions) == DEAD


def test_enacted_bill_is_not_demoted_by_later_procedural_noise():
    """Bills keep accruing filings after they become law. If the NEWEST
    status-bearing action simply won, an administrative referral logged after
    enactment would report an enacted bill as sitting in committee."""
    actions = [
        A(date(2026, 5, 1), "passage", "Passed House"),
        A(date(2026, 7, 7), "became-law", "Act 196, 07/07/2026"),
        A(date(2026, 7, 20), "referral-committee", "Referred to Committee on Engrossment"),
    ]
    assert derive_status(actions) == ENACTED


def test_veto_override_reads_as_enacted_not_vetoed():
    """An override is recorded alongside the veto. Reporting VETOED for a bill
    that is now law would be exactly backwards."""
    actions = [
        A(date(2026, 6, 1), "executive-veto", "Vetoed by Governor"),
        A(date(2026, 6, 15), None, "Veto overridden by Senate"),
    ]
    assert derive_status(actions) == ENACTED


def test_revived_bill_reports_its_latest_outcome():
    """Among terminal outcomes the latest-dated one wins, so a bill that died
    and was later enacted is enacted -- not permanently stuck at dead."""
    actions = [
        A(date(2026, 2, 3), "failure", "Died in Committee"),
        A(date(2026, 4, 1), "became-law", "Signed by Governor"),
    ]
    assert derive_status(actions) == ENACTED


def test_procedural_stage_only_used_when_no_outcome_exists():
    actions = [
        A(date(2026, 1, 5), "introduction", "Introduced"),
        A(date(2026, 1, 9), "referral-committee", "Referred to Ways and Means"),
    ]
    assert derive_status(actions) == IN_COMMITTEE


def test_passage_without_enrollment_is_one_chamber_only():
    """We must not claim `passed_both` without evidence of both chambers.
    A single `passage` means one chamber acted, nothing more."""
    actions = [
        A(date(2026, 1, 5), "introduction", "Introduced"),
        A(date(2026, 3, 2), "passage", "Passed Senate"),
    ]
    assert derive_status(actions) == PASSED_ONE_CHAMBER


def test_sent_to_governor_is_enrolled():
    actions = [
        A(date(2026, 3, 2), "passage", "Passed Senate"),
        A(date(2026, 4, 30), "executive-receipt", "Enrolled to Governor."),
    ]
    assert derive_status(actions) == ENROLLED


def test_withdrawal_is_reported():
    actions = [
        A(date(2026, 1, 5), "introduction", "Introduced"),
        A(date(2026, 2, 1), "withdrawal", "Withdrawn by author"),
    ]
    assert derive_status(actions) == WITHDRAWN


def test_veto_is_reported_when_not_overridden():
    actions = [
        A(date(2026, 5, 1), "passage", "Passed both chambers"),
        A(date(2026, 6, 1), "executive-veto", "Vetoed by Governor"),
    ]
    assert derive_status(actions) == VETOED


def test_no_recognizable_action_yields_none_not_a_guess():
    """"Not determined" is a legitimate answer. Inventing INTRODUCED for a bill
    whose record we cannot read would be a fabricated status."""
    actions = [
        A(date(2026, 1, 5), None, "Fiscal note requested"),
        A(date(2026, 1, 6), None, "Notice of public comment period"),
    ]
    assert derive_status(actions) is None


def test_no_actions_at_all_yields_none():
    assert derive_status([]) is None


def test_undated_action_does_not_outrank_a_dated_one():
    """Undated actions sort oldest. An undated referral must not override a
    dated enactment just because it happens to be last in the list."""
    actions = [
        A(date(2026, 7, 7), "became-law", "Act 196"),
        A(None, "referral-committee", "Referred to committee"),
    ]
    assert derive_status(actions) == ENACTED


def test_failed_motion_to_withdraw_is_not_a_withdrawal():
    """The text fallback is deliberately narrow. "Motion to withdraw failed"
    contains the word withdraw but means the opposite; matching it would
    silently kill a live bill."""
    actions = [
        A(date(2026, 1, 5), "introduction", "Introduced"),
        A(date(2026, 2, 1), None, "Motion to withdraw failed"),
    ]
    assert derive_status(actions) != WITHDRAWN


def test_classification_beats_text_on_the_same_action():
    """Structured upstream data wins over prose guessing. Here the description
    would text-match INTRODUCED, but the classification says the bill passed."""
    actions = [A(date(2026, 3, 2), "passage", "Introduced version passed the Senate")]
    assert derive_status(actions) == PASSED_ONE_CHAMBER


def test_introduction_only_bill_is_introduced():
    assert derive_status([A(date(2026, 1, 5), "introduction", "Introduced")]) == INTRODUCED
