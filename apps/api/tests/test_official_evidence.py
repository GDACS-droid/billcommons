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
    assert result["items"][0]["raw_sha"] == "a" * 64
    assert "data" not in result["items"][0]
    assert any("READ ONLY" in query for query, _ in db.calls)
    assert any("statement_timeout" in query for query, _ in db.calls)
    assert db.calls[-1][1]["limit"] == 100
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
    assert result["items"][0]["local_hash"] == "b" * 64
    assert result["items"][0]["diff_hash"] == "c" * 64
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
