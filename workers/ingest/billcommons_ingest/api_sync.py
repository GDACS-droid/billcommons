"""`api_sync` job handler: incremental Open States v3 API sync for one state.

Per docs/SPEC.md "Refresh targets" and ARCHITECTURE.md's ingestion-tiers
table ("T2 Open States bulk CSV + v3 API (bootstrap + incremental)"), this is
the incremental counterpart to `openstates_bulk.ingest_session_csv_zip`:
instead of a full bulk-CSV bootstrap, it asks the v3 API for bills
`updated_since` the jurisdiction's last successful sync and upserts just the
changed rows.

Quota discipline is a hard requirement here (v3's free tier is ~6 req/min /
250/day, shared across every jurisdiction's sync): each call defaults to
`per_page=20`, caps at `MAX_PAGES_PER_RUN` (10) pages per state per run, and
stops paginating early once a page's bills are all older than
`updated_since` (v3 returns bills newest-updated-first by default, so this
is a correct, not just an optimistic, early-exit).

Upsert scope for this round (a modest, api-specific upsert -- reusing
`openstates_bulk`'s CSV-row-shaped helpers isn't a clean fit since the v3
JSON shape differs from the CSV columns): bills (core fields + latest
action derived from `actions`), bill_actions, and sponsorships. Versions/
documents/votes/subjects are left to the bulk-CSV bootstrap path and a
future incremental-fulltext pass -- logged clearly as a known gap, not
silently dropped.

Auth: requires `OPENSTATES_API_KEY` in the environment (see
`openstates_api.OpenStatesClient._resolve_api_key`); a missing/invalid key
surfaces as `OpenStatesAuthError`/`OpenStatesAPIError` (401), which the
caller (worker dispatch) lets propagate so the job fails with a clear
error message rather than silently no-op'ing. 429s are handled by the
client's own backoff+retry; if that's exhausted the job raises and the
queue's normal exponential backoff takes over for the next attempt.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest.openstates_api import OpenStatesClient
from billcommons_schema.models import (
    Bill,
    BillAction,
    IngestionRun,
    Jurisdiction,
    Session as SessionModel,
    Sponsorship,
)
from billcommons_shared.normalize import normalize_bill_number

SOURCE_NAME = "openstates_api_sync"
DEFAULT_PER_PAGE = 20
MAX_PAGES_PER_RUN = 10
INCLUDE = ["sponsorships", "actions", "sources"]


@dataclass
class ApiSyncResult:
    state: str
    bills_created: int = 0
    bills_updated: int = 0
    bills_unchanged: int = 0
    actions: int = 0
    sponsorships: int = 0
    pages_fetched: int = 0
    warnings: list[str] = field(default_factory=list)


def _bill_checksum(payload: dict) -> str:
    """Same idea as openstates_bulk._bill_checksum but over the v3 JSON
    shape's equivalent fields, so unchanged-since-last-sync bills are
    genuinely skipped rather than rewritten every run."""
    key_fields = (
        payload.get("identifier") or "",
        payload.get("title") or "",
        ",".join(payload.get("classification") or []),
        payload.get("latest_action_description") or "",
        str(payload.get("latest_action_date") or ""),
    )
    return hashlib.sha256("|".join(key_fields).encode("utf-8")).hexdigest()


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _resolve_source_url(payload: dict) -> str | None:
    for source in payload.get("sources") or []:
        url = source.get("url")
        if url:
            return url
    return None


def _resolve_session_row(
    bill_payload: dict,
    sessions_by_identifier: dict[str, SessionModel],
    active_session: SessionModel | None,
) -> SessionModel | None:
    """Resolve a v3 bill payload's `session` string to OUR local session
    row. v3's `session` field is a string session identifier (the same kind
    of value stored in `Session.identifier`), so an exact match against this
    jurisdiction's known sessions is the correct, non-guessing resolution --
    falling back to the jurisdiction's active session only when the
    payload's session string doesn't (yet) match any session row we know
    about (e.g. a brand-new session the registry/bootstrap hasn't seeded
    yet). This is what actually prevents a same-numbered bill in a
    DIFFERENT session from being conflated with the current session's bill
    of the same number (see `bill_by_session_and_identifier_norm`'s key)."""
    session_identifier = bill_payload.get("session")
    if session_identifier:
        matched = sessions_by_identifier.get(session_identifier)
        if matched is not None:
            return matched
    return active_session


def sync_state(
    db: OrmSession,
    jurisdiction: Jurisdiction,
    *,
    client: OpenStatesClient | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    max_pages: int = MAX_PAGES_PER_RUN,
) -> ApiSyncResult:
    """Incrementally sync one jurisdiction via the v3 API. Caller commits.

    `updated_since` is THIS sync pipeline's own watermark: the `finished_at`
    of the jurisdiction's most recent SUCCESSFUL `ingestion_runs` row with
    `source_name == SOURCE_NAME` ("openstates_api_sync") -- i.e. the last
    time an api_sync run actually completed. Falls back to None (a full
    `per_page`*`max_pages` pull) only the first time a jurisdiction is
    api-synced with no prior successful api_sync run recorded.

    Deliberately NOT derived from `jurisdiction_coverage.last_success_at`:
    that field is stamped by `coverage.recompute_coverage_row` on every
    recompute-coverage pass whenever bill_count > 0 -- a read-only counting
    pass that runs on its own schedule, independent of whether an actual
    sync happened. Using it as `updated_since` meant a recompute pass
    between two real syncs would silently advance the watermark past
    upstream changes that occurred in that window, and api_sync would never
    ask the API about them again."""
    client = client or OpenStatesClient()
    result = ApiSyncResult(state=jurisdiction.abbreviation)
    retrieved_at = datetime.now(timezone.utc)

    last_successful_run_finished_at = db.execute(
        select(func.max(IngestionRun.finished_at)).where(
            IngestionRun.jurisdiction_id == jurisdiction.id,
            IngestionRun.source_name == SOURCE_NAME,
            IngestionRun.status == "success",
        )
    ).scalar_one_or_none()
    updated_since = (
        last_successful_run_finished_at.isoformat() if last_successful_run_finished_at is not None else None
    )

    # Every session row for this jurisdiction, keyed by its own `identifier`
    # -- v3's bill `session` field is a string session identifier, and this
    # is how a bill payload's session is resolved to OUR session row (see
    # `_resolve_session_row` below). `active_session` remains the fallback
    # for the (should-be-rare) case where a bill's session string doesn't
    # match any session row we know about yet.
    sessions_by_identifier: dict[str, SessionModel] = {
        s.identifier: s
        for s in db.execute(
            select(SessionModel).where(SessionModel.jurisdiction_id == jurisdiction.id)
        ).scalars()
    }
    active_session = db.execute(
        select(SessionModel)
        .where(SessionModel.jurisdiction_id == jurisdiction.id, SessionModel.active.is_(True))
        .order_by(SessionModel.start_date.desc().nulls_last())
    ).scalars().first()

    # PRIMARY dedup key: openstates_id, which is unique across the whole
    # `bills` table (not just this jurisdiction) and unambiguously identifies
    # ONE bill regardless of which session it's in.
    bill_by_openstates_id: dict[str, Bill] = {
        b.openstates_id: b
        for b in db.execute(
            select(Bill).where(Bill.jurisdiction_id == jurisdiction.id, Bill.openstates_id.is_not(None))
        ).scalars()
    }
    # SECONDARY dedup key for bills without an openstates_id yet (e.g.
    # bulk-CSV-bootstrapped rows the API sync hasn't touched before): keyed
    # by (session_id, identifier_norm), NOT identifier_norm alone -- the same
    # bill number is reused across different sessions/biennia, and keying by
    # identifier_norm only meant a current-session bill could silently
    # overwrite an unrelated historical session's row sharing that number.
    bill_by_session_and_identifier_norm: dict[tuple, Bill] = {
        (b.session_id, b.identifier_norm): b
        for b in db.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalars()
    }

    page = 1
    stop_early = False
    while page <= max_pages and not stop_early:
        payload = client.search_bills(
            jurisdiction=jurisdiction.abbreviation.lower(),
            updated_since=updated_since,
            include=INCLUDE,
            page=page,
            per_page=per_page,
        )
        result.pages_fetched += 1
        bills_payload = payload.get("results", [])
        if not bills_payload:
            break

        # Recorded per-bill AT THE MOMENT each bill is classified
        # created/updated/unchanged (before its checksum is overwritten to
        # match the incoming payload) -- re-deriving "was this bill
        # unchanged" AFTER the loop by comparing bill.checksum against
        # _bill_checksum(payload) is always true by then for every bill on
        # the page (created bills get the new checksum on creation, updated
        # bills get it reassigned), so that comparison can never actually
        # observe a "changed" bill and always trips true. Track it here
        # instead, where "unchanged" genuinely means "not created, not
        # updated".
        page_unchanged_flags: list[bool] = []

        for bill_payload in bills_payload:
            identifier_raw = (bill_payload.get("identifier") or "").strip()
            if not identifier_raw:
                result.warnings.append(
                    f"skipped bill with no identifier (openstates id={bill_payload.get('id')!r})"
                )
                continue
            try:
                identifier_norm = normalize_bill_number(identifier_raw)
            except ValueError:
                identifier_norm = identifier_raw.upper().strip()
                result.warnings.append(
                    f"could not normalize bill identifier {identifier_raw!r}; used raw uppercase"
                )

            checksum = _bill_checksum(bill_payload)
            openstates_id = bill_payload.get("id")

            # PRIMARY match: openstates_id, unique across the whole table --
            # unambiguous regardless of session. Only falls back to the
            # (session_id, identifier_norm) key for bills that don't have an
            # openstates_id recorded yet (e.g. bulk-CSV-bootstrapped rows).
            session_row = _resolve_session_row(bill_payload, sessions_by_identifier, active_session)
            bill = bill_by_openstates_id.get(openstates_id) if openstates_id else None
            if bill is None and session_row is not None:
                bill = bill_by_session_and_identifier_norm.get((session_row.id, identifier_norm))

            if bill is None:
                if session_row is None:
                    result.warnings.append(
                        f"no session row resolved for {jurisdiction.abbreviation} bill "
                        f"{identifier_raw!r} (session={bill_payload.get('session')!r}); skipped"
                    )
                    page_unchanged_flags.append(False)
                    continue
                bill = Bill(
                    jurisdiction_id=jurisdiction.id,
                    session_id=session_row.id,
                    identifier=identifier_raw,
                    identifier_norm=identifier_norm,
                    title=bill_payload.get("title") or "(untitled)",
                    chamber=bill_payload.get("chamber"),
                    bill_type=",".join(bill_payload.get("classification") or []) or None,
                    openstates_id=openstates_id,
                    source_name=SOURCE_NAME,
                    upstream_id=openstates_id,
                    retrieved_at=retrieved_at,
                    checksum=checksum,
                    parser_version="openstates_api_sync/1",
                    source_url=_resolve_source_url(bill_payload),
                )
                db.add(bill)
                db.flush()
                if openstates_id:
                    bill_by_openstates_id[openstates_id] = bill
                bill_by_session_and_identifier_norm[(session_row.id, identifier_norm)] = bill
                result.bills_created += 1
                page_unchanged_flags.append(False)
            elif bill.checksum == checksum:
                result.bills_unchanged += 1
                page_unchanged_flags.append(True)
                # Unchanged -- but v3 pagination is newest-updated-first, so
                # once we've hit a batch of all-unchanged bills we've likely
                # walked past the `updated_since` boundary; still let this
                # bill's children get checked (an action list can genuinely
                # have grown without the bill's own core fields differing).
            else:
                bill.title = bill_payload.get("title") or bill.title
                bill.chamber = bill_payload.get("chamber") or bill.chamber
                classifications = bill_payload.get("classification") or []
                bill.bill_type = ",".join(classifications) if classifications else bill.bill_type
                bill.openstates_id = openstates_id or bill.openstates_id
                bill.source_name = SOURCE_NAME
                bill.upstream_id = openstates_id
                bill.retrieved_at = retrieved_at
                bill.checksum = checksum
                bill.parser_version = "openstates_api_sync/1"
                bill.source_url = bill.source_url or _resolve_source_url(bill_payload)
                result.bills_updated += 1
                page_unchanged_flags.append(False)
                if bill.openstates_id:
                    bill_by_openstates_id[bill.openstates_id] = bill

            _upsert_actions(db, bill, bill_payload.get("actions") or [], result, retrieved_at)
            _upsert_sponsorships(db, bill, bill_payload.get("sponsorships") or [], result, retrieved_at)

        # Early-exit: v3 sorts by most-recently-updated first, so once we've
        # seen a full page where every bill was already unchanged, further
        # pages are strictly older than what we've already synced -- no
        # point spending quota walking the rest of the state's bills.
        all_unchanged_this_page = bool(page_unchanged_flags) and all(page_unchanged_flags)
        pagination = payload.get("pagination", {})
        max_page = pagination.get("max_page", page)
        if all_unchanged_this_page or page >= max_page:
            stop_early = True
        page += 1

    db.flush()
    return result


def _upsert_actions(
    db: OrmSession, bill: Bill, action_payloads: list[dict], result: ApiSyncResult, retrieved_at: datetime
) -> None:
    if not action_payloads:
        return
    existing = {
        (a.description, a.order)
        for a in db.execute(select(BillAction).where(BillAction.bill_id == bill.id)).scalars()
    }
    latest_date = None
    latest_text = None
    for i, action_payload in enumerate(action_payloads):
        description = action_payload.get("description") or ""
        order = i
        key = (description, order)
        action_date = _parse_date(action_payload.get("date"))
        if key not in existing:
            db.add(
                BillAction(
                    bill_id=bill.id,
                    description=description,
                    action_date=action_date,
                    classification=",".join(action_payload.get("classification") or []) or None,
                    order=order,
                    source_name=SOURCE_NAME,
                    retrieved_at=retrieved_at,
                )
            )
            existing.add(key)
            result.actions += 1
        if action_date is not None:
            latest_date = action_date
            latest_text = description
    if latest_date is not None:
        bill.latest_action_date = latest_date
        bill.latest_action_text = latest_text
    db.flush()


def _upsert_sponsorships(
    db: OrmSession, bill: Bill, sponsorship_payloads: list[dict], result: ApiSyncResult, retrieved_at: datetime
) -> None:
    if not sponsorship_payloads:
        return
    existing = {
        (s.name, s.classification)
        for s in db.execute(select(Sponsorship).where(Sponsorship.bill_id == bill.id)).scalars()
    }
    for sponsorship_payload in sponsorship_payloads:
        name = sponsorship_payload.get("name")
        classification = sponsorship_payload.get("classification")
        key = (name, classification)
        if key not in existing:
            db.add(
                Sponsorship(
                    bill_id=bill.id,
                    name=name,
                    classification=classification,
                    primary=bool(sponsorship_payload.get("primary")),
                    source_name=SOURCE_NAME,
                    retrieved_at=retrieved_at,
                )
            )
            existing.add(key)
            result.sponsorships += 1
    db.flush()


def run_api_sync_job(db: OrmSession, state: str, *, client: OpenStatesClient | None = None) -> ApiSyncResult:
    """Entry point for the worker dispatch: resolve the jurisdiction, run
    the sync, and record an `ingestion_runs` row. Caller commits."""
    jurisdiction = db.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation == state.upper())
    ).scalar_one_or_none()
    if jurisdiction is None:
        raise ValueError(f"no jurisdiction row for state {state!r}; run seed-registry first")

    run = IngestionRun(
        jurisdiction_id=jurisdiction.id,
        source_name=SOURCE_NAME,
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    db.add(run)
    db.flush()

    try:
        result = sync_state(db, jurisdiction, client=client)
        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        run.bills_created = result.bills_created
        run.bills_updated = result.bills_updated
        db.flush()
        return result
    except Exception as exc:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.error = str(exc)[:4000]
        db.flush()
        raise
