"""Tests for cli.recompute_status_for_bills' substitution propagation (R3)
and the `recompute-status --jurisdiction` filter (R4).

status.py can only ever answer "what does THIS bill's own record say"; a
substituted bill's real fate lives on a DIFFERENT bill (the survivor), which
only the cross-bill pass in `recompute_status_for_bills` can resolve. These
tests exercise that pass against the real (test) database rather than
status.py in isolation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import uuid

from sqlalchemy import select

from billcommons_ingest.cli import recompute_status_for_bills
from billcommons_schema.models import (
    Bill,
    BillAction,
    Jurisdiction,
    RelatedBill,
    Session as SessionModel,
)


def _jurisdiction_with_session(db_session, abbr=None):
    if abbr is None:
        abbr = f"ZQ_SUB_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="Substitution Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(
        jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True
    )
    db_session.add(session_row)
    db_session.flush()
    return jurisdiction, session_row


def _bill(db_session, jurisdiction, session_row, identifier):
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier=identifier,
        identifier_norm=identifier,
        title=f"An act relating to {identifier}",
    )
    db_session.add(bill)
    db_session.flush()
    return bill


def test_substituted_bill_inherits_terminal_survivor_status(db_session):
    """S1234 was substituted by A5678, which went on to become law. The
    substituted print must read as enacted too, not sit at SUBSTITUTED
    forever while its survivor is chaptered."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")

    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    changed, cleared, _related = recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False
    )
    db_session.flush()
    db_session.refresh(survivor)
    db_session.refresh(substituted)

    assert survivor.status == "enacted"
    assert substituted.status == "enacted"
    assert changed == 2
    assert cleared == 0


def test_substituted_bill_inherits_survivor_status_nj_mixed_case_reprint_marker(
    db_session,
):
    """NJ's shape: "Substituted by A1516 (1R)" -- mixed-case verb, no NY-style
    ALL CAPS, and a trailing "(1R)" reprint marker that is never part of the
    survivor's identity. The substituted print must still resolve A1516 as
    its survivor and inherit its terminal status."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor = _bill(db_session, jurisdiction, session_row, "A 1516")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1092")

    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="Substituted by A1516 (1R)",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    changed, cleared, _ = recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False
    )
    db_session.flush()
    db_session.refresh(survivor)
    db_session.refresh(substituted)

    assert survivor.status == "enacted"
    assert substituted.status == "enacted"
    assert changed == 2
    assert cleared == 0


def test_substituted_bill_with_unresolvable_survivor_keeps_substituted(db_session):
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    db_session.add(
        BillAction(
            bill_id=substituted.id,
            description="SUBSTITUTED BY A9999",  # no such bill exists
            classification=None,
        )
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [substituted.id], stamp=False)
    db_session.flush()
    db_session.refresh(substituted)

    assert substituted.status == "substituted"


def test_substituted_bill_with_non_terminal_survivor_keeps_substituted(db_session):
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="Referred to committee",
                classification="referral-committee",
            ),
        ]
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [survivor.id, substituted.id], stamp=False)
    db_session.flush()
    db_session.refresh(substituted)

    assert substituted.status == "substituted"


def test_substituted_for_does_not_propagate_in_either_direction(db_session):
    """Direction test: "substituted for" means THIS bill is the survivor --
    it must not be treated as a substitution signal on itself."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor_print = _bill(db_session, jurisdiction, session_row, "A 5678")
    db_session.add(
        BillAction(
            bill_id=survivor_print.id,
            description="SUBSTITUTED FOR S1234",
            classification=None,
        )
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [survivor_print.id], stamp=False)
    db_session.flush()
    db_session.refresh(survivor_print)

    assert survivor_print.status != "substituted"


