"""PostgreSQL-only Scout vertical slice and contention tests.

This file is deliberately opt-in.  It never creates schema and only cleans
rows whose UUIDs it created.  The explicit URL/acknowledgement gates make it
safe to keep alongside ordinary API tests while still exercising Postgres
partial indexes and row locks that SQLite cannot emulate.

Run (only against the named disposable local database)::

    BILLCOMMONS_TEST_DATABASE_URL='postgresql:///billcommons_scout_verify_20260901_test?host=/var/run/postgresql' \
    BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
    PYTHONPATH=apps/api:packages/schema:packages/shared:workers/scout \
    pytest -q apps/api/tests/test_scout_postgres.py
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool
from starlette.requests import Request

# The optional worker is intentionally not installed in the API image.  This
# test exercises it from source, exactly like the worker's own test suite.
_WORKER_ROOT = Path(__file__).resolve().parents[3] / "workers" / "scout"
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))

from billcommons_api.app import create_app
from billcommons_api.deps import get_db
from billcommons_api.routers import scout
from billcommons_schema.models import (
    ApiCustomer,
    Bill,
    BillSubject,
    Jurisdiction,
    ScoutBrowserSession,
    ScoutFinding,
    ScoutJobEvent,
    ScoutMonitor,
    ScoutMonitorRun,
    ScoutRawBlob,
    ScoutResearchJob,
    ScoutSource,
    Session as LegislativeSession,
)
from billcommons_shared.db import _use_psycopg3
from billcommons_shared.rawstore import FilesystemRawStore
from billcommons_shared.scout import BrowserCapture, ScoutSettings, canonicalize_url, content_hash
from billcommons_scout.providers import MockResearchBrowserProvider
from billcommons_scout.providers import SolariResearchBrowserProvider
from billcommons_scout.rawstore import PostgresScoutRawStore
import billcommons_scout.runner as scout_runner_module
from billcommons_scout.runner import ScoutRunner


_URL = os.environ.get("BILLCOMMONS_TEST_DATABASE_URL")
_DISPOSABLE_DB_RE = re.compile(r"^billcommons_scout_(?:test|verify|closeout)_\d{8}_test$")


def _assert_disposable_database(url: str) -> None:
    """Reject production/Railway URLs before this test opens a connection.

    A Unix-domain socket is permitted because the documented disposable DB on
    the development host uses one.  Its database name must still be an exact
    Scout test/verify name, and the destructive acknowledgement is mandatory.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    database = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
    query = parse_qs(parsed.query, keep_blank_values=True)
    query_hosts = query.get("host", [])
    ambiguous_target = (
        len(query_hosts) > 1
        or bool(host and query_hosts)
        or any(query.get(key) for key in ("hostaddr", "service", "servicefile"))
    )
    query_host = query_hosts[0] if len(query_hosts) == 1 else ""
    if any(token in url.lower() for token in ("railway", "render.com", "supabase", "neon.tech")):
        raise RuntimeError("REFUSING Scout Postgres tests against a hosted/production-like database URL")
    local_tcp = host in {"localhost", "127.0.0.1", "::1"}
    local_socket = not host and query_host == "/var/run/postgresql"
    if ambiguous_target or not (local_tcp or local_socket) or not _DISPOSABLE_DB_RE.fullmatch(database):
        raise RuntimeError(
            "REFUSING Scout Postgres tests: require localhost or /var/run/postgresql "
            "and a dated database name billcommons_scout_test_YYYYMMDD_test, "
            "billcommons_scout_verify_YYYYMMDD_test, or billcommons_scout_closeout_YYYYMMDD_test"
        )
    if os.environ.get("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE") != "1":
        raise RuntimeError(
            "REFUSING Scout Postgres tests without BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1"
        )


if _URL:
    _assert_disposable_database(_URL)

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="requires explicit BILLCOMMONS_TEST_DATABASE_URL and destructive acknowledgement",
)


@dataclass
class PostgresScoutHarness:
    engine: object
    sessions: sessionmaker[Session]
    customer_ids: list[uuid.UUID] = field(default_factory=list)
    bill_ids: list[uuid.UUID] = field(default_factory=list)
    session_ids: list[uuid.UUID] = field(default_factory=list)
    jurisdiction_ids: list[uuid.UUID] = field(default_factory=list)
    raw_blob_keys: list[str] = field(default_factory=list)

    def customer(self, label: str) -> ApiCustomer:
        customer = ApiCustomer(id=uuid.uuid4(), email=f"scout-pg-{label}-{uuid.uuid4().hex[:12]}@example.test")
        with self.sessions() as db:
            db.add(customer)
            db.commit()
        self.customer_ids.append(customer.id)
        return customer

    def florida_bill(
        self,
        *,
        source_url: str = "https://www.flsenate.gov/Session/Bill/2026/9999",
        identifier: str = "HB 9999",
        title: str = "AI Generated Political Advertising Act",
        latest_action_text: str = "Referred to the Committee on Ethics and Elections",
    ) -> Bill:
        """Seed the smallest real structured-data path for a topical query."""
        with self.sessions() as db:
            florida = db.execute(
                select(Jurisdiction).where(Jurisdiction.abbreviation == "FL")
            ).scalar_one_or_none()
            if florida is None:
                florida = Jurisdiction(
                    id=uuid.uuid4(),
                    name="Florida (Scout PostgreSQL fixture)",
                    abbreviation="FL",
                    classification="state",
                    source_name="scout-postgres-test",
                )
                db.add(florida)
                db.flush()
                self.jurisdiction_ids.append(florida.id)
            legislative_session = LegislativeSession(
                id=uuid.uuid4(),
                jurisdiction_id=florida.id,
                identifier=f"2026-scout-pg-{uuid.uuid4().hex}",
                name="Scout PostgreSQL fixture",
                classification="primary",
                active=True,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
            )
            bill = Bill(
                id=uuid.uuid4(),
                jurisdiction_id=florida.id,
                session_id=legislative_session.id,
                identifier=identifier,
                identifier_norm=identifier,
                title=title,
                description="Florida legislation concerning AI generated political advertising.",
                status="in_committee",
                latest_action_text=latest_action_text,
                source_name="scout-postgres-test",
                source_url=source_url,
            )
            db.add_all((legislative_session, bill, BillSubject(bill_id=bill.id, subject="Political advertising")))
            db.commit()
        self.session_ids.append(legislative_session.id)
        self.bill_ids.append(bill.id)
        return bill

    def cleanup(self) -> None:
        """Delete only UUID-addressed fixture rows; never truncate/cascade globally."""
        with self.sessions() as db:
            if self.customer_ids:
                # Monitor runs retain job references, so delete owner monitors
                # first; their child run journal rows cascade with them.
                db.execute(delete(ScoutMonitor).where(ScoutMonitor.customer_id.in_(self.customer_ids)))
                db.execute(delete(ScoutResearchJob).where(ScoutResearchJob.customer_id.in_(self.customer_ids)))
            if self.raw_blob_keys:
                db.execute(delete(ScoutRawBlob).where(ScoutRawBlob.sha256.in_(self.raw_blob_keys)))
            if self.bill_ids:
                db.execute(delete(BillSubject).where(BillSubject.bill_id.in_(self.bill_ids)))
                db.execute(delete(Bill).where(Bill.id.in_(self.bill_ids)))
            if self.session_ids:
                db.execute(delete(LegislativeSession).where(LegislativeSession.id.in_(self.session_ids)))
            if self.jurisdiction_ids:
                db.execute(delete(Jurisdiction).where(Jurisdiction.id.in_(self.jurisdiction_ids)))
            if self.customer_ids:
                db.execute(delete(ApiCustomer).where(ApiCustomer.id.in_(self.customer_ids)))
            db.commit()
        self.engine.dispose()


@pytest.fixture()
def pg_scout() -> Iterator[PostgresScoutHarness]:
    assert _URL is not None
    _assert_disposable_database(_URL)
    engine = create_engine(_use_psycopg3(_URL), poolclass=NullPool)
    harness = PostgresScoutHarness(
        engine=engine,
        sessions=sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    try:
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture()
def scout_api(monkeypatch, pg_scout: PostgresScoutHarness):
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ALLOW_PUBLIC", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_ACTIVE_JOBS", "2")
    monkeypatch.setattr(scout, "_check_origin", lambda request: None)
    monkeypatch.setattr(
        scout,
        "_require_session",
        lambda request, db: db.get(ApiCustomer, uuid.UUID(request.headers["x-test-customer"])),
    )
    app = create_app()

    def db_override():
        db = pg_scout.sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = db_override
    return app


def _direct_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/scout/jobs", "headers": []})


def _direct_request_for(customer: ApiCustomer) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v1/scout/jobs",
        "headers": [(b"x-test-customer", str(customer.id).encode())],
    })


def test_postgres_vertical_slice_api_owner_runner_provenance_and_constraints(
    pg_scout: PostgresScoutHarness, scout_api
):
    customer = pg_scout.customer("vertical")
    bill = pg_scout.florida_bill()
    headers = {"x-test-customer": str(customer.id)}
    query = "Research Florida legislation involving AI-generated political advertising"

    with TestClient(scout_api) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": query, "jurisdiction": "FL"}, headers=headers)
        assert created.status_code == 201, created.text
        job_id = uuid.UUID(created.json()["job"]["id"])

        body = (
            b"<main><h1>HB 9999 AI Generated Political Advertising Act</h1>"
            b"<p>Referred to the Committee on Ethics and Elections</p></main>"
        )
        rawstore = PostgresScoutRawStore(pg_scout.sessions)
        runner = ScoutRunner(
            pg_scout.sessions,
            rawstore,
            MockResearchBrowserProvider(),
            settings=ScoutSettings(enabled=True),
            fetcher=lambda url: (200, "text/html; charset=utf-8", body),
        )
        claim = runner.claim_next("scout-postgres-test-worker")
        assert claim is not None and claim.job_id == job_id
        runner.process(claim.job_id, claim.token)

        response = client.get(f"/api/v1/scout/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()

    with pg_scout.sessions() as db:
        source = db.scalar(select(ScoutSource).where(ScoutSource.job_id == job_id))
        assert source is not None and source.raw_ref
        pg_scout.raw_blob_keys.append(source.raw_ref)
        assert db.get(ScoutRawBlob, source.raw_ref) is not None
        raw_ref = source.raw_ref
    # A fresh store instance (representing a restarted worker) reads the exact
    # evidence retained by the API-created job's real Postgres worker path.
    assert PostgresScoutRawStore(pg_scout.sessions).get(raw_ref) == body

    assert payload["status"] == "completed"
    assert payload["partial_success"] is False
    assert payload["strategy_detail"]["structured_lookup"] == "title_terms"
    assert payload["finding_count"] == 1
    assert payload["sources"][0]["official"] is True
    assert payload["sources"][0]["content_hash"] == content_hash(body)
    assert payload["findings"][0]["bill_id"] == str(bill.id)
    assert "Referred to the Committee on Ethics and Elections" in payload["findings"][0]["what_happened"]
    assert {event["kind"] for event in payload["events"]} >= {"claimed", "structured_candidates", "source_persisted", "finished"}

    # Prove the migration objects are genuinely present, then exercise the
    # status check constraint rather than merely trusting model metadata.
    with pg_scout.sessions() as db:
        indexes = db.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'scout_research_jobs'"))
        index_defs = {name: definition for name, definition in indexes}
        assert "uq_scout_research_jobs_active_cache" in index_defs
        assert "UNIQUE" in index_defs["uq_scout_research_jobs_active_cache"]
        assert "queued" in index_defs["uq_scout_research_jobs_active_cache"]
        assert db.scalar(text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_scout_research_jobs_status'")) == 1
        bad = ScoutResearchJob(
            customer_id=customer.id,
            original_query="invalid status fixture",
            normalized_query="invalid status fixture",
            jurisdiction="FL",
            cache_key=uuid.uuid4().hex,
            status="impossible",
            strategy={}, limits={}, usage={},
        )
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_postgres_browser_shell_vertical_slice_releases_mock_session_and_retains_provenance(
    tmp_path, pg_scout: PostgresScoutHarness, scout_api
):
    """A deterministic 200 JavaScript shell may use only the explicit browser path.

    This is intentionally a MockResearchBrowserProvider test: it proves durable
    routing/provenance/cleanup mechanics without opening a network connection
    or spending a Solari browser session.
    """
    customer = pg_scout.customer("browser-shell")
    source_url = "https://www.myfloridahouse.gov/Sections/Bills/billsdetail.aspx?BillId=9999"
    bill = pg_scout.florida_bill(source_url=source_url)
    headers = {"x-test-customer": str(customer.id)}
    direct_shell = b"<html><noscript>Enable JavaScript</noscript></html>"
    browser_body = (
        b"<main><h1>HB 9999 AI Generated Political Advertising Act</h1>"
        b"<p>Referred to the Committee on Ethics and Elections</p>"
        b"<p>Status: in_committee</p></main>"
    )
    canonical_url = canonicalize_url(source_url)
    provider = MockResearchBrowserProvider(
        captures={
            canonical_url: BrowserCapture(
                provider_session_id="mock-browser-session-pg-9999",
                url=canonical_url,
                mime_type="text/html; charset=utf-8",
                body=browser_body,
                pages=1,
                actions=1,
            )
        }
    )

    with TestClient(scout_api) as client:
        created = client.post(
            "/api/v1/scout/jobs",
            json={"query": "HB 9999", "jurisdiction": "FL"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        job_id = uuid.UUID(created.json()["job"]["id"])

        runner = ScoutRunner(
            pg_scout.sessions,
            FilesystemRawStore(tmp_path / "raw"),
            provider,
            settings=ScoutSettings(enabled=True),
            fetcher=lambda _url: (200, "text/html; charset=utf-8", direct_shell),
        )
        claim = runner.claim_next("scout-postgres-browser-shell-worker")
        assert claim is not None and claim.job_id == job_id
        runner.process(claim.job_id, claim.token)

        owner_response = client.get(f"/api/v1/scout/jobs/{job_id}", headers=headers)
        assert owner_response.status_code == 200, owner_response.text
        payload = owner_response.json()

    assert provider.released == ["mock-browser-session-pg-9999"]
    assert payload["status"] == "completed"
    assert payload["partial_success"] is False
    assert payload["usage"]["browser_sessions"] == 1
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["url"] == canonical_url
    assert payload["sources"][0]["official"] is True
    assert payload["sources"][0]["mechanism"] == "browser"
    assert payload["sources"][0]["status"] == 200
    assert payload["sources"][0]["content_hash"] == content_hash(browser_body)
    assert payload["findings"][0]["bill_id"] == str(bill.id)
    assert payload["findings"][0]["source_url"] == canonical_url
    displayed_excerpt = payload["findings"][0]["excerpt"].casefold()
    assert "hb 9999" in displayed_excerpt
    assert "referred to the committee on ethics and elections" in displayed_excerpt
    assert len(payload["browser_sessions"]) == 1
    assert payload["browser_sessions"][0]["status"] == "released"
    assert payload["browser_sessions"][0]["pages"] == 1
    assert payload["browser_sessions"][0]["actions"] == 1
    assert payload["browser_sessions"][0]["replay_available"] is False
    assert payload["browser_sessions"][0]["runtime_ms"] is not None

    with pg_scout.sessions() as db:
        browser_session = db.scalar(select(ScoutBrowserSession).where(ScoutBrowserSession.job_id == job_id))
        assert browser_session is not None
        assert browser_session.status == "released"
        assert browser_session.provider_session_id == "mock-browser-session-pg-9999"
        assert browser_session.released_at is not None
        assert browser_session.source_id is not None
        source = db.get(ScoutSource, browser_session.source_id)
        assert source is not None
        assert source.retrieval_mechanism == "browser"
        assert source.official is True
        assert source.content_hash == content_hash(browser_body)
        finding = db.scalar(select(ScoutFinding).where(ScoutFinding.job_id == job_id))
        assert finding is not None and finding.source_id == source.id


@pytest.mark.skipif(
    os.environ.get("BILLCOMMONS_SCOUT_LIVE_PRODUCT_CHECK") != "1",
    reason="explicitly opt-in, billable one-session Solari product-path check",
)
def test_live_product_path_routes_house_rejection_through_solari_and_releases(
    tmp_path, pg_scout: PostgresScoutHarness, scout_api
):
    """Exercise the real worker/browser ledger without inventing a finding.

    The fixture metadata deliberately does not claim to describe BillId 84174.
    A useful matching finding is therefore not required; the live assertion is
    routing, bounded browser use, durable terminal state, and cleanup. The
    deterministic test above separately proves the evidence/finding contract.
    """
    customer = pg_scout.customer("live-browser-path")
    source_url = (
        "https://www.myfloridahouse.gov/Sections/Bills/"
        "billsdetail.aspx?BillId=84174"
    )
    pg_scout.florida_bill(source_url=source_url)
    headers = {"x-test-customer": str(customer.id)}
    provider = SolariResearchBrowserProvider()

    with TestClient(scout_api) as client:
        created = client.post(
            "/api/v1/scout/jobs",
            json={"query": "HB 9999", "jurisdiction": "FL"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        job_id = uuid.UUID(created.json()["job"]["id"])
        runner = ScoutRunner(
            pg_scout.sessions,
            FilesystemRawStore(tmp_path / "raw"),
            provider,
            settings=ScoutSettings(
                enabled=True,
                max_pages=1,
                max_actions=1,
                browser_wall_seconds=60,
                browser_cleanup_seconds=10,
                max_external_requests=2,
                max_retries=0,
            ),
        )
        claim = runner.claim_next("scout-live-product-check")
        assert claim is not None and claim.job_id == job_id
        runner.process(claim.job_id, claim.token)
        payload = client.get(
            f"/api/v1/scout/jobs/{job_id}", headers=headers
        ).json()

    assert payload["status"] in {"completed", "partial"}
    assert payload["usage"]["browser_sessions"] == 1
    assert payload["usage"]["browser_pages"] <= 1
    assert payload["usage"]["browser_actions"] <= 1
    with pg_scout.sessions() as db:
        session = db.scalar(
            select(ScoutBrowserSession).where(
                ScoutBrowserSession.job_id == job_id
            )
        )
        assert session is not None
        assert session.status == "released"
        assert session.provider_session_id
        assert session.released_at is not None
        assert session.pages <= 1 and session.actions <= 1
        findings = db.scalars(
            select(ScoutFinding).where(ScoutFinding.job_id == job_id)
        ).all()
        for finding in findings:
            displayed = finding.excerpt.casefold()
            assert "hb 9999" in displayed
            assert "referred to the committee on ethics and elections" in displayed


@pytest.mark.skipif(
    os.environ.get("BILLCOMMONS_SCOUT_LIVE_FINDING_CHECK") != "1",
    reason="explicitly opt-in live direct official-source finding check",
)
def test_live_direct_flsenate_hb_625_retains_exact_supported_evidence(
    tmp_path, pg_scout: PostgresScoutHarness, scout_api
):
    """Live contract, documented 2026-09-01; fail closed if the source changes.

    This is deliberately separate from billable Solari smoke coverage. The
    official Senate page must remain directly retrievable and support both the
    exact structured identifier/action and at least one bill-scoped adjacent
    analysis before Scout is allowed to complete. It is skipped in ordinary CI
    and uses the guarded disposable DB.
    """
    customer = pg_scout.customer("live-direct-hb625")
    source_url = "https://flsenate.gov/Session/Bill/2026/625/ByCategory"
    pg_scout.florida_bill(
        source_url=source_url,
        identifier="HB 625",
        title="Scout live direct evidence fixture",
        latest_action_text="Chapter No. 2026-141",
    )
    headers = {"x-test-customer": str(customer.id)}
    provider = MockResearchBrowserProvider()
    with TestClient(scout_api) as client:
        created = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 625", "jurisdiction": "FL"}, headers=headers
        )
        assert created.status_code == 201, created.text
        job_id = uuid.UUID(created.json()["job"]["id"])
        runner = ScoutRunner(
            pg_scout.sessions,
            FilesystemRawStore(tmp_path / "raw"),
            provider,
            settings=ScoutSettings(
                enabled=True,
                max_external_requests=3,
                max_related_documents=2,
                max_retries=0,
            ),
        )
        with pg_scout.sessions() as db:
            queued = db.get(ScoutResearchJob, job_id)
            assert queued is not None
            assert runner._candidates(db, queued), "live HB 625 fixture did not enter the structured-first route"
        claim = runner.claim_next("scout-live-direct-finding")
        assert claim is not None and claim.job_id == job_id
        runner.process(claim.job_id, claim.token)
        response = client.get(f"/api/v1/scout/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()

    # A browser fallback would make this live direct contract untruthful.
    assert provider.released == []
    assert payload["status"] == "completed", {
        "error_class": payload.get("error_class"),
        "events": [event.get("kind") for event in payload.get("events", [])],
        "source_statuses": [
            (source.get("url"), source.get("status"), source.get("mime_type"))
            for source in payload.get("sources", [])
        ],
    }
    assert len(payload["sources"]) >= 2
    assert all(source["mechanism"] == "direct" for source in payload["sources"])
    bill_finding = next(
        finding for finding in payload["findings"]
        if finding["source_url"] == canonicalize_url(source_url)
    )
    excerpt = bill_finding["excerpt"].casefold()
    assert "hb 625" in excerpt
    assert "chapter no. 2026-141" in excerpt
    related = [
        finding for finding in payload["findings"]
        if "/Analyses/" in finding["source_url"]
    ]
    assert related
    assert any("hb 625" in finding["excerpt"].casefold() for finding in related)


def test_postgres_simultaneous_identical_submissions_coalesce(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    customer = pg_scout.customer("coalesce")
    monkeypatch.setattr(scout, "_require_session", lambda _request, db: db.get(ApiCustomer, customer.id))
    barrier = threading.Barrier(10)

    def submit() -> dict:
        with pg_scout.sessions() as db:
            barrier.wait(timeout=10)
            return scout.create_job(
                scout.CreateScoutJob(query="HB 9999", jurisdiction="FL"),
                _direct_request(),
                Response(),
                db,
            )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _number: submit(), range(10)))

    assert sum(not item["coalesced"] for item in results) == 1
    assert sum(item["coalesced"] for item in results) == 9
    ids = {item["job"]["id"] for item in results}
    assert len(ids) == 1
    with pg_scout.sessions() as db:
        active = db.scalars(
            select(ScoutResearchJob).where(
                ScoutResearchJob.customer_id == customer.id,
                ScoutResearchJob.status.in_(("queued", "running")),
            )
        ).all()
    assert len(active) == 1


def test_postgres_ten_distinct_submissions_cannot_bypass_owner_active_limit(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    customer = pg_scout.customer("quota")
    # Keep this test about the owner active-job lock, not the stricter browser
    # reservation: the default 600-second budget admits one 400-second worst
    # case, while this fixture intentionally proves two active jobs serialize.
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "800")
    monkeypatch.setattr(scout, "_require_session", lambda _request, db: db.get(ApiCustomer, customer.id))
    barrier = threading.Barrier(10)

    def submit(number: int) -> str:
        with pg_scout.sessions() as db:
            barrier.wait(timeout=10)
            try:
                result = scout.create_job(
                    scout.CreateScoutJob(query=f"AI generated political advertising topic {number}", jurisdiction="FL"),
                    _direct_request(),
                    Response(),
                    db,
                )
                return "created" if not result["coalesced"] else "coalesced"
            except HTTPException as exc:
                assert exc.status_code == 429
                return "limited"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(submit, range(10)))

    assert results.count("created") == 2
    assert results.count("limited") == 8
    with pg_scout.sessions() as db:
        active_count = db.scalar(
            select(text("count(*)")).select_from(ScoutResearchJob).where(
                ScoutResearchJob.customer_id == customer.id,
                ScoutResearchJob.status.in_(("queued", "running")),
            )
        )
    assert active_count == 2


def test_postgres_simultaneous_distinct_submissions_reserve_daily_browser_budget(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    customer = pg_scout.customer("daily-browser-reservation")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    monkeypatch.setattr(scout, "_require_session", lambda _request, db: db.get(ApiCustomer, customer.id))
    barrier = threading.Barrier(2)

    def submit(number: int) -> str:
        with pg_scout.sessions() as db:
            barrier.wait(timeout=10)
            try:
                result = scout.create_job(
                    scout.CreateScoutJob(query=f"daily browser topic {number}", jurisdiction="FL"),
                    _direct_request(),
                    Response(),
                    db,
                )
                return "created" if not result["coalesced"] else "coalesced"
            except HTTPException as exc:
                assert exc.status_code == 429
                assert exc.detail["code"] == "scout_daily_browser_limit"
                return "limited"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))

    assert results.count("created") == 1
    assert results.count("limited") == 1


def test_postgres_platform_active_capacity_is_atomic_across_customers(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    customers = [pg_scout.customer(f"platform-active-{number}") for number in range(2)]
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_ACTIVE_JOBS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_JOBS", "10")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_BROWSER_SECONDS", "800")
    monkeypatch.setattr(
        scout,
        "_require_session",
        lambda request, db: db.get(ApiCustomer, uuid.UUID(request.headers["x-test-customer"])),
    )
    barrier = threading.Barrier(2)

    def submit(index: int) -> str:
        with pg_scout.sessions() as db:
            barrier.wait(timeout=10)
            try:
                result = scout.create_job(
                    scout.CreateScoutJob(query=f"platform active topic {index}", jurisdiction="FL"),
                    _direct_request_for(customers[index]), Response(), db,
                )
                return "created" if not result["coalesced"] else "coalesced"
            except HTTPException as exc:
                assert exc.status_code == 429
                assert exc.detail["code"] == "scout_platform_active_job_limit"
                return "limited"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert sorted(outcomes) == ["created", "limited"]
    with pg_scout.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScoutResearchJob).where(
            ScoutResearchJob.status.in_(("queued", "running"))
        )) == 1


def test_postgres_platform_daily_job_limit_refuses_without_partial_write(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    first, second = pg_scout.customer("platform-daily-first"), pg_scout.customer("platform-daily-second")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_ACTIVE_JOBS", "10")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_JOBS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_BROWSER_SECONDS", "800")
    monkeypatch.setattr(
        scout,
        "_require_session",
        lambda request, db: db.get(ApiCustomer, uuid.UUID(request.headers["x-test-customer"])),
    )
    with pg_scout.sessions() as db:
        first_result = scout.create_job(
            scout.CreateScoutJob(query="platform daily first", jurisdiction="FL"),
            _direct_request_for(first), Response(), db,
        )
        assert first_result["coalesced"] is False
    with pg_scout.sessions() as db:
        with pytest.raises(HTTPException) as limited:
            scout.create_job(
                scout.CreateScoutJob(query="platform daily second", jurisdiction="FL"),
                _direct_request_for(second), Response(), db,
            )
        assert limited.value.status_code == 429
        assert limited.value.detail["code"] == "scout_platform_daily_job_limit"
        assert db.scalar(select(func.count()).select_from(ScoutResearchJob)) == 1


def test_postgres_platform_browser_reservation_and_terminal_release(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    first, second = pg_scout.customer("platform-browser-first"), pg_scout.customer("platform-browser-second")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_ACTIVE_JOBS", "2")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_JOBS", "10")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_BROWSER_SECONDS", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    monkeypatch.setattr(
        scout,
        "_require_session",
        lambda request, db: db.get(ApiCustomer, uuid.UUID(request.headers["x-test-customer"])),
    )
    with pg_scout.sessions() as db:
        created = scout.create_job(
            scout.CreateScoutJob(query="platform browser first", jurisdiction="FL"),
            _direct_request_for(first), Response(), db,
        )
        assert created["coalesced"] is False
    with pg_scout.sessions() as db:
        with pytest.raises(HTTPException) as limited:
            scout.create_job(
                scout.CreateScoutJob(query="platform browser second", jurisdiction="FL"),
                _direct_request_for(second), Response(), db,
            )
        assert limited.value.status_code == 429
        assert limited.value.detail["code"] == "scout_platform_daily_browser_limit"
        assert db.scalar(select(func.count()).select_from(ScoutResearchJob)) == 1
        job = db.get(ScoutResearchJob, uuid.UUID(created["job"]["id"]))
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.fresh_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
    # Terminal jobs release active capacity; cache reuse remains free and
    # creates no row before another customer consumes the freed slot.
    with pg_scout.sessions() as db:
        cached = scout.create_job(
            scout.CreateScoutJob(query="platform browser first", jurisdiction="FL"),
            _direct_request_for(first), Response(), db,
        )
        assert cached["coalesced"] is True and cached["cached"] is True
        released = scout.create_job(
            scout.CreateScoutJob(query="platform browser second", jurisdiction="FL"),
            _direct_request_for(second), Response(), db,
        )
        assert released["coalesced"] is False


def test_postgres_retained_raw_evidence_refuses_new_job_before_insert(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    """Immutable evidence already at capacity must reject before creating a job."""
    customer = pg_scout.customer("raw-capacity-preflight")
    retained = b"0123456789"
    retained_key = content_hash(retained)
    with pg_scout.sessions() as db:
        db.add(ScoutRawBlob(sha256=retained_key, data=retained, metadata_json={}))
        db.commit()
    pg_scout.raw_blob_keys.append(retained_key)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DIRECT_BYTES", "10")
    # Ten retained bytes plus one ten-byte worst-case reservation cannot fit.
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_RETAINED_RAWSTORE_BYTES", "19")
    monkeypatch.setattr(scout, "_require_session", lambda _request, db: db.get(ApiCustomer, customer.id))

    with pg_scout.sessions() as db:
        with pytest.raises(HTTPException) as rejected:
            scout.create_job(
                scout.CreateScoutJob(query="raw capacity preflight", jurisdiction="FL"),
                _direct_request(), Response(), db,
            )
        assert rejected.value.status_code == 429
        assert rejected.value.detail["code"] == "scout_rawstore_capacity_limit"
        assert db.scalar(select(func.count()).select_from(ScoutResearchJob)) == 0


def test_postgres_raw_evidence_reservation_is_atomic_and_releases_on_terminal_job(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    """Two owners may reserve exactly the global byte ceiling; N+1 cannot race past it."""
    customers = [pg_scout.customer(f"raw-capacity-{number}") for number in range(3)]
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DIRECT_BYTES", "10")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_RETAINED_RAWSTORE_BYTES", "20")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_ACTIVE_JOBS", "10")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_JOBS", "10")
    monkeypatch.setattr(
        scout,
        "_require_session",
        lambda request, db: db.get(ApiCustomer, uuid.UUID(request.headers["x-test-customer"])),
    )
    barrier = threading.Barrier(2)

    def submit(index: int) -> tuple[str, dict | str]:
        with pg_scout.sessions() as db:
            barrier.wait(timeout=10)
            try:
                result = scout.create_job(
                    scout.CreateScoutJob(query=f"raw reservation topic {index}", jurisdiction="FL"),
                    _direct_request_for(customers[index]), Response(), db,
                )
                return "created", result
            except HTTPException as exc:
                return "limited", exc.detail["code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert [outcome for outcome, _value in outcomes].count("created") == 2
    created = [value for outcome, value in outcomes if outcome == "created"]

    # A third owner gets a stable capacity error, rather than a third queued
    # job that would make the worker discover the byte limit too late.
    with pg_scout.sessions() as db:
        with pytest.raises(HTTPException) as rejected:
            scout.create_job(
                scout.CreateScoutJob(query="raw reservation topic 2", jurisdiction="FL"),
                _direct_request_for(customers[2]), Response(), db,
            )
        assert rejected.value.status_code == 429
        assert rejected.value.detail["code"] == "scout_rawstore_capacity_limit"
        assert db.scalar(select(func.count()).select_from(ScoutResearchJob).where(
            ScoutResearchJob.status.in_(("queued", "running"))
        )) == 2

        completed = db.get(ScoutResearchJob, uuid.UUID(created[0]["job"]["id"]))
        assert completed is not None
        completed.status = "completed"
        completed.completed_at = datetime.now(timezone.utc)
        completed.fresh_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()

    # A terminal job releases its future-evidence reservation. Reusing either
    # an active request or a fresh terminal result remains a zero-cost read,
    # even while the two remaining reservations exactly fill the ceiling.
    with pg_scout.sessions() as db:
        released = scout.create_job(
            scout.CreateScoutJob(query="raw reservation topic 2", jurisdiction="FL"),
            _direct_request_for(customers[2]), Response(), db,
        )
        assert released["coalesced"] is False
        before_reuse = db.scalar(select(func.count()).select_from(ScoutResearchJob))
        active_reuse = scout.create_job(
            scout.CreateScoutJob(query="raw reservation topic 1", jurisdiction="FL"),
            _direct_request_for(customers[1]), Response(), db,
        )
        fresh_reuse = scout.create_job(
            scout.CreateScoutJob(query="raw reservation topic 0", jurisdiction="FL"),
            _direct_request_for(customers[0]), Response(), db,
        )
        assert active_reuse["coalesced"] is True and "cached" not in active_reuse
        assert fresh_reuse["coalesced"] is True and fresh_reuse["cached"] is True
        assert db.scalar(select(func.count()).select_from(ScoutResearchJob)) == before_reuse


def test_postgres_runner_rawstore_capacity_failure_is_terminal_and_leaves_no_stage(
    pg_scout: PostgresScoutHarness, scout_api
):
    """A late RawStore refusal must not leave a clickable source or partial claim."""
    customer = pg_scout.customer("rawstore-runner-capacity")
    baseline = b"capacity-filled"
    baseline_key = content_hash(baseline)
    body = b"<main>HB 701 Filed</main>"
    with pg_scout.sessions() as db:
        db.add(ScoutRawBlob(sha256=baseline_key, data=baseline, metadata_json={}))
        job = ScoutResearchJob(
            id=uuid.uuid4(),
            customer_id=customer.id,
            original_query="HB 701",
            normalized_query="hb 701",
            jurisdiction="FL",
            cache_key=uuid.uuid4().hex,
            status="running",
            claim_token="rawstore-capacity-claim",
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            strategy={},
            limits={"max_direct_bytes": len(body)},
            usage={},
        )
        db.add(job)
        db.commit()
    pg_scout.raw_blob_keys.append(baseline_key)
    runner = ScoutRunner(
        pg_scout.sessions,
        PostgresScoutRawStore(pg_scout.sessions, max_retained_bytes=len(baseline)),
        MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, max_direct_bytes=len(body)),
    )

    assert runner._persist_capture(
        job.id,
        "rawstore-capacity-claim",
        None,
        "HB 701",
        "Filed",
        {"identifier": "HB 701", "latest_action": "Filed"},
        "https://www.flsenate.gov/Session/Bill/2026/701",
        "direct",
        200,
        "text/html",
        body,
    ) is None

    with pg_scout.sessions() as db:
        persisted = db.get(ScoutResearchJob, job.id)
        assert persisted is not None
        assert (persisted.status, persisted.error_class, persisted.partial_success) == (
            "failed", "rawstore_capacity_exceeded", False,
        )
        assert db.scalar(select(func.count()).select_from(ScoutSource).where(
            ScoutSource.job_id == job.id
        )) == 0
        assert db.scalar(select(func.count()).select_from(ScoutFinding).where(
            ScoutFinding.job_id == job.id
        )) == 0
        events = db.execute(
            select(ScoutJobEvent).where(ScoutJobEvent.job_id == job.id).order_by(ScoutJobEvent.created_at)
        ).scalars().all()
        capacity_events = [event for event in events if event.kind == "rawstore_capacity_exceeded"]
        assert len(capacity_events) == 1 and capacity_events[0].detail == {}
        assert any(
            event.kind == "finished"
            and event.detail == {"status": "failed", "error_class": "rawstore_capacity_exceeded"}
            for event in events
        )
        assert db.scalar(select(func.count()).select_from(ScoutRawBlob).where(
            ScoutRawBlob.sha256 == baseline_key
        )) == 1
        assert db.scalar(select(func.count()).select_from(ScoutRawBlob).where(
            ScoutRawBlob.sha256 == content_hash(body)
        )) == 0


