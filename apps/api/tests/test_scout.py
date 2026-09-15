from __future__ import annotations

import uuid
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from billcommons_api.app import create_app
from billcommons_api.deps import get_db
from billcommons_api.routers import scout
from billcommons_schema.base import Base
from billcommons_schema.models import ApiCustomer, ScoutBrowserSession, ScoutFinding, ScoutJobEvent, ScoutMonitor, ScoutMonitorRun, ScoutResearchJob, ScoutSource
from billcommons_shared.scout import scout_cache_key
from billcommons_shared.scout_admission import admit_scout_job
from billcommons_shared.scout_monitors import compare_snapshots


def _app(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def uuid_default(conn, _record):
        conn.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    tables = [Base.metadata.tables[name] for name in (
        "api_customers", "scout_research_jobs", "scout_job_events", "scout_sources",
        "scout_findings", "scout_browser_sessions", "scout_monitors", "scout_monitor_runs",
        # New-job admission totals durable raw evidence before it writes a
        # queue row, so the router fixture needs the production dependency.
        "scout_raw_blobs",
    )]
    Base.metadata.create_all(engine, tables=tables)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    owner = ApiCustomer(id=uuid.uuid4(), email="owner@example.test")
    other = ApiCustomer(id=uuid.uuid4(), email="other@example.test")
    with sessions() as db:
        db.add_all((owner, other))
        db.commit()
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ALLOW_PUBLIC", "1")
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


def test_monitor_list_reports_configured_policy_even_when_creation_disabled(monkeypatch):
    app, owner, _other, _sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "0")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_SAVED_MONITORS", "5")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MONITOR_MIN_CADENCE_SECONDS", "43200")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MONITOR_MAX_CADENCE_SECONDS", "43200")
    with TestClient(app) as client:
        response = client.get("/api/v1/scout/monitors", headers={"x-test-customer": str(owner.id)})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"monitors": [], "policy": {
        "max_saved_monitors": 5, "min_cadence_seconds": 43200, "max_cadence_seconds": 43200,
    }}


