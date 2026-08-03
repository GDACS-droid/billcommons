from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.emptiness import COMMITTEES, describe_empty
from billcommons_api.errors import not_found
from billcommons_api.etag import make_etag
from billcommons_api.pagination import (
    MAX_PAGE,
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    Page,
    clamp_per_page,
    paginate,
)
from billcommons_api.schemas import CommitteeOut
from billcommons_schema.models import Committee, Organization

router = APIRouter(prefix="/committees", tags=["committees"])


@router.get("", response_model=Page[CommitteeOut])
def list_committees(
    request: Request,
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    page: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[CommitteeOut]:
    per_page = clamp_per_page(per_page)
    stmt = select(Committee)
    count_stmt = select(func.count()).select_from(Committee)

    if jurisdiction:
        from billcommons_schema.models import Jurisdiction

        stmt = (
            stmt.join(Organization, Organization.id == Committee.organization_id)
            .join(Jurisdiction, Jurisdiction.id == Organization.jurisdiction_id)
            .where(Jurisdiction.abbreviation == jurisdiction.upper())
        )
        count_stmt = (
            count_stmt.join(Organization, Organization.id == Committee.organization_id)
            .join(Jurisdiction, Jurisdiction.id == Organization.jurisdiction_id)
            .where(Jurisdiction.abbreviation == jurisdiction.upper())
        )

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(stmt.order_by(Committee.name).offset((page - 1) * per_page).limit(per_page))
        .scalars()
        .all()
    )
    items = [CommitteeOut.model_validate(r) for r in rows]
    data_status, notice = (None, None)
    if not items:
        data_status, notice = describe_empty(db, Committee, COMMITTEES)
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
        data_status=data_status,
        notice=notice,
    )


@router.get("/{committee_id}", response_model=CommitteeOut)
def get_committee(
    committee_id: uuid.UUID, response: Response, db: OrmSession = Depends(get_db)
) -> CommitteeOut:
    row = db.get(Committee, committee_id)
    if row is None:
        raise not_found("committee_not_found", f"No committee with id {committee_id}")
    response.headers["ETag"] = make_etag(row.id, row.updated_at)
    return CommitteeOut.model_validate(row)
