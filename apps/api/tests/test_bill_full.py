"""The aggregate bill endpoint must agree with the endpoints it replaces.

The bill page fanned out to nine requests per render over ~200,000 crawlable
pages. This collapses that, which is only safe if the collapsed payload says
exactly what the individual endpoints say -- a faster answer that quietly
differs from the canonical one is worse than the fan-out.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def a_bill_id(client):
    res = client.get("/api/v1/bills", params={"per_page": 1})
    assert res.status_code == 200
    items = res.json().get("data") or []
    if not items:
        pytest.skip("no bills in the test database")
    return items[0]["id"]


def test_full_matches_the_individual_endpoints(client, a_bill_id):
    full = client.get(f"/api/v1/bills/{a_bill_id}/full")
    assert full.status_code == 200
    body = full.json()

    for section, path in [
        ("versions", "versions"),
        ("actions", "actions"),
        ("sponsors", "sponsors"),
        ("votes", "votes"),
        ("documents", "documents"),
        ("related", "related"),
        ("subjects", "subjects"),
    ]:
        one = client.get(f"/api/v1/bills/{a_bill_id}/{path}")
        assert one.status_code == 200
        assert body[section] == one.json(), f"{section} disagrees with /{path}"

    detail = client.get(f"/api/v1/bills/{a_bill_id}")
    assert body["bill"] == detail.json()


def test_full_carries_the_derived_enrollment_flag(client, a_bill_id):
    """Composed from the real handler, not re-implemented -- so the flag that
    stops an enrolled-but-adjourned bill reading as 'awaiting signature' must
    survive the collapse."""
    body = client.get(f"/api/v1/bills/{a_bill_id}/full").json()
    assert "enrolled_outcome_uncaptured" in body["bill"]


def test_full_includes_the_jurisdiction(client, a_bill_id):
    """The ninth request the page used to make."""
    body = client.get(f"/api/v1/bills/{a_bill_id}/full").json()
    assert body["jurisdiction"] is not None
    assert body["jurisdiction"]["id"] == body["bill"]["jurisdiction_id"]


def test_missing_bill_is_a_404(client):
    res = client.get("/api/v1/bills/00000000-0000-0000-0000-000000000000/full")
    assert res.status_code == 404
