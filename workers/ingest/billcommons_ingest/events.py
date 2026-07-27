"""Record entries in the `bill_events` change log.

Every ingest path that mutates a bill or one of its children calls
`record_event` in the SAME transaction as the mutation. That coupling is the
whole point: an event and the change it describes commit together or not at
all, so the log cannot claim a change that was rolled back, and a committed
change cannot go unannounced.

This replaces reaching up to stamp `bills.updated_at` from child writers,
which had already been missed twice (sponsorships; the full-text crawl landing
extracted_text) precisely because it depended on every author remembering.

KINDS -- deliberately coarse. A consumer wants to know which of its cached
collections to refetch, not a field-level patch:

    created   the bill first appeared in this corpus
    status    derived status moved (detail: "in_committee -> enacted")
    actions   one or more actions were added
    sponsors  one or more sponsorships were added
    text      document text became available (the bill is now searchable and
              diffable -- for a policy tracker, usually the event that matters)
    metadata  title/chamber/type/source fields changed
"""
from __future__ import annotations

from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import BillEvent

CREATED = "created"
STATUS = "status"
ACTIONS = "actions"
SPONSORS = "sponsors"
TEXT = "text"
METADATA = "metadata"

ALL_KINDS = frozenset({CREATED, STATUS, ACTIONS, SPONSORS, TEXT, METADATA})


def record_event(
    db: OrmSession, bill_id, kind: str, detail: str | None = None
) -> BillEvent:
    """Append one change-log entry. Caller commits.

    Raises on an unknown kind rather than writing it: the kind is a public API
    value that consumers branch on, so a typo silently entering the feed would
    be a contract break that no test or type checker would catch.
    """
    if kind not in ALL_KINDS:
        raise ValueError(f"unknown bill event kind {kind!r}; expected one of {sorted(ALL_KINDS)}")
    event = BillEvent(bill_id=bill_id, kind=kind, detail=detail)
    db.add(event)
    return event
