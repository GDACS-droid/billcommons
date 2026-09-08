"""Replay archived evidence after current corpus changes, on disposable PG."""
import hashlib

import pytest
from sqlalchemy import select

from billcommons_ingest import official_observer as observer
from billcommons_ingest.official_replay import EvidenceReplayError, replay_reconciliation
from billcommons_schema.models import BillAction, OfficialRawBlob, OfficialReconciliationRun
from tests.test_official_observer import (
    FL_FIXTURE, FL_SOURCE_URL, NOW, _archive, _captured, _fl_captured, _fl_chamber, _fl_local_bill, _fl_target, _local_bill, _target,
)


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


def test_legacy_comparison_replays_under_its_recorded_version(db_session, completed_run):
    import json
    from billcommons_ingest import official_ca_actions as ca
    from billcommons_schema.models import OfficialSourceObservation
    from billcommons_shared.reconciliation import reconcile_events
    run, _ = completed_run
    obs = db_session.get(OfficialSourceObservation, run.observation_id)
    batch = ca.parse_ca_official_actions_zip(bytes(db_session.get(OfficialRawBlob, obs.raw_sha256).data),
        source_url=obs.source_url, retrieved_at=obs.retrieved_at)
    official = {'events': observer._official_events(batch.events_by_official_bill_id[run.official_bill_id],
        ca.map_official_bill_id(run.official_bill_id))}
    local = json.loads(db_session.get(OfficialRawBlob, run.local_snapshot_sha256).data)
    legacy = reconcile_events(official, local)
    run.comparator_version = 'reconcile-events/1'
    run.summary = legacy['summary']
    run.diff_sha256 = observer.store_official_raw_blob(db_session, observer._canonical_json_bytes(legacy), 'application/json')
    db_session.flush()
    assert replay_reconciliation(db_session, run.id)['status'] == 'reproduced'
    # A version relabel must not certify a differently shaped retained diff.
    run.comparator_version = observer.COMPARATOR_VERSION
    db_session.flush()
    with pytest.raises(EvidenceReplayError, match='does not reproduce'):
        replay_reconciliation(db_session, run.id)



def test_florida_replay_uses_retained_page_and_local_snapshot_after_corpus_changes(db_session, unique_abbr, monkeypatch):
    jurisdiction, _ = _fl_target(db_session, unique_abbr)
    bill = _fl_local_bill(db_session, jurisdiction)
    from billcommons_ingest import official_fl_senate_actions as fl
    parsed = fl.parse_florida_senate_bill_history(FL_FIXTURE.read_bytes(), source_url=FL_SOURCE_URL)
    db_session.add(BillAction(
        bill_id=bill.id,
        organization_id=_fl_chamber(db_session, jurisdiction, parsed.actions[0].chamber).id,
        description=parsed.actions[0].description,
        action_date=parsed.actions[0].action_date,
        source_name='retained-import',
        upstream_id='old-local-id',
    ))
    monkeypatch.setattr(observer, '_capture_fl_senate_detail', lambda source_url: _fl_captured(FL_FIXTURE.read_bytes()))
    observer.observe_due_target(db_session, now=NOW)
    db_session.flush()
    run = db_session.scalar(select(OfficialReconciliationRun))
    assert run and run.status == 'completed'

    bill.title = 'Changed after stored Florida comparison'
    db_session.flush()
    result = replay_reconciliation(db_session, run.id)

    assert result['status'] == 'reproduced'
    assert result['comparator_version'] == 'fl-senate-action-content-multiset/1'
    assert result['diff_sha256'] == run.diff_sha256
    assert 'occurrence' in result['interpretation']
