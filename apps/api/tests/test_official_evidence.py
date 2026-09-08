from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid

import pytest
from fastapi import HTTPException

from billcommons_api.routers import official_evidence


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.closed = False

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        if params and "sha256" in params:
            return _Result(self.rows)
        return _Result(self.rows)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_observations_is_bounded_public_metadata_and_read_only(monkeypatch):
    observation_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _DB(
        [
            {
                "id": observation_id,
                "adapter_name": "ca-legislature",
                "adapter_version": "1",
                "source_url": "https://legislature.example/acts",
                "scope": {"session": "2026"},
                "target_scope": {"jurisdiction": "CA"},
                "retrieved_at": now,
                "upstream_updated_at": None,
                "http_status": 200,
                "raw_sha256": "a" * 64,
                "status": "succeeded",
                "error_class": None,
                "record_count": 3,
                "created_at": now,
            }
        ]
    )
    monkeypatch.setattr(official_evidence, "get_session", lambda: db)

    result = official_evidence.observations("ca", 100)

    assert result["jurisdiction"] == "CA"
    assert result["items"][0]["observation_id"] == str(observation_id)
    assert result["items"][0]["target_scope"] == {"jurisdiction": "CA"}
    assert result["items"][0]["raw_sha256"] == "a" * 64
    assert "data" not in result["items"][0]
    assert any("READ ONLY" in query for query, _ in db.calls)
    assert any("statement_timeout" in query for query, _ in db.calls)
    assert db.calls[-1][1]["limit"] == 101
    assert db.closed


def test_reconciliations_returns_bounded_summary_metadata(monkeypatch):
    observation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    db = _DB(
        [
            {
                "id": run_id,
                "observation_id": observation_id,
                "bill_id": None,
                "official_bill_id": "AB-1",
                "local_snapshot_at": now,
                "comparator_version": "cmp-1",
                "status": "completed",
                "summary": {"changed": 1},
                "local_snapshot_sha256": "b" * 64,
                "diff_sha256": "c" * 64,
                "error_class": None,
                "completed_at": now,
            }
        ]
    )
    monkeypatch.setattr(official_evidence, "get_session", lambda: db)

    result = official_evidence.reconciliations(observation_id, 20)

    assert result["items"][0]["reconciliation_id"] == str(run_id)
    assert result["items"][0]["summary"] == {"changed": 1}
    assert result["items"][0]["local_snapshot_sha256"] == "b" * 64
    assert result["items"][0]["diff_sha256"] == "c" * 64
    assert result["items"][0]["diff_sha256"] == "c" * 64
    assert db.closed


def test_blob_verifies_hash_and_forces_download(monkeypatch):
    data = b"public official evidence"
    digest = hashlib.sha256(data).hexdigest()
    db = _DB([{"data": data, "content_type": "text/html"}])
    monkeypatch.setattr(official_evidence, "get_session", lambda: db)

    result = official_evidence.blob(digest)

    assert result.body == data
    assert result.media_type == "application/octet-stream"
    assert result.headers["etag"] == f'"{digest}"'
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in result.headers["content-disposition"]


def test_blob_corruption_fails_closed_and_jurisdiction_is_validated(monkeypatch):
    digest = hashlib.sha256(b"expected").hexdigest()
    db = _DB([{"data": b"corrupt", "content_type": "application/octet-stream"}])
    monkeypatch.setattr(official_evidence, "get_session", lambda: db)

    with pytest.raises(HTTPException) as corrupted:
        official_evidence.blob(digest)
    assert corrupted.value.status_code == 503

    with pytest.raises(HTTPException) as invalid:
        official_evidence.observations("XX", 20)
    assert invalid.value.status_code == 400


def test_cleanup_failure_is_redacted_and_always_closes(monkeypatch):
    class Broken(_DB):
        def rollback(self):
            raise RuntimeError("private connection diagnostic")
    db = Broken([])
    monkeypatch.setattr(official_evidence, "get_session", lambda: db)
    with pytest.raises(HTTPException) as failure:
        official_evidence.observations("CA", 20)
    assert db.closed
    assert failure.value.status_code == 503
    assert "private" not in failure.value.detail


def test_initialization_failure_still_closes_when_rollback_also_fails(monkeypatch):
    class Broken(_DB):
        def execute(self, *args, **kwargs):
            raise RuntimeError("private initialization diagnostic")
        def rollback(self):
            raise RuntimeError("private rollback diagnostic")
    db = Broken([])
    monkeypatch.setattr(official_evidence, "get_session", lambda: db)
    with pytest.raises(HTTPException) as failure:
        official_evidence.observations("CA", 20)
    assert db.closed
    assert failure.value.status_code == 503
    assert "private" not in failure.value.detail