def test_postgres_global_browser_cap_and_reaper_claim_are_cross_runner_atomic(tmp_path, pg_scout: PostgresScoutHarness):
    """Use independent DB sessions: SQLite cannot prove advisory-lock behavior."""
    customer = pg_scout.customer("browser-contention")
    now = datetime.now(timezone.utc)
    with pg_scout.sessions() as db:
        jobs = [
            ScoutResearchJob(
                id=uuid.uuid4(), customer_id=customer.id, original_query=f"job {n}", normalized_query=f"job {n}",
                jurisdiction="FL", cache_key=uuid.uuid4().hex, status="running", claim_token=f"token-{n}",
                lease_expires_at=now + timedelta(minutes=5), strategy={}, limits={}, usage={},
            )
            for n in range(2)
        ]
        db.add_all(jobs)
        db.commit()
    settings = ScoutSettings(enabled=True, max_concurrent_browser_sessions=1, browser_cleanup_seconds=1)
    provider = MockResearchBrowserProvider()
    runners = [ScoutRunner(pg_scout.sessions, FilesystemRawStore(tmp_path / f"raw-{n}"), provider, settings=settings) for n in range(2)]
    barrier = threading.Barrier(2)

    def reserve(index: int):
        barrier.wait(timeout=10)
        return runners[index]._reserve_browser_slot(jobs[index].id, f"token-{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        slots = list(pool.map(reserve, range(2)))
    assert sum(slot is not None for slot in slots) == 1

    with pg_scout.sessions() as db:
        slot = db.scalar(select(ScoutBrowserSession).where(ScoutBrowserSession.id == next(item for item in slots if item is not None)))
        assert slot is not None
        slot.provider_session_id = "one-orphan"
        slot.status = "cleanup_failed"
        db.commit()

    class CountingProvider(MockResearchBrowserProvider):
        def release(self, provider_session_id, *, cleanup_seconds=None):
            self.released.append(provider_session_id)
            time.sleep(0.05)
            return None

    shared = CountingProvider()
    reapers = [ScoutRunner(pg_scout.sessions, FilesystemRawStore(tmp_path / f"reap-{n}"), shared, settings=settings) for n in range(2)]
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda runner: (barrier.wait(timeout=10), runner.reap_sessions())[1], reapers))
    assert sum(outcomes) == 1
    assert shared.released == ["one-orphan"]



