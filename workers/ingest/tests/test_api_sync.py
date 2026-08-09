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
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, text

from billcommons_ingest import cli as cli_mod
from billcommons_ingest.api_sync import ApiSyncResult, _bill_checksum, run_api_sync_job, sync_state
from billcommons_ingest.fulltext import FETCH_TEXT_KIND, enqueue_fulltext_jobs
from billcommons_ingest.openstates_api import OpenStatesClient
from billcommons_shared.db import get_session as real_get_session
from billcommons_schema.models import (
    Bill,
    BillAction,
    BillDocument,
    BillVersion,
    IngestJob,
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
    versions=None,
    documents=None,
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
    if versions is not None:
        payload["versions"] = versions
    if documents is not None:
        payload["documents"] = documents
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


# ---------------------------------------------------------------------------
# Versions/documents repair: api_sync must materialize bill_versions/
# bill_documents from the v3 `versions`/`documents` includes, using the exact
# same natural keys and placeholder-version convention as
# openstates_bulk.ingest_session_csv_zip.
# ---------------------------------------------------------------------------


def _client_capturing_params(pages: dict, seen_params: list) -> OpenStatesClient:
    """Like `_client_with_pages`, but appends each request's raw
    `httpx.QueryParams` (not a plain dict) to `seen_params` -- a plain
    `dict(request.url.params)` collapses a repeated query key like
    `include=versions&include=documents` down to its LAST value only,
    which would hide exactly the serialization this module's tests need to
    assert (`QueryParams.get_list` preserves every repeated value)."""

    def handler(request):
        seen_params.append(request.url.params)
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=pages.get(page, {"results": [], "pagination": {"max_page": page}}))

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://v3.openstates.org")
    return OpenStatesClient(client=http_client, api_key="test-key")


def test_sync_state_requests_and_persists_versions_documents(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(
        openstates_id="ocd-bill/v1",
        identifier="HB 200",
        title="An act on schools",
        versions=[
            {
                "note": "Introduced",
                "date": "2026-01-05",
                "links": [
                    {"url": "https://example.gov/hb200-intro.pdf", "media_type": "application/pdf"},
                    {"url": "https://example.gov/hb200-intro.html", "media_type": "text/html"},
                ],
            }
        ],
        documents=[
            {"note": "Fiscal Note", "links": [{"url": "https://example.gov/hb200-fn.pdf", "media_type": "application/pdf"}]},
            {"note": "Staff Analysis", "links": [{"url": "https://example.gov/hb200-an.pdf", "media_type": "application/pdf"}]},
        ],
    )
    seen_params: list = []
    client = _client_capturing_params({1: {"results": [payload], "pagination": {"max_page": 1}}}, seen_params)

    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert len(seen_params) == 1
    includes = seen_params[0].get_list("include")
    assert "versions" in includes
    assert "documents" in includes
    assert "sponsorships" in includes
    assert "actions" in includes
    assert "sources" in includes

    assert result.versions == 1, "one real upstream version, not counting the synthetic placeholder"
    assert result.documents == 4, "2 links on the real version + 2 standalone document links"

    bill = db_session.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalar_one()
    versions = db_session.execute(select(BillVersion).where(BillVersion.bill_id == bill.id)).scalars().all()
    assert len(versions) == 2, "1 real version + 1 synthetic placeholder for the 2 standalone documents"

    real_version = next(v for v in versions if v.note == "Introduced")
    placeholder = next(v for v in versions if v.note == "(document, no version)")
    assert real_version.date == date(2026, 1, 5)
    assert real_version.source_name == "openstates_api_sync"
    assert real_version.retrieved_at is not None
    assert placeholder.date is None
    assert placeholder.license_note == "synthetic placeholder: doc had no matching version row"

    real_docs = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id == real_version.id)
    ).scalars().all()
    assert {d.url for d in real_docs} == {
        "https://example.gov/hb200-intro.pdf",
        "https://example.gov/hb200-intro.html",
    }
    for d in real_docs:
        assert d.source_name == "openstates_api_sync"
        assert d.retrieved_at is not None

    placeholder_docs = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id == placeholder.id)
    ).scalars().all()
    assert {d.url for d in placeholder_docs} == {
        "https://example.gov/hb200-fn.pdf",
        "https://example.gov/hb200-an.pdf",
    }


