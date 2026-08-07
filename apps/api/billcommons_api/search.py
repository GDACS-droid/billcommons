"""Search query builder for GET /api/v1/search.

Strategy (per BRIEF-wave2.md "api" section):
1. Bill-number fast path: if `q` normalizes cleanly via
   billcommons_shared.normalize.normalize_bill_number, look up bills by
   identifier_norm (exact + prefix) first -- this is the "HB 123"/"HB123"/
   "H.B. 123" case and always wins when it matches.
2. Otherwise, full-text search: websearch_to_tsquery('english', q) over
   bills.search_tsv, UNION'd with matches against bill_documents.text_tsv
   (full text of attached documents), ranked with ts_rank and highlighted
   with ts_headline.
3. Trigram fallback: if the FTS branch returns nothing, fall back to a
   pg_trgm similarity match on bills.title for fuzzy title search.

All three branches are combined with the same structural filters
(jurisdiction, session, chamber, status, sponsor, subject, committee,
date range) applied as a WHERE clause on the underlying bills query, and the
same pagination/sort semantics. Deterministic lexical retrieval only --
no embeddings in v1.

Security note (highlight field): source titles/descriptions/document text
come from upstream jurisdictions and are not sanitized HTML -- they must be
treated as plain text. `highlight` is therefore PLAIN TEXT with a pair of
non-HTML sentinel tokens (HIGHLIGHT_START_SENTINEL / HIGHLIGHT_STOP_SENTINEL)
wrapping each matched fragment, produced via ts_headline's StartSel/StopSel
options. It contains no HTML and must never be rendered with
dangerouslySetInnerHTML; clients split on the sentinels and render their own
<mark> (or equivalent) around the wrapped segments.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from billcommons_shared.normalize import normalize_bill_number

VALID_SORTS = {"relevance", "latest_action", "introduced", "jurisdiction"}

# Sentinel tokens used to mark matched fragments inside the plain-text
# `highlight` field returned by the FTS branch (see module docstring
# "Security note"). These are unlikely-to-occur plain-text tokens -- never
# HTML -- so the field can never carry a script/markup payload from
# externally sourced bill titles/descriptions/document text.
HIGHLIGHT_START_SENTINEL = "⟦H⟧"  # ⟦H⟧
HIGHLIGHT_STOP_SENTINEL = "⟦/H⟧"  # ⟦/H⟧
_TS_HEADLINE_OPTIONS = (
    "MaxFragments=1, MinWords=5, MaxWords=25, "
    f"StartSel={HIGHLIGHT_START_SENTINEL}, StopSel={HIGHLIGHT_STOP_SENTINEL}"
)

# Per-branch cap on raw full-text matches, taken BEFORE ts_rank/GROUP BY.
#
# A common English word ("act" appears in nearly every bill's "AN ACT...")
# matches on the order of the entire corpus. Measured against the live prod
# document table: capping via `ORDER BY rank DESC LIMIT` still cost ~10s,
# because Postgres must compute ts_rank (detoasting a tsvector) for every one
# of ~490k matching rows before it can sort and take the top N. Taking the
# LIMIT on a cheap, INDEXED, deterministic order instead -- `ORDER BY b.id`
# for the bills branch, `ORDER BY bv.bill_id, bd.id` for the bill_documents
# branch -- lets the planner stop scanning early (measured ~0.4-1.9s per
# branch, still a >5x cut) while making the sample stable: the same query
# with the same params always selects the same MATCH_CAP rows, so the data
# page, its total, and repeated requests for the same page all agree. (An
# earlier version used a plain unordered LIMIT -- ~0.1-1.7s, faster still --
# but Postgres is then free to pick a DIFFERENT arbitrary MATCH_CAP subset
# per statement/request: the result page and its total could describe
# different samples, and page reloads could reshuffle. Caught in cross-family
# verify; the ORDER BY cost was worth paying for correctness.)
#
# Tradeoff: for a term this common, the capped sample is not guaranteed to
# contain the globally highest-ranked matches (it's "the first MATCH_CAP rows
# by bill id," not "top MATCH_CAP by rank"), so relevance ordering among
# results is only correct within the sample, and `total` (see
# full_text_search below) reports the capped count, not a true corpus-wide
# count. For the overwhelming majority of real queries (anything that isn't
# a near-universal word) the match set is already well under MATCH_CAP and
# this has no effect at all.
#
# The bill_documents branch caps on document ROWS, not distinct bills -- a
# bill with many matching document versions can consume more than one slot
# of the cap, other bills' documents unseen. Capping on DISTINCT bill_id
# instead (dedupe-then-limit) was measured at ~2.7s, a real latency cost for
# a correctness edge case with no observed prod impact; documented here as a
# known limitation rather than implemented.
#
# The structural filters (jurisdiction/session/chamber/status/sponsor/
# subject/committee/date -- see _filter_clause) are applied INSIDE each
# capped branch, not just in the outer query, on purpose: capping on the raw
# text match alone and filtering afterward would fill the whole MATCH_CAP
# sample with unfiltered hits, then filter it down -- for a common word plus
# a narrow filter (e.g. q=act, sponsor=Smith) that can starve real, existing
# filtered matches out of the sample entirely and return a false near-empty
# result. The capped branches also unconditionally INNER JOIN jurisdictions/
# sessions (needed for where_sql, which can reference j./s. columns even when
# no filter is set): bills.jurisdiction_id/session_id are NOT NULL with
# enforced FK constraints, and prod has zero NULL or orphaned rows (verified
# directly against the live DB), so this join cannot silently drop a bill.
#
# Non-relevance sort on a saturated query (see full_text_saturation_notice
# below): the obviously-more-correct fix would ORDER BY the requested sort
# key (e.g. b.latest_action_date DESC) INSIDE these capped subqueries too,
# so the cap itself keeps the globally correct top-N-by-sort instead of an
# arbitrary id-ordered slice that's merely re-sorted afterward. Measured
# against the live corpus for q=act&sort=latest_action: the bills-only
# branch stays cheap (~0.2s, the GIN bitmap scan + top-N heapsort handles
# it fine), but the bill_documents branch costs 10s+, because nothing
# indexes (bill_documents match) x (bills.latest_action_date) together --
# Postgres must seq-scan and sort the ~490k-row raw match set before the
# LIMIT can apply. That's well past what a request should cost, with no
# cheap indexed alternative available in this batch, so the cap stays
# id-ordered for both branches; full_text_saturation_notice says so
# explicitly instead. Honesty over heroics.
MATCH_CAP = 5_000


def full_text_saturation_notice(sort: str) -> str:
    """ResponseMeta notice for a full-text search whose match set was
    saturated at MATCH_CAP (see above) in at least one branch.

    Always true regardless of sort: `total` on a saturated query is the
    sampled/deduped count, not a true corpus-wide count, and an exact-looking
    number like "8,432 results" invites a caller to treat it as one anyway.

    When `sort` isn't "relevance", a second and sharper caveat applies: the
    capped branches are id-ordered, not sort-key-ordered (see the MATCH_CAP
    comment for why), so a non-relevance sort is only correct WITHIN the
    id-ordered sample -- it is not guaranteed to surface the true top results
    by that sort across the full corpus. Narrowing the query is the honest
    way to get a globally accurate sort order back.
    """
    notice = (
        f"This query matched more than {MATCH_CAP:,} bills in at least one "
        "search branch. Results are a deterministic sample of the match set, "
        "not the full corpus: the total above is the sampled/deduplicated "
        "count, not a true corpus-wide total, and pagination stops at the "
        "end of that sample."
    )
    if sort != "relevance":
        notice += (
            f" Because the match set is sampled, sorting by {sort!r} is only "
            "correct within that sample -- it is not guaranteed to be the "
            "true top results by this sort across the full corpus. Narrow "
            "the query (jurisdiction, session, date range, sponsor, etc.) "
            "for a globally accurate sort order."
        )
    return notice


@dataclass
class SearchFilters:
    q: str | None = None
    jurisdiction: str | None = None  # jurisdiction abbreviation
    session: str | None = None  # session identifier
    chamber: str | None = None
    status: str | None = None
    sponsor: str | None = None
    subject: str | None = None
    committee: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    sort: str = "relevance"
    page: int = 1
    per_page: int = 25


def _filter_clause(f: SearchFilters) -> tuple[str, dict]:
    """Build the shared structural WHERE fragment + params, applied on top
    of `bills b` joined to `jurisdictions j` and `sessions s`."""
    clauses: list[str] = []
    params: dict = {}

    if f.jurisdiction:
        clauses.append("j.abbreviation = :jurisdiction")
        params["jurisdiction"] = f.jurisdiction.upper()
    if f.session:
        clauses.append("s.identifier = :session")
        params["session"] = f.session
    if f.chamber:
        clauses.append("b.chamber = :chamber")
        params["chamber"] = f.chamber
    if f.status:
        clauses.append("b.status = :status")
        params["status"] = f.status
    if f.date_from:
        clauses.append("b.latest_action_date >= :date_from")
        params["date_from"] = f.date_from
    if f.date_to:
        clauses.append("b.latest_action_date <= :date_to")
        params["date_to"] = f.date_to
    if f.sponsor:
        clauses.append(
            "EXISTS (SELECT 1 FROM sponsorships sp WHERE sp.bill_id = b.id "
            "AND sp.name ILIKE :sponsor)"
        )
        params["sponsor"] = f"%{f.sponsor}%"
    if f.subject:
        clauses.append(
            "EXISTS (SELECT 1 FROM bill_subjects bs WHERE bs.bill_id = b.id "
            "AND bs.subject ILIKE :subject)"
        )
        params["subject"] = f"%{f.subject}%"
    if f.committee:
        clauses.append(
            "EXISTS (SELECT 1 FROM bill_actions ba JOIN organizations o "
            "ON o.id = ba.organization_id WHERE ba.bill_id = b.id "
            "AND o.name ILIKE :committee)"
        )
        params["committee"] = f"%{f.committee}%"

    where = " AND ".join(clauses)
    return where, params


def _order_by(sort: str) -> str:
    if sort == "latest_action":
        return "b.latest_action_date DESC NULLS LAST, b.id"
    if sort == "introduced":
        return "b.introduced_date DESC NULLS LAST, b.id"
    if sort == "jurisdiction":
        return "j.abbreviation ASC, b.identifier_norm ASC"
    return "rank DESC, b.id"  # relevance (default)


_BASE_SELECT = """
    SELECT b.id, b.jurisdiction_id, b.session_id, b.chamber, b.identifier,
           b.identifier_norm, b.title, b.short_title, b.bill_type, b.status,
           b.status_date, b.introduced_date, b.latest_action_text,
           b.latest_action_date, b.source_url
    FROM bills b
    JOIN jurisdictions j ON j.id = b.jurisdiction_id
    JOIN sessions s ON s.id = b.session_id