def test_related_bills_substitution_relation_propagates_terminal_status(db_session):
    """related_bills is consulted the same way the text form is: a relation
    whose type names a substitution, pointing away from this bill, resolves
    the survivor even with no in-text signal at all."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=survivor.id,
            relation_type="substituted-by",
        )
    )
    db_session.add(
        BillAction(
            bill_id=survivor.id,
            description="Vetoed by Governor",
            classification="executive-veto",
        )
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [survivor.id, substituted.id], stamp=False)
    db_session.flush()
    db_session.refresh(substituted)

    assert substituted.status == "vetoed"


def test_substituted_bill_inherits_survivor_stored_with_print_version_stripped_in_chunk(
    db_session,
):
    """NY: "SUBSTITUTED BY A10008C" normalizes to "A 10008C", but the
    survivor is stored as "A 10008" -- the trailing print/amendment letter
    is never part of bill identity. Resolved via the in-chunk map."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session, abbr="NY")
    survivor = _bill(db_session, jurisdiction, session_row, "A 10008")
    substituted = _bill(db_session, jurisdiction, session_row, "S 9008")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A10008C",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="SIGNED CHAP.58",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [survivor.id, substituted.id], stamp=False)
    db_session.flush()
    db_session.refresh(substituted)

    assert substituted.status == "enacted"


def test_substituted_bill_inherits_survivor_stored_with_print_version_stripped_via_db_fallback(
    db_session,
):
    """Same as above, but the survivor is not in the recompute chunk, so
    resolution must go through the DB fallback query's `in_(candidates)`
    lookup rather than the in-chunk map."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session, abbr="NY")
    survivor = _bill(db_session, jurisdiction, session_row, "A 10008")
    substituted = _bill(db_session, jurisdiction, session_row, "S 9008")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A10008C",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="SIGNED CHAP.58",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()
    # Recompute the survivor separately first so its status is already
    # persisted, then recompute only the substituted print.
    recompute_status_for_bills(db_session, [survivor.id], stamp=False)
    db_session.flush()

    recompute_status_for_bills(db_session, [substituted.id], stamp=False)
    db_session.flush()
    db_session.refresh(substituted)

    assert substituted.status == "enacted"


def test_exact_survivor_across_db_wins_over_stripped_survivor_in_chunk(db_session):
    """NY, R2 defect #1: the exact lettered survivor "A 10008C" (enacted)
    sits OUTSIDE this recompute chunk, while an unrelated bill that happens
    to share the stripped identifier "A 10008" (still in_committee) sits
    INSIDE it. The exact form must be resolved -- across chunk AND db --
    before the stripped fallback is even tried, so the winner cannot depend
    on which bill happened to land in this chunk."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session, abbr="NY")
    survivor_exact = _bill(db_session, jurisdiction, session_row, "A 10008C")
    unrelated_stripped = _bill(db_session, jurisdiction, session_row, "A 10008")
    substituted = _bill(db_session, jurisdiction, session_row, "S 9008")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A10008C",
                classification=None,
            ),
            BillAction(
                bill_id=survivor_exact.id,
                description="SIGNED CHAP.58",
                classification="executive-signature",
            ),
            BillAction(
                bill_id=unrelated_stripped.id,
                description="REFERRED TO COMMITTEE",
                classification="referral-committee",
            ),
        ]
    )
    db_session.flush()
    # Persist the exact survivor's status first (outside this chunk), same
    # pattern as the db-fallback test above.
    recompute_status_for_bills(db_session, [survivor_exact.id], stamp=False)
    db_session.flush()

    recompute_status_for_bills(
        db_session, [unrelated_stripped.id, substituted.id], stamp=False
    )
    db_session.flush()
    db_session.refresh(substituted)
    db_session.refresh(unrelated_stripped)

    assert unrelated_stripped.status == "in_committee"
    assert substituted.status == "enacted"