def test_sync_state_skips_falsy_link_urls(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(
        openstates_id="ocd-bill/v2",
        identifier="HB 201",
        title="An act on parks",
        versions=[
            {
                "note": "Introduced",
                "date": "2026-01-05",
                "links": [
                    {"url": "", "media_type": "application/pdf"},
                    {"url": "https://example.gov/hb201-intro.pdf", "media_type": "application/pdf"},
                ],
            }
        ],
        documents=[
            {"note": "Fiscal Note", "links": [{"url": None, "media_type": "application/pdf"}]},
        ],
    )
    client = _client_capturing_params({1: {"results": [payload], "pagination": {"max_page": 1}}}, [])

    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.documents == 1, "only the valid version link counts; url-less links are skipped"

    bill = db_session.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalar_one()
    versions = db_session.execute(select(BillVersion).where(BillVersion.bill_id == bill.id)).scalars().all()
    assert len(versions) == 1, "no synthetic placeholder version -- the only standalone document link had no url"
    assert versions[0].note == "Introduced"

    docs = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id == versions[0].id)
    ).scalars().all()
    assert {d.url for d in docs} == {"https://example.gov/hb201-intro.pdf"}


def test_sync_state_version_document_upsert_is_idempotent(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(
        openstates_id="ocd-bill/v2",
        identifier="HB 201",
        title="An act on roads",
        versions=[
            {
                "note": "Introduced",
                "date": "2026-01-05",
                "links": [{"url": "https://example.gov/hb201-intro.pdf", "media_type": "application/pdf"}],
            }
        ],
        documents=[{"note": "Fiscal Note", "links": [{"url": "https://example.gov/hb201-fn.pdf", "media_type": "application/pdf"}]}],
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    first = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()
    assert first.versions == 1
    assert first.documents == 2

    bill = db_session.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalar_one()
    versions_before = db_session.execute(select(BillVersion).where(BillVersion.bill_id == bill.id)).scalars().all()
    docs_before = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id.in_([v.id for v in versions_before]))
    ).scalars().all()
    assert len(versions_before) == 2
    assert len(docs_before) == 2

    client2 = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    second = sync_state(db_session, jurisdiction, client=client2)
    db_session.flush()

    assert second.versions == 0
    assert second.documents == 0

    versions_after = db_session.execute(select(BillVersion).where(BillVersion.bill_id == bill.id)).scalars().all()
    docs_after = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id.in_([v.id for v in versions_after]))
    ).scalars().all()
    assert len(versions_after) == 2, "no duplicate placeholder or real version on rerun"
    assert len(docs_after) == 2
    assert {(v.id, v.note) for v in versions_after} == {(v.id, v.note) for v in versions_before}
    assert {(d.id, d.url) for d in docs_after} == {(d.id, d.url) for d in docs_before}


