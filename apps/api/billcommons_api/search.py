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


def full_text_search(db: Session, f: SearchFilters) -> tuple[list[dict], int]:
    """websearch_to_tsquery over bills.search_tsv, UNIONed with matches in
    bill_documents.text_tsv (mapped back to their parent bill), ranked."""
    where, params = _filter_clause(f)
    where_sql = f" AND {where}" if where else ""
    params["q"] = f.q or ""
    params["headline_options"] = _TS_HEADLINE_OPTIONS

    order = _order_by(f.sort)

    # ts_headline is computed ONLY for the page being returned, never inside
    # the match CTE. The original query headlined the FULL extracted text of
    # every matching document before the LIMIT -- for a common word ("act"
    # appears in essentially every bill) that is a quarter-million headline
    # calls to return one row, and it blew the statement timeout in prod the
    # week the text corpus reached 126k bills. Rank is cheap; headline is not.
    sql = text(f"""
        WITH matches AS (
            SELECT b.id AS bill_id,
                   ts_rank(b.search_tsv, websearch_to_tsquery('english', :q)) AS rank
            FROM bills b
            WHERE b.search_tsv @@ websearch_to_tsquery('english', :q)
            UNION ALL
            SELECT bv.bill_id AS bill_id,
                   ts_rank(bd.text_tsv, websearch_to_tsquery('english', :q)) AS rank
            FROM bill_documents bd
            JOIN bill_versions bv ON bv.id = bd.bill_version_id
            WHERE bd.text_tsv @@ websearch_to_tsquery('english', :q)
        ),
        ranked AS (
            SELECT bill_id, max(rank) AS rank
            FROM matches
            GROUP BY bill_id
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
               p.latest_action_date, p.source_url, p.rank, hl.highlight
        FROM page p
        LEFT JOIN LATERAL (
            -- One highlight per bill: prefer the title/description snippet
            -- (what the old max(highlight) usually surfaced), fall back to
            -- the best-ranked matching document's text.
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

    count_sql = text(f"""
        WITH matches AS (
            SELECT b.id AS bill_id
            FROM bills b
            WHERE b.search_tsv @@ websearch_to_tsquery('english', :q)
            UNION
            SELECT bv.bill_id AS bill_id
            FROM bill_documents bd
            JOIN bill_versions bv ON bv.id = bd.bill_version_id
            WHERE bd.text_tsv @@ websearch_to_tsquery('english', :q)
        )
        SELECT count(*)
        FROM matches m
        JOIN bills b ON b.id = m.bill_id
        JOIN jurisdictions j ON j.id = b.jurisdiction_id
        JOIN sessions s ON s.id = b.session_id
        WHERE 1=1{where_sql}
    """)

    count = db.execute(count_sql, params).scalar_one()
    offset = (f.page - 1) * f.per_page
    rows = db.execute(sql, {**params, "limit": f.per_page, "offset": offset}).mappings().all()
    return [dict(r) | {"match_type": "full_text"} for r in rows], count


def trigram_fallback(db: Session, f: SearchFilters) -> tuple[list[dict], int]:
    """Fuzzy title search via pg_trgm similarity, used only when the
    full-text branch returns zero rows."""
    where, params = _filter_clause(f)
    where_sql = f" AND {where}" if where else ""
    params["q"] = f.q or ""
    params["threshold"] = 0.15

    sql = text(f"""
        SELECT b.id, b.jurisdiction_id, b.session_id, b.chamber, b.identifier,
               b.identifier_norm, b.title, b.short_title, b.bill_type, b.status,
               b.status_date, b.introduced_date, b.latest_action_text,
               b.latest_action_date, b.source_url, similarity(b.title, :q) AS rank
        FROM bills b
        JOIN jurisdictions j ON j.id = b.jurisdiction_id
        JOIN sessions s ON s.id = b.session_id
        WHERE similarity(b.title, :q) > :threshold{where_sql}
        ORDER BY rank DESC, b.id
        LIMIT :limit OFFSET :offset
    """)
    count_sql = text(f"""
        SELECT count(*) FROM bills b
        JOIN jurisdictions j ON j.id = b.jurisdiction_id
        JOIN sessions s ON s.id = b.session_id
        WHERE similarity(b.title, :q) > :threshold{where_sql}
    """)

    count = db.execute(count_sql, params).scalar_one()
    offset = (f.page - 1) * f.per_page
    rows = db.execute(sql, {**params, "limit": f.per_page, "offset": offset}).mappings().all()
    return [
        dict(r) | {"match_type": "fuzzy_title", "highlight": None} for r in rows
    ], count


def run_search(db: Session, f: SearchFilters) -> tuple[list[dict], int]:
    """Run the tiered search strategy and return (rows, total_count)."""
    if f.q:
        fast_path = bill_number_lookup(db, f)
        if fast_path is not None:
            rows, count = fast_path
            if rows or count:
                return rows, count

        rows, count = full_text_search(db, f)
        if rows or count:
            return rows, count

        return trigram_fallback(db, f)

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
    ], count
