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
from billcommons_api.schemas import CoverageRowOut
from billcommons_schema.models import Jurisdiction, JurisdictionCoverage, Session

router = APIRouter(prefix="/coverage", tags=["coverage"])

DEFAULT_SOURCE_NAME = "Open States / Plural"


def _to_coverage_row(coverage: JurisdictionCoverage, jurisdiction: Jurisdiction, session: Session | None) -> CoverageRowOut:
    full_text_pct = (
        round(100 * coverage.full_text_count / coverage.bill_count, 1)
        if coverage.bill_count > 0
        else None
    )
    known_gaps = (
        [line.strip() for line in coverage.known_gaps.splitlines() if line.strip()]
        if coverage.known_gaps
        else []
    )
    return CoverageRowOut(
        jurisdiction_code=jurisdiction.abbreviation,
        jurisdiction_name=jurisdiction.name,
        session_identifier=session.identifier if session else None,
        session_status=(
            ("active" if session.active else "adjourned") if session else None
        ),
        bill_count=coverage.bill_count,
        full_text_count=coverage.full_text_count,
        full_text_pct=full_text_pct,
        last_update=coverage.last_success_at.isoformat() if coverage.last_success_at else None,
        source_name=DEFAULT_SOURCE_NAME,
        validation_sample=None,
        validation_pass_rate=(
            float(coverage.validation_pass_rate) if coverage.validation_pass_rate is not None else None
        ),
        status=coverage.status,
        known_gaps=known_gaps,
    )


@router.get("", response_model=Page[CoverageRowOut])
def list_coverage(
    request: Request,
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    status: str | None = Query(None),
    page: int = Query(DEFAULT_PAGE, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[CoverageRowOut]:
    per_page = clamp_per_page(per_page, maximum=200)
    stmt = select(JurisdictionCoverage, Jurisdiction, Session).join(
        Jurisdiction, Jurisdiction.id == JurisdictionCoverage.jurisdiction_id
    ).outerjoin(Session, Session.id == JurisdictionCoverage.session_id)
    count_stmt = (
        select(func.count())
        .select_from(JurisdictionCoverage)
        .join(Jurisdiction, Jurisdiction.id == JurisdictionCoverage.jurisdiction_id)
    )

    if jurisdiction:
        stmt = stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())
        count_stmt = count_stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())
    if status:
        stmt = stmt.where(JurisdictionCoverage.status == status)
        count_stmt = count_stmt.where(JurisdictionCoverage.status == status)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(Jurisdiction.name, JurisdictionCoverage.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    items = [_to_coverage_row(coverage, jur, sess) for coverage, jur, sess in rows]
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )
