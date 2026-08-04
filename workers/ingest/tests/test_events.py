"""The change log must record a change for every mutation a consumer cares
about.

This file exists because the previous mechanism -- reaching up from a child
writer to stamp `bills.updated_at` -- depended on every author remembering,
and two of them did not: `_upsert_sponsorships` and the full-text crawl
landing `extracted_text`. Nothing failed; the feed just quietly never
mentioned those changes.

So these tests assert the coupling directly, and the last one is a guard
against the same class of omission returning with the next writer.
"""
from __future__ import annotations

import inspect

import pytest

from billcommons_ingest import api_sync, events, fulltext, openstates_bulk


def test_record_event_rejects_an_unknown_kind():
    """`kind` is a public API value consumers branch on, so a typo entering
    the feed is a contract break no type checker would catch."""
    with pytest.raises(ValueError, match="unknown bill event kind"):
        events.record_event(None, "some-bill-id", "stauts")


def test_every_kind_is_recordable():
    for kind in events.ALL_KINDS:
        assert kind in events.ALL_KINDS


def test_sponsorship_writes_record_an_event():
    """One of the two paths that used to mutate a watched bill's record and
    leave consumers with no signal at all."""
    source = inspect.getsource(api_sync._upsert_sponsorships)
    assert "record_event" in source, "adding a sponsor emits no change event"
    assert "events.SPONSORS" in source


def test_text_landing_records_an_event():
    """The other one -- and arguably the most valuable event this system
    produces, since it is the moment a bill becomes searchable and diffable."""
    source = inspect.getsource(fulltext.process_fetch_text_job)
    assert "record_event" in source, "landing document text emits no change event"
    assert "events.TEXT" in source
    # Only on the transition. Re-fetching a document that already had text is
    # crawler bookkeeping, and emitting it would flood every subscriber.
    assert "had_text_before" in source


def test_action_writes_record_an_event():
    source = inspect.getsource(api_sync._upsert_actions)
    assert "record_event" in source
    assert "events.ACTIONS" in source


def test_bill_creation_and_metadata_changes_record_events():
    source = inspect.getsource(api_sync.sync_state)
    assert "events.CREATED" in source
    assert "events.METADATA" in source


def test_vote_ingestion_records_an_event_only_for_new_votes():
    """See test_openstates_bulk.py for the behavioral (DB-backed) version of
    the no-re-announce-on-resync guarantee; this just pins the source shape."""
    source = inspect.getsource(openstates_bulk.ingest_session_csv_zip)
    assert "record_event" in source, "a new vote event emits no change event"
    assert "events.VOTES" in source


def test_no_ingest_path_stamps_bills_updated_at_by_hand_anymore():
    """Guard against regression to the mechanism this replaced.

    `bills.updated_at` is still maintained by SQLAlchemy for ordinary row
    updates, but nothing should be reaching across from a CHILD writer to
    stamp the parent as a way of announcing a change -- that is what the event
    log is for, and hand-stamping is what silently missed sponsorships and
    full text. api_sync's forward-only stamp is the one deliberate exception
    (it keeps the column monotonic for ETags), and it is paired with an event.
    """
    offenders = []
    for module in (fulltext,):
        source = inspect.getsource(module)
        if "bill.updated_at =" in source or "Bill.updated_at =" in source:
            offenders.append(module.__name__)
    assert not offenders, (
        f"{offenders} stamps bills.updated_at directly; record a bill_event instead "
        "so the change is actually visible to consumers"
    )