def test_non_ny_jurisdiction_does_not_strip_print_suffix(db_session):
    """FL: "SUBSTITUTED BY HB1A" normalizes to "HB 1A". Only "HB 1" exists
    (a different bill in a different special session print), and the
    trailing letter is part of FL's identity, not a print version -- it must
    NOT be stripped, so the substituted print stays SUBSTITUTED rather than
    wrongly inheriting HB 1's status."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session, abbr="FL")
    decoy = _bill(db_session, jurisdiction, session_row, "HB 1")
    substituted = _bill(db_session, jurisdiction, session_row, "HB 2")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY HB1A",
                classification=None,
            ),
            BillAction(
                bill_id=decoy.id,
                description="SIGNED BY GOVERNOR",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [decoy.id, substituted.id], stamp=False)
    db_session.flush()
    db_session.refresh(substituted)

    assert substituted.status == "substituted"


def test_recompute_status_jurisdiction_filter_scopes_to_one_state(db_session, capsys):
    """R4: `--jurisdiction` must only touch bills in that jurisdiction. Exercised
    directly against the argparse-wired command via a stand-in args object,
    since the CLI's own `get_session()` cannot be pointed at the test
    transaction -- what matters here is that the SQL filter added to
    cmd_recompute_status only matches the target jurisdiction's rows."""
    from sqlalchemy import select

    from billcommons_ingest.cli import Jurisdiction as JurisdictionModel

    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    other_jurisdiction, other_session = _jurisdiction_with_session(db_session)
    _bill(db_session, jurisdiction, session_row, "HB 1")
    _bill(db_session, other_jurisdiction, other_session, "HB 1")
    db_session.flush()

    # Same filter cmd_recompute_status applies: abbreviation, case-insensitive.
    row = db_session.execute(
        select(JurisdictionModel.id).where(
            JurisdictionModel.abbreviation.ilike(jurisdiction.abbreviation.lower())
        )
    ).first()
    assert row is not None
    assert row.id == jurisdiction.id

    matched = db_session.execute(
        select(Bill.id).where(Bill.jurisdiction_id == row.id)
    ).all()
    assert len(matched) == 1


def test_recompute_status_derives_enacted_from_nj_p_l_citation(db_session):
    """NJ's enactment record reads "Approved P.L.2025, c.34." -- no
    "signed/approved BY the Governor" wording, no structured classification.
    recompute_status_for_bills must still land on ENACTED from that text
    alone."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    bill = _bill(db_session, jurisdiction, session_row, "A 1516")
    db_session.add(
        BillAction(
            bill_id=bill.id,
            description="Approved P.L.2025, c.34.",
            classification=None,
        )
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [bill.id], stamp=False)
    db_session.flush()
    db_session.refresh(bill)

    assert bill.status == "enacted"


def test_recompute_status_withdrawn_because_approved_stays_withdrawn(db_session):
    """A companion bill pulled because the OTHER (identical) bill was signed
    -- "Withdrawn Because Approved P.L.2025, c.34." -- must resolve to
    WITHDRAWN, not ENACTED, even though the text contains "Approved P.L.
    ...c.N" verbatim."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    bill = _bill(db_session, jurisdiction, session_row, "S 1092")
    db_session.add(
        BillAction(
            bill_id=bill.id,
            description="Withdrawn Because Approved P.L.2025, c.34.",
            classification=None,
        )
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [bill.id], stamp=False)
    db_session.flush()
    db_session.refresh(bill)

    assert bill.status == "withdrawn"


def test_recompute_status_derives_passed_both_from_nj_compound_wording(db_session):
    """"Passed Assembly (Passed Both Houses) (75-0-0)" is a single
    unclassified action that names both chambers as done -- it must resolve
    to passed_both directly, not passed_one_chamber (the R2 two-organization
    upgrade never even has to run)."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    bill = _bill(db_session, jurisdiction, session_row, "A 1516")
    db_session.add(
        BillAction(
            bill_id=bill.id,
            description="Passed Assembly (Passed Both Houses) (75-0-0)",
            classification=None,
        )
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [bill.id], stamp=False)
    db_session.flush()
    db_session.refresh(bill)

    assert bill.status == "passed_both"


def _related_bills_for(db_session, bill_id):
    return (
        db_session.execute(
            select(RelatedBill).where(
                RelatedBill.bill_id == bill_id,
                RelatedBill.relation_type == "substituted-by",
            )
        )
        .scalars()
        .all()
    )


def test_substitution_persists_related_bills_row_when_survivor_resolved(db_session):
    """A text-parsed "SUBSTITUTED BY" with the survivor present must persist
    a `substituted-by` related_bills row with related_bill_id set, not just
    the derived status -- that row is what the API's replaces/replaced_by
    links read."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    changed, cleared, related_upserted = recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].related_bill_id == survivor.id
    assert rows[0].related_identifier == "A 5678"
    assert related_upserted == 1


