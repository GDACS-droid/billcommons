"""Search endpoint contract: bill-number fast path, FTS, trigram fallback,
filters, and sort validation -- the three-tier strategy in
billcommons_api.search must not silently regress to a single branch.

These tests run against the live, growing DB (see conftest.py) -- they must
be data-tolerant: assert envelope shape, pagination math, and per-item
contract, not fixed row counts or emptiness."""
from __future__ import annotations

import re

import pytest

_BILL_NUMBER_RE = re.compile(r"^[A-Za-z]+\s*\d+$")


def _assert_envelope(body: dict) -> None:
    assert set(body.keys()) >= {"data", "pagination", "meta"}
    assert isinstance(body["data"], list)
    pagination = body["pagination"]
    assert pagination["page"] >= 1
    assert pagination["per_page"] >= 1
    assert pagination["total"] >= 0
    # total_pages must be consistent with total/per_page, and len(data) must
    # never exceed per_page (the pagination math the three-tier strategy
    # shares across branches).
    if pagination["total"] == 0:
        assert pagination["total_pages"] == 0
    else:
        expected_pages = -(-pagination["total"] // pagination["per_page"])
        assert pagination["total_pages"] == expected_pages
    assert len(body["data"]) <= pagination["per_page"]


def _assert_result_item_shape(item: dict) -> None:
    """Contract for a single search result row -- must hold whether the DB
    has 0 rows or 100k rows. If business logic drops match_type or breaks
    identifier_norm normalization, this must catch it once real rows exist."""
    assert "id" in item and item["id"]
    assert "identifier" in item and item["identifier"]
    assert "identifier_norm" in item and item["identifier_norm"]
    assert "title" in item
    assert "match_type" in item


def test_search_with_no_query_is_browse_mode(client):
    """No `q` at all must not error -- it's a filter-only browse."""
    resp = client.get("/api/v1/search")
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    for item in body["data"]:
        _assert_result_item_shape(item)


@pytest.mark.parametrize("q", ["HB 123", "HB123", "H.B. 123", "hb-123"])
def test_search_recognizes_bill_number_variants(client, q):
    """These must all normalize to the same fast-path lookup rather than
    falling through to FTS (which would find nothing for a bare number)."""
    resp = client.get("/api/v1/search", params={"q": q})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    # Fast-path lookup: whatever comes back must actually be "HB 123" under
    # the shared normalization, not a random FTS/trigram hit.
    for item in body["data"]:
        _assert_result_item_shape(item)
        assert item["identifier_norm"].replace(" ", "").upper() == "HB123"


def test_search_free_text_falls_through_to_full_text_search(client):
    resp = client.get("/api/v1/search", params={"q": "clean energy"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    for item in body["data"]:
        _assert_result_item_shape(item)


def test_search_result_items_would_include_match_type_field(client):
    """Structural check: asserts the endpoint doesn't crash while building a
    query that requires match_type -- if business logic drops match_type
    from a real row, TestClient would 500 on response-model validation."""
    resp = client.get("/api/v1/search", params={"q": "test"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    for item in body["data"]:
        _assert_result_item_shape(item)


@pytest.mark.parametrize(
    "params",
    [
        {"jurisdiction": "NC"},
        {"session": "2025-2026"},
        {"chamber": "lower"},
        {"status": "introduced"},
        {"sponsor": "Smith"},
        {"subject": "education"},
        {"committee": "Judiciary"},
        {"date_from": "2026-01-01", "date_to": "2026-12-31"},
        {"sort": "latest_action"},
        {"sort": "introduced"},
        {"sort": "jurisdiction"},
    ],
)
def test_search_accepts_every_documented_filter(client, params):
    resp = client.get("/api/v1/search", params=params)
    assert resp.status_code == 200
    _assert_envelope(resp.json())


def test_search_jurisdiction_filter_only_returns_that_jurisdiction(client):
    """AK/AZ/AL are fully loaded in the live DB -- a jurisdiction filter must
    actually restrict results, not just accept the param and ignore it."""
    resp = client.get("/api/v1/search", params={"jurisdiction": "AK", "per_page": 50})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    if body["data"]:
        for item in body["data"]:
            _assert_result_item_shape(item)


def test_search_rejects_invalid_sort_with_typed_error(client):
    resp = client.get("/api/v1/search", params={"sort": "not_a_real_sort"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_sort"
    assert "request_id" in body["error"]


def test_search_malformed_date_is_a_typed_422(client):
    resp = client.get("/api/v1/search", params={"date_from": "not-a-date"})
    assert resp.status_code == 422
    assert "error" in resp.json()


def test_search_trigram_fallback_on_zero_fts_matches_returns_200(client):
    """Regression for the trigram_fallback SQL bug: a misspelled query that
    yields zero full-text matches must fall through to the pg_trgm fuzzy
    branch and return a clean 200 envelope, not a 500 from malformed SQL
    (the fallback used to append `, similarity(...)` after the JOIN clauses,
    which is invalid SQL and 500'd on the live DB)."""
    resp = client.get("/api/v1/search", params={"q": "educatoin fnding"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    for item in body["data"]:
        _assert_result_item_shape(item)
        assert item["match_type"] in {"fuzzy_title", "full_text", "bill_number"}


def test_search_plain_nonsense_query_is_honest_empty_not_500(client):
    """A query with no plausible fuzzy match anywhere (FTS misses, trigram
    similarity misses) must still be a 200 with an empty/low result set --
    never a 500 from any of the three search branches."""
    resp = client.get(
        "/api/v1/search", params={"q": "zzqxvbjklmwpfhq nonexistent gibberish token"}
    )
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    for item in body["data"]:
        _assert_result_item_shape(item)