def test_http_validation_rejects_bad_filters_before_database(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    def no_database():
        pytest.fail("invalid request opened a database")
    monkeypatch.setattr(official_evidence, "get_session", no_database)
    app = FastAPI()
    app.include_router(official_evidence.router)
    with TestClient(app) as client:
        for path in (
            "/official-evidence/observations?jurisdiction=CA&limit=101",
            "/official-evidence/observations?jurisdiction=CA&offset=-1",
            "/official-evidence/observations?jurisdiction=CA&offset=10001",
            "/official-evidence/reconciliations?observation_id=bad",
            "/official-evidence/blobs/not-a-hash",
        ):
            assert client.get(path).status_code == 422


def test_missing_blob_is_404_and_closes(monkeypatch):
    db = _DB([])
    monkeypatch.setattr(official_evidence, "get_session", lambda: db)
    with pytest.raises(HTTPException) as failure:
        official_evidence.blob("a" * 64)
    assert failure.value.status_code == 404
    assert db.closed


@pytest.mark.parametrize('enabled,status,age,observed,expected', [
    (False, 'failed', 0, True, 'disabled'),
    (True, None, 0, False, 'not_observed'),
    (True, 'failed', 0, True, 'failed'),
    (True, 'succeeded', 3601, True, 'observation_overdue'),
    (True, 'succeeded', 0, True, 'observed'),
])
def test_overview_distinguishes_observation_health(monkeypatch, enabled, status, age, observed, expected):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    row = dict(abbreviation='CA', id=uuid.uuid4(), adapter_name='fixture',
               source_url='https://example.invalid', scope={}, enabled=enabled,
               cadence_seconds=3600, next_check_at=now, consecutive_failures=0,
               observation_id=uuid.uuid4() if observed else None,
               retrieved_at=now - timedelta(seconds=age) if observed else None,
               status=status, error_class=None, raw_sha256=None, record_count=0)
    db = _DB([row])
    monkeypatch.setattr(official_evidence, 'get_session', lambda: db)
    body = official_evidence.overview()
    state = next(item for item in body['items'] if item['jurisdiction'] == 'CA')
    assert state['targets'][0]['state'] == expected
    assert state['official_freshness'] == 'unverified'
    assert db.closed


def test_overview_refuses_silent_inventory_truncation(monkeypatch):
    db = _DB([{}] * 1001)
    monkeypatch.setattr(official_evidence, 'get_session', lambda: db)
    with pytest.raises(HTTPException) as error:
        official_evidence.overview()
    assert error.value.status_code == 503
    assert db.closed


@pytest.mark.parametrize('path,scope', [
    ('/official-evidence/observations', {'jurisdiction': 'CA'}),
    ('/official-evidence/reconciliations', {'observation_id': str(uuid.uuid4())}),
    ('/corpus-updates', {'bill_id': str(uuid.uuid4())}),
    ('/corpus-updates/derived', {'bill_id': str(uuid.uuid4())}),
])
@pytest.mark.parametrize('offset,row_count,next_offset,limited', [
    (0, 3, 2, False),
    (9998, 3, 10000, False),
    (9999, 3, None, True),
    (10000, 3, None, True),
    (10000, 2, None, False),
    (10000, 0, None, False),
])
def test_http_pagination_always_has_fetchable_continuation_or_explicit_cap(
    monkeypatch, path, scope, offset, row_count, next_offset, limited,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from billcommons_api.routers import corpus_updates

    now = datetime.now(timezone.utc)
    row = dict.fromkeys((
        'upstream_updated_at', 'error_class', 'causal_corpus_update_evidence_id',
    ))
    row.update({key: uuid.uuid4() for key in ('id', 'observation_id', 'bill_id')})
    row.update({key: now for key in (
        'retrieved_at', 'created_at', 'completed_at', 'local_snapshot_at', 'derived_at',
    )})
    row.update({key: 'a' * 64 for key in (
        'raw_sha256', 'local_snapshot_sha256', 'diff_sha256', 'response_sha256',
        'before_snapshot_sha256', 'after_snapshot_sha256', 'derivation_input_sha256',
    )})
    row.update(adapter_name='fixture', adapter_version='1', source_url='https://example.invalid',
               scope={}, target_scope={}, http_status=200, status='succeeded', record_count=1,
               official_bill_id='AB-1', comparator_version='fixture/1', summary={},
               original_bill_upstream_id='fixture', source_name='fixture', request_scope={},
               processing_version='fixture/1', mutation_kind='updated', changed_components=[])
    db = _DB([row.copy() for _ in range(row_count)])
    monkeypatch.setattr(official_evidence, 'get_session', lambda: db)
    app = FastAPI()
    app.include_router(official_evidence.router)
    app.include_router(corpus_updates.router)
    with TestClient(app) as client:
        response = client.get(path, params={**scope, 'offset': offset, 'limit': 2})
        assert response.status_code == 200
        body = response.json()
        assert body['has_more'] is (next_offset is not None)
        assert body['next_offset'] == next_offset
        assert body['pagination_limited'] is limited
        assert len(body['items']) == min(row_count, 2)
        assert db.calls[-1][1]['offset'] == offset
        assert db.calls[-1][1]['limit'] == 3
        assert db.closed
        if next_offset is not None:
            assert client.get(path, params={**scope, 'offset': next_offset, 'limit': 2}).status_code == 200
