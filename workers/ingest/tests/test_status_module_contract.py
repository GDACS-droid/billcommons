"""Every symbol cli.py reaches for on the status module must exist.

On 2026-08-02 a refactor of status.py deleted ActionRow, derive_status,
status_for_action and both text/classification helpers while cli.py went on
calling them. `recompute_status_for_bills` -- the function that derives every
bill's status -- therefore raised AttributeError on every invocation for about
seven hours.

It failed silently by construction. The adjournment sweep wraps its call in
`except Exception: traceback.print_exc()`, so the pipeline rolled back, printed
into Railway's log stream, and carried on looking healthy. No status was ever
written wrong; statuses simply stopped being computed, which is invisible from
outside.

The API and MCP suites were green throughout, because neither imports this
package. This test is the cheap structural check that closes that gap: it needs
no database, no fixtures, and it fails the moment the two files disagree.
"""
from __future__ import annotations

import ast
import pathlib

from billcommons_ingest import status as status_mod

CLI = pathlib.Path(__file__).resolve().parents[1] / "billcommons_ingest" / "cli.py"


def _referenced_status_attributes() -> set[str]:
    """Every `status_mod.NAME` in cli.py, read from the AST rather than by
    regex so a name in a comment or a string cannot produce a false failure."""
    tree = ast.parse(CLI.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "status_mod"
        ):
            names.add(node.attr)
    return names


def test_cli_only_uses_symbols_the_status_module_defines():
    referenced = _referenced_status_attributes()
    assert referenced, "found no status_mod usages -- the check is not looking at anything"
    missing = sorted(n for n in referenced if not hasattr(status_mod, n))
    assert not missing, (
        f"cli.py calls status_mod.{{{','.join(missing)}}} which status.py does not "
        "define. recompute_status_for_bills will raise AttributeError at runtime "
        "and the adjournment sweep will swallow it."
    )


def test_the_derivation_entry_points_still_exist():
    """Named explicitly so deleting one fails here rather than in production
    at 3am inside an except block."""
    for name in (
        "ActionRow",
        "derive_status",
        "status_for_action",
        "apply_session_outcome",
        "substitution_target",
        "substitution_lookup_candidates",
        "LIVE_STATUSES",
        "TERMINAL_STATUSES",
        "SUBSTITUTED",
    ):
        assert hasattr(status_mod, name), f"status.{name} is gone"


def test_substitution_lookup_candidates_tolerates_ny_print_version_suffix():
    """"SUBSTITUTED BY A10008C" normalizes to "A 10008C", but the corpus
    identifies the bill as "A 10008" -- the trailing letter is NY's print/
    amendment version, never part of bill identity. Exact match must still
    be tried first, and only when the caller opts in with print_suffix=True
    (NY only -- FL "HB 1A" / CA "AB 1X" use the same shape as identity)."""
    assert status_mod.substitution_lookup_candidates("A 10008C") == ["A 10008C"]
    assert status_mod.substitution_lookup_candidates(
        "A 10008C", print_suffix=True
    ) == ["A 10008C", "A 10008"]
    assert status_mod.substitution_lookup_candidates("A 10008") == ["A 10008"]
    assert status_mod.substitution_lookup_candidates("A 10008", print_suffix=True) == [
        "A 10008"
    ]
    assert status_mod.substitution_lookup_candidates("HB 12") == ["HB 12"]
    # print_suffix is a caller-supplied opt-in, not a jurisdiction lookup --
    # the FL/CA gate (only NY passes print_suffix=True) lives in the CLI's
    # pass-2 resolution, not in this function.
    assert status_mod.substitution_lookup_candidates("HB 1A") == ["HB 1A"]


def test_substituted_is_in_the_vocabulary_and_non_terminal():
    """Added for the substitution-propagation fix (R3): SUBSTITUTED is a LIVE
    status, never a terminal one -- a substituted print only concludes once
    its survivor does, and `recompute_status_for_bills` is what resolves
    that, not this module."""
    assert status_mod.SUBSTITUTED in status_mod.ALL_STATUSES
    assert status_mod.SUBSTITUTED in status_mod.LIVE_STATUSES
    assert status_mod.SUBSTITUTED not in status_mod.TERMINAL_STATUSES


