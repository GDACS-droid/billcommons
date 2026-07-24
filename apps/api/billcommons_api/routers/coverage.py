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
from billcommons_api.schemas import CoverageOut
from billcommons_schema.models import Jurisdiction, JurisdictionCoverage

router = APIRouter(prefix="/coverage", tags=["coverage"])


@router.get("", response_model=Page[CoverageOut])
def list_coverage(
    request: Request,
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    status: str | None = Query(None),
    page: int = Query(DEFAULT_PAGE, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[CoverageOut]:
    per_page = clamp_per_page(per_page)
    stmt = select(JurisdictionCoverage)
    count_stmt = select(func.count()).select_from(JurisdictionCoverage)

    if jurisdiction:
        stmt = stmt.join(
            Jurisdiction, Jurisdiction.id == JurisdictionCoverage.jurisdiction_id
        ).where(Jurisdiction.abbreviation == jurisdiction.upper())
        count_stmt = count_stmt.join(
            Jurisdiction, Jurisdiction.id == JurisdictionCoverage.jurisdiction_id
        ).where(Jurisdiction.abbreviation == jurisdiction.upper())
    if status:
        stmt = stmt.where(JurisdictionCoverage.status == status)
        count_stmt = count_stmt.where(JurisdictionCoverage.status == status)

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(JurisdictionCoverage.jurisdiction_id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    items = [CoverageOut.model_validate(r) for r in rows]
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
