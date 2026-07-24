"""Tests for billcommons_ingest.api_sync using an injected httpx
MockTransport wrapped in an OpenStatesClient -- no real network/API key
needed (mirrors test_openstates_api.py's pattern).

Business intent: an api_sync run must create genuinely new bills, must
leave an unchanged bill's core fields untouched on a second identical run
(idempotency -- the entire reason this exists instead of always re-bulk-
ingesting), and must record ingestion_runs so operators can see what an
incremental sync actually did.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from billcommons_ingest.api_sync import _bill_checksum, run_api_sync_job, sync_state
from billcommons_ingest.openstates_api import OpenStatesClient
from billcommons_schema.models import (
    Bill,
    BillAction,
    IngestionRun,
    Jurisdiction,
    JurisdictionCoverage,
    Session as SessionModel,
    Sponsorship,
)


def _make_jurisdiction_with_active_session(db_session, abbr=None):
    if abbr is None:
        abbr = f"ZQ_SYNC_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="API Sync Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db_session.add(session_row)
    db_session.flush()
    return jurisdiction, session_row


def _v3_bill_payload(
    *,
    openstates_id,
    identifier,
    title,
    action_description="Introduced",
    action_date="2026-01-05",
    session=None,
):
    payload = {
        "id": openstates_id,
        "identifier": identifier,
        "title": title,
        "chamber": "lower",
        "classification": ["bill"],
        "actions": [
            {"description": action_description, "date": action_date, "classification": ["introduction"]}
        ],
        "sponsorships": [{"name": "Jane Doe", "classification": "primary", "primary": True}],
        "sources": [{"url": "https://example-legislature.gov/bill/1"}],
    }
    if session is not None:
        payload["session"] = session
    return payload


def _client_with_pages(pages: dict) -> OpenStatesClient:
    def handler(request):
        page = int(dict(request.url.params).get("page", "1"))
        return httpx.Response(200, json=pages.get(page, {"results": [], "pagination": {"max_page": page}}))

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://v3.openstates.org")
    return OpenStatesClient(client=http_client, api_key="test-key")


def test_sync_state_creates_new_bill(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(openstates_id="ocd-bill/1", identifier="HB 1", title="An act on roads")
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.bills_created == 1
    assert result.actions == 1
    assert result.sponsorships == 1

    bill = db_session.execute(
        select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)
    ).scalar_one()
    assert bill.identifier == "HB 1"
    assert bill.title == "An act on roads"
    assert bill.source_url == "https://example-legislature.gov/bill/1"
    assert bill.latest_action_text == "Introduced"


def test_sync_state_is_idempotent_on_unchanged_bill(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(openstates_id="ocd-bill/2", identifier="HB 2", title="An act on bridges")
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    first = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()
    assert first.bills_created == 1

    # Re-running the identical payload must not create a duplicate bill or
    # re-count it as created/updated, and must not duplicate the action or
    # sponsorship rows either (checksum-based unchanged-skip, same
    # contract as openstates_bulk).
    client2 = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    second = sync_state(db_session, jurisdiction, client=client2)
    db_session.flush()

    assert second.bills_created == 0
    assert second.bills_unchanged == 1
    assert second.actions == 0
    assert second.sponsorships == 0

    bill = db_session.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalar_one()
    actions = db_session.execute(select(BillAction).where(BillAction.bill_id == bill.id)).scalars().all()
    sponsorships = db_session.execute(select(Sponsorship).where(Sponsorship.bill_id == bill.id)).scalars().all()
    assert len(actions) == 1
    assert len(sponsorships) == 1


def test_sync_state_updates_changed_bill_title(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload_v1 = _v3_bill_payload(openstates_id="ocd-bill/3", identifier="HB 3", title="Original title")
    client_v1 = _client_with_pages({1: {"results": [payload_v1], "pagination": {"max_page": 1}}})
    sync_state(db_session, jurisdiction, client=client_v1)
    db_session.flush()

    payload_v2 = _v3_bill_payload(openstates_id="ocd-bill/3", identifier="HB 3", title="Amended title")
    client_v2 = _client_with_pages({1: {"results": [payload_v2], "pagination": {"max_page": 1}}})
    result = sync_state(db_session, jurisdiction, client=client_v2)
    db_session.flush()

    assert result.bills_updated == 1

    bill = db_session.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalar_one()
    assert bill.title == "Amended title"


def test_sync_state_does_not_conflate_same_bill_number_across_sessions(db_session):
    """Regression for Finding A: keying the in-memory bill lookup by
    identifier_norm ALONE (jurisdiction-wide, ignoring session) meant a
    current-session "HB 1" sync could silently overwrite a DIFFERENT,
    historical session's "HB 1" row -- corrupting old sessions' data every
    time a new session reused a low bill number (which is normal;
    legislatures restart numbering every session).

    Two session rows sharing the identifier "HB 1": one seeded as an OLD,
    inactive session (simulating a bill already ingested by a prior bulk
    bootstrap), the other seeded as the CURRENT active session. Syncing a v3
    payload for "HB 1" tagged with the OLD session's identifier must update
    ONLY the old session's bill row, leaving the (as-yet-nonexistent, in
    this test) current-session "HB 1" untouched -- and must NOT create a
    duplicate current-session row keyed by identifier alone.
    """
    abbr = f"ZQ_XSESSION_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="Cross-Session Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()

    old_session = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2024 Session", active=False)
    current_session = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db_session.add(old_session)
    db_session.add(current_session)
    db_session.flush()

    # A bill already ingested (e.g. by a prior bulk-CSV bootstrap) in the OLD
    # session, sharing the identifier "HB 1" with whatever the new session's
    # own "HB 1" will eventually be.
    old_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=old_session.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="Old session's HB 1 -- historical text",
        openstates_id="ocd-bill/old-session-hb1",
    )
    db_session.add(old_bill)
    db_session.flush()
    old_bill_id = old_bill.id

    # v3 payload for the SAME openstates_id, tagged with the OLD session's
    # identifier and an updated title -- a real incremental update to the
    # historical bill, not a new current-session bill.
    payload = _v3_bill_payload(
        openstates_id="ocd-bill/old-session-hb1",
        identifier="HB 1",
        title="Old session's HB 1 -- corrected text",
        session="2024 Session",
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.bills_created == 0, "the old session's existing HB 1 must be UPDATED, not duplicated as a new bill"
    assert result.bills_updated == 1

    all_hb1_rows = db_session.execute(
        select(Bill).where(Bill.jurisdiction_id == jurisdiction.id, Bill.identifier_norm == "HB 1")
    ).scalars().all()
    assert len(all_hb1_rows) == 1, "no duplicate current-session HB 1 row must be created"

    updated_old_bill = db_session.get(Bill, old_bill_id)
    assert updated_old_bill.title == "Old session's HB 1 -- corrected text"
    assert updated_old_bill.session_id == old_session.id, (
        "the update must land on the OLD session's row -- a session-blind identifier_norm "
        "key would have created/updated a row under the (wrong) active/current session instead"
    )


def test_sync_state_warns_when_payload_session_does_not_match_any_known_session(db_session):
    """Regression for Finding 6(b): falling back to the active session when
    a payload's session string doesn't match any known session row used to
    be completely silent -- a real misassignment risk (the bill may
    actually belong to a different, not-yet-seeded session) with no
    operator-visible trace. Must now emit a warning through the same
    `result.warnings` channel the "no session row resolved" skip path
    already uses."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    payload = _v3_bill_payload(
        openstates_id="ocd-bill/mismatch-1",
        identifier="HB 60",
        title="An act",
        session="2099 Nonexistent Session",
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    result = sync_state(db_session, jurisdiction, client=client)

    assert result.bills_created == 1, "the bill must still be created (falls back to active session)"
    assert any(
        "did not match any known" in w and "2099 Nonexistent Session" in w for w in result.warnings
    ), "a session string that matches no known session row must produce a visible warning"