def test_substitution_target_detects_nj_mixed_case_with_reprint_marker():
    """NJ files "Substituted by A1516 (1R)" -- mixed-case verb, no NY-style
    ALL CAPS, and a trailing "(1R)"/"(2R)" reprint marker that is never part
    of the survivor's identity. The survivor must resolve to plain "A1516"
    (normalized "A 1516"), with the reprint marker dropped entirely."""
    assert status_mod.substitution_target("Substituted by A1516 (1R)") == "A 1516"
    assert status_mod.substitution_target("Substituted by A1516 (2R)") == "A 1516"
    # No space before the parenthesized marker -- also seen in the corpus.
    assert status_mod.substitution_target("Substituted by A1516(1R)") == "A 1516"


def test_substitution_target_is_case_insensitive():
    """Detection must not depend on NY's ALL CAPS convention -- NJ's action
    text uses ordinary sentence case for the same wording."""
    assert status_mod.substitution_target("substituted by a1516 (1r)") == "A 1516"
    assert status_mod.substitution_target("SUBSTITUTED BY A1516 (1R)") == "A 1516"


def test_status_for_action_detects_nj_substitution_shape():
    """The bill-level SUBSTITUTED classification must fire on NJ's shape too,
    not only NY's ALL CAPS "SUBSTITUTED BY A10008C" form."""
    action = status_mod.ActionRow(
        action_date=None,
        classification=None,
        description="Substituted by A1516 (1R)",
    )
    assert status_mod.status_for_action(action) == status_mod.SUBSTITUTED


def test_status_for_action_does_not_treat_reported_with_substitute_as_substitution():
    """"Reported from committee with substitute" names an amendment mechanism,
    not "substituted BY <bill>" -- it must never imply SUBSTITUTED."""
    action = status_mod.ActionRow(
        action_date=None,
        classification=None,
        description="Reported from committee with substitute",
    )
    assert status_mod.status_for_action(action) != status_mod.SUBSTITUTED
    assert status_mod.substitution_target("Reported from committee with substitute") is None


def test_status_for_action_detects_nj_enactment_p_l_citation():
    """NJ's enactment record names the chapter law, not "signed/approved BY
    the Governor": "Approved P.L.2025, c.34." must read as ENACTED the same
    as the "signed by governor" wording other jurisdictions use."""
    for text in (
        "Approved P.L.2025, c.34.",
        "approved p.l.2025, c.34",
        "APPROVED P.L. 2025, C. 34",
    ):
        action = status_mod.ActionRow(action_date=None, classification=None, description=text)
        assert status_mod.status_for_action(action) == status_mod.ENACTED, text


def test_status_for_action_withdrawn_because_approved_is_not_enacted():
    """NJ also files "Withdrawn Because Approved P.L.2025, c.34." on a
    companion bill pulled because the OTHER (identical) bill was the one
    signed into law. That must stay WITHDRAWN, never read as this bill's
    own enactment just because "approved" appears in the text."""
    action = status_mod.ActionRow(
        action_date=None,
        classification=None,
        description="Withdrawn Because Approved P.L.2025, c.34.",
    )
    assert status_mod.status_for_action(action) == status_mod.WITHDRAWN


def test_status_for_action_detects_nj_passed_both_and_one_chamber_wording():
    """NJ's unclassified passage actions: "Passed by the Senate (40-0)" is
    one chamber; "Passed Assembly (Passed Both Houses) (75-0-0)" names the
    second chamber's vote and explicitly says both have now passed it --
    the compound wording must resolve to PASSED_BOTH, not stop at the
    one-chamber reading it also contains."""
    one_chamber = status_mod.ActionRow(
        action_date=None, classification=None, description="Passed by the Senate (40-0)"
    )
    assert status_mod.status_for_action(one_chamber) == status_mod.PASSED_ONE_CHAMBER

    both_houses = status_mod.ActionRow(
        action_date=None,
        classification=None,
        description="Passed Assembly (Passed Both Houses) (75-0-0)",
    )
    assert status_mod.status_for_action(both_houses) == status_mod.PASSED_BOTH

    assembly_only = status_mod.ActionRow(
        action_date=None, classification=None, description="Passed Assembly (36-0)"
    )
    assert status_mod.status_for_action(assembly_only) == status_mod.PASSED_ONE_CHAMBER


