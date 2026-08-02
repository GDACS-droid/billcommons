from __future__ import annotations

import difflib
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import selectinload

from billcommons_shared.enrollment import enrolled_outcome_is_uncaptured
from billcommons_shared.normalize import normalize_bill_number

from billcommons_api.deps import get_db
from billcommons_api.errors import bad_request, conflict, not_found
from billcommons_api.etag import make_etag
from billcommons_api.labels import attach_bill_labels
from billcommons_api.pagination import (
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    Page,
    clamp_per_page,
    paginate,
)
from billcommons_api.schemas import (
    BillActionOut,
    BillBatchEnvelope,
    BillCompareEnvelope,
    BillCompareOut,
    BillDetail,
    BillLookupKey,
    BillLookupRequest,
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
    # Join a table only when a filter actually uses it.
    #
    # Both statements used to join `jurisdictions` AND `sessions`
    # unconditionally. Neither join can change the result when unfiltered --
    # both are many-to-one over a NOT NULL foreign key, so they match exactly
    # one row per bill -- but they are not free: on an unfiltered count they
    # turn an index-only aggregate into a hash join over the whole corpus. The
    # planner cannot drop them for us, because it cannot know the FK guarantees
    # one match.
    stmt = select(Bill)
    count_stmt = select(func.count()).select_from(Bill)

    if jurisdiction:
        stmt = stmt.join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id).where(
            Jurisdiction.abbreviation == jurisdiction.upper()
        )
        count_stmt = count_stmt.join(
            Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id
        ).where(Jurisdiction.abbreviation == jurisdiction.upper())
    if session:
        stmt = stmt.join(Session, Session.id == Bill.session_id).where(
            Session.identifier == session
        )
        count_stmt = count_stmt.join(Session, Session.id == Bill.session_id).where(
            Session.identifier == session
        )
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
    items = attach_bill_labels(db, [BillSummary.model_validate(r) for r in rows])
    return paginate(
        items,
        page=page,
        per_page=per_page,
        total=total,
        api_version="v1",
        request_id=request.state.request_id,
    )


# The GET form is additionally bounded by URL length in practice (~100
# session-qualified keys approaches the default request-line limit on common
# proxies, which return 414 before this check runs) -- which is why POST
# /bills/lookup exists and is the documented path for a real watchlist.
#
# 250, not 100: the first integrator to build a daily sync had ~130 tracked
# bills, so a 100 cap silently turned "one request" into "paginate the thing
# that exists to avoid pagination". Cost is bounded by jurisdiction, not key
# count -- lookups are grouped, so 250 keys spanning 50 states is 50 queries,
# the same as 100 keys spanning 50 states.
MAX_BATCH_KEYS = 250


