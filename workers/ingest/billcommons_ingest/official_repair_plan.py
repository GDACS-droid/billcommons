"""Plan, but never execute, remediation of one retained failed observation.

Run with an explicit DATABASE_URL:
  python -m billcommons_ingest.official_repair_plan OBSERVATION_UUID
No HTTP, schedule changes, corpus writes, or inferred source freshness. A parser
candidate must still pass source review and a bounded production canary.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid

from sqlalchemy import select, text

from billcommons_ingest import official_ca_actions as ca
from billcommons_ingest.official_discovery import ADAPTER_NAME as DISCOVERY_ADAPTER
from billcommons_ingest.official_diagnostics import failure_diagnosis
from billcommons_ingest.official_observer import ADAPTER_NAME
from billcommons_ingest.official_parser_provenance import (
    ParserProvenanceError,
    is_sha256,
    parser_source_sha256,
)
from billcommons_ingest.official_replay import EvidenceReplayError, _load_blob
from billcommons_schema.models import OfficialSourceObservation, OfficialSourceTarget

PLAN_VERSION = 'official-repair-plan/1'
# Codes are application-owned. Never echo arbitrary exception/source messages.
_DISCOVERY_ACTIONS = {
    'robots_disallowed': 'review_source_access_policy',
    'robots_unavailable': 'retry_as_scheduled',
    'robots_body_invalid': 'review_source_access_policy',
    'robots_slow_cadence_review_required': 'review_source_access_policy',
    'https_source_review_required': 'review_source_endpoint',
    'SsrfRejected': 'review_source_endpoint',
    'javascript_rendering_required': 'review_browser_adapter',
    'TimeoutFailure': 'retry_as_scheduled',
    'source_http_failure': 'review_source_endpoint',
    'source_not_html': 'review_source_schema',
    'source_body_invalid': 'review_capture_limit',
}


def _candidate_parser_sha256(parser: object) -> str:
    """Fingerprint the actual shared callable used by the local replay."""

    try:
        return parser_source_sha256(parser)
    except ParserProvenanceError as exc:
        raise EvidenceReplayError("candidate parser source is unavailable") from exc


def _recorded_parser_provenance(observation: OfficialSourceObservation) -> dict[str, str]:
    """Describe recorded parser evidence without manufacturing a baseline.

    Parser-source digests were added after some observations already existed.
    Their retained archives remain useful, but an absent value is explicitly
    historical absence rather than an invitation to substitute today's parser.
    """

    scope = observation.scope if isinstance(observation.scope, dict) else {}
    failure = scope.get('failure')
    if isinstance(failure, dict) and failure.get('parser_source_status') == 'unavailable':
        return {'status': 'unavailable', 'reason': 'parser_source_unavailable_at_observation'}
    if isinstance(failure, dict) and 'parser_source_sha256' in failure:
        digest = failure['parser_source_sha256']
        if is_sha256(digest):
            return {'status': 'recorded', 'parser_source_sha256': digest}
        return {'status': 'invalid', 'reason': 'recorded_parser_source_invalid'}
    return {'status': 'unavailable', 'reason': 'historical_parser_source_not_recorded'}


def plan_observation_repair(db, observation_id: uuid.UUID) -> dict:
    """Read immutable evidence and report present parser behavior separately.

    Superseded failures remain useful regression fixtures but are never emitted
    as retry candidates. Even a latest failure is not automatically executable.
    """
    observation = db.get(OfficialSourceObservation, observation_id)
    if observation is None or observation.status not in {'failed', 'invalid'}:
        raise EvidenceReplayError('a retained failed or invalid observation is required')
    target = db.get(OfficialSourceTarget, observation.target_id)
    if target is None:
        raise EvidenceReplayError('observation target is absent')
    latest_id = db.scalar(select(OfficialSourceObservation.id)
        .where(OfficialSourceObservation.target_id == observation.target_id)
        .order_by(OfficialSourceObservation.retrieved_at.desc(),
                  OfficialSourceObservation.created_at.desc(),
                  OfficialSourceObservation.id.desc()).limit(1))
    superseded = latest_id != observation.id
    plan = {
        'plan_version': PLAN_VERSION,
        'observation_id': str(observation.id), 'target_id': str(target.id),
        'latest_observation_id': str(latest_id) if latest_id else None,
        'superseded': superseded, 'target_enabled': target.enabled,
        'next_check_at': target.next_check_at.isoformat(),
        'recorded_status': observation.status,
        'recorded_adapter_version': observation.adapter_version,
        'raw_sha256': observation.raw_sha256,
        'raw_integrity': 'not_retained',
        'parser_replay': 'not_applicable',
        'recommended_action': 'inspect_adapter_failure',
        'execution_authorized': False,
        'interpretation': 'Proposal from retained evidence; no source request, corpus update, or freshness proof.',
    }
    recorded_failure = observation.scope.get('failure') if isinstance(observation.scope, dict) else None
    if isinstance(recorded_failure, dict) and recorded_failure.get('version') == 1:
        try:
            code = recorded_failure.get('code')
            if not isinstance(code, str):
                raise ValueError('recorded diagnosis requires a known code')
            safe_failure = failure_diagnosis(ca.OfficialCaActionsError(
                'Recorded source failure', code=code, details=recorded_failure.get('details')),
                stage=recorded_failure.get('stage'))
        except (TypeError, ValueError):
            pass  # Legacy/malformed metadata never becomes an instruction.
        else:
            plan['recorded_failure'] = safe_failure
    if observation.adapter_name == ADAPTER_NAME:
        plan['recorded_parser_provenance'] = _recorded_parser_provenance(observation)
    raw = _load_blob(db, observation.raw_sha256) if observation.raw_sha256 else None
    if raw is not None:
        plan['raw_integrity'] = 'verified'
    if observation.adapter_name == ADAPTER_NAME and raw is not None:
        plan['candidate_adapter_version'] = ca.ADAPTER_VERSION
        # Preserve this public field name while binding it to the actual
        # callable executed below, which currently lives in shared code.
        plan['candidate_parser_sha256'] = _candidate_parser_sha256(
            ca.parse_ca_official_actions_zip
        )
        try:
            batch = ca.parse_ca_official_actions_zip(raw, source_url=observation.source_url,
                                                    retrieved_at=observation.retrieved_at)
        except (ValueError, ca.OfficialCaActionsError) as exc:
            diagnosis = failure_diagnosis(exc, stage='parse')
            plan.update(parser_replay='rejected', current_failure=diagnosis,
                        recommended_action=diagnosis['recommended_action'])
        else:
            plan.update(parser_replay='accepted', scoped_bill_count=len(batch.scoped_bill_ids),
                        event_count=batch.event_count, recommended_action='review_parser_repair_canary')
    elif observation.adapter_name == ADAPTER_NAME:
        # The historical generic error alone cannot identify Sunday failures.
        plan['recommended_action'] = plan.get('recorded_failure', {}).get(
            'recommended_action', 'collect_next_scheduled_capture_diagnosis')
        if observation.http_status == 429 or (observation.http_status is not None and 500 <= observation.http_status <= 599):
            plan['recommended_action'] = 'retry_as_scheduled'
            plan['recorded_http_status'] = observation.http_status
        elif observation.http_status is not None and 400 <= observation.http_status <= 499:
            plan['recommended_action'] = 'review_source_endpoint'
            plan['recorded_http_status'] = observation.http_status
    elif observation.adapter_name == DISCOVERY_ADAPTER:
        plan['recommended_action'] = _DISCOVERY_ACTIONS.get(observation.error_class, 'inspect_adapter_failure')
    if (target.adapter_name, target.source_url) != (observation.adapter_name, observation.source_url):
        plan['recommended_action'] = 'review_changed_target'
    if superseded:
        plan['historical_recommendation'] = plan['recommended_action']
        plan['recommended_action'] = 'retain_regression_fixture'
    if not target.enabled and not superseded:
        plan['requires_target_enable_review'] = True
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('observation_id', type=uuid.UUID)
    args = parser.parse_args()
    if not os.environ.get('DATABASE_URL'):
        parser.error('explicit DATABASE_URL is required; implicit fallback is prohibited')
    from billcommons_shared.db import get_session
    try:
        with get_session() as db:
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            result = plan_observation_repair(db, args.observation_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_class': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