def test_status_for_action_passed_one_chamber_does_not_match_committee_reading_or_motion_text():
    """verify-ship F1/F1a: the unguarded "passed" + chamber-name pattern hit
    ~17k non-NJ actions that never meant one-chamber passage -- committee
    referrals, reading stages, and motions/amendments that happen to use the
    word "passed" without it being clause-initial. None of these may yield
    PASSED_ONE_CHAMBER (or PASSED_BOTH)."""
    false_positives = [
        "Passed Senate Committee",
        "Passed by the Senate Judiciary Committee",
        "Passed Senate First Reading",
        "Passed Assembly Second Reading",
        "Motion to table passed by the Senate",
        "Amendment passed by the Assembly",
        "Committee report recommending bill be passed by House committee",
        # round 3 (Finding A): committee-name allowlist was too short
        "Passed Senate Education Committee",
        "Passed House Ways and Means Committee",
        "Passed Assembly Health Committee",
        # round 3 (Finding B): bare "and passed" -- subject is the motion
        "Amended and passed by the Assembly",
        "Considered and passed by the Senate",
        "Motion was considered and passed by the Senate",
        # round 3 (Finding C): PASSED_BOTH needs the same guards
        "Bill was not passed by both houses",
        "Committee report recommending the bill be passed by both houses",
        "Motion to table passed by both houses",
    ]
    for text in false_positives:
        action = status_mod.ActionRow(action_date=None, classification=None, description=text)
        got = status_mod.status_for_action(action)
        assert got != status_mod.PASSED_ONE_CHAMBER, text
        assert got != status_mod.PASSED_BOTH, text


def test_status_for_action_passed_one_chamber_excludes_general_assembly():
    """verify-ship F2: "General Assembly" (VA/IL's name for the whole
    bicameral legislature) is not a single chamber -- "Passed by the General
    Assembly" must not resolve to PASSED_ONE_CHAMBER. Nor is it confirmed to
    always mean PASSED_BOTH everywhere it appears, so it is left
    undetermined (missing > wrong) rather than guessed."""
    action = status_mod.ActionRow(
        action_date=None,
        classification=None,
        description="Passed by the General Assembly",
    )
    assert status_mod.status_for_action(action) != status_mod.PASSED_ONE_CHAMBER

    # Bonus: "Senate and Assembly" reads as bicameral too, and is likewise
    # left undetermined rather than misread as one chamber.
    conjunction = status_mod.ActionRow(
        action_date=None,
        classification=None,
        description="Passed by the Senate and Assembly",
    )
    assert status_mod.status_for_action(conjunction) != status_mod.PASSED_ONE_CHAMBER


def test_status_for_action_passed_one_chamber_positive_shapes():
    """Clause-initial passage wording, with or without "by the", still
    resolves to PASSED_ONE_CHAMBER after the F1/F1a tightening."""
    positives = [
        "Passed Senate",
        "Passed by the Assembly",
        "Read third time and passed House",
        "Passed Senate (36-0)",
        "Passed House 98-0",
        "Passed Senate block vote (40-Y 0-N 0-A)",
        "Passed by House with immediate effect",
        "Passed Senate; referred to House Judiciary Committee",
        "Passed Assembly, amended",
    ]
    for text in positives:
        action = status_mod.ActionRow(action_date=None, classification=None, description=text)
        assert status_mod.status_for_action(action) == status_mod.PASSED_ONE_CHAMBER, text


def test_status_for_action_passed_both_accepts_passed_by_both_houses():
    """deepseek F3: "passed by both houses" (with "by"), not only "passed
    both houses", must resolve to PASSED_BOTH."""
    action = status_mod.ActionRow(
        action_date=None, classification=None, description="Passed by both houses"
    )
    assert status_mod.status_for_action(action) == status_mod.PASSED_BOTH


