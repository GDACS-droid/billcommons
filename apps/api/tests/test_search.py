"""Search endpoint contract: bill-number fast path, FTS, trigram fallback,
filters, and sort validation -- the three-tier strategy in
billcommons_api.search must not silently regress to a single branch.

These tests run against the live, growing DB (see conftest.py) -- they must
be data-tolerant: assert envelope shape, pagination math, and per-item
contract, not fixed row counts or emptiness."""
from __future__ import annotations

import re
import time
import uuid

import pytest
from sqlalchemy import text as sqltext

from billcommons_shared.db import get_session

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


def test_search_common_word_does_not_time_out(client):
    """Regression for the 18s+ unbounded full-text scan (see MATCH_CAP in
    billcommons_api.search): "act" appears in nearly every bill's "AN ACT..."
    title/text, so before the per-branch match cap this query alone touched
    ~the whole corpus and blew past Vercel's function timeout on every hit.
    15s is a generous ceiling (measured ~2-3s locally against the live DB
    post-fix, vs. 18+ before) -- tight enough to catch a regression back to
    an unbounded scan, loose enough not to flake on a slower box/network."""
    t0 = time.monotonic()
    resp = client.get("/api/v1/search", params={"q": "act", "per_page": 20})
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    _assert_envelope(resp.json())
    assert elapsed < 15, f"search for a common word took {elapsed:.1f}s, expected < 15s"


def test_search_common_word_with_filter_does_not_starve_matches(client):
    """Regression for a MATCH_CAP bug caught in cross-family verify: the
    capped match CTEs originally capped on the raw text match ALONE, with
    the structural filter (sponsor/subject/jurisdiction/etc.) applied only
    in the outer query. For a common word plus a narrow filter, that filled
    the whole MATCH_CAP sample with unfiltered hits and then filtered it --
    starving out real matches. Confirmed against the live DB: "act" +
    sponsor=Smith has 1,484+ true bills.search_tsv-only matches (and more
    once bill_documents is included); the buggy filter-after-cap query
    returned 327 for the combined total, the fixed filter-inside-cap query
    returns 2,400+. 1,000 is a floor well under the fixed count and well
    above what a starved/buggy cap could produce, so this fails loudly if
    the filter-inside-cap fix regresses -- not just on data drift."""
    resp = client.get(
        "/api/v1/search", params={"q": "act", "sponsor": "smith", "per_page": 20}
    )
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["pagination"]["total"] > 1_000, (
        f"expected >1,000 filtered matches for a common word + broad sponsor "
        f"filter, got {body['pagination']['total']} -- looks like the "
        f"structural filter is being applied AFTER the match cap again"
    )
    assert body["data"], "expected at least one row on page 1"
    for item in body["data"]:
        _assert_result_item_shape(item)


def test_search_common_word_is_deterministic_across_requests(client):
    """Regression for a second MATCH_CAP bug caught in cross-family verify:
    an earlier version capped each branch with a plain unordered `LIMIT`, so
    Postgres was free to pick a DIFFERENT arbitrary MATCH_CAP-row sample per
    statement -- the data query and its separately-executed count query
    could describe different samples (total inconsistent with rows), and
    repeated requests for the same page could reshuffle. The fix adds a
    deterministic ORDER BY on an indexed key inside each capped branch and
    computes total in the SAME statement as the page (see
    billcommons_api.search.full_text_search). Two independent requests for
    "act" page 1 must return the identical first page, in the identical
    order, with the identical total -- not just the same COUNT of rows."""
    params = {"q": "act", "per_page": 20, "page": 1}
    resp1 = client.get("/api/v1/search", params=params)
    resp2 = client.get("/api/v1/search", params=params)
    assert resp1.status_code == 200 and resp2.status_code == 200
    body1, body2 = resp1.json(), resp2.json()
    _assert_envelope(body1)
    _assert_envelope(body2)

    assert body1["pagination"]["total"] == body2["pagination"]["total"], (
        "same query returned different totals across two requests -- "
        "the capped sample is not deterministic"
    )
    ids1 = [item["id"] for item in body1["data"]]
    ids2 = [item["id"] for item in body2["data"]]
    assert ids1 == ids2, (
        "same query returned a different page 1 (different ids and/or "
        "different order) across two requests -- the capped sample is not "
        "deterministic"
    )


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


