"""Contract tests for GET /feeds/{jurisdiction}.atom.

Runs against the live DB, like the rest of this suite (see conftest.py) --
but unlike most of it, this feed's edge cases (a hostile title, an empty
jurisdiction, the safety-lag boundary) cannot be exercised against whatever
happens to already be in the corpus, so this file commits its own throwaway
jurisdiction/bill/bill_events rows (abbreviation prefixed "ZZ" the way
workers/ingest/tests already marks its own test jurisdictions) and tears
them down in a fixture, rather than reading existing rows read-only.

This is the LIVE Railway prod DB (per conftest.py's own docstring), not a
sandbox, so a leaked "Test State ZZ..." row is not merely untidy -- it shows
up in /jurisdictions, /coverage, and the sitemap for real, same class of bug
578b649 already fixed for the ingest suite's own test jurisdictions. Two
belts for that: teardown runs in try/finally on a FRESH session (a test that
failed mid-transaction can leave the fixture's own session unusable, which
would otherwise skip every DELETE below it), and setup sweeps any leftover
rows from a prior run that got killed before its own teardown ran.
"""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from billcommons_api.routers.changes import COMMIT_SAFETY_LAG_SECONDS
from billcommons_schema.models import Bill, BillEvent, Jurisdiction, Session as SessionModel
from billcommons_shared.db import get_session

ATOM_NS = "{http://www.w3.org/2005/Atom}"

HOSTILE_DETAIL = '<script>alert(1)</script> & "quotes" & \'apostrophes\''

# Comfortably past the safety lag, so a "past the watermark" fixture event
# never flakes against clock skew near the boundary. Also comfortably inside
# routers/feeds.py's FEED_LOOKBACK_DAYS (30 days) floor -- second-scale
# offsets stay inside that window by many orders of magnitude, so adding the
# floor changes nothing about what this fixture proves.
_SAFE_OFFSET = timedelta(seconds=COMMIT_SAFETY_LAG_SECONDS + 60)
# Comfortably inside it -- must NOT appear in the feed.
_TOO_RECENT_OFFSET = timedelta(seconds=5)

# Both halves of this fixture's own naming convention -- a sweep or a
# teardown must match on BOTH so it can never delete a real jurisdiction
# that merely happens to start with "ZZ" (there is no such US state, but the
# match stays narrow on purpose rather than trusting that).
_TEST_ABBR_PREFIX = "ZZ"
_TEST_NAME_PREFIX = "Test State "

# The sweep below is a SEPARATE hazard from the one it fixes: this suite runs
# against the shared LIVE Railway DB (see conftest.py), and more than one
# pytest run can be live against it at once on this box (dispatcher and
# builder runs do overlap in practice). An unconditional sweep would delete
# an in-flight fixture belonging to that OTHER run the instant this run's
# setup executes -- turning a self-healing cleanup into active flakiness
# for a run doing nothing wrong. Restricting the sweep to rows older than
# this age means a genuinely leaked row (from a run that was killed, so
# nothing is still using it) still gets swept on the next run, while a row
# from a run still in progress is left alone for the whole duration any
# single test in this file could plausibly take.
_SWEEP_MIN_AGE = timedelta(hours=1)


def _purge_jurisdiction(db, jurisdiction_id) -> None:
    """Delete one jurisdiction and its children in FK order (children before
    parents). Safe to call on a partially-built or already-torn-down
    fixture -- every statement is a no-op if its target has no matching
    rows."""
    bills_subq = "(SELECT id FROM bills WHERE jurisdiction_id = :j)"
    db.execute(
        text(f"DELETE FROM bill_events WHERE bill_id IN {bills_subq}"), {"j": jurisdiction_id}
    )
    db.execute(text("DELETE FROM bills WHERE jurisdiction_id = :j"), {"j": jurisdiction_id})
    db.execute(text("DELETE FROM sessions WHERE jurisdiction_id = :j"), {"j": jurisdiction_id})
    db.execute(text("DELETE FROM jurisdictions WHERE id = :j"), {"j": jurisdiction_id})


def _sweep_leftover_test_jurisdictions(db) -> None:
    """Self-healing: a run killed between commit and its own teardown leaves
    a jurisdiction matching this fixture's naming convention behind. Run at
    the START of every fixture use (not just once per session) so the suite
    recovers on the very next run rather than accumulating across many killed
    runs before anyone notices.

    Age-gated at _SWEEP_MIN_AGE: this suite shares a LIVE DB with any other
    concurrently running instance of itself (see _SWEEP_MIN_AGE's comment),
    so a fresh (i.e. still in-flight, still someone else's) fixture row must
    never be swept just because it also happens to match the naming
    convention. `created_at` (TimestampMixin) is exactly the "how old is
    this row" signal that distinguishes the two cases."""
    cutoff = datetime.now(timezone.utc) - _SWEEP_MIN_AGE
    leftover_ids = db.execute(
        text(
            "SELECT id FROM jurisdictions "
            "WHERE abbreviation LIKE :abbr_pat AND name LIKE :name_pat "
            "AND created_at < :cutoff"
        ),
        {"abbr_pat": f"{_TEST_ABBR_PREFIX}%", "name_pat": f"{_TEST_NAME_PREFIX}%", "cutoff": cutoff},
    ).scalars().all()
    for jid in leftover_ids:
        _purge_jurisdiction(db, jid)
    if leftover_ids:
        db.commit()


