"""search_legislation's full-text branch must cap-then-rank, mirroring
billcommons_api.search.MATCH_CAP.

`.order_by(ts_rank(...).desc()).limit(limit_n)` computes ts_rank -- detoasting
a tsvector -- for EVERY row matching the WHERE clause before it can sort and
take the top N. For a common word ("act" appears in nearly every bill's
"AN ACT...") that touches close to the whole corpus on every call, on a
public, anonymous-callable MCP server where a single request can trigger it.
The fix caps candidate bill ids on a cheap, id-ordered query FIRST (see
FTS_MATCH_CAP), then ranks only that capped set.
"""
from __future__ import annotations

import inspect
import time
import uuid

from sqlalchemy import text as sqltext

import billcommons_mcp.tools as tools
from billcommons_shared.db import get_session

SRC = inspect.getsource(tools)


def test_full_text_branch_caps_candidates_before_ranking():
    assert "FTS_MATCH_CAP" in SRC
    assert "candidate_stmt" in SRC
    assert ".order_by(Bill.id)" in SRC, (
        "the candidate cap must use a cheap, deterministic, indexed order -- "
        "not an unordered or rank-ordered LIMIT"
    )


def test_full_text_branch_ranks_only_the_capped_candidate_set():
    idx_candidates = SRC.index("candidate_ids = list(")
    idx_rank_order = SRC.index("func.ts_rank(Bill.search_tsv")
    assert idx_candidates < idx_rank_order, (
        "candidates must be capped BEFORE ranking, not the other way round"
    )


def test_search_capped_sets_results_truncated():
    """The tool's existing results_truncated contract (see
    test_search_bill_number_ambiguity.test_truncation_is_explicit) must also
    fire when the full-text cap binds, not just on the bill-number path."""
    assert "search_capped" in SRC
    idx_search_capped = SRC.index("search_capped = len(candidate_ids)")
    idx_truncated_check = SRC.index("if number_truncated or search_capped:")
    assert idx_search_capped < idx_truncated_check


def test_common_word_search_returns_quickly():
    """Live-DB regression: "act" must not blow up into an unbounded rank-all
    scan. 15s mirrors the API's sibling ceiling (test_search.py); measured
    ~1.5-3.5s locally against the live DB post-fix."""
    t0 = time.monotonic()
    result = tools.search_legislation(q="act", limit=10)
    elapsed = time.monotonic() - t0
    assert "error" not in result
    assert elapsed < 15, f"search_legislation('act') took {elapsed:.1f}s, expected < 15s"


def test_common_word_search_marks_results_truncated():
    """"act" is common enough to saturate FTS_MATCH_CAP against the live
    corpus (confirmed: bills.search_tsv alone matches 55k+ rows) -- the tool
    must say so rather than presenting a silently-sampled top 10 as exhaustive."""
    result = tools.search_legislation(q="act", limit=10)
    assert result.get("results_truncated") is True


def test_narrow_filtered_search_is_not_falsely_marked_truncated():
    """The flip side: a query that does not saturate the cap (a jurisdiction
    filter narrows "act" well below FTS_MATCH_CAP) must not carry the
    truncation flag -- otherwise every search would read as incomplete."""
    result = tools.search_legislation(q="act", jurisdiction="NC", limit=10)
    assert "error" not in result
    assert result.get("results_truncated") is not True


def test_candidate_cap_fetches_one_past_the_cap():
    """Regression for the exact-boundary bug caught in verify: a plain
    `.limit(FTS_MATCH_CAP)` selection can't distinguish "exactly
    FTS_MATCH_CAP matches" (genuinely exhaustive) from "FTS_MATCH_CAP+1 or
    more" (truly saturated) -- both return exactly FTS_MATCH_CAP rows. The
    candidate query must fetch one extra probe row and test `>`, not `==`,
    against the un-trimmed count."""
    assert ".limit(fts_match_cap + 1)" in SRC, (
        "candidate_stmt must over-fetch by one row to detect the exact boundary"
    )
    assert "search_capped = len(candidate_ids) > fts_match_cap" in SRC, (
        "saturation must be `> fts_match_cap`, not `== fts_match_cap`"
    )