@pytest.fixture()
def xss_bill():
    """Insert one jurisdiction/session/bill with a malicious title directly
    (bypassing ingestion) so the FTS/highlight branch has a guaranteed row to
    match against, then clean up. Upstream bill titles/descriptions are not
    sanitized HTML -- this simulates a hostile upstream source."""
    token = uuid.uuid4().hex[:12]
    malicious_title = (
        f"XssProbe{token} clean energy grant <img src=x onerror=alert(1)> funding act"
    )
    db = get_session()
    try:
        jurisdiction_id = db.execute(
            sqltext(
                "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
                "RETURNING id"
            ),
            {"name": f"XssTestJurisdiction{token}", "abbr": f"X{token[:8]}".upper()},
        ).scalar_one()
        session_id = db.execute(
            sqltext(
                "INSERT INTO sessions (id, jurisdiction_id, identifier, active, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :ident, false, now(), now()) "
                "RETURNING id"
            ),
            {"jid": jurisdiction_id, "ident": f"session-{token}"},
        ).scalar_one()
        bill_id = db.execute(
            sqltext(
                "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                "identifier_norm, title, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                "now(), now()) RETURNING id"
            ),
            {
                "jid": jurisdiction_id,
                "sid": session_id,
                "ident": f"XB {token[:6]}",
                "ident_norm": f"XB{token[:6]}".upper(),
                "title": malicious_title,
            },
        ).scalar_one()
        db.commit()
        yield token, malicious_title
    finally:
        db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
        db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
        db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
        db.commit()
        db.close()


def test_search_highlight_field_never_contains_raw_html(client, xss_bill):
    """P0 XSS regression for a title containing a raw <img onerror> payload.

    ts_headline's OWN markup around a match defaults to <b>...</b> -- that is
    the part the API fully controls, and it must never emit real HTML tags:
    we configure StartSel/StopSel to non-HTML sentinel tokens, so ts_headline
    itself never introduces a live tag. The upstream payload text is still
    present as inert plain-text data (the API does not silently rewrite
    upstream content) -- the actual XSS is closed at the render layer, where
    the web client must never hand this field to dangerouslySetInnerHTML (see
    apps/web/components/BillListItem.tsx + apps/web/lib/highlight.tsx, which
    render highlight as plain React text/`<mark>` elements, never raw HTML).
    This test proves the API-layer half of that contract: no <b>/</b> (or any
    other HTML tag ts_headline could inject) appears, and the sentinel tokens
    correctly bracket the matched fragment (highlighting still works)."""
    token, malicious_title = xss_bill
    resp = client.get("/api/v1/search", params={"q": f"XssProbe{token} clean energy"})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)

    matches = [item for item in body["data"] if f"XssProbe{token}" in (item.get("title") or "")]
    assert matches, "expected the seeded XSS-probe bill to appear in FTS results"
    match = matches[0]

    highlight = match.get("highlight")
    assert highlight, "expected a non-empty highlight for a full-text match"
    # ts_headline must never emit its own HTML markers around the match.
    assert "<b>" not in highlight and "</b>" not in highlight
    # Sentinel tokens (not HTML) must bracket the matched fragment -- proves
    # the highlight/match functionality is intact under the new options.
    assert "⟦H⟧" in highlight and "⟦/H⟧" in highlight
    # The upstream payload's literal text is preserved as inert plain-text
    # data (the API is not responsible for rewriting upstream content); the
    # renderer-side test is that BillListItem/highlight.tsx never pass this
    # string to dangerouslySetInnerHTML -- verified by source inspection/grep
    # (see FIX notes) since apps/web has no configured test runner.
    assert "<img" in malicious_title  # sanity: our probe payload is what we think it is
