"""health/ready must report a real DB ping, not a hardcoded 200 -- these are
used by Railway for liveness/readiness so a lie here masks real outages."""
from __future__ import annotations


def test_health_pings_db_and_reports_ok(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_ready_reports_ready_true_when_db_reachable(client):
    resp = client.get("/api/v1/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["database"] == "ok"
