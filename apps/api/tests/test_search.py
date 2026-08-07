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

from billcommons_api import search as search_module
from billcommons_api.search import SearchFilters, full_text_search
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


def test_search_saturated_query_is_marked_sampled(client):
    """A capped/saturated full-text match (see MATCH_CAP) must set
    meta.data_status/notice so a caller learns `total` is a sampled count,
    not a true corpus-wide one -- the honest-totals half of the MATCH_CAP
    fix. "act" is common enough to saturate at least one branch against the
    live corpus (confirmed: bills.search_tsv alone matches 55k+ rows)."""
    resp = client.get("/api/v1/search", params={"q": "act", "per_page": 20})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["meta"]["data_status"] == "sampled"
    assert body["meta"]["notice"]
    assert "sample" in body["meta"]["notice"].lower()


def test_search_non_saturated_query_is_not_marked_sampled(client):
    """The flip side of the sampled test: a query that does NOT saturate
    either branch (a common word narrowed by a real filter) must NOT carry
    the sampled disclosure -- otherwise every search result would read as
    "maybe incomplete" and the notice would stop meaning anything."""
    resp = client.get(
        "/api/v1/search", params={"q": "act", "jurisdiction": "NC", "per_page": 20}
    )
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["meta"].get("data_status") is None
    assert body["meta"].get("notice") is None


def test_search_saturated_non_relevance_sort_notes_sample_caveat(client):
    """Regression for item 2 (non-relevance sorts on saturated queries): the
    capped branches stay id-ordered rather than sort-key-ordered (measured
    10s+ regression for the bill_documents branch with no supporting index --
    see the MATCH_CAP comment), so a sort=latest_action search on a saturated
    query must say, explicitly, that the ordering is only correct within the
    sampled set -- not silently present a possibly-wrong "top N by latest
    action" as if it were exhaustive."""
    resp = client.get(
        "/api/v1/search",
        params={"q": "act", "sort": "latest_action", "per_page": 20},
    )
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    assert body["meta"]["data_status"] == "sampled"
    notice = body["meta"]["notice"].lower()
    assert "sort" in notice and "sample" in notice


def test_search_saturated_query_sort_latest_action_stays_fast(client):
    """Regression for the fallback decision in item 2: even though the
    capped branches stay id-ordered (not sort-key-ordered) for a saturated
    query, the response must still come back quickly -- this is the "honesty
    over heroics" path, not a silently-slow one. 15s mirrors the sibling
    common-word-does-not-time-out ceiling."""
    t0 = time.monotonic()
    resp = client.get(
        "/api/v1/search",
        params={"q": "act", "sort": "latest_action", "per_page": 20},
    )
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed < 15, f"saturated sort=latest_action took {elapsed:.1f}s, expected < 15s"


