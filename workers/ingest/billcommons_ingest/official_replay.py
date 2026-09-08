"""Read-only reproduction of one retained CA reconciliation, without HTTP.

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
from billcommons_ingest.official_observer import (
    ADAPTER_NAME, COMPARATOR_VERSION, MAX_BLOB_BYTES,
    _canonical_json_bytes, _official_events,
)
from billcommons_schema.models import OfficialRawBlob, OfficialReconciliationRun, OfficialSourceObservation
from billcommons_shared.reconciliation import reconcile_events


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


def replay_reconciliation(db, reconciliation_id: uuid.UUID) -> dict:
    """Use only immutable recorded inputs; never consult today's local actions."""
    run = db.get(OfficialReconciliationRun, reconciliation_id)
    if run is None or run.status != 'completed':
        raise EvidenceReplayError('a completed reconciliation is required')
    observation = db.get(OfficialSourceObservation, run.observation_id)
    if (observation is None or observation.status != 'succeeded'
            or observation.adapter_name != ADAPTER_NAME
            or observation.adapter_version != ca.ADAPTER_VERSION
            or run.comparator_version != COMPARATOR_VERSION):
        raise EvidenceReplayError('unsupported recorded adapter/comparator version or outcome')
    raw = _load_blob(db, observation.raw_sha256)
    local_bytes = _load_blob(db, run.local_snapshot_sha256)
    stored_diff = _load_blob(db, run.diff_sha256)
    batch = ca.parse_ca_official_actions_zip(raw, source_url=observation.source_url,
                                            retrieved_at=observation.retrieved_at)
    events = batch.events_by_official_bill_id.get(run.official_bill_id)
    if events is None:
        raise EvidenceReplayError('recorded bill is absent from retained archive')
    mapping = ca.map_official_bill_id(run.official_bill_id)
    official = {'events': _official_events(events, mapping)}
    reproduced = reconcile_events(official, json.loads(local_bytes))
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
            'interpretation': 'Exact replay of retained evidence; no current-source freshness claim.'}


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
