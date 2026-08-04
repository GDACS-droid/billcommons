"""Implementations of the 10 Bill Commons MCP tools.

Each function opens its own short-lived DB session (stateless HTTP server —
no session affinity assumed), does parameterized queries only (all external
text, including tool arguments, is untrusted input), and returns a plain
JSON-serializable dict: canonical ids, official source_url fields wherever
available, a `retrieved_at` freshness timestamp, and either a `coverage_warning`
or an `error` key when data is thin or the request can't be satisfied.

"official" data = values captured verbatim from an upstream source (provenance
columns populated). "derived" data = computed by Bill Commons itself (e.g.
trigram similarity scores, diff results) and is always labeled as such.
"""
from __future__ import annotations

import threading
import time

import difflib
import uuid
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import selectinload

from billcommons_mcp.telemetry import record_invocation
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    Jurisdiction,
    JurisdictionCoverage,
    LegislativeEvent,
    Session as SessionModel,
    VoteEvent,
)
from billcommons_shared.db import get_session
from billcommons_shared.evidence import (
    citation_text,
    download_url,
    evidence_digest,
    permalink,
    reproducibility_note,
    snapshot_id,
)
from billcommons_shared.normalize import normalize_bill_number
from billcommons_shared.topics import TOPICS, topic_bill_count

from billcommons_mcp.common import (
    ToolError,
    coverage_warning_for_jurisdiction,
    find_jurisdiction,
    full_text_warning_for_jurisdiction,
    iso,
    meta_envelope,
    serialize_coverage_row,
    to_str_id,
    utcnow,
)
from billcommons_mcp.rate_limit import _env_int
from billcommons_mcp.serializers import (
    serialize_bill_full,
    serialize_bill_summary,
    serialize_event,
    serialize_session,
    serialize_vote_event,
)

MAX_LIMIT = 50
DEFAULT_LIMIT = 20

# Work caps for expensive tools: this is a public, anonymous-callable MCP
# server (Finding 2 / rate-limiting-and-work-caps hardening) -- every tool
# that can do O(n) or worse work over caller-influenced data must have a hard
# ceiling, independent of the request-rate limiter, so a single request can't
# pin a worker on a multi-MB quadratic diff or an unbounded fan-out.
# All three env-tunable via `_env_int` (rate_limit.py's defensive int
# parser, reused here rather than re-implemented): a malformed/non-numeric
# env value falls back to the default instead of crashing the whole MCP
# server at import time (`int(os.environ.get(...))` would raise ValueError
# straight out of the module body, before any tool could even be
# registered).
MAX_COMPARE_TEXT_BYTES = _env_int(
    "MCP_MAX_COMPARE_TEXT_BYTES", 1_500_000
)  # ~1.5MB combined text before refusing a diff; env-tunable for tests
MAX_EVIDENCE_VOTES = _env_int("MCP_MAX_EVIDENCE_VOTES", 100)
MAX_EVIDENCE_HEARINGS = _env_int("MCP_MAX_EVIDENCE_HEARINGS", 100)
MAX_EVIDENCE_HISTORY_ITEMS = _env_int("MCP_MAX_EVIDENCE_HISTORY_ITEMS", 500)


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _bill_query_base():
    return select(Bill).options(
        selectinload(Bill.subjects),
        selectinload(Bill.sponsorships),
        selectinload(Bill.actions),
        selectinload(Bill.versions).selectinload(BillVersion.documents),
        selectinload(Bill.vote_events),
    )


def _run_tool(fn, tool_name: str | None = None):
    """Wrap a tool body so ToolError -> structured error dict, and any other
    exception is converted to a generic structured error rather than leaking
    a traceback to the client.

    Also the single chokepoint where usage is recorded. Every tool passes
    through here, so instrumenting it means no tool can be added later and
    silently go unmeasured -- which is how we ended up unable to answer
    "is anyone using this?" in the first place.
    """
    started = time.monotonic()
    outcome, error_code = "ok", None
    try:
        result = fn()
        # A structured error dict is still an error, even though nothing raised.
        if isinstance(result, dict) and isinstance(result.get("error"), dict):
            outcome = "error"
            error_code = result["error"].get("code")
        return result
    except ToolError as e:
        outcome, error_code = "error", getattr(e, "code", "tool_error")
        return e.to_dict()
    except Exception as e:  # noqa: BLE001 - convert to structured tool error
        outcome, error_code = "error", "internal_error"
        return {"error": {"code": "internal_error", "message": str(e)}}
    finally:
        if tool_name:
            record_invocation(
                tool=tool_name,
                outcome=outcome,
                error_code=error_code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )


# ---------------------------------------------------------------------------
# 1. search_legislation
# ---------------------------------------------------------------------------


