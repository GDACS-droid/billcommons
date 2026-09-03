"""California official-source Tier-1 full-text adapter.

California's live bill site (`leginfo.legislature.ca.gov`) publishes
`Disallow: /` in its robots.txt, so `fulltext.py`'s polite per-document
fetch correctly dead-letters every CA `bill_documents` row as
`fulltext_status=robots_disallowed` -- that's expected, ToS-respecting
behavior, not a bug. California separately publishes an OFFICIAL bulk
download of the exact same bill text at
`https://downloads.leginfo.legislature.ca.gov/` (a plain Apache directory
listing, no robots.txt at all -- 404 on `/robots.txt`, which is standard
robots.txt semantics for "no restriction stated"), intended for bulk
consumers. This module is the Tier-1 adapter for that source.

See docs/sources/ca-official-bulk.md for the full recon writeup (file
layout, table schema, join key, licensing).

Design, in one paragraph: `parse_ca_bulk_zip` downloads+parses the annual
`pubinfo_<year>.zip` (no DB session touched during this phase -- it's a
pure function of bytes) into an in-memory map of
`{CA_BILL_ID: [(version_num, bill_version_action_date, plain_text), ...]}`
plus an exact `BILL_VERSION_ID` index, picking the LATEST version's text per
bill (the version with the greatest `(bill_version_action_date, version_num)`).
`apply_ca_bulk_fulltext` then
opens short, batched DB transactions to match that map against our
existing CA `bill_documents` rows via the `bill_id=` query-string param
already present in every CA `source_url`/`bill_documents.url` (populated
by `openstates_bulk.py` from Open States' own CA scrape), and writes
`extracted_text` (+ provenance) onto any row whose current text is missing
or was itself a `robots_disallowed`/other terminal fulltext_status. Exact
`billPdf.xhtml?version=` links receive only that exact version's text;
unversioned bill navigation links receive the latest text. Analyses and other
non-bill-text URLs are never populated.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import Text, cast, column, select, update, values
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import Bill, BillDocument, BillVersion, Jurisdiction
from billcommons_shared.httpc import new_client

SOURCE_NAME = "CA leginfo official bulk"
PARSER_VERSION = "ca_bulk/1"

DOWNLOADS_BASE_URL = "https://downloads.leginfo.legislature.ca.gov"
_OFFICIAL_BULK_HOST = "downloads.leginfo.legislature.ca.gov"
_OFFICIAL_BULK_PATH_RE = re.compile(
    r"^/pubinfo_(?:(?P<year>(?:19|20)\d{2})|daily_(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))\.zip$"
)

# Fetch-status values persisted (see fulltext.py's identical convention) so a
# CA bulk-populated document is distinguishable from a "never attempted"
# document (license_note IS NULL) and from the robots_disallowed dead-letter
# it supersedes.
STATUS_OK = "ok"

# Terminal fulltext_status values (mirrors fulltext.TERMINAL_STATUSES) that
# this adapter is allowed to OVERWRITE -- a document dead-lettered by the
# polite per-URL fetcher (most commonly robots_disallowed for CA) is exactly
# the case this bulk adapter exists to unblock. A document already holding
# real extracted_text (from ANY source) is left untouched unless its
# checksum would actually change (see `apply_ca_bulk_fulltext`).
OVERWRITABLE_TERMINAL_NOTES = frozenset(
    {
        "fulltext_status=robots_disallowed",
        "fulltext_status=fetch_error",
        "fulltext_status=too_many_redirects",
        "fulltext_status=unsupported_type",
        "fulltext_status=scanned_pdf_no_text",
        # A document the polite fetcher gave up on after MAX_FETCH_ATTEMPTS,
        # and one whose last failure was OUR worker's fault, are both exactly
        # what a bulk source is for -- leaving them out would let the retry cap
        # permanently hide CA documents this adapter can fill for free.
        "fulltext_status=permanently_failed",
        "fulltext_status=worker_error",
    }
)


def _mark_status(document: BillDocument, status: str) -> None:
    document.license_note = f"fulltext_status={status}"


# ---------------------------------------------------------------------------
# BILL_VERSION_TBL.dat column layout
# ---------------------------------------------------------------------------
# Confirmed 2026-07-24 from CA's own MySQL loader script
# (pubinfo_load.zip -> bill_version_tbl.sql), which LOAD DATA-s this exact
# tab-delimited, backtick-optionally-enclosed column order. The 15th column
# (`@var1` in CA's own loader) is NOT the XML text itself -- it's the
# filename of a sibling `.lob` file in the same zip holding the actual bill
# text (mirrors BILL_ANALYSIS_TBL's `.lob` convention, confirmed against a
# live pubinfo_Fri.zip sample). CA's own loader does
# `LOAD_FILE(concat('c:\\pubinfo\\', @var1))` to pull that file's bytes in.
_BILL_VERSION_COLUMNS = (
    "bill_version_id",
    "bill_id",
    "version_num",
    "bill_version_action_date",
    "bill_version_action",
    "request_num",
    "subject",
    "vote_required",
    "appropriation",
    "fiscal_committee",
    "local_program",
    "substantive_changes",
    "urgency",
    "taxlevy",
    "bill_xml_lob_filename",
    "active_flg",
    "trans_uid",
    "trans_update",
)


def _split_dat_line(line: str) -> list[str]:
    """Split one BILL_VERSION_TBL.dat line on tabs, stripping CA's optional
    backtick enclosure per field (mirrors the loader's
    `OPTIONALLY ENCLOSED BY '`'`). Never raises on a short/malformed line --
    returns whatever fields are present (caller defensively `.get()`s by
    index)."""
    fields = line.split("\t")
    out = []
    for f in fields:
        f = f.strip("\n\r")
        if f.startswith("`") and f.endswith("`") and len(f) >= 2:
            f = f[1:-1]
        out.append(f)
    return out


@dataclass
class BillVersionRow:
    bill_version_id: str
    ca_bill_id: str
    version_num: int
    action_date: datetime | None
    lob_filename: str | None


def _parse_bill_version_tbl(raw: bytes) -> list[BillVersionRow]:
    """Parse BILL_VERSION_TBL.dat bytes into rows. Malformed/short lines are
    skipped (never fabricated), matching the defensive-parsing convention
    used across this codebase's other bulk adapters."""
    rows: list[BillVersionRow] = []
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = _split_dat_line(line)
        if len(fields) < len(_BILL_VERSION_COLUMNS):
            continue
        record = dict(zip(_BILL_VERSION_COLUMNS, fields))
        bill_version_id = record["bill_version_id"]
        ca_bill_id = record["bill_id"]
        if not bill_version_id or not ca_bill_id:
            continue
        try:
            version_num = int(record["version_num"])
        except ValueError:
            version_num = 0
        action_date = _parse_dat_datetime(record["bill_version_action_date"])
        lob_filename = record["bill_xml_lob_filename"] or None
        if lob_filename and lob_filename.upper() == "NULL":
            lob_filename = None
        rows.append(
            BillVersionRow(
                bill_version_id=bill_version_id,
                ca_bill_id=ca_bill_id,
                version_num=version_num,
                action_date=action_date,
                lob_filename=lob_filename,
            )
        )
    return rows


def _parse_dat_datetime(value: str | None) -> datetime | None:
    if not value or value.upper() == "NULL":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# XML -> plain text (mirrors fulltext.extract_text_from_xml: strip tags,
# preserve line/section structure best-effort)
# ---------------------------------------------------------------------------

_XML_TAG_RE = re.compile(r"<[^>]+>")


def _extract_text_from_bill_xml(raw: bytes) -> str:
    """Strip CA bill-version XML tags to plain text, turning each closing
    tag into a line break so section/paragraph structure survives (same
    approach as fulltext.extract_text_from_xml, duplicated here rather than
    imported to keep this module's only cross-module dependency on the
    schema/shared packages, not on fulltext.py's job-queue-shaped API)."""
    text = raw.decode("utf-8", errors="replace")
    with_breaks = _XML_TAG_RE.sub(
        lambda m: "\n" if m.group(0).startswith("</") else "", text
    )
    normalized = with_breaks.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    out_lines: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                out_lines.append(line)
        else:
            blank_run = 0
            out_lines.append(line)
    return "\n".join(out_lines).strip("\n")


# ---------------------------------------------------------------------------
# Download + parse (no DB session touched)
# ---------------------------------------------------------------------------


@dataclass
class CaBulkTextEntry:
    ca_bill_id: str
    version_num: int
    action_date: datetime | None
    text: str
    checksum: str
    bill_version_id: str = ""


@dataclass
class ParseResult:
    """Latest per-bill and exact per-``BILL_VERSION_ID`` extracted text.

    The latest map serves canonical unversioned bill pages only.  Versioned
    URLs must use the exact map, so an older amendment is never silently
    replaced by enrolled text.
    """

    by_bill_id: dict[str, CaBulkTextEntry] = field(default_factory=dict)
    by_bill_version_id: dict[str, CaBulkTextEntry] = field(default_factory=dict)
    conflicted_bill_version_ids: set[str] = field(default_factory=set)
    zip_source_url: str = ""
    versions_seen: int = 0
    versions_with_text: int = 0
    warnings: list[str] = field(default_factory=list)


def download_pubinfo_zip(url: str, *, client: httpx.Client | None = None) -> bytes:
    """Stream-download a pubinfo zip from the official CA downloads host.
    Politeness: honest User-Agent (via new_client), generous timeout --
    these files are large (annual dump ~1GB) and the host has no
    robots.txt restriction (confirmed: /robots.txt -> 404, i.e. no
    disallow rule at all)."""
    owns_client = client is None
    client = client or new_client(timeout=900.0)
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks = []
            for chunk in response.iter_bytes():
                chunks.append(chunk)
            return b"".join(chunks)
    finally:
        if owns_client:
            client.close()


def parse_ca_bulk_zip(zip_bytes: bytes, *, source_url: str = "") -> ParseResult:
    """Parse a pubinfo_<...>.zip's BILL_VERSION_TBL.dat + referenced `.lob`
    XML files into a per-bill latest-version plain-text map. Pure function
    of bytes -- no DB session, no network. Skips (never fabricates) a bill
    version whose `.lob` file is missing from the zip or unparseable as
    XML/text."""
    result = ParseResult(zip_source_url=source_url)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # Index every member by its basename (the part after the last '/')
        # ONCE, up front -- CA's zips are flat (no directory nesting) so
        # this is just an exact-name index, but built via basename to stay
        # robust to a future nested layout. A per-row `endswith` scan over
        # the full namelist (the original approach) is O(members) per
        # lookup -- with ~16k BILL_VERSION_TBL rows against a ~16k-member
        # zip that's ~256M string comparisons, which is the actual
        # bottleneck this index avoids (confirmed: parsing the real
        # pubinfo_2025.zip never finished in 6+ CPU-minutes with the naive
        # scan; the indexed version completes in seconds).
        by_basename: dict[str, str] = {n.rsplit("/", 1)[-1]: n for n in names}

        dat_name = by_basename.get("BILL_VERSION_TBL.dat")
        if dat_name is None:
            result.warnings.append("no BILL_VERSION_TBL.dat found in zip")
            return result
        with zf.open(dat_name) as f:
            version_rows = _parse_bill_version_tbl(f.read())
        result.versions_seen = len(version_rows)

        for row in version_rows:
            if not row.lob_filename:
                continue
            lob_name = by_basename.get(row.lob_filename)
            if lob_name is None:
                continue
            with zf.open(lob_name) as lob_f:
                raw = lob_f.read()
            text = _extract_text_from_bill_xml(raw)
            if not text.strip():
                continue
            result.versions_with_text += 1

            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            entry = CaBulkTextEntry(
                ca_bill_id=row.ca_bill_id,
                version_num=row.version_num,
                action_date=row.action_date,
                text=text,
                checksum=checksum,
                bill_version_id=row.bill_version_id,
            )
            # Exact version identity is the durable key used by billPdf URLs.
            # Conflicting duplicate source IDs are unsafe: withhold that exact
            # version and any latest map entry that depended on it rather than
            # silently selecting whichever row happened to arrive first.
            exact = result.by_bill_version_id.get(row.bill_version_id)
            if exact is not None and (
                exact.ca_bill_id != entry.ca_bill_id or exact.checksum != entry.checksum
            ):
                result.conflicted_bill_version_ids.add(row.bill_version_id)
                result.by_bill_version_id.pop(row.bill_version_id, None)
                if result.by_bill_id.get(exact.ca_bill_id) == exact:
                    result.by_bill_id.pop(exact.ca_bill_id, None)
                result.warnings.append(
                    "conflicting BILL_VERSION_ID withheld from CA bulk text: "
                    f"{row.bill_version_id}"
                )
                continue
            if row.bill_version_id in result.conflicted_bill_version_ids:
                continue
            result.by_bill_version_id.setdefault(row.bill_version_id, entry)

            existing = result.by_bill_id.get(row.ca_bill_id)
            candidate_key = (row.action_date or datetime.min, row.version_num)
            if existing is not None:
                existing_key = (existing.action_date or datetime.min, existing.version_num)
                if candidate_key <= existing_key:
                    continue
            result.by_bill_id[row.ca_bill_id] = entry
    return result


def _ca_bill_id_from_url(url: str | None) -> str | None:
    """Extract the `bill_id=` query-string param from a CA bill_documents
    URL (e.g. `.../billNavClient.xhtml?bill_id=202520260AB1`). Returns None
    if the URL is missing/unparseable or has no such param -- never
    fabricates a bill id."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
    except ValueError:
        return None
    values = qs.get("bill_id")
    if not values:
        return None
    return values[0]


_CA_LEGINFO_HOST = "leginfo.legislature.ca.gov"
_VERSIONED_BILL_PDF_PATH = "/faces/billPdf.xhtml"
_LATEST_BILL_PATHS = frozenset(
    {
        "/faces/billNavClient.xhtml",
        "/faces/billStatusClient.xhtml",
    }
)


def _document_text_entry(
    url: str | None, parse_result: ParseResult
) -> CaBulkTextEntry | None:
    """Return the only official bulk-text entry eligible for ``url``.

    This is deliberately stricter than the historical ``bill_id`` join:
    bill analyses and arbitrary links may carry the same bill id, but are not
    bill text.  A malformed/unknown version is fail-closed rather than
    falling back to latest text.
    """

    if not url:
        return None
    try:
        parsed = urlparse(url)
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.hostname.lower() != _CA_LEGINFO_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        return None

    bill_ids = query.get("bill_id", [])
    if len(bill_ids) != 1 or not bill_ids[0]:
        return None
    if parsed.path == _VERSIONED_BILL_PDF_PATH:
        versions = query.get("version", [])
        if (
            set(query) != {"bill_id", "version"}
            or len(versions) != 1
            or not versions[0]
        ):
            return None
        entry = parse_result.by_bill_version_id.get(versions[0])
        return entry if entry is not None and entry.ca_bill_id == bill_ids[0] else None
    if parsed.path in _LATEST_BILL_PATHS and set(query) == {"bill_id"}:
        return parse_result.by_bill_id.get(bill_ids[0])
    return None


# ---------------------------------------------------------------------------
# Apply to the live DB
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    matched: int = 0
    populated: int = 0
    provenance_repaired: int = 0
    provenance_repair_candidates: int = 0
    provenance_repair_digest: str = ""
    unchanged_skipped: int = 0
    no_match: int = 0
    ineligible_cleaned: int = 0
    dry_run: bool = False


BATCH_SIZE = 200


@dataclass(frozen=True)
class _ContentUpdate:
    """One content replacement planned from a stable pre-write snapshot."""

    document_id: object
    expected_checksum: str | None
    checksum: str
    text: str
    source_url: str | None
    expected_document_url: str
    expected_source_name: str | None
    expected_parser_version: str | None


@dataclass(frozen=True)
class _ProvenanceRepair:
    """A source URL-only correction for content this adapter already owns."""

    document_id: object
    expected_checksum: str
    expected_source_url: str


@dataclass(frozen=True)
class _IneligibleCleanup:
    """Clear a false CA-bulk attachment from an adapter-owned non-text URL."""

    document_id: object
    expected_checksum: str | None
    expected_source_url: str | None
    expected_parent_source_name: str | None
    expected_parent_source_url: str | None
    expected_parent_upstream_id: str | None
    expected_parent_retrieved_at: datetime | None
    expected_parent_upstream_updated_at: datetime | None
    expected_parent_raw_ref: str | None
    expected_parent_parser_version: str | None
    document_url: str
    parent_source_name: str | None
    parent_source_url: str | None
    parent_upstream_id: str | None
    parent_retrieved_at: datetime | None
    parent_upstream_updated_at: datetime | None
    parent_raw_ref: str | None
    parent_parser_version: str | None


def _normalize_official_bulk_url(source_url: str) -> str:
    """Validate and normalize the one official CA bulk artifact URL shape.

    The bulk artifact is security/provenance input, not a generic download
    override.  Reject credentials, ports, query strings, fragments, malformed
    daily archive names, and non-odd-year annual archives before a DB query or
    download happens.  Returning a reconstructed URL makes equivalent host
    casing deterministic in retained provenance.
    """

    parsed = urlparse(source_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid CA official bulk URL port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != _OFFICIAL_BULK_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CA bulk source must be a canonical official downloads URL")
    match = _OFFICIAL_BULK_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise ValueError("CA bulk source has an unsupported official artifact path")
    year = match.group("year")
    if year is not None and int(year) % 2 == 0:
        raise ValueError("CA annual bulk source must name an odd session year")
    return f"https://{_OFFICIAL_BULK_HOST}{parsed.path}"


def _provenance_repair_digest(repairs: list[_ProvenanceRepair]) -> str:
    """Stable operator-proof digest, independent of database select order."""

    payload = "".join(
        f"{item.document_id}\t{item.expected_checksum}\t{item.expected_source_url}\n"
        for item in sorted(
            repairs,
            key=lambda item: (str(item.document_id), item.expected_checksum, item.expected_source_url),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _chunks(items: list[object], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _execute_content_updates(
    db: OrmSession,
    updates: list[_ContentUpdate],
    *,
    retrieved_at: datetime,
    jurisdiction_abbreviation: str,
) -> None:
    """Apply a bounded batch without loading/expiring ORM document rows.

    The checksum predicate is a fail-closed concurrency guard: a concurrent
    writer changes no content here; it turns this run into a visible error
    instead of silently replacing newer text.
    """

    if not updates:
        return
    payload = values(
        column("document_id", BillDocument.id.type),
        column("expected_checksum", BillDocument.checksum.type),
        column("checksum", BillDocument.checksum.type),
        column("extracted_text", Text),
        column("source_url", BillDocument.source_url.type),
        column("expected_document_url", BillDocument.url.type),
        column("expected_source_name", BillDocument.source_name.type),
        column("expected_parser_version", BillDocument.parser_version.type),
        name="ca_bulk_content_updates",
    ).data(
        [
            (
                item.document_id,
                item.expected_checksum,
                item.checksum,
                item.text,
                item.source_url,
                item.expected_document_url,
                item.expected_source_name,
                item.expected_parser_version,
            )
            for item in updates
        ]
    ).alias("updates")
    result = db.execute(
        update(BillDocument)
        .where(BillDocument.id == payload.c.document_id)
        .where(BillDocument.checksum.is_not_distinct_from(payload.c.expected_checksum))
        .where(BillDocument.url == payload.c.expected_document_url)
        .where(BillDocument.source_name.is_not_distinct_from(payload.c.expected_source_name))
        .where(
            BillDocument.parser_version.is_not_distinct_from(
                payload.c.expected_parser_version
            )
        )
        .where(
            select(1)
            .select_from(BillVersion)
            .join(Bill, Bill.id == BillVersion.bill_id)
            .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
            .where(
                BillVersion.id == BillDocument.bill_version_id,
                Jurisdiction.abbreviation == jurisdiction_abbreviation,
            )
            .exists()
        )
        .values(
            extracted_text=payload.c.extracted_text,
            source_name=SOURCE_NAME,
            source_url=payload.c.source_url,
            checksum=payload.c.checksum,
            parser_version=PARSER_VERSION,
            retrieved_at=retrieved_at,
            license_note=f"fulltext_status={STATUS_OK}",
        )
    )
    if result.rowcount != len(updates):
        raise RuntimeError(
            "CA bulk full-text content update lost a concurrent row "
            f"(expected {len(updates)}, updated {result.rowcount})"
        )


def _execute_provenance_repairs(
    db: OrmSession,
    repairs: list[_ProvenanceRepair],
    *,
    canonical_source_url: str,
    jurisdiction_abbreviation: str,
) -> None:
    """Repair only the URL of checksum-equal rows owned by this adapter.

    This deliberately leaves text, checksum, parser status, classification,
    and timestamps untouched.  The exact rowcount assertion prevents a stale
    plan from modifying a row whose ownership/checksum changed concurrently.
    """

    if not repairs:
        return
    payload = values(
        column("document_id", BillDocument.id.type),
        column("expected_checksum", BillDocument.checksum.type),
        column("expected_source_url", BillDocument.source_url.type),
        name="ca_bulk_provenance_repairs",
    ).data(
        [
            (item.document_id, item.expected_checksum, item.expected_source_url)
            for item in repairs
        ]
    ).alias("repairs")
    result = db.execute(
        update(BillDocument)
        .where(BillDocument.id == payload.c.document_id)
        .where(BillDocument.checksum == payload.c.expected_checksum)
        .where(BillDocument.source_name == SOURCE_NAME)
        .where(BillDocument.parser_version == PARSER_VERSION)
        .where(BillDocument.source_url == payload.c.expected_source_url)
        .where(BillDocument.source_url.like("file://%"))
        .where(
            select(1)
            .select_from(BillVersion)
            .join(Bill, Bill.id == BillVersion.bill_id)
            .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
            .where(
                BillVersion.id == BillDocument.bill_version_id,
                Jurisdiction.abbreviation == jurisdiction_abbreviation,
            )
            .exists()
        )
        .values(source_url=canonical_source_url)
    )
    if result.rowcount != len(repairs):
        raise RuntimeError(
            "CA bulk full-text provenance repair lost a concurrent or non-owned row "
            f"(expected {len(repairs)}, updated {result.rowcount})"
        )


def _execute_ineligible_cleanups(
    db: OrmSession,
    cleanups: list[_IneligibleCleanup],
    *,
    jurisdiction_abbreviation: str,
) -> None:
    """Remove only this adapter's false attachment from non-bill-text URLs.

    The parent version is the upstream identity source.  Restore every
    available parent provenance field; a missing parent source URL falls back
    to the document URL.  Clear adapter-only text/checksum and leave a
    terminal robots status. Every snapshot field is rechecked in the DML so a
    concurrent ingest turns into an explicit rollback.
    """

    if not cleanups:
        return
    payload = values(
        column("document_id", BillDocument.id.type),
        column("expected_checksum", BillDocument.checksum.type),
        column("expected_source_url", BillDocument.source_url.type),
        column("expected_parent_source_name", BillVersion.source_name.type),
        column("expected_parent_source_url", BillVersion.source_url.type),
        column("expected_parent_upstream_id", BillVersion.upstream_id.type),
        column("expected_parent_retrieved_at", BillVersion.retrieved_at.type),
        column("expected_parent_upstream_updated_at", BillVersion.upstream_updated_at.type),
        column("expected_parent_raw_ref", BillVersion.raw_ref.type),
        column("expected_parent_parser_version", BillVersion.parser_version.type),
        column("document_url", BillDocument.url.type),
        column("parent_source_name", BillVersion.source_name.type),
        column("parent_source_url", BillVersion.source_url.type),
        column("parent_upstream_id", BillVersion.upstream_id.type),
        column("parent_retrieved_at", BillVersion.retrieved_at.type),
        column("parent_upstream_updated_at", BillVersion.upstream_updated_at.type),
        column("parent_raw_ref", BillVersion.raw_ref.type),
        column("parent_parser_version", BillVersion.parser_version.type),
        name="ca_bulk_ineligible_cleanups",
    ).data(
        [
            (
                item.document_id,
                item.expected_checksum,
                item.expected_source_url,
                item.expected_parent_source_name,
                item.expected_parent_source_url,
                item.expected_parent_upstream_id,
                item.expected_parent_retrieved_at,
                item.expected_parent_upstream_updated_at,
                item.expected_parent_raw_ref,
                item.expected_parent_parser_version,
                item.document_url,
                item.parent_source_name,
                item.parent_source_url,
                item.parent_upstream_id,
                item.parent_retrieved_at,
                item.parent_upstream_updated_at,
                item.parent_raw_ref,
                item.parent_parser_version,
            )
            for item in cleanups
        ]
    ).alias("cleanups")
    result = db.execute(
        update(BillDocument)
        .where(BillDocument.id == payload.c.document_id)
        .where(BillDocument.checksum.is_not_distinct_from(payload.c.expected_checksum))
        .where(BillDocument.source_url.is_not_distinct_from(payload.c.expected_source_url))
        .where(BillDocument.url == payload.c.document_url)
        .where(BillDocument.source_name == SOURCE_NAME)
        .where(BillDocument.parser_version == PARSER_VERSION)
        .where(
            select(1)
            .select_from(BillVersion)
            .join(Bill, Bill.id == BillVersion.bill_id)
            .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
            .where(
                BillVersion.id == BillDocument.bill_version_id,
                BillVersion.source_name.is_not_distinct_from(
                    payload.c.expected_parent_source_name
                ),
                BillVersion.source_url.is_not_distinct_from(
                    payload.c.expected_parent_source_url
                ),
                BillVersion.upstream_id.is_not_distinct_from(
                    payload.c.expected_parent_upstream_id
                ),
                BillVersion.retrieved_at.is_not_distinct_from(
                    cast(
                        payload.c.expected_parent_retrieved_at,
                        BillVersion.retrieved_at.type,
                    )
                ),
                BillVersion.upstream_updated_at.is_not_distinct_from(
                    cast(
                        payload.c.expected_parent_upstream_updated_at,
                        BillVersion.upstream_updated_at.type,
                    )
                ),
                BillVersion.raw_ref.is_not_distinct_from(payload.c.expected_parent_raw_ref),
                BillVersion.parser_version.is_not_distinct_from(
                    payload.c.expected_parent_parser_version
                ),
                Jurisdiction.abbreviation == jurisdiction_abbreviation,
            )
            .exists()
        )
        .values(
            extracted_text=None,
            checksum=None,
            source_name=payload.c.parent_source_name,
            source_url=payload.c.parent_source_url,
            upstream_id=payload.c.parent_upstream_id,
            retrieved_at=cast(payload.c.parent_retrieved_at, BillDocument.retrieved_at.type),
            upstream_updated_at=cast(
                payload.c.parent_upstream_updated_at,
                BillDocument.upstream_updated_at.type,
            ),
            raw_ref=payload.c.parent_raw_ref,
            parser_version=payload.c.parent_parser_version,
            license_note="fulltext_status=robots_disallowed",
        )
    )
    if result.rowcount != len(cleanups):
        raise RuntimeError(
            "CA bulk full-text ineligible cleanup lost a concurrent or non-owned row "
            f"(expected {len(cleanups)}, updated {result.rowcount})"
        )


def apply_ca_bulk_fulltext(
    db: OrmSession,
    parse_result: ParseResult,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Match eligible official CA bill-text URLs against parsed bulk entries and write
    `extracted_text` (+ provenance) onto any row that currently has no
    text OR whose fulltext_status is one of the overwritable terminal
    statuses (see OVERWRITABLE_TERMINAL_NOTES) -- skipping (idempotent) any
    row whose stored checksum already matches the computed one.

    Commits in batches of BATCH_SIZE (mirrors openstates_bulk.py's
    checkpoint cadence) so a crash partway through loses at most one
    batch, not the whole run -- safe because re-running is checksum-
    idempotent. `dry_run=True` computes and returns match counts without
    writing anything (no db.add/commit calls at all).

    `limit`, if given, caps the number of bill_documents ROWS considered
    (not the number of CA bills in parse_result) -- used for bounded
    live-proof runs and tests.
    """
    return _apply_ca_bulk_fulltext_for_jurisdiction(
        db,
        parse_result,
        jurisdiction_abbreviation="CA",
        limit=limit,
        dry_run=dry_run,
    )


def _apply_ca_bulk_fulltext_for_jurisdiction(
    db: OrmSession,
    parse_result: ParseResult,
    *,
    jurisdiction_abbreviation: str,
    limit: int | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Implementation shared with isolated tests; public calls are CA-only."""

    canonical_source_url = _normalize_official_bulk_url(parse_result.zip_source_url)
    result = ApplyResult(dry_run=dry_run)

    stmt = (
        select(
            BillDocument.id,
            BillDocument.url,
            BillDocument.checksum,
            BillDocument.source_name,
            BillDocument.parser_version,
            BillDocument.source_url,
            BillVersion.source_name.label("version_source_name"),
            BillVersion.source_url.label("version_source_url"),
            BillVersion.upstream_id.label("version_upstream_id"),
            BillVersion.retrieved_at.label("version_retrieved_at"),
            BillVersion.upstream_updated_at.label("version_upstream_updated_at"),
            BillVersion.raw_ref.label("version_raw_ref"),
            BillVersion.parser_version.label("version_parser_version"),
        )
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .join(Bill, Bill.id == BillVersion.bill_id)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .where(
            Jurisdiction.abbreviation == jurisdiction_abbreviation,
            BillDocument.url.is_not(None),
            BillDocument.url != "",
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    documents = db.execute(stmt).mappings().all()
    now = datetime.now(timezone.utc)
    mutations: list[_ContentUpdate | _ProvenanceRepair | _IneligibleCleanup] = []

    for document in documents:
        entry = _document_text_entry(document["url"], parse_result)
        if entry is None:
            if (
                document["source_name"] == SOURCE_NAME
                and document["parser_version"] == PARSER_VERSION
            ):
                result.ineligible_cleaned += 1
                mutations.append(
                    _IneligibleCleanup(
                        document_id=document["id"],
                        expected_checksum=document["checksum"],
                        expected_source_url=document["source_url"],
                        expected_parent_source_name=document["version_source_name"],
                        expected_parent_source_url=document["version_source_url"],
                        expected_parent_upstream_id=document["version_upstream_id"],
                        expected_parent_retrieved_at=document["version_retrieved_at"],
                        expected_parent_upstream_updated_at=document[
                            "version_upstream_updated_at"
                        ],
                        expected_parent_raw_ref=document["version_raw_ref"],
                        expected_parent_parser_version=document["version_parser_version"],
                        document_url=document["url"],
                        parent_source_name=document["version_source_name"],
                        parent_source_url=document["version_source_url"] or document["url"],
                        parent_upstream_id=document["version_upstream_id"],
                        parent_retrieved_at=document["version_retrieved_at"],
                        parent_upstream_updated_at=document["version_upstream_updated_at"],
                        parent_raw_ref=document["version_raw_ref"],
                        parent_parser_version=document["version_parser_version"],
                    )
                )
            else:
                result.no_match += 1
            continue

        result.matched += 1

        checksum_matches = document["checksum"] == entry.checksum
        if checksum_matches:
            if (
                document["source_name"] == SOURCE_NAME
                and document["parser_version"] == PARSER_VERSION
                and isinstance(document["source_url"], str)
                and document["source_url"].startswith("file://")
            ):
                result.provenance_repaired += 1
                mutations.append(
                    _ProvenanceRepair(
                        document_id=document["id"],
                        expected_checksum=entry.checksum,
                        expected_source_url=document["source_url"],
                    )
                )
            else:
                result.unchanged_skipped += 1
            continue

        # A checksum change is the existing, intentional update path even for
        # a document that already contains non-terminal text: the CA official
        # bulk artifact may contain a later amended version.
        result.populated += 1
        mutations.append(
            _ContentUpdate(
                document_id=document["id"],
                expected_checksum=document["checksum"],
                checksum=entry.checksum,
                text=entry.text,
                source_url=canonical_source_url,
                expected_document_url=document["url"],
                expected_source_name=document["source_name"],
                expected_parser_version=document["parser_version"],
            )
        )

    provenance_repairs = [item for item in mutations if isinstance(item, _ProvenanceRepair)]
    result.provenance_repair_candidates = len(provenance_repairs)
    result.provenance_repair_digest = _provenance_repair_digest(provenance_repairs)

    if dry_run:
        return result

    # The plan above is a plain snapshot.  Commit boundaries therefore cannot
    # expire 10k ORM objects and turn a URL-only repair into an N+1 reload.
    for batch in _chunks(mutations, BATCH_SIZE):
        content_updates = [item for item in batch if isinstance(item, _ContentUpdate)]
        provenance_repairs = [item for item in batch if isinstance(item, _ProvenanceRepair)]
        ineligible_cleanups = [item for item in batch if isinstance(item, _IneligibleCleanup)]
        try:
            _execute_content_updates(
                db,
                content_updates,
                retrieved_at=now,
                jurisdiction_abbreviation=jurisdiction_abbreviation,
            )
            if provenance_repairs:
                _execute_provenance_repairs(
                    db,
                    provenance_repairs,
                    canonical_source_url=canonical_source_url,
                    jurisdiction_abbreviation=jurisdiction_abbreviation,
                )
            _execute_ineligible_cleanups(
                db,
                ineligible_cleanups,
                jurisdiction_abbreviation=jurisdiction_abbreviation,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_ca_fulltext(
    *,
    zip_url: str | None = None,
    zip_path: str | Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Download (or read from a local path) the CA pubinfo bulk zip, parse
    it, and apply the resulting full text against the live CA
    bill_documents rows.

    `zip_url` defaults to the current-session annual dump
    (`pubinfo_<current_year>.zip`) if not given. `zip_path` (mostly for
    tests/manual runs against an already-downloaded file) takes precedence
    over `zip_url` if both are given. `dry_run=True` reports match counts
    without writing to the DB (see `apply_ca_bulk_fulltext`).

    Opens its own DB session (this is the orchestrator-facing entry point
    the CLI subcommand will call) -- download+parse happen BEFORE any
    session is opened, per the module's no-session-held-across-network-IO
    design.
    """
    from billcommons_shared.db import get_session

    if zip_path is not None:
        if zip_url is None:
            raise ValueError(
                "--zip-path requires the matching canonical https://downloads.leginfo.legislature.ca.gov "
                "URL via --zip-url; refusing to persist file:// provenance"
            )
        source_url = _normalize_official_bulk_url(zip_url)
        zip_bytes = Path(zip_path).read_bytes()
    else:
        current_year = datetime.now(timezone.utc).year
        # CA publishes one annual zip per ODD year for the 2-year session
        # (e.g. pubinfo_2025.zip covers the 2025-2026 session); an even
        # current year still uses the prior odd year's file.
        session_year = current_year if current_year % 2 == 1 else current_year - 1
        source_url = _normalize_official_bulk_url(
            zip_url or f"{DOWNLOADS_BASE_URL}/pubinfo_{session_year}.zip"
        )
        zip_bytes = download_pubinfo_zip(source_url)

    parse_result = parse_ca_bulk_zip(zip_bytes, source_url=source_url)

    db = get_session()
    try:
        return apply_ca_bulk_fulltext(db, parse_result, limit=limit, dry_run=dry_run)
    finally:
        db.close()


def main() -> None:  # pragma: no cover - thin CLI-less manual entry point
    """Manual entry point (`python -m billcommons_ingest.ca_bulk_fulltext`).
    The orchestrator wires a proper `ca-fulltext` subcommand into cli.py
    separately; this is provided so the module is runnable stand-alone
    during development."""
    result = run_ca_fulltext()
    print(
        f"CA bulk full text: matched={result.matched} populated={result.populated} "
        f"ineligible_cleaned={result.ineligible_cleaned} "
        f"unchanged_skipped={result.unchanged_skipped} no_match={result.no_match}"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
