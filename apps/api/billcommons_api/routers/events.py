from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.pagination import (
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    Page,
    clamp_per_page,
    paginate,
)
from billcommons_api.schemas import LegislativeEventOut
from billcommons_schema.models import Jurisdiction, LegislativeEvent

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=Page[LegislativeEventOut])
def list_events(
    request: Request,
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    page: int = Query(DEFAULT_PAGE, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[LegislativeEventOut]:
    per_page = clamp_per_page(per_page)
    stmt = select(LegislativeEvent, Jurisdiction).outerjoin(
        Jurisdiction, Jurisdiction.id == LegislativeEvent.jurisdiction_id
    )
    count_stmt = (
        select(func.count())
        .select_from(LegislativeEvent)
        .outerjoin(Jurisdiction, Jurisdiction.id == LegislativeEvent.jurisdiction_id)
    )

    if jurisdiction:
        stmt = stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())
        count_stmt = count_stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(LegislativeEvent.start_date.desc().nullslast())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    items = []
    for event_row, jurisdiction_row in rows:
        item = LegislativeEventOut.model_validate(event_row)
        item.jurisdiction_abbreviation = jurisdiction_row.abbreviation if jurisdiction_row else None
        items.append(item)
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
