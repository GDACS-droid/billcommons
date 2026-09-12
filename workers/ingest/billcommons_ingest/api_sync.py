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
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest.openstates_api import OpenStatesClient, RetainedOpenStatesResponse
from billcommons_ingest.openstates_bulk import _parse_date_field
from billcommons_ingest.session_match import MatchPath, SessionCandidate, resolve_session
from billcommons_ingest import corpus_update_evidence
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    ApiSyncSnapshotBlocker,
    IngestionRun,
    Jurisdiction,
    Session as SessionModel,
    Sponsorship,
)
from billcommons_ingest import events
from billcommons_shared.normalize import normalize_bill_number

SOURCE_NAME = "openstates_api_sync"
CA_OFFICIAL_ACTION_SOURCE_PREFIX = "ca_official_action_sweep/"
DEFAULT_PER_PAGE = 20
MAX_PAGES_PER_RUN = 10
INCLUDE = ["sponsorships", "actions", "sources", "versions", "documents", "abstracts"]
SNAPSHOT_BLOCKER_PROCESSING_VERSION = "openstates_api_sync_snapshot_overflow/1"


class ApiSyncConcurrencyBusy(RuntimeError):
    """Another transaction is already snapshotting this jurisdiction/source."""


def _normalize_action_description(value: str | None) -> str:
    """Whitespace-stable action identity shared with the CA official sweep."""
    return " ".join((value or "").split())


def _is_ca_official_action(action: BillAction) -> bool:
    return (getattr(action, "source_name", None) or "").startswith(CA_OFFICIAL_ACTION_SOURCE_PREFIX)


@dataclass
class ApiSyncResult:
    state: str
    # Exact lower bound used for every page in this scan.  Continuation jobs
    # must carry it verbatim; recomputing it between chunks can change the
    # result set under offset pagination.
    updated_since_used: str | None = None
    # The first chunk's start time is the safe next watermark once every page
    # completes.  A later continuation's own wall-clock start would skip
    # updates that arrived while the multi-job scan was running.
    cycle_started_at: datetime | None = None
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
    # Exact corpus ledger rows created by this successful sync transaction.
    # The status worker receives this map only after committing the job, so it
    # never guesses from historical evidence when it records a derivation.
    corpus_evidence_by_bill: dict = field(default_factory=dict)
    # Typed evidence-cap failures are isolated bill-by-bill.  This count is
    # operational context only; the durable blocker is the authority that
    # prevents a cycle from becoming a successful watermark.
    snapshot_overflows: int = 0
    # Set by the job wrapper after it queries durable blockers for the whole
    # jurisdiction/source, including blockers created by an earlier chunk.
    snapshot_blockers_remaining: bool = False


def _snapshot_blocker_identity(*, session_id: object, identifier_norm: str) -> str:
    """Return a safe, deterministic identity for an overflowed source bill."""
    # This natural key deliberately remains stable if Open States later adds
    # an id to a previously id-less response.  A changed identifier/session
    # is a different source identity and must not clear an older blocker.
    identity = f"local-natural-key\x00{session_id}\x00{identifier_norm}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _api_sync_lock_key(jurisdiction_id: uuid.UUID) -> int:
    """Stable signed bigint advisory-lock key for one jurisdiction/source."""
    digest = hashlib.sha256(f"{SOURCE_NAME}\x00{jurisdiction_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _acquire_api_sync_snapshot_lock(db: OrmSession, jurisdiction: Jurisdiction) -> None:
    """Acquire the transaction-scoped snapshot lock or reject a concurrent scan.

    This is deliberately nonblocking.  Queue callers record the ordinary job
    failure/backoff; direct callers receive a typed exception and must retry.
    Returning a partial/success result while another transaction owns the
    snapshot would permit an unsafe watermark advance.
    """
    acquired = db.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
        {"lock_key": _api_sync_lock_key(jurisdiction.id)},
    ).scalar_one()
    if not acquired:
        raise ApiSyncConcurrencyBusy(
            "api-sync snapshot is already running for this jurisdiction and source"
        )


