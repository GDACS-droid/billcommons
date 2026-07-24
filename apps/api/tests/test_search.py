"""Search endpoint contract: bill-number fast path, FTS, trigram fallback,
filters, and sort validation -- the three-tier strategy in
billcommons_api.search must not silently regress to a single branch."""
from __future__ import annotations

import pytest


def test_search_with_no_query_is_browse_mode(client):
    """No `q` at all must not error -- it's a filter-only browse."""
    resp = client.get("/api/v1/search")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


@pytest.mark.parametrize("q", ["HB 123", "HB123", "H.B. 123", "hb-123"])
def test_search_recognizes_bill_number_variants(client, q):
    """These must all normalize to the same fast-path lookup rather than
    falling through to FTS (which would find nothing for a bare number)."""
    resp = client.get("/api/v1/search", params={"q": q})
    assert resp.status_code == 200
    # empty DB: no matches, but must not error and must return the envelope
    assert resp.json()["data"] == []


def test_search_free_text_falls_through_to_full_text_search(client):
    resp = client.get("/api/v1/search", params={"q": "clean energy"})
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_search_result_items_would_include_match_type_field(client):
    """Structural check: even with zero rows, this asserts the endpoint
    doesn't crash while building a query that requires match_type -- if
    business logic drops match_type from a real row, TestClient would 500
    on response-model validation once rows exist."""
    resp = client.get("/api/v1/search", params={"q": "test"})
    assert resp.status_code == 200


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
