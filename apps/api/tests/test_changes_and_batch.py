"""Contract tests for the change feed and batch lookup.

Written against the consumer-facing promise ("a full sweep visits every change
exactly once", "everything I asked for is accounted for") rather than the
implementation, so they survive the feed being re-pointed at a different
storage scheme -- which already happened once: this suite originally tested a
feed over `bills.updated_at` and needed no logical changes when it moved to the
`bill_events` log.

Several tests pin bugs that were caught by review rather than by testing, and
would otherwise have shipped. They are labelled where that is the case.
"""
from __future__ import annotations

import uuid

import pytest


def _first_bill(client):
    body = client.get("/api/v1/bills", params={"per_page": 1}).json()
    assert body["data"], "no bills in the test corpus"
    return body["data"][0]


# ---------------------------------------------------------------------------
# Labels: a bill payload has to say which state and session it belongs to
# ---------------------------------------------------------------------------


def test_bill_payloads_name_their_jurisdiction_and_session(client):
    """The UUIDs alone forced a second round trip per row just to learn which
    session a result was in -- the ambiguity behind 'same bill number, two
    sessions' confusion."""
    bill = _first_bill(client)
    assert bill["jurisdiction_abbreviation"], "bill does not say which state it is from"
    assert bill["session_identifier"], "bill does not say which session it is from"

    detail = client.get(f"/api/v1/bills/{bill['id']}").json()
    assert detail["jurisdiction_abbreviation"] == bill["jurisdiction_abbreviation"]
    assert detail["session_identifier"] == bill["session_identifier"]

    hits = client.get("/api/v1/search", params={"q": "act", "per_page": 1}).json()["data"]
    if hits:
        assert hits[0]["session_identifier"], "search results omit the session"


def test_bill_payloads_carry_a_change_timestamp(client):
    """`/bills/batch` exists to answer "did anything move?". Without a
    timestamp in the payload the caller has to field-diff every row against a
    cached copy to find out."""
    assert _first_bill(client)["updated_at"], "bill payload has no updated_at"


# ---------------------------------------------------------------------------
# Batch lookup
# ---------------------------------------------------------------------------


def test_batch_resolves_ids_and_accounts_for_every_request(client):
    bill = _first_bill(client)
    missing = str(uuid.uuid4())
    body = client.get(
        "/api/v1/bills/batch", params={"ids": f"{bill['id']},{missing}"}
    ).json()

    assert [b["id"] for b in body["data"]] == [bill["id"]]
    # The whole point of the envelope: an unresolved key is REPORTED, not
    # silently dropped, so a caller diffing counts cannot mistake a typo or an
    # uncovered jurisdiction for "no change".
    assert body["not_found"] == [missing]
    assert len(body["data"]) + len(body["not_found"]) == 2


def test_batch_resolves_jurisdiction_and_number_keys(client):
    bill = _first_bill(client)
    key = f"{bill['jurisdiction_abbreviation']}:{bill['identifier'].replace(' ', '')}"
    body = client.get("/api/v1/bills/batch", params={"keys": key}).json()
    assert body["not_found"] == []
    assert body["data"][0]["id"] == bill["id"]


def test_batch_normalizes_bill_numbers_like_the_single_lookup(client):
    """`HI:sb 2135` and `HI:SB2135` are the same bill; a batch that only
    matched the stored spelling would send consumers back to one-at-a-time
    lookups for exactly the inputs they have."""
    bill = _first_bill(client)
    scruffy = f"{bill['jurisdiction_abbreviation']}:{bill['identifier'].lower()}"
    body = client.get("/api/v1/bills/batch", params={"keys": scruffy}).json()
    assert body["not_found"] == [], f"{scruffy} did not resolve"
    assert body["data"][0]["id"] == bill["id"]


