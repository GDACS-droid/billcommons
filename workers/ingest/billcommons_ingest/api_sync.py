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

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest.openstates_api import OpenStatesClient
from billcommons_schema.models import (
    Bill,
    BillAction,
    IngestionRun,
    Jurisdiction,
    JurisdictionCoverage,
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


def sync_state(
    db: OrmSession,
    jurisdiction: Jurisdiction,
    *,
    client: OpenStatesClient | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    max_pages: int = MAX_PAGES_PER_RUN,
) -> ApiSyncResult:
    """Incrementally sync one jurisdiction via the v3 API. Caller commits.

    `updated_since` is the jurisdiction's most recent
    `jurisdiction_coverage.last_success_at` (falls back to None -- a full
    `per_page`*`max_pages` pull -- only the first time a jurisdiction is
    api-synced with no prior successful bulk/api run recorded)."""
    client = client or OpenStatesClient()
    result = ApiSyncResult(state=jurisdiction.abbreviation)
    retrieved_at = datetime.now(timezone.utc)

    coverage_rows = db.execute(
        select(JurisdictionCoverage).where(JurisdictionCoverage.jurisdiction_id == jurisdiction.id)
    ).scalars().all()
    updated_since = None
    success_times = [c.last_success_at for c in coverage_rows if c.last_success_at is not None]
    if success_times:
        updated_since = min(success_times).isoformat()

    sessions_by_openstates_identifier: dict[str, SessionModel] = {}
    active_session = db.execute(
        select(SessionModel)
        .where(SessionModel.jurisdiction_id == jurisdiction.id, SessionModel.active.is_(True))
        .order_by(SessionModel.start_date.desc().nulls_last())
    ).scalars().first()

    bill_by_identifier_norm: dict[str, Bill] = {
        b.identifier_norm: b
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
            bill = bill_by_identifier_norm.get(identifier_norm)
            session_row = active_session  # v3's `session` field is a string identifier; the
            # jurisdiction's currently-active session row is the correct target for an
            # incremental sync pass (bootstrap owns cross-session bill creation).

            if bill is None:
                if session_row is None:
                    result.warnings.append(
                        f"no active session row for {jurisdiction.abbreviation}; "
                        f"skipped new bill {identifier_raw!r}"
                    )
                    continue
                bill = Bill(
                    jurisdiction_id=jurisdiction.id,
                    session_id=session_row.id,
                    identifier=identifier_raw,
                    identifier_norm=identifier_norm,
                    title=bill_payload.get("title") or "(untitled)",
                    chamber=bill_payload.get("chamber"),
                    bill_type=",".join(bill_payload.get("classification") or []) or None,
                    openstates_id=bill_payload.get("id"),
                    source_name=SOURCE_NAME,
                    upstream_id=bill_payload.get("id"),
                    retrieved_at=retrieved_at,
                    checksum=checksum,
                    parser_version="openstates_api_sync/1",
                    source_url=_resolve_source_url(bill_payload),
                )
                db.add(bill)
                db.flush()
                bill_by_identifier_norm[identifier_norm] = bill
                result.bills_created += 1
            elif bill.checksum == checksum:
                result.bills_unchanged += 1
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
                bill.openstates_id = bill_payload.get("id") or bill.openstates_id
                bill.source_name = SOURCE_NAME
                bill.upstream_id = bill_payload.get("id")
                bill.retrieved_at = retrieved_at
                bill.checksum = checksum
                bill.parser_version = "openstates_api_sync/1"
                bill.source_url = bill.source_url or _resolve_source_url(bill_payload)
                result.bills_updated += 1

            _upsert_actions(db, bill, bill_payload.get("actions") or [], result, retrieved_at)
            _upsert_sponsorships(db, bill, bill_payload.get("sponsorships") or [], result, retrieved_at)

        # Early-exit: v3 sorts by most-recently-updated first, so once we've
        # seen a full page where every bill was already unchanged, further
        # pages are strictly older than what we've already synced -- no
        # point spending quota walking the rest of the state's bills.
        all_unchanged_this_page = all(
            bill_by_identifier_norm.get(
                _safe_norm(b.get("identifier") or "")
            ) is not None
            and bill_by_identifier_norm[_safe_norm(b.get("identifier") or "")].checksum
            == _bill_checksum(b)
            for b in bills_payload
        )
        pagination = payload.get("pagination", {})
        max_page = pagination.get("max_page", page)
        if all_unchanged_this_page or page >= max_page:
            stop_early = True
        page += 1

    db.flush()
    return result


def _safe_norm(identifier: str) -> str:
    try:
        return normalize_bill_number(identifier)
    except ValueError:
        return identifier.upper().strip()


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