def test_sync_state_adds_amended_version_to_core_unchanged_bootstrap_bill(db_session):
    """Direct regression test for the production blind spot: a bill whose
    core checksum is unchanged (so the OLD code never even looked at
    versions/documents) must still pick up a new version/link that appeared
    upstream since bootstrap."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    base_payload = _v3_bill_payload(
        openstates_id="ocd-bill/amend-1", identifier="SB 200", title="An act on ethics"
    )
    bootstrapped_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="SB 200",
        identifier_norm="SB 200",
        title="An act on ethics",
        checksum=_bill_checksum(base_payload),
        openstates_id="ocd-bill/amend-1",
    )
    db_session.add(bootstrapped_bill)
    db_session.flush()

    existing_version = BillVersion(
        bill_id=bootstrapped_bill.id,
        note="Introduced",
        date=date(2026, 1, 5),
        source_name="openstates_bulk_csv",
        retrieved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    db_session.add(existing_version)
    db_session.flush()

    payload = _v3_bill_payload(
        openstates_id="ocd-bill/amend-1",
        identifier="SB 200",
        title="An act on ethics",
        versions=[
            {"note": "Introduced", "date": "2026-01-05", "links": []},
            {
                "note": "08/06/26 - Amended Senate",
                "date": "2026-08-06",
                "links": [{"url": "https://example.gov/sb200-amended.pdf", "media_type": "application/pdf"}],
            },
        ],
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})

    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.bills_unchanged == 1, "the core checksum genuinely does not change"
    assert result.bills_created == 0
    assert result.bills_updated == 0
    assert result.versions == 1, "only the newly-appeared amended version, not the pre-existing Introduced version"
    assert result.documents == 1

    versions = db_session.execute(
        select(BillVersion).where(BillVersion.bill_id == bootstrapped_bill.id)
    ).scalars().all()
    assert {v.note for v in versions} == {"Introduced", "08/06/26 - Amended Senate"}

    client2 = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    second = sync_state(db_session, jurisdiction, client=client2)
    db_session.flush()
    assert second.versions == 0
    assert second.documents == 0
    versions_after = db_session.execute(
        select(BillVersion).where(BillVersion.bill_id == bootstrapped_bill.id)
    ).scalars().all()
    assert len(versions_after) == 2


def test_sync_state_adds_new_link_to_existing_version_without_duplicate_version(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    base_payload = _v3_bill_payload(openstates_id="ocd-bill/link-1", identifier="HB 300", title="An act on parks")
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 300",
        identifier_norm="HB 300",
        title="An act on parks",
        checksum=_bill_checksum(base_payload),
        openstates_id="ocd-bill/link-1",
    )
    db_session.add(bill)
    db_session.flush()

    existing_version = BillVersion(
        bill_id=bill.id,
        note="Introduced",
        date=date(2026, 1, 5),
        source_name="openstates_bulk_csv",
        retrieved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    db_session.add(existing_version)
    db_session.flush()
    db_session.add(
        BillDocument(
            bill_version_id=existing_version.id,
            url="https://example.gov/hb300.pdf",
            media_type="application/pdf",
            source_name="openstates_bulk_csv",
            retrieved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        )
    )
    db_session.flush()

    payload = _v3_bill_payload(
        openstates_id="ocd-bill/link-1",
        identifier="HB 300",
        title="An act on parks",
        versions=[
            {
                "note": "Introduced",
                "date": "2026-01-05",
                "links": [
                    {"url": "https://example.gov/hb300.pdf", "media_type": "application/pdf"},
                    {"url": "https://example.gov/hb300.html", "media_type": "text/html"},
                ],
            }
        ],
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.versions == 0, "the (bill_id, note, date) version already exists -- must not duplicate"
    assert result.documents == 1, "only the new URL format is a new document"

    versions = db_session.execute(select(BillVersion).where(BillVersion.bill_id == bill.id)).scalars().all()
    assert len(versions) == 1
    docs = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id == existing_version.id)
    ).scalars().all()
    assert {d.url for d in docs} == {"https://example.gov/hb300.pdf", "https://example.gov/hb300.html"}


def test_sync_state_standalone_documents_share_one_placeholder(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(
        openstates_id="ocd-bill/orphan-1",
        identifier="HB 400",
        title="An act on trails",
        documents=[
            {"note": "Fiscal Note", "links": [{"url": "https://example.gov/hb400-fn.pdf", "media_type": "application/pdf"}]},
            {
                "note": "Analysis",
                "links": [
                    {"url": "https://example.gov/hb400-an.pdf", "media_type": "application/pdf"},
                    {"url": "https://example.gov/hb400-an.html", "media_type": "text/html"},
                ],
            },
        ],
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    result = sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    assert result.versions == 0
    assert result.documents == 3

    bill = db_session.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalar_one()
    placeholders = db_session.execute(
        select(BillVersion).where(BillVersion.bill_id == bill.id, BillVersion.note == "(document, no version)")
    ).scalars().all()
    assert len(placeholders) == 1, "multiple standalone document objects must share ONE placeholder version"
    docs = db_session.execute(
        select(BillDocument).where(BillDocument.bill_version_id == placeholders[0].id)
    ).scalars().all()
    assert {d.url for d in docs} == {
        "https://example.gov/hb400-fn.pdf",
        "https://example.gov/hb400-an.pdf",
        "https://example.gov/hb400-an.html",
    }

    client2 = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    second = sync_state(db_session, jurisdiction, client=client2)
    db_session.flush()
    assert second.versions == 0
    assert second.documents == 0
    placeholders_after = db_session.execute(
        select(BillVersion).where(BillVersion.bill_id == bill.id, BillVersion.note == "(document, no version)")
    ).scalars().all()
    assert len(placeholders_after) == 1


def test_api_synced_documents_are_eligible_for_existing_fulltext_enqueue(db_session):
    """Proves the natural integration: api_sync inserts bill_documents rows
    exactly like the bulk path, and the EXISTING enqueue_fulltext_jobs (no
    new code in api_sync itself) discovers and enqueues them."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    payload = _v3_bill_payload(
        openstates_id="ocd-bill/fulltext-1",
        identifier="HB 500",
        title="An act on libraries",
        versions=[
            {
                "note": "Introduced",
                "date": "2026-01-05",
                "links": [{"url": "https://example.gov/hb500-intro.pdf", "media_type": "application/pdf"}],
            }
        ],
        documents=[{"note": "Fiscal Note", "links": [{"url": "https://example.gov/hb500-fn.pdf", "media_type": "application/pdf"}]}],
    )
    client = _client_with_pages({1: {"results": [payload], "pagination": {"max_page": 1}}})
    sync_state(db_session, jurisdiction, client=client)
    db_session.flush()

    bill = db_session.execute(select(Bill).where(Bill.jurisdiction_id == jurisdiction.id)).scalar_one()
    document_ids = [
        d.id
        for d in db_session.execute(
            select(BillDocument)
            .join(BillVersion, BillDocument.bill_version_id == BillVersion.id)
            .where(BillVersion.bill_id == bill.id)
        ).scalars()
    ]
    assert len(document_ids) == 2

    added = enqueue_fulltext_jobs(db_session, document_ids=document_ids)
    db_session.flush()
    assert added == 2

    jobs = db_session.execute(
        select(IngestJob).where(
            IngestJob.kind == FETCH_TEXT_KIND,
            IngestJob.payload["document_id"].astext.in_([str(d) for d in document_ids]),
        )
    ).scalars().all()
    assert len(jobs) == 2
    assert {j.payload.get("document_id") for j in jobs} == {str(d) for d in document_ids}

    second_added = enqueue_fulltext_jobs(db_session, document_ids=document_ids)
    db_session.flush()
    assert second_added == 0


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
    """Regression for the child-row pagination blind spot: a page where
    EVERY bill's core checksum is unchanged is NOT evidence that later pages
    hold no changes, because a version/document can change upstream without
    moving `_bill_checksum`'s fields (identifier/title/classification/
    latest-action). The old code's `all_unchanged_this_page` early exit
    stopped pagination on exactly such a page -- silently missing later
    pages' real changes, including version-only deltas. That early exit is
    now REMOVED entirely: pagination stops only on an empty page, upstream's
    own `pagination.max_page`, or the page budget.

    Canned 3-page fixture: page 1 has a bill with a core-checksum-unchanged
    payload that nonetheless carries a NEW version (must still be reached
    and its version upserted -- this is the direct case the old early exit
    would have masked if it had ever tried to look past page 1). Page 2 has
    a bill with a real core-field CHANGE. Page 3 has a brand-new bill. All
    three pages must now be processed -- proving a core-unchanged page no
    longer terminates upstream pagination.
    """
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    # Page 1: core-unchanged bill, but the incoming payload carries a NEW
    # version the old early-exit's page-1-is-"all unchanged" check would
    # have made pagination stop before ever reaching page 2/3.
    unchanged_base_payload = _v3_bill_payload(
        openstates_id="ocd-bill/page1", identifier="HB 100", title="Same title throughout"
    )
    unchanged_core_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 100",
        identifier_norm="HB 100",
        title="Same title throughout",
        checksum=_bill_checksum(unchanged_base_payload),
    )
    db_session.add(unchanged_core_bill)
    db_session.flush()

    page1_payload = _v3_bill_payload(
        openstates_id="ocd-bill/page1",
        identifier="HB 100",
        title="Same title throughout",
        versions=[
            {
                "note": "Amended",
                "date": "2026-02-01",
                "links": [{"url": "https://example.gov/hb100-amended.pdf", "media_type": "application/pdf"}],
            }
        ],
    )

    # Page 2: a bill with a REAL core-field change (title differs from its
    # stored checksum).
    stale_payload_for_seed = _v3_bill_payload(
        openstates_id="ocd-bill/page2", identifier="HB 101", title="Original title"
    )
    changed_bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 101",
        identifier_norm="HB 101",
        title="Original title",
        checksum=_bill_checksum(stale_payload_for_seed),
    )
    db_session.add(changed_bill)
    db_session.flush()

    page2_payload = _v3_bill_payload(
        openstates_id="ocd-bill/page2", identifier="HB 101", title="Amended title"
    )

    # Page 3: a brand-new bill -- must now actually be reached.
    page3_payload = _v3_bill_payload(
        openstates_id="ocd-bill/page3", identifier="HB 102", title="Brand new bill on page 3"
    )

    pages = {
        1: {"results": [page1_payload], "pagination": {"max_page": 3}},
        2: {"results": [page2_payload], "pagination": {"max_page": 3}},
        3: {"results": [page3_payload], "pagination": {"max_page": 3}},
    }
    client = _client_with_pages(pages)

    result = sync_state(db_session, jurisdiction, client=client, max_pages=10)

    assert result.pages_fetched == 3, (
        "a core-unchanged page 1 must NOT stop pagination -- pages 2 and 3 must both be fetched"
    )
    assert result.bills_unchanged == 1, "page 1's bill has no core-field change"
    assert result.bills_updated == 1, "page 2's bill has a real core-field change"
    assert result.bills_created == 1, "page 3's brand-new bill must actually be reached and created"
    assert result.versions == 1, "page 1's unchanged bill still got its new version upserted"
    assert result.next_page is None, "upstream's own max_page (3) was reached, not the budget"

    updated = db_session.execute(
        select(Bill).where(Bill.jurisdiction_id == jurisdiction.id, Bill.identifier_norm == "HB 101")
    ).scalar_one()
    assert updated.title == "Amended title", "page 2's real change must actually be applied"

    unchanged_versions = db_session.execute(
        select(BillVersion).where(BillVersion.bill_id == unchanged_core_bill.id)
    ).scalars().all()
    assert {v.note for v in unchanged_versions} == {"Amended"}


