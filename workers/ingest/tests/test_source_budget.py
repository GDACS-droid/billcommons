"""Shared admission invariants against explicitly disposable PostgreSQL."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from billcommons_schema.models import SourceRequestBudget
from billcommons_shared import source_budget as module
from billcommons_shared.db import get_engine

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def scope():
    # Only committed rows owned by this test are removed. The ingestion
    # conftest and outer launcher require a disposable local database.
    value = "budget-test-" + uuid.uuid4().hex
    yield value
    with get_engine().begin() as db:
        db.execute(delete(SourceRequestBudget).where(SourceRequestBudget.scope == value))


def admit(scope, now, limit=7, interval=1):
    with Session(get_engine()) as db:
        decision = module.reserve_request(db, scope=scope, daily_limit=limit,
                                          minimum_interval_seconds=interval, now=now)
        db.commit()
        return decision


def test_concurrent_connections_share_daily_limit_and_pacing(scope):
    decisions = []
    with ThreadPoolExecutor(max_workers=8) as workers:
        for step in range(10):
            now = NOW + timedelta(seconds=step * 2)
            decisions.extend(workers.map(lambda _: admit(scope, now), range(8)))
    assert sum(d.admitted for d in decisions) == 7
    assert any(d.exhausted for d in decisions)
    assert all(d.requests_reserved <= 7 for d in decisions)
    with Session(get_engine()) as db:
        row = db.get(SourceRequestBudget, scope)
        assert row.requests_reserved == 7


def test_midnight_resets_count_without_resetting_global_pacing(scope):
    before = NOW.replace(hour=23, minute=59, second=58)
    assert admit(scope, before, limit=1, interval=10).admitted
    after = before + timedelta(seconds=3)
    waiting = admit(scope, after, limit=1, interval=10)
    assert not waiting.admitted and not waiting.exhausted
    assert waiting.requests_reserved == 0
    assert waiting.retry_after_seconds == 7
    assert admit(scope, before + timedelta(seconds=10), limit=1, interval=10).admitted
    assert admit(scope, before + timedelta(seconds=20), limit=1, interval=10).exhausted


def test_restart_or_ingest_rollback_does_not_refund_admission(scope):
    with Session(get_engine()) as ingest:
        ingest.execute(select(1))
        module.consume_request(scope=scope, daily_limit=1, minimum_interval_seconds=1,
                               session_factory=lambda: Session(get_engine()))
        ingest.rollback()
    # Recreate the session/caller, just as a replacement process would.
    with pytest.raises(module.RequestBudgetExhausted):
        module.consume_request(scope=scope, daily_limit=1, minimum_interval_seconds=1,
                               session_factory=lambda: Session(get_engine()))
    with Session(get_engine()) as db:
        assert db.get(SourceRequestBudget, scope).requests_reserved == 1


def test_tighter_callers_cannot_be_overridden_later_that_day(scope):
    assert admit(scope, NOW, limit=5, interval=1).admitted
    assert admit(scope, NOW + timedelta(seconds=1), limit=2, interval=10).admitted
    limited = admit(scope, NOW + timedelta(seconds=30), limit=100, interval=1)
    assert limited.exhausted and limited.request_limit == 2
    with Session(get_engine()) as db:
        assert db.get(SourceRequestBudget, scope).minimum_interval_seconds == 10


def test_clock_regression_fails_closed(scope):
    assert admit(scope, NOW).admitted
    with pytest.raises(module.RequestBudgetUnavailable):
        admit(scope, NOW - timedelta(days=1))
    with Session(get_engine()) as db:
        assert db.get(SourceRequestBudget, scope).requests_reserved == 1


def test_database_failure_never_falls_back_or_leaks_details():
    def fail():
        raise RuntimeError("private-database-connection")

    with pytest.raises(module.RequestBudgetUnavailable) as error:
        module.consume_request(scope="test", daily_limit=1, minimum_interval_seconds=1,
                               session_factory=fail)
    assert "private-database" not in str(error.value)


def test_pacing_sleep_occurs_after_session_closes(monkeypatch):
    events = []
    decisions = iter([
        module.Admission(False, False, 1.0, 0, 1, "2026-09-08"),
        module.Admission(True, False, 0.0, 1, 1, "2026-09-08"),
    ])

    class FakeSession:
        def __enter__(self):
            events.append("open")
            return self

        def __exit__(self, *args):
            events.append("close")

        def execute(self, statement):
            pass

        def commit(self):
            events.append("commit")

    monkeypatch.setattr(module, "reserve_request", lambda *args, **kwargs: next(decisions))
    module.consume_request(scope="test", daily_limit=1, minimum_interval_seconds=1,
                           session_factory=FakeSession, sleep=lambda _: events.append("sleep"))
    assert events == ["open", "commit", "close", "sleep", "open", "commit", "close"]
