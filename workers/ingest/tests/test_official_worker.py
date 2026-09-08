import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from billcommons_ingest.official_worker import (
    ObservationDeadlineExceeded,
    TLS_REPAIR_CYCLE_LIMIT,
    _run_tls_repair_cycle,
    _tls_repair_enabled_from_env,
    _transaction_deadline,
    run_cycle,
    seed_ca_targets,
)
from billcommons_schema.models import Jurisdiction, OfficialSourceTarget


def test_seed_requires_existing_jurisdiction(db_session):
    with pytest.raises(ValueError, match="jurisdiction must exist"):
        seed_ca_targets(db_session)


def test_registration_is_disabled_idempotent_and_preserves_schedule(db_session):
    db_session.add(Jurisdiction(abbreviation="CA", name="California", classification="state"))
    db_session.flush()
    assert seed_ca_targets(db_session) == 7
    targets = db_session.scalars(select(OfficialSourceTarget)).all()
    assert len(targets) == 7
    assert all(not target.enabled for target in targets)
    next_check = datetime.now(timezone.utc) + timedelta(days=2)
    targets[0].next_check_at = next_check
    targets[0].consecutive_failures = 3
    assert seed_ca_targets(db_session, enable=True) == 7
    assert len(db_session.scalars(select(OfficialSourceTarget)).all()) == 7
    assert all(target.enabled for target in targets)
    assert targets[0].next_check_at == next_check
    assert targets[0].consecutive_failures == 3


def test_registration_refuses_changed_reviewed_scope(db_session):
    db_session.add(Jurisdiction(abbreviation="CA", name="California", classification="state"))
    db_session.flush()
    seed_ca_targets(db_session)
    target = db_session.scalars(select(OfficialSourceTarget)).first()
    target.scope = {"day": "Mon", "sessions": ["20992000"]}
    db_session.flush()
    with pytest.raises(ValueError, match="differs from reviewed scope"):
        seed_ca_targets(db_session, enable=True)


def test_registration_preserves_valid_observation_continuation(db_session):
    from billcommons_ingest.official_ca_actions import ADAPTER_VERSION

    db_session.add(Jurisdiction(abbreviation="CA", name="California", classification="state"))
    db_session.flush()
    seed_ca_targets(db_session)
    target = db_session.scalars(select(OfficialSourceTarget)).first()
    cursor = {"observation_id": str(uuid4()), "raw_sha256": "a" * 64,
              "next_bill_index": 500, "adapter_version": ADAPTER_VERSION}
    target.scope = {**target.scope, "continuation": cursor}
    db_session.flush()
    assert seed_ca_targets(db_session, enable=True) == 7
    assert target.scope["continuation"] == cursor
    assert target.enabled is True
    target.scope = {**target.scope, "continuation": None}
    db_session.flush()
    with pytest.raises(ValueError, match="differs from reviewed scope"):
        seed_ca_targets(db_session, enable=True)


class SessionProbe:
    def __init__(self, events):
        self.events = events

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


def result():
    return SimpleNamespace(target_id="target", status="succeeded", record_count=2,
                           reconciliation_count=1)


def test_cycle_commits_before_emitting_success_and_stops_at_empty_queue():
    events = []
    pending = iter([result(), None])
    processed = run_cycle(stop=threading.Event(), max_observations=7,
        session_factory=lambda: SessionProbe(events), observer=lambda db: next(pending),
        emit=lambda record: events.append(record["event"]))
    assert processed == 1
    assert events == ["commit", "close", "official_observation", "commit", "close"]


def test_failed_transaction_rolls_back_and_emits_no_success():
    events = []
    def broken(db):
        raise RuntimeError("private diagnostic")
    with pytest.raises(RuntimeError):
        run_cycle(stop=threading.Event(), max_observations=7,
            session_factory=lambda: SessionProbe(events), observer=broken,
            emit=lambda record: events.append("unexpected"))
    assert events == ["rollback", "close"]


def test_stop_request_finishes_current_observation_and_prevents_next_claim():
    stop = threading.Event()
    events = []
    def observe(db):
        stop.set()
        return result()
    assert run_cycle(stop=stop, max_observations=7,
        session_factory=lambda: SessionProbe(events), observer=observe,
        emit=lambda record: events.append("reported")) == 1
    assert events == ["commit", "close", "reported"]


def test_cycle_hard_cap_prevents_unbounded_claims():
    events = []
    assert run_cycle(stop=threading.Event(), max_observations=2,
        session_factory=lambda: SessionProbe(events), observer=lambda db: result(),
        emit=lambda record: None) == 2
    assert events == ["commit", "close", "commit", "close"]