def _record_snapshot_overflow_blocker(
    db: OrmSession,
    *,
    jurisdiction: Jurisdiction,
    bill_id: uuid.UUID | None,
    source_identity_sha256: str,
    component: str,
    record_cap: int,
    seen_at: datetime,
    cycle_started_at: datetime,
    cycle_start_page: int,
    updated_since: str | None,
) -> None:
    """Atomically create or reactivate the record outside the failed savepoint."""
    updated_since_sha256 = (
        hashlib.sha256(updated_since.encode("utf-8")).hexdigest()
        if updated_since is not None
        else None
    )
    statement = insert(ApiSyncSnapshotBlocker).values(
        jurisdiction_id=jurisdiction.id,
        bill_id=bill_id,
        source_name=SOURCE_NAME,
        source_identity_sha256=source_identity_sha256,
        component=component,
        record_cap=record_cap,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        active=True,
        processing_version=SNAPSHOT_BLOCKER_PROCESSING_VERSION,
        cycle_started_at=cycle_started_at,
        cycle_start_page=cycle_start_page,
        updated_since_sha256=updated_since_sha256,
    )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=(
                ApiSyncSnapshotBlocker.jurisdiction_id,
                ApiSyncSnapshotBlocker.source_name,
                ApiSyncSnapshotBlocker.source_identity_sha256,
            ),
            set_={
                "bill_id": func.coalesce(statement.excluded.bill_id, ApiSyncSnapshotBlocker.bill_id),
                "component": statement.excluded.component,
                "record_cap": statement.excluded.record_cap,
                "last_seen_at": statement.excluded.last_seen_at,
                "active": True,
                "resolved_at": None,
                "processing_version": statement.excluded.processing_version,
                "cycle_started_at": statement.excluded.cycle_started_at,
                "cycle_start_page": statement.excluded.cycle_start_page,
                "updated_since_sha256": statement.excluded.updated_since_sha256,
                "updated_at": func.now(),
            },
        )
    )


def _resolve_snapshot_overflow_blocker(
    db: OrmSession,
    *,
    jurisdiction: Jurisdiction,
    source_identity_sha256: str,
    resolved_at: datetime,
) -> None:
    """Resolve only the matching blocker after its full snapshot succeeded."""
    db.execute(
        update(ApiSyncSnapshotBlocker)
        .where(
            ApiSyncSnapshotBlocker.jurisdiction_id == jurisdiction.id,
            ApiSyncSnapshotBlocker.source_name == SOURCE_NAME,
            ApiSyncSnapshotBlocker.active.is_(True),
            ApiSyncSnapshotBlocker.source_identity_sha256 == source_identity_sha256,
        )
        .values(active=False, resolved_at=resolved_at, updated_at=func.now())
    )


def _has_active_snapshot_blockers(db: OrmSession, jurisdiction: Jurisdiction) -> bool:
    return db.execute(
        select(ApiSyncSnapshotBlocker.id)
        .where(
            ApiSyncSnapshotBlocker.jurisdiction_id == jurisdiction.id,
            ApiSyncSnapshotBlocker.source_name == SOURCE_NAME,
            ApiSyncSnapshotBlocker.active.is_(True),
        )
        .limit(1)
    ).scalar_one_or_none() is not None


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
        _resolve_abstract(payload) or "",
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


