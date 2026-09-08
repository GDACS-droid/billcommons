"""Replay archived evidence after current corpus changes, on disposable PG."""
import hashlib

import pytest
from sqlalchemy import select

from billcommons_ingest import official_observer as observer
from billcommons_ingest.official_replay import EvidenceReplayError, replay_reconciliation
from billcommons_schema.models import OfficialRawBlob, OfficialReconciliationRun
from tests.test_official_observer import NOW, _archive, _captured, _target, _local_bill


@pytest.fixture()
def completed_run(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _target(db_session, unique_abbr)
    bill = _local_bill(db_session, jurisdiction)
    raw = _archive()
    monkeypatch.setattr(observer, '_capture_ca_response', lambda day: _captured(raw))
    observer.observe_due_target(db_session, now=NOW)
    db_session.flush()
    run = db_session.scalar(select(OfficialReconciliationRun))
    assert run.status == 'completed'
    return run, bill


def test_replay_uses_retained_inputs_after_corpus_changes(db_session, completed_run, monkeypatch):
    run, bill = completed_run
    bill.title = 'Changed after the original comparison'
    db_session.flush()
    monkeypatch.setattr(observer, '_capture_ca_response', lambda day: pytest.fail('no network replay'))
    result = replay_reconciliation(db_session, run.id)
    assert result['status'] == 'reproduced'
    assert result['diff_sha256'] == run.diff_sha256
    assert result['summary'] == run.summary


def test_replay_rejects_changed_summary(db_session, completed_run):
    run, _ = completed_run
    run.summary = {'tampered': True}
    with pytest.raises(EvidenceReplayError, match='does not reproduce'):
        replay_reconciliation(db_session, run.id)


def test_replay_rejects_valid_hash_for_different_diff(db_session, completed_run):
    run, _ = completed_run
    data = b'{"altered":true}'
    digest = hashlib.sha256(data).hexdigest()
    db_session.add(OfficialRawBlob(sha256=digest, data=data, content_type='application/json'))
    db_session.flush()
    run.diff_sha256 = digest
    with pytest.raises(EvidenceReplayError, match='does not reproduce'):
        replay_reconciliation(db_session, run.id)


def test_replay_rejects_unsupported_comparator(db_session, completed_run):
    run, _ = completed_run
    run.comparator_version = 'future-comparator/2'
    with pytest.raises(EvidenceReplayError, match='unsupported'):
        replay_reconciliation(db_session, run.id)