def test_sync_state_does_not_warn_when_payload_session_matches(db_session):
    """Sanity check the other direction: a payload session that DOES match a
    known session row must not spuriously warn."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    payload = _v3_bill_payload(
        openstates_id="ocd-bill/match-1", identifier="HB 61", title="An act", session="2026 Session"
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    result = sync_state(db_session, jurisdiction, client=client)

    assert result.bills_created == 1
    assert not any("did not match any known" in w for w in result.warnings)


def test_sync_state_skips_new_bill_without_active_session(db_session):
    abbr = f"ZQ_NOACTIVE_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="No Active Session State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    # No session rows at all -- a brand-new bill has nowhere to attach.

    payload = _v3_bill_payload(openstates_id="ocd-bill/4", identifier="HB 4", title="An act")
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    result = sync_state(db_session, jurisdiction, client=client)
    assert result.bills_created == 0
    assert any("no session row resolved" in w for w in result.warnings)


def test_sync_state_respects_max_pages_cap(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    # 3 pages available upstream, but max_pages=1 should stop after page 1
    # (quota discipline -- SPEC/brief: default per_page=20, max 10 pages,
    # here capped tighter to make the test cheap and deterministic).
    pages = {
        1: {
            "results": [_v3_bill_payload(openstates_id="ocd-bill/p1", identifier="HB 10", title="Page one bill")],
            "pagination": {"max_page": 3},
        },
        2: {
            "results": [_v3_bill_payload(openstates_id="ocd-bill/p2", identifier="HB 11", title="Page two bill")],
            "pagination": {"max_page": 3},
        },
    }
    client = _client_with_pages(pages)

    result = sync_state(db_session, jurisdiction, client=client, max_pages=1)
    assert result.pages_fetched == 1
    assert result.bills_created == 1


def test_run_api_sync_job_records_ingestion_run(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(openstates_id="ocd-bill/5", identifier="HB 5", title="An act on parks")
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    result = run_api_sync_job(db_session, jurisdiction.abbreviation, client=client)
    db_session.flush()

    assert result.bills_created == 1

    run = db_session.execute(
        select(IngestionRun).where(IngestionRun.jurisdiction_id == jurisdiction.id)
    ).scalar_one()
    assert run.status == "success"
    assert run.bills_created == 1
    assert run.source_name == "openstates_api_sync"


def test_run_api_sync_job_raises_for_unknown_state(db_session):
    with pytest.raises(ValueError):
        run_api_sync_job(db_session, "ZZ_NOT_A_REAL_STATE")


def _client_recording_updated_since(pages: dict, seen_updated_since: list) -> OpenStatesClient:
    def handler(request):
        seen_updated_since.append(dict(request.url.params).get("updated_since"))
        page = int(dict(request.url.params).get("page", "1"))
        return httpx.Response(200, json=pages.get(page, {"results": [], "pagination": {"max_page": page}}))

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://v3.openstates.org")
    return OpenStatesClient(client=http_client, api_key="test-key")


def test_sync_state_watermark_ignores_coverage_recompute_stamps(db_session):
    """Regression for Finding B: the watermark api_sync uses for
    `updated_since` must be THIS pipeline's own last-successful-sync time
    (ingestion_runs), never `jurisdiction_coverage.last_success_at` --
    `coverage.recompute_coverage_row` stamps that field on EVERY
    recompute-coverage pass whenever bill_count > 0, regardless of whether
    an actual sync happened. If api_sync derived updated_since from it, a
    recompute pass running between two real syncs would silently advance
    the watermark and cause upstream changes in that window to be skipped
    forever.

    Simulates exactly that collision: seed a JurisdictionCoverage row with
    `last_success_at` stamped to a time AFTER a real api_sync run actually
    completed (as a mere recompute pass would do), then run api_sync again
    and assert the `updated_since` sent to the API is the REAL last sync
    time from ingestion_runs, not the later, spurious coverage timestamp.
    """
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    real_sync_finished_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add(
        IngestionRun(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            source_name="openstates_api_sync",
            started_at=real_sync_finished_at,
            finished_at=real_sync_finished_at,
            status="success",
        )
    )
    # A coverage recompute pass that ran LATER, with no real sync involved --
    # stamps last_success_at to a time strictly after the real sync.
    spurious_recompute_stamp = datetime(2026, 3, 1, tzinfo=timezone.utc)
    db_session.add(
        JurisdictionCoverage(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            status="BOOTSTRAPPED",
            bill_count=1,
            last_success_at=spurious_recompute_stamp,
        )
    )
    db_session.flush()

    seen_updated_since: list = []
    client = _client_recording_updated_since(
        {1: {"results": [], "pagination": {"max_page": 1}}}, seen_updated_since
    )

    sync_state(db_session, jurisdiction, client=client)

    assert len(seen_updated_since) == 1
    assert seen_updated_since[0] == real_sync_finished_at.isoformat(), (
        "updated_since must come from the last SUCCESSFUL api_sync ingestion_runs row, "
        "not the (later, spurious) jurisdiction_coverage.last_success_at stamp"
    )


def test_sync_state_first_run_has_no_updated_since_watermark(db_session):
    """No prior successful api_sync ingestion_runs row for this jurisdiction
    -- updated_since must be None (a full pull), even if a coverage row
    happens to carry a last_success_at stamp from an unrelated bulk
    bootstrap/recompute pass."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    db_session.add(
        JurisdictionCoverage(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            status="BOOTSTRAPPED",
            bill_count=1,
            last_success_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
    )
    db_session.flush()

    seen_updated_since: list = []
    client = _client_recording_updated_since(
        {1: {"results": [], "pagination": {"max_page": 1}}}, seen_updated_since
    )

    sync_state(db_session, jurisdiction, client=client)

    assert len(seen_updated_since) == 1
    assert seen_updated_since[0] is None


