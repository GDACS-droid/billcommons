"""Unit tests for the URL resolver layer (workers/ingest/billcommons_ingest/
url_resolvers.py). Pure functions, no DB/network fixtures needed.

The MA docket -> authoritative-bill-number resolution itself is a live,
multi-step lookup (fulltext._resolve_ma_document) and is tested with
network mocks in tests/test_fulltext.py instead -- see that file's
"MA docket resolution" section.
"""
from __future__ import annotations

import json

import pytest

from billcommons_ingest.fulltext import (
    STATUS_FETCH_ERROR,
    STATUS_NO_DOCUMENT_TEXT,
    STATUS_OK,
    DocumentFetchError,
    extract_document_text,
    extract_text_from_ma_document_json,
    sniff_content_type,
)
from billcommons_ingest.url_resolvers import (
    RESOLVER_RULES,
    is_ma_docket_id,
    ma_api_url,
    ma_docket_from_url,
    resolve_fetch_url,
    resolver_name_for_candidate,
)

MA_DOCKET_URL = "https://malegislature.gov/Bills/194/HD177.pdf"
MA_ALREADY_BILL_STYLE_URL = "https://malegislature.gov/Bills/194/H177.pdf"
IA_STALE_URL = "https://www.legis.iowa.gov/docs/publications/LGEG/91/attachments/SF397.html"


# ---------------------------------------------------------------------------
# no-rule passthrough -- MA is deliberately NOT in RESOLVER_RULES anymore
# (see module docstring in url_resolvers.py): its resolution is a live,
# multi-step lookup handled entirely in fulltext.py, not a static candidate
# list, so resolve_fetch_url treats it the same as any other jurisdiction
# with no matching rule.
# ---------------------------------------------------------------------------


def test_no_rule_jurisdiction_returns_only_the_original_url():
    url = "https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=1"
    assert resolve_fetch_url("ca", url) == [url]


def test_unknown_jurisdiction_code_returns_only_the_original_url():
    assert resolve_fetch_url("zz", MA_DOCKET_URL) == [MA_DOCKET_URL]


def test_ma_is_not_in_resolver_rules_and_resolve_fetch_url_returns_only_the_original():
    # MA resolution needs live network round-trips (fetch the docket's
    # record, read its authoritative BillNumber, only then fetch the
    # bill's own record) -- it cannot be expressed as this module's
    # data-driven, network-free candidate list, so it is handled entirely
    # by fulltext._resolve_ma_document, not resolve_fetch_url.
    assert {rule.jurisdiction for rule in RESOLVER_RULES} == {"ia"}
    assert resolve_fetch_url("ma", MA_DOCKET_URL) == [MA_DOCKET_URL]
    assert resolve_fetch_url("MA", MA_DOCKET_URL) == [MA_DOCKET_URL]


def test_ia_rule_does_not_fire_for_a_url_without_the_stale_segment():
    current = "https://www.legis.iowa.gov/docs/publications/LGI/91/SF2471.pdf"
    assert resolve_fetch_url("ia", current) == [current]


# ---------------------------------------------------------------------------
# ordering: original first, then rewrites (IA -- the only remaining
# data-driven rule)
# ---------------------------------------------------------------------------


def test_ia_candidates_are_original_first_then_rewrite():
    candidates = resolve_fetch_url("ia", IA_STALE_URL)
    assert candidates == [
        IA_STALE_URL,
        "https://www.legis.iowa.gov/docs/publications/LGI/91/attachments/SF397.html",
    ]


def test_resolve_fetch_url_never_duplicates_a_candidate_equal_to_the_original():
    current = "https://www.legis.iowa.gov/docs/publications/LGI/91/SF2471.pdf"
    candidates = resolve_fetch_url("ia", current)
    assert candidates.count(current) == 1


# ---------------------------------------------------------------------------
# resolver_name_for_candidate (used to tag license_note on success)
# ---------------------------------------------------------------------------


def test_resolver_name_for_candidate_is_none_for_the_original_url():
    assert resolver_name_for_candidate("ia", IA_STALE_URL, IA_STALE_URL) is None


def test_resolver_name_for_candidate_matches_the_owning_rule():
    ia_candidate = "https://www.legis.iowa.gov/docs/publications/LGI/91/attachments/SF397.html"
    assert resolver_name_for_candidate("ia", IA_STALE_URL, ia_candidate) == "ia_lgeg_to_lgi"


def test_resolver_name_for_candidate_returns_none_for_an_unrelated_url():
    assert resolver_name_for_candidate("ia", IA_STALE_URL, "https://example.com/unrelated") is None


def test_resolver_rules_table_is_data_driven_not_hardcoded_elsewhere():
    # Every rule's jurisdiction is lowercase (resolve_fetch_url lowercases
    # the incoming code before comparing) and every name is unique (it's the
    # license_note tag, so a collision would make two different fixes
    # indistinguishable in the data).
    jurisdictions_and_names = [(rule.jurisdiction, rule.name) for rule in RESOLVER_RULES]
    assert all(code == code.lower() for code, _name in jurisdictions_and_names)
    names = [name for _code, name in jurisdictions_and_names]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# MA URL-shape helpers (pure -- the live docket->bill resolution these feed
# is tested in test_fulltext.py)
# ---------------------------------------------------------------------------


def test_ma_docket_from_url_parses_docket_style_id():
    parsed = ma_docket_from_url(MA_DOCKET_URL)
    assert parsed is not None
    assert parsed.court == "194"
    assert parsed.doc_id == "HD177"
    assert parsed.path_prefix == "https://malegislature.gov/Bills/"
    assert parsed.path_suffix == ".pdf"


def test_ma_docket_from_url_parses_already_bill_style_id():
    parsed = ma_docket_from_url(MA_ALREADY_BILL_STYLE_URL)
    assert parsed is not None
    assert parsed.doc_id == "H177"


