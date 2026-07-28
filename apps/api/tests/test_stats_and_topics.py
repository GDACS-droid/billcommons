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