def test_status_for_action_round4_guards_motion_reading_and_withdrawal_shapes():
    """Round 4 (orchestrator patch after the 9d2d89a panel): shapes that are
    absent from today's corpus but that first-match-wins would otherwise get
    WRONG (the costlier failure class), plus the positives the tightening
    must keep."""
    def got(text):
        return status_mod.status_for_action(
            status_mod.ActionRow(action_date=None, classification=None, description=text)
        )

    # Finding 1: any withdrawal wording that cites the companion's chapter law
    # stays WITHDRAWN -- on main the generic "^withdrawn" caught this.
    assert got("Withdrawn from Consideration because Approved P.L.2025, c.34.") == status_mod.WITHDRAWN
    assert got("Withdrawn Because Approved P.L.2025, c.34.") == status_mod.WITHDRAWN
    assert got("Approved P.L.2025, c.34.") == status_mod.ENACTED

    wrong = [
        # Finding 2: ":" / "(" no longer start a one-chamber clause
        "Motion to table: Passed by the Senate",
        "Motion to table (passed Senate 20-19)",
        # Findings 3/4: numeric ordinals and on/in/at readings
        "Passed Senate 2nd Reading",
        "Passed House 3rd Reading",
        "Passed Senate on second reading",
        # Finding 8: shorthand naming both chambers
        "Passed Senate/House",
    ]
    for text in wrong:
        assert got(text) != status_mod.PASSED_ONE_CHAMBER, text
        assert got(text) != status_mod.PASSED_BOTH, text

    # Positives that must survive the tightening
    assert got("Passed Senate 36-0") == status_mod.PASSED_ONE_CHAMBER
    assert got("Passed by the Senate (40-0)") == status_mod.PASSED_ONE_CHAMBER
    assert got("Read third time and passed House block vote (97-Y 0-N 0-A)") == status_mod.PASSED_ONE_CHAMBER
    assert got("Passed Assembly (Passed Both Houses) (75-0-0)") == status_mod.PASSED_BOTH
    # Finding 5: PASSED_BOTH now honours the same floor-verb clause start
    assert got("Engrossed and passed by both houses") == status_mod.PASSED_BOTH


def test_status_for_action_round5_withdrawal_joiners_and_motion_subjects():
    """Round 5 (orchestrator patch after the 5a79fff panel): semicolon-joined
    withdrawals must stay WITHDRAWN (a regression vs main's generic
    "^withdrawn"), floor-verb phrases only count when clause-initial, and
    "(" / ":" / "," / "-" after the chamber word only terminate a passage
    record when a vote tally follows."""
    def got(text):
        return status_mod.status_for_action(
            status_mod.ActionRow(action_date=None, classification=None, description=text)
        )

    assert got("Withdrawn from Consideration; Approved P.L.2025, c.34.") == status_mod.WITHDRAWN
    assert got("Withdrawn from Consideration, Approved P.L.2025, c.34.") == status_mod.WITHDRAWN
    # The bill's own enactment after an amendment withdrawal is ENACTED
    assert got("Amendment withdrawn and bill Approved P.L.2025, c.34.") == status_mod.ENACTED

    wrong = [
        "Motion to table: Passed by both houses",
        "Amendment ordered and passed by the Senate",
        "Motion ordered and passed by both houses",
        "Passed Senate (2nd Reading)",
        "Passed House: Third Reading",
        "Passed Assembly, Education Committee",
        "Passed Senate, on second reading",
        "Passed Senate - 2nd Reading",
    ]
    for text in wrong:
        assert got(text) not in (status_mod.PASSED_ONE_CHAMBER, status_mod.PASSED_BOTH), text

    # Tallies after punctuation still pass
    assert got("Passed Senate: 36-0") == status_mod.PASSED_ONE_CHAMBER
    assert got("Passed by the Assembly (75-0-0)") == status_mod.PASSED_ONE_CHAMBER
    assert got("Passed Senate - 36-0") == status_mod.PASSED_ONE_CHAMBER
    assert got("Ordered and passed by both houses") == status_mod.PASSED_BOTH


