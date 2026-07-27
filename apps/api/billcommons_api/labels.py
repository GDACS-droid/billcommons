"""Fill the human-readable labels on bill payloads.

`bills` carries `jurisdiction_id` and `session_id` as bare UUIDs. Serving only
those made every list, search and change-feed response un-actionable on its
own: a caller holding a result had no way to say which state it belonged to,
or which session -- and "which session" is exactly the axis on which two
different bills share one number. The fix has to apply to EVERY producer of a
bill payload, so it lives here rather than being repeated per router.

Deliberately a bulk post-pass over already-materialized items rather than a
join or a relationship load: it works on any row shape a producer happened to
build (ORM entity, search Row, batch lookup) and cannot regress into an N+1
the way a lazy-loaded `bill.session.identifier` property would.

The two lookup tables are cached in process. They are tiny and effectively
static -- 51 jurisdictions and 77 sessions, against 209k bills -- so querying
them on every request was two extra round trips per API call forever, on the
hottest paths in the service, to fetch a few kilobytes that almost never
change. Cached, a label miss falls through to a real query, so a session added
after startup resolves on first use rather than being served as null.
"""
from __future__ import annotations

import threading
from typing import Iterable, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import Jurisdiction, Session

T = TypeVar("T")

_lock = threading.Lock()
_jurisdiction_cache: dict = {}
_session_cache: dict = {}


def _resolve(db: OrmSession, cache: dict, model, wanted: set) -> dict:
    """Return {id: label} for `wanted`, querying only the ids not yet cached."""
    missing = wanted - cache.keys()
    if missing:
        rows = db.execute(
            select(model.id, model.abbreviation if model is Jurisdiction else model.identifier)
            .where(model.id.in_(missing))
        ).all()
        with _lock:
            for row_id, label in rows:
                cache[row_id] = label
    return cache


def reset_label_cache() -> None:
    """Drop the cached labels. For tests, and for any caller that renames a
    jurisdiction or session and needs the change visible without a restart."""
    with _lock:
        _jurisdiction_cache.clear()
        _session_cache.clear()


def attach_bill_labels(db: OrmSession, items: Iterable[T]) -> list[T]:
    """Set `jurisdiction_abbreviation` and `session_identifier` on each item.

    Items must expose `jurisdiction_id`/`session_id` and accept the two label
    attributes (i.e. anything deriving from schemas.BillSummary). Returns the
    same objects, mutated, so callers can use it inline.
    """
    items = list(items)
    if not items:
        return items

    abbreviations = _resolve(
        db, _jurisdiction_cache, Jurisdiction, {i.jurisdiction_id for i in items if i.jurisdiction_id}
    )
    identifiers = _resolve(
        db, _session_cache, Session, {i.session_id for i in items if i.session_id}
    )

    for item in items:
        item.jurisdiction_abbreviation = abbreviations.get(item.jurisdiction_id)
        item.session_identifier = identifiers.get(item.session_id)
    return items
