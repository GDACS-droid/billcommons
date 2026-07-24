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

import httpx
import pytest
from sqlalchemy import select

from billcommons_ingest.api_sync import run_api_sync_job, sync_state
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


def _v3_bill_payload(*, openstates_id, identifier, title, action_description="Introduced", action_date="2026-01-05"):
    return {
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
    assert any("no active session" in w for w in result.warnings)


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