def test_ma_docket_from_url_returns_none_for_a_non_ma_bills_url():
    assert ma_docket_from_url("https://example.com/bills/HD177.pdf") is None
    assert ma_docket_from_url("https://malegislature.gov/Committees/J26") is None


def test_ma_docket_from_url_is_anchored_to_the_official_ma_hosts():
    # T2-4: a mirror/archive URL may use MA's path convention but must stay
    # on its own direct-fetch path rather than being resolved through MA's API.
    assert ma_docket_from_url("https://archive.example/Bills/194/H100.pdf") is None
    assert ma_docket_from_url("https://www.malegislature.gov/Bills/194/H100.pdf") is not None
    assert ma_docket_from_url("https://malegislature.gov:443/Bills/194/HD177.pdf") is not None
    assert ma_docket_from_url("https://user@www.malegislature.gov/Bills/194/HD177.pdf") is not None


def test_is_ma_docket_id_distinguishes_docket_from_bill_shape():
    assert is_ma_docket_id("HD177") is True
    assert is_ma_docket_id("SD3668") is True
    assert is_ma_docket_id("hd177") is True  # case-insensitive
    # Bill ids (no "D") are NOT docket ids -- this is the entire fix: the
    # old code derived H177 from HD177 by SHAPE and treated the guess as
    # good enough to fetch. H177 is itself already bill-style and is a
    # real, unrelated bill (its own docket is HD4189, verified live
    # 2026-08-21) -- never to be conflated with docket HD177.
    assert is_ma_docket_id("H177") is False
    assert is_ma_docket_id("S2045") is False


def test_ma_api_url_builds_the_documents_endpoint():
    assert ma_api_url("194", "HD177") == "https://malegislature.gov/api/GeneralCourts/194/Documents/HD177"


# ---------------------------------------------------------------------------
# MA JSON extraction (fulltext.py's side)
# ---------------------------------------------------------------------------

MA_API_URL = "https://malegislature.gov/api/GeneralCourts/194/Documents/H177"


def test_sniff_content_type_returns_json_only_for_the_ma_api_url_shape():
    raw = json.dumps({"DocumentText": "hello"}).encode("utf-8")
    assert sniff_content_type("application/json; charset=utf-8", MA_API_URL, raw) == "json"
    # Same body, different host: NOT sniffed as json (scoped, not generic).
    assert sniff_content_type("application/json", "https://example.com/x", raw) == "text"


def test_sniff_content_type_does_not_treat_suffix_matched_hosts_as_ma_api():
    # T2-4: endswith("malegislature.gov") incorrectly accepted this host.
    raw = json.dumps({"DocumentText": "hello"}).encode("utf-8")
    url = "https://notmalegislature.gov/api/GeneralCourts/194/Documents/H177"
    assert sniff_content_type("application/json", url, raw) == "text"


def test_sniff_content_type_falls_back_to_magic_bytes_for_ma_api_without_header():
    raw = json.dumps({"DocumentText": "hello"}).encode("utf-8")
    assert sniff_content_type(None, MA_API_URL, raw) == "json"


def test_extract_text_from_ma_document_json_returns_document_text_field():
    raw = json.dumps({"DocumentText": "SECTION 1. Hello world."}).encode("utf-8")
    assert extract_text_from_ma_document_json(raw) == "SECTION 1. Hello world."


def test_extract_text_from_ma_document_json_raises_for_malformed_json():
    # Item 2: malformed JSON is a fact about THIS FETCH, never a permanent
    # verdict about the document -- must raise (so the caller classifies
    # it as a transient, retryable fetch error), never silently return an
    # empty/"no text" sentinel.
    with pytest.raises(ValueError):
        extract_text_from_ma_document_json(b"not json")


def test_extract_text_from_ma_document_json_raises_for_non_object_json():
    with pytest.raises(ValueError):
        extract_text_from_ma_document_json(b"[1, 2, 3]")


def test_extract_text_from_ma_document_json_returns_empty_string_for_non_string_field():
    raw = json.dumps({"DocumentText": None}).encode("utf-8")
    assert extract_text_from_ma_document_json(raw) == ""


def test_extract_document_text_json_branch_ok_with_real_text():
    raw = json.dumps({"DocumentText": "SECTION 1. Real bill text."}).encode("utf-8")
    outcome = extract_document_text("json", raw)
    assert outcome.status == STATUS_OK
    assert outcome.extracted_text == "SECTION 1. Real bill text."


def test_extract_document_text_json_branch_no_document_text_for_empty_field():
    # A well-formed body whose DocumentText genuinely is empty stays
    # STATUS_NO_DOCUMENT_TEXT at this generic-extractor level (this
    # differs from the MA docket resolver's own no-bill-number handling,
    # which uses STATUS_MA_DOCKET_NO_BILL_NUMBER -- non-terminal -- because
    # a docket may be assigned a bill number on a LATER day; this
    # generic-extractor path never sees that context).
    raw = json.dumps({"DocumentText": ""}).encode("utf-8")
    outcome = extract_document_text("json", raw)
    assert outcome.status == STATUS_NO_DOCUMENT_TEXT
    assert outcome.extracted_text is None


def test_extract_document_text_json_branch_malformed_body_is_never_terminal():
    # Item 2, at the extract_document_text level: malformed/non-text JSON
    # must NOT come back as STATUS_NO_DOCUMENT_TEXT (terminal) -- it must
    # raise so process_fetch_text_job's extraction-exception handling
    # classifies it as a transient, attempts-charging DocumentFetchError.
    with pytest.raises(DocumentFetchError) as excinfo:
        extract_document_text("json", b"{not valid json")
    assert excinfo.value.status == STATUS_FETCH_ERROR
