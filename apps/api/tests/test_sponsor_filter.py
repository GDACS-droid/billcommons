"""?sponsor= on /api/v1/bills.

997,758 sponsorship rows were loaded and unreachable: there was no way to ask
"what did this member file". That is the core lobbyist and legislative-affairs
query, so it is worth a filter even though the underlying names are bare
surnames with no party or district behind them.

The tests below run against the live DB and must stay tolerant of its contents
-- they assert relationships (the filtered set is a subset, every returned bill
really has a matching sponsor) rather than fixed counts.
"""
from __future__ import annotations

from sqlalchemy import text

from billcommons_shared.db import get_session


def _a_sponsor_name() -> str | None:
    """A sponsor name that actually exists, so the test does not depend on any
    particular legislator still being in office."""
    db = get_session()
    try:
        return db.execute(
            text(
                "SELECT name FROM sponsorships "
                "WHERE name IS NOT NULL AND length(name) BETWEEN 4 AND 20 "
                "LIMIT 1"
            )
        ).scalar()
    finally:
        db.close()


def test_sponsor_filter_narrows_the_result_set(client):
    name = _a_sponsor_name()
    assert name, "no sponsorships in the DB -- unrelated failure"

    unfiltered = client.get("/api/v1/bills?per_page=1").json()["pagination"]["total"]
    filtered = client.get(f"/api/v1/bills?sponsor={name}&per_page=1").json()
    assert filtered["pagination"]["total"] > 0
    assert filtered["pagination"]["total"] < unfiltered


def test_every_returned_bill_actually_has_that_sponsor(client):
    """Guards the EXISTS: a join would return a bill once per matching sponsor
    row, inflating both the page and the total."""
    name = _a_sponsor_name()
    body = client.get(f"/api/v1/bills?sponsor={name}&per_page=10").json()
    ids = [b["id"] for b in body["data"]]
    assert ids

    assert len(ids) == len(set(ids)), "a bill was returned more than once"

    db = get_session()
    try:
        matched = db.execute(
            text(
                "SELECT count(DISTINCT bill_id) FROM sponsorships "
                "WHERE bill_id = ANY(:ids) AND lower(name) LIKE :needle"
            ),
            {"ids": ids, "needle": f"%{name.lower()}%"},
        ).scalar()
    finally:
        db.close()
    assert matched == len(ids)


def test_sponsor_filter_is_case_insensitive(client):
    name = _a_sponsor_name()
    lower = client.get(f"/api/v1/bills?sponsor={name.lower()}&per_page=1").json()
    upper = client.get(f"/api/v1/bills?sponsor={name.upper()}&per_page=1").json()
    assert lower["pagination"]["total"] == upper["pagination"]["total"]


def test_sponsor_filter_composes_with_jurisdiction(client):
    name = _a_sponsor_name()
    national = client.get(f"/api/v1/bills?sponsor={name}&per_page=1").json()
    scoped = client.get(f"/api/v1/bills?sponsor={name}&jurisdiction=FL&per_page=1").json()
    assert scoped["pagination"]["total"] <= national["pagination"]["total"]


def test_one_character_sponsor_is_rejected(client):
    """A single character matches most of the table. Rejected declaratively via
    min_length so it never reaches the query planner."""
    assert client.get("/api/v1/bills?sponsor=a").status_code == 422


def test_unknown_sponsor_is_empty_not_an_error(client):
    body = client.get("/api/v1/bills?sponsor=zzzzznotarealsponsor").json()
    assert body["data"] == []
    assert body["pagination"]["total"] == 0