def test_candidate_probe_row_is_trimmed_before_ranking():
    """The MATCH_CAP+1'th probe row (see above) must never reach `results` --
    it exists only to answer "was there one more row"."""
    idx_capped = SRC.index("search_capped = len(candidate_ids) > fts_match_cap")
    idx_trim = SRC.index("candidate_ids = candidate_ids[:fts_match_cap]")
    idx_rank_query = SRC.index("tsq_stmt = (\n                        stmt.where(Bill.id.in_(candidate_ids)")
    assert idx_capped < idx_trim < idx_rank_query, (
        "the probe row must be trimmed AFTER computing search_capped but "
        "BEFORE the ranking query runs against candidate_ids"
    )


def test_rank_query_reapplies_the_fts_predicate():
    """Regression for the second verify finding: the ranking query filters
    `Bill.id.in_(candidate_ids)` in a SEPARATE statement from the one that
    built candidate_ids. Under READ COMMITTED, a bill whose search_tsv
    changed between the two statements (e.g. a concurrent ingest write)
    could otherwise be returned despite no longer matching `q` -- the
    ranking query must re-apply tsq_filter, not rely solely on
    candidate_ids having been built from a still-valid match."""
    idx_rank_query = SRC.index("stmt.where(Bill.id.in_(candidate_ids), tsq_filter)")
    idx_order = SRC.index(".order_by(func.ts_rank(Bill.search_tsv")
    assert idx_rank_query < idx_order, (
        "the ranking query must filter on Bill.id.in_(candidate_ids) AND tsq_filter together"
    )


def _probe_text(token: str) -> str:
    """Build a search-probe string that's structurally immune to
    normalize_bill_number's fast-path pattern
    (`^([A-Z]*)\\s*0*(\\d+)([A-Z]*)$`, see billcommons_shared.normalize.
    _PATTERN -- one optional leading letter block, ONE contiguous digit
    run, one optional trailing letter block), regardless of what `token`
    (random hex, i.e. an unpredictable mix of digits and a-f letters)
    happens to contain.

    A plain "McpCapBoundaryProbe{token}" collides whenever token's digits
    happen to end up as a single contiguous run flanked only by letters --
    not just the all-digits case (~0.9% of runs): e.g. an all-hex-letter
    lead-in followed by an all-digit tail ("...probeABCDEF123456") also
    parses cleanly as prefix+digits. A single fixed letter spliced into the
    token (e.g. "{token[:6]}Q{token[6:]}") does NOT close this gap either --
    it was measured to still collide ~0.2% of the time for exactly that
    shape, since the fast path treats ANY letters immediately adjacent to
    the digit run (from the token or the splice) as a valid prefix/suffix.

    The only way to be immune regardless of token content is to force TWO
    digits that can never be merged into one run: a literal digit ("1")
    right after the fixed text prefix, and a literal letter-then-digit
    ("Z2") at the very end. _PATTERN allows only one letters->digits
    transition and one digits->letters transition in the whole string; the
    forced "1" starts the (only) digit run the pattern could recognize, so
    by the time "Z" appears it is provably *not* reclassifiable as leading
    prefix (a digit -- "1" -- already occurred before it), and the pattern's
    trailing group is letters-only, so a digit ("2") can never follow it.
    No arrangement of `token` between them can restore a match. Verified
    empirically over 500k random 12-hex-char tokens plus adversarial
    all-digit/all-letter/alternating extremes: zero collisions."""
    return f"McpCapBoundaryProbe1{token}Z2"


def _insert_boundary_bills(db, token: str, n: int) -> str:
    """Insert `n` bills matching a unique FTS token and nothing else in the
    live corpus. Returns the jurisdiction_id (for cleanup)."""
    jurisdiction_id = db.execute(
        sqltext(
            "INSERT INTO jurisdictions (id, name, abbreviation, classification, "
            "created_at, updated_at) "
            "VALUES (gen_random_uuid(), :name, :abbr, 'state', now(), now()) "
            "RETURNING id"
        ),
        {"name": f"McpCapBoundary{token}", "abbr": f"Z{token[:8]}".upper()},
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
                "ident": f"MB {token[:6]}{i}",
                "ident_norm": f"MB{token[:6]}{i}".upper(),
                "title": f"{_probe_text(token)} act number {i}",
            },
        )
    db.commit()
    return jurisdiction_id