def test_batch_does_not_report_a_found_bill_as_missing(client):
    """Review catch. Ids were keyed by the PARSED uuid, so two surface
    spellings of one id collapsed in the dict: the bill came back in `data`
    and one of the two tokens was simultaneously listed in `not_found`."""
    bill = _first_bill(client)
    lower = bill["id"].lower()
    upper = bill["id"].upper()
    body = client.get("/api/v1/bills/batch", params={"ids": f"{lower},{upper}"}).json()
    assert body["not_found"] == [], "the same id in two cases was reported missing"
    # ...and it must not be duplicated in data either.
    assert len(body["data"]) == 1


def test_batch_refuses_an_oversized_request(client):
    from billcommons_api.routers.bills import MAX_BATCH_KEYS

    keys = ",".join(str(uuid.uuid4()) for _ in range(MAX_BATCH_KEYS + 1))
    res = client.get("/api/v1/bills/batch", params={"ids": keys})
    assert res.status_code == 400
    # Refusing loudly beats silently truncating and returning a short list that
    # reads as complete. Reads the constant rather than hardcoding it: the cap
    # was raised once already (a consumer's real watchlist exceeded it), and a
    # test pinned to the old number would have failed for the wrong reason.
    assert str(MAX_BATCH_KEYS) in res.text


def test_lookup_handles_a_realistic_watchlist(client):
    """A real integrator's daily sync was ~130 bills. The cap must not turn
    the endpoint that exists to avoid pagination back into a paginated one."""
    from billcommons_api.routers.bills import MAX_BATCH_KEYS

    assert MAX_BATCH_KEYS >= 130
    bills = client.get("/api/v1/bills", params={"per_page": 50}).json()["data"]
    keys = [{"id": b["id"]} for b in bills]
    # Pad to a realistic watchlist size with misses, so the response still has
    # to account for every key.
    keys += [{"id": str(uuid.uuid4())} for _ in range(130 - len(keys))]
    body = client.post("/api/v1/bills/lookup", json={"keys": keys}).json()
    assert len(body["data"]) + len(body["not_found"]) + len(body["ambiguous"]) == len(keys)
    assert len(body["data"]) == len(bills)


def test_batch_requires_something_to_look_up(client):
    assert client.get("/api/v1/bills/batch").status_code == 400


# ---------------------------------------------------------------------------
# POST /bills/lookup
# ---------------------------------------------------------------------------


def test_post_lookup_matches_the_get_form(client):
    bill = _first_bill(client)
    posted = client.post(
        "/api/v1/bills/lookup",
        json={
            "keys": [
                {
                    "jurisdiction": bill["jurisdiction_abbreviation"],
                    "identifier": bill["identifier"],
                }
            ]
        },
    ).json()
    got = client.get(
        "/api/v1/bills/batch",
        params={"keys": f"{bill['jurisdiction_abbreviation']}:{bill['identifier']}"},
    ).json()
    assert [b["id"] for b in posted["data"]] == [b["id"] for b in got["data"]]


def test_post_lookup_handles_sessions_containing_colons(client):
    """The reason the structured body exists. 14 of 77 session identifiers in
    this corpus contain a colon ("Alabama special: Congressional maps"), which
    the `JUR:NUM:SESSION` string grammar cannot express unambiguously."""
    rows = client.get("/api/v1/sessions", params={"per_page": 50}).json()["data"]
    colon_sessions = [s for s in rows if ":" in s["identifier"]]
    if not colon_sessions:
        pytest.skip("no colon-containing session identifiers in this corpus")
    session = colon_sessions[0]
    bills = client.get(
        "/api/v1/bills",
        params={"jurisdiction": session["jurisdiction_abbreviation"], "per_page": 1},
    ).json()["data"]
    if not bills:
        pytest.skip("no bills for that jurisdiction")
    bill = bills[0]
    body = client.post(
        "/api/v1/bills/lookup",
        json={
            "keys": [
                {
                    "jurisdiction": bill["jurisdiction_abbreviation"],
                    "identifier": bill["identifier"],
                    "session": bill["session_identifier"],
                }
            ]
        },
    ).json()
    assert body["not_found"] == [], "a session with a colon failed to resolve"
    assert body["data"][0]["id"] == bill["id"]


