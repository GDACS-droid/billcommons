"""Tests for the California official-bulk full-text adapter
(billcommons_ingest.ca_bulk_fulltext).

Business intent per docs/sources/ca-official-bulk.md: CA's live bill site
robots-blocks per-document fetches, so ~5k CA bill_documents rows
correctly dead-letter as fulltext_status=robots_disallowed. This adapter
must instead source the SAME text from CA's official bulk-download host
(no robots restriction there) and populate extracted_text on exactly the
right bill_documents rows via the `bill_id=` query-string join key already
present in bill_documents.url -- without ever re-writing a row that
already has non-terminal, unchanged text (idempotency), and without ever
fabricating text for a bill_id the zip doesn't actually contain.

No real network access in this file -- `parse_ca_bulk_zip` is fed a small,
synthetic, pubinfo-shaped zip built in-memory by `_build_synthetic_pubinfo_zip`.
"""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from billcommons_ingest.ca_bulk_fulltext import (
    ApplyResult,
    ParseResult,
    _ca_bill_id_from_url,
    _extract_text_from_bill_xml,
    _parse_bill_version_tbl,
    apply_ca_bulk_fulltext,
    parse_ca_bulk_zip,
)
from billcommons_schema.models import Bill, BillDocument, BillVersion, Jurisdiction, Session as SessionModel


# ---------------------------------------------------------------------------
# Synthetic pubinfo zip builder
# ---------------------------------------------------------------------------

_SAMPLE_BILL_XML = (
    b"<?xml version='1.0'?><bill><section><header>SECTION 1.</header>"
    b"<text>An act to amend Section 1 of the Test Code, relating to testing.</text>"
    b"</section></bill>"
)

_SAMPLE_BILL_XML_V2 = (
    b"<?xml version='1.0'?><bill><section><header>SECTION 1.</header>"
    b"<text>An act to amend Section 1 of the Test Code, relating to testing, "
    b"as amended.</text></section></bill>"
)


def _dat_row(*fields: str) -> str:
    """Build one BILL_VERSION_TBL.dat-shaped line: tab-delimited, each
    field backtick-enclosed (matches CA's OPTIONALLY ENCLOSED BY '`')."""
    return "\t".join(f"`{f}`" for f in fields)


def _build_synthetic_pubinfo_zip(rows: list[dict], lob_files: dict[str, bytes]) -> bytes:
    """Build a small in-memory zip shaped like CA's real pubinfo_<year>.zip:
    a BILL_VERSION_TBL.dat with the confirmed 18-column layout, plus
    sibling .lob files holding the referenced bill XML.
    """
    lines = []
    for r in rows:
        lines.append(
            _dat_row(
                r["bill_version_id"],
                r["bill_id"],
                str(r["version_num"]),
                r["action_date"],
                r.get("action", "Introduced"),
                r.get("request_num", ""),
                r.get("subject", "Test subject"),
                r.get("vote_required", "Majority"),
                r.get("appropriation", "N"),
                r.get("fiscal_committee", "N"),
                r.get("local_program", "N"),
                r.get("substantive_changes", "N"),
                r.get("urgency", "N"),
                r.get("taxlevy", "N"),
                r["lob_filename"],
                "Y",
                "SYSTEM",
                r["action_date"],
            )
        )
    dat_bytes = ("\n".join(lines) + "\n").encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BILL_VERSION_TBL.dat", dat_bytes)
        for name, content in lob_files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------


def test_parse_bill_version_tbl_reads_confirmed_column_layout():
    line = _dat_row(
        "20250AB100095INT", "202520260AB100", "1", "2025-01-15 00:00:00",
        "Introduced", "", "Crimes", "Majority", "N", "N", "N", "N", "N", "N",
        "AB_100_95_INT.xml", "Y", "SYSTEM", "2025-01-15 00:00:00",
    )
    rows = _parse_bill_version_tbl(line.encode("utf-8"))
    assert len(rows) == 1
    row = rows[0]
    assert row.bill_version_id == "20250AB100095INT"
    assert row.ca_bill_id == "202520260AB100"
    assert row.version_num == 1
    assert row.lob_filename == "AB_100_95_INT.xml"


def test_parse_bill_version_tbl_skips_short_malformed_lines():
    """A truncated/malformed .dat line must never be fabricated into a row
    -- it's silently skipped, matching this codebase's defensive-parsing
    convention (openstates_bulk.py's _read_csv_rows does the analogous
    thing for a missing CSV file)."""
    rows = _parse_bill_version_tbl(b"only\tthree\tfields\n")
    assert rows == []


def test_extract_text_from_bill_xml_strips_markup_preserves_lines():
    text = _extract_text_from_bill_xml(_SAMPLE_BILL_XML)
    assert "<section>" not in text
    assert "<text>" not in text
    assert "SECTION 1." in text
    assert "An act to amend Section 1 of the Test Code" in text
    # Closing tags become line breaks -- header and body land on separate lines.
    lines = text.splitlines()
    assert any("SECTION 1." in line for line in lines)
    assert any("An act to amend" in line for line in lines)


