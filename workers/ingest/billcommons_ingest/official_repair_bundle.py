"""Create a local, review-only regression bundle from one retained CA failure.

Run with an explicit database target and an explicit empty output directory:

  python -m billcommons_ingest.official_repair_bundle OBSERVATION_UUID --output-dir /tmp/ca-repair

The command reads durable evidence, writes only the named local directory, and
never fetches a source, changes a schedule, or writes application records.
It prepares review evidence; it does not author or promote a parser repair.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from sqlalchemy import text

from billcommons_ingest import official_ca_actions as ca
from billcommons_ingest.official_diagnostics import failure_diagnosis
from billcommons_ingest.official_observer import ADAPTER_NAME, MAX_BLOB_BYTES
from billcommons_ingest.official_parser_provenance import (
    ParserProvenanceError,
    is_sha256,
    parser_source_sha256,
)
from billcommons_ingest.official_repair_plan import plan_observation_repair
from billcommons_ingest.official_replay import EvidenceReplayError, _load_blob
from billcommons_schema.models import OfficialSourceObservation


BUNDLE_VERSION = "official-repair-bundle/1"
FIXTURE_NAME = "retained-ca-archive.zip"
MANIFEST_NAME = "manifest.json"
TEST_NAME = "test_repair_bundle_regression.py"
MAX_SAMPLE_BILLS = 20
MAX_SAMPLE_EVENTS_PER_BILL = 50
MAX_MANIFEST_BYTES = 64 * 1024
_CA_DELTA_URLS = frozenset(ca.ca_delta_url(day) for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))


class RepairBundleError(ValueError):
    """The requested review artifact is not safely materializable."""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _sample_digest(batch) -> tuple[int, str]:
    """Hash bounded normalized action content without emitting source rows."""

    bill_ids = sorted(batch.scoped_bill_ids)[:MAX_SAMPLE_BILLS]
    sample = []
    for bill_id in bill_ids:
        events = batch.events_by_official_bill_id.get(bill_id, ())
        sample.append({
            "bill_id": bill_id,
            "events": [
                {
                    "date": event.action_date.isoformat() if event.action_date is not None else None,
                    # Current parsed events do not expose a chamber field. Keep
                    # the key so a future exposed value becomes evidence rather
                    # than silently changing this artifact shape.
                    "chamber": getattr(event, "chamber", None),
                    "description": event.description,
                    "source_identity": event.occurrence_id,
                }
                for event in events[:MAX_SAMPLE_EVENTS_PER_BILL]
            ],
        })
    return len(sample), hashlib.sha256(_canonical_json_bytes(sample)).hexdigest()


def _candidate_replay(raw: bytes, *, source_url: str, retrieved_at: datetime) -> dict[str, object]:
    """Run only the loaded pure parser and retain bounded public evidence."""

    try:
        batch = ca.parse_ca_official_actions_zip(
            raw,
            source_url=source_url,
            retrieved_at=retrieved_at,
        )
    except (TypeError, ValueError, ca.OfficialCaActionsError) as exc:
        return {"result": "rejected", "failure": failure_diagnosis(exc, stage="parse")}
    sample_count, sample_sha256 = _sample_digest(batch)
    return {
        "result": "accepted",
        "scoped_bill_count": len(batch.scoped_bill_ids),
        "event_count": batch.event_count,
        "sample_count": sample_count,
        "sample_sha256": sample_sha256,
    }


def _recorded_before(plan: dict, observation: OfficialSourceObservation) -> dict[str, object]:
    """Keep recorded facts distinct from the current candidate replay."""

    failure = plan.get("recorded_failure")
    return {
        "recorded_not_rerun": True,
        "observation_id": str(observation.id),
        "status": observation.status,
        "adapter_version": observation.adapter_version,
        "superseded": plan["superseded"],
        "latest_observation_id": plan["latest_observation_id"],
        "failure": failure if isinstance(failure, dict) else {"status": "unavailable"},
        "parser_provenance": plan["recorded_parser_provenance"],
    }


def _is_eligible_parse_failure(plan: dict, observation: OfficialSourceObservation) -> bool:
    """Accept a current diagnosis or narrow historical parse-failure evidence."""

    recorded = plan.get("recorded_failure")
    if isinstance(recorded, dict):
        return recorded.get("stage") == "parse"
    # Before structured diagnoses, only an invalid retained archive marked by
    # the CA parser's stable exception class is sufficient. A failed capture
    # or replay is deliberately not reclassified from its raw bytes alone.
    return observation.status == "invalid" and observation.error_class == "OfficialCaActionsError"


def _validated_replay_input(observation: OfficialSourceObservation) -> tuple[str, datetime]:
    source_url = observation.source_url
    retrieved_at = observation.retrieved_at
    if source_url not in _CA_DELTA_URLS:
        raise RepairBundleError("retained observation does not have a validated CA source URL")
    if not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise RepairBundleError("retained observation does not have a valid retrieval timestamp")
    return source_url, retrieved_at.astimezone(timezone.utc)


def _manifest_for(plan: dict, observation: OfficialSourceObservation, raw: bytes) -> dict[str, object]:
    try:
        candidate_digest = parser_source_sha256(ca.parse_ca_official_actions_zip)
    except ParserProvenanceError as exc:
        raise RepairBundleError("candidate parser source is unavailable") from exc
    source_url, retrieved_at = _validated_replay_input(observation)
    replay = _candidate_replay(raw, source_url=source_url, retrieved_at=retrieved_at)
    return {
        "bundle_version": BUNDLE_VERSION,
        "fixture": {
            "filename": FIXTURE_NAME,
            "byte_count": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "recorded_before": _recorded_before(plan, observation),
        "candidate_parser": {"source_sha256": candidate_digest},
        "replay_input": {"source_url": source_url, "retrieved_at": retrieved_at.isoformat()},
        "candidate_replay": replay,
        "promotion_state": "requires_human_review",
        "execution_authorized": False,
        "interpretation": (
            "Retained local evidence for parser review only; this bundle does not "
            "propose parser code, retry a target, or authorize promotion."
        ),
    }


def _reject_symlink_path(path: Path) -> None:
    """Refuse symlinked output paths and existing symlink ancestors."""

    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            raise RepairBundleError("output path must not traverse a symlink")


def _output_path(output_dir: str | Path) -> Path:
    path = Path(output_dir)
    if not path.name or any(part == ".." for part in path.parts):
        raise RepairBundleError("output directory must not use traversal")
    path = path.absolute()
    _reject_symlink_path(path)
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise RepairBundleError("output directory parent must be an existing non-symlink directory")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise RepairBundleError("output path must be an absent or empty non-symlink directory")
        if any(path.iterdir()):
            raise RepairBundleError("refusing to overwrite a non-empty output directory")
    return path


def _generated_test() -> str:
    """Return fixed test code; values are read from the deterministic manifest."""

    return '''"""Generated retained-archive parser regression evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import stat

