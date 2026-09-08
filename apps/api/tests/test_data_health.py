"""Public report caching, failure isolation and API contract."""
import threading

import pytest
from fastapi import HTTPException

from billcommons_api.routers import data_health as module


def test_cache_reuses_observation_until_expiry():
    now = [0.0]
    cache = module.ReportCache(clock=lambda: now[0])
    calls = []

    def load():
        calls.append(1)
        return {"generated_at": len(calls)}

    assert cache.get(load) == {"generated_at": 1}
    now[0] = 299
    assert cache.get(load) == {"generated_at": 1}
    now[0] = 300
    assert cache.get(load) == {"generated_at": 2}
    assert len(calls) == 2


def test_failed_refresh_never_returns_expired_success_and_releases_lock():
    now = [0.0]
    cache = module.ReportCache(clock=lambda: now[0])
    cache.get(lambda: {"generated_at": "old"})
    now[0] = 301

    def broken():
        raise RuntimeError("private diagnostic")

    with pytest.raises(RuntimeError):
        cache.get(broken)
    with pytest.raises(HTTPException) as error:
        cache.get(lambda: pytest.fail("failed scan restarted during cooldown"))
    assert error.value.status_code == 503
    assert error.value.headers["Retry-After"] == "30"
    now[0] += module.FAILURE_RETRY_SECONDS
    assert cache.get(lambda: {"generated_at": "new"}) == {"generated_at": "new"}


def test_failed_refresh_cooldown_bounds_repeated_public_requests():
    now = [0.0]
    cache = module.ReportCache(clock=lambda: now[0])
    calls = []
    def broken():
        calls.append(1)
        raise RuntimeError("private diagnostic")
    with pytest.raises(RuntimeError):
        cache.get(broken)
    for second in range(1, 30):
        now[0] = float(second)
        with pytest.raises(HTTPException) as error:
            cache.get(broken)
        assert error.value.headers["Retry-After"] == str(30 - second)
    assert len(calls) == 1
    now[0] = 30.0
    with pytest.raises(RuntimeError):
        cache.get(broken)
    assert len(calls) == 2


def test_concurrent_refresh_does_not_start_second_database_scan():
    cache = module.ReportCache()
    entered = threading.Event()
    finish = threading.Event()

    def load():
        entered.set()
        assert finish.wait(5)
        return {"generated_at": "now"}

    thread = threading.Thread(target=lambda: cache.get(load))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(HTTPException) as error:
            cache.get(lambda: pytest.fail("duplicate database scan"))
        assert error.value.status_code == 503
        assert error.value.headers["Retry-After"] == "5"
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive()


def test_public_report_route(client, monkeypatch):
    report = {"report_version": 1, "generated_at": "2026-09-08T00:00:00+00:00",
              "jurisdictions": [], "defects": [], "summary": {}}
    monkeypatch.setattr(module, "_cache", module.ReportCache())
    monkeypatch.setattr(module, "_load_report", lambda: report)
    response = client.get("/api/v1/data-health")
    assert response.status_code == 200
    assert response.json() == report
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-data-health-max-age"] == "300"


def test_public_error_does_not_leak_database_details(client, monkeypatch):
    def broken():
        raise RuntimeError("private diagnostic must never be returned")

    monkeypatch.setattr(module, "_cache", module.ReportCache())
    monkeypatch.setattr(module, "_load_report", broken)
    response = client.get("/api/v1/data-health")
    assert response.status_code == 503
    assert "private diagnostic" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "30"


def test_database_snapshot_is_read_only_and_closed(monkeypatch):
    statements = []

    class Session:
        def execute(self, stmt):
            statements.append(str(stmt))

        def rollback(self):
            statements.append("rollback")

        def close(self):
            statements.append("close")

    monkeypatch.setattr(module, "get_session", Session)
    monkeypatch.setattr(module, "collect_report", lambda db: {"ok": True})
    assert module._load_report() == {"ok": True}
    assert statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SET LOCAL statement_timeout = '5s'", "rollback", "close",
    ]


def test_rollback_failure_still_closes_session(monkeypatch):
    closed = []

    class Session:
        def execute(self, stmt):
            pass

        def rollback(self):
            raise RuntimeError("private rollback failure")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "get_session", Session)
    monkeypatch.setattr(module, "collect_report", lambda db: {})
    with pytest.raises(RuntimeError):
        module._load_report()
    assert closed == [True]


def test_cache_publishes_payload_and_expiry_as_one_generation():
    now = [0.0]
    cache = module.ReportCache(clock=lambda: now[0])
    old = {'generated_at': 'old'}
    fresh = {'generated_at': 'fresh'}
    cache.get(lambda: old)
    now[0] = 301
    replaced = [False]

    def clock_with_concurrent_publication():
        if not replaced[0]:
            replaced[0] = True
            cache.entry = (fresh, 601.0)
        return now[0]

    cache.clock = clock_with_concurrent_publication
    result = cache.get(lambda: pytest.fail('new cache generation should be reused'))
    assert result is fresh


def test_actual_postgres_report_route_reads_snapshot(client, monkeypatch):
    # Exercise SQLAlchemy autobegin + SET TRANSACTION on actual PostgreSQL.
    monkeypatch.setattr(module, '_cache', module.ReportCache())
    response = client.get('/api/v1/data-health')
    assert response.status_code == 200
    body = response.json()
    assert body['summary']['jurisdiction_count'] == 51
    assert response.headers['cache-control'] == 'no-store'


def test_collection_time_counts_against_observation_lifetime():
    now = [0.0]
    cache = module.ReportCache(clock=lambda: now[0])
    def slow_load():
        now[0] += 45
        return {"generation": "first"}
    assert cache.get(slow_load)["generation"] == "first"
    now[0] = 299
    assert cache.get(lambda: pytest.fail("early refresh"))["generation"] == "first"
    now[0] = 300
    assert cache.get(lambda: {"generation": "second"})["generation"] == "second"


def test_expired_during_collection_is_never_published():
    now = [0.0]
    cache = module.ReportCache(clock=lambda: now[0])
    def expired_load():
        now[0] = module.REPORT_TTL_SECONDS
        return {"generation": "expired"}
    with pytest.raises(RuntimeError):
        cache.get(expired_load)
    assert cache.entry is None
    with pytest.raises(HTTPException):
        cache.get(lambda: pytest.fail("failure cooldown bypassed"))
