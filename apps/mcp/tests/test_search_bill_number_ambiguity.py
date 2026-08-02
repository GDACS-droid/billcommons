"""A bill number matching several sessions is AMBIGUOUS, never "exact".

The bill-number fast path had no ORDER BY, no LIMIT, and reported
`bill_number_exact` however many bills matched. "HB 1" with no jurisdiction
filter therefore selected every HB 1 in the corpus, returned them in whatever
order Postgres chose, sliced them in Python, and labelled the survivors an
exact match -- telling an agent it had THE bill when it had a pile of
candidates. TX HB 1 alone resolves to three sessions.
"""
import inspect

from billcommons_mcp import tools

SRC = inspect.getsource(tools)


def test_bill_number_path_orders_deterministically():
    assert ".order_by(Bill.jurisdiction_id, Bill.session_id, Bill.id)" in SRC, (
        "unordered results make the Python-side slice arbitrary"
    )


def test_bill_number_path_limits_in_sql():
    assert ".limit(limit_n + 1)" in SRC, (
        "must limit in SQL, and fetch one extra so truncation is detectable"
    )


def test_multi_session_match_is_not_reported_as_exact():
    assert '"bill_number_ambiguous"' in SRC
    idx_amb = SRC.index('match_type = "bill_number_ambiguous"')
    idx_exact = SRC.index('match_type = "bill_number_exact"')
    assert idx_amb < idx_exact, (
        "the ambiguous branch must be evaluated before falling through to exact"
    )


def test_ambiguity_payload_tells_the_caller_how_to_resolve_it():
    assert '"distinct_sessions"' in SRC
    assert "add a " in SRC and "jurisdiction" in SRC
    assert "get_bill_record" in SRC


def test_truncation_is_explicit():
    assert '"results_truncated"' in SRC, (
        "a list that stops at the limit must say so"
    )
