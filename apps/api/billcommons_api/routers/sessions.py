from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.pagination import (
    MAX_PAGE,
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    Page,
    clamp_per_page,
    paginate,
)
from billcommons_api.schemas import Session as SessionOut
from billcommons_schema.models import Jurisdiction, Session

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=Page[SessionOut])
def list_sessions(
    request: Request,
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    active: bool | None = Query(None),
    page: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[SessionOut]:
    per_page = clamp_per_page(per_page)
    stmt = select(Session, Jurisdiction).join(
        Jurisdiction, Jurisdiction.id == Session.jurisdiction_id
    )
    count_stmt = (
        select(func.count())
        .select_from(Session)
        .join(Jurisdiction, Jurisdiction.id == Session.jurisdiction_id)
    )

    if jurisdiction:
        stmt = stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())
        count_stmt = count_stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())
    if active is not None:
        stmt = stmt.where(Session.active == active)
        count_stmt = count_stmt.where(Session.active == active)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(Session.start_date.desc().nullslast())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    items = []
    for session_row, jurisdiction_row in rows:
        item = SessionOut.model_validate(session_row)
        item.jurisdiction_abbreviation = jurisdiction_row.abbreviation
        item.jurisdiction_name = jurisdiction_row.name
        items.append(item)
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