def test_sync_state_explicit_since_start_page_and_budget(db_session):
    """The catch-up/replay path's `updated_since_override`/`start_page`/
    `max_pages` contract: `max_pages` bounds THIS call's own request count
    starting at `start_page` (not an absolute page number), the override is
    sent verbatim on every request in the chunk, and truncation vs.
    completion is reported via `next_page`/`max_page_seen` rather than
    silently assumed either way."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)

    def _payload_for_page(n):
        return _v3_bill_payload(openstates_id=f"ocd-bill/replay-{n}", identifier=f"HB {900 + n}", title=f"Bill {n}")

    pages = {n: {"results": [_payload_for_page(n)], "pagination": {"max_page": 7}} for n in range(1, 8)}
    seen_params: list = []
    client = _client_capturing_params(pages, seen_params)

    result = sync_state(
        db_session,
        jurisdiction,
        client=client,
        updated_since_override="2026-07-24T05:42:31.004956+00:00",
        start_page=3,
        max_pages=2,
    )

    assert [int(p.get("page", "1")) for p in seen_params] == [3, 4], (
        "a chunk with start_page=3, max_pages=2 must request exactly pages 3 and 4, not 1 and 2"
    )
    for p in seen_params:
        assert p.get("updated_since") == "2026-07-24T05:42:31.004956+00:00", (
            "every request in the chunk must use the exact override, unchanged"
        )
    assert result.pages_fetched == 2
    assert result.max_page_seen == 7
    assert result.next_page == 5, "budget (2 pages) ended before upstream's max_page (7) -- resume at page 5"

    # A second chunk whose requested pages actually reach upstream's max_page
    # (7) must report next_page=None -- genuinely caught up, not merely
    # stopped by budget.
    seen_params2: list = []
    client2 = _client_capturing_params(pages, seen_params2)
    result2 = sync_state(
        db_session,
        jurisdiction,
        client=client2,
        updated_since_override="2026-07-24T05:42:31.004956+00:00",
        start_page=6,
        max_pages=2,
    )
    assert [int(p.get("page", "1")) for p in seen_params2] == [6, 7]
    assert result2.pages_fetched == 2
    assert result2.max_page_seen == 7
    assert result2.next_page is None, "the chunk's own last page (7) reached upstream's max_page -- complete"


def test_sync_state_override_does_not_read_or_use_normal_watermark(db_session):
    """`updated_since_override` must be used VERBATIM in place of the
    computed watermark, and must not itself read/derive
    `ingestion_runs.started_at` -- the catch-up/replay path must never be
    influenced by (or, per the CLI layer, ever advance) the ordinary
    incremental-sync watermark."""
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    # A prior successful api_sync run exists with a DIFFERENT started_at --
    # if the override were ignored, updated_since would come from here.
    db_session.add(
        IngestionRun(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            source_name="openstates_api_sync",
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            status="success",
        )
    )
    db_session.flush()

    seen_updated_since: list = []
    client = _client_recording_updated_since(
        {1: {"results": [], "pagination": {"max_page": 1}}}, seen_updated_since
    )

    sync_state(
        db_session,
        jurisdiction,
        client=client,
        updated_since_override="2026-07-24T05:42:31.004956+00:00",
    )

    assert seen_updated_since == ["2026-07-24T05:42:31.004956+00:00"], (
        "the override must be used verbatim, not the ordinary watermark from ingestion_runs"
    )


def test_sync_state_rejects_invalid_start_page_and_max_pages(db_session):
    jurisdiction, session_row = _make_jurisdiction_with_active_session(db_session)
    client = _client_with_pages({1: {"results": [], "pagination": {"max_page": 1}}})
    with pytest.raises(ValueError):
        sync_state(db_session, jurisdiction, client=client, start_page=0)
    with pytest.raises(ValueError):
        sync_state(db_session, jurisdiction, client=client, max_pages=0)


# ---------------------------------------------------------------------------
# backfill-api-versions CLI command (cli.run_api_versions_backfill)
# ---------------------------------------------------------------------------


def test_backfill_command_commits_chunks_and_does_not_advance_normal_watermark(monkeypatch):
    """Injects a fake `api_sync_mod.sync_state` (no real network/DB writes
    from the sync itself -- that logic is covered elsewhere in this file)
    to isolate the CHUNKING ORCHESTRATION this command owns: one shared
    client across every chunk, one committing session per chunk closed
    before the next chunk starts, a later chunk's failure rolled back and
    reported as a resume point WITHOUT discarding earlier committed chunks,
    and -- the whole point of this command existing separately from
    `api-sync` -- never writing a normal-watermark
    `ingestion_runs(source_name='openstates_api_sync')` row.

    Runs against the real DB (like test_fulltext.py's `session_factory=
    get_session` tests) because the durability claim ("earlier chunks
    survive a later chunk's rollback") is only meaningful across separate,
    really-committed transactions -- a single savepoint-scoped `db_session`
    can't distinguish that from an in-memory list.
    """
    abbr = f"ZQ_BACKFILL_{uuid.uuid4().hex[:8].upper()}"
    setup = real_get_session()
    try:
        jurisdiction = Jurisdiction(name="Backfill Test State", abbreviation=abbr, classification="state")
        setup.add(jurisdiction)
        setup.flush()
        session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
        setup.add(session_row)
        setup.commit()
        jurisdiction_id = jurisdiction.id
    finally:
        setup.close()

    try:
        calls: list = []

        def fake_sync_state(db, jurisdiction_arg, *, client, max_pages, updated_since_override, start_page):
            calls.append(
                {
                    "client": client,
                    "max_pages": max_pages,
                    "updated_since_override": updated_since_override,
                    "start_page": start_page,
                }
            )
            if start_page == 3:
                raise RuntimeError("simulated 502 on page 3")
            result = ApiSyncResult(state=jurisdiction_arg.abbreviation)
            result.pages_fetched = 1
            result.max_page_seen = 3
            result.next_page = start_page + 1 if start_page < 3 else None
            return result

        monkeypatch.setattr(cli_mod.api_sync_mod, "sync_state", fake_sync_state)

        sentinel_client = object()
        result = cli_mod.run_api_versions_backfill(
            abbr,
            "2026-07-24T05:42:31.004956+00:00",
            start_page=1,
            page_budget=3,
            commit_pages=1,
            client=sentinel_client,
        )

        assert result["status"] == "error"
        assert result["resume_page"] == 3, "the FAILING chunk's own starting page is the resume point"
        assert len(calls) == 3
        assert all(c["client"] is sentinel_client for c in calls), (
            "the SAME client instance must serve every chunk's calls -- never one client per chunk/page"
        )
        assert [c["start_page"] for c in calls] == [1, 2, 3]
        assert all(
            c["updated_since_override"] == "2026-07-24T05:42:31.004956+00:00" for c in calls
        ), "the override must be passed unchanged to every chunk"
        assert result["pages_fetched"] == 2, "only the two SUCCESSFUL (durable) chunks count toward the totals"

        check = real_get_session()
        try:
            runs = check.execute(
                select(IngestionRun).where(
                    IngestionRun.jurisdiction_id == jurisdiction_id,
                    IngestionRun.source_name == "openstates_api_sync",
                )
            ).scalars().all()
            assert runs == [], (
                "backfill-api-versions must never write a normal-watermark "
                "ingestion_runs(source_name='openstates_api_sync') row"
            )
        finally:
            check.close()
    finally:
        cleanup = real_get_session()
        try:
            cleanup.execute(text("DELETE FROM sessions WHERE jurisdiction_id=:j"), {"j": jurisdiction_id})
            cleanup.execute(text("DELETE FROM jurisdictions WHERE id=:j"), {"j": jurisdiction_id})
            cleanup.commit()
        finally:
            cleanup.close()


def test_backfill_command_help_lists_all_five_arguments():
    parser = cli_mod.build_parser()
    subparsers_action = next(
        a for a in parser._subparsers._group_actions if hasattr(a, "choices")  # noqa: SLF001
    )
    backfill_parser = subparsers_action.choices["backfill-api-versions"]
    dest_names = {action.dest for action in backfill_parser._actions}  # noqa: SLF001
    assert {"state", "since", "start_page", "page_budget", "commit_pages"} <= dest_names


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


# --- cmd_sync_worker budget-deferral (Change 4) ---------------------------
#
# There is no existing cmd_sync_worker test in this suite (it is a
# long-running worker loop, not something the rest of this file exercises),
# so per the spec these test the smallest extracted helpers cmd_sync_worker
# calls on OpenStatesDailyBudgetExceeded -- `_next_utc_midnight_with_jitter`
# and `defer_job_for_budget` -- against a fully stubbed session, never a
# real IngestJob row or the live DB.
#
# The concurrency regression test below is the FakeSession-simulation
# variant, not two real sessions against the live DB: this suite's
# db_session fixture (conftest.py) gives every test exactly one connection
# wrapped in an outer transaction + SAVEPOINT that always rolls back, so a
# second, genuinely independent, durably-committing session isn't available
# without either opening a raw second connection directly against the live
# shared DB (real writes to a real ingest_jobs row that a real worker could
# also claim -- exactly the kind of prod-DB side effect this task's hard
# rules say to avoid) or bypassing the fixture's isolation. Simulating the
# contested state (mutate the fake job to look like a second-claimer already
# has it, then call defer_job_for_budget and assert it declines) exercises
# the same guard without either risk.


class _FakeJob:
    def __init__(self, job_id, attempts, status="running", locked_by="worker-1"):
        self.id = job_id
        self.attempts = attempts
        self.status = status
        self.run_after = None
        self.locked_by = locked_by
        self.locked_at = datetime(2026, 8, 8, tzinfo=timezone.utc)


class _FakeSession:
    def __init__(self, job):
        self._job = job
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def get(self, cls, job_id, with_for_update=False):
        return self._job if job_id == self._job.id else None

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_next_utc_midnight_with_jitter_is_tomorrow_within_five_minutes():
    now = datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc)
    run_after = cli_mod._next_utc_midnight_with_jitter(now=now)

    midnight = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)
    assert midnight <= run_after <= midnight + timedelta(minutes=5)


def test_defer_job_for_budget_requeues_without_burning_an_attempt():
    # claim_job bumped attempts 1 -> 2, then the claiming transaction rolled
    # back on OpenStatesDailyBudgetExceeded -- which puts the row back to
    # exactly this state: queued, unlocked, attempts reverted to 1.
    job = _FakeJob(job_id="job-1", attempts=1, status="queued", locked_by=None)
    job.locked_at = None
    session = _FakeSession(job)
    tomorrow = datetime(2026, 8, 9, 0, 3, tzinfo=timezone.utc)

    deferred = cli_mod.defer_job_for_budget(
        "job-1",
        object,
        claimed_attempts=2,
        run_after=tomorrow,
        session_factory=lambda: session,
    )

    assert deferred is True
    assert job.status == "queued"
    assert job.run_after == tomorrow
    assert job.attempts == 1, "net attempts across the cycle must be unchanged"
    assert job.locked_by is None
    assert session.committed is True
    assert session.closed is True


def test_defer_job_for_budget_missing_job_still_commits_and_closes():
    job = _FakeJob(job_id="job-1", attempts=1, status="queued", locked_by=None)
    session = _FakeSession(job)

    deferred = cli_mod.defer_job_for_budget(
        "some-other-job",
        object,
        claimed_attempts=1,
        run_after=datetime(2026, 8, 9, tzinfo=timezone.utc),
        session_factory=lambda: session,
    )

    assert deferred is False
    assert session.committed is True
    assert session.closed is True
    assert job.status == "queued"  # untouched -- id didn't match
    assert job.run_after is None


def test_defer_job_for_budget_declines_when_row_reclaimed_concurrently():
    """Regression for the codex-verify finding: between this job's claiming
    transaction rolling back (row -> queued, attempts=1, unlocked) and
    defer_job_for_budget's fresh transaction locking the row, a second
    sync-worker claimed it (status='running', attempts back to 2,
    locked_by='worker-2'). Writing the deferral unconditionally would
    clobber that worker's claim; the guard must leave the row untouched and
    report it as not deferred."""
    job = _FakeJob(job_id="job-1", attempts=2, status="running", locked_by="worker-2")
    session = _FakeSession(job)
    tomorrow = datetime(2026, 8, 9, 0, 3, tzinfo=timezone.utc)

    deferred = cli_mod.defer_job_for_budget(
        "job-1",
        object,
        claimed_attempts=2,  # the FIRST worker's claim -- expects attempts==1
        run_after=tomorrow,
        session_factory=lambda: session,
    )

    assert deferred is False
    # Nothing about the second worker's claim was touched.
    assert job.status == "running"
    assert job.attempts == 2
    assert job.locked_by == "worker-2"
    assert job.run_after is None
    assert session.rolled_back is True
    assert session.committed is False
    assert session.closed is True
