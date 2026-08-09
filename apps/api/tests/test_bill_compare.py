"""GET /bills/{id}/compare?from=..&to=.. -- deterministic diff of two bill
version texts (docs/SPEC.md "Version diffing"). Response shape must match
apps/web/app/bills/[id]/compare/page.tsx's CompareResponse exactly:
{data: {bill_id, from_version_id, to_version_id, diff_lines: [{type,text}]}, meta}.
Empty-DB tolerant: no fixture rows are assumed to exist."""
from __future__ import annotations

import pytest

NIL_UUID = "00000000-0000-0000-0000-000000000000"


def test_compare_missing_bill_is_typed_404(client):
    resp = client.get(
        f"/api/v1/bills/{NIL_UUID}/compare",
        params={"from": NIL_UUID, "to": NIL_UUID},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "bill_not_found"
    assert "request_id" in body["error"]


def test_compare_missing_query_params_is_422(client):
    resp = client.get(f"/api/v1/bills/{NIL_UUID}/compare")
    assert resp.status_code == 422


def test_compare_bad_version_uuid_is_422(client):
    resp = client.get(
        f"/api/v1/bills/{NIL_UUID}/compare",
        params={"from": "not-a-uuid", "to": NIL_UUID},
    )
    assert resp.status_code == 422


def test_compare_unknown_version_on_real_bill_is_404_or_bill_404(client):
    """Whether the bill_id itself is unknown or its versions are unknown, the
    endpoint must never 500 -- always a typed error."""
    resp = client.get(
        f"/api/v1/bills/{NIL_UUID}/compare",
        params={"from": NIL_UUID, "to": "11111111-1111-1111-1111-111111111111"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] in {"bill_not_found", "version_not_found"}


def test_compare_response_shape_when_bill_exists_but_no_versions_match(client):
    """Pick a real bill from the seeded DB (if any) and hit compare with
    version ids that don't belong to it -- must 404 version_not_found, never
    500, and never a raw ORM dump."""
    bills_resp = client.get("/api/v1/bills", params={"per_page": 1})
    assert bills_resp.status_code == 200
    rows = bills_resp.json()["data"]
    if not rows:
        return  # empty-DB tolerant: nothing further to assert
    bill_id = rows[0]["id"]
    resp = client.get(
        f"/api/v1/bills/{bill_id}/compare",
        params={"from": NIL_UUID, "to": "11111111-1111-1111-1111-111111111111"},
    )
    assert resp.status_code in (404, 409)
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] in {
        "version_not_found",
        "extracted_text_unavailable",
    }


def test_documents_expose_the_text_is_partial_flag(client):
    """Every document row must carry text_is_partial so consumers can tell
    salvaged-from-malformed-PDF text (incomplete by construction) from
    complete extractions before quoting or diffing it."""
    res = client.get("/api/v1/bills", params={"per_page": 1})
    assert res.status_code == 200
    items = res.json().get("data") or []
    if not items:
        pytest.skip("no bills in the test database")
    docs = client.get(f"/api/v1/bills/{items[0]['id']}/documents")
    assert docs.status_code == 200
    for doc in docs.json():
        assert "text_is_partial" in doc
        assert isinstance(doc["text_is_partial"], bool)