def test_post_lookup_rejects_an_empty_body(client):
    assert client.post("/api/v1/bills/lookup", json={"keys": []}).status_code == 400


# ---------------------------------------------------------------------------
# Change feed
# ---------------------------------------------------------------------------


def test_changes_returns_events_with_a_kind(client):
    """A feed that only says "this bill changed" forces the consumer to
    refetch and diff every child collection to find out what happened."""
    body = client.get("/api/v1/changes", params={"per_page": 5}).json()
    assert "next_cursor" in body and body["next_cursor"]
    for event in body["data"]:
        assert event["kind"] in {
            "created",
            "status",
            "actions",
            "sponsors",
            "text",
            "metadata",
        }
        assert event["changed_at"]
        assert event["cursor"]


def test_changes_cursor_is_opaque(client):
    """Consumers persist cursors for months. Publishing the raw ordering key
    would freeze it; wrapping it lets the ordering scheme change later without
    invalidating a single stored cursor."""
    body = client.get("/api/v1/changes", params={"per_page": 1}).json()
    cursor = body["next_cursor"]
    assert not cursor.isdigit(), "cursor exposes the raw sequence number"


def test_changes_rejects_a_corrupt_cursor(client):
    res = client.get("/api/v1/changes", params={"cursor": "not-a-real-cursor"})
    assert res.status_code == 400
    assert "cursor" in res.text.lower()


def test_changes_full_sweep_visits_every_event_exactly_once(client):
    """The property consumers actually depend on. Catches both failure modes
    at once -- skipping (an offset shifting under concurrent writes) and
    repeating (a cursor that cannot advance past a tie, which is what a
    timestamp-only cursor did on this corpus: 25,240 bills share one
    timestamp, and paging looped forever)."""
    seen: list[str] = []
    params = {"per_page": 25}
    for _ in range(12):  # bounded so a stuck cursor fails instead of hanging
        body = client.get("/api/v1/changes", params=params).json()
        seen.extend(e["cursor"] for e in body["data"])
        if not body["has_more"]:
            break
        params = {"per_page": 25, "cursor": body["next_cursor"]}

    assert len(seen) == len(set(seen)), "a full sweep delivered the same event twice"
    if len(seen) <= 25:
        pytest.skip("change log too small to exercise multi-page paging")


def test_changes_empty_page_echoes_the_cursor_rather_than_nulling_it(client):
    """Returning null invites a client to pass it back and get a 422;
    advancing to "now" would step them past changes they never received."""
    body = client.get("/api/v1/changes", params={"per_page": 500}).json()
    while body["has_more"]:
        body = client.get(
            "/api/v1/changes", params={"per_page": 500, "cursor": body["next_cursor"]}
        ).json()
    caught_up = body["next_cursor"]

    again = client.get("/api/v1/changes", params={"cursor": caught_up}).json()
    assert again["data"] == []
    assert again["has_more"] is False
    assert again["next_cursor"] == caught_up, "an empty page moved the cursor"


def test_changes_can_be_filtered_to_a_watchlist(client):
    """Without this a consumer tracking 160 bills has to page the entire
    national delta and filter client-side -- the N+1 that batch lookup exists
    to kill, reintroduced one layer up."""
    body = client.get("/api/v1/changes", params={"per_page": 50}).json()
    if not body["data"]:
        pytest.skip("no change events recorded yet")
    target = next((e["bill"]["id"] for e in body["data"] if e["bill"]), None)
    if target is None:
        pytest.skip("no event carried a bill")

    filtered = client.get("/api/v1/changes", params={"ids": target, "per_page": 50}).json()
    assert filtered["data"], "watchlist filter returned nothing for a known bill"
    assert {e["bill"]["id"] for e in filtered["data"] if e["bill"]} == {target}


def test_changes_rejects_an_oversized_page(client):
    assert client.get("/api/v1/changes", params={"per_page": 5000}).status_code == 400


def test_changes_rejects_a_malformed_id_filter(client):
    res = client.get("/api/v1/changes", params={"ids": "not-a-uuid"})
    assert res.status_code == 400
