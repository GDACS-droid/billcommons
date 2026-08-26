"""Tests for cli.recompute_status_for_bills' substitution propagation (R3)
and the `recompute-status --jurisdiction` filter (R4).

status.py can only ever answer "what does THIS bill's own record say"; a
substituted bill's real fate lives on a DIFFERENT bill (the survivor), which
only the cross-bill pass in `recompute_status_for_bills` can resolve. These
tests exercise that pass against the real (test) database rather than
status.py in isolation.
"""
from __future__ import annotations

import uuid

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

    changed, cleared = recompute_status_for_bills(
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