import pytest

from billcommons_shared import ca_official_actions as parser_module
from billcommons_ingest.official_diagnostics import failure_diagnosis


ROOT = Path(__file__).parent


def _bounded_read(path, maximum):
    assert not path.is_symlink()
    info = path.stat()
    assert stat.S_ISREG(info.st_mode)
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    assert len(data) <= maximum
    return data


MANIFEST = json.loads(_bounded_read(ROOT / "manifest.json", 65536).decode("utf-8"))


def _sample_digest(batch):
    sample = []
    for bill_id in sorted(batch.scoped_bill_ids)[:20]:
        sample.append({
            "bill_id": bill_id,
            "events": [
                {
                    "date": event.action_date.isoformat() if event.action_date is not None else None,
                    "chamber": getattr(event, "chamber", None),
                    "description": event.description,
                    "source_identity": event.occurrence_id,
                }
                for event in batch.events_by_official_bill_id.get(bill_id, ())[:50]
            ],
        })
    encoded = json.dumps(sample, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return len(sample), hashlib.sha256(encoded).hexdigest()


def test_retained_ca_parser_regression(monkeypatch):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("generated parser regression test must not use network")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    assert MANIFEST["bundle_version"] == "official-repair-bundle/1"
    assert MANIFEST["fixture"]["filename"] == "retained-ca-archive.zip"
    assert MANIFEST["promotion_state"] == "requires_human_review"
    assert MANIFEST["execution_authorized"] is False
    raw = _bounded_read(ROOT / "retained-ca-archive.zip", 8 * 1024 * 1024)
    assert len(raw) == MANIFEST["fixture"]["byte_count"]
    assert hashlib.sha256(raw).hexdigest() == MANIFEST["fixture"]["sha256"]
    assert hashlib.sha256(Path(parser_module.__file__).read_bytes()).hexdigest() == MANIFEST["candidate_parser"]["source_sha256"]
    replay = MANIFEST["candidate_replay"]
    if replay["result"] == "accepted":
        batch = parser_module.parse_ca_official_actions_zip(
            raw,
            source_url=MANIFEST["replay_input"]["source_url"],
            retrieved_at=datetime.fromisoformat(MANIFEST["replay_input"]["retrieved_at"]),
        )
        assert len(batch.scoped_bill_ids) == replay["scoped_bill_count"]
        assert batch.event_count == replay["event_count"]
        assert _sample_digest(batch) == (replay["sample_count"], replay["sample_sha256"])
    else:
        with pytest.raises((TypeError, ValueError, parser_module.OfficialCaActionsError)) as raised:
            parser_module.parse_ca_official_actions_zip(
                raw,
                source_url=MANIFEST["replay_input"]["source_url"],
                retrieved_at=datetime.fromisoformat(MANIFEST["replay_input"]["retrieved_at"]),
            )
        assert failure_diagnosis(raised.value, stage="parse") == replay["failure"]
'''


def _write_bundle(output_dir: Path, manifest: dict[str, object], raw: bytes) -> dict[str, object]:
    """Stage known files beside the destination, then atomically publish them."""

    stage = output_dir.parent / f".{output_dir.name}.repair-bundle-{uuid.uuid4().hex}"
    created_stage = False
    try:
        stage.mkdir(mode=0o700)
        created_stage = True
        fixture = stage / FIXTURE_NAME
        fixture.write_bytes(raw)
        encoded_manifest = _canonical_json_bytes(manifest) + b"\n"
        if len(encoded_manifest) > MAX_MANIFEST_BYTES:
            raise RepairBundleError("bundle manifest exceeds the local size bound")
        (stage / MANIFEST_NAME).write_bytes(encoded_manifest)
        (stage / TEST_NAME).write_text(_generated_test(), encoding="utf-8")
        validated = validate_repair_bundle(stage)
        if output_dir.exists():
            # It was preflighted as empty. Recheck immediately before replacing
            # so a concurrent writer cannot have its content discarded.
            if output_dir.is_symlink() or not output_dir.is_dir() or any(output_dir.iterdir()):
                raise RepairBundleError("refusing to overwrite a changed output directory")
            # POSIX rename replaces an empty directory atomically. Keeping it
            # present also preserves it if publication fails.
        os.replace(stage, output_dir)
        return validated
    except Exception:
        if created_stage and stage.exists() and stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise


def _read_manifest(bundle_dir: Path) -> dict[str, object]:
    manifest_path = bundle_dir / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RepairBundleError("bundle manifest is absent or not a regular file")
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise RepairBundleError("bundle manifest exceeds the local size bound")
        with manifest_path.open("rb") as stream:
            encoded = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise RepairBundleError("bundle manifest exceeds the local size bound")
        manifest = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairBundleError("bundle manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("bundle_version") != BUNDLE_VERSION:
        raise RepairBundleError("bundle manifest version is unsupported")
    return manifest


def validate_repair_bundle(bundle_dir: str | Path) -> dict[str, object]:
    """Verify local fixture and loaded-parser evidence without database access."""

    root = Path(bundle_dir).absolute()
    _reject_symlink_path(root)
    if root.is_symlink() or not root.is_dir():
        raise RepairBundleError("bundle directory is absent or unsafe")
    manifest = _read_manifest(root)
    test_path = root / TEST_NAME
    expected_test = _generated_test().encode("utf-8")
    if test_path.is_symlink() or not test_path.is_file():
        raise RepairBundleError("bundle regression test is absent or unsafe")
    try:
        with test_path.open("rb") as stream:
            actual_test = stream.read(len(expected_test) + 1)
    except OSError as exc:
        raise RepairBundleError("bundle regression test is unreadable") from exc
    if actual_test != expected_test:
        raise RepairBundleError("bundle regression test does not match the fixed generator")
    if manifest.get("promotion_state") != "requires_human_review" or manifest.get("execution_authorized") is not False:
        raise RepairBundleError("bundle cannot authorize execution or promotion")
    fixture = manifest.get("fixture")
    candidate = manifest.get("candidate_parser")
    if not isinstance(fixture, dict) or fixture.get("filename") != FIXTURE_NAME:
        raise RepairBundleError("bundle fixture metadata is unsupported")
    if not isinstance(candidate, dict) or not is_sha256(candidate.get("source_sha256")):
        raise RepairBundleError("candidate parser source evidence is invalid")
    fixture_path = root / FIXTURE_NAME
    if fixture_path.is_symlink() or not fixture_path.is_file():
        raise RepairBundleError("bundle fixture is absent or not a regular file")
    try:
        if fixture_path.stat().st_size > MAX_BLOB_BYTES:
            raise RepairBundleError("bundle fixture exceeds the local size bound")
        with fixture_path.open("rb") as stream:
            raw = stream.read(MAX_BLOB_BYTES + 1)
    except OSError as exc:
        raise RepairBundleError("bundle fixture is unreadable") from exc
    if len(raw) > MAX_BLOB_BYTES or fixture.get("byte_count") != len(raw) or fixture.get("sha256") != hashlib.sha256(raw).hexdigest():
        raise RepairBundleError("bundle fixture evidence does not verify")
    try:
        current_digest = parser_source_sha256(ca.parse_ca_official_actions_zip)
    except ParserProvenanceError as exc:
        raise RepairBundleError("candidate parser source is unavailable") from exc
    if current_digest != candidate["source_sha256"]:
        raise RepairBundleError("candidate parser source evidence does not verify")
    replay_input = manifest.get("replay_input")
    if not isinstance(replay_input, dict) or replay_input.get("source_url") not in _CA_DELTA_URLS:
        raise RepairBundleError("bundle replay source is invalid")
    try:
        retrieved_at = datetime.fromisoformat(replay_input["retrieved_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RepairBundleError("bundle replay timestamp is invalid") from exc
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise RepairBundleError("bundle replay timestamp must include its timezone")
    reproduced = _candidate_replay(raw, source_url=replay_input["source_url"], retrieved_at=retrieved_at)
    if reproduced != manifest.get("candidate_replay"):
        raise RepairBundleError("candidate replay evidence does not reproduce")
    return manifest


def build_repair_bundle(db, observation_id: uuid.UUID, output_dir: str | Path) -> dict[str, object]:
    """Materialize one retained CA failure as a review-only local bundle."""

    destination = _output_path(output_dir)
    try:
        plan = plan_observation_repair(db, observation_id)
    except EvidenceReplayError as exc:
        raise RepairBundleError("retained CA fixture evidence does not verify") from exc
    observation = db.get(OfficialSourceObservation, observation_id)
    if observation is None or observation.adapter_name != ADAPTER_NAME:
        raise RepairBundleError("a retained CA failure is required")
    if not _is_eligible_parse_failure(plan, observation):
        raise RepairBundleError("a retained CA parse failure is required")
    if plan["recorded_parser_provenance"]["status"] == "invalid":
        raise RepairBundleError("recorded parser source evidence is invalid")
    if observation.raw_sha256 is None:
        raise RepairBundleError("a verified retained CA fixture is required")
    try:
        raw = _load_blob(db, observation.raw_sha256)
    except EvidenceReplayError as exc:
        raise RepairBundleError("retained CA fixture evidence does not verify") from exc
    if raw is None:
        raise RepairBundleError("a verified retained CA fixture is required")
    manifest = _manifest_for(plan, observation, raw)
    return _write_bundle(destination, manifest, raw)


create_repair_bundle = build_repair_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation_id", type=uuid.UUID)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("explicit DATABASE_URL is required; implicit fallback is prohibited")
    try:
        from billcommons_shared.db import get_session

        with get_session() as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            manifest = build_repair_bundle(db, args.observation_id, args.output_dir)
        print(json.dumps({
            "bundle_version": manifest["bundle_version"],
            "promotion_state": manifest["promotion_state"],
            "execution_authorized": manifest["execution_authorized"],
        }, sort_keys=True))
        return 0
    except Exception as exc:
        # Avoid writing URLs, retained bytes, database text, or arbitrary
        # parser messages to a terminal that may be captured operationally.
        print(json.dumps({"status": "failed", "error_class": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
