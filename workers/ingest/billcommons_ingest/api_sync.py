"""`api_sync` job handler: incremental Open States v3 API sync for one state.

Per docs/SPEC.md "Refresh targets" and ARCHITECTURE.md's ingestion-tiers
table ("T2 Open States bulk CSV + v3 API (bootstrap + incremental)"), this is
the incremental counterpart to `openstates_bulk.ingest_session_csv_zip`:
instead of a full bulk-CSV bootstrap, it asks the v3 API for bills
`updated_since` the jurisdiction's last successful sync and upserts just the
changed rows.

Quota discipline is a hard requirement here (v3's free tier is ~6 req/min /
250/day, shared across every jurisdiction's sync): each call defaults to
`per_page=20` and caps at `MAX_PAGES_PER_RUN` (10) pages per state per run.
Pagination stops only on an empty page, upstream's own `pagination.max_page`,
or that page budget -- NOT on a page whose bills all had an unchanged core
checksum: a version/document can change upstream without moving a bill's
core fields, so a core-unchanged page is not evidence later pages hold no
changes (this WAS an early-exit here; removed, see `sync_state`'s docstring
and `result.next_page`/`result.max_page_seen`, which expose a budget-truncated
run instead of silently treating it as complete).

Upsert scope for this round: bills (core fields + latest action derived
from `actions`), bill_actions, sponsorships, and -- as of the
versions/documents repair -- bill_versions and bill_documents, upserted by
`_upsert_versions_and_documents` using the exact same natural keys and
placeholder-version convention as `openstates_bulk`'s bulk-CSV bootstrap
path (see that module's "Versions + version links" / "Documents + document
links" sections). Votes/subjects remain out of scope for this incremental
path and are left to the bulk-CSV bootstrap.

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
from billcommons_ingest.openstates_bulk import _parse_date_field
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    IngestionRun,
    Jurisdiction,
    Session as SessionModel,
    Sponsorship,
)
from billcommons_ingest import events
from billcommons_shared.normalize import normalize_bill_number

SOURCE_NAME = "openstates_api_sync"
DEFAULT_PER_PAGE = 20
MAX_PAGES_PER_RUN = 10
INCLUDE = ["sponsorships", "actions", "sources", "versions", "documents"]


@dataclass
class ApiSyncResult:
    state: str
    bills_created: int = 0
    bills_updated: int = 0
    bills_unchanged: int = 0
    actions: int = 0
    sponsorships: int = 0
    versions: int = 0
    documents: int = 0
    pages_fetched: int = 0
    # Largest `pagination.max_page` this call actually observed from
    # upstream (across all pages fetched this call, not just the last).
    max_page_seen: int = 0
    # The next page a caller should ask for to continue this same
    # updated_since/override window -- None means this call's own budget
    # reached upstream's own max_page (or an empty page), i.e. genuinely
    # caught up, not merely "we stopped". Non-None means the page budget
    # ended before upstream did; this is a resumable cursor, not a claim of
    # completeness.
    next_page: int | None = None
    warnings: list[str] = field(default_factory=list)
    # Bills this run created or wrote actions to. Reported explicitly rather
    # than re-derived afterwards from `updated_at >= <cycle start>`: that test
    # compares a DB-written column against a worker-host clock, and any skew,
    # any run whose `retrieved_at` predates the cycle stamp, or any bill
    # stamped forward-only to an older value drops out of the window silently.
    # A bill missing from that window never gets its status re-derived, so the
    # field keeps serving a stale answer with no error anywhere.
    touched_bill_ids: set = field(default_factory=set)


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
    result: ApiSyncResult,
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
    of the same number (see `bill_by_session_and_identifier_norm`'s key).

    Falling back silently used to be a real misassignment risk with no
    operator-visible trace: a payload session string that doesn't match any
    known session row gets silently attributed to whatever session happens
    to be `active` right now, which is WRONG whenever that's a different
    session than the one the bill is actually in. This logs a warning to
    `result.warnings` (the same operator-visible channel the "no session row
    resolved" skip path a few lines up in `sync_state` already uses) any
    time this fallback fires with a NON-empty session_identifier that
    genuinely didn't match -- i.e. every case where we can't prove the
    fallback is even correct, not merely every case where session is
    entirely absent from the payload."""
    session_identifier = bill_payload.get("session")
    if session_identifier:
        matched = sessions_by_identifier.get(session_identifier)
        if matched is not None:
            return matched
        result.warnings.append(
            f"bill payload session={session_identifier!r} did not match any known "
            f"session row for this jurisdiction; falling back to the active session "
            f"({active_session.identifier if active_session else 'none'}) -- this bill "
            f"may be misassigned if it actually belongs to a different, not-yet-seeded session"
        )
    return active_session