@router.get("/batch", response_model=BillBatchEnvelope)
def batch_lookup_bills(
    request: Request,
    ids: str | None = Query(
        None, description="Comma-separated bill UUIDs, e.g. `ids=<uuid>,<uuid>`"
    ),
    keys: str | None = Query(
        None,
        description=(
            "Comma-separated `JURISDICTION:BILL_NUMBER` pairs, e.g. "
            "`keys=HI:SB2135,NY:S9417`. Bill numbers are normalized, so "
            "`HI:sb 2135` resolves the same bill. Append a session to "
            "disambiguate: `NY:S9417:2025-2026 Regular Session`."
        ),
    ),
    db: OrmSession = Depends(get_db),
) -> BillBatchEnvelope:
    """Resolve up to 100 bills in one request.

    Exists because watchlist monitoring -- the main reason to consume this API
    -- was one HTTP round trip per bill. A caller tracking 160 bills spent 160
    requests to answer "did anything move?", which is both slow for them and
    the single easiest way to walk into the rate limit.

    Anything requested but not resolved comes back in `not_found` rather than
    being silently absent; see BillBatchEnvelope.
    """
    # De-duplicated, order preserved. Repeats are not an error (a watchlist
    # assembled from several sources will contain them), but they must not
    # produce the same bill twice in `data`, and a token that differs only by
    # case from one already resolved must not be reported as missing.
    requested: list[str] = []
    seen_tokens: set[str] = set()
    for raw in (ids or "").split(",") + (keys or "").split(","):
        token = raw.strip()
        if token and token.casefold() not in seen_tokens:
            seen_tokens.add(token.casefold())
            requested.append(token)
    if not requested:
        raise bad_request(
            "no_keys", "Provide at least one lookup in `ids` or `keys`."
        )
    if len(requested) > MAX_BATCH_KEYS:
        raise bad_request(
            "too_many_keys",
            f"Batch lookup accepts at most {MAX_BATCH_KEYS} entries per request, "
            f"got {len(requested)}. Split the batch.",
        )

    parsed = []
    for token in requested:
        try:
            parsed.append((token, BillLookupKey(id=uuid.UUID(token))))
            continue
        except ValueError:
            pass
        # maxsplit=2: session identifiers routinely contain colons of their own
        # ("Alabama special: Congressional maps"), and a naive split would keep
        # only the fragment before the first one, match nothing, and report a
        # real bill as missing. 14 of this corpus's 77 sessions are affected.
        parts = [p.strip() for p in token.split(":", 2)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            parsed.append((token, None))
            continue
        parsed.append(
            (
                token,
                BillLookupKey(
                    jurisdiction=parts[0],
                    identifier=parts[1],
                    session=parts[2] if len(parts) > 2 and parts[2] else None,
                ),
            )
        )
    return _resolve_lookup(db, parsed, request)


@router.post("/lookup", response_model=BillBatchEnvelope)
def lookup_bills(
    request: Request,
    body: BillLookupRequest,
    db: OrmSession = Depends(get_db),
) -> BillBatchEnvelope:
    """Batch lookup with a structured JSON body. Same response shape as
    `GET /bills/batch`, which it supersedes for anything non-trivial.

    The GET form encodes each lookup as `JUR:NUMBER:SESSION` in the query
    string, which breaks in two ways that are not hypothetical: 100
    session-qualified keys is 4-8KB of URL, past the default request-line limit
    on common proxies (they return 414 before our own error can fire), and the
    colon delimiter collides with session identifiers and OCD jurisdiction ids
    that contain colons themselves.

    Keys are echoed back on each result so callers can correlate without
    re-implementing bill-number normalization.
    """
    if not body.keys:
        raise bad_request("no_keys", "Provide at least one entry in `keys`.")
    if len(body.keys) > MAX_BATCH_KEYS:
        raise bad_request(
            "too_many_keys",
            f"Lookup accepts at most {MAX_BATCH_KEYS} entries per request, "
            f"got {len(body.keys)}. Split the batch.",
        )
    parsed = []
    seen: set[str] = set()
    for key in body.keys:
        token = _lookup_token(key)
        if token.casefold() in seen:
            continue
        seen.add(token.casefold())
        parsed.append((token, key if (key.id or key.identifier) else None))
    return _resolve_lookup(db, parsed, request)


def _lookup_token(key: BillLookupKey) -> str:
    if key.id:
        return str(key.id)
    parts = [key.jurisdiction or "", key.identifier or ""]
    if key.session:
        parts.append(key.session)
    return ":".join(parts)


def _resolve_lookup(
    db: OrmSession, parsed: list[tuple[str, "BillLookupKey | None"]], request: Request
) -> BillBatchEnvelope:
    """Resolve parsed lookup keys. Shared by the GET and POST forms so the two
    can never answer the same question differently."""
    found: dict[str, Bill] = {}
    ambiguous: dict[str, list[str]] = {}

    # UUID lookups: one query for all of them. Keyed by token, not by the
    # parsed UUID -- two surface spellings of one id (case, formatting) would
    # otherwise collapse in the dict and the loser would be reported missing
    # while its own bill sat in `data`.
    by_id = {token: key.id for token, key in parsed if key is not None and key.id}
    if by_id:
        rows = {
            row.id: row
            for row in db.execute(
                select(Bill).where(Bill.id.in_(set(by_id.values())))
            ).scalars()
        }
        for token, bill_id in by_id.items():
            if bill_id in rows:
                found[token] = rows[bill_id]

    # Grouped by jurisdiction so a batch spanning n states costs n queries
    # rather than one per bill.
    by_jurisdiction: dict[str, list[tuple[str, str, str | None]]] = {}
    for token, key in parsed:
        if key is None or key.id or not key.jurisdiction or not key.identifier:
            continue
        try:
            identifier_norm = normalize_bill_number(key.identifier)
        except ValueError:
            identifier_norm = key.identifier.upper().strip()
        by_jurisdiction.setdefault(key.jurisdiction.upper().strip(), []).append(
            (token, identifier_norm, key.session)
        )

    for abbreviation, entries in by_jurisdiction.items():
        rows = db.execute(
            select(Bill, Session.identifier)
            .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
            .join(Session, Session.id == Bill.session_id)
            .where(
                Jurisdiction.abbreviation == abbreviation,
                Bill.identifier_norm.in_({e[1] for e in entries}),
            )
        ).all()
        for token, identifier_norm, session_identifier in entries:
            matches = [
                bill
                for bill, session_id in rows
                if bill.identifier_norm == identifier_norm
                and (session_identifier is None or session_id == session_identifier)
            ]
            # A bill number matching more than one session is AMBIGUOUS, not
            # missing, and the two demand opposite responses: "missing" means
            # drop it from the watchlist, "ambiguous" means re-ask with a
            # session. Collapsing them into not_found tells a consumer to
            # delete a bill we actually hold. Measured on this corpus: 981
            # ambiguous pairs covering 2,459 bills, 726 of them in Texas alone.
            if len(matches) == 1:
                found[token] = matches[0]
            elif len(matches) > 1:
                ambiguous[token] = sorted(
                    {
                        session_id
                        for bill, session_id in rows
                        if bill.identifier_norm == identifier_norm
                    }
                )

    tokens = [token for token, _ in parsed]
    items = attach_bill_labels(
        db, [BillSummary.model_validate(found[t]) for t in tokens if t in found]
    )
    return BillBatchEnvelope(
        data=items,
        not_found=[t for t in tokens if t not in found and t not in ambiguous],
        ambiguous=ambiguous,
        meta={"api_version": "v1", "request_id": request.state.request_id},
    )


@router.get("/{bill_id}", response_model=BillDetail)
def get_bill(
    bill_id: uuid.UUID, response: Response, db: OrmSession = Depends(get_db)
) -> BillDetail:
    row = _get_bill_or_404(db, bill_id)
    response.headers["ETag"] = make_etag(row.id, row.updated_at)
    detail = attach_bill_labels(db, [BillDetail.model_validate(row)])[0]
    # An enrolled bill whose session closed long ago is not awaiting anything.
    # See BillDetail.enrolled_outcome_uncaptured.
    session_end = db.execute(
        select(Session.end_date).where(Session.id == row.session_id)
    ).scalar_one_or_none()
    detail.enrolled_outcome_uncaptured = enrolled_outcome_is_uncaptured(
        row.status, session_end
    )
    return detail


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
    # One query for every event's records, not one query per event.
    #
    # This was an N+1: a bill with 12 vote events issued 12 separate
    # `vote_records` queries. Before `ix_vote_records_vote_event_id` existed
    # each of those was a full scan of a 6.7M-row table, so `/votes` could cost
    # several seconds and hold its connection for the duration. The index makes
    # each lookup sub-millisecond, but the round trips are still per-event and
    # this route is called for every crawled bill page.
    by_event: dict[uuid.UUID, list[VoteRecord]] = {}
    if events:
        records = (
            db.execute(
                select(VoteRecord).where(
                    VoteRecord.vote_event_id.in_([e.id for e in events])
                )
            )
            .scalars()
            .all()
        )
        for record in records:
            by_event.setdefault(record.vote_event_id, []).append(record)

    out = []
    for e in events:
        item = VoteEventOut.model_validate(e)
        item.votes = [VoteRecordOut.model_validate(r) for r in by_event.get(e.id, [])]
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

    # Same-session resolution is only valid for same-session relation types.
    # A prior-session link's identifier names a bill in the PREVIOUS session;
    # resolving it here would attach the current session's unrelated reuse of
    # that number (NY reuses nearly every number every cycle).
    wanted = {
        r.related_identifier.strip()
        for r in rows
        if r.related_bill_id is None
        and r.related_identifier
        and r.relation_type != "prior-session"
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
        if (
            item.related_bill_id is None
            and r.related_identifier
            and r.relation_type != "prior-session"
        ):
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
    # Select the five columns this response actually returns, NOT the whole row.
    #
    # `select(BillDocument)` pulled `extracted_text` and `text_tsv` for every
    # document -- averaging ~10.9 KB per row across the table and reaching into
    # megabytes on long bills -- purely so the loop below could compute one
    # boolean from it. That cost TOAST decompression in Postgres, the bytes on
    # the wire, and the whole payload in Python memory, on a route the website
    # calls for every crawled bill page.
    #
    # The emptiness test moves into SQL. `<> ''` matters as well as NOT NULL:
    # a failed extraction can land an empty string, and `bool('')` was already
    # treating that as "no text" -- keep the two in agreement.
    rows = db.execute(
        select(
            BillDocument.id,
            BillDocument.bill_version_id,
            BillDocument.media_type,
            BillDocument.url,
            (
                (BillDocument.extracted_text.isnot(None))
                & (BillDocument.extracted_text != "")
            ).label("has_extracted_text"),
        )
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .where(BillVersion.bill_id == bill_id)
    ).all()
    return [
        BillDocumentOut(
            id=r.id,
            bill_version_id=r.bill_version_id,
            media_type=r.media_type,
            url=r.url,
            has_extracted_text=bool(r.has_extracted_text),
        )
        for r in rows
    ]


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
