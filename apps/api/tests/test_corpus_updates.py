"""Real database/HTTP proof of bounded forward-mutation evidence access."""
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from billcommons_schema.models import Bill, CorpusUpdateEvidence, OfficialRawBlob
from billcommons_shared.db import get_session


@pytest.fixture()
def retained_updates():
    token = uuid.uuid4().hex
    raw = ('{ "retained fixture": "' + token + '" }').encode()
    digest = hashlib.sha256(raw).hexdigest()
    evidence_ids = []
    with get_session() as db:
        bill_id = db.scalar(select(Bill.id).limit(1))
        assert bill_id is not None
        db.add(OfficialRawBlob(sha256=digest, data=raw, content_type='application/json'))
        db.flush()
        for index in range(3):
            row = CorpusUpdateEvidence(bill_id=bill_id,
                original_bill_upstream_id='fixture/' + token,
                source_name='openstates_v3_api', source_url='https://example.invalid/bills',
                request_scope={'page': index + 1}, response_sha256=digest,
                before_snapshot_sha256=digest, after_snapshot_sha256=digest,
                processing_version='fixture/1', mutation_kind='updated',
                changed_components=['documents'], retrieved_at=datetime.now(timezone.utc))
            db.add(row)
            db.flush()
            evidence_ids.append(row.id)
        db.commit()
    try:
        yield bill_id, digest, raw
    finally:
        with get_session() as db:
            db.execute(delete(CorpusUpdateEvidence).where(CorpusUpdateEvidence.id.in_(evidence_ids)))
            db.execute(delete(OfficialRawBlob).where(OfficialRawBlob.sha256 == digest))
            db.commit()


def test_forward_evidence_pagination_and_exact_source_download(client, retained_updates):
    bill_id, digest, raw = retained_updates
    ids = set()
    offset = 0
    for _ in range(3):
        response = client.get('/api/v1/corpus-updates',
            params={'bill_id': str(bill_id), 'limit': 1, 'offset': offset})
        assert response.status_code == 200
        body = response.json()
        assert len(body['items']) == 1
        item = body['items'][0]
        ids.add(item['id'])
        assert item['source_name'] == 'openstates_v3_api'
        assert item['response_sha256'] == digest
        assert item['changed_components'] == ['documents']
        assert 'data' not in item
        offset = body['next_offset']
    assert len(ids) == 3
    assert offset is None
    assert body['has_more'] is False
    assert client.get(item['evidence_urls']['source_response']).content == raw


def test_forward_evidence_requires_scoped_bounded_queries(client):
    assert client.get('/api/v1/corpus-updates').status_code == 422
    bill = str(uuid.uuid4())
    assert client.get('/api/v1/corpus-updates', params={'bill_id': bill, 'limit': 101}).status_code == 422
    assert client.get('/api/v1/corpus-updates', params={'bill_id': bill, 'offset': 10001}).status_code == 422
    response = client.get('/api/v1/corpus-updates', params={'bill_id': bill})
    assert response.status_code == 200
    assert response.json()['items'] == []
    assert 'Absence of records does not prove' in response.json()['interpretation']