def test_total_deadline_escapes_adapter_exception_handler():
    swallowed = []
    with pytest.raises(ObservationDeadlineExceeded):
        with _transaction_deadline(0.02):
            try:
                time.sleep(1)
            except Exception:
                swallowed.append(True)
    assert swallowed == []


def test_deadline_rolls_back_without_emitting_success():
    events = []
    def expired(db):
        raise ObservationDeadlineExceeded()
    with pytest.raises(ObservationDeadlineExceeded):
        run_cycle(stop=threading.Event(), max_observations=1,
            session_factory=lambda: SessionProbe(events), observer=expired,
            emit=lambda record: events.append("unexpected success"))
    assert events == ["rollback", "close"]


def test_tls_repair_is_disabled_by_default_and_requires_exact_env_opt_in(monkeypatch):
    monkeypatch.delenv("OFFICIAL_TLS_REPAIR_ENABLED", raising=False)
    assert _tls_repair_enabled_from_env() is False
    monkeypatch.setenv("OFFICIAL_TLS_REPAIR_ENABLED", "true")
    assert _tls_repair_enabled_from_env() is False
    monkeypatch.setenv("OFFICIAL_TLS_REPAIR_ENABLED", "1")
    assert _tls_repair_enabled_from_env() is True


def test_cycle_default_never_invokes_tls_repair_runner():
    calls = []
    events = []
    assert run_cycle(
        stop=threading.Event(),
        max_observations=1,
        session_factory=lambda: SessionProbe(events),
        observer=lambda _db: None,
        tls_repair_runner=lambda **_kwargs: calls.append("called"),
        emit=lambda record: events.append(record["event"]),
    ) == 0
    assert calls == []
    assert events == ["commit", "close"]


def test_enabled_cycle_runs_bounded_tls_repair_with_reused_dependencies():
    events = []
    calls = []
    fetcher = object()
    rawstore = object()

    def run_repair(**kwargs):
        calls.append(kwargs)
        return {"planned": 2, "succeeded": 1, "failed": 0, "skipped": 1, "expired": 0}

    assert run_cycle(
        stop=threading.Event(),
        max_observations=1,
        session_factory=lambda: SessionProbe(events),
        observer=lambda _db: None,
        tls_repair_enabled=True,
        tls_repair_fetcher=fetcher,
        tls_repair_rawstore=rawstore,
        tls_repair_runner=run_repair,
        emit=lambda record: events.append(record),
    ) == 0
    assert len(calls) == 1
    assert calls[0]["limit"] == TLS_REPAIR_CYCLE_LIMIT
    assert calls[0]["fetcher"] is fetcher
    assert calls[0]["rawstore"] is rawstore
    assert events[-1] == {
        "event": "official_tls_repair_cycle",
        "planned": 2,
        "succeeded": 1,
        "failed": 0,
        "skipped": 1,
        "expired": 0,
    }


def test_tls_repair_failure_isolated_after_observation_commit():
    events = []

    def broken_repair(**_kwargs):
        raise RuntimeError("upstream diagnostic must not be emitted")

    assert run_cycle(
        stop=threading.Event(),
        max_observations=1,
        session_factory=lambda: SessionProbe(events),
        observer=lambda _db: result(),
        tls_repair_enabled=True,
        tls_repair_fetcher=object(),
        tls_repair_rawstore=object(),
        tls_repair_runner=broken_repair,
        emit=events.append,
    ) == 1
    emitted = [record for record in events if isinstance(record, dict)]
    assert emitted == [
        {"event": "official_observation", "target_id": "target", "status": "succeeded",
         "record_count": 2, "reconciliation_count": 1},
        {"event": "official_tls_repair_failed", "error_class": "RuntimeError"},
    ]
    assert events.count("commit") == 1


def test_tls_repair_planning_commits_before_bounded_execution():
    events = []
    calls = []

    def seed(db, *, limit):
        calls.append(("seed", limit))
        return 2

    def execute(*, session_factory, fetcher, rawstore, limit):
        calls.append(("execute", limit, fetcher, rawstore))
        return SimpleNamespace(succeeded=1, failed=0, skipped=1, expired=0)

    counts = _run_tls_repair_cycle(
        session_factory=lambda: SessionProbe(events),
        fetcher="shared-fetcher",
        rawstore="shared-rawstore",
        limit=TLS_REPAIR_CYCLE_LIMIT,
        seed_candidates=seed,
        run_due_repairs=execute,
    )
    assert calls == [
        ("seed", TLS_REPAIR_CYCLE_LIMIT),
        ("execute", TLS_REPAIR_CYCLE_LIMIT, "shared-fetcher", "shared-rawstore"),
    ]
    assert events == ["commit", "close"]
    assert counts == {"planned": 2, "succeeded": 1, "failed": 0, "skipped": 1, "expired": 0}
