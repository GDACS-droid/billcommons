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


@pytest.fixture()
def retained_derivations():
    from sqlalchemy import text

    token = uuid.uuid4().hex
    raw = ('{"local derivation fixture":"' + token + '"}').encode()
    digest = hashlib.sha256(raw).hexdigest()
    with get_session() as db:
        base_bill = db.scalar(select(Bill).limit(1))
        assert base_bill is not None
        bill = Bill(jurisdiction_id=base_bill.jurisdiction_id,
                    session_id=base_bill.session_id,
                    identifier='Evidence ' + token, identifier_norm='EVIDENCE' + token,
                    title='Isolated derived evidence fixture')
        db.add(bill)
        db.add(OfficialRawBlob(sha256=digest, data=raw, content_type='application/json'))
        db.flush()
        bill_id = bill.id
        for _ in range(3):
            evidence_id = uuid.uuid4()
            db.execute(text('''INSERT INTO derived_status_evidence
                (id, bill_id, derivation_input_sha256, before_snapshot_sha256,
                 after_snapshot_sha256, processing_version, changed_components, derived_at)
                VALUES (:id, :bill_id, :digest, :digest, :digest, 'fixture/1', '["status"]', now())'''),
                {'id': evidence_id, 'bill_id': bill_id, 'digest': digest})
        db.commit()
    try:
        yield bill_id, raw
    finally:
        with get_session() as db:
            db.execute(text('DELETE FROM derived_status_evidence WHERE bill_id=:bill_id'), {'bill_id': bill_id})
            db.execute(delete(Bill).where(Bill.id == bill_id))
            db.execute(delete(OfficialRawBlob).where(OfficialRawBlob.sha256 == digest))
            db.commit()


def test_derived_evidence_pagination_and_exact_local_input_download(client, retained_derivations):
    bill_id, raw = retained_derivations
    ids = set()
    offset = 0
    for _ in range(3):
        response = client.get('/api/v1/corpus-updates/derived',
                              params={'bill_id': str(bill_id), 'limit': 1, 'offset': offset})
        assert response.status_code == 200
        body = response.json()
        assert len(body['items']) == 1
        item = body['items'][0]
        ids.add(item['id'])
        assert item['causal_corpus_update_evidence_id'] is None
        assert item['changed_components'] == ['status']
        assert 'data' not in item
        assert client.get(item['evidence_urls']['local_derivation_inputs']).content == raw
        offset = body['next_offset']
    assert len(ids) == 3
    assert offset is None
    assert body['has_more'] is False
    assert 'not newly fetched official assertions' in body['interpretation']


def test_derived_evidence_requires_scoped_bounded_queries(client):
    path = '/api/v1/corpus-updates/derived'
    assert client.get(path).status_code == 422
    bill_id = str(uuid.uuid4())
    assert client.get(path, params={'bill_id': bill_id, 'limit': 101}).status_code == 422
    assert client.get(path, params={'bill_id': bill_id, 'offset': 10001}).status_code == 422
    response = client.get(path, params={'bill_id': bill_id})
    assert response.status_code == 200
    assert response.json()['items'] == []