def test_scout_california_admission_requires_explicit_session_and_uses_retained_strategy(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        rejected = client.post(
            "/api/v1/scout/jobs", json={"query": "AB 123", "jurisdiction": "CA"}, headers=headers
        )
        assert rejected.status_code == 422
        created = client.post(
            "/api/v1/scout/jobs",
            json={"query": "AB 123 2025-2026", "jurisdiction": "CA"},
            headers=headers,
        )
        assert created.status_code == 201
        job_id = uuid.UUID(created.json()["job"]["id"])
    with sessions() as db:
        job = db.get(ScoutResearchJob, job_id)
        assert job.strategy == {
            "adapter": "california_retained_p0",
            "mode": "retained_official_archive",
        }
        assert job.cache_key == scout_cache_key(
            "AB 123 2025-2026", "CA", freshness_bucket="scout-ca-retained-p0"
        )


def test_scout_california_terminal_evidence_can_be_saved_as_a_monitor(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    cache_key = scout_cache_key("AB 123 2025-2026", "CA", freshness_bucket="scout-ca-retained-p0")
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id, original_query="AB 123 2025-2026", normalized_query="ab 123 2025-2026",
            jurisdiction="CA", cache_key=cache_key, status="completed",
            strategy={"adapter": "california_retained_p0", "mode": "retained_official_archive"},
            limits={"max_ca_parse_seconds": 5}, usage={}, completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.flush()
        source = ScoutSource(
            job_id=job.id, canonical_url="https://www.leginfo.ca.gov/pub/25-26/bill/asm/ab_0101-0150/ab_123_bill_20260115_status.html",
            official=True, retrieval_mechanism="official_retained_archive", content_hash="a" * 64, raw_ref="a" * 64,
        )
        db.add(source)
        db.flush()
        db.add(ScoutFinding(
            job_id=job.id, source_id=source.id, title="AB 123", what_happened="Read first time.",
            confidence="high", extractor_version="ca-monitor-test",
        ))
        db.commit()
        job_id = job.id
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post(f"/api/v1/scout/jobs/{job_id}/monitor", json={}, headers=headers)
        assert created.status_code == 201
        monitor = created.json()["monitor"]
        history = client.get(f"/api/v1/scout/monitors/{monitor['id']}/runs", headers=headers)
    assert history.status_code == 200
    payload = history.json()
    assert payload["next_cursor"] is None
    assert payload["runs"][0]["status"] == "baseline"
    assert payload["runs"][0]["source_snapshot"]["sources"][0]["content_hash"] == "a" * 64
    with sessions() as db:
        saved = db.get(ScoutMonitor, uuid.UUID(monitor["id"]))
        original = db.get(ScoutResearchJob, job_id)
    assert saved.cache_key == cache_key
    assert original.strategy["adapter"] == "california_retained_p0"
    assert original.limits["max_ca_parse_seconds"] == 5


def test_scout_partial_monitor_snapshot_excludes_unverified_stages(monkeypatch):
    """A partial result may retain final sources, but never staging barriers."""
    app, owner, _other, sessions = _app(monkeypatch)
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id, original_query="AB 123 2025-2026", normalized_query="ab 123 2025-2026",
            jurisdiction="CA", cache_key="partial-monitor-stages", status="partial",
            strategy={"adapter": "california_retained_p0", "mode": "retained_official_archive"},
            limits={}, usage={}, completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.flush()
        evidenced = ScoutSource(
            job_id=job.id, canonical_url="https://www.leginfo.ca.gov/retained/evidenced.zip",
            official=True, retrieval_mechanism="official_retained_archive",
            content_hash="a" * 64, raw_ref="a" * 64,
        )
        no_finding = ScoutSource(
            job_id=job.id, canonical_url="https://www.leginfo.ca.gov/retained/no-finding.zip",
            official=True, retrieval_mechanism="official_retained_archive",
            content_hash="b" * 64, raw_ref="b" * 64,
        )
        # First staging barrier: RawStore has not accepted the bytes yet.
        unresolved_stage = ScoutSource(
            job_id=job.id, canonical_url="https://www.leginfo.ca.gov/retained/unresolved.zip",
            official=False, retrieval_mechanism="staged",
        )
        # Reclaimed claim: a stage can keep its hash/ref for garbage collection
        # after a fenced worker loses ownership. It is still not final evidence.
        reclaimed_stage = ScoutSource(
            job_id=job.id, canonical_url="https://www.leginfo.ca.gov/retained/reclaimed.zip",
            official=False, retrieval_mechanism="staged",
            content_hash="c" * 64, raw_ref="c" * 64,
        )
        incomplete_final = ScoutSource(
            job_id=job.id, canonical_url="https://www.leginfo.ca.gov/retained/incomplete.zip",
            official=True, retrieval_mechanism="official_retained_archive",
        )
        db.add_all((evidenced, no_finding, unresolved_stage, reclaimed_stage, incomplete_final))
        db.flush()
        db.add(ScoutFinding(
            job_id=job.id, source_id=evidenced.id, title="AB 123", what_happened="Retained action.",
            confidence="high", extractor_version="partial-monitor-test",
        ))
        db.commit()
        job_id = job.id
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post(f"/api/v1/scout/jobs/{job_id}/monitor", json={}, headers=headers)
        assert created.status_code == 201
        monitor_id = created.json()["monitor"]["id"]
        history = client.get(f"/api/v1/scout/monitors/{monitor_id}/runs", headers=headers)
    snapshot = history.json()["runs"][0]["source_snapshot"]
    by_url = {source["canonical_url"]: source for source in snapshot["sources"]}
    assert set(by_url) == {
        "https://www.leginfo.ca.gov/retained/evidenced.zip",
        "https://www.leginfo.ca.gov/retained/no-finding.zip",
    }
    assert by_url["https://www.leginfo.ca.gov/retained/no-finding.zip"]["finding_ids"] == []


def test_scout_monitor_rejects_finding_without_final_snapshot_source(monkeypatch):
    """A finding cannot make staged or incomplete evidence monitorable."""
    app, owner, _other, sessions = _app(monkeypatch)
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id, original_query="AB 123 2025-2026", normalized_query="ab 123 2025-2026",
            jurisdiction="CA", cache_key="monitor-incomplete-only", status="partial",
            strategy={"adapter": "california_retained_p0", "mode": "retained_official_archive"},
            limits={}, usage={}, completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.flush()
        incomplete = ScoutSource(
            job_id=job.id, canonical_url="https://www.leginfo.ca.gov/retained/incomplete-only.zip",
            official=True, retrieval_mechanism="official_retained_archive",
        )
        db.add(incomplete)
        db.flush()
        db.add(ScoutFinding(
            job_id=job.id, source_id=incomplete.id, title="AB 123", what_happened="Incomplete retention.",
            confidence="high", extractor_version="incomplete-monitor-test",
        ))
        db.commit()
        job_id = job.id
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        rejected = client.post(f"/api/v1/scout/jobs/{job_id}/monitor", json={}, headers=headers)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "monitor_baseline_missing_final_evidence"
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ScoutMonitor).where(
            ScoutMonitor.customer_id == owner.id
        )) == 0


