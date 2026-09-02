"""An action revised upstream must be written, re-derived, and announced.

Open States revises actions in place -- 42% of them arrive with no
classification at all and are classified later, which is exactly the event that
moves a bill's derived status. `_upsert_actions` originally keyed existing
actions on `(description, order)` and only ever INSERTED, so a reclassification
was a three-way silent failure: the child row was never updated, the status was
never re-derived, and no change event was emitted. A signed bill kept reporting
`introduced` forever with nothing logged anywhere.

Three reviewers on the model panel flagged this independently. None of the
existing tests caught it, because every one of them exercised a FIRST sync
against an empty table -- the only scenario in which insert-only is correct.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from billcommons_ingest import api_sync, events


class _FakeQueryResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows


class _FakeSession:
    """Just enough Session to drive _upsert_actions without a database."""

    def __init__(self, existing_actions):
        self._existing = existing_actions
        self.added = []
        self.flushed = 0

    def execute(self, _stmt):
        return _FakeQueryResult(list(self._existing))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed += 1


class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture()
def bill():
    return _Row(id="bill-1", updated_at=None, latest_action_date=None, latest_action_text=None)


def _payload(description, order_date, classification):
    return {"description": description, "date": order_date, "classification": classification}


def test_a_reclassified_action_is_written_and_announced(bill, monkeypatch):
    """The headline case: an action that was unclassified is later classified
    `executive-signature`. The bill has been signed."""
    recorded = []
    monkeypatch.setattr(
        events, "record_event", lambda db, bid, kind, detail=None: recorded.append((kind, detail))
    )

    existing = [
        _Row(
            description="Signed by Governor",
            order=0,
            action_date=date(2026, 5, 1),
            classification=None,
            retrieved_at=None,
        )
    ]
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")
    now = datetime.now(timezone.utc)

    api_sync._upsert_actions(
        db,
        bill,
        [_payload("Signed by Governor", "2026-05-01", ["executive-signature"])],
        result,
        now,
    )

    assert existing[0].classification == "executive-signature", "revision was not written"
    assert db.added == [], "a revision must not duplicate the action as a new row"
    assert bill.id in result.touched_bill_ids, "revision did not mark the bill for status re-derivation"
    assert recorded, "revision emitted no change event -- consumers never hear about it"
    kind, detail = recorded[0]
    assert kind == events.ACTIONS
    assert "revised" in detail


def test_a_corrected_action_date_is_written(bill, monkeypatch):
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    existing = [
        _Row(
            description="Referred to committee",
            order=0,
            action_date=date(2026, 1, 5),
            classification="referral-committee",
            retrieved_at=None,
        )
    ]
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [_payload("Referred to committee", "2026-01-12", ["referral-committee"])],
        result,
        datetime.now(timezone.utc),
    )
    assert existing[0].action_date == date(2026, 1, 12)
    assert bill.id in result.touched_bill_ids


def test_an_unchanged_action_is_not_reported_as_a_change(bill, monkeypatch):
    """The other half of the contract. A nightly sync over a quiet corpus must
    stay silent -- if every sync announced every bill, the feed would be noise
    and the status recompute would re-derive all 209k bills every night."""
    recorded = []
    monkeypatch.setattr(
        events, "record_event", lambda db, bid, kind, detail=None: recorded.append(kind)
    )
    existing = [
        _Row(
            description="Referred to committee",
            order=0,
            action_date=date(2026, 1, 5),
            classification="referral-committee",
            retrieved_at=None,
        )
    ]
    bill.latest_action_date = date(2026, 1, 5)
    bill.latest_action_text = "Referred to committee"
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [_payload("Referred to committee", "2026-01-05", ["referral-committee"])],
        result,
        datetime.now(timezone.utc),
    )
    assert recorded == [], "an unchanged sync emitted a change event"
    assert result.touched_bill_ids == set()
    assert db.added == []


def test_a_genuinely_new_action_still_inserts(bill, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        events, "record_event", lambda db, bid, kind, detail=None: recorded.append(detail)
    )
    db = _FakeSession([])
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db, bill, [_payload("Introduced", "2026-01-01", [])], result, datetime.now(timezone.utc)
    )
    assert len(db.added) == 1
    assert result.actions == 1
    assert "added" in recorded[0]


def test_shifted_or_reversed_action_order_reuses_and_reorders_each_matching_row(bill, monkeypatch):
    """CSV `order` and v3-list position are not one shared natural key.

    The stored rows model a bulk import with one-based orders; the incoming
    list is reversed.  Content matching must reuse both rows, derive the true
    latest action by date, and announce the scalar repair even though no child
    row was added or revised.
    """
    recorded = []
    monkeypatch.setattr(
        events, "record_event", lambda db, bid, kind, detail=None: recorded.append((kind, detail))
    )
    existing = [
        _Row(
            description="Introduced",
            order=1,
            action_date=date(2026, 1, 5),
            classification="introduction",
            retrieved_at=None,
        ),
        _Row(
            description="Passed House",
            order=2,
            action_date=date(2026, 3, 1),
            classification="passage",
            retrieved_at=None,
        ),
    ]
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [
            _payload("Passed House", "2026-03-01", ["passage"]),
            _payload("Introduced", "2026-01-05", ["introduction"]),
        ],
        result,
        datetime.now(timezone.utc),
    )

    assert db.added == []
    assert result.actions == 0
    assert bill.latest_action_date == date(2026, 3, 1)
    assert bill.latest_action_text == "Passed House"
    assert bill.id in result.touched_bill_ids
    assert [row.order for row in existing] == [1, 0]
    assert recorded == [(events.ACTIONS, "1 action(s) revised")]


def test_same_day_reorder_repairs_latest_action(bill, monkeypatch):
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    existing = [
        _Row(description="Committee passage", order=2, action_date=date(2026, 3, 1), classification="passage", retrieved_at=None),
        _Row(description="Referred to floor", order=1, action_date=date(2026, 3, 1), classification="referral", retrieved_at=None),
    ]
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [
            {**_payload("Committee passage", "2026-03-01", ["passage"]), "order": 1},
            {**_payload("Referred to floor", "2026-03-01", ["referral"]), "order": 2},
        ],
        result,
        datetime.now(timezone.utc),
    )

    assert [row.order for row in existing] == [1, 2]
    assert bill.latest_action_text == "Referred to floor"
    assert bill.id in result.touched_bill_ids


def test_string_action_classification_is_not_joined_character_by_character(bill, monkeypatch):
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    db = _FakeSession([])
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [{"description": "Signed", "date": "2026-03-01", "classification": "executive-signature"}],
        result,
        datetime.now(timezone.utc),
    )

    assert db.added[0].classification == "executive-signature"


def test_new_action_preserves_upstream_order_when_present(bill, monkeypatch):
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    db = _FakeSession([])
    result = api_sync.ApiSyncResult(state="XX")
    payload = _payload("Passed Senate", "2026-03-02", ["passage"])
    payload["order"] = 27

    api_sync._upsert_actions(db, bill, [payload], result, datetime.now(timezone.utc))

    assert len(db.added) == 1
    assert db.added[0].order == 27


def test_boolean_order_is_not_treated_as_an_integer(bill, monkeypatch):
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    db = _FakeSession([])
    result = api_sync.ApiSyncResult(state="XX")
    payload = _payload("Introduced", "2026-01-01", ["introduction"])
    payload["order"] = True

    api_sync._upsert_actions(db, bill, [payload], result, datetime.now(timezone.utc))

    assert len(db.added) == 1
    assert db.added[0].order == 0


def test_ambiguous_same_description_inserts_instead_of_revising_the_wrong_row(bill, monkeypatch):
    """Without an upstream action id, two same-description candidates cannot
    safely be distinguished after a date correction; preserve both."""
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    existing = [
        _Row(
            description="Read first time",
            order=0,
            action_date=date(2026, 1, 5),
            classification="reading-1",
            retrieved_at=None,
        ),
        _Row(
            description="Read first time",
            order=1,
            action_date=date(2026, 1, 6),
            classification="reading-1",
            retrieved_at=None,
        ),
    ]
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [_payload("Read first time", "2026-01-07", ["reading-1"])],
        result,
        datetime.now(timezone.utc),
    )

    assert len(db.added) == 1
    assert result.actions == 1
    assert [row.action_date for row in existing] == [date(2026, 1, 5), date(2026, 1, 6)]


def test_ambiguous_exact_duplicates_do_not_revise_an_arbitrary_row(bill, monkeypatch):
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    existing = [
        _Row(
            description="Passed Senate",
            order=8,
            action_date=date(2026, 3, 2),
            classification=None,
            retrieved_at=None,
        ),
        _Row(
            description="Passed Senate",
            order=9,
            action_date=date(2026, 3, 2),
            classification="reading-3",
            retrieved_at=None,
        ),
    ]
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [_payload("Passed Senate", "2026-03-02", ["passage"])],
        result,
        datetime.now(timezone.utc),
    )

    assert [row.classification for row in existing] == [None, "reading-3"]
    assert len(db.added) == 1
    assert db.added[0].classification == "passage"


def test_ambiguous_exact_duplicate_with_same_classification_is_idempotent(bill, monkeypatch):
    recorded = []
    monkeypatch.setattr(events, "record_event", lambda *a, **k: recorded.append(a))
    existing = [
        _Row(
            description="Passed Senate",
            order=8,
            action_date=date(2026, 3, 2),
            classification="passage",
            retrieved_at=None,
        ),
        _Row(
            description="Passed Senate",
            order=9,
            action_date=date(2026, 3, 2),
            classification="passage",
            retrieved_at=None,
        ),
    ]
    bill.latest_action_date = date(2026, 3, 2)
    bill.latest_action_text = "Passed Senate"
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(
        db,
        bill,
        [_payload("Passed Senate", "2026-03-02", ["passage"])],
        result,
        datetime.now(timezone.utc),
    )

    assert db.added == []
    assert result.touched_bill_ids == set()
    assert recorded == []


def test_empty_incoming_actions_still_repairs_latest_from_local_history(bill, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        events, "record_event", lambda db, bid, kind, detail=None: recorded.append((kind, detail))
    )
    existing = [
        _Row(
            description="Introduced",
            order=1,
            action_date=date(2026, 1, 5),
            classification="introduction",
            retrieved_at=None,
        ),
        _Row(
            description="Passed Senate",
            order=2,
            action_date=date(2026, 3, 2),
            classification="passage",
            retrieved_at=None,
        ),
    ]
    db = _FakeSession(existing)
    result = api_sync.ApiSyncResult(state="XX")

    api_sync._upsert_actions(db, bill, [], result, datetime.now(timezone.utc))

    assert bill.latest_action_date == date(2026, 3, 2)
    assert bill.latest_action_text == "Passed Senate"
    assert bill.id in result.touched_bill_ids
    assert recorded == [(events.ACTIONS, "latest action recalculated")]


def test_updated_at_never_moves_backwards(bill, monkeypatch):
    """`retrieved_at` is minted once at the start of a sync that can run for
    minutes, so it may be older than a value already on the row. Moving the
    timestamp backwards would drop the bill behind cursors that had already
    passed it -- published as changed to nobody."""
    monkeypatch.setattr(events, "record_event", lambda *a, **k: None)
    later = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 7, 27, 11, 0, tzinfo=timezone.utc)
    bill.updated_at = later

    db = _FakeSession([])
    api_sync._upsert_actions(
        db, bill, [_payload("Introduced", "2026-01-01", [])], api_sync.ApiSyncResult(state="XX"), earlier
    )
    assert bill.updated_at == later, "updated_at moved backwards"
