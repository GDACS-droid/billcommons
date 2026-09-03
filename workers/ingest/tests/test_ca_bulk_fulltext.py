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
import hashlib
import uuid
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

import billcommons_ingest.ca_bulk_fulltext as ca_bulk_fulltext
from billcommons_ingest.ca_bulk_fulltext import (
    ApplyResult,
    CaBulkTextEntry,
    BATCH_SIZE,
    ParseResult,
    PARSER_VERSION,
    SOURCE_NAME,
    _ca_bill_id_from_url,
    _extract_text_from_bill_xml,
    _normalize_official_bulk_url,
    _parse_bill_version_tbl,
    apply_ca_bulk_fulltext,
    parse_ca_bulk_zip,
    run_ca_fulltext,
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


def test_local_zip_requires_explicit_canonical_official_provenance(tmp_path):
    """A pinned local artifact must never turn stored provenance into file://."""

    with pytest.raises(ValueError, match="requires the matching canonical"):
        run_ca_fulltext(zip_path=tmp_path / "pubinfo_2025.zip")
    with pytest.raises(ValueError):
        run_ca_fulltext(
            zip_path=tmp_path / "pubinfo_2025.zip",
            zip_url="https://example.invalid/pubinfo_2025.zip",
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
        "https://downloads.leginfo.legislature.ca.gov:443/pubinfo_2025.zip",
        "https://user@downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
        "https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip?cache=1",
        "https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip#fragment",
        "https://downloads.leginfo.legislature.ca.gov/pubinfo_2026.zip",
        "https://downloads.leginfo.legislature.ca.gov/pubinfo_daily_Funday.zip",
        "https://downloads.leginfo.legislature.ca.gov/other.zip",
        "file:///var/lib/billcommons/pubinfo_2025.zip",
    ],
)
def test_official_bulk_url_contract_rejects_noncanonical_artifacts(url):
    with pytest.raises(ValueError):
        _normalize_official_bulk_url(url)


@pytest.mark.parametrize("day", ["Wed", "Sat", "Sun"])
def test_official_bulk_url_contract_normalizes_exact_pinned_daily_artifact(day):
    assert _normalize_official_bulk_url(
        f"https://DOWNLOADS.LEGINFO.LEGISLATURE.CA.GOV/pubinfo_daily_{day}.zip"
    ) == f"https://downloads.leginfo.legislature.ca.gov/pubinfo_daily_{day}.zip"


def test_apply_rejects_untrusted_parse_provenance_before_database_access():
    class NoDatabaseAccess:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("validation must run before any database query")

    with pytest.raises(ValueError):
        apply_ca_bulk_fulltext(
            NoDatabaseAccess(),  # type: ignore[arg-type]
            ParseResult(zip_source_url="file:///var/lib/billcommons/pubinfo_2025.zip"),
            dry_run=True,
        )


def test_remote_override_rejects_untrusted_url_before_download(monkeypatch):
    monkeypatch.setattr(
        ca_bulk_fulltext,
        "download_pubinfo_zip",
        lambda _url: (_ for _ in ()).throw(AssertionError("must not download")),
    )
    with pytest.raises(ValueError):
        run_ca_fulltext(zip_url="https://example.invalid/pubinfo_2025.zip", dry_run=True)


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
    assert result.by_bill_version_id["20250ZZTEST95INT"].version_num == 1
    assert result.by_bill_version_id["20250ZZTEST96AMD"].version_num == 2