def test_sync_state_continues_past_unchanged_page_when_later_page_has_changes(db_session):
    """Regression for the one-page-cap bug: the old all_unchanged_this_page
    check re-derived "unchanged" AFTER each bill's checksum had already been
    overwritten to match the incoming payload, so a page containing ANY
    created/updated bill still could look "all unchanged" in some runs and,
    worse, a genuinely all-unchanged page could mask real changes further
    on if the sync logic were ever asked to keep going regardless. The
    concrete failure this guards against: pagination silently capping at
    page 1 (~20 bills) on every incremental sync regardless of what's on
    page 1, because the post-mutation re-derivation of "unchanged" could
    never actually observe a changed bill.

    Canned 3-page fixture: page 1 has a bill with a REAL change (must be
    reached and counted as updated -- and per-page mutation must not make
    the early-exit check see it as "unchanged", which is exactly the bug:
    with it, sync would have stopped after page 1 on EVERY run regardless
    of content). Page 2 is genuinely unchanged, which correctly stops
    pagination there by design (v3 sorts newest-updated-first, so an
    all-unchanged page means everything further back is stale) -- page 3's
    brand-new bill is intentionally never reached, proving the early-exit
    still fires on a REAL all-unchanged page and isn't simply disabled by
    this fix.
    """
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    # Pre-seed a bill on page 1 with a STALE checksum -- the incoming page-1
    # payload has a different title, so this must be picked up as a real
    # change. This is the exact case the bug broke: right after this bill's
    # checksum field is reassigned to match the new payload (as part of
    # applying the update), a naive post-hoc recheck of
    # "bill.checksum == _bill_checksum(payload)" is trivially true again,
    # making a page that had a real change look "all unchanged".
    stale_payload_for_seed = _v3_bill_payload(
        openstates_id="ocd-bill/page1", identifier="HB 100", title="Original title"
    )
    changed_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 100",
        identifier_norm="HB 100",
        title="Original title",
        checksum=_bill_checksum(stale_payload_for_seed),
    )
    db_session.add(changed_bill)
    db_session.flush()

    page1_payload = _v3_bill_payload(
        openstates_id="ocd-bill/page1", identifier="HB 100", title="Amended title"
    )

    # Pre-seed a bill on page 2 whose checksum already matches its payload
    # (genuinely unchanged).
    unchanged_payload = _v3_bill_payload(
        openstates_id="ocd-bill/page2", identifier="HB 101", title="Unchanged bill"
    )
    unchanged_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 101",
        identifier_norm="HB 101",
        title="Unchanged bill",
        checksum=_bill_checksum(unchanged_payload),
    )
    db_session.add(unchanged_bill)
    db_session.flush()

    page3_payload = _v3_bill_payload(
        openstates_id="ocd-bill/page3", identifier="HB 102", title="Brand new bill on page 3"
    )

    pages = {
        1: {"results": [page1_payload], "pagination": {"max_page": 3}},
        2: {"results": [unchanged_payload], "pagination": {"max_page": 3}},
        3: {"results": [page3_payload], "pagination": {"max_page": 3}},
    }
    client = _client_with_pages(pages)

    result = sync_state(db_session, jurisdiction, client=client, max_pages=10)

    assert result.pages_fetched == 2, (
        "page 1 had a real change so it must be fetched and applied (the one-page-cap bug would "
        "have made page 1 look all-unchanged regardless); page 2 is genuinely all-unchanged so "
        "the early-exit correctly stops there without wasting quota on page 3"
    )
    assert result.bills_updated == 1
    assert result.bills_unchanged == 1
    assert result.bills_created == 0, "page 3's new bill must NOT be reached -- the genuine early-exit still works"

    updated = db_session.execute(
        select(Bill).where(Bill.jurisdiction_id == jurisdiction.id, Bill.identifier_norm == "HB 100")
    ).scalar_one()
    assert updated.title == "Amended title", "page 1's real change must actually be applied"