def _terminal_monitor_baseline(
    pg_scout: PostgresScoutHarness, customer: ApiCustomer, *, operator: bool = False, query: str = "HB 625"
) -> ScoutResearchJob:
    """Create the smallest completed evidence-bearing owner job for monitor APIs."""
    now = datetime.now(timezone.utc)
    with pg_scout.sessions() as db:
        job = ScoutResearchJob(
            customer_id=customer.id,
            original_query=query,
            normalized_query=query.casefold(),
            jurisdiction="FL",
            cache_key=f"monitor-{uuid.uuid4().hex}",
            status="completed",
            strategy={"mode": "operator_lifecycle_validation", "user_finding": False} if operator else {"mode": "structured_first"},
            limits={}, usage={}, completed_at=now,
        )
        db.add(job)
        db.flush()
        source = ScoutSource(
            job_id=job.id,
            canonical_url="https://www.flsenate.gov/Session/Bill/2026/625",
            official=True,
            retrieval_mechanism="direct",
            content_hash="a" * 64,
            raw_ref="a" * 64,
        )
        db.add(source)
        db.flush()
        db.add(ScoutFinding(
            job_id=job.id, source_id=source.id, title="HB 625", what_happened="Official history retained.",
            confidence="high", extractor_version="monitor-test",
        ))
        db.commit()
        return job


