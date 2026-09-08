import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from billcommons_ingest.official_worker import run_cycle, seed_ca_targets
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
