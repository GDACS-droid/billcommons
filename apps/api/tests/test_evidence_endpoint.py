"""The evidence endpoint is what makes a packet's permalink resolve.

An agent hands a `permalink` back with its citation. If that URL 404s, the
citation cannot be checked by the human reading the report, and the packet is
a UUID stranded in a transcript. These tests cover the parts a reader depends
on: the snapshot id is present and stable, the download is actually a
download, and re-checking a citation is cheap.

Empty-DB tolerant per conftest: everything here skips if no bill exists.
"""
from __future__ import annotations

import pytest

from billcommons_shared.evidence import DIGEST_VERSION


@pytest.fixture()
def a_bill_id(client):
    res = client.get("/api/v1/bills", params={"per_page": 1})
    assert res.status_code == 200
    items = res.json().get("data") or res.json().get("items") or []
    if not items:
        pytest.skip("no bills in the test database")
    return items[0]["id"]


def test_evidence_packet_has_a_versioned_snapshot(client, a_bill_id):
    res = client.get(f"/api/v1/bills/{a_bill_id}/evidence")
    assert res.status_code == 200
    cite = res.json()["how_to_cite"]
    assert cite["snapshot_id"].startswith(f"{DIGEST_VERSION}_")
    assert cite["permalink"].endswith(f"/evidence/{a_bill_id}")
    assert "does not archive packets" in cite["reproducibility"]


def test_snapshot_is_stable_across_requests(client, a_bill_id):
    """Two reads of an unchanged bill must agree. An id that churns on its own
    would report every citation as broken."""
    a = client.get(f"/api/v1/bills/{a_bill_id}/evidence").json()["how_to_cite"]
    b = client.get(f"/api/v1/bills/{a_bill_id}/evidence").json()["how_to_cite"]
    assert a["snapshot_id"] == b["snapshot_id"]
    # retrieved_at is when you asked, NOT when the record changed -- so it must
    # not be folded into the snapshot. If it were, every read would look like a
    # change and the id would be worthless.
    assert "retrieved_at" in a and "retrieved_at" in b


def test_rechecking_a_citation_is_a_304(client, a_bill_id):
    """The common operation is 'does what I cited still hold?'. That should
    cost a conditional request, not a full payload."""
    first = client.get(f"/api/v1/bills/{a_bill_id}/evidence")
    etag = first.headers["ETag"]
    again = client.get(
        f"/api/v1/bills/{a_bill_id}/evidence", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304


def test_download_serves_an_attachment_named_for_the_snapshot(client, a_bill_id):
    res = client.get(f"/api/v1/bills/{a_bill_id}/evidence", params={"download": "1"})
    assert res.status_code == 200
    disposition = res.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert res.json()["how_to_cite"]["snapshot_id"] in disposition


def test_the_question_is_echoed_into_the_artifact(client, a_bill_id):
    res = client.get(
        f"/api/v1/bills/{a_bill_id}/evidence",
        params={"question": "Did this become law?"},
    )
    assert res.json()["request"]["question"] == "Did this become law?"


def test_status_is_never_presented_as_official(client, a_bill_id):
    """The whole reason this packet exists. `status` is our conclusion, and a
    citation is precisely where that gets laundered into a fact."""
    body = client.get(f"/api/v1/bills/{a_bill_id}/evidence").json()
    assert "status" in body["record"]["derived_fields"]
    assert "derived by Bill Commons" in body["record"]["derived_note"]
    if body["record"]["data"]["status"]:
        assert "derived by Bill Commons" in body["how_to_cite"]["cite_as"]


def test_hearings_absence_is_never_reported_as_zero(client, a_bill_id):
    """A hearings count of 0 reads as 'none scheduled'. We do not collect
    hearings at all, which is a different and much weaker claim."""
    body = client.get(f"/api/v1/bills/{a_bill_id}/evidence").json()
    assert body["counts"]["hearings"] is None
    assert "not collected" in body["hearings_note"]


def test_a_missing_bill_is_a_404_not_an_empty_packet(client):
    res = client.get("/api/v1/bills/00000000-0000-0000-0000-000000000000/evidence")
    assert res.status_code == 404
