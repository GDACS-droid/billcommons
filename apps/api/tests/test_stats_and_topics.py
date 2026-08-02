"""/api/v1/stats/mortality and /api/v1/topics.

The mortality report exists to serve one finding -- most bills end by the
session adjourning, not by a vote -- so the invariant that matters is that its
buckets PARTITION the corpus: every bill lands in exactly one of
enacted/died_on_adjournment/killed/pending/unknown and the buckets sum back to
the total. A report whose rows don't reconcile is worse than no report; it is
the site's most quotable page.

Topics are curated slices presented as "every X bill in the country", so the
contract tests pin that the endpoint serves the standard list envelope (the
web hub pages through it with fetchAllPages) and that an unknown slug is a
loud 404, never an empty 200 that renders as "0 bills on this topic".
"""
from __future__ import annotations

from billcommons_api.routers.stats import _DIED_ON_ADJOURNMENT, _ENACTED, _KILLED, _PENDING


def test_mortality_buckets_partition_every_jurisdiction(client):
    resp = client.get("/api/v1/stats/mortality")
    assert resp.status_code == 200
    body = resp.json()

    for row in body["data"]:
        assert (
            row["enacted"]
            + row["died_on_adjournment"]
            + row["killed"]
            + row["pending"]
            + row["unknown"]
            == row["total"]
        ), f"buckets do not partition {row['jurisdiction_code']}"


def test_mortality_totals_are_the_sum_of_the_rows(client):
    """The headline number ("35% died on adjournment") is quoted from totals
    while the table renders the rows; if they can drift apart the page can
    contradict itself."""
    body = client.get("/api/v1/stats/mortality").json()
    for key in ("total", "enacted", "died_on_adjournment", "killed", "pending", "unknown"):
        assert body["totals"][key] == sum(row[key] for row in body["data"])


def test_mortality_buckets_cover_the_status_vocabulary():
    """Every status the ingest can assign must be claimed by exactly one
    bucket. A new status added to the vocabulary but not routed here would
    silently land in 'unknown' and misstate the report."""
    from billcommons_ingest import status as ingest_status

    claimed = _ENACTED | _DIED_ON_ADJOURNMENT | _KILLED | _PENDING
    assert claimed == set(ingest_status.ALL_STATUSES)


def test_topics_list_serves_curated_topics(client):
    resp = client.get("/api/v1/topics")
    assert resp.status_code == 200
    topics = {t["slug"]: t for t in resp.json()["data"]}
    assert "artificial-intelligence" in topics
    for topic in topics.values():
        assert topic["name"] and topic["description"]
        assert isinstance(topic["bill_count"], int)


def test_topic_bills_is_a_standard_pageable_list(client):
    """The web hub fetches ALL pages via fetchAllPages, which depends on the
    standard {data, pagination, meta} envelope and honest total_pages."""
    resp = client.get("/api/v1/topics/artificial-intelligence", params={"per_page": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"data", "pagination", "meta"}
    assert body["pagination"]["per_page"] == 5
    for bill in body["data"]:
        # The hub links each bill by canonical URL, which needs both labels.
        assert bill["jurisdiction_abbreviation"]
        assert bill["session_identifier"]


def test_unknown_topic_is_a_404_not_an_empty_page(client):
    resp = client.get("/api/v1/topics/underwater-basket-weaving")
    assert resp.status_code == 404


def test_mortality_exposes_a_cross_state_comparable_figure():
    """did_not_pass must equal died_on_adjournment + killed, per row and total.

    Which of the two terminal buckets a state uses is decided by whether its
    clerk files a death action -- CA/WI/NY report 100% killed, MA/MO/IA report
    100% died_on_adjournment -- so only the SUM is comparable across states.
    The report publishes a per-state table, so it has to offer the comparable
    number rather than inviting a comparison of a filing convention.
    """
    from fastapi.testclient import TestClient
    from billcommons_api.app import create_app

    client = TestClient(create_app())
    body = client.get("/api/v1/stats/mortality").json()

    for row in body["data"]:
        assert row["did_not_pass"] == row["died_on_adjournment"] + row["killed"], (
            f"{row['jurisdiction_code']}: did_not_pass is not the sum of the two "
            "terminal buckets"
        )
        if row["total"]:
            assert row["did_not_pass_pct"] == round(
                100 * row["did_not_pass"] / row["total"], 1
            )
        # The degenerate flag must be exactly "one bucket is empty".
        expected = row["did_not_pass"] > 0 and (
            row["died_on_adjournment"] == 0 or row["killed"] == 0
        )
        assert row["terminal_split_is_degenerate"] is expected, (
            f"{row['jurisdiction_code']}: degenerate flag disagrees with the buckets"
        )

    totals = body["totals"]
    assert totals["did_not_pass"] == totals["died_on_adjournment"] + totals["killed"]


def test_mortality_flags_the_states_where_the_split_is_meaningless():
    """At least one real jurisdiction must trip the degenerate flag.

    If this ever returns nothing, either the corpus changed shape or the flag
    stopped working -- both are worth knowing, because the per-state table is
    published as citable.
    """
    from fastapi.testclient import TestClient
    from billcommons_api.app import create_app

    client = TestClient(create_app())
    rows = client.get("/api/v1/stats/mortality").json()["data"]
    degenerate = [r for r in rows if r["terminal_split_is_degenerate"]]
    assert degenerate, "no jurisdiction flagged degenerate — flag is likely broken"
