"""Read-only reproduction of one retained CA or Florida reconciliation, without HTTP.

Run with an explicitly configured DATABASE_URL:
  python -m billcommons_ingest.official_replay RECONCILIATION_UUID
The result attests reproduction under the recorded supported parser/comparator
versions. It does not certify that the publisher's source was complete.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid

from sqlalchemy import text

from billcommons_ingest import official_ca_actions as ca
from billcommons_ingest import official_fl_senate_actions as fl
from billcommons_ingest import official_fl_senate_capture as fl_capture
from billcommons_ingest.official_observer import (
    ADAPTER_NAME, COMPARATOR_VERSION, MAX_BLOB_BYTES,
    _canonical_json_bytes, _fl_official_bill_id, _fl_official_events, _fl_regular_session_identifier, _official_events,
)
from billcommons_schema.models import OfficialRawBlob, OfficialReconciliationRun, OfficialSourceObservation
from billcommons_shared.reconciliation import reconcile_events
from billcommons_ingest.official_fl_senate_reconciliation import (
    COMPARATOR_VERSION as FL_COMPARATOR_VERSION,
    reconcile_fl_senate_action_content,
)
from billcommons_ingest.official_ca_reconciliation import reconcile_ca_action_content


class EvidenceReplayError(ValueError):
    """Recorded bytes or versions do not support the claimed reproduction."""


def _load_blob(db, digest: str | None) -> bytes:
    if digest is None:
        raise EvidenceReplayError('required evidence hash is absent')
    blob = db.get(OfficialRawBlob, digest)
    if blob is None:
        raise EvidenceReplayError('required evidence blob is absent')
    data = bytes(blob.data)
    if len(data) > MAX_BLOB_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise EvidenceReplayError('evidence hash or size validation failed')
    return data


def _replay_ca(
    run: OfficialReconciliationRun,
    observation: OfficialSourceObservation,
    raw: bytes,
    local_bytes: bytes,
) -> tuple[dict, str]:
    if (
        observation.adapter_version != ca.ADAPTER_VERSION
        or run.comparator_version not in {"reconcile-events/1", COMPARATOR_VERSION}
    ):
        raise EvidenceReplayError('unsupported recorded adapter/comparator version or outcome')
    batch = ca.parse_ca_official_actions_zip(raw, source_url=observation.source_url,
                                            retrieved_at=observation.retrieved_at)
    events = batch.events_by_official_bill_id.get(run.official_bill_id)
    if events is None:
        raise EvidenceReplayError('recorded bill is absent from retained archive')
    mapping = ca.map_official_bill_id(run.official_bill_id)
    official = {'events': _official_events(events, mapping)}
    if run.comparator_version == "reconcile-events/1":
        reproduced = reconcile_events(official, json.loads(local_bytes))
    else:
        reproduced = reconcile_ca_action_content(official, json.loads(local_bytes), scope={
            "jurisdiction": "CA", "session": mapping.session_identifier,
            "bill_id": mapping.official_bill_id,
        })
    interpretation = ('Exact historical replay; legacy CA identity differences do not establish missing actions.'
                      if run.comparator_version == 'reconcile-events/1' else
                      'Exact replay of retained content comparison; no occurrence or current-source freshness proof.')
    return reproduced, interpretation


def _replay_fl(
    run: OfficialReconciliationRun,
    observation: OfficialSourceObservation,
    raw: bytes,
    local_bytes: bytes,
) -> tuple[dict, str]:
    if observation.adapter_version != fl.ADAPTER_VERSION or run.comparator_version != FL_COMPARATOR_VERSION:
        raise EvidenceReplayError('unsupported recorded adapter/comparator version or outcome')
    try:
        source_scope = fl_capture.detail_scope(observation.source_url)
        parsed = fl.parse_florida_senate_bill_history(raw, source_url=source_scope.source_url)
        session_identifier = _fl_regular_session_identifier(parsed.session_year)
    except (TypeError, ValueError, fl.OfficialFloridaSenateActionsError) as exc:
        raise EvidenceReplayError('retained Florida source bytes do not satisfy the recorded parser contract') from exc
    if (
        parsed.source_url != source_scope.source_url
        or parsed.session_year != source_scope.source_session_year
        or parsed.bill_number != source_scope.source_bill_number
        or _fl_official_bill_id(parsed) != run.official_bill_id
    ):
        raise EvidenceReplayError('retained Florida source identity does not bind to the recorded run')
    official = {'events': _fl_official_events(parsed, session_identifier=session_identifier)}
    reproduced = reconcile_fl_senate_action_content(official, json.loads(local_bytes), scope={
        'jurisdiction': 'FL', 'session': session_identifier, 'bill_id': parsed.bill_identifier,
    })
    return reproduced, 'Exact replay of retained Florida content comparison; no occurrence, current-source freshness, or completeness proof.'


def replay_reconciliation(db, reconciliation_id: uuid.UUID) -> dict:
    """Use only immutable recorded inputs; never consult today's local actions."""
    run = db.get(OfficialReconciliationRun, reconciliation_id)
    if run is None or run.status != 'completed':
        raise EvidenceReplayError('a completed reconciliation is required')
    observation = db.get(OfficialSourceObservation, run.observation_id)
    if observation is None or observation.status != 'succeeded':
        raise EvidenceReplayError('unsupported recorded adapter/comparator version or outcome')
    raw = _load_blob(db, observation.raw_sha256)
    local_bytes = _load_blob(db, run.local_snapshot_sha256)
    stored_diff = _load_blob(db, run.diff_sha256)
    if observation.adapter_name == ADAPTER_NAME:
        reproduced, interpretation = _replay_ca(run, observation, raw, local_bytes)
    elif observation.adapter_name == fl_capture.ADAPTER_NAME:
        reproduced, interpretation = _replay_fl(run, observation, raw, local_bytes)
    else:
        raise EvidenceReplayError('unsupported recorded adapter/comparator version or outcome')
    reproduced_bytes = _canonical_json_bytes(reproduced)
    if reproduced_bytes != stored_diff or reproduced['summary'] != run.summary:
        raise EvidenceReplayError('recorded reconciliation does not reproduce')
    return {'status': 'reproduced', 'reconciliation_id': str(run.id),
            'observation_id': str(observation.id), 'source_url': observation.source_url,
            'adapter_version': observation.adapter_version,
            'comparator_version': run.comparator_version,
            'raw_sha256': observation.raw_sha256,
            'local_snapshot_sha256': run.local_snapshot_sha256,
            'diff_sha256': run.diff_sha256, 'summary': reproduced['summary'],
            'interpretation': interpretation}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reconciliation_id', type=uuid.UUID)
    args = parser.parse_args()
    if not os.environ.get('DATABASE_URL'):
        parser.error('explicit DATABASE_URL is required; implicit fallback is prohibited')
    from billcommons_shared.db import get_session
    try:
        with get_session() as db:
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            result = replay_reconciliation(db, args.reconciliation_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # DB errors can contain connection credentials or raw SQL/payloads.
        print(json.dumps({'status': 'failed', 'error_class': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