def test_scout_monitor_compares_latest_retained_version_not_uuid_order(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    now = datetime.now(timezone.utc)
    url = "https://www.flsenate.gov/Session/Bill/2026/625"
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id, original_query="HB 625", normalized_query="hb 625",
            jurisdiction="FL", cache_key="monitor-version-order", status="completed",
            completed_at=now, strategy={}, limits={}, usage={},
        )
        db.add(job)
        db.flush()
        # A reclaimed job can retain an earlier final source and then fetch a
        # changed version. UUID ordering deliberately opposes observation time.
        newer = ScoutSource(
            id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"), job_id=job.id,
            canonical_url=url, official=True, retrieval_mechanism="direct",
            content_hash="b" * 64, raw_ref="raw/" + "b" * 64, retrieved_at=now,
        )
        older = ScoutSource(
            id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"), job_id=job.id,
            canonical_url=url, official=True, retrieval_mechanism="direct",
            content_hash="a" * 64, raw_ref="raw/" + "a" * 64,
            retrieved_at=now - timedelta(minutes=5),
        )
        db.add_all((newer, older))
        db.flush()
        db.add(ScoutFinding(
            job_id=job.id, source_id=newer.id, title="Retained version", what_happened="Official text retained",
            excerpt="Official text", excerpt_hash="c" * 64, confidence=1.0, extractor_version="test",
        ))
        db.commit()
        job_id = job.id
    with TestClient(app) as client:
        headers = {"x-test-customer": str(owner.id)}
        saved = client.post(f"/api/v1/scout/jobs/{job_id}/monitor", json={}, headers=headers)
        assert saved.status_code == 201
        monitor_id = saved.json()["monitor"]["id"]
        history = client.get(f"/api/v1/scout/monitors/{monitor_id}/runs", headers=headers)
        assert history.status_code == 200
    snapshot = history.json()["runs"][0]["source_snapshot"]
    latest_only = {"sources": [{"canonical_url": url, "content_hash": "b" * 64}]}
    # Retain both evidence references, but compare the newest version in either
    # baseline/current position. A retry must not manufacture an A↔B change.
    assert len(snapshot["sources"]) == 2
    for before, after in ((snapshot, latest_only), (latest_only, snapshot)):
        result = compare_snapshots(before, after, complete=True)
        assert result["changed_sources"] == []
        assert result["unchanged_source_count"] == 1


def test_scout_creation_snapshots_document_processing_caps(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_RELATED_DOCUMENTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DIRECT_BYTES", "1024")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_PDF_PAGES", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_PDF_TEXT_CHARS", "400")
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 625"}, headers={"x-test-customer": str(owner.id)}
        )
        assert created.status_code == 201
        job_id = uuid.UUID(created.json()["job"]["id"])
        # A subsequent rollout may change its process setting, but this queued
        # job retains the cap under which it was admitted.
        monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_RELATED_DOCUMENTS", "2")
        monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DIRECT_BYTES", "2048")
        monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_PDF_PAGES", "4")
        monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_PDF_TEXT_CHARS", "800")
        with sessions() as db:
            limits = db.get(ScoutResearchJob, job_id).limits
            assert {
                name: limits[name]
                for name in ("max_related_documents", "max_related_vote_records", "max_related_bill_versions", "max_related_meeting_documents", "max_direct_bytes", "max_pdf_pages", "max_pdf_text_chars")
            } == {
                "max_related_documents": 1,
                "max_related_vote_records": 1,
                "max_related_bill_versions": 1,
                "max_related_meeting_documents": 1,
                "max_direct_bytes": 1024,
                "max_pdf_pages": 3,
                "max_pdf_text_chars": 400,
            }