def test_document_text_entry_requires_exact_versioned_pdf_and_only_allows_canonical_navigation():
    bill_id = "202520260SB957"
    introduced = CaBulkTextEntry(
        ca_bill_id=bill_id,
        version_num=1,
        action_date=None,
        text="introduced text",
        checksum="introduced",
        bill_version_id="20250SB95795INT",
    )
    enrolled = CaBulkTextEntry(
        ca_bill_id=bill_id,
        version_num=8,
        action_date=None,
        text="enrolled text",
        checksum="enrolled",
        bill_version_id="20250SB95795ENR",
    )
    parsed = ParseResult(
        by_bill_id={bill_id: enrolled},
        by_bill_version_id={
            introduced.bill_version_id: introduced,
            enrolled.bill_version_id: enrolled,
        },
    )

    assert ca_bulk_fulltext._document_text_entry(
        f"https://leginfo.legislature.ca.gov/faces/billPdf.xhtml?bill_id={bill_id}&version=20250SB95795INT",
        parsed,
    ) == introduced
    assert ca_bulk_fulltext._document_text_entry(
        f"https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id={bill_id}",
        parsed,
    ) == enrolled
    for url in (
        f"https://leginfo.legislature.ca.gov/faces/billAnalysisClient.xhtml?bill_id={bill_id}",
        f"https://leginfo.legislature.ca.gov/faces/billPdf.xhtml?bill_id={bill_id}&version=unknown",
        f"https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id={bill_id}&version=20250SB95795INT",
        f"https://example.invalid/faces/billPdf.xhtml?bill_id={bill_id}&version=20250SB95795INT",
    ):
        assert ca_bulk_fulltext._document_text_entry(url, parsed) is None


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


def test_parse_ca_bulk_zip_withholds_conflicting_duplicate_version_id():
    bill_id = "202520260SB957"
    version_id = "20250SB95795AMD"
    result = parse_ca_bulk_zip(
        _build_synthetic_pubinfo_zip(
            [
                {
                    "bill_version_id": version_id,
                    "bill_id": bill_id,
                    "version_num": 2,
                    "action_date": "2025-02-01 00:00:00",
                    "lob_filename": "first.xml",
                },
                {
                    "bill_version_id": version_id,
                    "bill_id": bill_id,
                    "version_num": 2,
                    "action_date": "2025-02-01 00:00:00",
                    "lob_filename": "conflict.xml",
                },
            ],
            {"first.xml": _SAMPLE_BILL_XML, "conflict.xml": _SAMPLE_BILL_XML_V2},
        )
    )
    assert version_id in result.conflicted_bill_version_ids
    assert version_id not in result.by_bill_version_id
    assert bill_id not in result.by_bill_id
    assert any("conflicting BILL_VERSION_ID" in warning for warning in result.warnings)


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


def _make_versioned_ca_documents(db_session, *, ca_bill_id: str, count: int = 8):
    """SB 957-shaped fixture: eight version PDFs, one latest nav, analyses."""

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
        identifier="SB 957",
        identifier_norm="SB 957",
        title="A versioned CA-style bill",
    )
    db_session.add(bill)
    db_session.flush()

    documents = []
    rows = []
    lobs = {}
    for number in range(1, count + 1):
        version_id = f"20250SB95795V{number:02d}"
        version = BillVersion(
            bill_id=bill.id,
            note=f"version {number}",
            source_name="openstates_bulk_csv",
        )
        db_session.add(version)
        db_session.flush()
        document = BillDocument(
            bill_version_id=version.id,
            url=(
                "https://leginfo.legislature.ca.gov/faces/billPdf.xhtml?"
                f"bill_id={ca_bill_id}&version={version_id}"
            ),
            license_note="fulltext_status=robots_disallowed",
        )
        db_session.add(document)
        documents.append(document)
        rows.append(
            {
                "bill_version_id": version_id,
                "bill_id": ca_bill_id,
                "version_num": number,
                "action_date": f"2025-01-{number:02d} 00:00:00",
                "lob_filename": f"v{number}.xml",
            }
        )
        lobs[f"v{number}.xml"] = (
            f"<bill><text>SB 957 official version {number}</text></bill>".encode()
        )

    nav = BillDocument(
        bill_version_id=documents[-1].bill_version_id,
        url=f"https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id={ca_bill_id}",
        license_note="fulltext_status=robots_disallowed",
    )
    db_session.add(nav)
    documents.append(nav)
    analysis = BillDocument(
        bill_version_id=documents[-2].bill_version_id,
        url=(
            "https://leginfo.legislature.ca.gov/faces/billAnalysisClient.xhtml?"
            f"bill_id={ca_bill_id}"
        ),
        extracted_text="incorrectly attached enrolled text",
        checksum="incorrect-enrolled-checksum",
        source_name=SOURCE_NAME,
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
        parser_version=PARSER_VERSION,
        license_note="fulltext_status=ok",
    )
    db_session.add(analysis)
    db_session.flush()
    return jurisdiction, documents, analysis, parse_ca_bulk_zip(
        _build_synthetic_pubinfo_zip(rows, lobs),
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
    )


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


def test_content_update_fails_closed_when_url_changes_after_planning(db_session, monkeypatch):
    ca_bill_id = f"202520260ZZ{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(
        db_session, ca_bill_id=ca_bill_id, license_note="fulltext_status=robots_disallowed"
    )
    original_update = ca_bulk_fulltext._execute_content_updates

    def change_url(db, updates, *, retrieved_at, jurisdiction_abbreviation):
        db.execute(
            update(BillDocument)
            .where(BillDocument.id == document.id)
            .values(url="https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=other")
        )
        return original_update(
            db,
            updates,
            retrieved_at=retrieved_at,
            jurisdiction_abbreviation=jurisdiction_abbreviation,
        )

    monkeypatch.setattr(ca_bulk_fulltext, "_execute_content_updates", change_url)
    with pytest.raises(RuntimeError, match="expected 1, updated 0"):
        _apply_scoped_to_jurisdiction(
            db_session, _one_entry_parse_result(ca_bill_id), jurisdiction.abbreviation
        )


def test_versioned_pdfs_receive_exact_text_nav_receives_latest_and_analysis_is_cleaned(db_session):
    """Never flatten SB 957-style version history into the enrolled text."""

    ca_bill_id = f"202520260SB{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, documents, analysis, parse_result = _make_versioned_ca_documents(
        db_session, ca_bill_id=ca_bill_id
    )

    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert result.matched == 9  # eight exact PDFs plus one canonical nav URL
    assert result.populated == 9
    assert result.ineligible_cleaned == 1
    db_session.flush()

    for number, document in enumerate(documents[:8], start=1):
        db_session.refresh(document)
        assert document.extracted_text == f"SB 957 official version {number}"
        assert document.checksum == parse_result.by_bill_version_id[
            f"20250SB95795V{number:02d}"
        ].checksum
    db_session.refresh(documents[-1])
    assert documents[-1].extracted_text == "SB 957 official version 8"

    db_session.refresh(analysis)
    assert analysis.extracted_text is None
    assert analysis.checksum is None
    assert analysis.source_name == "openstates_bulk_csv"
    assert analysis.source_url == analysis.url
    assert analysis.parser_version is None
    assert analysis.license_note == "fulltext_status=robots_disallowed"

    second = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert second.populated == 0
    assert second.ineligible_cleaned == 0


@pytest.mark.parametrize(
    "suffix",
    [
        "billPdf.xhtml?bill_id={bill_id}&version=unknown",
        "billNavClient.xhtml?bill_id={bill_id}&version=unexpected",
        "billAnalysisClient.xhtml?bill_id={bill_id}",
    ],
)
def test_adapter_owned_ineligible_urls_are_cleaned_but_foreign_rows_are_preserved(db_session, suffix):
    ca_bill_id = f"202520260SB{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(
        db_session, ca_bill_id=ca_bill_id
    )
    version = db_session.get(BillVersion, document.bill_version_id)
    assert version is not None
    version.source_name = "openstates_api_sync"
    document.url = f"https://leginfo.legislature.ca.gov/faces/{suffix.format(bill_id=ca_bill_id)}"
    document.extracted_text = "incorrect CA bulk text"
    document.checksum = "incorrect"
    document.source_name = SOURCE_NAME
    document.source_url = "https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip"
    document.parser_version = PARSER_VERSION
    document.license_note = "fulltext_status=ok"
    db_session.flush()

    result = _apply_scoped_to_jurisdiction(
        db_session, _one_entry_parse_result(ca_bill_id), jurisdiction.abbreviation
    )
    assert result.ineligible_cleaned == 1
    db_session.refresh(document)
    assert document.extracted_text is None
    assert document.checksum is None
    assert document.source_name == "openstates_api_sync"
    assert document.source_url == document.url
    assert document.parser_version is None

    # A non-adapter source may carry an analysis; this CA bulk repair never
    # mutates it just because it happens to include a CA bill_id parameter.
    document.extracted_text = "foreign analysis"
    document.checksum = "foreign"
    document.source_name = "independent archive"
    document.source_url = "https://archive.example.invalid/analysis"
    document.parser_version = "archive/1"
    db_session.flush()
    foreign = _apply_scoped_to_jurisdiction(
        db_session, _one_entry_parse_result(ca_bill_id), jurisdiction.abbreviation
    )
    assert foreign.ineligible_cleaned == 0
    db_session.refresh(document)
    assert document.extracted_text == "foreign analysis"
    assert document.checksum == "foreign"
    assert document.source_name == "independent archive"


def test_ineligible_cleanup_fails_closed_on_concurrent_ownership_change(db_session, monkeypatch):
    ca_bill_id = f"202520260SB{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(
        db_session, ca_bill_id=ca_bill_id
    )
    version = db_session.get(BillVersion, document.bill_version_id)
    assert version is not None
    version.source_name = "openstates_api_sync"
    document.url = (
        "https://leginfo.legislature.ca.gov/faces/billAnalysisClient.xhtml?"
        f"bill_id={ca_bill_id}"
    )
    document.extracted_text = "incorrect CA bulk text"
    document.checksum = "incorrect"
    document.source_name = SOURCE_NAME
    document.source_url = "https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip"
    document.parser_version = PARSER_VERSION
    db_session.flush()
    original_cleanup = ca_bulk_fulltext._execute_ineligible_cleanups

    def change_ownership(db, cleanups, *, jurisdiction_abbreviation):
        db.execute(
            update(BillDocument)
            .where(BillDocument.id == document.id)
            .values(source_name="concurrent source")
        )
        return original_cleanup(
            db, cleanups, jurisdiction_abbreviation=jurisdiction_abbreviation
        )

    monkeypatch.setattr(ca_bulk_fulltext, "_execute_ineligible_cleanups", change_ownership)
    with pytest.raises(RuntimeError, match="expected 1, updated 0"):
        _apply_scoped_to_jurisdiction(
            db_session, _one_entry_parse_result(ca_bill_id), jurisdiction.abbreviation
        )


def test_ineligible_cleanup_fails_closed_on_parent_provenance_drift(db_session, monkeypatch):
    ca_bill_id = f"202520260SB{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(
        db_session, ca_bill_id=ca_bill_id
    )
    version = db_session.get(BillVersion, document.bill_version_id)
    assert version is not None
    version.source_name = "openstates_api_sync"
    document.url = (
        "https://leginfo.legislature.ca.gov/faces/billAnalysisClient.xhtml?"
        f"bill_id={ca_bill_id}"
    )
    document.extracted_text = "incorrect CA bulk text"
    document.checksum = "incorrect"
    document.source_name = SOURCE_NAME
    document.source_url = "https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip"
    document.parser_version = PARSER_VERSION
    db_session.flush()
    original_cleanup = ca_bulk_fulltext._execute_ineligible_cleanups

    def change_parent(db, cleanups, *, jurisdiction_abbreviation):
        db.execute(
            update(BillVersion)
            .where(BillVersion.id == version.id)
            .values(raw_ref="concurrent-parent-provenance")
        )
        return original_cleanup(
            db, cleanups, jurisdiction_abbreviation=jurisdiction_abbreviation
        )

    monkeypatch.setattr(ca_bulk_fulltext, "_execute_ineligible_cleanups", change_parent)
    with pytest.raises(RuntimeError, match="expected 1, updated 0"):
        _apply_scoped_to_jurisdiction(
            db_session, _one_entry_parse_result(ca_bill_id), jurisdiction.abbreviation
        )


def test_ineligible_cleanup_restores_available_parent_provenance(db_session):
    ca_bill_id = f"202520260SB{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(
        db_session, ca_bill_id=ca_bill_id
    )
    version = db_session.get(BillVersion, document.bill_version_id)
    assert version is not None
    parent_time = datetime(2025, 2, 1, tzinfo=timezone.utc)
    version.source_name = "openstates_bulk_csv"
    version.source_url = "https://openstates.org/ca/sb957"
    version.upstream_id = "version-upstream-id"
    version.retrieved_at = parent_time
    version.upstream_updated_at = parent_time
    version.raw_ref = "parent-raw-ref"
    version.parser_version = "openstates/1"
    document.url = (
        "https://leginfo.legislature.ca.gov/faces/billAnalysisClient.xhtml?"
        f"bill_id={ca_bill_id}"
    )
    document.extracted_text = "incorrect CA bulk text"
    document.checksum = "incorrect"
    document.source_name = SOURCE_NAME
    document.source_url = "https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip"
    document.parser_version = PARSER_VERSION
    document.raw_ref = "bulk-raw-ref"
    db_session.flush()

    result = _apply_scoped_to_jurisdiction(
        db_session, _one_entry_parse_result(ca_bill_id), jurisdiction.abbreviation
    )
    assert result.ineligible_cleaned == 1
    db_session.refresh(document)
    assert document.source_name == version.source_name
    assert document.source_url == version.source_url
    assert document.upstream_id == version.upstream_id
    assert document.retrieved_at == parent_time
    assert document.upstream_updated_at == parent_time
    assert document.raw_ref == version.raw_ref
    assert document.parser_version == version.parser_version


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
    parse_result = parse_ca_bulk_zip(
        zip_bytes,
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
    )
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
    parse_result = parse_ca_bulk_zip(
        zip_bytes,
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
    )

    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation, dry_run=True)
    assert result.matched == 1
    assert result.populated == 1

    db_session.flush()
    db_session.refresh(document)
    assert document.extracted_text is None
    assert document.license_note is None


def _one_entry_parse_result(ca_bill_id: str) -> ParseResult:
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
    return parse_ca_bulk_zip(
        zip_bytes,
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
    )


def _make_owned_checksum_equal_document(db_session):
    ca_bill_id = f"202520260ZZ{uuid.uuid4().hex[:6].upper()}"
    jurisdiction, document = _make_zz_jurisdiction_with_ca_style_document(
        db_session,
        ca_bill_id=ca_bill_id,
        license_note="fulltext_status=ok",
    )
    parse_result = _one_entry_parse_result(ca_bill_id)
    entry = parse_result.by_bill_id[ca_bill_id]
    document.extracted_text = entry.text
    document.checksum = entry.checksum
    document.source_name = SOURCE_NAME
    document.parser_version = PARSER_VERSION
    document.source_url = "file:///var/lib/billcommons/pubinfo_2025.zip"
    db_session.flush()
    return jurisdiction, document, parse_result


def test_checksum_equal_owned_file_provenance_is_repaired_without_rewriting_text(db_session):
    jurisdiction, document, parse_result = _make_owned_checksum_equal_document(db_session)
    original_text = document.extracted_text
    original_status = document.license_note
    original_retrieved_at = document.retrieved_at

    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)

    assert result.populated == 0
    assert result.provenance_repaired == 1
    assert result.provenance_repair_candidates == 1
    assert result.provenance_repair_digest == hashlib.sha256(
        f"{document.id}\t{document.checksum}\tfile:///var/lib/billcommons/pubinfo_2025.zip\n".encode("utf-8")
    ).hexdigest()
    assert result.unchanged_skipped == 0
    db_session.refresh(document)
    assert document.source_url == parse_result.zip_source_url
    assert document.extracted_text == original_text
    assert document.license_note == original_status
    assert document.retrieved_at == original_retrieved_at


def test_checksum_equal_unowned_source_is_not_canonicalized(db_session):
    jurisdiction, document, parse_result = _make_owned_checksum_equal_document(db_session)
    document.source_name = "Third-party archive"
    document.parser_version = "third-party/1"
    document.source_url = "https://archive.example.invalid/ca-bill.xml"
    db_session.flush()

    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)

    assert result.populated == 0
    assert result.provenance_repaired == 0
    assert result.unchanged_skipped == 1
    db_session.refresh(document)
    assert document.source_url == "https://archive.example.invalid/ca-bill.xml"
    assert document.source_name == "Third-party archive"
    assert document.parser_version == "third-party/1"


def test_checksum_equal_adapter_owned_https_provenance_is_preserved(db_session):
    jurisdiction, document, parse_result = _make_owned_checksum_equal_document(db_session)
    document.source_url = "https://downloads.leginfo.legislature.ca.gov/pubinfo_2023.zip"
    db_session.flush()

    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)

    assert result.provenance_repaired == 0
    assert result.provenance_repair_candidates == 0
    assert result.unchanged_skipped == 1
    db_session.refresh(document)
    assert document.source_url == "https://downloads.leginfo.legislature.ca.gov/pubinfo_2023.zip"


def test_provenance_repair_is_idempotent_and_dry_run_does_not_write(db_session):
    jurisdiction, document, parse_result = _make_owned_checksum_equal_document(db_session)

    dry = _apply_scoped_to_jurisdiction(
        db_session, parse_result, jurisdiction.abbreviation, dry_run=True
    )
    assert dry.populated == 0
    assert dry.provenance_repaired == 1
    db_session.refresh(document)
    assert document.source_url.startswith("file://")

    first = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert first.provenance_repaired == 1
    second = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert second.provenance_repaired == 0
    assert second.unchanged_skipped == 1


def test_provenance_repair_batches_at_the_document_checkpoint(db_session, monkeypatch):
    jurisdiction = Jurisdiction(
        name="CA Bulk Batch Test State",
        abbreviation=f"ZZ_CABULK_{uuid.uuid4().hex[:8].upper()}",
        classification="state",
    )
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(
        jurisdiction_id=jurisdiction.id,
        identifier="2025-2026 Session",
        active=True,
    )
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

    entries = {}
    for index in range(BATCH_SIZE + 1):
        ca_bill_id = f"202520260ZZBATCH{index}"
        text = f"official CA text {index}"
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        entries[ca_bill_id] = CaBulkTextEntry(
            ca_bill_id=ca_bill_id,
            version_num=1,
            action_date=None,
            text=text,
            checksum=checksum,
        )
        db_session.add(
            BillDocument(
                bill_version_id=version.id,
                url=f"https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id={ca_bill_id}",
                extracted_text=text,
                checksum=checksum,
                source_name=SOURCE_NAME,
                parser_version=PARSER_VERSION,
                source_url="file:///var/lib/billcommons/pubinfo_2025.zip",
                license_note="fulltext_status=ok",
            )
        )
    db_session.flush()
    parse_result = ParseResult(
        by_bill_id=entries,
        zip_source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2025.zip",
    )

    original_commit = db_session.commit
    commit_count = 0

    def count_commit():
        nonlocal commit_count
        commit_count += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", count_commit)
    result = _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)

    assert result.provenance_repaired == BATCH_SIZE + 1
    assert result.populated == 0
    assert commit_count == 2
    repaired = db_session.execute(
        select(BillDocument.source_url)
        .join(BillVersion)
        .join(Bill)
        .where(Bill.jurisdiction_id == jurisdiction.id)
    ).scalars().all()
    assert repaired == [parse_result.zip_source_url] * (BATCH_SIZE + 1)


def test_provenance_repair_rowcount_mismatch_rolls_back_batch(db_session, monkeypatch):
    jurisdiction, document, parse_result = _make_owned_checksum_equal_document(db_session)
    original_repair = ca_bulk_fulltext._execute_provenance_repairs
    original_rollback = db_session.rollback
    rollback_count = 0

    def race_before_repair(db, repairs, *, canonical_source_url, jurisdiction_abbreviation):
        db.execute(
            update(BillDocument)
            .where(BillDocument.id == document.id)
            .values(source_name="concurrent source")
        )
        return original_repair(
            db,
            repairs,
            canonical_source_url=canonical_source_url,
            jurisdiction_abbreviation=jurisdiction_abbreviation,
        )

    def count_rollback():
        nonlocal rollback_count
        rollback_count += 1
        return original_rollback()

    monkeypatch.setattr(ca_bulk_fulltext, "_execute_provenance_repairs", race_before_repair)
    monkeypatch.setattr(db_session, "rollback", count_rollback)
    with pytest.raises(RuntimeError, match="expected 1, updated 0"):
        _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert rollback_count == 1


def test_provenance_repair_rejects_file_provenance_drift_after_planning(db_session, monkeypatch):
    jurisdiction, document, parse_result = _make_owned_checksum_equal_document(db_session)
    original_repair = ca_bulk_fulltext._execute_provenance_repairs

    def replace_file_url(db, repairs, *, canonical_source_url, jurisdiction_abbreviation):
        db.execute(
            update(BillDocument)
            .where(BillDocument.id == document.id)
            .values(source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_2023.zip")
        )
        return original_repair(
            db,
            repairs,
            canonical_source_url=canonical_source_url,
            jurisdiction_abbreviation=jurisdiction_abbreviation,
        )

    monkeypatch.setattr(ca_bulk_fulltext, "_execute_provenance_repairs", replace_file_url)
    with pytest.raises(RuntimeError, match="expected 1, updated 0"):
        _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)


def test_provenance_repair_fails_closed_if_jurisdiction_moves_after_planning(db_session, monkeypatch):
    jurisdiction, _document, parse_result = _make_owned_checksum_equal_document(db_session)
    original_repair = ca_bulk_fulltext._execute_provenance_repairs
    original_rollback = db_session.rollback
    rollback_count = 0

    def move_jurisdiction_before_repair(db, repairs, *, canonical_source_url, jurisdiction_abbreviation):
        db.execute(
            update(Jurisdiction)
            .where(Jurisdiction.id == jurisdiction.id)
            .values(abbreviation=f"ZZ_MOVED_{uuid.uuid4().hex[:8]}")
        )
        return original_repair(
            db,
            repairs,
            canonical_source_url=canonical_source_url,
            jurisdiction_abbreviation=jurisdiction_abbreviation,
        )

    def count_rollback():
        nonlocal rollback_count
        rollback_count += 1
        return original_rollback()

    monkeypatch.setattr(ca_bulk_fulltext, "_execute_provenance_repairs", move_jurisdiction_before_repair)
    monkeypatch.setattr(db_session, "rollback", count_rollback)
    with pytest.raises(RuntimeError, match="expected 1, updated 0"):
        _apply_scoped_to_jurisdiction(db_session, parse_result, jurisdiction.abbreviation)
    assert rollback_count == 1


def _apply_scoped_to_jurisdiction(db_session, parse_result: ParseResult, abbreviation: str, *, dry_run: bool = False) -> ApplyResult:
    """Exercise the production implementation against an isolated test scope."""

    return ca_bulk_fulltext._apply_ca_bulk_fulltext_for_jurisdiction(
        db_session,
        parse_result,
        jurisdiction_abbreviation=abbreviation,
        dry_run=dry_run,
    )
