from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.labels import attach_bill_labels
from billcommons_api.pagination import (
    MAX_PAGE,
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    Page,
    clamp_per_page,
    paginate,
)
from billcommons_api.schemas import BillSummary, TopicListEnvelope, TopicOut
from billcommons_schema.models import Bill
from billcommons_shared.topics import TOPICS, membership_clause

router = APIRouter(prefix="/topics", tags=["topics"])

# TOPICS + membership_clause moved to billcommons_shared.topics (2026-08) so
# the MCP's list_topics tool and this router query the exact same registry
# instead of drifting -- see that module's docstring. `_membership_clause`
# kept as a local alias: it's referenced by name in existing tests/callers
# below and this is a query-builder, not public API surface, so there's no
# reason to touch every call site just to rename it.
_membership_clause = membership_clause


@router.get("", response_model=TopicListEnvelope)
def list_topics(request: Request, db: OrmSession = Depends(get_db)) -> TopicListEnvelope:
    items = []
    for topic in TOPICS.values():
        count = db.execute(
            select(func.count()).select_from(Bill).where(_membership_clause(topic))
        ).scalar_one()
        items.append(
            TopicOut(
                slug=topic.slug,
                name=topic.name,
                description=topic.description,
                bill_count=count,
            )
        )
    return TopicListEnvelope(
        data=items,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )


@router.get("/{slug}", response_model=Page[BillSummary])
def topic_bills(
    request: Request,
    slug: str,
    page: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[BillSummary]:
    topic = TOPICS.get(slug)
    if topic is None:
        raise HTTPException(status_code=404, detail=f"unknown topic {slug!r}")

    clause = _membership_clause(topic)
    per_page = clamp_per_page(per_page)
    total = db.execute(
        select(func.count()).select_from(Bill).where(clause)
    ).scalar_one()
    rows = (
        db.execute(
            select(Bill)
            .where(clause)
            .order_by(Bill.latest_action_date.desc().nullslast(), Bill.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    items = attach_bill_labels(db, [BillSummary.model_validate(r) for r in rows])
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