def search_legislation(
    q: str,
    jurisdiction: str | None = None,
    chamber: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Search bills by keyword/phrase (FTS) or by normalized bill number.

    Args:
        q: search text, or a bill number like "HB 123" / "H.B. 123".
        jurisdiction: optional two-letter state code or name to filter by.
        chamber: optional chamber filter ("upper"/"lower"/etc).
        status: optional bill status filter.
        limit: max results (default 20, max 50).
    """

    def body() -> dict[str, Any]:
        if not q or not q.strip():
            raise ToolError("invalid_argument", "q must be a non-empty search string")
        limit_n = _clamp_limit(limit)
        db = get_session()
        try:
            jurisdiction_row: Jurisdiction | None = None
            coverage_warning = None
            if jurisdiction:
                jurisdiction_row = find_jurisdiction(db, jurisdiction)
                if jurisdiction_row is None:
                    raise ToolError(
                        "unknown_jurisdiction",
                        f"No jurisdiction found matching {jurisdiction!r}",
                    )
                coverage_warning = coverage_warning_for_jurisdiction(db, jurisdiction_row)

            # Bill-number fast path: try normalizing q as an identifier.
            bill_number_match: str | None = None
            try:
                bill_number_match = normalize_bill_number(q)
            except ValueError:
                bill_number_match = None

            stmt = _bill_query_base()
            if jurisdiction_row is not None:
                stmt = stmt.where(Bill.jurisdiction_id == jurisdiction_row.id)
            if chamber:
                stmt = stmt.where(Bill.chamber.ilike(chamber))
            if status:
                stmt = stmt.where(Bill.status.ilike(status))

            match_type = "full_text_search"
            number_ambiguity: dict[str, Any] | None = None
            number_truncated = False
            if bill_number_match:
                # Deterministic order + a real LIMIT. This path had neither:
                # "HB 1" with no jurisdiction filter selected every HB 1 in the
                # corpus, unbounded, and the payload then sliced it with
                # `results[:limit_n]` -- so WHICH bills a caller saw depended on
                # whatever order Postgres happened to return, and the rest
                # vanished with no indication. The full-text branch below has
                # always limited in SQL; this one did not.
                #
                # Fetch one extra row so truncation is detectable rather than
                # inferred from a count that equals the limit by coincidence.
                number_stmt = (
                    stmt.where(Bill.identifier_norm == bill_number_match)
                    .order_by(Bill.jurisdiction_id, Bill.session_id, Bill.id)
                    .limit(limit_n + 1)
                )
                results = list(db.execute(number_stmt).unique().scalars().all())
                number_truncated = len(results) > limit_n
                results = results[:limit_n]
                if results:
                    # A bill number matching several sessions is AMBIGUOUS, not
                    # exact, and this project's whole contract is that the two
                    # are never conflated -- TX HB 1 resolves to three sessions.
                    # Reporting "bill_number_exact" for a multi-session match
                    # told an agent it had THE bill when it had a pile of
                    # candidates.
                    distinct_sessions = {b.session_id for b in results}
                    if len(distinct_sessions) > 1 or number_truncated:
                        match_type = "bill_number_ambiguous"
                        number_ambiguity = {
                            "identifier": bill_number_match,
                            "distinct_sessions": len(distinct_sessions),
                            # Tell the caller what they are actually missing.
                            # Saying "add a jurisdiction" to someone who already
                            # passed jurisdiction="TX" reads as the tool not
                            # having listened, and sends them round the same loop.
                            "message": (
                                f"{bill_number_match!r} matches bills in "
                                f"{len(distinct_sessions)} session(s)"
                                + (" or more" if number_truncated else "")
                                + ". This is ambiguous, not a single bill -- add a "
                                + (
                                    "session"
                                    if jurisdiction_row is not None
                                    else "jurisdiction (and session)"
                                )
                                + " to resolve it, or use get_bill_record with a "
                                "bill_id."
                            ),
                        }
                    else:
                        match_type = "bill_number_exact"
                else:
                    results = None
            else:
                results = None

            if results is None:
                tsq_stmt = stmt.where(
                    Bill.search_tsv.op("@@")(func.websearch_to_tsquery("english", q))
                ).order_by(
                    func.ts_rank(
                        Bill.search_tsv, func.websearch_to_tsquery("english", q)
                    ).desc()
                )
                results = list(db.execute(tsq_stmt.limit(limit_n)).unique().scalars().all())
                match_type = "full_text_search"

            if not results:
                match_type = "no_match"

            payload = {
                "query": q,
                "match_type": match_type,
                "jurisdiction": jurisdiction_row.abbreviation if jurisdiction_row else None,
                "results": [serialize_bill_summary(b) for b in results[:limit_n]],
                "result_count": len(results[:limit_n]),
                "meta": meta_envelope(),
            }
            if number_ambiguity:
                payload["ambiguity"] = number_ambiguity
            if number_truncated:
                # Explicit, per the same rule the evidence packet follows: a
                # list that stops at the limit must say so, or "the list ends
                # here" and "we stopped here" are indistinguishable.
                payload["results_truncated"] = True
            if coverage_warning:
                payload["coverage_warning"] = coverage_warning
            elif not jurisdiction_row and not results:
                payload["coverage_warning"] = {
                    "message": (
                        "No jurisdiction filter was given and no results were found. "
                        "The Bill Commons database is under active ingestion; consult "
                        "get_jurisdiction_coverage before concluding a topic has no "
                        "matching legislation."
                    )
                }
            return payload
        finally:
            db.close()

    return _run_tool(body, "search_legislation")


# ---------------------------------------------------------------------------
# 2. get_bill_record
# ---------------------------------------------------------------------------


def get_bill_record(
    bill_id: str | None = None,
    jurisdiction: str | None = None,
    session: str | None = None,
    identifier: str | None = None,
    include_full_text: bool = False,
) -> dict[str, Any]:
    """Fetch the full record for a single bill, either by canonical bill_id
    (UUID) or by (jurisdiction, session, identifier).

    Args:
        bill_id: canonical UUID of the bill, if known.
        jurisdiction: two-letter state code or name (required if bill_id omitted).
        session: session identifier as stored (e.g. "2026 Regular Session").
        identifier: bill number, e.g. "HB 123" (required if bill_id omitted).
        include_full_text: if true, include extracted_text of documents (can be large).
    """

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            bill: Bill | None = None
            if bill_id:
                try:
                    bid = uuid.UUID(bill_id)
                except ValueError as e:
                    raise ToolError("invalid_argument", f"bill_id is not a valid UUID: {bill_id!r}") from e
                stmt = _bill_query_base().where(Bill.id == bid)
                bill = db.execute(stmt).unique().scalar_one_or_none()
            else:
                if not jurisdiction or not identifier:
                    raise ToolError(
                        "invalid_argument",
                        "Provide either bill_id, or jurisdiction + identifier "
                        "(session recommended to disambiguate).",
                    )
                jurisdiction_row = find_jurisdiction(db, jurisdiction)
                if jurisdiction_row is None:
                    raise ToolError(
                        "unknown_jurisdiction", f"No jurisdiction found matching {jurisdiction!r}"
                    )
                try:
                    norm = normalize_bill_number(identifier)
                except ValueError as e:
                    raise ToolError(
                        "invalid_argument", f"Could not parse bill identifier: {identifier!r}"
                    ) from e
                stmt = _bill_query_base().where(
                    Bill.jurisdiction_id == jurisdiction_row.id,
                    Bill.identifier_norm == norm,
                )
                if session:
                    stmt = stmt.join(SessionModel).where(SessionModel.identifier == session)
                candidates = list(db.execute(stmt).unique().scalars().all())
                if len(candidates) > 1:
                    raise ToolError(
                        "ambiguous_bill",
                        f"Multiple bills match {identifier!r} in {jurisdiction_row.abbreviation}; "
                        "pass session to disambiguate.",
                        candidates=[to_str_id(c.id) for c in candidates],
                    )
                bill = candidates[0] if candidates else None

            if bill is None:
                # Distinguish "no such bill" from "jurisdiction has no coverage".
                warning = None
                if jurisdiction:
                    jr = find_jurisdiction(db, jurisdiction)
                    if jr is not None:
                        warning = coverage_warning_for_jurisdiction(db, jr)
                err = ToolError(
                    "bill_not_found",
                    "No bill matched the given identifiers.",
                )
                payload = err.to_dict()
                if warning:
                    payload["coverage_warning"] = warning
                return payload

            payload: dict[str, Any] = {
                "bill": serialize_bill_full(bill, include_text=include_full_text),
                "meta": meta_envelope(),
            }
            jr = db.get(Jurisdiction, bill.jurisdiction_id)
            if jr is not None:
                warning = coverage_warning_for_jurisdiction(db, jr)
                if warning:
                    payload["coverage_warning"] = warning
            return payload
        finally:
            db.close()

    return _run_tool(body, "get_bill_record")


# ---------------------------------------------------------------------------
# 3. compare_bill_versions
# ---------------------------------------------------------------------------


def compare_bill_versions(
    bill_id: str,
    version_id_a: str | None = None,
    version_id_b: str | None = None,
) -> dict[str, Any]:
    """Deterministic diff between two versions of a bill's extracted text.

    If version_id_a/b are omitted, compares the earliest and latest versions
    that have extracted text. Errors if fewer than 2 versions have text.
    This is a DERIVED result (difflib unified + structured diff), not an
    official document. Refuses (structured `text_too_large` error) when the
    combined text of the two versions exceeds MAX_COMPARE_TEXT_BYTES, since
    the underlying diff is O(n*m) and unbounded input sizes are a DoS vector
    on this public, anonymous-callable server.
    """

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            try:
                bid = uuid.UUID(bill_id)
            except ValueError as e:
                raise ToolError("invalid_argument", f"bill_id is not a valid UUID: {bill_id!r}") from e

            stmt = (
                select(Bill)
                .options(selectinload(Bill.versions).selectinload(BillVersion.documents))
                .where(Bill.id == bid)
            )
            bill = db.execute(stmt).unique().scalar_one_or_none()
            if bill is None:
                raise ToolError("bill_not_found", f"No bill found with id {bill_id!r}")

            # Collect (version, text) pairs where extracted_text is present,
            # picking the first document with text per version.
            texted_versions: list[tuple[BillVersion, str]] = []
            for v in sorted(bill.versions, key=lambda v: (v.date or utcnow().date())):
                for doc in v.documents:
                    if doc.extracted_text:
                        texted_versions.append((v, doc.extracted_text))
                        break

            if len(texted_versions) < 2:
                jr = db.get(Jurisdiction, bill.jurisdiction_id)
                warning = coverage_warning_for_jurisdiction(db, jr) if jr else None
                full_text_warning = full_text_warning_for_jurisdiction(db, jr) if jr else None
                payload = ToolError(
                    "insufficient_versions",
                    f"Bill has {len(texted_versions)} version(s) with extracted text; "
                    "at least 2 are required to compare.",
                ).to_dict()
                if full_text_warning:
                    payload["coverage_warning"] = full_text_warning
                elif warning:
                    payload["coverage_warning"] = warning
                return payload

            def pick(vid: str | None, fallback: tuple[BillVersion, str]) -> tuple[BillVersion, str]:
                if vid is None:
                    return fallback
                for v, t in texted_versions:
                    if str(v.id) == vid:
                        return v, t
                raise ToolError(
                    "version_not_found",
                    f"Version {vid!r} not found (or has no extracted text) for this bill.",
                )

            version_a, text_a = pick(version_id_a, texted_versions[0])
            version_b, text_b = pick(version_id_b, texted_versions[-1])

            combined_bytes = len(text_a.encode("utf-8")) + len(text_b.encode("utf-8"))
            if combined_bytes > MAX_COMPARE_TEXT_BYTES:
                raise ToolError(
                    "text_too_large",
                    "Combined text of the two versions "
                    f"({combined_bytes:,} bytes) exceeds the "
                    f"{MAX_COMPARE_TEXT_BYTES:,}-byte limit for a diff. "
                    "This tool computes an O(n*m) diff and cannot safely "
                    "process documents this large; download the version(s) "
                    "via get_bill_record(include_full_text=True) and diff "
                    "them client-side instead.",
                    combined_bytes=combined_bytes,
                    limit_bytes=MAX_COMPARE_TEXT_BYTES,
                )

            lines_a = text_a.splitlines()
            lines_b = text_b.splitlines()
            unified = list(
                difflib.unified_diff(
                    lines_a,
                    lines_b,
                    fromfile=f"version:{version_a.note or version_a.id}",
                    tofile=f"version:{version_b.note or version_b.id}",
                    lineterm="",
                )
            )

            structured: list[dict[str, Any]] = []
            sm = difflib.SequenceMatcher(a=lines_a, b=lines_b, autojunk=False)
            for op, i1, i2, j1, j2 in sm.get_opcodes():
                if op == "equal":
                    continue
                structured.append(
                    {
                        "op": op,  # replace/delete/insert
                        "a_start": i1,
                        "a_end": i2,
                        "a_lines": lines_a[i1:i2],
                        "b_start": j1,
                        "b_end": j2,
                        "b_lines": lines_b[j1:j2],
                    }
                )

            return {
                "bill_id": str(bill.id),
                "version_a": {"id": str(version_a.id), "note": version_a.note, "date": (
                    version_a.date.isoformat() if version_a.date else None
                )},
                "version_b": {"id": str(version_b.id), "note": version_b.note, "date": (
                    version_b.date.isoformat() if version_b.date else None
                )},
                "diff": {
                    "label": "derived",
                    "method": "difflib.unified_diff + SequenceMatcher opcodes",
                    "unified_diff": unified,
                    "structured_changes": structured,
                },
                "meta": meta_envelope(),
            }
        finally:
            db.close()

    return _run_tool(body, "compare_bill_versions")


# ---------------------------------------------------------------------------
# 4. find_similar_bills
# ---------------------------------------------------------------------------


def find_similar_bills(bill_id: str, limit: int | None = None) -> dict[str, Any]:
    """Find bills with similar titles (trigram similarity) and/or shared long
    text n-grams, across jurisdictions. DERIVED (deterministic, labeled as such);
    not an official cross-reference.
    """

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            try:
                bid = uuid.UUID(bill_id)
            except ValueError as e:
                raise ToolError("invalid_argument", f"bill_id is not a valid UUID: {bill_id!r}") from e

            bill = db.get(Bill, bid)
            if bill is None:
                raise ToolError("bill_not_found", f"No bill found with id {bill_id!r}")

            limit_n = _clamp_limit(limit)

            # Trigram similarity over titles via pg_trgm, excluding self.
            sim_stmt = text(
                """
                select b.id, b.identifier, b.title, b.jurisdiction_id, b.source_url,
                       similarity(b.title, :title) as sim
                from bills b
                where b.id != :bid
                  and similarity(b.title, :title) > 0.2
                order by sim desc
                limit :limit_n
                """
            )
            rows = db.execute(
                sim_stmt, {"title": bill.title, "bid": bid, "limit_n": limit_n}
            ).mappings().all()

            similar = [
                {
                    "id": str(r["id"]),
                    "identifier": r["identifier"],
                    "title": r["title"],
                    "jurisdiction_id": str(r["jurisdiction_id"]),
                    "official_source_url": r["source_url"],
                    "similarity_score": float(r["sim"]),
                    "similarity_method": "pg_trgm title similarity (derived)",
                }
                for r in rows
            ]

            payload: dict[str, Any] = {
                "bill_id": str(bid),
                "label": "derived",
                "method": "trigram title similarity (pg_trgm); shared-text n-grams not "
                "computed when full text is unavailable",
                "similar_bills": similar,
                "result_count": len(similar),
                "meta": meta_envelope(),
            }
            if not similar:
                jr = db.get(Jurisdiction, bill.jurisdiction_id)
                warning = coverage_warning_for_jurisdiction(db, jr) if jr else None
                if warning:
                    payload["coverage_warning"] = warning
                else:
                    payload["note"] = (
                        "No similar bills found above the similarity threshold; this may "
                        "reflect thin overall database coverage rather than a genuine "
                        "absence of similar legislation."
                    )
            return payload
        finally:
            db.close()

    return _run_tool(body, "find_similar_bills")


# ---------------------------------------------------------------------------
# 5. get_vote_details
# ---------------------------------------------------------------------------


def get_vote_details(vote_event_id: str | None = None, bill_id: str | None = None) -> dict[str, Any]:
    """Fetch vote event(s) with member-level records.

    Args:
        vote_event_id: canonical UUID of a specific vote event.
        bill_id: if given (and vote_event_id omitted), returns all vote events for that bill.
    """

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            if vote_event_id:
                try:
                    vid = uuid.UUID(vote_event_id)
                except ValueError as e:
                    raise ToolError(
                        "invalid_argument", f"vote_event_id is not a valid UUID: {vote_event_id!r}"
                    ) from e
                stmt = (
                    select(VoteEvent)
                    .options(selectinload(VoteEvent.vote_records))
                    .where(VoteEvent.id == vid)
                )
                vote = db.execute(stmt).unique().scalar_one_or_none()
                if vote is None:
                    raise ToolError("vote_not_found", f"No vote event found with id {vote_event_id!r}")
                return {
                    "vote_events": [serialize_vote_event(vote)],
                    "meta": meta_envelope(),
                }

            if not bill_id:
                raise ToolError("invalid_argument", "Provide vote_event_id or bill_id.")
            try:
                bid = uuid.UUID(bill_id)
            except ValueError as e:
                raise ToolError("invalid_argument", f"bill_id is not a valid UUID: {bill_id!r}") from e

            bill = db.get(Bill, bid)
            if bill is None:
                raise ToolError("bill_not_found", f"No bill found with id {bill_id!r}")

            stmt = (
                select(VoteEvent)
                .options(selectinload(VoteEvent.vote_records))
                .where(VoteEvent.bill_id == bid)
                .order_by(VoteEvent.start_date)
            )
            votes = list(db.execute(stmt).unique().scalars().all())

            payload: dict[str, Any] = {
                "bill_id": str(bid),
                "vote_events": [serialize_vote_event(v) for v in votes],
                "result_count": len(votes),
                "meta": meta_envelope(),
            }
            if not votes:
                jr = db.get(Jurisdiction, bill.jurisdiction_id)
                warning = coverage_warning_for_jurisdiction(db, jr) if jr else None
                if warning:
                    payload["coverage_warning"] = warning
                else:
                    payload["note"] = "No recorded vote events found for this bill."
            return payload
        finally:
            db.close()

    return _run_tool(body, "get_vote_details")


# ---------------------------------------------------------------------------
# 6. get_upcoming_hearings
# ---------------------------------------------------------------------------


def get_upcoming_hearings(
    jurisdiction: str | None = None, bill_id: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    """List upcoming (start_date >= now) legislative hearings/events, optionally
    filtered by jurisdiction or bill.

    NOT COLLECTED TODAY. Bill Commons ingests no hearing or committee-calendar
    data for any jurisdiction, so this tool returns an empty list every time.
    An empty result means "we do not have this data", NEVER "no hearings are
    scheduled" -- do not report the absence of hearings to a user as a fact
    about the legislature's calendar. Check `data_status` on the response.
    """

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            limit_n = _clamp_limit(limit)
            jurisdiction_row: Jurisdiction | None = None
            if jurisdiction:
                jurisdiction_row = find_jurisdiction(db, jurisdiction)
                if jurisdiction_row is None:
                    raise ToolError(
                        "unknown_jurisdiction", f"No jurisdiction found matching {jurisdiction!r}"
                    )

            stmt = select(LegislativeEvent).where(LegislativeEvent.start_date >= utcnow())
            if jurisdiction_row is not None:
                stmt = stmt.where(LegislativeEvent.jurisdiction_id == jurisdiction_row.id)
            if bill_id:
                try:
                    bid = uuid.UUID(bill_id)
                except ValueError as e:
                    raise ToolError("invalid_argument", f"bill_id is not a valid UUID: {bill_id!r}") from e
                stmt = stmt.where(LegislativeEvent.bill_id == bid)
            stmt = stmt.order_by(LegislativeEvent.start_date).limit(limit_n)

            events = list(db.execute(stmt).unique().scalars().all())

            payload: dict[str, Any] = {
                "jurisdiction": jurisdiction_row.abbreviation if jurisdiction_row else None,
                "hearings": [serialize_event(e) for e in events],
                "result_count": len(events),
                "meta": meta_envelope(),
            }
            if not events:
                # Distinguish "we hold no calendar data at all" from "we hold
                # some and none matched". The old note claimed a daily refresh
                # target and possible ingestion lag; neither exists -- nothing
                # in the pipeline has ever written a legislative_events row, so
                # that note invited an agent to report a real empty calendar.
                # Probing the table (rather than hardcoding the disclosure)
                # keeps this honest in both directions if hearings are ingested
                # later.
                any_events = db.execute(
                    select(LegislativeEvent.id).limit(1)
                ).scalar_one_or_none()
                if any_events is None:
                    payload["data_status"] = "not_collected"
                    payload["note"] = (
                        "Bill Commons does not collect hearing or committee-calendar "
                        "data for any jurisdiction. This empty list means the data is "
                        "not held, NOT that no hearings are scheduled. Consult the "
                        "chamber's own calendar."
                    )
                else:
                    payload["data_status"] = "no_match"
                    payload["note"] = (
                        "No upcoming hearings matched this query. Hearing coverage is "
                        "partial; absence is not evidence that none are scheduled."
                    )
                    warning = (
                        coverage_warning_for_jurisdiction(db, jurisdiction_row)
                        if jurisdiction_row
                        else None
                    )
                    if warning:
                        payload["coverage_warning"] = warning
            else:
                payload["data_status"] = "ok"
            return payload
        finally:
            db.close()

    return _run_tool(body, "get_upcoming_hearings")


# ---------------------------------------------------------------------------
# 7. trace_legislative_history
# ---------------------------------------------------------------------------


def trace_legislative_history(bill_id: str) -> dict[str, Any]:
    """Full chronological legislative history for a bill: actions, versions,
    vote events, and related bills, merged into one timeline."""
    return _run_tool(_trace_legislative_history_body(bill_id), "trace_legislative_history")


def _trace_legislative_history_body(bill_id: str):
    """The work, without the telemetry wrapper.

    build_legislative_evidence_packet composes this. When it called the public
    tool instead, ONE user request recorded TWO invocations -- and since the
    packet is the flagship, the usage figures were quietly inflated by exactly
    the tool we most wanted an honest count of. Same failure as the uptime
    monitor: the metric measuring itself.

    Callers that are themselves tools must use this; only the public entry
    point records.
    """

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            try:
                bid = uuid.UUID(bill_id)
            except ValueError as e:
                raise ToolError("invalid_argument", f"bill_id is not a valid UUID: {bill_id!r}") from e

            stmt = _bill_query_base().where(Bill.id == bid)
            bill = db.execute(stmt).unique().scalar_one_or_none()
            if bill is None:
                raise ToolError("bill_not_found", f"No bill found with id {bill_id!r}")

            related_stmt = text(
                """
                select id, related_bill_id, related_identifier, relation_type
                from related_bills where bill_id = :bid
                """
            )
            related_rows = db.execute(related_stmt, {"bid": bid}).mappings().all()

            timeline: list[dict[str, Any]] = []
            for a in bill.actions:
                if a.action_date:
                    timeline.append(
                        {
                            "date": a.action_date.isoformat(),
                            "event_type": "action",
                            "description": a.description,
                            "classification": a.classification,
                        }
                    )
            for v in bill.versions:
                if v.date:
                    timeline.append(
                        {
                            "date": v.date.isoformat(),
                            "event_type": "version",
                            "description": f"Version published: {v.note or v.id}",
                        }
                    )
            for ve in bill.vote_events:
                if ve.start_date:
                    timeline.append(
                        {
                            "date": ve.start_date.isoformat(),
                            "event_type": "vote",
                            "description": ve.motion_text or "Vote recorded",
                            "result": ve.result,
                        }
                    )
            timeline.sort(key=lambda e: e["date"])

            payload: dict[str, Any] = {
                "bill_id": str(bid),
                "identifier": bill.identifier,
                "status": bill.status,
                "status_date": bill.status_date.isoformat() if bill.status_date else None,
                "timeline": timeline,
                "related_bills": [
                    {
                        "id": str(r["id"]),
                        "related_bill_id": str(r["related_bill_id"]) if r["related_bill_id"] else None,
                        "related_identifier": r["related_identifier"],
                        "relation_type": r["relation_type"],
                    }
                    for r in related_rows
                ],
                "official_source_url": bill.source_url,
                "meta": meta_envelope(),
            }
            if not timeline:
                jr = db.get(Jurisdiction, bill.jurisdiction_id)
                warning = coverage_warning_for_jurisdiction(db, jr) if jr else None
                if warning:
                    payload["coverage_warning"] = warning
            return payload
        finally:
            db.close()

    return body


# ---------------------------------------------------------------------------
# 8. build_legislative_evidence_packet
# ---------------------------------------------------------------------------


def build_legislative_evidence_packet(
    bill_id: str,
    include_full_text: bool = False,
    question: str | None = None,
) -> dict[str, Any]:
    """Compile a single evidence packet for a bill: full record, timeline,
    vote events with member votes, and hearings, each with official source
    URLs and explicit official-vs-derived labeling. Intended as citation-ready
    output for downstream research/reporting. Votes/hearings/history are
    capped (MAX_EVIDENCE_VOTES / MAX_EVIDENCE_HEARINGS /
    MAX_EVIDENCE_HISTORY_ITEMS) so a data anomaly on one bill can't produce
    an unbounded response on this public, anonymous-callable server. Finding
    5 honesty requirement: a capped section's `data` list going silently
    quiet at exactly the cap would look, to an AI/downstream consumer, like
    "this bill genuinely only has N votes/hearings/history items" -- a
    materially different (and false) claim from "there are MORE than N and
    this packet only shows the first N". Each section therefore reports an
    explicit `truncated` boolean + the `cap` applied (see the top-level
    `truncated` block below), computed from a real COUNT of the underlying
    rows, not inferred from "the capped list happens to be exactly N long"
    (which would itself be a false positive whenever a bill legitimately has
    exactly N records).
    """

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            try:
                bid = uuid.UUID(bill_id)
            except ValueError as e:
                raise ToolError("invalid_argument", f"bill_id is not a valid UUID: {bill_id!r}") from e

            stmt = _bill_query_base().where(Bill.id == bid)
            bill = db.execute(stmt).unique().scalar_one_or_none()
            if bill is None:
                raise ToolError("bill_not_found", f"No bill found with id {bill_id!r}")

            total_votes = db.execute(
                select(func.count()).select_from(VoteEvent).where(VoteEvent.bill_id == bid)
            ).scalar_one()
            vote_stmt = (
                select(VoteEvent)
                .options(selectinload(VoteEvent.vote_records))
                .where(VoteEvent.bill_id == bid)
                .order_by(VoteEvent.start_date)
                .limit(MAX_EVIDENCE_VOTES)
            )
            votes = list(db.execute(vote_stmt).unique().scalars().all())
            votes_truncated = total_votes > MAX_EVIDENCE_VOTES

            total_hearings = db.execute(
                select(func.count()).select_from(LegislativeEvent).where(LegislativeEvent.bill_id == bid)
            ).scalar_one()
            hearing_stmt = (
                select(LegislativeEvent)
                .where(LegislativeEvent.bill_id == bid)
                .order_by(LegislativeEvent.start_date)
                .limit(MAX_EVIDENCE_HEARINGS)
            )
            hearings = list(db.execute(hearing_stmt).unique().scalars().all())
            hearings_truncated = total_hearings > MAX_EVIDENCE_HEARINGS

            # Unrecorded internal call: see _trace_legislative_history_body.
            history = _trace_legislative_history_body(str(bid))()
            full_timeline = history.get("timeline", [])
            total_history_items = len(full_timeline) if isinstance(full_timeline, list) else 0
            if isinstance(full_timeline, list):
                history["timeline"] = full_timeline[:MAX_EVIDENCE_HISTORY_ITEMS]
            history_truncated = total_history_items > MAX_EVIDENCE_HISTORY_ITEMS

            jr = db.get(Jurisdiction, bill.jurisdiction_id)
            warning = coverage_warning_for_jurisdiction(db, jr) if jr else None
            sess = db.get(SessionModel, bill.session_id) if bill.session_id else None

            # Ids for the snapshot digest come from dedicated queries rather
            # than the capped lists above: the digest must describe the BILL,
            # not this packet's display limits. Two packets of the same bill
            # under different caps are the same evidence and must hash alike.
            all_vote_ids = list(
                db.execute(select(VoteEvent.id).where(VoteEvent.bill_id == bid)).scalars()
            )
            all_action_ids = list(
                db.execute(select(BillAction.id).where(BillAction.bill_id == bid)).scalars()
            )
            all_version_ids = list(
                db.execute(select(BillVersion.id).where(BillVersion.bill_id == bid)).scalars()
            )

            digest = evidence_digest(
                bill_id=str(bid),
                jurisdiction_code=jr.abbreviation if jr else None,
                session_name=sess.name if sess else None,
                identifier=bill.identifier,
                title=bill.title,
                status=bill.status,
                action_ids=all_action_ids,
                version_ids=all_version_ids,
                vote_event_ids=all_vote_ids,
            )
            snapshot = snapshot_id(digest)
            retrieved_at = iso(utcnow())

            packet: dict[str, Any] = {
                "bill_id": str(bid),
                "generated_at": retrieved_at,
                # Everything a reader needs to cite this and to find out later
                # whether it still says the same thing.
                # Named how_to_cite, not "citation": the packet already has a
                # `citations` block of source URLs, and two near-identical keys
                # in one object is how a consumer grabs the wrong one.
                "how_to_cite": {
                    "snapshot_id": snapshot,
                    "permalink": permalink(str(bid)),
                    "download_url": download_url(str(bid)),
                    "retrieved_at": retrieved_at,
                    "cite_as": citation_text(
                        identifier=bill.identifier,
                        title=bill.title,
                        jurisdiction_name=jr.name if jr else None,
                        session_name=sess.name if sess else None,
                        status=bill.status,
                        retrieved_at=retrieved_at,
                        snapshot=snapshot,
                        bill_id=str(bid),
                    ),
                    "reproducibility": reproducibility_note(),
                },
                # What this packet was assembled to answer, echoed back so the
                # artifact carries its own scope. An agent that files a packet
                # without the question it answered has produced a document
                # nobody can check the relevance of.
                "request": {
                    "question": question,
                    "jurisdiction_scope": (
                        f"{jr.name} ({jr.abbreviation}) only"
                        if jr
                        else "single bill; jurisdiction unknown"
                    ),
                    "scope_note": (
                        "This packet covers ONE bill. It is not a search of the "
                        "jurisdiction and cannot support a claim that no other "
                        "bill addresses this subject."
                    ),
                },
                "official_record": {
                    # NOT wholly official, and saying so is the entire point of
                    # this packet. serialize_bill_full carries `status`, which
                    # this project DERIVES -- classification, then a narrow text
                    # fallback, then None when unsure -- and `died_on_adjournment`
                    # is inferred from the session calendar with no filed action
                    # behind it at all. Labelling that block "official" beside
                    # citations.bill_source_url told an agent the legislature had
                    # said it. No legislature said it; we concluded it.
                    "label": "official, except the fields named in derived_fields",
                    "derived_fields": ["status", "status_date"],
                    "derived_note": (
                        "`status` is derived by Bill Commons from the action "
                        "record and the session calendar, not reported by the "
                        "jurisdiction. `died_on_adjournment` in particular has no "
                        "filed action behind it: the session ended while the bill "
                        "was pending. Cite it as a Bill Commons conclusion, not as "
                        "an official record. `status_date` is not populated."
                    ),
                    "data": serialize_bill_full(bill, include_text=include_full_text),
                },
                "legislative_history": {
                    "label": "official (source: bill_actions/bill_versions/vote_events); "
                    "timeline ordering is derived",
                    "data": history.get("timeline", []),
                },
                "votes": {
                    "label": "official",
                    "data": [serialize_vote_event(v) for v in votes],
                },
                "hearings": {
                    # Bill Commons collects NO hearing data -- legislative_events
                    # is empty corpus-wide. An empty list labelled "official"
                    # reads as "the legislature scheduled none", which is a
                    # different and much stronger claim than "we do not have
                    # this". That is precisely the absence-vs-ignorance
                    # confusion this packet exists to prevent, so the label has
                    # to carry it.
                    "label": (
                        "official"
                        if hearings
                        else "not collected -- Bill Commons has no hearing data"
                    ),
                    "data": [serialize_event(h) for h in hearings],
                    **(
                        {}
                        if hearings
                        else {
                            "absence_note": (
                                "An empty list here means Bill Commons does not "
                                "collect hearing events. It does NOT mean no "
                                "hearings were scheduled. Check the jurisdiction's "
                                "own calendar."
                            )
                        }
                    ),
                },
                "citations": {
                    "bill_source_url": bill.source_url,
                    "version_source_urls": [
                        v.source_url for v in bill.versions if v.source_url
                    ],
                },
                # Finding 5: explicit, per-section truncation flags so an AI
                # consumer of this packet knows when it's looking at a
                # PARTIAL list rather than silently mistaking "the list ends
                # at the cap" for "that's the whole history" -- each section
                # also reports the cap actually applied.
                "truncated": {
                    "votes": votes_truncated,
                    "hearings": hearings_truncated,
                    "history": history_truncated,
                },
                "truncation_caps": {
                    "votes": MAX_EVIDENCE_VOTES,
                    "hearings": MAX_EVIDENCE_HEARINGS,
                    "history": MAX_EVIDENCE_HISTORY_ITEMS,
                },
                "meta": meta_envelope(),
            }
            if warning:
                packet["coverage_warning"] = warning
            return packet
        finally:
            db.close()

    return _run_tool(body, "build_legislative_evidence_packet")


# ---------------------------------------------------------------------------
# 9. get_jurisdiction_coverage
# ---------------------------------------------------------------------------


def get_jurisdiction_coverage(state: str | None = None) -> dict[str, Any]:
    """Return jurisdiction_coverage rows: for a single state (abbreviation or
    name) if given, else the full 51-jurisdiction matrix."""

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            if state:
                jurisdiction_row = find_jurisdiction(db, state)
                if jurisdiction_row is None:
                    raise ToolError("unknown_jurisdiction", f"No jurisdiction found matching {state!r}")
                stmt = select(JurisdictionCoverage).where(
                    JurisdictionCoverage.jurisdiction_id == jurisdiction_row.id
                )
                rows = list(db.execute(stmt).scalars().all())
                return {
                    "jurisdiction": jurisdiction_row.abbreviation,
                    "coverage": [serialize_coverage_row(r) for r in rows] if rows else [],
                    "status": (
                        "NOT_STARTED"
                        if not rows
                        else rows[0].status
                        if len(rows) == 1
                        else "MIXED"
                    ),
                    "meta": meta_envelope(),
                }

            stmt = select(Jurisdiction)
            jurisdictions = list(db.execute(stmt).scalars().all())
            cov_stmt = select(JurisdictionCoverage)
            all_rows = list(db.execute(cov_stmt).scalars().all())
            by_jurisdiction: dict[str, list] = {}
            for r in all_rows:
                by_jurisdiction.setdefault(str(r.jurisdiction_id), []).append(r)

            matrix = []
            for j in jurisdictions:
                rows = by_jurisdiction.get(str(j.id), [])
                matrix.append(
                    {
                        "jurisdiction": j.abbreviation,
                        "name": j.name,
                        "coverage": [serialize_coverage_row(r) for r in rows],
                        "status": (
                            "NOT_STARTED" if not rows else rows[0].status if len(rows) == 1 else "MIXED"
                        ),
                    }
                )
            return {
                "coverage_matrix": matrix,
                "jurisdiction_count": len(matrix),
                "note": (
                    None
                    if matrix
                    else "No jurisdictions have been seeded into the database yet; "
                    "the full 51-jurisdiction registry lives at "
                    "data/registry/sessions-2026.json pending ingestion."
                ),
                "meta": meta_envelope(),
            }
        finally:
            db.close()

    return _run_tool(body, "get_jurisdiction_coverage")


# ---------------------------------------------------------------------------
# 10. get_active_sessions
# ---------------------------------------------------------------------------


def get_active_sessions(jurisdiction: str | None = None) -> dict[str, Any]:
    """List sessions flagged active=true, optionally filtered by jurisdiction."""

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            jurisdiction_row: Jurisdiction | None = None
            if jurisdiction:
                jurisdiction_row = find_jurisdiction(db, jurisdiction)
                if jurisdiction_row is None:
                    raise ToolError(
                        "unknown_jurisdiction", f"No jurisdiction found matching {jurisdiction!r}"
                    )

            stmt = select(SessionModel).where(SessionModel.active.is_(True))
            if jurisdiction_row is not None:
                stmt = stmt.where(SessionModel.jurisdiction_id == jurisdiction_row.id)
            sessions = list(db.execute(stmt).scalars().all())

            payload: dict[str, Any] = {
                "jurisdiction": jurisdiction_row.abbreviation if jurisdiction_row else None,
                "active_sessions": [serialize_session(s) for s in sessions],
                "result_count": len(sessions),
                "meta": meta_envelope(),
            }
            if not sessions:
                warning = (
                    coverage_warning_for_jurisdiction(db, jurisdiction_row)
                    if jurisdiction_row
                    else None
                )
                if warning:
                    payload["coverage_warning"] = warning
                else:
                    payload["note"] = (
                        "No active sessions found in the database. This may reflect "
                        "pending ingestion rather than an actual absence of active "
                        "legislative sessions; see get_jurisdiction_coverage and "
                        "data/registry/sessions-2026.json for the authoritative "
                        "session calendar."
                    )
            return payload
        finally:
            db.close()

    return _run_tool(body, "get_active_sessions")


# ---------------------------------------------------------------------------
# 11. list_topics
# ---------------------------------------------------------------------------


def _topic_search_hint(topic) -> str:
    """A representative keyword phrase for search_legislation, derived from
    the topic's own membership rule rather than hand-picked, so it can never
    drift out of sync with what the topic actually matches. Always a real
    phrase, not a LIKE pattern -- title_patterns are `%wrapped%` for SQL,
    never empty (billcommons_shared.topics.Topic always has at least one)."""
    return topic.title_patterns[0].strip("%")


# list_topics is explicitly advertised as the call-FIRST discovery tool
# (see its docstring below and search_legislation's), on a server that is
# public, anonymous, and keyless by design (see rate_limit.py) -- there is
# no CDN or edge cache in front of it the way apps/web sits in front of the
# REST /topics list. Each topic's count is a corpus-wide, leading-wildcard
# LIKE scan (7 of them per call today; see billcommons_shared.topics'
# membership_clause), so an uncached list_topics reintroduces, on a surface
# with strictly weaker protection, exactly the class of cost 3d82eee already
# flagged as worth caching on the web side (there it was Data Cache
# staleness across deploys, not raw query cost, but the fix here is the same
# shape: a short TTL cache, in-process since this is a single-process server
# per rate_limit.py's stated assumption). 300s matches
# FEED_CACHE_CONTROL/TOPIC_LIST_REVALIDATE elsewhere in this codebase.
_TOPIC_COUNT_CACHE_TTL_SECONDS = 300
_topic_count_cache: dict[str, tuple[int, float]] = {}
_topic_count_cache_lock = threading.Lock()


def _cached_topic_bill_count(db, topic) -> int:
    now = time.monotonic()
    with _topic_count_cache_lock:
        cached = _topic_count_cache.get(topic.slug)
        if cached is not None and (now - cached[1]) < _TOPIC_COUNT_CACHE_TTL_SECONDS:
            return cached[0]
    # Query outside the lock: this is a read-only COUNT, and holding the
    # lock across a DB round trip would serialize every concurrent
    # list_topics call on the slowest query instead of just the cache dict
    # access. A harmless race is possible (two callers both miss and both
    # query), never a correctness issue -- the cache is an optimization on a
    # tool that reports "how many bills" for reference, not the tool that
    # returns them.
    count = topic_bill_count(db, topic)
    with _topic_count_cache_lock:
        _topic_count_cache[topic.slug] = (count, now)
    return count


def list_topics() -> dict[str, Any]:
    """List Bill Commons' curated cross-state topic trackers (e.g. artificial
    intelligence, youth online safety, cybersecurity, cryptocurrency) -- the
    DISCOVERY entry point for "what topics does Bill Commons track" and "how
    do I get every bill in one". Each topic is a title/subject membership
    rule tuned for precision over recall (see billcommons_shared.topics), not
    a raw keyword search -- search_legislation is the tool for an arbitrary
    keyword/phrase the caller supplies themselves. Returns bill_count (live,
    computed the same way as the public /topics endpoint) and how_to_fetch_bills
    for each topic; this tool itself does not return bill rows."""

    def body() -> dict[str, Any]:
        db = get_session()
        try:
            items = [
                {
                    "slug": topic.slug,
                    "name": topic.name,
                    "description": topic.description,
                    "bill_count": _cached_topic_bill_count(db, topic),
                    "how_to_fetch_bills": (
                        f"GET https://api.billcommons.org/api/v1/topics/{topic.slug} "
                        "for the full paginated bill list (JSON), or "
                        f"https://billcommons.org/topics/{topic.slug} for the human-"
                        "readable hub. Within this MCP server, "
                        f"search_legislation(q={_topic_search_hint(topic)!r}) finds a "
                        "similar slice via full-text search -- similar, not identical, "
                        "since this topic's membership rule also matches on subject "
                        "tags that a keyword search does not see."
                    ),
                }
                for topic in TOPICS.values()
            ]
            return {
                "topics": items,
                "result_count": len(items),
                "meta": meta_envelope(),
            }
        finally:
            db.close()

    return _run_tool(body, "list_topics")