def test_postgres_saved_monitor_requires_owner_terminal_evidence_and_supports_pause_history(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api
):
    customer = pg_scout.customer("monitor-owner")
    other = pg_scout.customer("monitor-other")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    operator = _terminal_monitor_baseline(pg_scout, customer, operator=True)
    second = _terminal_monitor_baseline(pg_scout, customer, query="HB 626")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_SAVED_MONITORS", "1")
    headers = {"x-test-customer": str(customer.id)}
    other_headers = {"x-test-customer": str(other.id)}
    with TestClient(scout_api) as client:
        denied = client.post(f"/api/v1/scout/jobs/{operator.id}/monitor", json={}, headers=headers)
        assert denied.status_code == 422 and denied.json()["error"]["code"] == "invalid_monitor_baseline"
        created = client.post(f"/api/v1/scout/jobs/{baseline.id}/monitor", json={"cadence_seconds": 21600}, headers=headers)
        assert created.status_code == 201, created.text
        monitor_id = uuid.UUID(created.json()["monitor"]["id"])
        repeated = client.post(f"/api/v1/scout/jobs/{baseline.id}/monitor", json={}, headers=headers)
        assert repeated.status_code == 200 and repeated.json()["created"] is False
        limited = client.post(f"/api/v1/scout/jobs/{second.id}/monitor", json={}, headers=headers)
        assert limited.status_code == 429 and limited.json()["error"]["code"] == "scout_monitor_limit"
        denied_history = client.get(f"/api/v1/scout/monitors/{monitor_id}/runs", headers=other_headers)
        assert denied_history.status_code == 404
        paused = client.patch(f"/api/v1/scout/monitors/{monitor_id}", json={"active": False}, headers=headers)
        assert paused.status_code == 200 and paused.json()["monitor"]["active"] is False
        resumed = client.patch(f"/api/v1/scout/monitors/{monitor_id}", json={"active": True}, headers=headers)
        assert resumed.status_code == 200 and resumed.json()["monitor"]["active"] is True
        history = client.get(f"/api/v1/scout/monitors/{monitor_id}/runs", headers=headers)
        assert history.status_code == 200
        run = history.json()["runs"][0]
    assert run["status"] == "baseline"
    assert run["source_snapshot"]["sources"][0]["content_hash"] == "a" * 64
    assert run["change_summary"]["absence_evaluated"] is False


def test_postgres_saved_monitor_history_cursor_returns_every_run_once(
    pg_scout: PostgresScoutHarness, scout_api
):
    customer = pg_scout.customer("monitor-history-cursor")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    headers = {"x-test-customer": str(customer.id)}
    with TestClient(scout_api) as client:
        saved = client.post(f"/api/v1/scout/jobs/{baseline.id}/monitor", json={}, headers=headers)
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    now = datetime.now(timezone.utc)
    with pg_scout.sessions() as db:
        for offset in (1, 2, 3):
            db.add(ScoutMonitorRun(
                monitor_id=monitor_id, status="completed", execution_mode="new",
                scheduled_for=now + timedelta(seconds=offset), completed_at=now + timedelta(seconds=offset),
                source_snapshot={}, change_summary={},
            ))
        db.commit()
    with TestClient(scout_api) as client:
        first = client.get(f"/api/v1/scout/monitors/{monitor_id}/runs?limit=2", headers=headers)
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["next_cursor"] == first_payload["runs"][-1]["id"]
        second = client.get(
            f"/api/v1/scout/monitors/{monitor_id}/runs?limit=2&cursor={first_payload['next_cursor']}",
            headers=headers,
        )
        assert second.status_code == 200
        second_payload = second.json()
    assert second_payload["next_cursor"] is None
    returned = [run["id"] for run in first_payload["runs"] + second_payload["runs"]]
    assert len(returned) == 4
    assert len(set(returned)) == 4


