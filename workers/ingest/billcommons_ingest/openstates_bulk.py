"""Open States bulk session-CSV zip ingestion.

Given a session zip (local path, file-like object, or URL), stream-parses
the CSV files documented in docs/sources/openstates-csv.md and performs
idempotent upserts into the canonical schema, with the raw zip archived to
RawStore and provenance columns populated on every touched entity.

Idempotency contract (per BRIEF-wave2.md / ARCHITECTURE.md):
    - Natural key for bills: (session_id, identifier_norm), with
      `openstates_id` also unique and checked.
    - A bill's `checksum` (sha256 of its normalized field tuple) is compared
      before writing; if unchanged, the row (and its children re-derived
      from the same zip) are left untouched -- "unchanged checksum -> no
      write."
    - Running the same zip twice produces identical row counts (verified in
      tests/test_openstates_bulk.py).

Performance / durability design (fix round 4, 2026-07):
    - Per-row existence/idempotency SELECTs are replaced with in-memory maps
      preloaded with ONE query per table, scoped to the touched bill ids for
      this session/zip. The main loops are then pure dict lookups -- no
      per-row round trips to the DB.
    - The caller-owns-the-transaction contract is preserved (callers still
      decide the outermost transaction boundary), but this function now
      commits in batches at safe checkpoints -- after each CSV-file section,
      and every ~PROGRESS_CHUNK rows within large sections -- so a crash
      mid-zip loses at most one chunk of work, not the whole run. This is
      safe *because* the pipeline is checksum-idempotent by design: a
      restart simply re-derives the same rows and finds them unchanged.
      Parents are always committed before any child section that
      foreign-keys to them.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillIdentifier,
    BillSubject,
    BillVersion,
    Organization,
    RelatedBill,
    Session as SessionModel,
    Sponsorship,
    VoteEvent,
    VoteRecord,
)
from billcommons_shared.normalize import normalize_bill_number
from billcommons_shared.rawstore import RawStore

SOURCE_NAME = "openstates_bulk_csv"

# Rows processed per intra-section commit/progress-log tick for large
# sections (bills, actions, sponsorships, votes). Chosen so Railway logs
# show liveness roughly every few seconds and a crash never loses more than
# this many rows of otherwise-idempotent work.
PROGRESS_CHUNK = 2000


class _BufferedInserter:
    """Buffers plain dict rows for a single ORM model and flushes them as one
    Core `INSERT ... VALUES (...), (...), ...` statement (`db.execute(insert(Model),
    rows)`), instead of `session.add(Model(**row))` + `session.flush()`.

    This distinction matters a lot here: benchmarked against the live (WAN,
    non-colocated) DB, ORM `add()`+`flush()` issues ONE INSERT round trip per
    row even when many objects are pending (SQLAlchemy 2.0's insertmanyvalues
    batching falls back to unbatched per-row RETURNING for this psycopg3/
    Postgres combination -- confirmed via `echo=True`: "insertmanyvalues ...
    (ordered; batch not supported)"), while Core bulk `insert()` genuinely
    batches into a handful of multi-row statements regardless. At ~78ms WAN
    RTT to the DB, that difference is the entire "2 hours vs minutes" gap
    described in the fix-round-4 brief for child tables like vote_records.

    Rows must include an `id` (assign a client-side `uuid.uuid4()` before
    buffering) if anything downstream in the same ingest pass needs to
    reference that id before the buffer is flushed.

    IMPORTANT: this class does NOT auto-flush at any row-count threshold --
    every call site is responsible for calling `.flush()` explicitly at its
    own `i % PROGRESS_CHUNK == 0` checkpoint (matching the surrounding
    section's commit cadence). An earlier version auto-flushed internally at
    PROGRESS_CHUNK rows, which caused a real bug: a child inserter (e.g.
    BillDocument, keyed on a parent BillVersion's client-side id) could hit
    its own internal threshold and flush BEFORE the parent BillVersion
    inserter had flushed the referenced rows, since the two buffers' internal
    counters aren't synchronized -- a foreign-key violation in production.
    Explicit, caller-controlled flush ordering (parents' `.flush()` called
    before children's, at the same checkpoint) avoids that class of bug.
    """

    def __init__(self, db: OrmSession, model) -> None:
        self.db = db
        self.model = model
        self._rows: list[dict] = []

    def add(self, row: dict) -> None:
        self._rows.append(row)

    def flush(self) -> None:
        if not self._rows:
            return
        self.db.execute(insert(self.model), self._rows)
        self._rows = []


@dataclass
class BulkIngestResult:
    """Counts returned to the caller / recorded on an IngestionRun row."""

    bills_created: int = 0
    bills_updated: int = 0
    bills_unchanged: int = 0
    actions: int = 0
    sponsorships: int = 0
    versions: int = 0
    documents: int = 0
    subjects: int = 0
    vote_events: int = 0
    vote_records: int = 0
    related_bills: int = 0
    organizations: int = 0
    raw_ref: str | None = None
    warnings: list[str] = field(default_factory=list)


def _parse_pg_array_repr(value: str | None) -> list[str]:
    """Parse Django's Python-list-repr serialization of Postgres ArrayFields
    (e.g. "['bill']" or "['education', 'appropriations']") defensively.

    Falls back to treating the raw non-empty string as a single-element
    list if it isn't a valid Python literal (never raises, never
    fabricates additional entries).
    """
    if not value:
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return [value]
    if isinstance(parsed, (list, tuple)):
        return [str(x) for x in parsed]
    return [str(parsed)]


def _parse_date_field(value: str | None) -> date | None:
    """Parse Open States' loosely-typed date/datetime strings
    (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS+HH:MM or YYYY / YYYY-MM) into a date.
    Returns None (never fabricates) if the value is missing or unparseable.
    """
    if not value:
        return None
    value = value.strip()
    candidates = [
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d",
        "%Y-%m",
        "%Y",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # Common variant: trailing "+00:00" style offset with a space instead of
    # 'T' but seconds missing.
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _parse_bool_field(value: str | None) -> bool:
    return str(value).strip().lower() in ("true", "1", "t", "yes")


def _read_csv_rows(zf: zipfile.ZipFile, member_names: list[str]) -> list[dict]:
    """Return DictReader rows for the first matching member name found in
    the zip (Open States nests files under `<STATE>/<SESSION>/...`, so we
    match by suffix). Returns [] if no matching member exists -- a missing
    file means "zero rows for that entity," never an error.
    """
    names = zf.namelist()
    for candidate in member_names:
        matches = [n for n in names if n.endswith(candidate)]
        if matches:
            with zf.open(matches[0]) as f:
                text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
                return list(csv.DictReader(text))
    return []


def _bill_checksum(row: dict) -> str:
    """Checksum over the fields we actually persist from the bill CSV row,
    used for the idempotent unchanged-skip check."""
    key_fields = (
        row.get("identifier", ""),
        row.get("title", ""),
        row.get("classification", ""),
        row.get("subject", ""),
        row.get("organization_classification", ""),
    )
    return hashlib.sha256("|".join(key_fields).encode("utf-8")).hexdigest()


def _vote_event_checksum(row: dict) -> str:
    """Checksum over the vote CSV fields we persist, used for the
    idempotent unchanged-skip/update check (mirrors `_bill_checksum`)."""
    key_fields = (
        row.get("motion_text", ""),
        row.get("motion_classification", ""),
        row.get("start_date", ""),
        row.get("result", ""),
        row.get("organization_id", ""),
    )
    return hashlib.sha256("|".join(key_fields).encode("utf-8")).hexdigest()


def _resolve_organization(db: OrmSession, org_cache: dict[str, Organization], openstates_id: str | None):
    if not openstates_id:
        return None
    if openstates_id in org_cache:
        return org_cache[openstates_id]
    org = db.execute(
        select(Organization).where(Organization.openstates_id == openstates_id)
    ).scalar_one_or_none()
    if org is not None:
        org_cache[openstates_id] = org
    return org


def peek_session_slug(zip_source: str | Path | BinaryIO | bytes) -> str | None:
    """Return the `session_identifier` value the zip's own bills.csv rows
    carry (e.g. "2026rs", "2026F", "89R") without doing a full ingest --
    used by the bootstrap CLI to resolve which `sessions` row a zip
    belongs to when `--session` wasn't passed explicitly (see FIX 1 /
    cmd_bootstrap in cli.py). Returns None if the zip has no bill rows or
    no `session_identifier` column (never raises -- caller falls back to
    the jurisdiction's active session, same as before this existed)."""
    if isinstance(zip_source, (str, Path)):
        raw_bytes = Path(zip_source).read_bytes()
    elif isinstance(zip_source, bytes):
        raw_bytes = zip_source
    else:
        raw_bytes = zip_source.read()
        if hasattr(zip_source, "seek"):
            zip_source.seek(0)

    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        bill_rows = _read_csv_rows(zf, ["_bills.csv"])
        if not bill_rows:
            return None
        slug = bill_rows[0].get("session_identifier")
        return slug.strip() if slug else None


def ingest_session_csv_zip(
    db: OrmSession,
    zip_source: str | Path | BinaryIO | bytes,
    *,
    session_row: SessionModel,
    rawstore: RawStore,
    retrieved_at: datetime | None = None,
    progress_prefix: str | None = None,
) -> BulkIngestResult:
    """Parse a session bulk-CSV zip and upsert its contents against
    `session_row` (caller resolves which `sessions` row this zip belongs to
    -- see docs/sources/openstates-csv.md: the CSV's own
    `session_identifier`/`jurisdiction` columns are cross-checked, not
    trusted blindly as the join key, since a zip could in principle be fed
    for the wrong session by operator error).

    Commits in batches at internal checkpoints (see module docstring) --
    caller is still responsible for the final commit/rollback of whatever
    happens before/after this call (e.g. cli.py's IngestionRun bookkeeping),
    but should not assume nothing was committed if this raises partway
    through; that's the intended, idempotency-backed behavior.

    `progress_prefix` (e.g. "DE 153") is prepended to progress log lines;
    defaults to the session's identifier if not given.
    """
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    result = BulkIngestResult()
    progress_prefix = progress_prefix or session_row.identifier

    if isinstance(zip_source, (str, Path)):
        raw_bytes = Path(zip_source).read_bytes()
    elif isinstance(zip_source, bytes):
        raw_bytes = zip_source
    else:
        raw_bytes = zip_source.read()

    result.raw_ref = rawstore.put(
        raw_bytes,
        meta={
            "source_name": SOURCE_NAME,
            "session_id": str(session_row.id),
            "retrieved_at": retrieved_at.isoformat(),
        },
    )
    checksum_of_zip = hashlib.sha256(raw_bytes).hexdigest()

    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        bill_rows = _read_csv_rows(zf, [f"{_expected_suffix(session_row)}_bills.csv", "_bills.csv"])
        if not bill_rows:
            result.warnings.append("no bill rows found in zip (empty session or unexpected layout)")
            return result

        # -- Organizations first (bill actions/votes/sponsorships reference them) --
        # New orgs are buffered + bulk-inserted (see _BufferedInserter); updates
        # to already-existing orgs use plain ORM attribute assignment, which
        # DOES batch fine as UPDATE (benchmarked separately from the INSERT
        # path -- only INSERT-with-RETURNING is the slow case here).
        org_rows = _read_csv_rows(zf, ["_organizations.csv"])
        org_cache: dict[str, Organization] = {}
        wanted_org_ids = {row.get("id") for row in org_rows if row.get("id")}
        if wanted_org_ids:
            for org in db.execute(
                select(Organization).where(Organization.openstates_id.in_(wanted_org_ids))
            ).scalars():
                org_cache[org.openstates_id] = org
        new_org_rows: list[dict] = []
        for row in org_rows:
            openstates_id = row.get("id")
            if not openstates_id:
                continue
            org = org_cache.get(openstates_id)
            name = row.get("name") or (org.name if org else None) or "Unknown organization"
            classification = row.get("classification") or None
            checksum = hashlib.sha256(f"{name}|{classification}".encode("utf-8")).hexdigest()
            if org is None:
                new_org_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "openstates_id": openstates_id,
                        "name": name,
                        "classification": classification,
                        "source_name": SOURCE_NAME,
                        "retrieved_at": retrieved_at,
                        "checksum": checksum,
                    }
                )
                result.organizations += 1
            else:
                org.name = name
                org.classification = classification
                org.source_name = SOURCE_NAME
                org.retrieved_at = retrieved_at
                org.checksum = checksum
        if new_org_rows:
            db.execute(insert(Organization), new_org_rows)
        db.flush()
        db.commit()
        # Reload the cache so newly-inserted orgs are visible as ORM objects
        # to the rest of this function (sponsorships/actions/votes resolve
        # organization_id via org_cache).
        if wanted_org_ids:
            org_cache = {
                org.openstates_id: org
                for org in db.execute(
                    select(Organization).where(Organization.openstates_id.in_(wanted_org_ids))
                ).scalars()
            }

        # -- Bills --
        # Preload every existing bill for this session keyed by
        # identifier_norm -- one query instead of one SELECT per CSV row.
        bill_by_identifier_norm: dict[str, Bill] = {
            b.identifier_norm: b
            for b in db.execute(select(Bill).where(Bill.session_id == session_row.id)).scalars()
        }
        bill_by_openstates_id: dict[str, Bill] = {}
        # Brand-new bills are transient (never `db.add()`-ed) ORM instances
        # with a client-assigned id, held here until the buffered bulk
        # INSERT below -- see _BufferedInserter's docstring for why: ORM
        # add()+flush() does not batch INSERTs on this psycopg3/Postgres
        # combination even with a client-side id (benchmarked), so a plain
        # Core insert() with accumulated dict rows is used instead. The
        # transient objects are still usable exactly like normal Python
        # objects for all the child-entity sections below (bill.subjects,
        # bill.id, bill.source_url, etc. are plain attribute reads/writes
        # until something calls db.add()/flush() on them, which we never do
        # for these -- they're inserted purely via the dict-row buffer).
        new_bill_inserter = _BufferedInserter(db, Bill)

        for i, row in enumerate(bill_rows, start=1):
            openstates_id = row.get("id")
            identifier_raw = (row.get("identifier") or "").strip()
            if not identifier_raw:
                result.warnings.append(f"skipped bill row with no identifier (id={openstates_id!r})")
                continue

            try:
                identifier_norm = normalize_bill_number(identifier_raw)
            except ValueError:
                # Defensive: don't fabricate a normalized form for an
                # unparseable identifier; store as-is and skip norm dedupe.
                identifier_norm = identifier_raw.upper().strip()
                result.warnings.append(
                    f"could not normalize bill identifier {identifier_raw!r}; used raw uppercase"
                )

            checksum = _bill_checksum(row)
            bill = bill_by_identifier_norm.get(identifier_norm)

            if bill is None:
                bill_id = uuid.uuid4()
                classifications = _parse_pg_array_repr(row.get("classification"))
                bill = Bill(
                    id=bill_id,
                    jurisdiction_id=session_row.jurisdiction_id,
                    session_id=session_row.id,
                    identifier=identifier_raw,
                    identifier_norm=identifier_norm,
                    title=row.get("title") or "(untitled)",
                    chamber=row.get("organization_classification") or None,
                    bill_type=",".join(classifications) if classifications else None,
                    openstates_id=openstates_id,
                    source_name=SOURCE_NAME,
                    upstream_id=openstates_id,
                    retrieved_at=retrieved_at,
                    raw_ref=result.raw_ref,
                    checksum=checksum,
                    parser_version="openstates_bulk_csv/1",
                )
                new_bill_inserter.add(
                    {
                        "id": bill_id,
                        "jurisdiction_id": session_row.jurisdiction_id,
                        "session_id": session_row.id,
                        "identifier": identifier_raw,
                        "identifier_norm": identifier_norm,
                        "title": bill.title,
                        "chamber": bill.chamber,
                        "bill_type": bill.bill_type,
                        "openstates_id": openstates_id,
                        "source_name": SOURCE_NAME,
                        "upstream_id": openstates_id,
                        "retrieved_at": retrieved_at,
                        "raw_ref": result.raw_ref,
                        "checksum": checksum,
                        "parser_version": "openstates_bulk_csv/1",
                    }
                )
                bill_by_identifier_norm[identifier_norm] = bill
                bill_by_openstates_id[openstates_id or identifier_norm] = bill
                result.bills_created += 1
                if i % PROGRESS_CHUNK == 0:
                    new_bill_inserter.flush()
                    db.flush()
                    db.commit()
                    print(f"{progress_prefix}: bills {i}/{len(bill_rows)}")
                continue
            elif bill.checksum == checksum:
                result.bills_unchanged += 1
                bill_by_openstates_id[openstates_id or identifier_norm] = bill
                # Unchanged checksum -> skip write for this bill, but still
                # let child-entity upserts below run (they have their own
                # per-row checksums / natural keys, and may legitimately
                # have changed even if the bill's own core fields haven't).
                if i % PROGRESS_CHUNK == 0:
                    print(f"{progress_prefix}: bills {i}/{len(bill_rows)}")
                continue
            else:
                result.bills_updated += 1

            bill.title = row.get("title") or bill.title
            bill.chamber = row.get("organization_classification") or bill.chamber
            classifications = _parse_pg_array_repr(row.get("classification"))
            bill.bill_type = ",".join(classifications) if classifications else bill.bill_type
            bill.openstates_id = openstates_id or bill.openstates_id
            bill.source_name = SOURCE_NAME
            bill.upstream_id = openstates_id
            bill.retrieved_at = retrieved_at
            bill.raw_ref = result.raw_ref
            bill.checksum = checksum
            bill.parser_version = "openstates_bulk_csv/1"

            bill_by_openstates_id[openstates_id or identifier_norm] = bill

            # Subjects: replace-by-diff (small lists; simplest correct
            # approach). Preloaded map below (after this loop) handles the
            # "existing subjects" lookup; new bills have none yet.
            if i % PROGRESS_CHUNK == 0:
                db.flush()
                db.commit()
                print(f"{progress_prefix}: bills {i}/{len(bill_rows)}")

        new_bill_inserter.flush()  # bulk-insert every buffered brand-new bill in one round trip
        db.flush()
        db.commit()
        print(f"{progress_prefix}: bills {len(bill_rows)}/{len(bill_rows)} done "
              f"(created={result.bills_created} updated={result.bills_updated} "
              f"unchanged={result.bills_unchanged})")

        # The brand-new bills above are transient Python `Bill` objects (never
        # `db.add()`-ed -- they were written via the Core bulk-insert buffer,
        # not the ORM unit-of-work). Every section below this point mutates
        # `bill.description` / `bill.source_url` / `bill.latest_action_*` /
        # `bill.introduced_date` on whatever object sits in
        # `bill_by_openstates_id`/`bill_by_identifier_norm` and expects that
        # mutation to be persisted -- so swap those transient placeholders
        # for the real, session-attached rows now (one query for every new
        # bill's id) before any child section runs.
        new_bill_ids = [
            b.id for b in bill_by_identifier_norm.values() if b not in db
        ]
        if new_bill_ids:
            attached_by_id = {
                b.id: b
                for b in db.execute(select(Bill).where(Bill.id.in_(new_bill_ids))).scalars()
            }
            for key, b in list(bill_by_identifier_norm.items()):
                if b.id in attached_by_id:
                    bill_by_identifier_norm[key] = attached_by_id[b.id]
            for key, b in list(bill_by_openstates_id.items()):
                if b.id in attached_by_id:
                    bill_by_openstates_id[key] = attached_by_id[b.id]

        touched_bill_ids = [b.id for b in bill_by_openstates_id.values()]

        # -- Subjects (preloaded existing (bill_id, subject) pairs) --
        existing_subjects: set[tuple] = set()
        if touched_bill_ids:
            existing_subjects = {
                (bs.bill_id, bs.subject)
                for bs in db.execute(
                    select(BillSubject).where(BillSubject.bill_id.in_(touched_bill_ids))
                ).scalars()
            }
        subject_inserter = _BufferedInserter(db, BillSubject)
        for row in bill_rows:
            bill = bill_by_openstates_id.get(row.get("id"))
            if bill is None:
                continue
            subjects = _parse_pg_array_repr(row.get("subject"))
            for subject in subjects:
                key = (bill.id, subject)
                if subject and key not in existing_subjects:
                    subject_inserter.add({"bill_id": bill.id, "subject": subject})
                    existing_subjects.add(key)
                    result.subjects += 1
        subject_inserter.flush()
        db.flush()
        db.commit()

        # -- Abstracts (fallback description) --
        abstract_rows = _read_csv_rows(zf, ["_bill_abstracts.csv"])
        abstracts_by_bill: dict[str, str] = {}
        for row in abstract_rows:
            bill_openstates_id = row.get("bill_id")
            if bill_openstates_id and bill_openstates_id not in abstracts_by_bill:
                abstracts_by_bill[bill_openstates_id] = row.get("abstract") or ""
        for openstates_id, abstract_text in abstracts_by_bill.items():
            bill = bill_by_openstates_id.get(openstates_id)
            if bill is not None and not bill.description and abstract_text:
                bill.description = abstract_text
        db.flush()

        # -- Bill identifiers (alternates) --
        existing_bill_identifiers: set[tuple] = set()
        if touched_bill_ids:
            existing_bill_identifiers = {
                (bi.bill_id, bi.identifier_norm)
                for bi in db.execute(
                    select(BillIdentifier).where(BillIdentifier.bill_id.in_(touched_bill_ids))
                ).scalars()
            }
        bill_identifier_inserter = _BufferedInserter(db, BillIdentifier)
        for row in _read_csv_rows(zf, ["_bill_identifiers.csv"]):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            alt_identifier = row.get("identifier")
            if not alt_identifier:
                continue
            try:
                alt_norm = normalize_bill_number(alt_identifier)
            except ValueError:
                alt_norm = alt_identifier.upper().strip()
            key = (bill.id, alt_norm)
            if key not in existing_bill_identifiers:
                bill_identifier_inserter.add(
                    {"bill_id": bill.id, "identifier": alt_identifier, "identifier_norm": alt_norm}
                )
                existing_bill_identifiers.add(key)
        bill_identifier_inserter.flush()
        db.flush()

        # -- Bill sources (provenance + bill.source_url) --
        for row in _read_csv_rows(zf, ["_bill_sources.csv"]):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            url = row.get("url")
            if url and not bill.source_url:
                bill.source_url = url
        db.flush()

        # -- Related bills --
        existing_related: set[tuple] = set()
        if touched_bill_ids:
            existing_related = {
                (rb.bill_id, rb.related_identifier, rb.relation_type)
                for rb in db.execute(
                    select(RelatedBill).where(RelatedBill.bill_id.in_(touched_bill_ids))
                ).scalars()
            }
        related_bill_inserter = _BufferedInserter(db, RelatedBill)
        for row in _read_csv_rows(zf, ["_bill_related_bills.csv"]):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            related_identifier = row.get("identifier")
            relation_type = row.get("relation_type")
            key = (bill.id, related_identifier, relation_type)
            if related_identifier and key not in existing_related:
                related_bill_inserter.add(
                    {
                        "bill_id": bill.id,
                        "related_identifier": related_identifier,
                        "relation_type": relation_type,
                    }
                )
                existing_related.add(key)
                result.related_bills += 1
        related_bill_inserter.flush()
        db.flush()
        # Companion links arrive as identifier strings ("A 5209") with no FK.
        # Resolve them table-wide, not just for touched bills: a link often
        # lands before its target bill exists, so each run also heals links
        # left dangling by earlier runs. Companion is the only relation type
        # whose target lives in the same session (prior-session/replaces
        # point across sessions), and (session, identifier_norm) is unique.
        db.execute(
            text(
                """
                UPDATE related_bills r
                SET related_bill_id = b2.id
                FROM bills b, bills b2
                WHERE r.bill_id = b.id
                  AND r.related_bill_id IS NULL
                  AND r.relation_type = 'companion'
                  AND b2.jurisdiction_id = b.jurisdiction_id
                  AND b2.session_id = b.session_id
                  AND b2.identifier_norm =
                      upper(regexp_replace(r.related_identifier, '\\s+', ' ', 'g'))
                """
            )
        )
        db.commit()

        # -- Sponsorships --
        existing_sponsorships: set[tuple] = set()
        if touched_bill_ids:
            existing_sponsorships = {
                (s.bill_id, s.name, s.classification)
                for s in db.execute(
                    select(Sponsorship).where(Sponsorship.bill_id.in_(touched_bill_ids))
                ).scalars()
            }
        sponsorship_rows = _read_csv_rows(zf, ["_bill_sponsorships.csv"])
        sponsorship_inserter = _BufferedInserter(db, Sponsorship)
        for i, row in enumerate(sponsorship_rows, start=1):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            name = row.get("name")
            classification = row.get("classification")
            key = (bill.id, name, classification)
            organization_id = None
            org_openstates_id = row.get("organization_id")
            if org_openstates_id:
                org = _resolve_organization(db, org_cache, org_openstates_id)
                organization_id = org.id if org is not None else None
            if key not in existing_sponsorships:
                sponsorship_inserter.add(
                    {
                        "bill_id": bill.id,
                        "name": name,
                        "classification": classification,
                        "primary": _parse_bool_field(row.get("primary")),
                        "organization_id": organization_id,
                        "source_name": SOURCE_NAME,
                        "retrieved_at": retrieved_at,
                    }
                )
                existing_sponsorships.add(key)
                result.sponsorships += 1
            if i % PROGRESS_CHUNK == 0:
                sponsorship_inserter.flush()
                db.flush()
                db.commit()
                print(f"{progress_prefix}: sponsorships {i}/{len(sponsorship_rows)}")
        sponsorship_inserter.flush()
        db.flush()
        db.commit()

        # -- Actions --
        existing_actions: set[tuple] = set()
        if touched_bill_ids:
            existing_actions = {
                (a.bill_id, a.description, a.order)
                for a in db.execute(
                    select(BillAction).where(BillAction.bill_id.in_(touched_bill_ids))
                ).scalars()
            }
        action_rows = _read_csv_rows(zf, ["_bill_actions.csv"])
        action_inserter = _BufferedInserter(db, BillAction)
        for i, row in enumerate(action_rows, start=1):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            description = row.get("description") or ""
            action_date = _parse_date_field(row.get("date"))
            order_raw = row.get("order")
            order = int(order_raw) if order_raw not in (None, "") else None
            key = (bill.id, description, order)
            organization_id = None
            org_openstates_id = row.get("organization_id")
            if org_openstates_id:
                org = _resolve_organization(db, org_cache, org_openstates_id)
                organization_id = org.id if org is not None else None
            if key not in existing_actions:
                classifications = _parse_pg_array_repr(row.get("classification"))
                action_inserter.add(
                    {
                        "bill_id": bill.id,
                        "organization_id": organization_id,
                        "description": description,
                        "action_date": action_date,
                        "classification": ",".join(classifications) if classifications else None,
                        "order": order,
                        "source_name": SOURCE_NAME,
                        "retrieved_at": retrieved_at,
                    }
                )
                existing_actions.add(key)
                result.actions += 1
            if i % PROGRESS_CHUNK == 0:
                action_inserter.flush()
                db.flush()
                db.commit()
                print(f"{progress_prefix}: actions {i}/{len(action_rows)}")
        action_inserter.flush()
        db.flush()
        db.commit()

        # Derive latest_action_text/date from the action with the max
        # action_date (tie-break: highest `order` among that date), and
        # introduced_date from the action with the min action_date among
        # actions classified "introduction" (tie-break: lowest `order`).
        # Bulk CSV bills.csv has no status/date columns, so these are always
        # derived from bill_actions, never trusted from the source order the
        # CSV rows happened to arrive in -- some states' export CSVs do not
        # list actions in chronological order, so sorting by `order` alone
        # (or taking the first/last row as-read) silently picks a stale
        # action instead of the actual latest/earliest one.
        # Preload all actions for touched bills in one query and group in
        # memory instead of one SELECT per bill.
        actions_by_bill: dict = {}
        if touched_bill_ids:
            for action in db.execute(
                select(BillAction).where(BillAction.bill_id.in_(touched_bill_ids))
            ).scalars():
                actions_by_bill.setdefault(action.bill_id, []).append(action)
        for bill in bill_by_openstates_id.values():
            actions = actions_by_bill.get(bill.id)
            if actions:
                latest = max(
                    actions,
                    key=lambda a: (a.action_date or date.min, a.order or 0),
                )
                bill.latest_action_text = latest.description
                bill.latest_action_date = latest.action_date

                introduction_actions = [
                    a for a in actions if "introduction" in (a.classification or "")
                ]
                if introduction_actions:
                    first = min(
                        introduction_actions,
                        key=lambda a: (a.action_date or date.max, a.order or 0),
                    )
                    bill.introduced_date = first.action_date
        db.flush()
        db.commit()

        # -- Versions + version links (as bill_documents) --
        version_rows = _read_csv_rows(zf, ["_bill_versions.csv"])
        version_link_rows = _read_csv_rows(zf, ["_bill_version_links.csv"])
        links_by_version: dict[str, list[dict]] = {}
        for link in version_link_rows:
            links_by_version.setdefault(link.get("version_id"), []).append(link)

        existing_versions: dict[tuple, BillVersion] = {}
        if touched_bill_ids:
            for v in db.execute(
                select(BillVersion).where(BillVersion.bill_id.in_(touched_bill_ids))
            ).scalars():
                existing_versions[(v.bill_id, v.note, v.date)] = v

        existing_documents: set[tuple] = set()
        touched_version_ids_placeholder: list = list({v.id for v in existing_versions.values()})
        if touched_version_ids_placeholder:
            existing_documents = {
                (d.bill_version_id, d.url)
                for d in db.execute(
                    select(BillDocument).where(
                        BillDocument.bill_version_id.in_(touched_version_ids_placeholder)
                    )
                ).scalars()
            }

        version_by_openstates_id: dict[str, BillVersion] = {}
        version_inserter = _BufferedInserter(db, BillVersion)
        document_inserter = _BufferedInserter(db, BillDocument)
        for i, row in enumerate(version_rows, start=1):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            note = row.get("note") or ""
            version_date = _parse_date_field(row.get("date"))
            key = (bill.id, note, version_date)
            existing = existing_versions.get(key)
            if existing is None:
                # Client-side id (see the VoteEvent comment above) avoids a
                # flush-per-row here too -- we need existing.id immediately
                # below for the document key/link, but not from the DB. This
                # is a transient object (never db.add()-ed -- see the Bill
                # re-attachment note above); nothing mutates it by attribute
                # after creation, only .id is read, so that's safe here.
                existing = BillVersion(
                    id=uuid.uuid4(),
                    bill_id=bill.id,
                    note=note,
                    date=version_date,
                    source_name=SOURCE_NAME,
                    retrieved_at=retrieved_at,
                )
                version_inserter.add(
                    {
                        "id": existing.id,
                        "bill_id": bill.id,
                        "note": note,
                        "date": version_date,
                        "source_name": SOURCE_NAME,
                        "retrieved_at": retrieved_at,
                    }
                )
                existing_versions[key] = existing
                result.versions += 1
            version_by_openstates_id[row.get("id")] = existing

            for link in links_by_version.get(row.get("id"), []):
                url = link.get("url")
                media_type = link.get("media_type")
                doc_key = (existing.id, url)
                if doc_key not in existing_documents:
                    document_inserter.add(
                        {
                            "bill_version_id": existing.id,
                            "media_type": media_type,
                            "url": url,
                            "source_name": SOURCE_NAME,
                            "retrieved_at": retrieved_at,
                        }
                    )
                    existing_documents.add(doc_key)
                    result.documents += 1

            if i % PROGRESS_CHUNK == 0:
                version_inserter.flush()
                document_inserter.flush()
                db.flush()
                db.commit()
                print(f"{progress_prefix}: versions {i}/{len(version_rows)}")
        version_inserter.flush()
        document_inserter.flush()
        db.flush()
        db.commit()

        # -- Documents + document links (documents not tied to a version get
        #    a synthetic placeholder version row per docs/sources mapping) --
        document_rows = _read_csv_rows(zf, ["_bill_documents.csv"])
        document_link_rows = _read_csv_rows(zf, ["_bill_document_links.csv"])
        links_by_document: dict[str, list[dict]] = {}
        for link in document_link_rows:
            links_by_document.setdefault(link.get("document_id"), []).append(link)

        existing_placeholder_versions: dict = {}
        if touched_bill_ids:
            for v in db.execute(
                select(BillVersion).where(
                    BillVersion.bill_id.in_(touched_bill_ids),
                    BillVersion.note == "(document, no version)",
                )
            ).scalars():
                existing_placeholder_versions[v.bill_id] = v

        # Existing bill_documents on every already-loaded placeholder version,
        # keyed by bill_id -- ONE query for all placeholder versions instead
        # of one SELECT per bill-with-a-placeholder-version (that per-bill
        # version was a real bug: at ~78ms WAN RTT, a session with hundreds
        # of orphan-document bills cost tens of seconds here alone).
        placeholder_version_id_to_bill_id = {v.id: v.bill_id for v in existing_placeholder_versions.values()}
        placeholder_doc_urls_by_bill: dict = {bill_id: set() for bill_id in placeholder_version_id_to_bill_id.values()}
        if placeholder_version_id_to_bill_id:
            for d in db.execute(
                select(BillDocument).where(
                    BillDocument.bill_version_id.in_(placeholder_version_id_to_bill_id.keys())
                )
            ).scalars():
                bill_id = placeholder_version_id_to_bill_id[d.bill_version_id]
                placeholder_doc_urls_by_bill[bill_id].add(d.url)

        placeholder_version_by_bill: dict[str, BillVersion] = {}
        placeholder_inserter = _BufferedInserter(db, BillVersion)
        orphan_document_inserter = _BufferedInserter(db, BillDocument)
        for i, row in enumerate(document_rows, start=1):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            placeholder = placeholder_version_by_bill.get(str(bill.id))
            if placeholder is None:
                placeholder = existing_placeholder_versions.get(bill.id)
                if placeholder is None:
                    # Client-side id -- see the VoteEvent comment above;
                    # avoids a flush-per-bill-with-orphan-documents here.
                    placeholder = BillVersion(
                        id=uuid.uuid4(),
                        bill_id=bill.id,
                        note="(document, no version)",
                        source_name=SOURCE_NAME,
                        retrieved_at=retrieved_at,
                        license_note="synthetic placeholder: doc had no matching version row",
                    )
                    placeholder_inserter.add(
                        {
                            "id": placeholder.id,
                            "bill_id": bill.id,
                            "note": "(document, no version)",
                            "source_name": SOURCE_NAME,
                            "retrieved_at": retrieved_at,
                            "license_note": "synthetic placeholder: doc had no matching version row",
                        }
                    )
                    existing_placeholder_versions[bill.id] = placeholder
                    placeholder_doc_urls_by_bill[bill.id] = set()
                placeholder_version_by_bill[str(bill.id)] = placeholder

            preloaded_urls = placeholder_doc_urls_by_bill.setdefault(bill.id, set())
            for link in links_by_document.get(row.get("id"), []):
                url = link.get("url")
                media_type = link.get("media_type")
                if url not in preloaded_urls:
                    orphan_document_inserter.add(
                        {
                            "bill_version_id": placeholder.id,
                            "media_type": media_type,
                            "url": url,
                            "source_name": SOURCE_NAME,
                            "retrieved_at": retrieved_at,
                        }
                    )
                    preloaded_urls.add(url)
                    result.documents += 1

            if i % PROGRESS_CHUNK == 0:
                placeholder_inserter.flush()
                orphan_document_inserter.flush()
                db.flush()
                db.commit()
                print(f"{progress_prefix}: documents {i}/{len(document_rows)}")
        placeholder_inserter.flush()
        orphan_document_inserter.flush()
        db.flush()
        db.commit()

        # -- Votes --
        # Idempotency key: the Open States bulk CSV's own `id` column (e.g.
        # "ocd-vote/<uuid>") is stable across re-exports of the same vote and
        # is stored as `vote_events.upstream_id` (via ProvenanceMixin) -- so
        # it is the PRIMARY natural key here, exactly like every other
        # entity in this module keys off its own upstream id where one
        # exists. The coarser (bill_id, motion_text, start_date) tuple is
        # only a FALLBACK for the rare row with no `id` (never fabricated),
        # because real session zips legitimately contain multiple distinct
        # votes on the same bill/day with identical generic motion_text
        # (e.g. two chambers' votes both recorded as motion_text="SM" on the
        # same start_date) -- collapsing on that tuple alone silently merges
        # unrelated votes and their vote_records into one row.
        vote_rows = _read_csv_rows(zf, ["_votes.csv"])
        vote_count_rows = _read_csv_rows(zf, ["_vote_counts.csv"])
        vote_people_rows = _read_csv_rows(zf, ["_vote_people.csv"])
        counts_by_vote: dict[str, list[dict]] = {}
        for row in vote_count_rows:
            counts_by_vote.setdefault(row.get("vote_event_id"), []).append(row)
        people_by_vote: dict[str, list[dict]] = {}
        for row in vote_people_rows:
            people_by_vote.setdefault(row.get("vote_event_id"), []).append(row)

        existing_vote_events_by_upstream_id: dict[str, VoteEvent] = {}
        existing_vote_events_by_fallback_key: dict[tuple, VoteEvent] = {}
        if touched_bill_ids:
            for ve in db.execute(
                select(VoteEvent).where(VoteEvent.bill_id.in_(touched_bill_ids))
            ).scalars():
                if ve.upstream_id:
                    existing_vote_events_by_upstream_id[ve.upstream_id] = ve
                else:
                    existing_vote_events_by_fallback_key[
                        (ve.bill_id, ve.motion_text, ve.start_date, ve.organization_id)
                    ] = ve
        # Vote events with no bill_id (bill is None) can't be preloaded by
        # bill_id; fall back to the old per-row lookup for that rare case
        # only (never the common path for a normal session zip).

        # Existing vote_records for every preloaded vote_event, keyed by
        # vote_event_id -- avoids a per-vote_event SELECT for records. The
        # per-event membership set is keyed by (voter_name, option), not
        # voter_name alone -- a voter_name could in principle appear twice
        # on the same vote_event with different recorded options across
        # re-exports/corrections, and (vote_event_id, voter_name, option) is
        # the actual natural key for this child table.
        vote_records_by_event: dict[uuid.UUID, set[tuple]] = {
            ve.id: set()
            for ve in [*existing_vote_events_by_upstream_id.values(), *existing_vote_events_by_fallback_key.values()]
        }
        if vote_records_by_event:
            for vr in db.execute(
                select(VoteRecord).where(VoteRecord.vote_event_id.in_(vote_records_by_event.keys()))
            ).scalars():
                vote_records_by_event[vr.vote_event_id].add((vr.voter_name, vr.option))

        vote_event_inserter = _BufferedInserter(db, VoteEvent)
        vote_record_inserter = _BufferedInserter(db, VoteRecord)
        for i, row in enumerate(vote_rows, start=1):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            organization_id = None
            org_openstates_id = row.get("organization_id")
            if org_openstates_id:
                org = _resolve_organization(db, org_cache, org_openstates_id)
                organization_id = org.id if org is not None else None

            motion_text = row.get("motion_text") or ""
            start_date = _parse_date_field(row.get("start_date"))
            upstream_id = row.get("id") or None
            checksum = _vote_event_checksum(row)

            if upstream_id:
                existing = existing_vote_events_by_upstream_id.get(upstream_id)
                if existing is None and bill is None:
                    # Vote events with no bill_id weren't preloaded above
                    # (that preload is scoped to touched_bill_ids); fall
                    # back to a direct lookup by upstream_id for this rare
                    # case rather than assuming "not in the preloaded map"
                    # means "doesn't exist in the DB at all".
                    existing = db.execute(
                        select(VoteEvent).where(VoteEvent.upstream_id == upstream_id)
                    ).scalar_one_or_none()
            elif bill is not None:
                existing = existing_vote_events_by_fallback_key.get(
                    (bill.id, motion_text, start_date, organization_id)
                )
            else:
                existing = db.execute(
                    select(VoteEvent).where(
                        VoteEvent.bill_id.is_(None),
                        VoteEvent.motion_text == motion_text,
                        VoteEvent.start_date == start_date,
                    )
                ).scalar_one_or_none()

            yes_count = no_count = other_count = 0
            for count_row in counts_by_vote.get(row.get("id"), []):
                option = (count_row.get("option") or "").lower()
                try:
                    value = int(count_row.get("value") or 0)
                except ValueError:
                    value = 0
                if option == "yes":
                    yes_count += value
                elif option == "no":
                    no_count += value
                else:
                    other_count += value

            if existing is None:
                classifications = _parse_pg_array_repr(row.get("motion_classification"))
                # Assign the PK client-side (server_default is a fallback,
                # not DB-generated-always -- see UUIDPkMixin) so we can hand
                # VoteRecord children this id below WITHOUT needing it back
                # from the DB. Buffered via _BufferedInserter (Core bulk
                # insert), not session.add()+flush() -- see that class's
                # docstring: ORM add()+flush() issues one INSERT round trip
                # per row on this psycopg3/Postgres combo even with a
                # client-side id, which was the single biggest WAN-latency
                # cost in this loop (thousands of votes per session).
                vote_event = VoteEvent(
                    id=uuid.uuid4(),
                    bill_id=bill.id if bill is not None else None,
                    organization_id=organization_id,
                    motion_text=motion_text,
                    motion_classification=",".join(classifications) if classifications else None,
                    start_date=start_date,
                    result=row.get("result"),
                    yes_count=yes_count,
                    no_count=no_count,
                    other_count=other_count,
                    source_name=SOURCE_NAME,
                    upstream_id=upstream_id,
                    retrieved_at=retrieved_at,
                    checksum=checksum,
                )
                vote_event_inserter.add(
                    {
                        "id": vote_event.id,
                        "bill_id": vote_event.bill_id,
                        "organization_id": organization_id,
                        "motion_text": motion_text,
                        "motion_classification": vote_event.motion_classification,
                        "start_date": start_date,
                        "result": row.get("result"),
                        "yes_count": yes_count,
                        "no_count": no_count,
                        "other_count": other_count,
                        "source_name": SOURCE_NAME,
                        "upstream_id": upstream_id,
                        "retrieved_at": retrieved_at,
                        "checksum": checksum,
                    }
                )
                result.vote_events += 1
                if upstream_id:
                    existing_vote_events_by_upstream_id[upstream_id] = vote_event
                elif bill is not None:
                    existing_vote_events_by_fallback_key[
                        (bill.id, motion_text, start_date, organization_id)
                    ] = vote_event
                vote_records_by_event[vote_event.id] = set()
            else:
                vote_event = existing
                if vote_event.id not in vote_records_by_event:
                    vote_records_by_event[vote_event.id] = {
                        (vr.voter_name, vr.option)
                        for vr in db.execute(
                            select(VoteRecord).where(VoteRecord.vote_event_id == vote_event.id)
                        ).scalars()
                    }
                # Unchanged checksum -> skip write, matching the bill-level
                # short-circuit above; a changed checksum (e.g. corrected
                # result/counts on a re-export) updates the existing row in
                # place instead of leaving stale data or inserting a dupe.
                if vote_event.checksum != checksum:
                    vote_event.organization_id = organization_id
                    vote_event.motion_text = motion_text
                    classifications = _parse_pg_array_repr(row.get("motion_classification"))
                    vote_event.motion_classification = (
                        ",".join(classifications) if classifications else None
                    )
                    vote_event.start_date = start_date
                    vote_event.result = row.get("result")
                    vote_event.yes_count = yes_count
                    vote_event.no_count = no_count
                    vote_event.other_count = other_count
                    vote_event.source_name = SOURCE_NAME
                    vote_event.upstream_id = upstream_id or vote_event.upstream_id
                    vote_event.retrieved_at = retrieved_at
                    vote_event.checksum = checksum

            preloaded_records = vote_records_by_event[vote_event.id]
            for person_row in people_by_vote.get(row.get("id"), []):
                voter_name = person_row.get("voter_name")
                option = person_row.get("option") or "other"
                record_key = (voter_name, option)
                if record_key not in preloaded_records:
                    vote_record_inserter.add(
                        {
                            "vote_event_id": vote_event.id,
                            "voter_name": voter_name,
                            "option": option,
                        }
                    )
                    preloaded_records.add(record_key)
                    result.vote_records += 1

            if i % PROGRESS_CHUNK == 0:
                vote_event_inserter.flush()
                vote_record_inserter.flush()
                db.flush()
                db.commit()
                print(f"{progress_prefix}: votes {i}/{len(vote_rows)}")
        vote_event_inserter.flush()
        vote_record_inserter.flush()
        db.flush()
        db.commit()

    return result


def _expected_suffix(session_row: SessionModel) -> str:
    """Best-effort filename-stem prefix based on the session identifier;
    only used as a preferred-match hint in _read_csv_rows (which always
    falls back to matching by generic suffix, so an imperfect guess here
    never causes missed data)."""
    return session_row.identifier.replace(" ", "_")