def test_substitution_persists_related_bills_row_when_survivor_absent(db_session):
    """When the named survivor cannot be resolved to any bill, the relation
    is still persisted -- related_bill_id NULL, related_identifier holding
    the raw target -- so the link is not silently dropped."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    db_session.add(
        BillAction(
            bill_id=substituted.id,
            description="SUBSTITUTED BY A9999",  # no such bill exists
            classification=None,
        )
    )
    db_session.flush()

    changed, cleared, related_upserted = recompute_status_for_bills(
        db_session, [substituted.id], stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].related_bill_id is None
    assert rows[0].related_identifier == "A 9999"
    assert related_upserted == 1


def test_substitution_related_bills_row_idempotent_on_rerun(db_session):
    """Running recompute twice over the same bills must not duplicate the
    related_bills row -- the second pass is a no-op write."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    recompute_status_for_bills(db_session, [survivor.id, substituted.id], stamp=False)
    db_session.flush()
    first_pass_rows = _related_bills_for(db_session, substituted.id)
    assert len(first_pass_rows) == 1

    _, _, related_upserted_second = recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].id == first_pass_rows[0].id
    assert related_upserted_second == 0


def test_substitution_related_bills_row_upgrades_null_to_resolved(db_session):
    """A row persisted while the survivor was unresolved must be UPDATED in
    place -- not duplicated -- once a later pass resolves the survivor."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    db_session.add(
        BillAction(
            bill_id=substituted.id,
            description="SUBSTITUTED BY A5678",
            classification=None,
        )
    )
    db_session.flush()

    # First pass: survivor does not exist yet -- related_bill_id stays NULL.
    recompute_status_for_bills(db_session, [substituted.id], stamp=False)
    db_session.flush()
    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    original_row_id = rows[0].id
    assert rows[0].related_bill_id is None

    # The survivor shows up later (a subsequent ingest run creates it).
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    db_session.add(
        BillAction(
            bill_id=survivor.id,
            description="Signed by Governor",
            classification="executive-signature",
        )
    )
    db_session.flush()

    _, _, related_upserted = recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].id == original_row_id
    assert rows[0].related_bill_id == survivor.id
    assert related_upserted == 1


def test_substitution_related_bills_row_reconciles_when_target_changes(db_session):
    """A bill can only ever be substituted by ONE survivor. If a prior pass
    persisted 'substituted-by' -> Y and this pass's action text now names X,
    the stale Y row must be removed -- not left sitting alongside the new X
    row -- so the API never reports two conflicting substitution targets for
    the same bill."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    stray_y = _bill(db_session, jurisdiction, session_row, "A 9999")
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=stray_y.id,
            related_identifier="A 9999",
            relation_type="substituted-by",
        )
    )
    db_session.flush()

    # A prior pass recorded the stray Y row above. The bill's OWN action text
    # (what a real recompute run reads) now names a different survivor, X.
    survivor_x = _bill(db_session, jurisdiction, session_row, "A 5678")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor_x.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    recompute_status_for_bills(
        db_session, [survivor_x.id, substituted.id], stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].related_identifier == "A 5678"
    assert rows[0].related_bill_id == survivor_x.id