"""


def bill_number_lookup(db: Session, f: SearchFilters) -> tuple[list[dict], int] | None:
    """Bill-number fast path. Returns None if `q` doesn't parse as a bill
    number (caller should fall through to full-text search), else
    (matching rows, total count) -- rows may be an empty list."""
    if not f.q:
        return None
    try:
        norm = normalize_bill_number(f.q)
    except ValueError:
        return None

    where, params = _filter_clause(f)
    where_sql = f" AND {where}" if where else ""
    params["norm"] = norm
    sql = text(
        f"{_BASE_SELECT} WHERE b.identifier_norm = :norm{where_sql} "
        f"ORDER BY {_order_by('jurisdiction' if f.sort == 'relevance' else f.sort)} "
        "LIMIT :limit OFFSET :offset"
    )
    count_sql = text(
        f"SELECT count(*) FROM bills b JOIN jurisdictions j ON j.id = b.jurisdiction_id "
        f"JOIN sessions s ON s.id = b.session_id WHERE b.identifier_norm = :norm{where_sql}"
    )
    count = db.execute(count_sql, params).scalar_one()
    offset = (f.page - 1) * f.per_page
    rows = db.execute(sql, {**params, "limit": f.per_page, "offset": offset}).mappings().all()
    return [dict(r) | {"match_type": "bill_number", "rank": None, "highlight": None} for r in rows], count


def full_text_search(db: Session, f: SearchFilters) -> tuple[list[dict], int, bool]:
    """websearch_to_tsquery over bills.search_tsv, UNIONed with matches in
    bill_documents.text_tsv (mapped back to their parent bill), ranked.

    Returns (rows, total, capped) -- `capped` is True when either branch hit
    MATCH_CAP, i.e. the result is a sample rather than an exhaustive match
    (see full_text_saturation_notice, wired into the response by the router)."""
    where, params = _filter_clause(f)
    where_sql = f" AND {where}" if where else ""
    params["q"] = f.q or ""
    params["headline_options"] = _TS_HEADLINE_OPTIONS
    params["match_cap"] = MATCH_CAP
    # Fetch one extra row past MATCH_CAP so saturation is DETECTED, not
    # inferred from a count that equals the cap by coincidence. `count(*) =
    # :match_cap` on a plain `LIMIT :match_cap` selection cannot tell an
    # exactly-MATCH_CAP match set (genuinely exhaustive, `total` IS exact)
    # from a MATCH_CAP+1-or-more one (truly saturated) -- both return
    # exactly MATCH_CAP rows. Selecting MATCH_CAP+1 and testing `> :match_cap`
    # resolves the boundary; the extra probe row is trimmed back off before
    # it reaches ranking/pagination (see bills_matches/docs_matches below).
    params["match_cap_plus_one"] = MATCH_CAP + 1

    order = _order_by(f.sort)

    # ts_headline is computed ONLY for the page being returned, never inside
    # the match CTE. The original query headlined the FULL extracted text of
    # every matching document before the LIMIT -- for a common word ("act"
    # appears in essentially every bill) that is a quarter-million headline
    # calls to return one row, and it blew the statement timeout in prod the
    # week the text corpus reached 126k bills. Rank is cheap; headline is not.
    #
    # Single statement, no separate count query: `ranked_total` counts the
    # already-small, already-filtered/capped `ranked` set once (cheap -- see
    # MATCH_CAP), and is cross-joined ahead of `page` so it's present even
    # when the requested page is past the end (`page` returns zero rows, but
    # `ranked_total rt LEFT JOIN page p` still returns exactly one row, with
    # every p.* column NULL). This replaces two independently-executed
    # queries (a real inconsistency risk even with a deterministic ORDER BY,
    # if a write lands between them) with one query against one MVCC
    # snapshot -- rows and total are now guaranteed to describe the same
    # underlying capped sample, in this request and (given the deterministic
    # ORDER BY on `matches` above) across repeated requests too.
    sql = text(f"""
        WITH bills_matches_probe AS MATERIALIZED (
            -- MATCH_CAP+1, not MATCH_CAP: see the match_cap_plus_one comment
            -- above. This CTE is the exact-boundary fix -- it is deliberately
            -- ONE ROW LARGER than the sample that actually feeds ranking.
            SELECT b.id AS bill_id,
                   ts_rank(b.search_tsv, websearch_to_tsquery('english', :q)) AS rank
            FROM bills b
            JOIN jurisdictions j ON j.id = b.jurisdiction_id
            JOIN sessions s ON s.id = b.session_id
            WHERE b.search_tsv @@ websearch_to_tsquery('english', :q){where_sql}
            ORDER BY b.id
            LIMIT :match_cap_plus_one
        ),
        bills_matches AS (
            -- The actual sample: the probe row past MATCH_CAP (if any) is
            -- trimmed back off here, so it can never reach ranking/pagination
            -- -- it exists only to answer "was there a MATCH_CAP+1'th row".
            SELECT bill_id, rank FROM bills_matches_probe
            ORDER BY bill_id
            LIMIT :match_cap
        ),
        docs_matches_probe AS MATERIALIZED (
            -- DISTINCT-BILL cap: dedupe document-row matches down to one row
            -- per bill_id BEFORE the LIMIT, so a bill with many matching
            -- document versions consumes exactly one slot of MATCH_CAP
            -- instead of crowding OTHER bills' documents out of the sample
            -- (the raw per-document cap this replaced could do that -- a
            -- bill with 40 matching amendment versions used to eat 40 slots
            -- other bills never got a chance at). This costs a real
            -- aggregate over the full, uncapped matching-document set before
            -- the LIMIT can apply -- nothing indexes (matched document) x
            -- (bill_id) together, so there's no cheap early-stopping
            -- alternative -- but it's a bounded, one-time cost per request
            -- (measured ~0.5-2.7s against the live corpus for a saturating
            -- term) for a real correctness fix, not a hot loop. MATCH_CAP+1
            -- here for the same exact-boundary reason as bills_matches_probe.
            SELECT bill_id, max(rank) AS rank
            FROM (
                SELECT bv.bill_id AS bill_id,
                       ts_rank(bd.text_tsv, websearch_to_tsquery('english', :q)) AS rank
                FROM bill_documents bd
                JOIN bill_versions bv ON bv.id = bd.bill_version_id
                JOIN bills b ON b.id = bv.bill_id
                JOIN jurisdictions j ON j.id = b.jurisdiction_id
                JOIN sessions s ON s.id = b.session_id
                WHERE bd.text_tsv @@ websearch_to_tsquery('english', :q){where_sql}
            ) doc_matches
            GROUP BY bill_id
            ORDER BY bill_id
            LIMIT :match_cap_plus_one
        ),
        docs_matches AS (
            -- Trim the probe row -- see bills_matches.
            SELECT bill_id, rank FROM docs_matches_probe
            ORDER BY bill_id
            LIMIT :match_cap
        ),
        matches AS (
            SELECT bill_id, rank FROM bills_matches
            UNION ALL
            SELECT bill_id, rank FROM docs_matches
        ),
        cap_status AS (
            -- Test against the PROBE CTEs (MATCH_CAP+1 rows each), not the
            -- trimmed bills_matches/docs_matches (always <= MATCH_CAP rows,
            -- so `count(*) = :match_cap` on those could never distinguish
            -- "exactly MATCH_CAP matches" from "MATCH_CAP+1 or more" -- both
            -- look identical after trimming). `> :match_cap` on the probe's
            -- untrimmed count is the actual saturation test: it's only true
            -- when a MATCH_CAP+1'th row existed to fetch.
            -- Still cheap: the probe CTEs are MATERIALIZED and already
            -- capped at MATCH_CAP+1 rows each above, so these counts are
            -- over at-most-(MATCH_CAP+1) rows, not the underlying (possibly
            -- corpus-sized) match set.
            SELECT (SELECT count(*) FROM bills_matches_probe) > :match_cap AS bills_capped,
                   (SELECT count(*) FROM docs_matches_probe) > :match_cap AS docs_capped
        ),
        ranked AS (
            SELECT bill_id, max(rank) AS rank
            FROM matches
            GROUP BY bill_id
        ),
        ranked_total AS (
            SELECT count(*) AS total_count FROM ranked
        ),
        page AS (
            SELECT b.id, b.jurisdiction_id, b.session_id, b.chamber, b.identifier,
                   b.identifier_norm, b.title, b.short_title, b.bill_type, b.status,
                   b.status_date, b.introduced_date, b.latest_action_text,
                   b.latest_action_date, b.source_url, r.rank,
                   row_number() OVER (ORDER BY {order}) AS ord
            FROM ranked r
            JOIN bills b ON b.id = r.bill_id
            JOIN jurisdictions j ON j.id = b.jurisdiction_id
            JOIN sessions s ON s.id = b.session_id
            WHERE 1=1{where_sql}
            ORDER BY {order}
            LIMIT :limit OFFSET :offset
        )
        SELECT p.id, p.jurisdiction_id, p.session_id, p.chamber, p.identifier,
               p.identifier_norm, p.title, p.short_title, p.bill_type, p.status,
               p.status_date, p.introduced_date, p.latest_action_text,
               p.latest_action_date, p.source_url, p.rank, hl.highlight,
               rt.total_count, cs.bills_capped, cs.docs_capped
        FROM ranked_total rt
        CROSS JOIN cap_status cs
        LEFT JOIN page p ON true
        LEFT JOIN LATERAL (
            -- One highlight per bill: prefer the title/description snippet
            -- (what the old max(highlight) usually surfaced), fall back to
            -- the best-ranked matching document's text. p.id is NULL on the
            -- single placeholder row of an empty page (see above) -- every
            -- correlated condition below then compares against NULL and
            -- matches nothing, so this LATERAL correctly no-ops (highlight
            -- stays NULL) instead of erroring.
            SELECT h.highlight
            FROM (
                SELECT 0 AS pri,
                       ts_headline('english',
                                   coalesce(b2.title, '') || ' ' || coalesce(b2.description, ''),
                                   websearch_to_tsquery('english', :q),
                                   :headline_options) AS highlight
                FROM bills b2
                WHERE b2.id = p.id
                  AND b2.search_tsv @@ websearch_to_tsquery('english', :q)
                UNION ALL
                SELECT 1 AS pri,
                       ts_headline('english', best_doc.extracted_text,
                                   websearch_to_tsquery('english', :q),
                                   :headline_options) AS highlight
                FROM (
                    SELECT bd.extracted_text
                    FROM bill_documents bd
                    JOIN bill_versions bv ON bv.id = bd.bill_version_id
                    WHERE bv.bill_id = p.id
                      AND bd.text_tsv @@ websearch_to_tsquery('english', :q)
                    ORDER BY ts_rank(bd.text_tsv, websearch_to_tsquery('english', :q)) DESC
                    LIMIT 1
                ) best_doc
            ) h
            ORDER BY h.pri
            LIMIT 1
        ) hl ON true
        ORDER BY p.ord
    """)

    offset = (f.page - 1) * f.per_page
    raw_rows = db.execute(sql, {**params, "limit": f.per_page, "offset": offset}).mappings().all()
    # Always exactly one row minimum (the ranked_total/cap_status cross
    # joins): p.id is NULL on that row when the page is empty (zero real
    # matches at this offset, e.g. a page past the end of the capped/filtered
    # result set).
    total = raw_rows[0]["total_count"] if raw_rows else 0
    capped = bool(raw_rows[0]["bills_capped"] or raw_rows[0]["docs_capped"]) if raw_rows else False
    rows = [
        {k: v for k, v in dict(r).items() if k not in ("total_count", "bills_capped", "docs_capped")}
        | {"match_type": "full_text"}
        for r in raw_rows
        if r["id"] is not None
    ]
    return rows, total, capped


# Trigram similarity threshold shared by the `%` operator (below) and the
# `similarity()` call used only for ORDER BY. Kept as one named constant so
# the two can never drift apart -- see trigram_fallback for why they must
# agree.
_TRIGRAM_THRESHOLD = 0.15


def trigram_fallback(db: Session, f: SearchFilters) -> tuple[list[dict], int]:
    """Fuzzy title search via pg_trgm similarity, used only when the
    full-text branch returns zero rows.

    Predicate is the indexable `%` operator (not a bare `similarity(...) >
    threshold` function call) ANDed with the original strict `similarity(...)
    > threshold` check as a recheck. `%` is an operator pg_trgm's GIN opclass
    (ix_bills_title_trgm) actually knows how to accelerate -- a bare
    `similarity()` call is opaque to the planner and forces a sequential scan
    regardless of the index's presence -- but `%` alone is INCLUSIVE (>=
    threshold, per the GUC below) while the predicate it replaced was STRICT
    (> threshold), so `%` is used only to get the index-qualified candidate
    rows and the strict `similarity() > threshold` recheck is re-applied on
    top of it to keep the matched set identical to the old behavior; the
    recheck only runs over the (small) index-qualified row set, so the perf
    win survives. `%`'s notion of "similar enough" is controlled by the
    SESSION GUC `pg_trgm.similarity_threshold` (default 0.3), not by an
    operator argument, so it has to be set explicitly to match this
    endpoint's historical 0.15 threshold -- `set_config(..., true)` scopes
    that to the current transaction only, so it can never leak into an
    unrelated request sharing the same pooled connection. Verified via
    EXPLAIN against the live DB that the query still uses ix_bills_title_trgm
    (Bitmap Index Scan) with the recheck applied, and that the resulting
    id-set is identical to the old `similarity() > 0.15` form for the same
    query -- this is an index-usability fix, not a behavior change.
    """
    where, params = _filter_clause(f)
    where_sql = f" AND {where}" if where else ""
    params["q"] = f.q or ""
    params["threshold_str"] = str(_TRIGRAM_THRESHOLD)
    params["threshold"] = _TRIGRAM_THRESHOLD

    set_threshold_sql = text(
        "SELECT set_config('pg_trgm.similarity_threshold', :threshold_str, true)"
    )
    # `b.title % :q` alone is INCLUSIVE (>= threshold, per pg_trgm.similarity_
    # threshold), while the predicate it replaced was STRICT (similarity(...)
    # > threshold) -- a title landing exactly on the threshold would newly
    # match. `%` still does the work of getting ix_bills_title_trgm's Bitmap
    # Index Scan into the plan (that's the whole point of using it), but the
    # strict boundary is re-applied as a recheck on top of it so the matched
    # set is unchanged from the old behavior; the recheck only runs over the
    # (small) index-qualified row set, so the perf win survives.
    sql = text(f"""
        SELECT b.id, b.jurisdiction_id, b.session_id, b.chamber, b.identifier,
               b.identifier_norm, b.title, b.short_title, b.bill_type, b.status,
               b.status_date, b.introduced_date, b.latest_action_text,
               b.latest_action_date, b.source_url, similarity(b.title, :q) AS rank
        FROM bills b
        JOIN jurisdictions j ON j.id = b.jurisdiction_id
        JOIN sessions s ON s.id = b.session_id
        WHERE b.title % :q AND similarity(b.title, :q) > :threshold{where_sql}
        ORDER BY rank DESC, b.id
        LIMIT :limit OFFSET :offset
    """)
    count_sql = text(f"""
        SELECT count(*) FROM bills b
        JOIN jurisdictions j ON j.id = b.jurisdiction_id
        JOIN sessions s ON s.id = b.session_id
        WHERE b.title % :q AND similarity(b.title, :q) > :threshold{where_sql}
    """)

    db.execute(set_threshold_sql, params)
    count = db.execute(count_sql, params).scalar_one()
    offset = (f.page - 1) * f.per_page
    rows = db.execute(sql, {**params, "limit": f.per_page, "offset": offset}).mappings().all()
    return [
        dict(r) | {"match_type": "fuzzy_title", "highlight": None} for r in rows
    ], count


def run_search(db: Session, f: SearchFilters) -> tuple[list[dict], int, bool]:
    """Run the tiered search strategy and return (rows, total_count, capped).

    `capped` (see full_text_search/MATCH_CAP) is only ever True for the
    full-text branch -- the router uses it to attach the honest-totals
    ResponseMeta notice."""
    if f.q:
        fast_path = bill_number_lookup(db, f)
        if fast_path is not None:
            rows, count = fast_path
            if rows or count:
                return rows, count, False

        rows, count, capped = full_text_search(db, f)
        # `rows or count` is also, deliberately, the trigram-skip condition
        # for a saturated query: `capped` can only be True when the branch
        # hit MATCH_CAP, which requires MATCH_CAP real matches to exist, so
        # `capped` implies `count > 0` and this branch is always taken --
        # the trigram fallback below is reached only on a genuinely empty
        # full-text result, never as a "there might be more/better matches"
        # second opinion on a saturated one.
        if rows or count:
            return rows, count, capped

        return (*trigram_fallback(db, f), False)

    # No query text: browse mode, filters only, over the base bills table.
    where, params = _filter_clause(f)
    where_sql = f" WHERE {where}" if where else ""
    order = _order_by(f.sort if f.sort != "relevance" else "latest_action")
    sql = text(f"{_BASE_SELECT}{where_sql} ORDER BY {order} LIMIT :limit OFFSET :offset")
    count_sql = text(
        f"SELECT count(*) FROM bills b JOIN jurisdictions j ON j.id = b.jurisdiction_id "
        f"JOIN sessions s ON s.id = b.session_id{where_sql}"
    )
    count = db.execute(count_sql, params).scalar_one()
    offset = (f.page - 1) * f.per_page
    rows = db.execute(sql, {**params, "limit": f.per_page, "offset": offset}).mappings().all()
    return [
        dict(r) | {"match_type": "browse", "rank": None, "highlight": None} for r in rows
    ], count, False
