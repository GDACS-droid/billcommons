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
    PASSED_BOTH,
    PASSED_ONE_CHAMBER,
    SUBSTITUTED,
    VETOED,
    WITHDRAWN,
    ActionRow,
    derive_status,
    substitution_target,
)


def A(d, classification=None, description=None, organization_id=None):
    return ActionRow(
        action_date=d,
        classification=classification,
        description=description,
        organization_id=organization_id,
    )


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


# ---------------------------------------------------------------------------
# R1 -- stale carryover DEAD demotion (NY "DIED IN ASSEMBLY" at year-end,
# followed in year two by real progress)
# ---------------------------------------------------------------------------


def test_stale_dead_is_demoted_by_later_passage():
    """S6954-shaped fixture: died at the close of year one, passed both
    chambers in year two. The carryover artifact must not lock the bill dead
    forever."""
    actions = [
        A(date(2025, 1, 8), "introduction", "REFERRED TO RULES"),
        A(date(2025, 6, 20), None, "DIED IN SENATE"),
        A(date(2026, 1, 6), "passage", "PASSED SENATE"),
        A(date(2026, 1, 20), "passage", "PASSED ASSEMBLY"),
    ]
    assert derive_status(actions) != DEAD


def test_vetoed_is_never_demoted_by_later_procedural_filing():
    """Only DEAD is ever reconsidered. A veto is a deliberate act, and a
    procedural filing after it (even one that looks like 'progress') must not
    reopen it."""
    actions = [
        A(date(2026, 5, 1), "passage", "Passed both chambers"),
        A(date(2026, 6, 1), "executive-veto", "Vetoed by the Governor"),
        A(date(2026, 6, 15), "referral-committee", "Referred to committee on veto override"),
    ]
    assert derive_status(actions) == VETOED


def test_dead_with_only_later_non_progress_noise_stays_dead():
    """'Withdrawn'/administrative noise after a real death is not progress --
    the bill stays dead."""
    actions = [
        A(date(2026, 1, 5), "introduction", "Introduced"),
        A(date(2026, 3, 1), "failure", "Died in committee"),
        A(date(2026, 3, 15), None, "Notice of file closure"),
    ]
    assert derive_status(actions) == DEAD


def test_dead_with_same_date_progress_stays_dead():
    """Strictly-after only: an action dated the SAME day as the death does
    not prove the bill outlived it."""
    actions = [
        A(date(2026, 1, 5), "introduction", "Introduced"),
        A(date(2026, 3, 1), None, "Died in committee"),
        A(date(2026, 3, 1), "referral-committee", "Referred to committee"),
    ]
    assert derive_status(actions) == DEAD


def test_real_second_death_after_progress_still_wins():
    """A bill that died, showed transient progress, then genuinely died again
    LATER than that progress must stay dead -- the demotion rule only fires
    when progress comes after the winning (latest) DEAD, not before it."""
    actions = [
        A(date(2025, 6, 30), None, "Died in Assembly"),
        A(date(2025, 8, 1), "passage", "Passed Senate"),
        A(date(2025, 12, 31), None, "Died in Assembly"),
    ]
    assert derive_status(actions) == DEAD


# ---------------------------------------------------------------------------
# R2 -- PASSED_BOTH actually produced (two distinct chambers each recording a
# `passage` action)
# ---------------------------------------------------------------------------


def test_passage_in_two_distinct_chambers_is_passed_both():
    actions = [
        A(date(2026, 1, 10), "passage", "Passed Senate", organization_id="senate"),
        A(date(2026, 2, 1), "passage", "Passed Assembly", organization_id="assembly"),
    ]
    assert derive_status(actions) == PASSED_BOTH


def test_passage_in_one_chamber_twice_is_not_passed_both():
    """Two passage actions from the SAME chamber (e.g. a reconsideration) must
    not be misread as two chambers agreeing."""
    actions = [
        A(date(2026, 1, 10), "passage", "Passed Senate", organization_id="senate"),
        A(date(2026, 2, 1), "passage", "Passed Senate (reconsidered)", organization_id="senate"),
    ]
    assert derive_status(actions) == PASSED_ONE_CHAMBER


def test_passage_without_chamber_info_stays_passed_one_chamber():
    """No organization_id populated -- do not guess at PASSED_BOTH."""
    actions = [
        A(date(2026, 1, 10), "passage", "Passed Senate"),
        A(date(2026, 2, 1), "passage", "Passed Assembly"),
    ]
    assert derive_status(actions) == PASSED_ONE_CHAMBER


# ---------------------------------------------------------------------------
# R3 -- substitution text rule (substitution_target + SUBSTITUTED status)
# ---------------------------------------------------------------------------


def test_substituted_by_is_substituted_status():
    actions = [
        A(date(2026, 1, 5), "introduction", "Introduced"),
        A(date(2026, 3, 1), None, "SUBSTITUTED BY A10008C"),
    ]
    assert derive_status(actions) == SUBSTITUTED


def test_substituted_for_is_no_status_change():
    """Direction matters: this bill is the SURVIVOR here, so its own record
    says nothing about ITS fate."""
    actions = [A(date(2026, 3, 1), None, "SUBSTITUTED FOR A10008C")]
    assert derive_status(actions) is None


def test_substitution_target_extracts_normalized_survivor_identifier():
    assert substitution_target("SUBSTITUTED BY A10008C") == "A 10008C"
    # Whitespace/hyphen variants normalize identically.
    assert substitution_target("Substituted by A 10008-C") == "A 10008C"


def test_substitution_target_none_for_substituted_for():
    assert substitution_target("SUBSTITUTED FOR A10008C") is None


def test_substitution_target_none_when_absent():
    assert substitution_target("Referred to committee") is None
    assert substitution_target(None) is None
