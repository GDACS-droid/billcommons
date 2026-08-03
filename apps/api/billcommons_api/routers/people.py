from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_api.deps import get_db
from billcommons_api.emptiness import LEGISLATORS, describe_empty
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
from billcommons_api.schemas import PersonDetail, PersonSummary
from billcommons_schema.models import Jurisdiction, Person

router = APIRouter(prefix="/people", tags=["people"])


@router.get("", response_model=Page[PersonSummary])
def list_people(
    request: Request,
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    name: str | None = Query(None, description="Case-insensitive substring match on name"),
    page: int = Query(DEFAULT_PAGE, ge=1, le=MAX_PAGE),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[PersonSummary]:
    per_page = clamp_per_page(per_page)
    stmt = select(Person)
    count_stmt = select(func.count()).select_from(Person)

    if jurisdiction:
        stmt = stmt.join(Jurisdiction, Jurisdiction.id == Person.jurisdiction_id).where(
            Jurisdiction.abbreviation == jurisdiction.upper()
        )
        count_stmt = count_stmt.join(
            Jurisdiction, Jurisdiction.id == Person.jurisdiction_id
        ).where(Jurisdiction.abbreviation == jurisdiction.upper())
    if name:
        stmt = stmt.where(Person.name.ilike(f"%{name}%"))
        count_stmt = count_stmt.where(Person.name.ilike(f"%{name}%"))

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(stmt.order_by(Person.name).offset((page - 1) * per_page).limit(per_page))
        .scalars()
        .all()
    )
    items = [PersonSummary.model_validate(r) for r in rows]
    # An empty legislator list is the norm here, not an edge case: nothing
    # populates this table. Say so, rather than letting a caller read the empty
    # array as "this jurisdiction has no legislators".
    data_status, notice = (None, None)
    if not items:
        data_status, notice = describe_empty(db, Person, LEGISLATORS)
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


@router.get("/{person_id}", response_model=PersonDetail)
def get_person(
    person_id: uuid.UUID, response: Response, db: OrmSession = Depends(get_db)
) -> PersonDetail:
    row = db.get(Person, person_id)
    if row is None:
        raise not_found("person_not_found", f"No person with id {person_id}")
    response.headers["ETag"] = make_etag(row.id, row.updated_at)
    return PersonDetail.model_validate(row)