def test_scout_is_dark_when_feature_flag_is_off(monkeypatch):
    app, owner, _other, _sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "0")
    with TestClient(app) as client:
        response = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers={"x-test-customer": str(owner.id)})
    assert response.status_code == 404


def test_scout_requires_canary_or_explicit_public_rollout(monkeypatch):
    app, owner, _other, _sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ALLOW_PUBLIC", "0")
    monkeypatch.delenv("BILLCOMMONS_SCOUT_CANARY_EMAILS", raising=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/scout/jobs",
            json={"query": "HB 12"},
            headers={"x-test-customer": str(owner.id)},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "scout_canary_not_configured"


def test_scout_private_canary_gates_new_jobs_but_not_existing_owner_access(monkeypatch):
    app, owner, other, _sessions = _app(monkeypatch)
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 625"}, headers=headers
        )
        assert created.status_code == 201
        job_id = created.json()["job"]["id"]

        monkeypatch.setenv(
            "BILLCOMMONS_SCOUT_CANARY_EMAILS", f" {other.email.upper()} "
        )
        denied = client.post(
            "/api/v1/scout/jobs", json={"query": "SB 2"}, headers=headers
        )
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "scout_not_available"

        allowed = client.post(
            "/api/v1/scout/jobs",
            json={"query": "SB 2"},
            headers={"x-test-customer": str(other.id)},
        )
        assert allowed.status_code == 201

        # Removing an owner from the create cohort never strands their durable
        # record or prevents them from canceling already-admitted work.
        assert client.get(f"/api/v1/scout/jobs/{job_id}", headers=headers).status_code == 200
        assert client.post(f"/api/v1/scout/jobs/{job_id}/cancel", headers=headers).status_code == 200


def test_existing_owner_scoped_scout_reads_and_cancel_survive_dark_rollback(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    with TestClient(app) as client:
        headers = {"x-test-customer": str(owner.id)}
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        assert created.status_code == 201
        job_id = created.json()["job"]["id"]
        monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "0")
        assert client.get(f"/api/v1/scout/jobs/{job_id}", headers=headers).status_code == 200
        assert client.get(f"/api/v1/scout/jobs/{job_id}/evidence", headers=headers).status_code == 200
        assert client.post(f"/api/v1/scout/jobs/{job_id}/cancel", headers=headers).status_code == 200


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


def test_scout_does_not_reuse_a_fresh_pre_bill_text_cache_namespace(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    with sessions() as db:
        db.add(ScoutResearchJob(
            customer_id=owner.id,
            original_query="HB 625",
            normalized_query="hb 625",
            jurisdiction="FL",
            cache_key=scout_cache_key("HB 625", "FL", freshness_bucket="scout-p0-3-provenance"),
            status="completed",
            strategy={},
            limits={},
            usage={},
            fresh_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))
        db.commit()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 625"}, headers={"x-test-customer": str(owner.id)}
        )
    assert response.status_code == 201
    assert response.json()["coalesced"] is False


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
    assert returned["prior_source"]["job_id"] == str(job_id)
    assert returned["prior_source"]["canonical_url"] == "https://www.flsenate.gov/prior"
    assert returned["change_kind"] == "material"
    assert returned["change_summary"] == "Normalized text changed (12→20 chars; first difference at 8)."