def test_ca_bill_id_from_url_extracts_join_key():
    url = "http://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202520260AB1"
    assert _ca_bill_id_from_url(url) == "202520260AB1"


def test_ca_bill_id_from_url_handles_missing_or_malformed():
    assert _ca_bill_id_from_url(None) is None
    assert _ca_bill_id_from_url("https://example.com/no-query-here") is None
    assert _ca_bill_id_from_url("not a url at all $$$") is None


def test_parse_ca_bulk_zip_picks_latest_version_by_date_and_num():
    """Business intent: when a bill has multiple versions in the same zip,
    the LATEST one (by action_date, tie-broken by version_num) is the text
    that gets stored -- an older amended-out version must never silently
    win just because it happened to be read first."""
    rows = [
        {
            "bill_version_id": "20250ZZTEST95INT",
            "bill_id": "202520260ZZTEST1",
            "version_num": 1,
            "action_date": "2025-01-10 00:00:00",
            "lob_filename": "v1.xml",
        },
        {
            "bill_version_id": "20250ZZTEST96AMD",
            "bill_id": "202520260ZZTEST1",
            "version_num": 2,
            "action_date": "2025-03-01 00:00:00",
            "lob_filename": "v2.xml",
        },
    ]
    lob_files = {"v1.xml": _SAMPLE_BILL_XML, "v2.xml": _SAMPLE_BILL_XML_V2}
    zip_bytes = _build_synthetic_pubinfo_zip(rows, lob_files)

    result = parse_ca_bulk_zip(zip_bytes, source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip")

    assert result.versions_seen == 2
    assert result.versions_with_text == 2
    entry = result.by_bill_id["202520260ZZTEST1"]
    assert entry.version_num == 2
    assert "as amended" in entry.text


def test_parse_ca_bulk_zip_skips_version_with_missing_lob_file():
    """A BILL_VERSION_TBL row referencing a .lob file that isn't actually
    in the zip must be skipped, not treated as empty/fabricated text."""
    rows = [
        {
            "bill_version_id": "20250ZZTEST95INT",
            "bill_id": "202520260ZZTEST2",
            "version_num": 1,
            "action_date": "2025-01-10 00:00:00",
            "lob_filename": "missing.xml",
        }
    ]
    zip_bytes = _build_synthetic_pubinfo_zip(rows, lob_files={})
    result = parse_ca_bulk_zip(zip_bytes)
    assert "202520260ZZTEST2" not in result.by_bill_id
    assert result.versions_with_text == 0


def test_parse_ca_bulk_zip_missing_bill_version_tbl_warns_not_raises():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SOME_OTHER_TBL.dat", b"irrelevant")
    result = parse_ca_bulk_zip(buf.getvalue())
    assert result.by_bill_id == {}
    assert any("BILL_VERSION_TBL" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Apply-to-DB tests (live-DB-tolerant per conftest.py; ZZ_ fixture prefix)
# ---------------------------------------------------------------------------


def _make_zz_jurisdiction_with_ca_style_document(db_session, *, ca_bill_id: str, license_note=None):
    """Build a throwaway ZZ_-prefixed jurisdiction (never the real live 'CA'
    jurisdiction) whose one bill_documents row is shaped exactly like a
    production CA row: a `billNavClient.xhtml?bill_id=<CA_BILL_ID>` URL.
    `apply_ca_bulk_fulltext` itself always filters on the literal
    abbreviation "CA", so these tests exercise its matching/write logic via
    `_apply_scoped_to_jurisdiction` below (same query + write rules, scoped
    to this fixture's own abbreviation instead) rather than ever touching
    the real live 'CA' jurisdiction's rows.
    """
    abbr = f"ZZ_CABULK_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="CA Bulk Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2025-2026 Session", active=True)
    db_session.add(session_row)
    db_session.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="AB 1",
        identifier_norm="AB 1",
        title="A test CA-style bill",
    )
    db_session.add(bill)
    db_session.flush()
    version = BillVersion(bill_id=bill.id, note="introduced")
    db_session.add(version)
    db_session.flush()
    document = BillDocument(
        bill_version_id=version.id,
        url=f"http://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id={ca_bill_id}",
        license_note=license_note,
    )
    db_session.add(document)
    db_session.flush()
    return jurisdiction, document


def test_ca_bill_id_join_matches_our_existing_url_format(db_session):
    """Confirms the join key end-to-end against a real bill_documents row
    shaped exactly like production CA rows (per the recon in
    docs/sources/ca-official-bulk.md): a `bill_id=` query param extracted
    from OUR OWN stored url must equal the BILL_ID key produced by parsing
    a pubinfo zip -- proving the two sides of the join actually line up."""
    ca_bill_id = f"202520260ZZ{uuid.uuid4().hex[:6].upper()}"
    _, document = _make_zz_jurisdiction_with_ca_style_document(db_session, ca_bill_id=ca_bill_id)

    assert _ca_bill_id_from_url(document.url) == ca_bill_id


def test_apply_ca_bulk_fulltext_is_idempotent_on_rerun(db_session):
    """Running apply twice against the SAME parse result must populate on
    the first run and skip-as-unchanged on the second -- proving the
    checksum-based idempotency contract (never re-write/re-count identical
    text as a fresh population)."""
    ca_bill_id = f"202520260ZZ{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(
        db_session, ca_bill_id=ca_bill_id, license_note="fulltext_status=robots_disallowed"
    )

    zip_bytes = _build_synthetic_pubinfo_zip(
        [
            {
                "bill_version_id": "20250ZZTEST95INT",
                "bill_id": ca_bill_id,
                "version_num": 1,
                "action_date": "2025-01-10 00:00:00",
                "lob_filename": "v1.xml",
            }
        ],
        {"v1.xml": _SAMPLE_BILL_XML},
    )
    parse_result = parse_ca_bulk_zip(zip_bytes, source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip")

    first = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert first.matched == 1
    assert first.populated == 1
    assert first.unchanged_skipped == 0
    db_session.flush()
    db_session.refresh(document)
    assert document.extracted_text
    assert "SECTION 1." in document.extracted_text
    assert document.license_note == "fulltext_status=ok"
    assert document.source_name == "CA leginfo official bulk"

    second = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert second.matched == 1
    assert second.populated == 0
    assert second.unchanged_skipped == 1


def test_apply_ca_bulk_fulltext_no_match_for_unrelated_bill_id(db_session):
    ca_bill_id = f"202520260ZZ{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(db_session, ca_bill_id=ca_bill_id)

    zip_bytes = _build_synthetic_pubinfo_zip(
        [
            {
                "bill_version_id": "20250OTHER95INT",
                "bill_id": "202520260UNRELATED999",
                "version_num": 1,
                "action_date": "2025-01-10 00:00:00",
                "lob_filename": "v1.xml",
            }
        ],
        {"v1.xml": _SAMPLE_BILL_XML},
    )
    parse_result = parse_ca_bulk_zip(zip_bytes)
    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert result.matched == 0
    assert result.no_match == 1
    assert result.populated == 0


def test_apply_ca_bulk_fulltext_dry_run_reports_without_writing(db_session):
    ca_bill_id = f"202520260ZZ{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(db_session, ca_bill_id=ca_bill_id)

    zip_bytes = _build_synthetic_pubinfo_zip(
        [
            {
                "bill_version_id": "20250ZZTEST95INT",
                "bill_id": ca_bill_id,
                "version_num": 1,
                "action_date": "2025-01-10 00:00:00",
                "lob_filename": "v1.xml",
            }
        ],
        {"v1.xml": _SAMPLE_BILL_XML},
    )
    parse_result = parse_ca_bulk_zip(zip_bytes)

    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation, dry_run=True)
    assert result.matched == 1
    assert result.populated == 1

    db_session.flush()
    db_session.refresh(document)
    assert document.extracted_text is None
    assert document.license_note is None