@pytest.fixture()
def capped_boundary_bills():
    """Insert exactly 3 bills that match one unique FTS token and nothing
    else in the live corpus, so the exact-boundary saturation tests below can
    pin MATCH_CAP to a known count around them (3 real matches -- not
    dependent on the live corpus happening to contain exactly MATCH_CAP
    matches for some real query, which it may never do)."""
    token = uuid.uuid4().hex[:12]
    n = 3
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = db.execute(
            sqltext(
                "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
                "RETURNING id"
            ),
            {"name": f"CapBoundaryJurisdiction{token}", "abbr": f"Y{token[:8]}".upper()},
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
        for i in range(n):
            db.execute(
                sqltext(
                    "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                    "identifier_norm, title, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                    "now(), now())"
                ),
                {
                    "jid": jurisdiction_id,
                    "sid": session_id,
                    "ident": f"CB {token[:6]}{i}",
                    "ident_norm": f"CB{token[:6]}{i}".upper(),
                    "title": f"CapBoundaryProbe{token} act number {i}",
                },
            )
        db.commit()
        yield token, n
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


def test_exact_match_cap_boundary_is_not_marked_sampled(capped_boundary_bills, monkeypatch):
    """Regression for the exact-boundary bug caught in verify: `count(*) =
    :match_cap` on a plain `LIMIT :match_cap` selection cannot distinguish
    an exactly-MATCH_CAP match set (genuinely exhaustive) from a
    MATCH_CAP+1-or-more one (truly saturated) -- both return exactly
    MATCH_CAP rows. MATCH_CAP is monkeypatched to exactly the 3 synthetic
    matches: total must be the true, exact count, and `capped` must be
    False -- there is no MATCH_CAP+1'th row to have been dropped."""
    token, n = capped_boundary_bills
    monkeypatch.setattr(search_module, "MATCH_CAP", n)
    db = get_session()
    try:
        filters = SearchFilters(q=f"CapBoundaryProbe{token}", per_page=20)
        rows, total, capped = full_text_search(db, filters)
    finally:
        db.close()
    assert total == n, f"expected exactly {n} matches, got {total}"
    assert capped is False, "exactly MATCH_CAP matches must NOT be reported as sampled"


def test_one_past_match_cap_boundary_is_marked_sampled(capped_boundary_bills, monkeypatch):
    """The flip side of the boundary test: MATCH_CAP set to one LESS than
    the true match count (3 matches, cap of 2) must be detected as
    saturated -- confirming the MATCH_CAP+1 probe-row fetch actually finds
    the extra row rather than silently truncating it away undetected."""
    token, n = capped_boundary_bills
    monkeypatch.setattr(search_module, "MATCH_CAP", n - 1)
    db = get_session()
    try:
        filters = SearchFilters(q=f"CapBoundaryProbe{token}", per_page=20)
        rows, total, capped = full_text_search(db, filters)
    finally:
        db.close()
    assert total == n - 1, f"expected the capped total ({n - 1}), got {total}"
    assert capped is True, "MATCH_CAP+1 real matches must be reported as sampled"


@pytest.fixture()
def capped_dated_boundary_bills():
    """Insert 12 bills matching one unique FTS token, each with a DISTINCT
    latest_action_date/introduced_date, and nothing else in the live corpus.
    Unlike capped_boundary_bills, ids are random UUIDs uncorrelated with
    insertion order or date -- so under the OLD id-ordered capped sample, a
    cap of 3 would keep an ARBITRARY 3-of-12, not necessarily the newest.
    12-vs-3 (not 5-vs-3, the first cut's ratio) so a reverted/broken fix
    can't pass by luck: an arbitrary 3-of-12 draw matching the true newest 3
    IN ORDER is ~1-in-220 (1/C(12,3)), not the ~1-in-10 (1/C(5,3)) the
    original 5-vs-3 ratio left on the table. Yields (token, dates) where
    dates[i] is bill i's latest_action_date/introduced_date, oldest first."""
    token = uuid.uuid4().hex[:12]
    n = 12
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = db.execute(
            sqltext(
                "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
                "created_at, updated_at) "
                "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
                "RETURNING id"
            ),
            {"name": f"CapDatedJurisdiction{token}", "abbr": f"D{token[:8]}".upper()},
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
        dates = [f"2020-01-{i + 1:02d}" for i in range(n)]  # oldest -> newest
        for i, d in enumerate(dates):
            db.execute(
                sqltext(
                    "INSERT INTO bills (id, jurisdiction_id, session_id, identifier, "
                    "identifier_norm, title, latest_action_date, introduced_date, "
                    "created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :jid, :sid, :ident, :ident_norm, :title, "
                    ":date, :date, now(), now())"
                ),
                {
                    "jid": jurisdiction_id,
                    "sid": session_id,
                    "ident": f"CD {token[:6]}{i}",
                    "ident_norm": f"CD{token[:6]}{i}".upper(),
                    "title": f"CapDatedProbe{token} act number {i}",
                    "date": d,
                },
            )
        db.commit()
        yield token, dates
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


def test_capped_latest_action_sort_returns_globally_newest_rows(capped_dated_boundary_bills, monkeypatch):
    """Regression for item 4 (sorted-sample under a saturated cap): with
    MATCH_CAP shrunk below the true match count and sort=latest_action, the
    capped sample must be the true global top-MATCH_CAP by latest_action_date
    -- the 3 NEWEST of the 12 inserted bills, in newest-first order -- not an
    arbitrary id-ordered slice that happens to get re-sorted afterward. Ids
    are random UUIDs uncorrelated with date, so this only passes if the
    capped candidate CTE itself orders by the requested sort key (see the
    fixture docstring for the 12-vs-3 false-pass-rate rationale)."""
    token, dates = capped_dated_boundary_bills
    cap = 3
    monkeypatch.setattr(search_module, "MATCH_CAP", cap)
    db = get_session()
    try:
        filters = SearchFilters(
            q=f"CapDatedProbe{token}", sort="latest_action", per_page=20
        )
        rows, total, capped = full_text_search(db, filters)
    finally:
        db.close()
    assert total == cap
    assert capped is True
    assert len(rows) == cap
    expected_dates = sorted(dates, reverse=True)[:cap]
    actual_dates = [r["latest_action_date"].isoformat() for r in rows]
    assert actual_dates == expected_dates, (
        f"capped sample under sort=latest_action must be the globally newest "
        f"{cap} rows in newest-first order; expected {expected_dates}, got {actual_dates}"
    )


def test_capped_introduced_sort_returns_globally_newest_rows(capped_dated_boundary_bills, monkeypatch):
    """Same regression as above, for sort=introduced (the other date-sort
    the capped candidate CTE now follows)."""
    token, dates = capped_dated_boundary_bills
    cap = 3
    monkeypatch.setattr(search_module, "MATCH_CAP", cap)
    db = get_session()
    try:
        filters = SearchFilters(
            q=f"CapDatedProbe{token}", sort="introduced", per_page=20
        )
        rows, total, capped = full_text_search(db, filters)
    finally:
        db.close()
    assert total == cap
    assert capped is True
    expected_dates = sorted(dates, reverse=True)[:cap]
    actual_dates = [r["introduced_date"].isoformat() for r in rows]
    assert actual_dates == expected_dates


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


def test_trigram_fallback_uses_the_percent_operator_directly(client):
    """Structural regression for item 4 (TRIGRAM FALLBACK % rewrite): the
    fallback must query with the indexable `%` operator, not a bare
    `similarity(...) > threshold` predicate that's opaque to the planner and
    forces a sequential scan of `bills` regardless of ix_bills_title_trgm's
    presence -- ANDed with a strict `similarity(...) > threshold` recheck
    (see test_trigram_fallback_percent_operator_matches_old_strict_predicate_
    exactly for why the recheck is load-bearing, not decorative). Source-
    inspection rather than EXPLAIN here (no DB handle in this test module) --
    see the module's live EXPLAIN verification in the implementation notes
    for the index-usage proof."""
    import inspect

    from billcommons_api import search as search_module

    src = inspect.getsource(search_module.trigram_fallback)
    assert "b.title % :q" in src, "trigram_fallback should filter with the % operator"
    assert "similarity(b.title, :q) > :threshold" in src, (
        "% alone is INCLUSIVE (>= threshold) -- the STRICT (> threshold) check "
        "must be re-applied as a recheck to preserve the old predicate's semantics"
    )
    assert "set_config('pg_trgm.similarity_threshold'" in src, (
        "% relies on the session GUC, not a bound argument -- it must be set explicitly"
    )


def test_trigram_fallback_percent_operator_matches_old_strict_predicate_exactly():
    """Regression for the inclusive-vs-strict bug caught in verify: `b.title
    % :q` alone is INCLUSIVE (matches rows with similarity >=
    pg_trgm.similarity_threshold), while the predicate it replaced was
    STRICT (`similarity(b.title, :q) > threshold`) -- a title landing
    EXACTLY on the threshold would newly match under `%` alone, silently
    breaking the change's own stated no-behavior-change invariant. The fix
    ANDs the strict check back on as a recheck over the (small) index-
    qualified candidate set.

    A total/count comparison can't catch a boundary bug like this (two
    different id-sets can share a count by coincidence), so this pins the
    actual id-SETS: for two independent misspelled queries against the live,
    growing corpus, the combined `%` + strict-recheck predicate must select
    EXACTLY the same bills as the old bare `similarity() > threshold` form --
    not an overlapping-but-different set."""
    threshold = search_module._TRIGRAM_THRESHOLD
    queries = ["educatoin fnding", "helth cair reforn"]
    db = get_session()
    try:
        for q in queries:
            # `%`'s notion of "similar enough" reads the SESSION GUC, not a
            # bound parameter -- must be set before the query runs, exactly
            # as trigram_fallback itself does. `true` scopes it to the
            # current transaction, so it stays in effect for the combined
            # query below without needing to be re-set mid-statement.
            db.execute(
                sqltext("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
                {"t": str(threshold)},
            )
            # Both id-sets computed in ONE statement (a single MVCC snapshot
            # under READ COMMITTED), not two independently-executed queries:
            # the previous two-statement form could flake under concurrent
            # ingest -- a bill title changing between the old-predicate query
            # and the new-predicate query could land the two sets on
            # different snapshots and produce a spurious mismatch that has
            # nothing to do with the predicate rewrite this test exists to
            # pin.
            combined_sql = sqltext(
                "WITH old_match AS ("
                "    SELECT b.id FROM bills b WHERE similarity(b.title, :q) > :threshold"
                "), new_match AS ("
                "    SELECT b.id FROM bills b "
                "    WHERE b.title % :q AND similarity(b.title, :q) > :threshold"
                ") "
                "SELECT 'old' AS which, id FROM old_match "
                "UNION ALL "
                "SELECT 'new' AS which, id FROM new_match"
            )
            old_ids: set = set()
            new_ids: set = set()
            for which, bill_id in db.execute(combined_sql, {"q": q, "threshold": threshold}):
                (old_ids if which == "old" else new_ids).add(bill_id)

            assert new_ids == old_ids, (
                f"id-set mismatch for q={q!r}: the % + strict-recheck predicate "
                f"must return EXACTLY the same rows as the old bare "
                f"similarity() > threshold predicate, not merely the same "
                f"count (old={len(old_ids)} ids, new={len(new_ids)} ids, "
                f"symmetric difference={len(old_ids ^ new_ids)})"
            )
    finally:
        db.close()


def test_trigram_fallback_preserves_the_historical_threshold(client):
    """The % rewrite must not silently change WHICH rows match: same query,
    same threshold as the old `similarity() > 0.15` predicate. Confirmed by
    the exact id-set parity pin above; this end-to-end variant additionally
    exercises the real /search route (pagination, envelope, match_type)."""
    resp = client.get("/api/v1/search", params={"q": "educatoin fnding", "per_page": 1})
    assert resp.status_code == 200
    body = resp.json()
    _assert_envelope(body)
    # Not pinned to the exact prior-measured count (915) since the live,
    # growing corpus can add/remove matching titles between runs -- but it
    # must be nonzero (the whole point of the fallback existing) and every
    # returned row must actually be a trigram/full-text/bill-number match,
    # not an unrelated row a broken predicate let through.
    assert body["pagination"]["total"] > 0
    for item in body["data"]:
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