def _resolve_abstract(payload: dict) -> str | None:
    """Return the first non-empty current v3 abstract without fabricating one.

    Open States supplies abstracts as included child objects rather than as a
    bill scalar.  The API has no durable local abstract model, so the current
    summary belongs in ``Bill.description`` (the same representation the bulk
    importer uses).  Empty/whitespace-only entries are not a replacement for a
    useful existing description.
    """
    for abstract in payload.get("abstracts") or []:
        if not isinstance(abstract, dict):
            continue
        text = abstract.get("abstract")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _resolve_session_row(
    bill_payload: dict,
    sessions_by_identifier: dict[str, SessionModel],
    sessions_by_name: dict[str, list[SessionModel]],
    active_session: SessionModel | None,
    result: ApiSyncResult,
) -> SessionModel | None:
    """Resolve a v3 bill payload's session without cross-session guessing.

    A missing/blank payload session is the one case where active-session
    fallback remains legitimate.  A nonempty unknown value is evidence that
    this bill belongs somewhere specific, so assigning it to the current
    session would silently corrupt session-scoped bill identity; it therefore
    fails closed and the caller skips the bill.

    Open States' API uses compact session slugs for some jurisdictions (CA's
    ``20252026`` and ``20252026 Special Session 1`` are the important cases),
    while registry/bootstrap rows use human identifiers.  After exact local
    identifier/name lookup, reuse the shared bulk-import matcher.  It accepts
    only a unique year-and-classification-compatible candidate, so regular
    and special sessions cannot be conflated and ambiguity still fails closed.
    """
    raw_session = bill_payload.get("session")
    if raw_session is None or (isinstance(raw_session, str) and not raw_session.strip()):
        return active_session
    if not isinstance(raw_session, str):
        result.warnings.append(
            f"bill payload has non-string session={raw_session!r}; skipped rather than "
            "falling back to the active session"
        )
        return None

    session_identifier = raw_session.strip()
    matched = sessions_by_identifier.get(session_identifier)
    if matched is not None:
        return matched

    name_matches = sessions_by_name.get(session_identifier, [])
    if len(name_matches) == 1:
        return name_matches[0]
    if len(name_matches) > 1:
        result.warnings.append(
            f"bill payload session={raw_session!r} exactly matches {len(name_matches)} local "
            "session names; skipped rather than guessing"
        )
        return None

    candidates = [
        SessionCandidate(identifier=session.identifier, classification=session.classification)
        for session in sessions_by_identifier.values()
    ]
    match = resolve_session(session_identifier, candidates)
    if match.path == MatchPath.FUZZY and match.candidate is not None:
        return sessions_by_identifier[match.candidate.identifier]

    # A local session name is normally the legislature name, but supporting a
    # human session alias here costs little and covers older/manual seed rows.
    named_candidates = [
        SessionCandidate(identifier=name, classification=session.classification)
        for name, rows in sessions_by_name.items()
        for session in rows
    ]
    name_match = resolve_session(session_identifier, named_candidates)
    if name_match.path == MatchPath.FUZZY and name_match.candidate is not None:
        alias_rows = sessions_by_name[name_match.candidate.identifier]
        if len(alias_rows) == 1:
            return alias_rows[0]

    result.warnings.append(
        f"bill payload session={raw_session!r} did not match a unique known session row "
        f"for this jurisdiction ({match.reason}); skipped rather than falling back to "
        f"the active session ({active_session.identifier if active_session else 'none'})"
    )
    return None