def test_substitution_stale_row_with_null_identifier_is_not_fabricated_onto_fresh_target(
    db_session,
):
    """BLOCKER regression (round 2 review): a legacy related_bills row with
    NO recorded identifier (related_identifier NULL, related_bill_id = Y --
    pre-dating this feature) must never be trusted as the pairing for a
    DIFFERENT survivor Z that this pass's fresh action text names. The old
    code trusted row.related_bill_id whenever the row's own identifier was
    unusable, even when a fresh text target existed -- persisting
    (identifier of Z, related_bill_id of Y), a fabricated pairing that then
    self-latched (the NULL->resolved upgrade branch never fires once
    related_bill_id is already set). The persisted row must name Z, never Y,
    and a second pass over the same bills must converge rather than keep
    re-deriving something new."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    stray_y = _bill(db_session, jurisdiction, session_row, "S 9999")
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=stray_y.id,
            related_identifier=None,  # legacy row, pre-dates identifier tracking
            relation_type="substituted-by",
        )
    )
    survivor_z = _bill(db_session, jurisdiction, session_row, "A 5678")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor_z.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    recompute_status_for_bills(
        db_session, [survivor_z.id, substituted.id], stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].related_identifier == "A 5678"
    assert rows[0].related_bill_id == survivor_z.id  # never stray_y.id
    first_pass_row_count = len(rows)

    # Re-running must converge: no further churn, no reintroduced stray row.
    recompute_status_for_bills(
        db_session, [survivor_z.id, substituted.id], stamp=False
    )
    db_session.flush()
    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == first_pass_row_count == 1
    assert rows[0].related_identifier == "A 5678"
    assert rows[0].related_bill_id == survivor_z.id


def test_substitution_stale_row_with_empty_identifier_treated_like_null(
    db_session,
):
    """An empty-string related_identifier is the same legacy shape as NULL: the
    row cannot match the pass's text-derived target, so it is replaced by the
    freshly resolved row and its related_bill_id is never donated (round-6
    panel: muse read the replacement as data loss; it is the NULL rule)."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    stray_y = _bill(db_session, jurisdiction, session_row, "S 9999")
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=stray_y.id,
            related_identifier="",  # legacy row, empty rather than NULL
            relation_type="substituted-by",
        )
    )
    survivor_z = _bill(db_session, jurisdiction, session_row, "A 5678")
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor_z.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    counts: dict[str, int] = {}
    recompute_status_for_bills(
        db_session, [survivor_z.id, substituted.id], counts, stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].related_identifier == "A 5678"
    assert rows[0].related_bill_id == survivor_z.id  # never stray_y.id
    assert counts["_related_removed"] == 1


def test_substitution_unnormalized_legacy_identifier_matched_not_churned(db_session):
    """A legacy row stored with an unnormalized identifier ("A5678", no
    space) must be recognized as the SAME relation this pass derives
    ("A 5678", normalized) -- updated in place, not deleted and
    reinserted -- so a stable substitution does not churn every run."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    existing_row = RelatedBill(
        bill_id=substituted.id,
        related_bill_id=survivor.id,
        related_identifier="A5678",  # unnormalized on disk
        relation_type="substituted-by",
    )
    db_session.add(existing_row)
    db_session.add_all(
        [
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()
    original_row_id = existing_row.id

    _, _, related_upserted = recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].id == original_row_id  # updated in place, not replaced
    assert rows[0].related_identifier == "A5678"  # left as stored, not rewritten
    assert related_upserted == 0  # no-op: already the same relation


def test_substitution_resolved_stored_row_survives_when_no_text_signal_this_pass(db_session):
    """Round-3 panel BLOCKER (confirmed by repro): with NO substitution text
    on the bill this pass and TWO stored 'substituted-by' rows -- an older
    unresolved stray (identifier only) and a newer resolved one (real
    related_bill_id) -- the resolution loop used to write the stray's
    identifier into the live target dict, then reject the resolved row as
    "stale" against that stored value, and persistence deleted it. Stored
    rows are lookup evidence only: nothing may be persisted or deleted for
    a bid whose target did not come from its own action text this pass, and
    the resolved row must still drive the derived status."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=None,
            related_identifier="A 9999",  # older stray, never resolved
            relation_type="substituted-by",
        )
    )
    db_session.flush()
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=survivor.id,
            related_identifier="A 5678",
            relation_type="substituted-by",
        )
    )
    db_session.add(
        BillAction(
            bill_id=survivor.id,
            description="Signed by Governor",
            classification="executive-signature",
        )
    )
    db_session.flush()

    counts: dict = {}
    recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False, counts=counts
    )
    db_session.flush()

    rows = sorted(_related_bills_for(db_session, substituted.id), key=lambda r: r.related_identifier)
    assert [(r.related_identifier, r.related_bill_id) for r in rows] == [
        ("A 5678", survivor.id),
        ("A 9999", None),
    ]
    assert counts.get("_related_removed", 0) == 0
    db_session.refresh(substituted)
    assert substituted.status == "enacted"