def test_exact_match_cap_boundary_is_not_marked_truncated(monkeypatch):
    """Live-DB companion to the source-inspection boundary tests above:
    exactly FTS_MATCH_CAP real matches must NOT be reported as truncated --
    there is no FTS_MATCH_CAP+1'th row to have been dropped."""
    token = uuid.uuid4().hex[:12]
    n = 3
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = _insert_boundary_bills(db, token, n)
        monkeypatch.setattr(tools, "FTS_MATCH_CAP", n)
        result = tools.search_legislation(q=_probe_text(token), limit=20)
        assert "error" not in result
        assert result["result_count"] == n
        assert result.get("results_truncated") is not True
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


def test_one_past_match_cap_boundary_is_marked_truncated(monkeypatch):
    """The flip side: FTS_MATCH_CAP set one below the true match count (3
    matches, cap of 2) must be detected and flagged truncated."""
    token = uuid.uuid4().hex[:12]
    n = 3
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = _insert_boundary_bills(db, token, n)
        monkeypatch.setattr(tools, "FTS_MATCH_CAP", n - 1)
        result = tools.search_legislation(q=_probe_text(token), limit=20)
        assert "error" not in result
        assert result["result_count"] == n - 1
        assert result.get("results_truncated") is True
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


# ---------------------------------------------------------------------------
# Follow-up: clear search_capped/results_truncated on an empty final rank set
# ---------------------------------------------------------------------------


class _EmptyExecResult:
    """Minimal stand-in for a SQLAlchemy Result that always yields zero
    rows, regardless of what chain of .unique()/.scalars() the caller runs."""

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return []


class _ForcedEmptySecondQuerySession:
    """Wraps a real Session so its SECOND .execute() call returns zero rows
    while every other call passes straight through to the real session.

    search_legislation's full-text branch is exactly two statements: (1) the
    candidate_stmt id probe, (2) the tsq_stmt ranked re-check. Forcing call
    #2 to return nothing deterministically reproduces the race
    test_rank_query_reapplies_the_fts_predicate documents (a bill matching
    step 1 no longer matching by the time step 2 re-applies tsq_filter --
    e.g. a concurrent ingest write) without depending on real timing.

    Blanking "call #2" is inherently positional -- it silently degrades to a
    vacuous pass if an execute() call is ever added ahead of the probe (the
    thing meant to become empty would then be some OTHER, unrelated query).
    Every call's statement object is recorded in `.calls` so the test can
    assert call #1 actually IS the candidate/id-probe statement, not just
    trust the position -- a positional-drift tripwire, not a redesign."""

    def __init__(self, real):
        self._real = real
        self._call_count = 0
        self.calls: list = []

    def execute(self, *args, **kwargs):
        self._call_count += 1
        if args:
            self.calls.append(args[0])
        if self._call_count == 2:
            self._real.execute(*args, **kwargs)  # still run it, just discard the result
            return _EmptyExecResult()
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_zero_final_rows_clears_results_truncated(monkeypatch):
    """Regression: a capped candidate set (search_capped=True from the id
    probe) whose rank-stage re-check ends up with ZERO rows must report an
    honest no_match result, not results_truncated=True on an empty list --
    an empty page should never claim there's more to see."""
    token = uuid.uuid4().hex[:12]
    n = 2
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = _insert_boundary_bills(db, token, n)
        # cap = n - 1 so the id probe alone (step 1, real query, untouched)
        # marks search_capped=True.
        monkeypatch.setattr(tools, "FTS_MATCH_CAP", n - 1)

        real_get_session = tools.get_session
        wrapped_sessions: list[_ForcedEmptySecondQuerySession] = []

        def _get_wrapped_session():
            wrapped = _ForcedEmptySecondQuerySession(real_get_session())
            wrapped_sessions.append(wrapped)
            return wrapped

        monkeypatch.setattr(tools, "get_session", _get_wrapped_session)

        result = tools.search_legislation(q=_probe_text(token), limit=20)
        assert result["result_count"] == 0
        assert result["match_type"] == "no_match"
        assert result.get("results_truncated") is not True, (
            "an empty rank-stage result must not carry results_truncated=True"
        )

        # Positional-drift tripwire: confirm call #1 (the one that stayed
        # real and set search_capped=True) actually WAS the lean, single-
        # column candidate/id-probe statement -- not some other query that
        # happened to land first, which would make the "call #2 is the
        # rank re-check" assumption below silently wrong and this whole
        # test vacuous.
        assert len(wrapped_sessions) == 1, "expected exactly one DB session to be opened"
        calls = wrapped_sessions[0].calls
        assert len(calls) == 2, (
            f"expected exactly 2 execute() calls (id probe, then rank re-check), "
            f"got {len(calls)} -- if a query was added/removed, this test's "
            f"positional assumption about which call is the probe is stale"
        )
        probe_stmt = calls[0]
        probe_columns = list(probe_stmt.selected_columns)
        assert len(probe_columns) == 1 and "id" in str(probe_columns[0]).lower(), (
            f"call #1 must be the lean single-column id probe (candidate_stmt), "
            f"got columns={[str(c) for c in probe_columns]} -- this test's "
            f"'call #1 is the probe' assumption is stale"
        )
        probe_sql = str(probe_stmt).upper()
        assert "LIMIT" in probe_sql, "the candidate probe must be LIMIT-bounded"
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


