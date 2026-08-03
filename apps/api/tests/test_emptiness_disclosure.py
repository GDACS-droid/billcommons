"""Endpoints that hold no data must say so, not just return [].

/people, /committees and /events ship in the OpenAPI schema and return zero
rows for every query, because nothing populates those tables. The failure this
guards against is not a crash -- it is an agent reading `{"data": []}` for
"legislators in Florida" and telling a user that Florida has no legislators.

These assertions are deliberately about the SHAPE of the disclosure rather than
its exact wording, except for one thing: the notice must not be empty, and it
must not claim the data is merely delayed. Wording may be improved; the
guarantee that an empty result explains itself may not be dropped.
"""
from __future__ import annotations

import pytest

EMPTY_BY_DESIGN = [
    "/api/v1/people",
    "/api/v1/committees",
    "/api/v1/events",
]


@pytest.mark.parametrize("path", EMPTY_BY_DESIGN)
def test_empty_endpoint_explains_itself(client, path):
    body = client.get(path).json()
    assert body["data"] == [], f"{path} unexpectedly returned rows -- update this test"

    meta = body["meta"]
    assert meta["data_status"] in {"not_collected", "no_match"}
    assert meta["notice"], f"{path} returned an empty list with no explanation"


@pytest.mark.parametrize("path", EMPTY_BY_DESIGN)
def test_disclosure_does_not_blame_ingestion_lag(client, path):
    """The specific lie that was shipped, pinned so it cannot come back.

    The MCP hearings tool told callers that absence "may reflect ingestion lag
    rather than an empty calendar" for a table that has never held a row. That
    reads as "check back later" for something that is never coming, and it is
    worse than silence because it sounds like a status report.
    """
    meta = client.get(path).json()["meta"]
    notice = meta["notice"].lower()
    if meta["data_status"] != "not_collected":
        pytest.skip("dataset is populated; the lag wording is legitimate here")
    for phrase in ("ingestion lag", "refresh target", "check back"):
        assert phrase not in notice, f"{path} still explains absence as a delay"
    assert "not" in notice, "a not_collected notice should state what it is NOT"


@pytest.mark.parametrize("path", EMPTY_BY_DESIGN)
def test_disclosure_is_scoped_to_the_jurisdiction_filter(client, path):
    """A filtered query gets the same treatment as an unfiltered one -- the
    disclosure must not depend on the caller having asked broadly."""
    meta = client.get(f"{path}?jurisdiction=FL").json()["meta"]
    assert meta["data_status"] is not None
    assert meta["notice"]


def test_populated_endpoint_carries_no_notice(client):
    """The disclosure is for empty results only. A populated response that
    carried a 'coverage is partial' notice would train callers to ignore it,
    which is how the useful warnings stop being read."""
    body = client.get("/api/v1/bills?per_page=1").json()
    assert body["data"], "bills endpoint returned nothing -- unrelated failure"
    assert body["meta"]["data_status"] is None
    assert body["meta"]["notice"] is None
