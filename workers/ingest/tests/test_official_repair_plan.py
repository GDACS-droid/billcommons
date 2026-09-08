"""Repair proposals are bounded evidence reads, never retries or corpus edits."""
import copy
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import func, select

from billcommons_ingest import official_ca_actions as ca
from billcommons_ingest import official_observer as observer
from billcommons_ingest.official_repair_plan import plan_observation_repair
from billcommons_ingest.official_replay import EvidenceReplayError
from billcommons_schema.models import OfficialRawBlob, OfficialSourceObservation, OfficialReconciliationRun
from tests.test_official_observer import NOW, _archive, _target


def _failed(db, abbr, *, raw=None, error='OfficialCaActionsError', adapter=observer.ADAPTER_NAME):
    _, target = _target(db, abbr, adapter_name=adapter)
    digest = observer.store_official_raw_blob(db, raw, 'application/zip') if raw is not None else None
    obs = OfficialSourceObservation(target_id=target.id, adapter_name=adapter,
        adapter_version=ca.ADAPTER_VERSION, source_url=target.source_url,
        retrieved_at=NOW, status='invalid' if raw is not None else 'failed',
        raw_sha256=digest, error_class=error, scope={})
    db.add(obs)
    db.flush()
    return target, obs


def test_resolved_parser_failure_proposes_review_without_writes(db_session, unique_abbr, monkeypatch):
    target, obs = _failed(db_session, unique_abbr, raw=_archive())
    before = (copy.deepcopy(target.scope), target.next_check_at, target.enabled, target.consecutive_failures)
    monkeypatch.setattr(ca, 'fetch_ca_official_actions_response', lambda *a, **k: pytest.fail('no HTTP'))
    db_session.commit()  # releases only the fixture SAVEPOINT, not outer transaction
    counts = [db_session.scalar(select(func.count()).select_from(cls))
              for cls in (OfficialRawBlob, OfficialSourceObservation, OfficialReconciliationRun)]
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['parser_replay'] == 'accepted'
    assert plan['event_count'] == plan['scoped_bill_count'] == 1
    assert plan['recommended_action'] == 'review_parser_repair_canary'
    assert plan['raw_integrity'] == 'verified'
    assert len(plan['candidate_parser_sha256']) == 64
    assert plan['execution_authorized'] is False
    assert (target.scope, target.next_check_at, target.enabled, target.consecutive_failures) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    assert counts == [db_session.scalar(select(func.count()).select_from(cls))
                      for cls in (OfficialRawBlob, OfficialSourceObservation, OfficialReconciliationRun)]


def test_corrupted_blob_cannot_generate_repair_proposal(db_session, unique_abbr):
    _, obs = _failed(db_session, unique_abbr, raw=_archive())
    db_session.get(OfficialRawBlob, obs.raw_sha256).data = b'changed'
    db_session.flush()
    with pytest.raises(EvidenceReplayError, match='hash or size'):
        plan_observation_repair(db_session, obs.id)


def test_invalid_archive_is_rejected_without_raw_payload_leak(db_session, unique_abbr):
    _, obs = _failed(db_session, unique_abbr, raw=b'private-source-payload')
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['parser_replay'] == 'rejected'
    assert 'private-source-payload' not in str(plan)
    assert plan['execution_authorized'] is False


def test_superseded_failure_is_fixture_not_retry(db_session, unique_abbr):
    _, obs = _failed(db_session, unique_abbr, raw=_archive())
    later = OfficialSourceObservation(target_id=obs.target_id, adapter_name=obs.adapter_name,
        adapter_version=obs.adapter_version, source_url=obs.source_url, retrieved_at=NOW + timedelta(seconds=1),
        status='succeeded', raw_sha256=obs.raw_sha256, record_count=1, scope={})
    db_session.add(later)
    db_session.flush()
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['superseded'] is True
    assert plan['latest_observation_id'] == str(later.id)
    assert plan['recommended_action'] == 'retain_regression_fixture'
    assert plan['historical_recommendation'] == 'review_parser_repair_canary'


def test_missing_capture_does_not_invent_http_failure(db_session, unique_abbr):
    target, obs = _failed(db_session, unique_abbr)
    target.enabled = False
    db_session.flush()
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['raw_integrity'] == 'not_retained'
    assert plan['recommended_action'] == 'collect_next_scheduled_capture_diagnosis'
    assert plan['requires_target_enable_review'] is True
    assert 'http_status' not in plan


@pytest.mark.parametrize('error,action', [
    ('robots_disallowed', 'review_source_access_policy'),
    ('SsrfRejected', 'review_source_endpoint'),
    ('TimeoutFailure', 'retry_as_scheduled'),
    ('javascript_rendering_required', 'review_browser_adapter'),
    ('sensitive raw exception detail', 'inspect_adapter_failure'),
])
def test_discovery_failures_have_bounded_nonexecuting_actions(db_session, unique_abbr, error, action):
    _, obs = _failed(db_session, unique_abbr, adapter='official_link_discovery', error=error)
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['recommended_action'] == action
    assert plan['execution_authorized'] is False
    assert 'sensitive raw exception detail' not in str(plan)


def test_unknown_observation_is_rejected(db_session):
    with pytest.raises(EvidenceReplayError, match='retained failed'):
        plan_observation_repair(db_session, uuid.uuid4())


@pytest.mark.parametrize('status,action', [(503, 'retry_as_scheduled'), (429, 'retry_as_scheduled'), (404, 'review_source_endpoint')])
def test_capture_http_status_guides_plan_without_inventing_raw(db_session, unique_abbr, status, action):
    _, obs = _failed(db_session, unique_abbr)
    obs.http_status = status
    db_session.flush()
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['recommended_action'] == action
    assert plan['recorded_http_status'] == status
    assert plan['raw_integrity'] == 'not_retained'


def test_changed_target_is_not_parser_retry_candidate(db_session, unique_abbr):
    target, obs = _failed(db_session, unique_abbr, raw=_archive())
    target.source_url = ca.ca_delta_url('Tue')
    db_session.flush()
    assert plan_observation_repair(db_session, obs.id)['recommended_action'] == 'review_changed_target'


def test_recorded_diagnosis_revalidated_never_trusted_as_instruction(db_session, unique_abbr):
    _, obs = _failed(db_session, unique_abbr)
    obs.scope = {'failure': {'version': 1, 'stage': 'capture',
        'code': 'response_size_limit_exceeded', 'details': {'observed': 9000000, 'limit': 8388608},
        'recommended_action': 'raise_limit_and_retry_immediately', 'message': 'private payload'}}
    db_session.flush()
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['recommended_action'] == 'review_capture_limit'
    assert 'private payload' not in str(plan)
    assert 'raise_limit_and_retry_immediately' not in str(plan)


def test_malformed_recorded_diagnosis_is_not_echoed(db_session, unique_abbr):
    _, obs = _failed(db_session, unique_abbr)
    obs.scope = {'failure': {'version': 1, 'stage': ['private payload'], 'code': 'response_size_limit_exceeded'}}
    db_session.flush()
    plan = plan_observation_repair(db_session, obs.id)
    assert plan['recommended_action'] == 'collect_next_scheduled_capture_diagnosis'
    assert 'recorded_failure' not in plan