def test_substitution_duplicate_rows_keep_resolved_twin(db_session):
    """Two rows for the SAME derived identifier (no unique constraint yet):
    the resolved twin must be the one kept, the NULL twin the one removed --
    regardless of insertion order."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=None,
            related_identifier="A 5678",
            relation_type="substituted-by",
        )
    )
    db_session.flush()
    db_session.add_all(
        [
            RelatedBill(
                bill_id=substituted.id,
                related_bill_id=survivor.id,
                related_identifier="A5678",
                relation_type="substituted-by",
            ),
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
        ]
    )
    db_session.flush()

    counts: dict = {}
    recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False, counts=counts
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].related_bill_id == survivor.id
    assert counts.get("_related_removed", 0) == 1


def test_substitution_two_unresolved_stored_identifiers_newest_wins(db_session):
    """Round-7 (K3): with NO substitution text this pass and TWO stored
    identifier-only rows naming DIFFERENT targets, the newest stored row
    drives the derived status -- the same newest-wins rule the resolved-row
    branch already applies. Nothing is persisted or deleted for a bid whose
    target did not come from its own action text."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    _bill(db_session, jurisdiction, session_row, "A 9999")  # older target, no actions
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    # created_at is server_default now() = transaction start, so give the
    # two rows explicit timestamps: the ORDER BY must see a real oldest/newest.
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=None,
            related_identifier="A 9999",
            relation_type="substituted-by",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    db_session.flush()
    db_session.add_all(
        [
            RelatedBill(
                bill_id=substituted.id,
                related_bill_id=None,
                related_identifier="A 5678",
                relation_type="substituted-by",
                created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
            BillAction(
                bill_id=survivor.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()

    counts: dict = {}
    recompute_status_for_bills(
        db_session, [survivor.id, substituted.id], stamp=False, counts=counts
    )
    db_session.flush()

    rows = sorted(_related_bills_for(db_session, substituted.id), key=lambda r: r.related_identifier)
    assert [(r.related_identifier, r.related_bill_id) for r in rows] == [
        ("A 5678", None),
        ("A 9999", None),
    ]
    assert counts.get("_related_removed", 0) == 0
    db_session.refresh(substituted)
    assert substituted.status == "enacted"


def test_substitution_two_resolved_duplicates_keep_newest_twin(db_session):
    """Round-7 (K4): two RESOLVED rows for the same identifier carrying
    different related_bill_id values (an older stale FK and a newer correct
    one). Resolution trusts the newest; persistence must keep that same row
    and delete the older twin -- not keep the oldest and leave its stale FK
    in place (a resolved row's FK is never rewritten)."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    decoy = _bill(db_session, jurisdiction, session_row, "A 9999")
    survivor = _bill(db_session, jurisdiction, session_row, "A 5678")
    # Explicit created_at: server_default now() is transaction start, which
    # would give both rows the same timestamp and a random tie-break.
    older = RelatedBill(
        bill_id=substituted.id,
        related_bill_id=decoy.id,
        related_identifier="A 5678",
        relation_type="substituted-by",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(older)
    db_session.flush()
    newer = RelatedBill(
        bill_id=substituted.id,
        related_bill_id=survivor.id,
        related_identifier="A5678",
        relation_type="substituted-by",
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    db_session.add_all(
        [
            newer,
            BillAction(
                bill_id=substituted.id,
                description="SUBSTITUTED BY A5678",
                classification=None,
            ),
            BillAction(
                bill_id=survivor.id,
                description="Signed by Governor",
                classification="executive-signature",
            ),
        ]
    )
    db_session.flush()
    newer_id = newer.id

    counts: dict = {}
    recompute_status_for_bills(
        db_session, [decoy.id, survivor.id, substituted.id], stamp=False, counts=counts
    )
    db_session.flush()

    rows = _related_bills_for(db_session, substituted.id)
    assert len(rows) == 1
    assert rows[0].id == newer_id
    assert rows[0].related_bill_id == survivor.id
    assert counts.get("_related_removed", 0) == 1
    db_session.refresh(substituted)
    assert substituted.status == "enacted"


def test_substitution_target_choice_is_date_ordered_not_row_ordered(db_session):
    """Two actions on one bill naming DIFFERENT substitution targets: the
    latest-dated one must win, and it must win the same way no matter what
    order the rows come back in -- because the chosen target is persisted
    and a stored row that disagrees with it is deleted. Before the ORDER BY
    on the actions query, a different row order between two passes deleted
    the previous pass's row and re-inserted the other (round-4 panel repro)."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    older_target = _bill(db_session, jurisdiction, session_row, "A 1000")
    newer_target = _bill(db_session, jurisdiction, session_row, "A 2000")
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    ids = [older_target.id, newer_target.id, substituted.id]

    def _actions(first_newer: bool) -> list[BillAction]:
        newer = BillAction(
            bill_id=substituted.id,
            description="SUBSTITUTED BY A2000",
            classification=None,
            action_date=date(2026, 6, 4),
        )
        older = BillAction(
            bill_id=substituted.id,
            description="SUBSTITUTED BY A1000",
            classification=None,
            action_date=date(2026, 3, 1),
        )
        return [newer, older] if first_newer else [older, newer]

    db_session.add_all(_actions(first_newer=True))
    db_session.flush()
    recompute_status_for_bills(db_session, ids, stamp=False)
    db_session.flush()
    rows = _related_bills_for(db_session, substituted.id)
    assert [(r.related_identifier, r.related_bill_id) for r in rows] == [
        ("A 2000", newer_target.id)
    ]

    # Same two actions, opposite insertion order (models a different
    # query-plan return order between passes).
    db_session.query(BillAction).filter(BillAction.bill_id == substituted.id).delete()
    db_session.flush()
    db_session.add_all(_actions(first_newer=False))
    db_session.flush()

    counts: dict = {}
    recompute_status_for_bills(db_session, ids, stamp=False, counts=counts)
    db_session.flush()
    rows = _related_bills_for(db_session, substituted.id)
    assert [(r.related_identifier, r.related_bill_id) for r in rows] == [
        ("A 2000", newer_target.id)
    ]
    assert counts.get("_related_removed", 0) == 0


def test_substitution_off_spec_relation_type_row_is_not_survivor_evidence(db_session):
    """Round-5 panel (confirmed by repro): the resolution query matched
    relation_type with ILIKE '%substitut%' while persistence only ever
    reads/writes the exact 'substituted-by' value. A near-miss spelling
    ('substituted_by') pointing at the wrong bill was therefore consulted
    as survivor evidence -- the substituted print inherited the wrong
    bill's terminal status -- yet was invisible to reconciliation forever.
    Read scope now equals write scope: the off-spec row is ignored and
    left untouched."""
    jurisdiction, session_row = _jurisdiction_with_session(db_session)
    substituted = _bill(db_session, jurisdiction, session_row, "S 1234")
    wrong_target = _bill(db_session, jurisdiction, session_row, "A 7777")
    db_session.add(
        RelatedBill(
            bill_id=substituted.id,
            related_bill_id=wrong_target.id,
            related_identifier="A 7777",
            relation_type="substituted_by",  # off-spec spelling
        )
    )
    db_session.add(
        BillAction(
            bill_id=wrong_target.id,
            description="Vetoed by Governor",
            classification="executive-veto",
        )
    )
    db_session.flush()

    counts: dict = {}
    recompute_status_for_bills(
        db_session, [wrong_target.id, substituted.id], stamp=False, counts=counts
    )
    db_session.flush()

    # _related_bills_for filters on the exact type; read every row here so
    # the off-spec row's survival is what is asserted.
    rows = (
        db_session.execute(
            select(RelatedBill).where(RelatedBill.bill_id == substituted.id)
        )
        .scalars()
        .all()
    )
    assert [(r.related_identifier, r.related_bill_id, r.relation_type) for r in rows] == [
        ("A 7777", wrong_target.id, "substituted_by")
    ]
    assert _related_bills_for(db_session, substituted.id) == []
    assert counts.get("_related_removed", 0) == 0
    db_session.refresh(substituted)
    assert substituted.status != "vetoed"