def test_scout_payload_never_describes_cross_customer_prior_source(monkeypatch):
    app, owner, other, sessions = _app(monkeypatch)
    with TestClient(app) as client:
        owner_job = client.post(
            "/api/v1/scout/jobs",
            json={"query": "HB 12"},
            headers={"x-test-customer": str(owner.id)},
        ).json()["job"]
        other_job = client.post(
            "/api/v1/scout/jobs",
            json={"query": "SB 99"},
            headers={"x-test-customer": str(other.id)},
        ).json()["job"]
        with sessions() as db:
            foreign = ScoutSource(
                job_id=uuid.UUID(other_job["id"]),
                canonical_url="https://www.flsenate.gov/foreign",
                official=True,
                retrieval_mechanism="direct",
                content_hash="f" * 64,
            )
            db.add(foreign)
            db.flush()
            current = ScoutSource(
                job_id=uuid.UUID(owner_job["id"]),
                canonical_url="https://www.flsenate.gov/current",
                official=True,
                retrieval_mechanism="direct",
                prior_source_id=foreign.id,
                change_kind="material",
            )
            db.add(current)
            db.commit()
            current_id = str(current.id)
        payload = client.get(
            f"/api/v1/scout/jobs/{owner_job['id']}",
            headers={"x-test-customer": str(owner.id)},
        ).json()
    returned = next(source for source in payload["sources"] if source["id"] == current_id)
    assert returned["prior_source_id"] is not None
    assert returned["prior_source"] is None


def test_scout_quota_decision_locks_the_customer_row_before_counting():
    source = inspect.getsource(admit_scout_job)
    lock = source.index("with_for_update()")
    count = source.index("_browser_budget_totals")
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
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "5")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        job_id = uuid.UUID(created.json()["job"]["id"])
        with sessions() as db:
            job = db.get(ScoutResearchJob, job_id)
            job.status = "completed"
            db.add(ScoutBrowserSession(
                job_id=job_id, provider="mock", provider_session_id="runtime-recorded",
                status="released", runtime_ms=2000,
            ))
            db.commit()
        limited = client.post("/api/v1/scout/jobs", json={"query": "HB 13"}, headers=headers)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "scout_daily_browser_limit"


def test_scout_daily_browser_budget_reserves_queued_work(monkeypatch):
    app, owner, _other, _sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        assert created.status_code == 201
        limited = client.post("/api/v1/scout/jobs", json={"query": "HB 13"}, headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "scout_daily_browser_limit"


def test_scout_daily_browser_budget_counts_yesterday_session_released_today(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    now = datetime.now(timezone.utc)
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id, original_query="yesterday", normalized_query="yesterday",
            jurisdiction="FL", cache_key=uuid.uuid4().hex, status="completed", strategy={}, limits={}, usage={},
            created_at=now - timedelta(days=1), completed_at=now,
        )
        db.add(job)
        db.flush()
        db.add(ScoutBrowserSession(
            job_id=job.id, provider="mock", provider_session_id="midnight", status="released",
            runtime_ms=3000, created_at=now - timedelta(seconds=1), released_at=now,
        ))
        db.commit()
    with TestClient(app) as client:
        limited = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 12"}, headers={"x-test-customer": str(owner.id)}
        )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "scout_daily_browser_limit"


def test_scout_reconciles_malformed_active_browser_reservation(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        assert created.status_code == 201
        for malformed in (True, 0, "not-a-number"):
            with sessions() as db:
                job = db.get(ScoutResearchJob, uuid.UUID(created.json()["job"]["id"]))
                job.limits = {
                    "daily_browser_reservation_ms": malformed,
                    "max_external_requests": 1,
                    "browser_wall_seconds": 1,
                    "browser_cleanup_seconds": 1,
                }
                db.commit()
            limited = client.post(
                "/api/v1/scout/jobs", json={"query": f"HB {13 + len(str(malformed))}"}, headers=headers
            )
            assert limited.status_code == 429


def test_scout_charges_started_released_session_without_runtime_telemetry(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id, original_query="unknown runtime", normalized_query="unknown runtime",
            jurisdiction="FL", cache_key=uuid.uuid4().hex, status="completed", strategy={},
            limits={"browser_wall_seconds": 1, "browser_cleanup_seconds": 1}, usage={},
        )
        db.add(job)
        db.flush()
        db.add(ScoutBrowserSession(
            job_id=job.id, provider="mock", provider_session_id="started-without-runtime",
            status="released", runtime_ms=None, released_at=datetime.now(timezone.utc),
        ))
        db.commit()
    with TestClient(app) as client:
        limited = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 99"}, headers={"x-test-customer": str(owner.id)}
        )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "scout_daily_browser_limit"


