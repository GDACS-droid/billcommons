"""Every list endpoint must return the locked pagination envelope
{data, pagination:{page,per_page,total,total_pages}, meta:{...}} -- this is
the contract web/mcp consumers are built against, so shape drift here is a
breaking change even on an empty DB."""
from __future__ import annotations

import pytest

LIST_ENDPOINTS = [
    "/api/v1/jurisdictions",
    "/api/v1/sessions",
    "/api/v1/bills",
    "/api/v1/people",
    "/api/v1/committees",
    "/api/v1/events",
    "/api/v1/sources",
    "/api/v1/coverage",
    "/api/v1/search",
]


@pytest.mark.parametrize("path", LIST_ENDPOINTS)
def test_list_endpoint_envelope_shape(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    body = resp.json()

    assert set(body.keys()) == {"data", "pagination", "meta"}
    assert isinstance(body["data"], list)

    pagination = body["pagination"]
    assert set(pagination.keys()) == {"page", "per_page", "total", "total_pages"}
    assert pagination["page"] == 1
    assert pagination["total"] >= 0
    # total_pages must be the ceiling of total/per_page (0 when total is 0) --
    # this is the actual math a client paginates against, not just a present key.
    if pagination["total"] == 0:
        assert pagination["total_pages"] == 0
    else:
        expected = -(-pagination["total"] // pagination["per_page"])
        assert pagination["total_pages"] == expected

    meta = body["meta"]
    assert meta["api_version"] == "v1"
    assert meta["request_id"]


@pytest.mark.parametrize("path", [p for p in LIST_ENDPOINTS if p != "/api/v1/coverage"])
def test_per_page_is_clamped_to_max_50(client, path):
    resp = client.get(path, params={"per_page": 500})
    assert resp.status_code == 200
    assert resp.json()["pagination"]["per_page"] == 50


def test_coverage_per_page_allows_full_matrix(client):
    """The coverage matrix must never truncate the 51-jurisdiction view
    (SPEC: no jurisdiction silently missing) -- it clamps at 200, not 50."""
    resp = client.get("/api/v1/coverage", params={"per_page": 500})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["per_page"] == 200
    # the real invariant: when the registry is seeded, one page holds every
    # jurisdiction -- silent truncation of the matrix is the failure mode
    codes = {r["jurisdiction_code"] for r in body["data"]}
    if body["pagination"]["total"] >= 51:
        assert len(codes) >= 51, f"coverage page truncated: {len(codes)} jurisdictions"


@pytest.mark.parametrize("path", LIST_ENDPOINTS)
def test_page_2_beyond_data_is_still_200_with_empty_data(client, path):
    """On an empty/sparse DB, requesting page 2 must not 404/500 -- it's a
    valid page that simply has no rows yet."""
    resp = client.get(path, params={"page": 2, "per_page": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["page"] == 2
    assert isinstance(body["data"], list)


def test_request_id_header_present_and_matches_body(client):
    resp = client.get("/api/v1/jurisdictions")
    assert "X-Request-ID" in resp.headers
    assert resp.headers["X-Request-ID"] == resp.json()["meta"]["request_id"]


def test_deep_pagination_is_bounded(client):
    """`page` was bounded below and not above, so ?page=1000000 became
    OFFSET 50,000,000 -- and Postgres walks every one of those rows before
    discarding them. On a public, anonymous API that is a one-line denial of
    service that costs the caller nothing."""
    res = client.get("/api/v1/bills", params={"page": 1_000_000, "per_page": 50})
    assert res.status_code == 422


def test_the_bound_does_not_break_a_legitimate_browse(client):
    """The deepest real browse -- the largest state's bill list at the maximum
    page size -- must still work."""
    res = client.get("/api/v1/bills", params={"page": 900, "per_page": 50})
    assert res.status_code == 200
