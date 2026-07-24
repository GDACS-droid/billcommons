from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from billcommons_api.deps import get_db
from billcommons_api.errors import not_found
from billcommons_api.etag import make_etag
from billcommons_api.pagination import (
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    Page,
    clamp_per_page,
    paginate,
)
from billcommons_api.schemas import Jurisdiction as JurisdictionOut
from billcommons_schema.models import Jurisdiction

router = APIRouter(prefix="/jurisdictions", tags=["jurisdictions"])


@router.get("", response_model=Page[JurisdictionOut])
def list_jurisdictions(
    request: Request,
    page: int = Query(DEFAULT_PAGE, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: Session = Depends(get_db),
) -> Page[JurisdictionOut]:
    per_page = clamp_per_page(per_page)
    total = db.execute(select(func.count()).select_from(Jurisdiction)).scalar_one()
    rows = (
        db.execute(
            select(Jurisdiction)
            .order_by(Jurisdiction.name)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    items = [JurisdictionOut.model_validate(r) for r in rows]
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )


@router.get("/{jurisdiction_id}", response_model=JurisdictionOut)
def get_jurisdiction(
    jurisdiction_id: uuid.UUID,
    response: Response,
    db: Session = Depends(get_db),
) -> JurisdictionOut:
    row = db.get(Jurisdiction, jurisdiction_id)
    if row is None:
        raise not_found("jurisdiction_not_found", f"No jurisdiction with id {jurisdiction_id}")
    response.headers["ETag"] = make_etag(row.id, row.updated_at)
    return JurisdictionOut.model_validate(row)