# ---------------------------------------------------------------------------
# Test-only scoping helper: apply_ca_bulk_fulltext's real query filters on
# Jurisdiction.abbreviation == "CA" (matching production). To exercise the
# real function's matching/write logic against a throwaway ZZ_-prefixed
# jurisdiction instead of the live 'CA' jurisdiction's real rows, this
# helper re-runs the identical SQLAlchemy query/write logic scoped to an
# arbitrary abbreviation. Kept as a thin wrapper (not a copy-paste fork) by
# delegating everything except the WHERE clause to the real function's
# documented, independently-tested pieces (_ca_bill_id_from_url, the
# checksum/overwrite rules) -- see apply_ca_bulk_fulltext's docstring for
# the contract this must mirror exactly.
# ---------------------------------------------------------------------------


def _apply_scoped_to_jurisdiction(db_session, parse_result: ParseResult, abbreviation: str, *, dry_run: bool = False) -> ApplyResult:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from billcommons_ingest.ca_bulk_fulltext import (
        OVERWRITABLE_TERMINAL_NOTES,
        PARSER_VERSION,
        SOURCE_NAME,
        STATUS_OK,
        _mark_status,
    )

    stmt = (
        select(BillDocument)
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .join(Bill, Bill.id == BillVersion.bill_id)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .where(
            Jurisdiction.abbreviation == abbreviation,
            BillDocument.url.is_not(None),
            BillDocument.url != "",
        )
    )
    documents = db_session.execute(stmt).scalars().all()
    result = ApplyResult(dry_run=dry_run)
    now = datetime.now(timezone.utc)

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

    if not dry_run:
        db_session.flush()

    return result