@pytest.fixture()
def feed_fixture():
    """One throwaway jurisdiction with a session and a bill, plus a helper to
    add bill_events at a caller-chosen changed_at. Committed for real (the
    API serves from its own live connection, so a savepoint-rollback session
    like workers/ingest/tests uses would never be visible to it) and torn
    down at the end of the test."""
    db = get_session()
    _sweep_leftover_test_jurisdictions(db)

    abbr = f"{_TEST_ABBR_PREFIX}{uuid.uuid4().hex[:6].upper()}"
    jurisdiction = Jurisdiction(
        name=f"{_TEST_NAME_PREFIX}{abbr}", abbreviation=abbr, classification="state"
    )
    db.add(jurisdiction)
    db.flush()
    session_row = SessionModel(
        jurisdiction_id=jurisdiction.id, identifier="2026 Test Session", active=True
    )
    db.add(session_row)
    db.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="An act relating to fixture bills",
    )
    db.add(bill)
    db.flush()
    db.commit()

    def add_event(kind: str, detail: str | None, changed_at: datetime) -> None:
        event = BillEvent(bill_id=bill.id, kind=kind, detail=detail, changed_at=changed_at)
        db.add(event)
        db.commit()

    try:
        yield abbr, bill, add_event
    finally:
        db.close()
        # A FRESH session for teardown, not the one the test body (or a
        # failing assertion inside it) may have left in an aborted/failed
        # transaction state -- Postgres refuses every further statement on a
        # session whose transaction already errored, which would otherwise
        # skip every DELETE below and leak the fixture's rows into the live
        # DB (the exact failure mode this whole hardening pass is for).
        cleanup_db = get_session()
        try:
            _purge_jurisdiction(cleanup_db, jurisdiction.id)
            cleanup_db.commit()
        finally:
            cleanup_db.close()


def test_unknown_jurisdiction_is_404_in_the_repo_error_shape(client):
    resp = client.get(f"/api/v1/feeds/{uuid.uuid4().hex[:10]}.atom")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "jurisdiction_not_found"
    assert "request_id" in body["error"]


def test_empty_jurisdiction_is_a_valid_feed_with_zero_entries(client, feed_fixture):
    """No bill_events past the watermark -- a real, quiet answer, not an
    error. Truth-in-emptiness for a feed IS the feed: Atom already
    distinguishes "here is the (empty) feed" from any error status."""
    abbr, _bill, _add_event = feed_fixture
    resp = client.get(f"/api/v1/feeds/{abbr}.atom")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/atom+xml")

    root = ET.fromstring(resp.content)
    assert root.tag == f"{ATOM_NS}feed"
    assert root.find(f"{ATOM_NS}entry") is None
    assert root.findtext(f"{ATOM_NS}updated"), "an empty feed still needs a feed-level <updated>"


def test_feed_has_an_author_per_rfc_4287(client, feed_fixture):
    """RFC 4287 sec 4.1.1: a feed MUST have atom:author unless every entry
    carries its own -- ours don't, so the feed-level one is required for a
    valid feed, checked even on the empty-feed path since that's the one
    with no entries to (wrongly) supply it instead."""
    abbr, _bill, _add_event = feed_fixture
    resp = client.get(f"/api/v1/feeds/{abbr}.atom")
    root = ET.fromstring(resp.content)
    author = root.find(f"{ATOM_NS}author")
    assert author is not None
    assert author.findtext(f"{ATOM_NS}name")


def test_illegal_xml_control_characters_are_stripped_not_left_to_break_parsing(client, feed_fixture):
    """XML 1.0 forbids most C0 control characters in element content outright
    -- ElementTree escapes MARKUP characters (&, <, >) on serialization but
    passes an illegal control character through verbatim, which would make
    the resulting bytes not well-formed XML at all: a 200 response no feed
    reader (or ET.fromstring itself) can parse. Real upstream CSV exports are
    not guaranteed clean of these.

    Also covers the noncharacter U+FFFE (a second, DB-storable member of the
    same illegal-character-class bug: valid UTF-8, so it round-trips through
    Postgres fine, but a conforming XML parser is required to reject it) --
    built via chr() rather than a literal, same reason as the lone-surrogate
    unit test below."""
    abbr, _bill, add_event = feed_fixture
    now = datetime.now(timezone.utc)
    hostile_control_chars = "vote count corrected\x0cfor\x01real" + chr(0xFFFE)
    add_event("status", hostile_control_chars, now - _SAFE_OFFSET)

    resp = client.get(f"/api/v1/feeds/{abbr}.atom")
    assert resp.status_code == 200

    # Proves parseability directly, per the repo's own bar for this feature:
    # a hostile control char must not be ABLE to produce an unparseable 200.
    root = ET.fromstring(resp.content)
    entries = root.findall(f"{ATOM_NS}entry")
    assert len(entries) == 1
    summary = entries[0].findtext(f"{ATOM_NS}summary")
    assert "\x0c" not in summary
    assert "\x01" not in summary
    assert chr(0xFFFE) not in summary
    # The surrounding real content survives -- this is stripping, not
    # truncation at the first illegal byte.
    assert "vote count correctedforreal" == summary


