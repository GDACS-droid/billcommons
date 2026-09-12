"""Pure local checks for CA parser-repair review artifacts."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import zipfile

import pytest

from billcommons_ingest import official_observer as observer
from billcommons_ingest.official_parser_provenance import parser_source_sha256
from billcommons_ingest.official_repair_bundle import (
    FIXTURE_NAME,
    MANIFEST_NAME,
    TEST_NAME,
    RepairBundleError,
    build_repair_bundle,
    validate_repair_bundle,
)
from billcommons_ingest.official_repair_plan import plan_observation_repair
from billcommons_schema.models import OfficialRawBlob, OfficialSourceObservation, OfficialSourceTarget


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def _archive() -> bytes:
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter="\t", quotechar="`", lineterminator="\n")
    writer.writerow(["202520260AB12", "20252026", "0", "AB", "12"] + [""] * 14)
    bills = stream.getvalue().encode()
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter="\t", quotechar="`", lineterminator="\n")
    writer.writerow([
        "202520260AB12", "101", "2026-09-01 00:00:00", "Read first time.", "source",
        "2026-09-01 12:00:00", "1", "x", "x", "x", "x", "x", "x",
    ])
    history = stream.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("BILL_TBL.dat", bills)
        archive.writestr("BILL_HISTORY_TBL.dat", history)
    return output.getvalue()


class _FakeDb:
    def __init__(self, target, observation, blob, *, latest_id=None):
        self.target = target
        self.observation = observation
        self.blob = blob
        self.latest_id = latest_id if latest_id is not None else observation.id

    def get(self, model, key):
        if model is OfficialSourceObservation:
            return self.observation if key == self.observation.id else None
        if model is OfficialSourceTarget:
            return self.target if key == self.target.id else None
        if model is OfficialRawBlob:
            return self.blob if self.blob is not None and key == self.blob.sha256 else None
        raise AssertionError(f"unexpected model lookup: {model}")

    def scalar(self, _statement):
        return self.latest_id


def _failure_db(*, raw: bytes | None = None, parser_digest: str | None = None, adapter=observer.ADAPTER_NAME, latest_id=None):
    target_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    target = OfficialSourceTarget(
        id=target_id,
        adapter_name=adapter,
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_Mon.zip",
        scope={"day": "Mon", "sessions": ["20252026 regular", "special1"]},
        enabled=True,
        cadence_seconds=300,
        next_check_at=NOW + timedelta(minutes=5),
        consecutive_failures=1,
    )
    digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
    failure = {
        "version": 1,
        "stage": "parse",
        "code": "archive_crc_or_invalid_zip",
        "recommended_action": "review_source_schema",
    }
    if parser_digest is not None:
        failure["parser_source_sha256"] = parser_digest
    observation = OfficialSourceObservation(
        id=observation_id,
        target_id=target_id,
        adapter_name=adapter,
        adapter_version="ca-official-actions/1",
        source_url=target.source_url,
        retrieved_at=NOW,
        status="invalid",
        raw_sha256=digest,
        error_class="OfficialCaActionsError",
        scope={"failure": failure},
    )
    blob = OfficialRawBlob(sha256=digest, data=raw, content_type="application/zip") if raw is not None else None
    return _FakeDb(target, observation, blob, latest_id=latest_id), observation


def test_bundle_is_deterministic_and_generated_regression_runs_without_database_or_network(tmp_path):
    digest = parser_source_sha256(observer.ca_actions.parse_ca_official_actions_zip)
    db, observation = _failure_db(raw=_archive(), parser_digest=digest)
    output = tmp_path / "bundle"

    manifest = build_repair_bundle(db, observation.id, output)

    assert manifest["promotion_state"] == "requires_human_review"
    assert manifest["execution_authorized"] is False
    assert manifest["recorded_before"]["recorded_not_rerun"] is True
    assert manifest["recorded_before"]["parser_provenance"] == {
        "status": "recorded", "parser_source_sha256": digest,
    }
    assert manifest["candidate_replay"] == {
        "result": "accepted", "scoped_bill_count": 1, "event_count": 1,
        "sample_count": 1, "sample_sha256": manifest["candidate_replay"]["sample_sha256"],
    }
    assert sorted(path.name for path in output.iterdir()) == sorted([FIXTURE_NAME, MANIFEST_NAME, TEST_NAME])
    assert json.loads((output / MANIFEST_NAME).read_text()) == manifest
    assert validate_repair_bundle(output) == manifest

    repeat = tmp_path / "repeat"
    assert build_repair_bundle(db, observation.id, repeat) == manifest
    for filename in (FIXTURE_NAME, MANIFEST_NAME, TEST_NAME):
        assert (repeat / filename).read_bytes() == (output / filename).read_bytes()

    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env.pop("BILLCOMMONS_TEST_DATABASE_URL", None)
    env.pop("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", None)
    env["PYTHONPATH"] = os.pathsep.join((str(root / "packages/shared"), str(root / "workers/ingest")))
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--noconftest", "-c", "/dev/null", "-q", str(output / TEST_NAME)],
        cwd=output,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_tampered_blob_and_loaded_source_evidence_are_rejected(tmp_path, monkeypatch):
    raw = _archive()
    db, observation = _failure_db(raw=raw)
    db.blob.data = b"tampered retained bytes"
    with pytest.raises(RepairBundleError, match="fixture evidence"):
        build_repair_bundle(db, observation.id, tmp_path / "tampered-blob")

    db, observation = _failure_db(raw=raw, parser_digest="not-a-digest")
    with pytest.raises(RepairBundleError, match="recorded parser source evidence"):
        build_repair_bundle(db, observation.id, tmp_path / "tampered-recorded-source")

    db, observation = _failure_db(raw=raw)
    output = tmp_path / "bundle"
    build_repair_bundle(db, observation.id, output)
    monkeypatch.setattr(
        "billcommons_ingest.official_repair_bundle.ca.parse_ca_official_actions_zip",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(RepairBundleError, match="source evidence"):
        validate_repair_bundle(output)


def test_historical_provenance_absence_remains_usable_and_superseded_has_no_retry_semantics(tmp_path):
    db, observation = _failure_db(raw=_archive(), parser_digest=None, latest_id=uuid.uuid4())

    plan = plan_observation_repair(db, observation.id)
    assert plan["recorded_parser_provenance"] == {
        "status": "unavailable", "reason": "historical_parser_source_not_recorded",
    }
    assert plan["superseded"] is True
    assert plan["recommended_action"] == "retain_regression_fixture"
    assert plan["execution_authorized"] is False

    manifest = build_repair_bundle(db, observation.id, tmp_path / "historical")
    assert manifest["recorded_before"]["parser_provenance"]["status"] == "unavailable"
    assert manifest["recorded_before"]["superseded"] is True
    assert manifest["promotion_state"] == "requires_human_review"
    assert manifest["execution_authorized"] is False


def test_rejected_replay_keeps_a_safe_diagnosis_without_echoing_fixture_bytes(tmp_path):
    private_fixture = b"private retained payload that must not enter the manifest"
    db, observation = _failure_db(raw=private_fixture)

    manifest = build_repair_bundle(db, observation.id, tmp_path / "rejected")

    assert manifest["candidate_replay"]["result"] == "rejected"
    assert manifest["candidate_replay"]["failure"]["stage"] == "parse"
    assert b"private retained payload" not in (tmp_path / "rejected" / MANIFEST_NAME).read_bytes()


def test_forward_ca_parse_failure_scope_binds_the_loaded_shared_parser_source():
    target = OfficialSourceTarget(
        adapter_name=observer.ADAPTER_NAME,
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_Mon.zip",
        scope={"day": "Mon", "sessions": ["20252026 regular", "special1"]},
        enabled=True,
        cadence_seconds=300,
        next_check_at=NOW,
        consecutive_failures=0,
    )

    scope = observer._failure_scope(
        target,
        observer.ca_actions.OfficialCaActionsError("private parser detail"),
        stage="parse",
    )

    assert scope["failure"]["parser_source_sha256"] == parser_source_sha256(
        observer.ca_actions.parse_ca_official_actions_zip
    )


def test_bundle_refuses_unusable_input_traversal_and_overwrite(tmp_path):
    db, observation = _failure_db(raw=None)
    with pytest.raises(RepairBundleError, match="verified retained"):
        build_repair_bundle(db, observation.id, tmp_path / "missing")

    db, observation = _failure_db(raw=_archive(), adapter="other_adapter")
    with pytest.raises(RepairBundleError, match="retained CA failure"):
        build_repair_bundle(db, observation.id, tmp_path / "wrong-adapter")

    db, observation = _failure_db(raw=_archive())
    observation.scope["failure"]["stage"] = "capture"
    with pytest.raises(RepairBundleError, match="parse failure"):
        build_repair_bundle(db, observation.id, tmp_path / "capture-not-parse")

    db, observation = _failure_db(raw=_archive())
    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "keep").write_text("do not overwrite")
    with pytest.raises(RepairBundleError, match="non-empty"):
        build_repair_bundle(db, observation.id, populated)
    with pytest.raises(RepairBundleError, match="traversal"):
        build_repair_bundle(db, observation.id, tmp_path / "safe" / ".." / "escape")
