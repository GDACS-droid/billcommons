from __future__ import annotations

import difflib
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import selectinload

from billcommons_shared.normalize import normalize_bill_number

from billcommons_api.deps import get_db
from billcommons_api.errors import conflict, not_found
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
    BillCompareEnvelope,
    BillCompareOut,
    BillDetail,
    BillDocumentOut,
    BillSummary,
    BillVersionOut,
    RelatedBillOut,
    DiffLineOut,
    SponsorshipOut,
    VoteEventOut,
    VoteRecordOut,
)
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    BillSubject,
    Jurisdiction,
    RelatedBill,
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
    identifier: str | None = Query(
        None,
        description=(
            "Bill number, matched on the normalized form -- 'HB123', 'hb 123' and "
            "'H.B. 123' all find HB 123. Combine with jurisdiction (and session) to "
            "resolve a single bill."
        ),
    ),
    chamber: str | None = Query(None),
    status: str | None = Query(None),
    subject: str | None = Query(
        None, description="Subject/topic tag, matched case-insensitively"
    ),
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
    if identifier:
        # Match the stored normalized form, and normalize the caller's input the
        # same way the ingest did (billcommons_ingest.api_sync) so any surface
        # spelling of a bill number resolves. Unparseable input falls back to
        # raw uppercase, which is exactly what ingest stored in that case -- so
        # even malformed identifiers stay addressable rather than 0-result.
        try:
            identifier_norm = normalize_bill_number(identifier)
        except ValueError:
            identifier_norm = identifier.upper().strip()
        stmt = stmt.where(Bill.identifier_norm == identifier_norm)
        count_stmt = count_stmt.where(Bill.identifier_norm == identifier_norm)
    if chamber:
        stmt = stmt.where(Bill.chamber == chamber)
        count_stmt = count_stmt.where(Bill.chamber == chamber)
    if status:
        stmt = stmt.where(Bill.status == status)
        count_stmt = count_stmt.where(Bill.status == status)
    if subject:
        # EXISTS rather than a join: a bill carries several subject tags and a
        # join would return it once per matching tag, corrupting both the page
        # and the total.
        subject_match = select(BillSubject.id).where(
            BillSubject.bill_id == Bill.id,
            func.lower(BillSubject.subject) == subject.strip().lower(),
        )
        stmt = stmt.where(subject_match.exists())
        count_stmt = count_stmt.where(subject_match.exists())

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


@router.get("/{bill_id}/related", response_model=list[RelatedBillOut])
def list_related_bills(
    bill_id: uuid.UUID, db: OrmSession = Depends(get_db)
) -> list[RelatedBillOut]:
    """Companion, prior-session and superseded-bill cross-references.

    These have been collected all along (~100k rows, of which ~47k are
    `prior-session`) and were never exposed -- the single most-requested thing
    for multi-year policy tracking, because a bill that dies and returns next
    session under a new number is otherwise untrackable.

    Upstream gives us only the related bill's IDENTIFIER, so the target is
    resolved here, scoped to the same jurisdiction. Same-session companions
    usually resolve; prior-session links usually do not, because that session
    is outside this corpus. Unresolved links still return their identifier
    rather than being dropped.
    """
    bill = _get_bill_or_404(db, bill_id)
    rows = (
        db.execute(select(RelatedBill).where(RelatedBill.bill_id == bill_id))
        .scalars()
        .all()
    )

    wanted = {
        r.related_identifier.strip()
        for r in rows
        if r.related_bill_id is None and r.related_identifier
    }
    resolved: dict[str, uuid.UUID] = {}
    if wanted:
        norms = {}
        for raw in wanted:
            try:
                norms[normalize_bill_number(raw)] = raw
            except ValueError:
                continue
        if norms:
            matches = db.execute(
                select(Bill.id, Bill.identifier_norm).where(
                    Bill.jurisdiction_id == bill.jurisdiction_id,
                    Bill.session_id == bill.session_id,
                    Bill.identifier_norm.in_(list(norms)),
                )
            ).all()
            for match in matches:
                resolved[norms[match.identifier_norm]] = match.id

    out = []
    for r in rows:
        item = RelatedBillOut.model_validate(r)
        if item.related_bill_id is None and r.related_identifier:
            item.related_bill_id = resolved.get(r.related_identifier.strip())
        out.append(item)
    return out


@router.get("/{bill_id}/subjects", response_model=list[str])
def list_bill_subjects(bill_id: uuid.UUID, db: OrmSession = Depends(get_db)) -> list[str]:
    """Subject/topic tags. 263,485 of these were already stored and reachable
    only through /search?subject=; a consumer looking at one bill had no way to
    ask what it is ABOUT."""
    _get_bill_or_404(db, bill_id)
    return list(
        db.execute(
            select(BillSubject.subject)
            .where(BillSubject.bill_id == bill_id)
            .order_by(BillSubject.subject)
        ).scalars()
    )


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


@router.get("/{bill_id}/compare", response_model=BillCompareEnvelope)
def compare_bill_versions(
    bill_id: uuid.UUID,
    request: Request,
    from_: uuid.UUID = Query(..., alias="from", description="Bill version id to diff from"),
    to: uuid.UUID = Query(..., description="Bill version id to diff to"),
    db: OrmSession = Depends(get_db),
) -> BillCompareEnvelope:
    """Deterministic diff of extracted text between two versions of a bill.

    Mirrors apps/mcp compare_bill_versions (difflib), reimplemented locally
    per architecture: apps don't import across each other. DERIVED output,
    not an official document -- see docs/SPEC.md "Version diffing".
    """
    bill = _get_bill_or_404(db, bill_id)

    version_ids = {from_, to}
    versions = (
        db.execute(
            select(BillVersion)
            .options(selectinload(BillVersion.documents))
            .where(BillVersion.bill_id == bill.id, BillVersion.id.in_(version_ids))
        )
        .scalars()
        .all()
    )
    by_id = {v.id: v for v in versions}
    for vid in version_ids:
        if vid not in by_id:
            raise not_found(
                "version_not_found", f"No version {vid} found for bill {bill_id}"
            )

    def extracted_text(version: BillVersion) -> str | None:
        for doc in version.documents:
            if doc.extracted_text:
                return doc.extracted_text
        return None

    from_version = by_id[from_]
    to_version = by_id[to]
    text_from = extracted_text(from_version)
    text_to = extracted_text(to_version)

    missing = [
        str(v.id) for v, t in ((from_version, text_from), (to_version, text_to)) if t is None
    ]
    if missing:
        raise conflict(
            "extracted_text_unavailable",
            f"Version(s) {', '.join(missing)} have no extracted text yet.",
        )

    lines_from = text_from.splitlines()
    lines_to = text_to.splitlines()

    diff_lines: list[DiffLineOut] = []
    for line in difflib.unified_diff(
        lines_from,
        lines_to,
        fromfile=f"version:{from_version.note or from_version.id}",
        tofile=f"version:{to_version.note or to_version.id}",
        lineterm="",
    ):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            diff_lines.append(DiffLineOut(type="meta", text=line))
        elif line.startswith("+"):
            diff_lines.append(DiffLineOut(type="add", text=line))
        elif line.startswith("-"):
            diff_lines.append(DiffLineOut(type="remove", text=line))
        else:
            diff_lines.append(DiffLineOut(type="context", text=line))

    result = BillCompareOut(
        bill_id=bill.id,
        from_version_id=from_version.id,
        to_version_id=to_version.id,
        diff_lines=diff_lines,
    )
    return BillCompareEnvelope(
        data=result,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )
