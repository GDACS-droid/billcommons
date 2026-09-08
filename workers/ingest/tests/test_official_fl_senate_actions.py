"""Contracts for the pure Florida Senate bill-history parser."""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from billcommons_ingest import official_fl_senate_actions as adapter


SOURCE_URL = "https://www.flsenate.gov/Session/Bill/2025/7031"
FIXTURE = Path(__file__).parent / "fixtures" / "fl_senate_detail_2025_7031.html"
SOURCE_CAPTURE_SHA256 = "e57e81a8826451dba2b758771274715dd3d8377582fecec54272b3ba9bb73698"
FIXTURE_SHA256 = "3e543da731b746162a92f71c193de5cede4a3bdfa6d059edd0491e4f477c4480"


def _raw() -> bytes:
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    assert SOURCE_CAPTURE_SHA256.encode() in raw
    return raw


def _parse() -> adapter.ParsedFloridaSenateBillHistory:
    return adapter.parse_florida_senate_bill_history(_raw(), source_url=SOURCE_URL)


def test_parses_public_captured_detail_fixture_with_fact_and_location_evidence():
    parsed = _parse()

    assert parsed.source_url == SOURCE_URL
    assert parsed.source_sha256 == FIXTURE_SHA256
    assert (parsed.session_year, parsed.bill_number, parsed.bill_identifier, parsed.bill_title) == (
        "2025", "7031", "HB 7031", "Taxation",
    )
    assert parsed.table_row_count == 17
    assert len(parsed.actions) == 46
    assert parsed.actions[0] == adapter.FloridaSenateAction(
        action_date=date(2025, 4, 2), chamber="House", description="Filed",
        source_row_position=1, source_bullet_position=1,
    )
    assert parsed.actions[40].as_evidence() == {
        "date": "2025-06-27", "date_precision": "day", "chamber": None,
        "description": "Signed by Officers and presented to Governor",
        "source_row_position": 13, "source_bullet_position": 1,
    }
    # The page is a 2025 session detail page, yet the official final action is
    # in 2026.  The parser preserves the source day and does not infer a
    # session-bound action date.
    assert parsed.actions[-1].as_evidence() == {
        "date": "2026-06-18", "date_precision": "day", "chamber": "House",
        "description": "Veto Message transmitted to Secretary of State",
        "source_row_position": 17, "source_bullet_position": 1,
    }
    assert not hasattr(parsed.actions[-1], "occurrence_id")


@pytest.mark.parametrize(
    ("source_url", "message"),
    [
        ("http://www.flsenate.gov/Session/Bill/2025/7031", "exact https"),
        ("https://www.flsenate.gov/Session/Bill/2025/7031?StartTab=BillHistory", "exact https"),
        ("https://www.flsenate.gov/Session/Bill/2025/07031", "exact https"),
        ("https://www.flsenate.gov/Session/Bill/2025/7031/", "exact https"),
        ("\nhttps://www.flsenate.gov/Session/Bill/2025/7031", "canonical detail URL"),
        ("https://[::1", "could not be parsed"),
        ("https://www.flsenate.gov:99999/Session/Bill/2025/7031", "could not be parsed"),
        ("https://www.flsenate.gov/Session/Bill/2024/7031", "title disagrees"),
    ],
)
def test_rejects_unscoped_or_page_scope_disagreeing_urls(source_url: str, message: str):
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match=message):
        adapter.parse_florida_senate_bill_history(_raw(), source_url=source_url)


def test_rejects_source_url_over_whole_string_cap():
    overlong = SOURCE_URL + ("x" * adapter.MAX_SOURCE_URL_CHARS)
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="length cap"):
        adapter.parse_florida_senate_bill_history(_raw(), source_url=overlong)


def test_enforces_dom_depth_before_recursive_extraction():
    boundary = adapter._BoundedTreeBuilder()
    boundary.feed("<div>" * adapter.MAX_DOM_DEPTH + "</div>" * adapter.MAX_DOM_DEPTH)
    boundary.close()

    overbound = adapter._BoundedTreeBuilder()
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="DOM depth cap"):
        overbound.feed("<div>" * (adapter.MAX_DOM_DEPTH + 1))

    # The historic regression wrapped a real, valid detail page.  The parser
    # must report a bounded source-contract failure before its recursive
    # extraction helpers walk that synthetic depth.
    wrapped = (b"<div>" * adapter.MAX_DOM_DEPTH) + _raw() + (b"</div>" * adapter.MAX_DOM_DEPTH)
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="DOM depth cap"):
        adapter.parse_florida_senate_bill_history(wrapped, source_url=SOURCE_URL)


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        (b"House Bill 7031 (2025) - The Florida Senate", b"Senate Bill 7031 (2025) - The Florida Senate", "heading disagrees"),
        (b"<h2>HB 7031: Taxation</h2>", b"<h2>HB 7032: Taxation</h2>", "heading disagrees"),
        (b"<td class=\"centertext\">House</td>", b"<td class=\"centertext\">Committee</td>", "invalid chamber"),
        (b"&bull; Filed<br>", b"Filed<br>", "lacks bullet facts"),
    ],
)
def test_rejects_title_heading_or_row_field_contract_changes(before: bytes, after: bytes, message: str):
    raw = _raw()
    assert before in raw
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match=message):
        adapter.parse_florida_senate_bill_history(raw.replace(before, after), source_url=SOURCE_URL)


def test_rejects_duplicate_history_tables_and_last_action_mismatch():
    raw = _raw()
    marker = b'<div class="tabbody " id="tabBodyBillHistory">'
    duplicate = raw.replace(marker, marker + b'<table><thead></thead><tbody></tbody></table>', 1)
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="exactly one Bill History table"):
        adapter.parse_florida_senate_bill_history(duplicate, source_url=SOURCE_URL)

    missing = raw.replace(b'id="tabBodyBillHistory"', b'id="notBillHistory"', 1)
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="exactly one Bill History container"):
        adapter.parse_florida_senate_bill_history(missing, source_url=SOURCE_URL)

    mismatched = raw.replace(
        b"Veto Message transmitted to Secretary of State<br>",
        b"Different final action<br>",
        1,
    )
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="Last Action field disagrees"):
        adapter.parse_florida_senate_bill_history(mismatched, source_url=SOURCE_URL)

    future_dated_history = raw.replace(b"4/2/2025", b"6/19/2026", 1)
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="does not equal the latest"):
        adapter.parse_florida_senate_bill_history(future_dated_history, source_url=SOURCE_URL)


def test_rejects_malformed_date_and_bounded_input():
    raw = _raw()
    padded_date = raw.replace(b"4/2/2025", b"04/02/2025", 1)
    assert adapter.parse_florida_senate_bill_history(padded_date, source_url=SOURCE_URL).actions[0].action_date == date(2025, 4, 2)
    malformed_date = raw.replace(b"4/2/2025", b"4-2-2025", 1)
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="invalid exact m/d/yyyy date"):
        adapter.parse_florida_senate_bill_history(malformed_date, source_url=SOURCE_URL)
    with pytest.raises(adapter.OfficialFloridaSenateActionsError, match="byte bounds"):
        adapter.parse_florida_senate_bill_history(b"x" * (adapter.MAX_HTML_BYTES + 1), source_url=SOURCE_URL)
