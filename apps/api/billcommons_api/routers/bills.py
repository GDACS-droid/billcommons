from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

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
from billcommons_api.schemas import (
    BillActionOut,
    BillDetail,
    BillDocumentOut,
    BillSummary,
    BillVersionOut,
    SponsorshipOut,
    VoteEventOut,
    VoteRecordOut,
)
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    Jurisdiction,
    Session,
    Sponsorship,
    VoteEvent,
    VoteRecord,
)

router = APIRouter(prefix="/bills", tags=["bills"])


def _get_bill_or_404(db: OrmSession, bill_id: uuid.UUID) -> Bill:
    row = db.get(Bill, bill_id)
    if row is None:
        raise not_found("bill_not_found", f"No bill with id {bill_id}")
    return row


@router.get("", response_model=Page[BillSummary])
def list_bills(
    request: Request,
    jurisdiction: str | None = Query(None, description="Jurisdiction abbreviation, e.g. NC"),
    session: str | None = Query(None, description="Session identifier"),
    chamber: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(DEFAULT_PAGE, ge=1),
    per_page: int = Query(DEFAULT_PER_PAGE, ge=1),
    db: OrmSession = Depends(get_db),
) -> Page[BillSummary]:
    per_page = clamp_per_page(per_page)
    stmt = select(Bill).join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id).join(
        Session, Session.id == Bill.session_id
    )
    count_stmt = (
        select(func.count())
        .select_from(Bill)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .join(Session, Session.id == Bill.session_id)
    )

    if jurisdiction:
        stmt = stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())
        count_stmt = count_stmt.where(Jurisdiction.abbreviation == jurisdiction.upper())
    if session:
        stmt = stmt.where(Session.identifier == session)
        count_stmt = count_stmt.where(Session.identifier == session)
    if chamber:
        stmt = stmt.where(Bill.chamber == chamber)
        count_stmt = count_stmt.where(Bill.chamber == chamber)
    if status:
        stmt = stmt.where(Bill.status == status)
        count_stmt = count_stmt.where(Bill.status == status)

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(Bill.latest_action_date.desc().nullslast(), Bill.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    items = [BillSummary.model_validate(r) for r in rows]
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )


@router.get("/{bill_id}", response_model=BillDetail)
def get_bill(
    bill_id: uuid.UUID, response: Response, db: OrmSession = Depends(get_db)
) -> BillDetail:
    row = _get_bill_or_404(db, bill_id)
    response.headers["ETag"] = make_etag(row.id, row.updated_at)
    return BillDetail.model_validate(row)


@router.get("/{bill_id}/versions", response_model=list[BillVersionOut])
def list_bill_versions(bill_id: uuid.UUID, db: OrmSession = Depends(get_db)) -> list[BillVersionOut]:
    _get_bill_or_404(db, bill_id)
    rows = (
        db.execute(
            select(BillVersion).where(BillVersion.bill_id == bill_id).order_by(BillVersion.date)
        )
        .scalars()
        .all()
    )
    return [BillVersionOut.model_validate(r) for r in rows]


@router.get("/{bill_id}/actions", response_model=list[BillActionOut])
def list_bill_actions(bill_id: uuid.UUID, db: OrmSession = Depends(get_db)) -> list[BillActionOut]:
    _get_bill_or_404(db, bill_id)
    rows = (
        db.execute(
            select(BillAction)
            .where(BillAction.bill_id == bill_id)
            .order_by(BillAction.order.asc().nullslast(), BillAction.action_date)
        )
        .scalars()
        .all()
    )
    return [BillActionOut.model_validate(r) for r in rows]


@router.get("/{bill_id}/sponsors", response_model=list[SponsorshipOut])
def list_bill_sponsors(bill_id: uuid.UUID, db: OrmSession = Depends(get_db)) -> list[SponsorshipOut]:
    _get_bill_or_404(db, bill_id)
    rows = (
        db.execute(
            select(Sponsorship)
            .where(Sponsorship.bill_id == bill_id)
            .order_by(Sponsorship.primary.desc())
        )
        .scalars()
        .all()
    )
    return [SponsorshipOut.model_validate(r) for r in rows]


@router.get("/{bill_id}/votes", response_model=list[VoteEventOut])
def list_bill_votes(bill_id: uuid.UUID, db: OrmSession = Depends(get_db)) -> list[VoteEventOut]:
    _get_bill_or_404(db, bill_id)
    events = (
        db.execute(
            select(VoteEvent).where(VoteEvent.bill_id == bill_id).order_by(VoteEvent.start_date)
        )
        .scalars()
        .all()
    )
    out = []
    for e in events:
        records = (
            db.execute(select(VoteRecord).where(VoteRecord.vote_event_id == e.id)).scalars().all()
        )
        item = VoteEventOut.model_validate(e)
        item.votes = [VoteRecordOut.model_validate(r) for r in records]
        out.append(item)
    return out


@router.get("/{bill_id}/documents", response_model=list[BillDocumentOut])
def list_bill_documents(bill_id: uuid.UUID, db: OrmSession = Depends(get_db)) -> list[BillDocumentOut]:
    _get_bill_or_404(db, bill_id)
    rows = (
        db.execute(
            select(BillDocument)
            .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
            .where(BillVersion.bill_id == bill_id)
        )
        .scalars()
        .all()
    )
    out = []
    for r in rows:
        item = BillDocumentOut.model_validate(r)
        item.has_extracted_text = bool(r.extracted_text)
        out.append(item)
    return out