# ---------------------------------------------------------------------------
# Follow-up: clamp FTS_MATCH_CAP<=0 at the point of use
# ---------------------------------------------------------------------------


def test_fts_match_cap_is_clamped_at_read_time():
    assert "_clamped_fts_match_cap" in SRC
    assert "max(1, int(FTS_MATCH_CAP))" in SRC


def test_zero_fts_match_cap_does_not_footgun_empty_truncated_results(monkeypatch):
    """FTS_MATCH_CAP=0 (or negative) must clamp to 1, not fetch a
    `.limit(0 + 1)` = 1-row probe that trims to an empty candidate list --
    which would otherwise mark search_capped=True and return zero results
    flagged truncated. With the clamp, a single genuine match must come
    back, unflagged; a second genuine match must come back flagged (cap
    effectively 1)."""
    token = uuid.uuid4().hex[:12]
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = _insert_boundary_bills(db, token, 1)
        for env_value in (0, -5):
            monkeypatch.setattr(tools, "FTS_MATCH_CAP", env_value)
            result = tools.search_legislation(q=_probe_text(token), limit=20)
            assert "error" not in result
            assert result["result_count"] == 1, (
                f"FTS_MATCH_CAP={env_value} must clamp to >=1, not drop the one real match"
            )
            assert result.get("results_truncated") is not True
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()


def test_negative_fts_match_cap_with_two_matches_clamps_to_one_and_truncates(monkeypatch):
    """With 2 real matches and FTS_MATCH_CAP clamped to 1, the search must
    behave exactly like an explicit cap of 1: one result, flagged
    truncated -- not zero results (the pre-clamp footgun) and not both
    results unflagged (an unclamped cap)."""
    token = uuid.uuid4().hex[:12]
    db = get_session()
    jurisdiction_id = None
    try:
        jurisdiction_id = _insert_boundary_bills(db, token, 2)
        monkeypatch.setattr(tools, "FTS_MATCH_CAP", -1)
        result = tools.search_legislation(q=_probe_text(token), limit=20)
        assert "error" not in result
        assert result["result_count"] == 1
        assert result.get("results_truncated") is True
    finally:
        if jurisdiction_id is not None:
            db.execute(sqltext("DELETE FROM bills WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM sessions WHERE jurisdiction_id = :jid"), {"jid": jurisdiction_id})
            db.execute(sqltext("DELETE FROM jurisdictions WHERE id = :jid"), {"jid": jurisdiction_id})
            db.commit()
        db.close()
