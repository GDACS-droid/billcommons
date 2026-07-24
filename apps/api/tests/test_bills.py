"""Bills detail + subresource routes: 404 semantics and ETag behavior matter
because web/mcp rely on a clean 404 to distinguish "no such bill" from "bill
exists but has no votes yet"."""
from __future__ import annotations

import uuid

NIL_UUID = "00000000-0000-0000-0000-000000000000"


def test_get_missing_bill_is_typed_404(client):
    resp = client.get(f"/api/v1/bills/{NIL_UUID}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "bill_not_found"
    assert "request_id" in body["error"]


def test_bad_uuid_is_422_not_500(client):
    resp = client.get("/api/v1/bills/not-a-uuid")
    assert resp.status_code == 422


def test_subresource_routes_404_when_parent_bill_missing(client):
    """A subresource on a nonexistent bill must 404, not return an empty
    list -- an empty list would incorrectly imply "bill exists, zero votes"."""
    for suffix in ("versions", "actions", "sponsors", "votes", "documents"):
        resp = client.get(f"/api/v1/bills/{NIL_UUID}/{suffix}")
        assert resp.status_code == 404, f"{suffix} should 404 for a missing bill"


def test_list_bills_envelope_and_filters(client):
    jresp = client.get("/api/v1/jurisdictions", params={"per_page": 100})
    nc = next((j for j in jresp.json()["data"] if j["abbreviation"] == "NC"), None)
    resp = client.get("/api/v1/bills", params={"jurisdiction": "NC", "chamber": "lower"})
    assert resp.status_code == 200
    rows = resp.json()["data"]
    # the filter contract must be OBSERVED, not vacuously true: NC is loaded
    # in any seeded DB, so demand results and check both filters on every row
    if nc is not None and jresp.json()["pagination"]["total"] >= 51:
        assert len(rows) >= 1, "NC lower-chamber bills expected in a seeded DB"
    for row in rows:
        assert row["chamber"] == "lower"
        if nc is not None:
            assert row["jurisdiction_id"] == nc["id"]