def test_status_for_action_round6_span_anchor_and_tail_guards():
    """Round-6 panel repros: each of these produced a WRONG terminal status on
    the round-5 module where main produced the right one (or None)."""
    def s(text):
        return status_mod.status_for_action(status_mod.ActionRow(action_date=None, classification=None, description=text))

    # Withdrawn guard: unbounded, newline-tolerant span.
    assert s(
        "Withdrawn from Consideration because the identical companion measure, "
        "rather than this bill, was enacted and recorded as Approved P.L.2025, c.34."
    ) == status_mod.WITHDRAWN
    assert s("Withdrawn from consideration.\nApproved P.L.2025, c.34.") == status_mod.WITHDRAWN
    # Enactment record anchored to the bill itself; JR joint resolutions.
    assert s("Approved P.L.2025, c.34.") == status_mod.ENACTED
    assert s("Approved P.L.2025, JR-5.") == status_mod.ENACTED
    assert s("Amendment withdrawn and bill Approved P.L.2025, c.34.") == status_mod.ENACTED
    assert s("Corrective amendment to language approved P.L.2025, c.34.") is None
    # "(" is a clause start only in NJ's compound form.
    assert s("Passed Assembly (Passed Both Houses) (75-0-0)") == status_mod.PASSED_BOTH
    assert s("Passed by the Senate (Passed Both Houses) (40-0)") == status_mod.PASSED_BOTH
    assert s("Motion to table (Passed by both houses)") is None
    assert s("Motion to table (passed Senate 20-19)") is None
    # Tails: "by" only ahead of a vote, "block" only as "block vote", bare
    # number only as a dash tally.
    assert s("Passed Senate by 2/3 vote without amendment") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Senate by voice vote") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Senate, by the Judiciary Committee") is None
    assert s("Passed Senate block vote (40-Y 0-N 0-A)") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed House Block Grant Committee") is None
    assert s("Passed Senate 36-0") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Assembly 75-0-0") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Senate 2026 Session") is None
    assert s("Passed by both houses 36-0") == status_mod.PASSED_BOTH
    assert s("Passed by both houses 2026 Session") is None

    # Round 7: a punctuated integer is not a vote unless it is a dash tally,
    # and "bill approved P.L." only counts after "and" (the withdrawn-and-
    # approved shape) -- never as an embedded citation.
    assert s("Passed Senate, 2026 Session") is None
    assert s("Passed Senate: 2026") is None
    assert s("Passed Senate (2026)") is None
    assert s("Passed by both houses, 2026") is None
    assert s("Passed Senate (36-0)") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Senate, 36-0") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Assembly (Passed Both Houses) (75-0-0)") == status_mod.PASSED_BOTH
    assert s("Passed by both houses (40-0)") == status_mod.PASSED_BOTH
    # Round 8: a session-year range is not a vote tally.
    assert s("Passed Senate (2026-2027)") is None
    assert s("Passed by both houses 2026-2027") is None
    assert s("Passed Senate, 2026-2027 Session") is None
    assert s("Passed Assembly (Passed Both Houses) (999-0-0)") == status_mod.PASSED_BOTH
    # Round 9: the NJ compound entry carries the same tail guard.
    assert s("Passed Assembly (Passed Both Houses) Education Committee") is None
    assert s("Passed Senate (Passed Both Houses) - 2nd Reading") is None
    assert s("Passed Senate (Passed Both Houses)") == status_mod.PASSED_BOTH
    assert s("Passed Senate (Passed Both Houses).") == status_mod.PASSED_BOTH
    assert (
        s("Technical amendment conforming this measure to bill approved P.L.2024, c.12.")
        is None
    )
    assert s("Corrective amendment to the bill approved P.L.2025, c.34.") is None
    assert s("Amendment withdrawn and bill Approved P.L.2025, c.34.") == status_mod.ENACTED
    # Round 10: a tally must end the clause; text after it is not passage.
    assert s("Passed Senate (Passed Both Houses) (40-0) - 2nd Reading") is None
    assert s("Passed Senate (8-4) Judiciary Committee") is None
    assert s("Passed Senate 36-0 2nd Reading") is None
    assert s("Passed by both houses (40-0) Education Committee") is None
    assert s("Passed Senate (36-0).") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Senate (36-0); referred to Assembly") == status_mod.PASSED_ONE_CHAMBER
    assert s("Passed Assembly (Passed Both Houses) (75-0-0)") == status_mod.PASSED_BOTH
    assert s("Passed Assembly 75-0-0.") == status_mod.PASSED_ONE_CHAMBER
    # Round 11: a second parenthetical after the tally is not clause-end.
    assert s("Passed Senate (36-0) (2nd Reading)") is None
    assert s("Passed Senate (8-4) (Judiciary Committee)") is None
    assert s("Passed Assembly (Passed Both Houses) (75-0-0) (2nd Reading)") is None
    assert s("Passed by both houses (40-0) (Education Committee)") is None
    assert s("Passed Senate, 36-0 (referred to committee)") == status_mod.IN_COMMITTEE
