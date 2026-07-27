"""Bills detail + subresource routes: 404 semantics and ETag behavior matter
because web/mcp rely on a clean 404 to distinguish "no such bill" from "bill
exists but has no votes yet"."""
from __future__ import annotations

import uuid

import pytest

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


def test_list_bills_identifier_filter_resolves_a_single_bill(client):
    """A bill must be addressable by its NUMBER, not just its UUID.

    This is the lookup real consumers reach for ("does it have HI SB 2135?")
    and it is what the /states/{code}/bills/{session}/{slug} web routes resolve
    against, so a regression here breaks both the public API contract and every
    keyword bill URL on the site.
    """
    seed = client.get("/api/v1/bills", params={"jurisdiction": "NC", "per_page": 1})
    assert seed.status_code == 200
    rows = seed.json()["data"]
    if not rows:
        pytest.skip("no NC bills loaded in this DB")
    target = rows[0]

    resp = client.get(
        "/api/v1/bills",
        params={"jurisdiction": "NC", "identifier": target["identifier"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Narrowing is the whole point: an ignored filter would return the entire
    # NC corpus (thousands), so a small total is what proves the WHERE ran.
    assert body["pagination"]["total"] <= 20, "identifier filter did not narrow the result set"
    assert body["data"], "the bill we just read back must match its own identifier"
    for row in body["data"]:
        assert row["identifier_norm"] == target["identifier_norm"]
    assert any(row["id"] == target["id"] for row in body["data"])


def test_list_bills_identifier_filter_is_normalization_insensitive(client):
    """'hb123', 'H.B. 123' and 'HB 123' are the same bill to a human, so they
    must be the same bill to the API -- consumers type bill numbers by hand and
    the slug in a URL ('hb-123') is yet another surface form."""
    seed = client.get("/api/v1/bills", params={"jurisdiction": "NC", "per_page": 1})
    rows = seed.json()["data"]
    if not rows:
        pytest.skip("no NC bills loaded in this DB")
    target = rows[0]
    canonical = target["identifier_norm"]

    scrambled = canonical.replace(" ", "").lower()
    if scrambled == canonical:
        pytest.skip(f"identifier {canonical!r} has no distinct scrambled form")

    resp = client.get(
        "/api/v1/bills", params={"jurisdiction": "NC", "identifier": scrambled}
    )
    assert resp.status_code == 200
    body = resp.json()
    found = body["data"]
    # Without the narrowing assertion this test passes vacuously: an IGNORED
    # filter returns the unfiltered NC list, whose first page still contains
    # `target`. Demand the result set actually shrank.
    assert body["pagination"]["total"] <= 20, "identifier filter did not narrow the result set"
    assert found, f"{scrambled!r} must resolve to the same bill as {canonical!r}"
    assert any(row["id"] == target["id"] for row in found)


def _find_bill_with(client, path_suffix, minimum=1):
    """Find a real bill whose subresource is non-empty. The corpus is uneven --
    only ~28% of bills have related-bill links and ~42% have subjects -- so a
    test that grabs an arbitrary bill would pass vacuously on an empty list."""
    for jur in ("AL", "NC", "GA", "TX", "CA"):
        listing = client.get("/api/v1/bills", params={"jurisdiction": jur, "per_page": 50})
        if listing.status_code != 200:
            continue
        for row in listing.json()["data"]:
            resp = client.get(f"/api/v1/bills/{row['id']}/{path_suffix}")
            if resp.status_code == 200 and len(resp.json()) >= minimum:
                return row, resp.json()
    return None, None


def test_related_bills_are_exposed_with_identifier_and_type(client):
    """~100k related-bill rows (47k of them prior-session) were collected and
    never served. Cross-session linking is the hardest part of multi-year
    policy tracking, so each link must carry enough to act on: what KIND of
    relationship, and WHICH bill."""
    bill, related = _find_bill_with(client, "related")
    if bill is None:
        pytest.skip("no bill with related-bill links found in this DB")
    for link in related:
        assert link["relation_type"], "a link with no relation_type is unusable"
        # The identifier is what makes an UNRESOLVED link still actionable.
        assert link["related_identifier"] or link["related_bill_id"]


def test_related_bills_resolve_same_session_companions(client):
    """Upstream stores only an identifier. Companions live in the same session,
    so they should resolve to a real bill id -- otherwise the resolution step
    is dead code and consumers must do the lookup themselves."""
    for jur in ("AL", "NC", "GA"):
        listing = client.get("/api/v1/bills", params={"jurisdiction": jur, "per_page": 50})
        if listing.status_code != 200:
            continue
        for row in listing.json()["data"]:
            links = client.get(f"/api/v1/bills/{row['id']}/related").json()
            for link in links:
                if link["relation_type"] == "companion" and link["related_bill_id"]:
                    target = client.get(f"/api/v1/bills/{link['related_bill_id']}")
                    assert target.status_code == 200, "resolved id must be fetchable"
                    return
    pytest.skip("no resolvable companion link found in this DB")


def test_bill_subjects_are_exposed(client):
    bill, subjects = _find_bill_with(client, "subjects")
    if bill is None:
        pytest.skip("no bill with subjects found in this DB")
    assert all(isinstance(s, str) and s for s in subjects)


def test_subject_filter_narrows_and_every_row_actually_has_the_subject(client):
    """A filter that returns the whole corpus is worse than no filter."""
    bill, subjects = _find_bill_with(client, "subjects")
    if bill is None:
        pytest.skip("no bill with subjects found in this DB")
    subject = subjects[0]

    unfiltered = client.get("/api/v1/bills", params={"per_page": 1}).json()["pagination"]["total"]
    resp = client.get("/api/v1/bills", params={"subject": subject, "per_page": 25})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] < unfiltered, "subject filter did not narrow anything"
    assert body["data"], "the subject we read off a real bill must match at least that bill"
    for row in body["data"]:
        got = client.get(f"/api/v1/bills/{row['id']}/subjects").json()
        assert any(s.lower() == subject.lower() for s in got), (
            f"bill {row['id']} came back for subject {subject!r} but does not carry it"
        )


def test_related_and_subjects_404_for_a_missing_bill(client):
    for suffix in ("related", "subjects"):
        resp = client.get(f"/api/v1/bills/{NIL_UUID}/{suffix}")
        assert resp.status_code == 404, f"{suffix} must 404 for a missing bill"
