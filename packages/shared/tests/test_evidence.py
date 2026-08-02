"""The snapshot id must mean exactly what it claims and nothing more."""
from billcommons_shared.evidence import (
    DIGEST_VERSION,
    citation_text,
    evidence_digest,
    permalink,
    snapshot_id,
)

BASE = dict(
    bill_id="30e078ec-1bda-45ea-891a-98fc4dfbcc8f",
    jurisdiction_code="HI",
    session_name="Hawaii State Legislature",
    identifier="SR 117",
    title="A resolution",
    status="died_on_adjournment",
    action_ids=["a1", "a2"],
    version_ids=["v1"],
    vote_event_ids=[],
)


def test_row_order_does_not_change_the_snapshot():
    """Postgres returns rows in whatever order it likes. If that leaked into
    the hash, every re-request could report the bill as changed."""
    a = snapshot_id(evidence_digest(**BASE))
    b = snapshot_id(evidence_digest(**{**BASE, "action_ids": ["a2", "a1"]}))
    assert a == b


def test_a_changed_status_changes_the_snapshot():
    """Status is DERIVED, and a change in our own conclusion is exactly what a
    citing reader needs to be told about."""
    a = snapshot_id(evidence_digest(**BASE))
    b = snapshot_id(evidence_digest(**{**BASE, "status": "enacted"}))
    assert a != b


def test_a_new_action_changes_the_snapshot():
    a = snapshot_id(evidence_digest(**BASE))
    b = snapshot_id(evidence_digest(**{**BASE, "action_ids": ["a1", "a2", "a3"]}))
    assert a != b


def test_snapshot_is_version_prefixed():
    """Without the prefix, changing the digest's field set would silently
    invalidate every id ever issued and look to a reader like every bill
    changed on the same day."""
    assert snapshot_id(evidence_digest(**BASE)).startswith(f"{DIGEST_VERSION}_")


def test_citation_names_status_as_derived():
    """The single place a derived conclusion is most likely to be laundered
    into a fact is a footnote, so the caveat rides inside the sentence."""
    text = citation_text(
        identifier="SR 117",
        title="A resolution",
        jurisdiction_name="Hawaii",
        session_name="Hawaii State Legislature",
        status="died_on_adjournment",
        retrieved_at="2026-08-02T21:34:03+00:00",
        snapshot="bcs1_deadbeefdeadbeef",
        bill_id=BASE["bill_id"],
    )
    assert "derived by Bill Commons" in text
    assert "not an official designation" in text
    assert permalink(BASE["bill_id"]) in text
    assert "bcs1_deadbeefdeadbeef" in text


def test_citation_survives_a_bill_with_almost_no_metadata():
    """A packet for a thin record must still produce a usable citation rather
    than a sentence full of 'None'."""
    text = citation_text(
        identifier=None,
        title=None,
        jurisdiction_name=None,
        session_name=None,
        status=None,
        retrieved_at="2026-08-02T21:34:03+00:00",
        snapshot="bcs1_0000000000000000",
        bill_id=BASE["bill_id"],
    )
    assert "None" not in text
    assert BASE["bill_id"] in text
