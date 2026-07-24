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
`{CA_BILL_ID: [(version_num, bill_version_action_date, plain_text), ...]}`,
picking the LATEST version's text per bill (the version with the greatest
`(bill_version_action_date, version_num)`). `apply_ca_bulk_fulltext` then
opens short, batched DB transactions to match that map against our
existing CA `bill_documents` rows via the `bill_id=` query-string param
already present in every CA `source_url`/`bill_documents.url` (populated
by `openstates_bulk.py` from Open States' own CA scrape), and writes
`extracted_text` (+ provenance) onto any row whose current text is missing
or was itself a `robots_disallowed`/other terminal fulltext_status,
skipping (idempotent) if the computed checksum is unchanged from what's
already stored.
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
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import Bill, BillDocument, BillVersion, Jurisdiction
from billcommons_shared.httpc import new_client

SOURCE_NAME = "CA leginfo official bulk"
PARSER_VERSION = "ca_bulk/1"

DOWNLOADS_BASE_URL = "https://downloads.leginfo.legislature.ca.gov"

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


@dataclass
class ParseResult:
    """One entry per CA BILL_ID, holding the LATEST version's extracted
    text (by (action_date, version_num), never fabricated if both are
    missing -- ties fall back to insertion order)."""

    by_bill_id: dict[str, CaBulkTextEntry] = field(default_factory=dict)
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

            existing = result.by_bill_id.get(row.ca_bill_id)
            candidate_key = (row.action_date or datetime.min, row.version_num)
            if existing is not None:
                existing_key = (existing.action_date or datetime.min, existing.version_num)
                if candidate_key <= existing_key:
                    continue
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            result.by_bill_id[row.ca_bill_id] = CaBulkTextEntry(
                ca_bill_id=row.ca_bill_id,
                version_num=row.version_num,
                action_date=row.action_date,
                text=text,
                checksum=checksum,
            )
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


# ---------------------------------------------------------------------------
# Apply to the live DB
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    matched: int = 0
    populated: int = 0
    unchanged_skipped: int = 0
    no_match: int = 0
    dry_run: bool = False


BATCH_SIZE = 200


def apply_ca_bulk_fulltext(
    db: OrmSession,
    parse_result: ParseResult,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Match `parse_result.by_bill_id` against CA `bill_documents` rows via
    the `bill_id=` query param in `bill_documents.url`, and write
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
    result = ApplyResult(dry_run=dry_run)

    stmt = (
        select(BillDocument)
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .join(Bill, Bill.id == BillVersion.bill_id)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .where(
            Jurisdiction.abbreviation == "CA",
            BillDocument.url.is_not(None),
            BillDocument.url != "",
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    documents = db.execute(stmt).scalars().all()
    now = datetime.now(timezone.utc)
    touched = 0

    for document in documents:
        ca_bill_id = _ca_bill_id_from_url(document.url)
        if not ca_bill_id:
            result.no_match += 1
            continue
        entry = parse_result.by_bill_id.get(ca_bill_id)
        if entry is None:
            result.no_match += 1
            continue

        result.matched += 1

        has_text = bool(document.extracted_text)
        is_overwritable_terminal = document.license_note in OVERWRITABLE_TERMINAL_NOTES
        if has_text and not is_overwritable_terminal:
            # Already has real text from some source and isn't a
            # dead-lettered document this adapter is meant to unblock --
            # only touch it if the checksum would actually change (a
            # legitimate re-run picking up an amended bill version).
            if document.checksum == entry.checksum:
                result.unchanged_skipped += 1
                continue
        elif document.checksum == entry.checksum:
            result.unchanged_skipped += 1
            continue

        if dry_run:
            result.populated += 1
            continue

        document.extracted_text = entry.text
        document.source_name = SOURCE_NAME
        document.source_url = parse_result.zip_source_url or document.source_url
        document.checksum = entry.checksum
        document.parser_version = PARSER_VERSION
        document.retrieved_at = now
        _mark_status(document, STATUS_OK)
        result.populated += 1
        touched += 1

        if touched % BATCH_SIZE == 0:
            db.flush()
            db.commit()

    if not dry_run:
        db.flush()
        db.commit()

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
        zip_bytes = Path(zip_path).read_bytes()
        source_url = zip_url or f"file://{Path(zip_path).resolve()}"
    else:
        current_year = datetime.now(timezone.utc).year
        # CA publishes one annual zip per ODD year for the 2-year session
        # (e.g. pubinfo_2025.zip covers the 2025-2026 session); an even
        # current year still uses the prior odd year's file.
        session_year = current_year if current_year % 2 == 1 else current_year - 1
        url = zip_url or f"{DOWNLOADS_BASE_URL}/pubinfo_{session_year}.zip"
        zip_bytes = download_pubinfo_zip(url)
        source_url = url

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
        f"unchanged_skipped={result.unchanged_skipped} no_match={result.no_match}"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
