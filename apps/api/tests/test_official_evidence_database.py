"""Real PostgreSQL and HTTP evidence retrieval; conftest requires disposable DB."""
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from billcommons_schema.models import (
    Jurisdiction, OfficialRawBlob, OfficialSourceTarget,
    OfficialSourceObservation, OfficialReconciliationRun,
)
from billcommons_shared.db import get_session


@pytest.fixture()
def retained_evidence():
    token = uuid.uuid4().hex
    data = ("public official fixture " + token).encode()
    digest = hashlib.sha256(data).hexdigest()
    now = datetime.now(timezone.utc)
    with get_session() as db:
        jurisdiction = db.scalar(select(Jurisdiction).where(Jurisdiction.abbreviation == "CA"))
        created_jurisdiction = jurisdiction is None
        if jurisdiction is None:
            jurisdiction = Jurisdiction(abbreviation="CA", name="California", classification="state")
            db.add(jurisdiction)
            db.flush()
        jurisdiction_id = jurisdiction.id
        db.add(OfficialRawBlob(sha256=digest, data=data, content_type="text/html"))
        target = OfficialSourceTarget(jurisdiction_id=jurisdiction_id,
            adapter_name="fixture", source_url="https://example.invalid/" + token,
            scope={"fixture": token})
        db.add(target)
        db.flush()
        target_id = target.id
        observation = OfficialSourceObservation(target_id=target.id, adapter_name="fixture",
            adapter_version="1", source_url=target.source_url, scope=target.scope,
            retrieved_at=now, http_status=200, raw_sha256=digest, status="succeeded",
            record_count=3)
        db.add(observation)
        db.flush()
        observation_id = observation.id
        for number in range(3):
            db.add(OfficialReconciliationRun(observation_id=observation_id,
                official_bill_id=str(number), local_snapshot_at=now,
                comparator_version="fixture/1", status="completed", summary={"fixture": True},
                local_snapshot_sha256=digest, diff_sha256=digest))
        db.commit()
    try:
        yield observation_id, digest, data
    finally:
        with get_session() as db:
            db.execute(delete(OfficialReconciliationRun).where(
                OfficialReconciliationRun.observation_id == observation_id))
            db.execute(delete(OfficialSourceObservation).where(
                OfficialSourceObservation.id == observation_id))
            db.execute(delete(OfficialSourceTarget).where(OfficialSourceTarget.id == target_id))
            db.execute(delete(OfficialRawBlob).where(OfficialRawBlob.sha256 == digest))
            if created_jurisdiction:
                db.execute(delete(Jurisdiction).where(Jurisdiction.id == jurisdiction_id))
            db.commit()


def test_real_evidence_queries_paginate_and_serve_exact_retained_bytes(client, retained_evidence):
    observation_id, digest, data = retained_evidence
    observations = client.get("/api/v1/official-evidence/observations",
                              params={"jurisdiction": "CA"})
    assert observations.status_code == 200
    observation = next(item for item in observations.json()["items"]
                       if item["observation_id"] == str(observation_id))
    assert observation["raw_sha256"] == digest
    assert "data" not in observation
    seen = set()
    offset = 0
    for _ in range(3):
        response = client.get("/api/v1/official-evidence/reconciliations",
            params={"observation_id": str(observation_id), "limit": 1, "offset": offset})
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) == 1
        seen.add(page["items"][0]["reconciliation_id"])
        offset = page["next_offset"]
    assert len(seen) == 3
    assert page["has_more"] is False
    assert offset is None
    blob = client.get("/api/v1/official-evidence/blobs/" + digest)
    assert blob.status_code == 200
    assert blob.content == data
    assert blob.headers["content-type"] == "application/octet-stream"
    assert blob.headers["x-content-type-options"] == "nosniff"


def test_overview_covers_missing_states_and_never_promotes_discovery(client, retained_evidence):
    observation_id, digest, _ = retained_evidence
    response = client.get("/api/v1/official-evidence/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["jurisdiction_count"] == 51
    states = {item["jurisdiction"]: item for item in body["items"]}
    assert len(states) == 51
    assert states["WY"]["targets"] == []
    assert all(item["official_freshness"] == "unverified" for item in states.values())
    target = next(item for item in states["CA"]["targets"]
                  if item["observation_id"] == str(observation_id))
    assert target["raw_sha256"] == digest
    assert target["state"] == "disabled"