def sync_state(
    db: OrmSession,
    jurisdiction: Jurisdiction,
    *,
    client: OpenStatesClient | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    max_pages: int = MAX_PAGES_PER_RUN,
    updated_since_override: str | None = None,
    start_page: int = 1,
) -> ApiSyncResult:
    """Incrementally sync one jurisdiction via the v3 API. Caller commits.

    `max_pages` is the number of pages THIS CALL is allowed to fetch,
    starting at `start_page` -- not an absolute page number. The largest
    `pagination.max_page` this call observed from upstream is recorded on
    `result.max_page_seen`; if the budget runs out before reaching it,
    `result.next_page` is set to the next page a caller should resume from
    (else left `None`, meaning this call's own requests genuinely reached
    upstream's last page or an empty page -- caught up, not merely stopped).

    `updated_since_override`, if given, is used verbatim as the `updated_since`
    query param instead of the computed watermark below, and this call does
    NOT read/derive the normal watermark from `ingestion_runs` at all in that
    case. This is the explicit-`--since` catch-up/replay path
    (`backfill-api-versions` in cli.py): it must never read or advance the
    ordinary incremental-sync watermark, so ordinary `run_api_sync_job` calls
    (which never pass this) are completely unaffected by any replay.

    `start_page`, similarly, only matters to that same replay path -- ordinary
    calls always start at page 1 (the default).

    Absent an override, `updated_since` is THIS sync pipeline's own watermark: the `started_at`
    (NOT `finished_at`) of the jurisdiction's most recent SUCCESSFUL
    `ingestion_runs` row with `source_name == SOURCE_NAME`
    ("openstates_api_sync"). Falls back to None (a full `per_page`*`max_pages`
    pull) only the first time a jurisdiction is api-synced with no prior
    successful api_sync run recorded.

    Deliberately `started_at`, not `finished_at`: a run takes real wall-clock
    time to page through the v3 API (potentially minutes, given
    MAX_PAGES_PER_RUN's quota-conscious pacing), and an upstream bill update
    that lands ON THE OPEN STATES SIDE mid-run -- after this run's OWN
    `search_bills` calls have already fetched their pages, but before this
    run's `finished_at` is stamped -- would fall in the window
    (run_start, run_finish) and be silently skipped forever: it's newer than
    `started_at` (so a NEXT run using `started_at` would still ask about it)
    but older than `finished_at` (so a next run using `finished_at` would
    treat it as already covered by THIS run, even though this run's actual
    API calls never saw it because it hadn't landed yet when they fired).
    Using `started_at` accepts a small amount of guaranteed-safe overlap
    (bills already synced this run get asked about again next run and are
    filtered out by the checksum-unchanged branch) in exchange for never
    missing a bill that changed upstream during the run.

    Deliberately NOT derived from `jurisdiction_coverage.last_success_at`:
    that field is stamped by `coverage.recompute_coverage_row` on every
    recompute-coverage pass whenever bill_count > 0 -- a read-only counting
    pass that runs on its own schedule, independent of whether an actual
    sync happened. Using it as `updated_since` meant a recompute pass
    between two real syncs would silently advance the watermark past
    upstream changes that occurred in that window, and api_sync would never
    ask the API about them again."""
    if start_page < 1:
        raise ValueError(f"start_page must be >= 1, got {start_page!r}")
    if max_pages < 1:
        raise ValueError(f"max_pages must be >= 1, got {max_pages!r}")

    client = client or OpenStatesClient()
    result = ApiSyncResult(state=jurisdiction.abbreviation)
    retrieved_at = datetime.now(timezone.utc)

    if updated_since_override is not None:
        updated_since = updated_since_override
    else:
        last_successful_run_started_at = db.execute(
            select(func.max(IngestionRun.started_at)).where(
                IngestionRun.jurisdiction_id == jurisdiction.id,
                IngestionRun.source_name == SOURCE_NAME,
                IngestionRun.status == "success",
            )
        ).scalar_one_or_none()
        updated_since = (
            last_successful_run_started_at.isoformat() if last_successful_run_started_at is not None else None
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

    # `page` walks from `start_page`; `pages_fetched_this_call` bounds THIS
    # call's own request count at `max_pages`, independent of `page`'s
    # absolute value (a replay chunk starting at page 37 with max_pages=5
    # must fetch exactly 5 pages, not stop at page 5).
    #
    # No unchanged-page early exit here (removed, see module docstring):
    # a page can be entirely core-checksum-unchanged while still carrying a
    # NEW version/document for one of its bills (a version/document change
    # doesn't move `_bill_checksum`'s fields), so "every bill on this page
    # was core-unchanged" is not evidence later pages hold no changes.
    # Pagination now stops only on an empty result, upstream's own
    # `pagination.max_page`, or this call's page budget.
    page = start_page
    pages_fetched_this_call = 0
    while pages_fetched_this_call < max_pages:
        payload = client.search_bills(
            jurisdiction=jurisdiction.abbreviation.lower(),
            updated_since=updated_since,
            include=INCLUDE,
            page=page,
            per_page=per_page,
        )
        result.pages_fetched += 1
        pages_fetched_this_call += 1
        pagination = payload.get("pagination", {})
        upstream_max_page = pagination.get("max_page", page)
        result.max_page_seen = max(result.max_page_seen, upstream_max_page)

        bills_payload = payload.get("results", [])
        if not bills_payload:
            result.next_page = None
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
            openstates_id = bill_payload.get("id")

            # PRIMARY match: openstates_id, unique across the whole table --
            # unambiguous regardless of session. Only falls back to the
            # (session_id, identifier_norm) key for bills that don't have an
            # openstates_id recorded yet (e.g. bulk-CSV-bootstrapped rows).
            session_row = _resolve_session_row(bill_payload, sessions_by_identifier, active_session, result)
            bill = bill_by_openstates_id.get(openstates_id) if openstates_id else None
            if bill is None and session_row is not None:
                bill = bill_by_session_and_identifier_norm.get((session_row.id, identifier_norm))

            if bill is None:
                if session_row is None:
                    result.warnings.append(
                        f"no session row resolved for {jurisdiction.abbreviation} bill "
                        f"{identifier_raw!r} (session={bill_payload.get('session')!r}); skipped"
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
                result.touched_bill_ids.add(bill.id)
                events.record_event(db, bill.id, events.CREATED, identifier_raw)
            elif bill.checksum == checksum:
                result.bills_unchanged += 1
                # Unchanged -- but v3 pagination is newest-updated-first, so
                # once we've hit a batch of all-unchanged bills we've likely
                # walked past the `updated_since` boundary; still let this
                # bill's children get checked (an action list can genuinely
                # have grown without the bill's own core fields differing).
                #
                # Backfill openstates_id even on this "nothing else changed"
                # branch: a bulk-CSV-bootstrapped row (matched here via the
                # SECONDARY session+identifier_norm key precisely because it
                # had no openstates_id yet) would otherwise NEVER graduate to
                # the PRIMARY openstates_id dedup key as long as its core
                # fields stayed checksum-identical across every future sync --
                # a real, plausible steady state for a bill that's done
                # moving. Left unfixed, every future sync for that bill keeps
                # falling back to the weaker secondary key indefinitely.
                if openstates_id and not bill.openstates_id:
                    bill.openstates_id = openstates_id
                    bill.upstream_id = openstates_id
                    bill_by_openstates_id[openstates_id] = bill
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
                result.touched_bill_ids.add(bill.id)
                events.record_event(db, bill.id, events.METADATA)
                if bill.openstates_id:
                    bill_by_openstates_id[bill.openstates_id] = bill

            # Called for EVERY bill on the page, including the
            # checksum-unchanged branch above: `_bill_checksum` covers only
            # identifier/title/classification/latest-action, so a bill whose
            # core fields didn't move can still have a new version or
            # document upstream.
            _upsert_versions_and_documents(
                db, bill, bill_payload.get("versions") or [], bill_payload.get("documents") or [], result, retrieved_at
            )
            _upsert_actions(db, bill, bill_payload.get("actions") or [], result, retrieved_at)
            _upsert_sponsorships(db, bill, bill_payload.get("sponsorships") or [], result, retrieved_at)

        if page >= upstream_max_page:
            result.next_page = None
            break
        if pages_fetched_this_call >= max_pages:
            result.next_page = page + 1
            break
        page += 1

    if updated_since_override is None and result.next_page is not None:
        result.warnings.append(
            f"api-sync {jurisdiction.abbreviation}: TRUNCATED -- this run's page budget "
            f"({max_pages}) ended before upstream's own last page (max_page_seen="
            f"{result.max_page_seen}); result.next_page={result.next_page} was NOT followed "
            f"(ordinary sync only ever starts at page 1 next time). Some upstream-changed "
            f"bills/versions/documents beyond this run's budget may not have been synced this "
            f"cycle -- this is NOT silent: see backfill-api-versions for an explicit, bounded "
            f"catch-up replay of a specific --since window."
        )

    db.flush()
    return result


def _upsert_versions_and_documents(
    db: OrmSession,
    bill: Bill,
    version_payloads: list[dict],
    document_payloads: list[dict],
    result: ApiSyncResult,
    retrieved_at: datetime,
) -> bool:
    """Upsert one bill's v3 `versions[]`/`documents[]` into `bill_versions`/
    `bill_documents`, using EXACTLY the same natural keys and
    placeholder-version convention as `openstates_bulk`'s bulk-CSV bootstrap
    (see that module's "Versions + version links" / "Documents + document
    links" sections) -- this is the same canonical representation, just fed
    from the v3 JSON shape instead of CSV rows, so the two paths can never
    produce two different representations of the same upstream fact.

    Real versions (`versions[]`) key on `(bill_id, note, date)`, same as
    bulk. Standalone documents (`documents[]`, not tied to any version) all
    land under one synthetic placeholder version per bill --
    `note="(document, no version)"`, `date=None` -- deduplicated by URL,
    never one placeholder per document object.

    Append/idempotent-upsert only: never deletes a local version/document
    just because it's absent from one API response (matches bulk; protects
    live printed/shared URLs from an incomplete upstream page).

    Returns True iff at least one BillVersion/BillDocument row was added,
    so the caller can mark `bill.id` touched. Flushes once before
    returning so a caller-added `bill.id` reference (if any) sees the
    child rows. Caller (`sync_state`) owns commit/rollback.
    """
    if not version_payloads and not document_payloads:
        return False

    added_any = False

    # ONE preload query per bill for each of versions/documents -- never an
    # existence SELECT per nested link.
    existing_versions: dict[tuple, BillVersion] = {
        (v.bill_id, v.note, v.date): v
        for v in db.execute(select(BillVersion).where(BillVersion.bill_id == bill.id)).scalars()
    }
    version_ids = [v.id for v in existing_versions.values()]
    existing_documents: set[tuple] = set()
    if version_ids:
        existing_documents = {
            (d.bill_version_id, d.url)
            for d in db.execute(
                select(BillDocument).where(BillDocument.bill_version_id.in_(version_ids))
            ).scalars()
        }

    for version_payload in version_payloads:
        note = version_payload.get("note") or ""
        version_date = _parse_date_field(version_payload.get("date"))
        key = (bill.id, note, version_date)
        version = existing_versions.get(key)
        if version is None:
            # Client-side id so nested links can reference it before flush
            # (mirrors openstates_bulk's identical VoteEvent/BillVersion
            # pattern -- avoids a flush-per-version round trip).
            version = BillVersion(
                id=uuid.uuid4(),
                bill_id=bill.id,
                note=note,
                date=version_date,
                source_name=SOURCE_NAME,
                retrieved_at=retrieved_at,
            )
            db.add(version)
            existing_versions[key] = version
            result.versions += 1
            added_any = True

        for link in version_payload.get("links") or []:
            url = link.get("url")
            if not url:
                continue
            doc_key = (version.id, url)
            if doc_key not in existing_documents:
                db.add(
                    BillDocument(
                        bill_version_id=version.id,
                        media_type=link.get("media_type"),
                        url=url,
                        source_name=SOURCE_NAME,
                        retrieved_at=retrieved_at,
                    )
                )
                existing_documents.add(doc_key)
                result.documents += 1
                added_any = True

    if document_payloads:
        placeholder_key = (bill.id, "(document, no version)", None)
        placeholder = existing_versions.get(placeholder_key)
        for document_payload in document_payloads:
            for link in document_payload.get("links") or []:
                url = link.get("url")
                if not url:
                    continue
                if placeholder is None:
                    placeholder = BillVersion(
                        id=uuid.uuid4(),
                        bill_id=bill.id,
                        note="(document, no version)",
                        date=None,
                        source_name=SOURCE_NAME,
                        retrieved_at=retrieved_at,
                        license_note="synthetic placeholder: doc had no matching version row",
                    )
                    db.add(placeholder)
                    existing_versions[placeholder_key] = placeholder
                    added_any = True
                doc_key = (placeholder.id, url)
                if doc_key not in existing_documents:
                    db.add(
                        BillDocument(
                            bill_version_id=placeholder.id,
                            media_type=link.get("media_type"),
                            url=url,
                            source_name=SOURCE_NAME,
                            retrieved_at=retrieved_at,
                        )
                    )
                    existing_documents.add(doc_key)
                    result.documents += 1
                    added_any = True

    if added_any:
        result.touched_bill_ids.add(bill.id)

    db.flush()
    return added_any


def _upsert_actions(
    db: OrmSession, bill: Bill, action_payloads: list[dict], result: ApiSyncResult, retrieved_at: datetime
) -> None:
    if not action_payloads:
        return
    # Keyed to the ROW, not just its identity, so a corrected action can be
    # detected. Upstream routinely revises actions in place: 42% arrive with
    # no classification at all and get one later, which is precisely the event
    # that moves a bill's derived status. Matching on existence alone meant an
    # action reclassified to `executive-signature` was never written, never
    # re-derived, and never announced -- the bill kept reporting `introduced`
    # after it had been signed, permanently, with nothing logged anywhere.
    existing = {
        (a.description, a.order): a
        for a in db.execute(select(BillAction).where(BillAction.bill_id == bill.id)).scalars()
    }
    latest_date = None
    latest_text = None
    added = 0
    revised = 0
    for i, action_payload in enumerate(action_payloads):
        description = action_payload.get("description") or ""
        order = i
        key = (description, order)
        action_date = _parse_date(action_payload.get("date"))
        classification = ",".join(action_payload.get("classification") or []) or None
        current = existing.get(key)
        if current is not None:
            # Only the two fields the status derivation actually reads. A
            # no-op assignment would still leave the row clean, so this cannot
            # churn updated_at on an unchanged sync.
            changed_fields = []
            if current.action_date != action_date:
                current.action_date = action_date
                changed_fields.append("date")
            if current.classification != classification:
                current.classification = classification
                changed_fields.append("classification")
            if changed_fields:
                current.retrieved_at = retrieved_at
                revised += 1
        else:
            new_action = BillAction(
                bill_id=bill.id,
                description=description,
                action_date=action_date,
                classification=classification,
                order=order,
                source_name=SOURCE_NAME,
                retrieved_at=retrieved_at,
            )
            db.add(new_action)
            existing[key] = new_action
            result.actions += 1
            added += 1
        if action_date is not None:
            latest_date = action_date
            latest_text = description
    if latest_date is not None:
        bill.latest_action_date = latest_date
        bill.latest_action_text = latest_text
    if added or revised:
        # A REVISED action counts as a change to the bill for exactly the same
        # reason a new one does: it can move the derived status. An action
        # gaining a `failure` or `executive-signature` classification is the
        # whole event, and before this it was invisible on every channel --
        # not written, not re-derived, not announced.
        if bill.updated_at is None or retrieved_at > bill.updated_at:
            bill.updated_at = retrieved_at
        result.touched_bill_ids.add(bill.id)
        # One event per sync for the whole batch, not one per action: a
        # consumer refetches the bill's action list either way, and a bill
        # importing 40 historical actions should not occupy 40 pages of every
        # subscriber's feed.
        detail = ", ".join(
            part
            for part in (
                f"{added} action(s) added" if added else "",
                f"{revised} action(s) revised" if revised else "",
            )
            if part
        )
        events.record_event(db, bill.id, events.ACTIONS, detail)
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
    added = 0
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
            added += 1
    if added:
        # This path wrote child rows and never touched the parent, so before
        # the change log existed a new cosponsor on a watched bill was
        # invisible to consumers -- one of the two gaps that motivated moving
        # off `bills.updated_at`.
        result.touched_bill_ids.add(bill.id)
        events.record_event(db, bill.id, events.SPONSORS, f"{added} sponsor(s) added")
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