def sync_state(
    db: OrmSession,
    jurisdiction: Jurisdiction,
    *,
    client: OpenStatesClient | None = None,
    per_page: int = DEFAULT_PER_PAGE,
    max_pages: int = MAX_PAGES_PER_RUN,
    updated_since_override: str | None = None,
    start_page: int = 1,
    session: str | None = None,
    identifier: str | None = None,
    cycle_started_at: datetime | None = None,
    isolate_snapshot_overflows: bool = False,
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

    `session` and `identifier` optionally narrow the upstream search for a
    bounded repair/replay.  Omitting both preserves the ordinary statewide
    incremental query exactly.

    Direct callers retain fail-closed behavior for evidence snapshot caps by
    default.  The worker wrapper opts into per-bill isolation only when it can
    persist and check the durable blocker that makes the cycle incomplete.

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

    _acquire_api_sync_snapshot_lock(db, jurisdiction)
    client = client or OpenStatesClient()
    result = ApiSyncResult(state=jurisdiction.abbreviation)
    retrieved_at = datetime.now(timezone.utc)
    if cycle_started_at is None:
        cycle_started_at = retrieved_at
    elif cycle_started_at.tzinfo is None:
        raise ValueError("cycle_started_at must be timezone-aware")
    else:
        cycle_started_at = cycle_started_at.astimezone(timezone.utc)

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
    result.updated_since_used = updated_since

    # Exact local identifiers take precedence.  Names are a secondary exact
    # alias (only when unique); compact upstream slugs then get a shared,
    # ambiguity-safe fuzzy match in `_resolve_session_row`.
    session_rows = db.execute(
        select(SessionModel).where(SessionModel.jurisdiction_id == jurisdiction.id)
    ).scalars().all()
    sessions_by_identifier: dict[str, SessionModel] = {s.identifier: s for s in session_rows}
    sessions_by_name: dict[str, list[SessionModel]] = {}
    for session_row in session_rows:
        if session_row.name and session_row.name.strip():
            sessions_by_name.setdefault(session_row.name.strip(), []).append(session_row)
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
        search_kwargs = dict(
            jurisdiction=jurisdiction.abbreviation.lower(),
            updated_since=updated_since,
            include=INCLUDE,
            page=page,
            per_page=per_page,
        )
        if session is not None:
            search_kwargs["session"] = session
        if identifier is not None:
            search_kwargs["identifier"] = identifier
        response = client.search_bills_with_response(**search_kwargs)
        if not isinstance(response, RetainedOpenStatesResponse) or not response.is_retained_response:
            raise ValueError("api-sync requires a retained OpenStates HTTP response")
        payload = response.payload
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
            session_row = _resolve_session_row(
                bill_payload, sessions_by_identifier, sessions_by_name, active_session, result
            )
            if session_row is None:
                # Do this before openstates_id matching too: an existing row
                # is not permission to apply a payload we cannot safely place
                # in a local session.  Otherwise an unknown nonempty session
                # could still overwrite an already-ingested bill.
                result.warnings.append(
                    f"no session row resolved for {jurisdiction.abbreviation} bill "
                    f"{identifier_raw!r} (session={bill_payload.get('session')!r}); skipped"
                )
                continue
            bill = bill_by_openstates_id.get(openstates_id) if openstates_id else None
            if bill is not None and bill.session_id != session_row.id:
                # An Open States id is supposed to identify one bill in one
                # session.  A payload/local-session disagreement therefore
                # proves stale or corrupted identity data; it is never safe
                # to update the row merely because the external id matches.
                # This is the exact shape that previously merged California
                # regular and special-session measures sharing a number.
                result.warnings.append(
                    f"Open States id {openstates_id!r} for {identifier_raw!r} resolves to "
                    f"session {session_row.identifier!r}, but the existing Bill Commons row "
                    f"is in session_id={bill.session_id}; skipped pending identity repair"
                )
                continue
            if bill is None and session_row is not None:
                bill = bill_by_session_and_identifier_norm.get((session_row.id, identifier_norm))

            # Evidence snapshots are allowed to reject one pathological bill
            # at their fixed record cap.  Everything this payload can mutate
            # lives behind this savepoint, including blobs and ledger rows.
            # A bill-local result and deferred cache publication ensure that
            # savepoint rollback cannot leak work into later siblings.
            bill_savepoint = db.begin_nested()
            existing_bill_id = bill.id if bill is not None else None
            source_identity_sha256 = _snapshot_blocker_identity(
                session_id=session_row.id,
                identifier_norm=identifier_norm,
            )
            bill_result = ApiSyncResult(state=jurisdiction.abbreviation)

            def isolate_snapshot_overflow(exc: corpus_update_evidence.SnapshotRecordLimitExceeded) -> None:
                bill_savepoint.rollback()
                _record_snapshot_overflow_blocker(
                    db,
                    jurisdiction=jurisdiction,
                    bill_id=existing_bill_id,
                    source_identity_sha256=source_identity_sha256,
                    component=exc.component,
                    record_cap=exc.cap,
                    seen_at=retrieved_at,
                    cycle_started_at=cycle_started_at,
                    cycle_start_page=start_page,
                    updated_since=updated_since,
                )
                result.snapshot_overflows += 1
                result.warnings.append(
                    "api-sync evidence snapshot overflow isolated "
                    f"(component={exc.component}, cap={exc.cap})"
                )
                db.flush()

            # Take the complete local view before any core or child upsert.
            # The evidence serializer will compare this to a post-upsert view;
            # unchanged payloads therefore retain neither blob nor ledger row.
            try:
                before = corpus_update_evidence.snapshot_bill(db, bill) if bill is not None else None
            except corpus_update_evidence.SnapshotRecordLimitExceeded as exc:
                if not isolate_snapshot_overflows:
                    bill_savepoint.rollback()
                    raise
                isolate_snapshot_overflow(exc)
                continue
            except Exception:
                bill_savepoint.rollback()
                raise
            original_bill_upstream_id = (
                (bill.upstream_id or bill.openstates_id) if bill is not None else openstates_id
            )

            if bill is None:
                bill = Bill(
                    jurisdiction_id=jurisdiction.id,
                    session_id=session_row.id,
                    identifier=identifier_raw,
                    identifier_norm=identifier_norm,
                    title=bill_payload.get("title") or "(untitled)",
                    description=_resolve_abstract(bill_payload),
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
                bill_result.bills_created += 1
                bill_result.touched_bill_ids.add(bill.id)
                events.record_event(db, bill.id, events.CREATED, identifier_raw)
            elif bill.checksum == checksum:
                bill_result.bills_unchanged += 1
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
            else:
                bill.title = bill_payload.get("title") or bill.title
                abstract = _resolve_abstract(bill_payload)
                if abstract is not None:
                    bill.description = abstract
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
                bill_result.bills_updated += 1
                bill_result.touched_bill_ids.add(bill.id)
                events.record_event(db, bill.id, events.METADATA)

            # Called for EVERY bill on the page, including the
            # checksum-unchanged branch above: `_bill_checksum` covers only
            # identifier/title/classification/latest-action, so a bill whose
            # core fields didn't move can still have a new version or
            # document upstream.
            _upsert_versions_and_documents(
                db, bill, bill_payload.get("versions") or [], bill_payload.get("documents") or [], bill_result, retrieved_at
            )
            _upsert_actions(db, bill, bill_payload.get("actions") or [], bill_result, retrieved_at)
            _upsert_sponsorships(db, bill, bill_payload.get("sponsorships") or [], bill_result, retrieved_at)
            db.flush()
            try:
                evidence = corpus_update_evidence.record_update_evidence(
                    db,
                    bill=bill,
                    original_bill_upstream_id=original_bill_upstream_id,
                    response=response,
                    before=before,
                    after=corpus_update_evidence.snapshot_bill(db, bill),
                    retrieved_at=retrieved_at,
                )
            except corpus_update_evidence.SnapshotRecordLimitExceeded as exc:
                if not isolate_snapshot_overflows:
                    bill_savepoint.rollback()
                    raise
                isolate_snapshot_overflow(exc)
                continue
            except Exception:
                bill_savepoint.rollback()
                raise
            if evidence is not None:
                bill_result.corpus_evidence_by_bill[bill.id] = evidence.id
            _resolve_snapshot_overflow_blocker(
                db,
                jurisdiction=jurisdiction,
                source_identity_sha256=source_identity_sha256,
                resolved_at=retrieved_at,
            )
            bill_savepoint.commit()
            for name in (
                "bills_created",
                "bills_updated",
                "bills_unchanged",
                "actions",
                "sponsorships",
                "versions",
                "documents",
            ):
                setattr(result, name, getattr(result, name) + getattr(bill_result, name))
            result.touched_bill_ids.update(bill_result.touched_bill_ids)
            result.corpus_evidence_by_bill.update(bill_result.corpus_evidence_by_bill)
            result.warnings.extend(bill_result.warnings)
            if bill.openstates_id:
                bill_by_openstates_id[bill.openstates_id] = bill
            bill_by_session_and_identifier_norm[(session_row.id, identifier_norm)] = bill

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
    # The v3 include has no immutable action id.  Its list position is not a
    # natural key: it can differ from a bulk CSV's `order`, and an inserted
    # historical action shifts every later position.  Reconcile one-to-one by
    # stable content first, then permit a unique-description fallback for an
    # upstream date correction.  Classification deliberately remains outside
    # either key because reclassification is a revision of the same action.
    local_actions = list(
        db.execute(select(BillAction).where(BillAction.bill_id == bill.id)).scalars()
    )
    local_by_content: dict[tuple[str, date | None], list[BillAction]] = {}
    local_by_description: dict[str, list[BillAction]] = {}
    for action in local_actions:
        description_key = _normalize_action_description(action.description)
        local_by_content.setdefault((description_key, action.action_date), []).append(action)
        local_by_description.setdefault(description_key, []).append(action)
    consumed_action_ids: set[object] = set()

    def _action_token(action: BillAction) -> object:
        # UUID defaults are normally present before flush, but object identity
        # keeps reconciliation one-to-one for a newly constructed row too.
        action_id = getattr(action, "id", None)
        return action_id if action_id is not None else id(action)

    def _unconsumed(candidates: list[BillAction]) -> list[BillAction]:
        return [action for action in candidates if _action_token(action) not in consumed_action_ids]

    added = 0
    revised = 0
    for i, action_payload in enumerate(action_payloads):
        description = action_payload.get("description") or ""
        description_key = _normalize_action_description(description)
        order_raw = action_payload.get("order")
        order = order_raw if isinstance(order_raw, int) and not isinstance(order_raw, bool) else i
        action_date = _parse_date(action_payload.get("date"))
        # A same-day Open States list position is not comparable to the
        # official CA history sequence.  Preserve an official sequence as the
        # only source-backed ordering evidence for that day; this also keeps a
        # later generic API row from demoting the official status elsewhere.
        if any(_is_ca_official_action(action) and action.action_date == action_date for action in local_actions):
            order = None
        raw_classification = action_payload.get("classification")
        if isinstance(raw_classification, str):
            classification = raw_classification.strip() or None
        elif isinstance(raw_classification, list):
            classification = ",".join(
                item.strip() for item in raw_classification if isinstance(item, str) and item.strip()
            ) or None
        else:
            classification = None
        exact_matches = _unconsumed(local_by_content.get((description_key, action_date), []))
        current = exact_matches[0] if len(exact_matches) == 1 else None
        order_is_safe_to_reconcile = len(exact_matches) <= 1
        if current is None and len(exact_matches) > 1:
            # Existing duplicate rows are ambiguous only when this payload
            # would mutate one of them.  If at least one already represents
            # the complete incoming fact, consume it without a write so a
            # nightly sync cannot add a fresh duplicate forever.  Otherwise
            # preserve the conflicting rows and insert one authoritative
            # representation; the explicit cleanup pass can then collapse
            # exact content duplicates deterministically.
            official_matches = [
                action for action in exact_matches if _is_ca_official_action(action)
            ]
            if official_matches:
                # Multiple CA ledger rows can legitimately share normalized
                # text/date. The API has no immutable action ID to choose a
                # copy, so consume one without mutation rather than adding a
                # third fact solely because its classification differs.
                current = official_matches[0]
                order_is_safe_to_reconcile = False
            else:
                same_classification = [
                    action for action in exact_matches if action.classification == classification
                ]
                if same_classification:
                    same_order = [action for action in same_classification if action.order == order]
                    current = same_order[0] if same_order else same_classification[0]
                    order_is_safe_to_reconcile = bool(same_order)
        if current is None:
            description_matches = [
                action
                for action in _unconsumed(local_by_description.get(description_key, []))
                if not _is_ca_official_action(action)
            ]
            # A unique description is enough to identify a date correction;
            # multiple same-description actions are genuinely ambiguous, so
            # preserve them and insert rather than silently revising one.
            if len(description_matches) == 1:
                current = description_matches[0]
        if current is not None:
            consumed_action_ids.add(_action_token(current))
            authoritative_ca_same_day = (
                _is_ca_official_action(current) and current.action_date == action_date
            )
            if authoritative_ca_same_day:
                # Do not let a secondary API refresh alter the official
                # sequence, classification, or retrieval provenance of the
                # exact primary-source fact it matched.
                continue
            # Reconcile the upstream order as well as the two status fields.
            # Same-day actions use `order` as their deterministic tiebreaker;
            # leaving a bulk-era/list-position value stale can report the
            # wrong latest action even when all action facts are present.
            changed_fields = []
            if current.action_date != action_date:
                current.action_date = action_date
                changed_fields.append("date")
            if current.classification != classification:
                current.classification = classification
                changed_fields.append("classification")
            if order_is_safe_to_reconcile and current.order != order:
                current.order = order
                changed_fields.append("order")
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
            local_actions.append(new_action)
            local_by_content.setdefault((description_key, action_date), []).append(new_action)
            local_by_description.setdefault(description_key, []).append(new_action)
            consumed_action_ids.add(_action_token(new_action))
            result.actions += 1
            added += 1

    # Derive from the complete local history, never the arrival order of one
    # API response.  This both mirrors the bulk importer's invariant and
    # repairs a previously-regressed scalar when a bill is next encountered.
    # The CA official ledger is authoritative when it has an action on the
    # latest calendar date.  Preserve a genuinely newer API action (the
    # official snapshot can lag), but do not let an unsupported same-day API
    # row with an arbitrary legacy/list-position ``order`` displace the
    # ledger's final source sequence after the CA sweep.
    latest_date = max((action.action_date for action in local_actions if action.action_date is not None), default=None)
    latest_candidates = (
        [
            action
            for action in local_actions
            if action.action_date == latest_date
            and _is_ca_official_action(action)
        ]
        if latest_date is not None
        else []
    )
    latest = max(
        latest_candidates or local_actions,
        key=lambda action: (action.action_date or date.min, action.order if action.order is not None else 0),
        default=None,
    )
    latest_changed = False
    if latest is not None and latest.action_date is not None:
        latest_changed = (
            bill.latest_action_date != latest.action_date
            or bill.latest_action_text != latest.description
        )
        bill.latest_action_date = latest.action_date
        bill.latest_action_text = latest.description
    if added or revised or latest_changed:
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
                "latest action recalculated" if latest_changed and not (added or revised) else "",
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


def run_api_sync_job(
    db: OrmSession,
    state: str,
    *,
    client: OpenStatesClient | None = None,
    start_page: int = 1,
    updated_since_override: str | None = None,
    cycle_started_at: datetime | None = None,
) -> ApiSyncResult:
    """Entry point for the worker dispatch: resolve the jurisdiction, run
    the sync, and record an `ingestion_runs` row. Caller commits."""
    jurisdiction = db.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation == state.upper())
    ).scalar_one_or_none()
    if jurisdiction is None:
        raise ValueError(f"no jurisdiction row for state {state!r}; run seed-registry first")

    now = datetime.now(timezone.utc)
    if cycle_started_at is not None:
        if cycle_started_at.tzinfo is None:
            raise ValueError("cycle_started_at must be timezone-aware")
        cycle_started_at = cycle_started_at.astimezone(timezone.utc)
        if cycle_started_at > now:
            raise ValueError("cycle_started_at cannot be in the future")
    else:
        cycle_started_at = now

    run = IngestionRun(
        jurisdiction_id=jurisdiction.id,
        source_name=SOURCE_NAME,
        # All chunks share the first chunk's start time.  Only the final chunk
        # becomes successful, making this the conservative next watermark.
        started_at=cycle_started_at,
        status="running",
    )
    db.add(run)
    db.flush()

    try:
        result = sync_state(
            db,
            jurisdiction,
            client=client,
            start_page=start_page,
            updated_since_override=updated_since_override,
            cycle_started_at=cycle_started_at,
            isolate_snapshot_overflows=True,
        )
        result.cycle_started_at = cycle_started_at
        result.snapshot_blockers_remaining = _has_active_snapshot_blockers(db, jurisdiction)
        if result.next_page is None and not result.snapshot_blockers_remaining:
            run.status = "success"
        else:
            # Persist bounded, idempotent page writes, but never let an
            # incomplete offset-pagination scan become the normal watermark.
            # The next normal scan safely overlaps from the last real success.
            run.status = "failed"
            if result.next_page is not None:
                run.error = (
                    f"pagination truncated before upstream completion: next_page={result.next_page}, "
                    f"max_page_seen={result.max_page_seen}"
                )
            else:
                run.error = "snapshot evidence blockers remain unresolved"
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