def test_scout_charges_full_reservation_for_unknown_create_outcome(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "3")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id,
            original_query="unknown create",
            normalized_query="unknown create",
            jurisdiction="FL",
            cache_key=uuid.uuid4().hex,
            status="partial",
            strategy={},
            limits={
                "max_external_requests": 1,
                "browser_wall_seconds": 1,
                "browser_cleanup_seconds": 1,
            },
            usage={},
        )
        db.add(job)
        db.flush()
        db.add(ScoutBrowserSession(
            job_id=job.id,
            provider="SolariResearchBrowserProvider",
            provider_session_id=None,
            status="abandoned",
            error_class="create_outcome_unknown",
        ))
        db.commit()
    with TestClient(app) as client:
        limited = client.post(
            "/api/v1/scout/jobs",
            json={"query": "HB 96"},
            headers={"x-test-customer": str(owner.id)},
        )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "scout_daily_browser_limit"


def test_scout_holds_full_daily_browser_cap_for_active_legacy_cleanup_limit(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "5")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    with sessions() as db:
        db.add(ScoutResearchJob(
            customer_id=owner.id, original_query="legacy", normalized_query="legacy", jurisdiction="FL",
            cache_key=uuid.uuid4().hex, status="running", strategy={},
            # This is an old job's persisted wall setting, deliberately
            # missing its cleanup limit while current settings have changed.
            limits={"max_external_requests": 1, "browser_wall_seconds": 1}, usage={},
        ))
        db.commit()
    with TestClient(app) as client:
        limited = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 98"}, headers={"x-test-customer": str(owner.id)}
        )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "scout_daily_browser_limit"


def test_scout_charges_full_daily_browser_cap_for_terminal_legacy_session(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "5")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    with sessions() as db:
        job = ScoutResearchJob(
            customer_id=owner.id, original_query="legacy settled", normalized_query="legacy settled",
            jurisdiction="FL", cache_key=uuid.uuid4().hex, status="completed", strategy={},
            limits={"max_external_requests": 1, "browser_wall_seconds": 1}, usage={},
        )
        db.add(job)
        db.flush()
        db.add(ScoutBrowserSession(
            job_id=job.id, provider="mock", provider_session_id="legacy-terminal", status="released",
            runtime_ms=0, released_at=datetime.now(timezone.utc),
        ))
        db.commit()
    with TestClient(app) as client:
        limited = client.post(
            "/api/v1/scout/jobs", json={"query": "HB 97"}, headers={"x-test-customer": str(owner.id)}
        )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "scout_daily_browser_limit"


def test_scout_owner_payload_derives_browser_routed_requests_from_started_sessions(monkeypatch):
    app, owner, _other, sessions = _app(monkeypatch)
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
        job_id = uuid.UUID(created.json()["job"]["id"])
        with sessions() as db:
            db.add(
                ScoutBrowserSession(
                    job_id=job_id,
                    provider="mock",
                    provider_session_id="started-session",
                    status="released",
                    routed_requests=3,
                )
            )
            db.commit()
        payload = client.get(f"/api/v1/scout/jobs/{job_id}", headers=headers).json()
    assert payload["usage"]["browser_routed_requests"] == 3


def test_scout_owner_payload_omits_unknown_browser_routed_requests(monkeypatch):
    app, owner, _other, _sessions = _app(monkeypatch)
    headers = {"x-test-customer": str(owner.id)}
    with TestClient(app) as client:
        created = client.post("/api/v1/scout/jobs", json={"query": "HB 12"}, headers=headers)
    assert "browser_routed_requests" not in created.json()["job"]["usage"]


def test_enabled_scout_rejects_infeasible_browser_budget_at_app_start(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", "2")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", "1")
    monkeypatch.setenv("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", "1")
    with pytest.raises(ValueError, match="DAILY_BROWSER"):
        create_app()


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", "0"),
        ("BILLCOMMONS_SCOUT_ALLOW_PUBLIC", "not-a-boolean"),
    ),
)
def test_enabled_scout_rejects_invalid_explicit_configuration_at_app_start(
    monkeypatch, name, value
):
    monkeypatch.setenv("BILLCOMMONS_SCOUT_ENABLED", "1")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        create_app()