def test_postgres_saved_monitor_scheduler_uses_admission_and_finalizes_hash_delta(
    pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    customer = pg_scout.customer("monitor-scheduler")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    headers = {"x-test-customer": str(customer.id)}
    with TestClient(scout_api) as client:
        saved = client.post(f"/api/v1/scout/jobs/{baseline.id}/monitor", json={}, headers=headers)
        assert saved.status_code == 201, saved.text
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        monitor.consecutive_deferrals = 2
        db.commit()
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-raw"), MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True, browser_cleanup_seconds=1),
    )
    assert runner.schedule_one_due_monitor() is True
    with pg_scout.sessions() as db:
        queued = db.scalar(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.status == "queued"
        ))
        assert db.get(ScoutMonitor, monitor_id).consecutive_deferrals == 0
        assert queued is not None and queued.execution_mode == "new" and queued.job_id is not None
        scheduled_job_id = queued.job_id
    claim = runner.claim_next("monitor-postgres-test")
    assert claim is not None and claim.job_id == scheduled_job_id
    with pg_scout.sessions() as db:
        job = db.get(ScoutResearchJob, scheduled_job_id)
        source = ScoutSource(
            job_id=job.id,
            canonical_url="https://www.flsenate.gov/Session/Bill/2026/625",
            official=True,
            retrieval_mechanism="direct",
            content_hash="b" * 64,
            raw_ref="b" * 64,
        )
        db.add(source)
        db.flush()
        db.add(ScoutFinding(
            job_id=job.id, source_id=source.id, title="HB 625", what_happened="Updated official history retained.",
            confidence="high", extractor_version="monitor-test",
        ))
        runner._finish(db, job, claim.token, "completed", None, False)
    with pg_scout.sessions() as db:
        run = db.scalar(select(ScoutMonitorRun).where(ScoutMonitorRun.job_id == scheduled_job_id))
        assert run.status == "completed"
        assert run.change_summary["absence_evaluated"] is False
        assert run.change_summary["changed_sources"] == [{
            "canonical_url": "https://www.flsenate.gov/Session/Bill/2026/625",
            "previous_content_hash": "a" * 64,
            "content_hash": "b" * 64,
        }]
        # The production sessionmaker has autoflush disabled.  This finding
        # remained pending when _finish began, so its ID proves finalization
        # flushed evidence before snapshotting the monitor run.
        assert len(run.source_snapshot["sources"][0]["finding_ids"]) == 1
        assert "removed_sources" not in run.change_summary


def test_postgres_cached_monitor_run_flushes_its_id_before_advancing_baseline(
    pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    customer = pg_scout.customer("monitor-cached-baseline")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    headers = {"x-test-customer": str(customer.id)}
    with pg_scout.sessions() as db:
        stored = db.get(ScoutResearchJob, baseline.id)
        stored.fresh_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
    with TestClient(scout_api) as client:
        saved = client.post(f"/api/v1/scout/jobs/{baseline.id}/monitor", json={}, headers=headers)
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        monitor.consecutive_deferrals = 2
        db.commit()
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-cached-baseline"), MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True),
    )
    assert runner.schedule_one_due_monitor() is True
    with pg_scout.sessions() as db:
        run = db.scalar(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.execution_mode == "cached"
        ))
        monitor = db.get(ScoutMonitor, monitor_id)
    assert run is not None and run.status == "completed"
    assert monitor.last_completed_run_id == run.id
    assert monitor.consecutive_deferrals == 0


def test_postgres_coalesced_monitor_run_reconciles_terminalization_before_commit(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    """A terminalizer that misses an uncommitted coalesced run cannot strand it."""
    customer = pg_scout.customer("monitor-coalesced-terminal-race")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    headers = {"x-test-customer": str(customer.id)}
    with TestClient(scout_api) as client:
        saved = client.post(f"/api/v1/scout/jobs/{baseline.id}/monitor", json={}, headers=headers)
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    token = "coalesced-terminal-race-token"
    with pg_scout.sessions() as db:
        db.add(ScoutResearchJob(
            customer_id=customer.id,
            original_query=baseline.original_query,
            normalized_query=baseline.normalized_query,
            jurisdiction=baseline.jurisdiction,
            cache_key=baseline.cache_key,
            status="running",
            claim_token=token,
            limits={}, usage={},
        ))
        db.flush()
        active_id = db.scalar(select(ScoutResearchJob.id).where(
            ScoutResearchJob.customer_id == customer.id,
            ScoutResearchJob.cache_key == baseline.cache_key,
            ScoutResearchJob.status == "running",
        ))
        monitor = db.get(ScoutMonitor, monitor_id)
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert active_id is not None
    admitted = threading.Event()
    terminalized = threading.Event()
    release_scheduler = threading.Event()
    original_admit = scout_runner_module.admit_scout_job

    def pause_after_active_admission(*args, **kwargs):
        admission = original_admit(*args, **kwargs)
        assert admission.coalesced is True and admission.cached is False and admission.job.id == active_id
        admitted.set()
        assert terminalized.wait(timeout=10)
        assert release_scheduler.wait(timeout=10)
        return admission

    monkeypatch.setattr(scout_runner_module, "admit_scout_job", pause_after_active_admission)
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-coalesced-terminal-race"), MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.schedule_one_due_monitor)
        assert admitted.wait(timeout=10)
        with pg_scout.sessions() as db:
            active = db.execute(select(ScoutResearchJob).where(
                ScoutResearchJob.id == active_id
            ).with_for_update()).scalar_one()
            runner._finish(db, active, token, "completed", None, False)
        terminalized.set()
        release_scheduler.set()
        assert future.result(timeout=10) is True
    with pg_scout.sessions() as db:
        runs = db.scalars(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.job_id == active_id
        )).all()
    assert len(runs) == 1 and runs[0].status == "completed"


def test_postgres_saved_monitor_defers_when_shared_daily_admission_refuses(
    pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    customer = pg_scout.customer("monitor-deferred")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    with TestClient(scout_api) as client:
        saved = client.post(
            f"/api/v1/scout/jobs/{baseline.id}/monitor", json={},
            headers={"x-test-customer": str(customer.id)},
        )
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-deferred-raw"), MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True, per_customer_daily_jobs=1, browser_cleanup_seconds=1),
    )
    assert runner.schedule_one_due_monitor() is True
    with pg_scout.sessions() as db:
        deferred = db.scalar(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.status == "deferred"
        ))
        monitor = db.get(ScoutMonitor, monitor_id)
    assert deferred is not None and deferred.error_class == "scout_daily_job_limit"
    assert monitor.consecutive_deferrals == 1


@pytest.mark.parametrize("terminal_status", ("failed", "canceled"))
def test_postgres_admitted_monitor_resets_backoff_before_terminal_failure(
    terminal_status: str, pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    """A completed admission resets retry delay even when its job later fails."""
    customer = pg_scout.customer(f"monitor-backoff-{terminal_status}")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    with TestClient(scout_api) as client:
        saved = client.post(
            f"/api/v1/scout/jobs/{baseline.id}/monitor", json={},
            headers={"x-test-customer": str(customer.id)},
        )
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])

    # The baseline consumes this one-job daily budget, so the first due
    # attempt is durably deferred with a non-zero backoff counter.
    refusing = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / f"backoff-refuse-{terminal_status}"),
        MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True, per_customer_daily_jobs=1),
    )
    with pg_scout.sessions() as db:
        db.get(ScoutMonitor, monitor_id).next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert refusing.schedule_one_due_monitor() is True
    with pg_scout.sessions() as db:
        assert db.get(ScoutMonitor, monitor_id).consecutive_deferrals == 1
        db.get(ScoutMonitor, monitor_id).next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    # Raising the normal limit permits exactly one ordinary queued job. Its
    # later terminal failure/cancellation must not retain the old deferral.
    admitting = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / f"backoff-admit-{terminal_status}"),
        MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True, per_customer_daily_jobs=2),
    )
    assert admitting.schedule_one_due_monitor() is True
    claim = admitting.claim_next(f"monitor-backoff-{terminal_status}")
    assert claim is not None
    with pg_scout.sessions() as db:
        job = db.get(ScoutResearchJob, claim.job_id)
        admitting._finish(db, job, claim.token, terminal_status, terminal_status, False)
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        assert monitor.consecutive_deferrals == 0
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    # The failed/canceled job now consumes the two-job budget. The next
    # refusal starts over at 15 minutes rather than retaining the old backoff.
    assert admitting.schedule_one_due_monitor() is True
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        deferred = db.scalar(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.status == "deferred"
        ).order_by(ScoutMonitorRun.scheduled_for.desc()))
    assert deferred is not None and deferred.error_class == "scout_daily_job_limit"
    assert monitor.consecutive_deferrals == 1
    assert monitor.next_run_at - deferred.scheduled_for == timedelta(minutes=15)


def test_postgres_suspended_monitor_defers_without_reserving_a_job(
    pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    """A saved monitor cannot revive queue work after its owner is suspended."""
    customer = pg_scout.customer("monitor-suspended")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    with TestClient(scout_api) as client:
        saved = client.post(
            f"/api/v1/scout/jobs/{baseline.id}/monitor", json={},
            headers={"x-test-customer": str(customer.id)},
        )
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    with pg_scout.sessions() as db:
        stored_customer = db.get(ApiCustomer, customer.id)
        stored_customer.suspended_at = datetime.now(timezone.utc)
        monitor = db.get(ScoutMonitor, monitor_id)
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-suspended-raw"), MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True),
    )
    assert runner.schedule_one_due_monitor() is True
    with pg_scout.sessions() as db:
        jobs = db.scalars(select(ScoutResearchJob).where(
            ScoutResearchJob.customer_id == customer.id
        )).all()
        deferred = db.scalar(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.status == "deferred"
        ))
    assert [job.id for job in jobs] == [baseline.id]
    assert deferred is not None and deferred.error_class == "scout_rollout_not_available"


def test_postgres_terminal_monitor_reconciliation_does_not_lock_readonly_job(
    pg_scout: PostgresScoutHarness, tmp_path
):
    """A held job lock must not make the run-only reconciliation skip work."""
    customer = pg_scout.customer("monitor-reconcile-lock")
    now = datetime.now(timezone.utc)
    with pg_scout.sessions() as db:
        job = ScoutResearchJob(
            customer_id=customer.id, original_query="HB 625", normalized_query="hb 625",
            jurisdiction="FL", cache_key=f"monitor-reconcile-{uuid.uuid4().hex}", status="completed",
            strategy={}, limits={}, usage={}, completed_at=now,
        )
        monitor = ScoutMonitor(
            customer_id=customer.id, original_query="HB 625", normalized_query="hb 625",
            jurisdiction="FL", cache_key=job.cache_key, cadence_seconds=6 * 60 * 60,
            next_run_at=now,
        )
        db.add_all((job, monitor))
        db.flush()
        run = ScoutMonitorRun(
            monitor_id=monitor.id, job_id=job.id, status="queued", execution_mode="new",
            scheduled_for=now, source_snapshot={}, change_summary={},
        )
        db.add(run)
        db.commit()
        job_id, run_id = job.id, run.id
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-reconcile-lock"), MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True),
    )
    with pg_scout.sessions() as locking_db:
        # If reconciliation's joined SELECT locks ScoutResearchJob as well,
        # SKIP LOCKED silently misses this otherwise-ready run.
        locking_db.execute(select(ScoutResearchJob).where(
            ScoutResearchJob.id == job_id
        ).with_for_update()).scalar_one()
        assert runner._reconcile_terminal_monitor_run(run_id) is True
        locking_db.rollback()
    with pg_scout.sessions() as db:
        reconciled = db.get(ScoutMonitorRun, run_id)
    assert reconciled.status == "completed"



def test_postgres_monitor_scheduler_and_ad_hoc_admission_share_owner_and_platform_locks(
    monkeypatch, pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    """A due monitor and a direct request contend safely without queue bypass or deadlock."""
    customer = pg_scout.customer("monitor-admission-race")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "800")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_PLATFORM_MAX_DAILY_BROWSER_SECONDS", "800")
    with TestClient(scout_api) as client:
        saved = client.post(
            f"/api/v1/scout/jobs/{baseline.id}/monitor", json={},
            headers={"x-test-customer": str(customer.id)},
        )
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-admission-race"), MockResearchBrowserProvider(),
        # Use the same environment-derived limits as the API contestant.
        settings=ScoutSettings.from_env(),
    )
    barrier = threading.Barrier(2)

    def schedule() -> bool:
        barrier.wait(timeout=10)
        return runner.schedule_one_due_monitor()

    def submit() -> str:
        with pg_scout.sessions() as db:
            barrier.wait(timeout=10)
            result = scout.create_job(
                scout.CreateScoutJob(query="HB 626", jurisdiction="FL"),
                _direct_request_for(customer), Response(), db,
            )
            return "created" if not result["coalesced"] else "coalesced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        scheduled, submitted = list(pool.map(lambda fn: fn(), (schedule, submit)))
    assert scheduled is True and submitted == "created"
    with pg_scout.sessions() as db:
        active = db.scalars(select(ScoutResearchJob).where(
            ScoutResearchJob.customer_id == customer.id,
            ScoutResearchJob.status.in_(("queued", "running")),
        )).all()
        monitor_runs = db.scalars(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.status == "queued"
        )).all()
    assert len(active) == 2
    assert len(monitor_runs) == 1 and monitor_runs[0].job_id in {job.id for job in active}


def test_postgres_pause_while_monitor_job_active_finalizes_that_run_once(
    pg_scout: PostgresScoutHarness, scout_api, tmp_path
):
    customer = pg_scout.customer("monitor-pause-active")
    baseline = _terminal_monitor_baseline(pg_scout, customer)
    headers = {"x-test-customer": str(customer.id)}
    with TestClient(scout_api) as client:
        saved = client.post(f"/api/v1/scout/jobs/{baseline.id}/monitor", json={}, headers=headers)
        assert saved.status_code == 201
        monitor_id = uuid.UUID(saved.json()["monitor"]["id"])
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        monitor.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    runner = ScoutRunner(
        pg_scout.sessions, FilesystemRawStore(tmp_path / "monitor-pause-active"), MockResearchBrowserProvider(),
        settings=ScoutSettings(enabled=True, allow_public_rollout=True),
    )
    assert runner.schedule_one_due_monitor() is True
    with pg_scout.sessions() as db:
        run = db.scalar(select(ScoutMonitorRun).where(
            ScoutMonitorRun.monitor_id == monitor_id, ScoutMonitorRun.status == "queued"
        ))
        assert run is not None
        job_id = run.job_id
    with TestClient(scout_api) as client:
        paused = client.patch(f"/api/v1/scout/monitors/{monitor_id}", json={"active": False}, headers=headers)
        assert paused.status_code == 200 and paused.json()["monitor"]["active"] is False
    claim = runner.claim_next("monitor-pause-test")
    assert claim is not None and claim.job_id == job_id
    with pg_scout.sessions() as db:
        job = db.get(ScoutResearchJob, job_id)
        source = ScoutSource(
            job_id=job.id, canonical_url="https://www.flsenate.gov/Session/Bill/2026/625",
            official=True, retrieval_mechanism="direct", content_hash="c" * 64, raw_ref="c" * 64,
        )
        db.add(source)
        db.flush()
        db.add(ScoutFinding(
            job_id=job.id, source_id=source.id, title="HB 625", what_happened="Finalized after pause.",
            confidence="high", extractor_version="monitor-test",
        ))
        runner._finish(db, job, claim.token, "completed", None, False)
        runner._finish(db, job, claim.token, "completed", None, False)
    with pg_scout.sessions() as db:
        monitor = db.get(ScoutMonitor, monitor_id)
        runs = db.scalars(select(ScoutMonitorRun).where(ScoutMonitorRun.job_id == job_id)).all()
        finished = db.scalars(select(ScoutJobEvent).where(
            ScoutJobEvent.job_id == job_id, ScoutJobEvent.kind == "finished"
        )).all()
    assert monitor.active is False
    assert len(runs) == 1 and runs[0].status == "completed"
    assert len(finished) == 1
    assert runner.schedule_one_due_monitor() is False