def test_lone_surrogate_is_stripped_not_left_to_500_the_response():
    """A lone surrogate (U+D800-U+DFFF) is NOT valid UTF-8 -- Postgres/psycopg
    refuses to even store it (confirmed: encoding it raises UnicodeEncodeError
    at the driver level before a query is sent), so this cannot be exercised
    through the live-DB fixture the way the other illegal characters are.
    Goes around the DB entirely and calls build_atom_feed directly with an
    in-memory stand-in event/bill/jurisdiction instead -- proving the ONE
    thing that actually matters here: ET.tostring() must not raise
    UnicodeEncodeError (and turn a scrape artifact into a 500) when a lone
    surrogate reaches this function by whatever path (a mis-decoded upstream
    byte stream is the realistic one). Built via chr(), never a literal
    surrogate in this file's own source, which would not be valid UTF-8 and
    would break the file itself."""
    from types import SimpleNamespace

    from billcommons_api.atom import build_atom_feed

    jurisdiction = SimpleNamespace(abbreviation="ZZ", name="Test State")
    bill = SimpleNamespace(identifier="HB 1", id=uuid.uuid4())
    hostile = "vote count corrected" + chr(0xD800) + "for real"
    event = SimpleNamespace(
        seq=1, kind="status", detail=hostile, changed_at=datetime.now(timezone.utc)
    )

    xml_body = build_atom_feed(jurisdiction, [(event, bill)], "https://example.test/feed")

    root = ET.fromstring(xml_body)
    summary = root.find(f"{ATOM_NS}entry/{ATOM_NS}summary").text
    assert chr(0xD800) not in summary
    assert "vote count correctedfor real" == summary


def test_entries_reflect_recent_events_and_escape_a_hostile_title(client, feed_fixture):
    """`detail` is exactly the untrusted, upstream-sourced text this test
    needs to hammer -- it's what render_digest/send_alerts.py and this
    feed's <title>/<summary> both surface verbatim from bill_events."""
    abbr, bill, add_event = feed_fixture
    now = datetime.now(timezone.utc)
    add_event("status", HOSTILE_DETAIL, now - _SAFE_OFFSET)

    resp = client.get(f"/api/v1/feeds/{abbr}.atom")
    assert resp.status_code == 200

    # The raw bytes must never contain an unescaped "<script>" -- proves
    # escaping actually happened at the XML layer, not just that a parser
    # was later lenient enough to recover from it.
    assert b"<script>" not in resp.content
    assert b"&lt;script&gt;" in resp.content

    root = ET.fromstring(resp.content)
    entries = root.findall(f"{ATOM_NS}entry")
    assert len(entries) == 1
    title = entries[0].findtext(f"{ATOM_NS}title")
    # Round-trips through the parser back to the literal hostile string --
    # proof the escaping was reversible/correct, not merely present.
    assert HOSTILE_DETAIL in title
    assert f"{abbr} HB 1" in title
    assert "status" in title
    summary = entries[0].findtext(f"{ATOM_NS}summary")
    assert summary == HOSTILE_DETAIL

    link = entries[0].find(f"{ATOM_NS}link")
    assert link.get("href") == f"https://billcommons.org/bills/{bill.id}"

    entry_id = entries[0].findtext(f"{ATOM_NS}id")
    assert entry_id.startswith("tag:billcommons.org,")
    assert "bill_events/" in entry_id

    feed_updated = root.findtext(f"{ATOM_NS}updated")
    entry_updated = entries[0].findtext(f"{ATOM_NS}updated")
    assert feed_updated == entry_updated, "feed-level <updated> must be the newest entry's"


def test_events_inside_the_safety_lag_are_withheld(client, feed_fixture):
    """The commit-safety-lag invariant this feed reuses from /changes: an
    event newer than COMMIT_SAFETY_LAG_SECONDS must not be served yet."""
    abbr, _bill, add_event = feed_fixture
    now = datetime.now(timezone.utc)
    add_event("status", "old enough", now - _SAFE_OFFSET)
    add_event("status", "too recent", now - _TOO_RECENT_OFFSET)

    resp = client.get(f"/api/v1/feeds/{abbr}.atom")
    root = ET.fromstring(resp.content)
    entries = root.findall(f"{ATOM_NS}entry")
    titles = [e.findtext(f"{ATOM_NS}title") for e in entries]
    assert any("old enough" in t for t in titles)
    assert not any("too recent" in t for t in titles)


def test_cache_control_is_set_for_a_successful_feed(client, feed_fixture):
    abbr, _bill, _add_event = feed_fixture
    resp = client.get(f"/api/v1/feeds/{abbr}.atom")
    assert "max-age" in resp.headers.get("cache-control", "")
