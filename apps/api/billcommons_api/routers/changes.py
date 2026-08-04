"""Change feed, served from the append-only `bill_events` log.

This used to read `bills.updated_at`. That column could not carry a change
KIND, only existed on `bills` (so every child writer had to remember to reach
up and stamp the parent -- two already did not), and was mutable, so a
wholesale re-derivation restamped tens of thousands of rows and published them
as changes indistinguishable from real legislative movement. See migration
0005.

Two properties this endpoint must hold, both of which have a specific
mechanism here rather than a hope:

  * NEVER SKIP. A consumer that pages to the end has seen every event.
  * TOTAL ORDER. One cursor, monotonically advancing, no ties to get stuck on.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.errors import bad_request
from billcommons_api.labels import attach_bill_labels
from billcommons_api.schemas import ChangeEvent, ChangeFeedEnvelope
from billcommons_schema.models import Bill, BillEvent, Jurisdiction

router = APIRouter(tags=["changes"])

MAX_CHANGES_PER_PAGE = 500
DEFAULT_CHANGES_PER_PAGE = 100
MAX_IDS = 200

# How far behind the head of the log the feed refuses to serve.
#
# `seq` is allocated at INSERT and becomes visible at COMMIT, so a writer that
# holds a transaction open can own a LOW seq that only appears after HIGHER
# ones are already visible. A consumer that advanced past it would never see
# it -- a permanently missed change, reported as "nothing to report", which is
# the single worst failure a monitoring source can have.
#
# So the feed only serves events old enough that any transaction which could
# hold a lower seq has necessarily finished. This bounds the damage to
# LATENCY (a change is announced up to this many seconds late) instead of
# CORRECTNESS (a change is never announced at all). It is only sound while
# every writing transaction is shorter than this window; the ingest's
# api_sync commits per jurisdiction, well inside it.
COMMIT_SAFETY_LAG_SECONDS = 120

_CURSOR_VERSION = 1


def encode_cursor(seq: int) -> str:
    """Opaque cursor. Deliberately not the raw `seq`.

    Consumers persist cursors for months, so whatever shape is published here
    is frozen. Wrapping it means the ordering key can later change -- to a
    composite, a different column, a sharded scheme -- without breaking a
    single stored cursor, and it stops clients from doing arithmetic on a
    value whose gaps are meaningless (sequences skip on rollback).
    """
    raw = json.dumps({"v": _CURSOR_VERSION, "seq": seq}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if payload.get("v") != _CURSOR_VERSION:
            raise ValueError(f"unsupported cursor version {payload.get('v')!r}")
        return int(payload["seq"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise bad_request(
            "invalid_cursor",
            f"Could not read the cursor. Pass back the `next_cursor` from a "
            f"previous /changes response verbatim, or omit it to start from "
            f"the beginning. ({exc})",
        ) from None


@router.get("/changes", response_model=ChangeFeedEnvelope)
def list_changes(
    request: Request,
    cursor: str | None = Query(
        None,
        description=(
            "Opaque cursor from a previous response's `next_cursor`. Omit to "
            "start from the beginning of the log."
        ),
    ),
    ids: str | None = Query(
        None,
        description=(
            "Comma-separated bill UUIDs -- returns changes only for these "
            f"bills. Up to {MAX_IDS} per request. This is what makes the feed "
            "usable for a watchlist: without it a caller tracking 160 bills "
            "must page the entire national delta and filter client-side."
        ),
    ),
    jurisdiction: str | None = Query(
        None, description="Restrict to one jurisdiction abbreviation, e.g. NC"
    ),
    kind: str | None = Query(
        None,
        description=(
            "Restrict to one change kind: created, status, actions, sponsors, "
            "text, metadata, votes."
        ),
    ),
    per_page: int = Query(DEFAULT_CHANGES_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> ChangeFeedEnvelope:
    """Bill changes in commit order, oldest first.

    Poll with the `next_cursor` from the previous response. `has_more` false
    means you are caught up; keep the cursor and poll again later.
    """
    if per_page > MAX_CHANGES_PER_PAGE:
        raise bad_request(
            "per_page_too_large",
            f"per_page must be {MAX_CHANGES_PER_PAGE} or less, got {per_page}.",
        )

    after_seq = decode_cursor(cursor) if cursor else 0

    watermark = func.now() - timedelta(seconds=COMMIT_SAFETY_LAG_SECONDS)
    stmt = select(BillEvent).where(
        BillEvent.seq > after_seq, BillEvent.changed_at <= watermark
    )

    if ids:
        wanted = []
        for raw in ids.split(","):
            token = raw.strip()
            if not token:
                continue
            try:
                wanted.append(uuid.UUID(token))
            except ValueError:
                raise bad_request(
                    "invalid_id", f"{token!r} is not a valid bill id."
                ) from None
        if len(wanted) > MAX_IDS:
            raise bad_request(
                "too_many_ids",
                f"At most {MAX_IDS} ids per request, got {len(wanted)}.",
            )
        if wanted:
            stmt = stmt.where(BillEvent.bill_id.in_(wanted))
    if kind:
        stmt = stmt.where(BillEvent.kind == kind)
    if jurisdiction:
        stmt = stmt.where(
            BillEvent.bill_id.in_(
                select(Bill.id)
                .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
                .where(Jurisdiction.abbreviation == jurisdiction.upper())
            )
        )

    # One extra row tells us whether another page exists, without a COUNT over
    # a range that spans the whole log on a first sync.
    rows = (
        db.execute(stmt.order_by(BillEvent.seq).limit(per_page + 1)).scalars().all()
    )
    has_more = len(rows) > per_page
    rows = rows[:per_page]

    bills = {}
    if rows:
        bills = {
            b.id: b
            for b in db.execute(
                select(Bill).where(Bill.id.in_({r.bill_id for r in rows}))
            ).scalars()
        }

    items = [
        ChangeEvent(
            cursor=encode_cursor(row.seq),
            kind=row.kind,
            changed_at=row.changed_at,
            detail=row.detail,
            bill=bills.get(row.bill_id),
        )
        for row in rows
    ]
    attach_bill_labels(db, [i.bill for i in items if i.bill is not None])

    return ChangeFeedEnvelope(
        data=items,
        # The last event actually delivered. On an empty page the caller's own
        # cursor is echoed back rather than null: returning null invites a
        # client to send it back and get a 422, and advancing to "now" would
        # step them past changes they never received.
        next_cursor=items[-1].cursor if items else (cursor or encode_cursor(after_seq)),
        has_more=has_more,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )
