"""Per-jurisdiction Atom feed: GET /feeds/{jurisdiction}.atom.

Exists for consumers that want an RSS-reader-compatible feed of one
jurisdiction's recent change events rather than a JSON polling loop against
/changes -- the same underlying `bill_events` log, rendered as Atom 1.0
instead of paginated JSON, and capped to the most recent MAX_FEED_ENTRIES
rather than cursor-paginated (a feed reader re-fetches the whole document on
every poll; there is no cursor to hand it).

Reuses routers.changes.COMMIT_SAFETY_LAG_SECONDS rather than a second
constant: `seq` becomes visible out of commit order for the same reason on
every reader of `bill_events`, so this feed is bound by the identical
correctness argument documented there -- see that module for the actual
explanation, not duplicated here.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.atom import build_atom_feed
from billcommons_api.deps import get_db
from billcommons_api.errors import not_found
from billcommons_api.routers.changes import COMMIT_SAFETY_LAG_SECONDS
from billcommons_schema.models import Bill, BillEvent, Jurisdiction

router = APIRouter(tags=["feeds"])

# A feed reader re-fetches the whole document on every poll rather than
# advancing a cursor, so this is a fixed recency window, not a page size --
# matches /changes' DEFAULT_CHANGES_PER_PAGE, comfortably enough for any
# single jurisdiction's normal event rate between reader polls.
MAX_FEED_ENTRIES = 100

# Matches the value used elsewhere in this codebase for a cheap,
# infrequently-changing list (apps/web's TOPIC_LIST_REVALIDATE) -- short
# enough that a newly landed event is visible within a few minutes, long
# enough that a feed reader polling on its own schedule costs the DB
# essentially nothing.
FEED_CACHE_CONTROL = "public, max-age=300"

# A feed shows RECENT events by definition, so a jurisdiction with nothing
# newer than this is genuinely quiet -- not merely under-covered. Without
# this floor, `ORDER BY seq DESC LIMIT 100` on a quiet jurisdiction has no
# lower bound at all: Postgres would walk bill_events backwards from the
# head of the ENTIRE corpus (157k+ vote events alone) on every request until
# it happened to find 100 rows for that one jurisdiction, or ran out. The
# floor turns that into an index-bounded range scan on the existing
# ix_bill_events_changed_at index instead.
FEED_LOOKBACK_DAYS = 30


@router.get("/feeds/{jurisdiction}.atom")
def jurisdiction_atom_feed(
    jurisdiction: str,
    request: Request,
    db: OrmSession = Depends(get_db),
) -> Response:
    jurisdiction_row = db.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation == jurisdiction.upper())
    ).scalar_one_or_none()
    if jurisdiction_row is None:
        raise not_found(
            "jurisdiction_not_found", f"No jurisdiction with abbreviation {jurisdiction!r}"
        )

    # Same watermark discipline as /changes: only serve events old enough
    # that no transaction which could hold a lower `seq` is still open.
    watermark = func.now() - timedelta(seconds=COMMIT_SAFETY_LAG_SECONDS)
    lookback_floor = func.now() - timedelta(days=FEED_LOOKBACK_DAYS)
    stmt = (
        select(BillEvent, Bill)
        .join(Bill, Bill.id == BillEvent.bill_id)
        .where(
            Bill.jurisdiction_id == jurisdiction_row.id,
            BillEvent.changed_at <= watermark,
            BillEvent.changed_at >= lookback_floor,
        )
        .order_by(BillEvent.seq.desc())
        .limit(MAX_FEED_ENTRIES)
    )
    rows = list(db.execute(stmt).all())

    self_url = str(request.url)
    xml_body = build_atom_feed(jurisdiction_row, rows, self_url)

    # Headers set on a FastAPI-injected `Response` dependency are discarded
    # when the endpoint returns its OWN Response instance instead (which this
    # one must, to control media_type/content precisely) -- they have to be
    # set on the object actually being returned.
    return Response(
        content=xml_body,
        media_type="application/atom+xml",
        headers={"Cache-Control": FEED_CACHE_CONTROL},
    )
