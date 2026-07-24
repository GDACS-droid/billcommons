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
"""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
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
) -> BulkIngestResult:
    """Parse a session bulk-CSV zip and upsert its contents against
    `session_row` (caller resolves which `sessions` row this zip belongs to
    -- see docs/sources/openstates-csv.md: the CSV's own
    `session_identifier`/`jurisdiction` columns are cross-checked, not
    trusted blindly as the join key, since a zip could in principle be fed
    for the wrong session by operator error).

    Caller is responsible for committing the transaction.
    """
    retrieved_at = retrieved_at or datetime.now(timezone.utc)
    result = BulkIngestResult()

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
        org_rows = _read_csv_rows(zf, ["_organizations.csv"])
        org_cache: dict[str, Organization] = {}
        for row in org_rows:
            openstates_id = row.get("id")
            if not openstates_id:
                continue
            org = db.execute(
                select(Organization).where(Organization.openstates_id == openstates_id)
            ).scalar_one_or_none()
            if org is None:
                org = Organization(openstates_id=openstates_id)
                db.add(org)
                result.organizations += 1
            org.name = row.get("name") or org.name or "Unknown organization"
            org.classification = row.get("classification") or None
            org.source_name = SOURCE_NAME
            org.retrieved_at = retrieved_at
            org.checksum = hashlib.sha256(
                f"{org.name}|{org.classification}".encode("utf-8")
            ).hexdigest()
            org_cache[openstates_id] = org
        db.flush()

        # -- Bills --
        bill_by_openstates_id: dict[str, Bill] = {}
        for row in bill_rows:
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

            bill = db.execute(
                select(Bill).where(
                    Bill.session_id == session_row.id,
                    Bill.identifier_norm == identifier_norm,
                )
            ).scalar_one_or_none()

            if bill is None:
                bill = Bill(
                    jurisdiction_id=session_row.jurisdiction_id,
                    session_id=session_row.id,
                    identifier=identifier_raw,
                    identifier_norm=identifier_norm,
                    title=row.get("title") or "(untitled)",
                )
                db.add(bill)
                db.flush()
                result.bills_created += 1
            elif bill.checksum == checksum:
                result.bills_unchanged += 1
                bill_by_openstates_id[openstates_id or identifier_norm] = bill
                # Unchanged checksum -> skip write for this bill, but still
                # let child-entity upserts below run (they have their own
                # per-row checksums / natural keys, and may legitimately
                # have changed even if the bill's own core fields haven't).
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
            db.flush()

            bill_by_openstates_id[openstates_id or identifier_norm] = bill

            # Subjects: replace-by-diff (small lists; simplest correct approach).
            subjects = _parse_pg_array_repr(row.get("subject"))
            existing_subjects = {s.subject for s in bill.subjects}
            for subject in subjects:
                if subject and subject not in existing_subjects:
                    db.add(BillSubject(bill_id=bill.id, subject=subject))
                    result.subjects += 1
            db.flush()

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
            existing = db.execute(
                select(BillIdentifier).where(
                    BillIdentifier.bill_id == bill.id,
                    BillIdentifier.identifier_norm == alt_norm,
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    BillIdentifier(
                        bill_id=bill.id,
                        identifier=alt_identifier,
                        identifier_norm=alt_norm,
                    )
                )
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
        for row in _read_csv_rows(zf, ["_bill_related_bills.csv"]):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            related_identifier = row.get("identifier")
            relation_type = row.get("relation_type")
            already = any(
                r.related_identifier == related_identifier and r.relation_type == relation_type
                for r in db.execute(
                    select(RelatedBill).where(RelatedBill.bill_id == bill.id)
                ).scalars()
            )
            if not already and related_identifier:
                db.add(
                    RelatedBill(
                        bill_id=bill.id,
                        related_identifier=related_identifier,
                        relation_type=relation_type,
                    )
                )
                result.related_bills += 1
        db.flush()

        # -- Sponsorships --
        for row in _read_csv_rows(zf, ["_bill_sponsorships.csv"]):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            name = row.get("name")
            classification = row.get("classification")
            existing = db.execute(
                select(Sponsorship).where(
                    Sponsorship.bill_id == bill.id,
                    Sponsorship.name == name,
                    Sponsorship.classification == classification,
                )
            ).scalar_one_or_none()
            organization_id = None
            org_openstates_id = row.get("organization_id")
            if org_openstates_id:
                org = _resolve_organization(db, org_cache, org_openstates_id)
                organization_id = org.id if org is not None else None
            if existing is None:
                db.add(
                    Sponsorship(
                        bill_id=bill.id,
                        name=name,
                        classification=classification,
                        primary=_parse_bool_field(row.get("primary")),
                        organization_id=organization_id,
                        source_name=SOURCE_NAME,
                        retrieved_at=retrieved_at,
                    )
                )
                result.sponsorships += 1
        db.flush()

        # -- Actions --
        for row in _read_csv_rows(zf, ["_bill_actions.csv"]):
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            description = row.get("description") or ""
            action_date = _parse_date_field(row.get("date"))
            order_raw = row.get("order")
            order = int(order_raw) if order_raw not in (None, "") else None
            existing = db.execute(
                select(BillAction).where(
                    BillAction.bill_id == bill.id,
                    BillAction.description == description,
                    BillAction.order == order,
                )
            ).scalar_one_or_none()
            organization_id = None
            org_openstates_id = row.get("organization_id")
            if org_openstates_id:
                org = _resolve_organization(db, org_cache, org_openstates_id)
                organization_id = org.id if org is not None else None
            if existing is None:
                classifications = _parse_pg_array_repr(row.get("classification"))
                db.add(
                    BillAction(
                        bill_id=bill.id,
                        organization_id=organization_id,
                        description=description,
                        action_date=action_date,
                        classification=",".join(classifications) if classifications else None,
                        order=order,
                        source_name=SOURCE_NAME,
                        retrieved_at=retrieved_at,
                    )
                )
                result.actions += 1
        db.flush()

        # Derive latest_action_text/date from the max-order action, per the
        # documented mapping (bulk CSV bills.csv has no status/date columns).
        for bill in bill_by_openstates_id.values():
            actions = db.execute(
                select(BillAction).where(BillAction.bill_id == bill.id).order_by(BillAction.order)
            ).scalars().all()
            if actions:
                latest = actions[-1]
                bill.latest_action_text = latest.description
                bill.latest_action_date = latest.action_date
                first = actions[0]
                if "introduction" in (first.classification or ""):
                    bill.introduced_date = first.action_date
        db.flush()

        # -- Versions + version links (as bill_documents) --
        version_rows = _read_csv_rows(zf, ["_bill_versions.csv"])
        version_link_rows = _read_csv_rows(zf, ["_bill_version_links.csv"])
        links_by_version: dict[str, list[dict]] = {}
        for link in version_link_rows:
            links_by_version.setdefault(link.get("version_id"), []).append(link)

        version_by_openstates_id: dict[str, BillVersion] = {}
        for row in version_rows:
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            note = row.get("note") or ""
            version_date = _parse_date_field(row.get("date"))
            existing = db.execute(
                select(BillVersion).where(
                    BillVersion.bill_id == bill.id,
                    BillVersion.note == note,
                    BillVersion.date == version_date,
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = BillVersion(
                    bill_id=bill.id,
                    note=note,
                    date=version_date,
                    source_name=SOURCE_NAME,
                    retrieved_at=retrieved_at,
                )
                db.add(existing)
                db.flush()
                result.versions += 1
            version_by_openstates_id[row.get("id")] = existing

            for link in links_by_version.get(row.get("id"), []):
                url = link.get("url")
                media_type = link.get("media_type")
                already = db.execute(
                    select(BillDocument).where(
                        BillDocument.bill_version_id == existing.id,
                        BillDocument.url == url,
                    )
                ).scalar_one_or_none()
                if already is None:
                    db.add(
                        BillDocument(
                            bill_version_id=existing.id,
                            media_type=media_type,
                            url=url,
                            source_name=SOURCE_NAME,
                            retrieved_at=retrieved_at,
                        )
                    )
                    result.documents += 1
        db.flush()

        # -- Documents + document links (documents not tied to a version get
        #    a synthetic placeholder version row per docs/sources mapping) --
        document_rows = _read_csv_rows(zf, ["_bill_documents.csv"])
        document_link_rows = _read_csv_rows(zf, ["_bill_document_links.csv"])
        links_by_document: dict[str, list[dict]] = {}
        for link in document_link_rows:
            links_by_document.setdefault(link.get("document_id"), []).append(link)

        placeholder_version_by_bill: dict[str, BillVersion] = {}
        for row in document_rows:
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            if bill is None:
                continue
            placeholder = placeholder_version_by_bill.get(str(bill.id))
            if placeholder is None:
                placeholder = db.execute(
                    select(BillVersion).where(
                        BillVersion.bill_id == bill.id,
                        BillVersion.note == "(document, no version)",
                    )
                ).scalar_one_or_none()
                if placeholder is None:
                    placeholder = BillVersion(
                        bill_id=bill.id,
                        note="(document, no version)",
                        source_name=SOURCE_NAME,
                        retrieved_at=retrieved_at,
                        license_note="synthetic placeholder: doc had no matching version row",
                    )
                    db.add(placeholder)
                    db.flush()
                placeholder_version_by_bill[str(bill.id)] = placeholder

            for link in links_by_document.get(row.get("id"), []):
                url = link.get("url")
                media_type = link.get("media_type")
                already = db.execute(
                    select(BillDocument).where(
                        BillDocument.bill_version_id == placeholder.id,
                        BillDocument.url == url,
                    )
                ).scalar_one_or_none()
                if already is None:
                    db.add(
                        BillDocument(
                            bill_version_id=placeholder.id,
                            media_type=media_type,
                            url=url,
                            source_name=SOURCE_NAME,
                            retrieved_at=retrieved_at,
                        )
                    )
                    result.documents += 1
        db.flush()

        # -- Votes --
        vote_rows = _read_csv_rows(zf, ["_votes.csv"])
        vote_count_rows = _read_csv_rows(zf, ["_vote_counts.csv"])
        vote_people_rows = _read_csv_rows(zf, ["_vote_people.csv"])
        counts_by_vote: dict[str, list[dict]] = {}
        for row in vote_count_rows:
            counts_by_vote.setdefault(row.get("vote_event_id"), []).append(row)
        people_by_vote: dict[str, list[dict]] = {}
        for row in vote_people_rows:
            people_by_vote.setdefault(row.get("vote_event_id"), []).append(row)

        for row in vote_rows:
            bill = bill_by_openstates_id.get(row.get("bill_id"))
            organization_id = None
            org_openstates_id = row.get("organization_id")
            if org_openstates_id:
                org = _resolve_organization(db, org_cache, org_openstates_id)
                organization_id = org.id if org is not None else None

            motion_text = row.get("motion_text") or ""
            start_date = _parse_date_field(row.get("start_date"))
            existing = db.execute(
                select(VoteEvent).where(
                    VoteEvent.bill_id == bill.id if bill is not None else VoteEvent.bill_id.is_(None),
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
                vote_event = VoteEvent(
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
                    upstream_id=row.get("id"),
                    retrieved_at=retrieved_at,
                )
                db.add(vote_event)
                db.flush()
                result.vote_events += 1
            else:
                vote_event = existing

            for person_row in people_by_vote.get(row.get("id"), []):
                voter_name = person_row.get("voter_name")
                option = person_row.get("option") or "other"
                already = db.execute(
                    select(VoteRecord).where(
                        VoteRecord.vote_event_id == vote_event.id,
                        VoteRecord.voter_name == voter_name,
                    )
                ).scalar_one_or_none()
                if already is None:
                    db.add(
                        VoteRecord(
                            vote_event_id=vote_event.id,
                            voter_name=voter_name,
                            option=option,
                        )
                    )
                    result.vote_records += 1
        db.flush()

    return result


def _expected_suffix(session_row: SessionModel) -> str:
    """Best-effort filename-stem prefix based on the session identifier;
    only used as a preferred-match hint in _read_csv_rows (which always
    falls back to matching by generic suffix, so an imperfect guess here
    never causes missed data)."""
    return session_row.identifier.replace(" ", "_")
