from __future__ import annotations

import uuid
import inspect
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from billcommons_api.app import create_app
from billcommons_api.deps import get_db
from billcommons_api.routers import scout
from billcommons_schema.base import Base
from billcommons_schema.models import ApiCustomer, ScoutBrowserSession, ScoutJobEvent, ScoutResearchJob, ScoutSource


def _app(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def uuid_default(conn, _record):
        conn.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    tables = [Base.metadata.tables[name] for name in (
        "api_customers", "scout_research_jobs", "scout_job_events", "scout_sources",
        "scout_findings", "scout_browser_sessions",
    )]
    Base.metadata.create_all(engine, tables=tables)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    owner = ApiCustomer(id=uuid.uuid4(), email="owner@example.test")
    other = ApiCustomer(id=uuid.uuid4(), email="other@example.test")
    with sessions() as db:
        db.add_all((owner, other))
        db.commit()
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "1")
    monkeypatch.setattr(scout, "_check_origin", lambda request: None)
    monkeypatch.setattr(scout, "_require_session", lambda request, db: db.get(ApiCustomer, uuid.UUID(request.headers["x-test-customer"])))
    app = create_app()

    def db_override():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = db_override
    return app, owner, other, sessions


def test_scout_create_coalesces_and_owner_scopes_reads(monkeypatch):
    app, owner, other, sessions = _app(monkeypatch)
    with TestClient(app) as client:
        headers = {"x-test-customer": str(owner.id)}
        first = client.post("/api/v1/scout/jobs", json={"query": "HB 12", "jurisdiction": "FL"}, headers=headers)
        assert first.status_code == 201
        job_id = first.json()["job"]["id"]
        assert first.json()["job"]["findings"] == []
        duplicate = client.post("/api/v1/scout/jobs", json={"query": "  hb  12 ", "jurisdiction": "fl"}, headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json()["coalesced"] is True
        assert duplicate.json()["job"]["id"] == job_id
        denied = client.get(f"/api/v1/scout/jobs/{job_id}", headers={"x-test-customer": str(other.id)})
        assert denied.status_code == 404
        with sessions() as db:
            browser = ScoutBrowserSession(job_id=uuid.UUID(job_id), provider="mock", status="released")
            db.add(browser)
            db.commit()
            browser_id = browser.id
        denied_evidence = client.get(f"/api/v1/scout/jobs/{job_id}/evidence", headers={"x-test-customer": str(other.id)})
        assert denied_evidence.status_code == 404
        denied_replay = client.get(f"/api/v1/scout/jobs/{job_id}/browser-sessions/{browser_id}/replay", headers={"x-test-customer": str(other.id)})
        assert denied_replay.status_code == 404
        cancel = client.post(f"/api/v1/scout/jobs/{job_id}/cancel", headers=headers)
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "canceled"
        repeat = client.post(f"/api/v1/scout/jobs/{job_id}/cancel", headers=headers)
        assert repeat.status_code == 200 and repeat.json()["status"] == "canceled"
        with sessions() as db:
            terminal = db.execute(
                select(ScoutJobEvent).where(
                    ScoutJobEvent.job_id == uuid.UUID(job_id),
                    ScoutJobEvent.kind == "finished",
                )
            ).scalars().all()
            assert [event.detail["status"] for event in terminal] == ["canceled"]


def test_scout_is_dark_when_feature_flag_is_off(monkeypatch):
    app, owner, _other, _sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "0")
    with TestClient(app) as client:
        response = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers={"x-test-customer": str(owner.id)})
    assert response.status_code == 404


def test_scout_reuses_only_fresh_terminal_cache(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers).json()["job"]
        with sessions() as db:
            job = db.get(ScoutResearchJob, uuid.UUID(created["id"]))
            job.status = "completed"
            job.fresh_until = datetime.now(timezone.utc) + timedelta(minutes=1)
            db.commit()
        cached = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        assert cached.status_code == 200 and cached.json()["cached"] is True
        with sessions() as db:
            job = db.get(ScoutResearchJob, uuid.UUID(created["id"]))
            job.fresh_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        stale = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        assert stale.status_code == 201


def test_scout_payload_marks_an_expired_cache_as_a_miss(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers).json()["job"]
        with sessions() as db:
            job = db.get(ScoutResearchJob, uuid.UUID(created["id"]))
            job.status = "completed"
            job.fresh_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        assert client.get(f"/api/v1/scout/jobs/{created['id']}", headers=headers).json()["cache_status"] == "miss"
        with sessions() as db:
            job = db.get(ScoutResearchJob, uuid.UUID(created["id"]))
            job.fresh_until = datetime.now(timezone.utc) + timedelta(minutes=1)
            db.commit()
        assert client.get(f"/api/v1/scout/jobs/{created['id']}", headers=headers).json()["cache_status"] == "fresh"


def test_scout_payload_exposes_bounded_source_change_provenance(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers).json()["job"]
        job_id = uuid.UUID(created["id"])
        with sessions() as db:
            prior = ScoutSource(job_id=job_id, canonical_url="https://www.flsenate.gov/prior", official=True, retrieval_mechanism="direct")
            db.add(prior)
            db.flush()
            source = ScoutSource(
                job_id=job_id,
                canonical_url="https://www.flsenate.gov/current",
                official=True,
                retrieval_mechanism="direct",
                prior_source_id=prior.id,
                change_kind="material",
                change_summary="Normalized text changed (12→20 chars; first difference at 8).",
            )
            db.add(source)
            db.commit()
            source_id = str(source.id)
        payload = client.get(f"/api/v1/scout/jobs/{job_id}", headers=headers).json()
    returned = next(source for source in payload["sources"] if source["id"] == source_id)
    assert returned["prior_source_id"] == str(prior.id)
    assert returned["change_kind"] == "material"
    assert returned["change_summary"] == "Normalized text changed (12→20 chars; first difference at 8)."


def test_scout_quota_decision_locks_the_customer_row_before_counting():
    source = inspect.getsource(scout.create_job)
    lock = source.index("with_for_update()")
    count = source.index("active_count")
    assert lock < count, "Postgres must serialize the per-customer quota check/create decision"


def test_scout_daily_job_budget_is_durable_but_cached_work_remains_reusable(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_JOBS", "1")
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        assert created.status_code == 201
        job_id = uuid.UUID(created.json()["job"]["id"])
        with sessions() as db:
            job = db.get(ScoutResearchJob, job_id)
            job.status = "completed"
            job.fresh_until = datetime.now(timezone.utc) + timedelta(minutes=5)
            db.commit()
        cached = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        assert cached.status_code == 200 and cached.json()["cached"] is True
        limited = client.post("/api/v1/scout/jobs", json={"query": "HB 13"}, headers=headers)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "scout_daily_job_limit"


def test_scout_daily_browser_runtime_blocks_new_spend(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "1")
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        job_id = uuid.UUID(created.json()["job"]["id"])
        with sessions() as db:
            job = db.get(ScoutResearchJob, job_id)
            job.status = "completed"
            db.add(ScoutBrowserSession(job_id=job_id, provider="mock", status="released", runtime_ms=1000))
            db.commit()
        limited = client.post("/api/v1/scout/jobs", json={"query": "HB 13"}, headers=headers)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "scout_daily_browser_limit"