# ---------------------------------------------------------------------------
# Finding 6(a): watermark uses started_at, not finished_at
# ---------------------------------------------------------------------------


def test_sync_state_watermark_uses_started_at_not_finished_at(db_session):
    """Regression for Finding 6(a): using `finished_at` as the `updated_since`
    watermark creates a real gap-of-loss window. A run that takes real
    wall-clock time to complete (paging through the v3 API) could have an
    upstream bill update land ON THE OPEN STATES SIDE mid-run -- after this
    run's own `search_bills` calls already fetched their pages, but before
    `finished_at` is stamped. That update is newer than `started_at` (so a
    NEXT run keyed on started_at would still ask about it) but older than
    `finished_at` (so a next run keyed on finished_at would treat it as
    already covered, even though this run's calls never actually saw it).

    Seeds an ingestion_runs row with DISTINCT started_at/finished_at (a real
    multi-minute run) and asserts the next sync's `updated_since` is the
    EARLIER started_at value, not the later finished_at."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    run_started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    run_finished_at = datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)  # 5 minutes later
    db_session.add(
        IngestionRun(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            source_name="openstates_api_sync",
            started_at=run_started_at,
            finished_at=run_finished_at,
            status="success",
        )
    )
    db_session.flush()

    seen_updated_since: list = []
    client = _client_recording_updated_since(
        {1: {"results": [], "pagination": {"max_page": 1}}}, seen_updated_since
    )

    sync_state(db_session, jurisdiction, client=client)

    assert len(seen_updated_since) == 1
    assert seen_updated_since[0] == run_started_at.isoformat(), (
        "updated_since must be the last successful run's started_at, not its finished_at -- "
        "using finished_at would silently skip upstream changes that landed during the run"
    )


# ---------------------------------------------------------------------------
# Finding 6(c): unchanged-checksum branch backfills openstates_id
# ---------------------------------------------------------------------------


def test_sync_state_backfills_openstates_id_on_unchanged_bill(db_session):
    """Regression for Finding 6(c): a bulk-CSV-bootstrapped bill (no
    openstates_id yet, matched via the SECONDARY session+identifier_norm
    key) whose core fields are checksum-identical to the incoming v3 payload
    must still get openstates_id backfilled onto it -- otherwise a bill that
    stops changing (a real, plausible steady state once it's enacted/dead)
    NEVER graduates to the PRIMARY openstates_id dedup key, and every future
    sync keeps falling back to the weaker secondary key indefinitely."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    payload = _v3_bill_payload(openstates_id="ocd-bill/backfill-1", identifier="HB 55", title="A settled bill")
    # Seed a bulk-CSV-bootstrapped row: same checksum-relevant fields as the
    # payload (so this sync sees it as "unchanged"), but NO openstates_id --
    # exactly the shape a bulk bootstrap produces before api_sync ever runs.
    bootstrapped_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 55",
        identifier_norm="HB 55",
        title="A settled bill",
        checksum=_bill_checksum(payload),
        openstates_id=None,
    )
    db_session.add(bootstrapped_bill)
    db_session.flush()
    bootstrapped_bill_id = bootstrapped_bill.id

    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.bills_unchanged == 1, "checksum-identical core fields must still classify as unchanged"
    assert result.bills_created == 0
    assert result.bills_updated == 0

    refreshed = db_session.get(Bill, bootstrapped_bill_id)
    assert refreshed.openstates_id == "ocd-bill/backfill-1", (
        "openstates_id must be backfilled onto an unchanged bulk-bootstrapped row so it "
        "graduates to the primary dedup key instead of staying on the secondary key forever"
    )


def test_sync_state_unchanged_backfill_does_not_touch_already_resolved_bill(db_session):
    """An unchanged bill that ALREADY has an openstates_id (the normal
    steady state for any bill api_sync has touched before) must not have it
    overwritten to a different value from a payload that -- despite an
    identical checksum -- claims a different id; this would only happen for
    a genuinely different bill colliding on checksum, which should never
    silently reassign an already-resolved bill's identity."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    payload = _v3_bill_payload(openstates_id="ocd-bill/already-resolved", identifier="HB 56", title="A settled bill")
    resolved_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 56",
        identifier_norm="HB 56",
        title="A settled bill",
        checksum=_bill_checksum(payload),
        openstates_id="ocd-bill/already-resolved",
    )
    db_session.add(resolved_bill)
    db_session.flush()
    resolved_bill_id = resolved_bill.id

    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.bills_unchanged == 1

    refreshed = db_session.get(Bill, resolved_bill_id)
    assert refreshed.openstates_id == "ocd-bill/already-resolved"
