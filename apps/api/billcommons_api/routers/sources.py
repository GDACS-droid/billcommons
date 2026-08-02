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
from billcommons_api.schemas import SourceRecordOut
from billcommons_schema.models import SourceRecord

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=Page[SourceRecordOut])
def list_sources(
    request: Request,
    entity_type: str | None = Query(None),
    entity_id: uuid.UUID | None = Query(None),
    page: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[SourceRecordOut]:
    per_page = clamp_per_page(per_page)
    stmt = select(SourceRecord)
    count_stmt = select(func.count()).select_from(SourceRecord)

    if entity_type:
        stmt = stmt.where(SourceRecord.entity_type == entity_type)
        count_stmt = count_stmt.where(SourceRecord.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(SourceRecord.entity_id == entity_id)
        count_stmt = count_stmt.where(SourceRecord.entity_id == entity_id)

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(SourceRecord.retrieved_at.desc().nullslast())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    items = [SourceRecordOut.model_validate(r) for r in rows]
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
