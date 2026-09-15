"""Evaluate authenticated CA proposals locally without promoting a repair."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from billcommons_shared import ca_official_actions as ca
from billcommons_ingest.official_repair_bundle import (
    FIXTURE_NAME, RepairBundleError,
    validate_repair_bundle,
)
from billcommons_ingest.official_repair_proposal import validate_repair_proposal
from billcommons_ingest.official_repair_sandbox import (
    MAX_FIXTURE_BYTES, MAX_OUTPUT_BYTES, MAX_SOURCE_BYTES, run_ca_parser,
)

EVALUATION_VERSION = "official-repair-evaluation/1"
_EVENT_KEYS = frozenset({"occurrence_id", "official_bill_id", "history_id", "action_date",
                         "description", "sequence", "updated_at", "source_url", "raw_fields"})


def _verified_bytes(path: Path, *, digest: str, maximum: int) -> bytes:
    path = path.absolute()
    if ".." in path.parts or len(path.parts) < 2:
        raise RepairBundleError("evaluation input path is invalid")
    try:
        # Pin each ancestor instead of checking a pathname and then reopening
        # it. A later rename or symlink replacement cannot redirect this read.
        directory = os.open(path.anchor, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in path.parts[1:-1]:
                next_directory = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
                                          | os.O_CLOEXEC, dir_fd=directory)
                os.close(directory)
                directory = next_directory
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                raise RepairBundleError("evaluation input is not a bounded single-link regular file")
            raw = stream.read(maximum + 1)
    except OSError as exc:
        raise RepairBundleError("evaluation input is unreadable") from exc
    if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != digest:
        raise RepairBundleError("evaluation input changed after validation")
    return raw


def _facts(batch) -> dict:
    # Deliberately built in the trusted parent. The child has no expected answer
    # and its serializer/reporting code is never relied upon as an attestation.
    return {"status": "parsed", "bills": [
        {"bill_id": bill_id, "events": [
            {"occurrence_id": event.occurrence_id,
             "official_bill_id": event.official_bill_id,
             "history_id": event.history_id,
             "action_date": event.action_date.isoformat() if event.action_date is not None else None,
             "description": event.description,
             "sequence": event.sequence,
             "updated_at": event.updated_at,
             "source_url": event.source_url,
             "raw_fields": dict(event.raw_fields)}
            for event in batch.events_by_official_bill_id[bill_id]]}
        for bill_id in batch.scoped_bill_ids]}


def _fact_evidence(payload: dict) -> dict:
    """Check the bounded portable schema, then hash all returned facts."""
    if (set(payload) != {"status", "bills"} or payload["status"] != "parsed"
            or not isinstance(payload["bills"], list) or len(payload["bills"]) > 100_000):
        raise ValueError("invalid parsed facts")
    bill_ids = set()
    event_count = 0
    for bill in payload["bills"]:
        if (not isinstance(bill, dict) or set(bill) != {"bill_id", "events"}
                or not isinstance(bill["bill_id"], str) or not bill["bill_id"]
                or bill["bill_id"] in bill_ids or not isinstance(bill["events"], list)):
            raise ValueError("invalid bill facts")
        bill_ids.add(bill["bill_id"])
        event_count += len(bill["events"])
        if event_count > 100_000:
            raise ValueError("too many event facts")
        for event in bill["events"]:
            if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
                raise ValueError("invalid event facts")
            for field in ("occurrence_id", "official_bill_id", "history_id", "description",
                          "updated_at", "source_url"):
                if not isinstance(event[field], str):
                    raise ValueError("invalid event text")
            if event["official_bill_id"] != bill["bill_id"]:
                raise ValueError("invalid event identity")
            if event["action_date"] is not None and not isinstance(event["action_date"], str):
                raise ValueError("invalid event date")
            if event["sequence"] is not None and type(event["sequence"]) is not int:
                raise ValueError("invalid event sequence")
            if (not isinstance(event["raw_fields"], dict)
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           for k, v in event["raw_fields"].items())):
                raise ValueError("invalid raw fields")
    digest = hashlib.sha256()
    byte_count = 0
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, allow_nan=False)
    for fragment in encoder.iterencode(payload):
        encoded = fragment.encode("utf-8")
        byte_count += len(encoded)
        if byte_count > MAX_OUTPUT_BYTES:
            raise ValueError("serialized facts exceed evaluator limit")
        digest.update(encoded)
    return {"bill_count": len(bill_ids), "event_count": event_count,
            "facts_sha256": digest.hexdigest()}


def evaluate_repair_proposal(proposal_dir: str | Path, *, expected_proposal_sha256: str) -> dict:
    """Compare one pinned proposal on its retained fixture; no promotion writes.

    Matching the current baseline is a regression observation, not proof that a
    historical failure is repaired. A differing result requires an independent
    expected-fact oracle and review. Authored regression code remains inert.
    """
    root = Path(proposal_dir).absolute()
    proposal = validate_repair_proposal(root, expected_proposal_sha256=expected_proposal_sha256)
    baseline = validate_repair_bundle(root / "baseline",
                                      expected_manifest_sha256=proposal["baseline_manifest_sha256"])
    sources = {label: _verified_bytes(root / filename,
                    digest=proposal["artifacts"][filename]["sha256"], maximum=MAX_SOURCE_BYTES)
               for label, filename in (("baseline", "baseline-parser.py.txt"),
                                       ("candidate", "candidate-parser.py.txt"))}
    fixture = _verified_bytes(root / "baseline" / FIXTURE_NAME,
                              digest=baseline["fixture"]["sha256"], maximum=MAX_FIXTURE_BYTES)
    replay = baseline["replay_input"]
    report = {
        "evaluation_version": EVALUATION_VERSION,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "proposal_sha256": expected_proposal_sha256,
        "baseline_manifest_sha256": proposal["baseline_manifest_sha256"],
        "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
        "scope": "one_retained_ca_archive",
        "runtime": {"python": sys.version, "system": os.uname().sysname,
                    "release": os.uname().release, "machine": os.uname().machine},
        "regression_tests": {"status": "not_run",
            "source_sha256": proposal["artifacts"]["candidate-regression.py.txt"]["sha256"]},
        "promotion_state": "requires_human_review",
        "promotion_authorized": False,
    }
    try:
        batch = ca.parse_ca_official_actions_zip(
            fixture, source_url=replay["source_url"],
            retrieved_at=datetime.fromisoformat(replay["retrieved_at"]))
    except (ca.OfficialCaActionsError, ValueError, TypeError):
        expected = None
    else:
        try:
            expected = _fact_evidence(_facts(batch))
        except (ValueError, TypeError):
            report.update({"comparison": "baseline_output_unrepresentable",
                           "trusted_baseline": {"status": "output_unrepresentable"},
                           "baseline": {"status": "not_run"},
                           "candidate": {"status": "not_run"}})
            return report
    report["trusted_baseline"] = expected or {"status": "rejected"}
    for label in ("baseline", "candidate"):
        result = run_ca_parser(sources[label], fixture, source_url=replay["source_url"],
                               retrieved_at=replay["retrieved_at"])
        entry = {"status": result.status, "source_sha256": result.source_sha256,
                 "fixture_sha256": result.fixture_sha256,
                 "bootstrap_sha256": result.bootstrap_sha256}
        if result.status == "returned":
            try:
                entry["facts"] = _fact_evidence(result.payload)
            except (ValueError, TypeError, RecursionError):
                entry["status"] = "invalid_output"
        report[label] = entry
        if result.status in {"cleanup_pending", "runner_busy"}:
            if result.cleanup is not None:
                entry["cleanup"] = result.cleanup
            if label == "baseline":
                report["candidate"] = {"status": "not_run"}
            report["comparison"] = result.status
            return report
    if expected is None:
        comparison = "requires_independent_oracle"
    elif report["baseline"].get("facts") != expected:
        comparison = "baseline_runtime_mismatch"
    elif report["candidate"]["status"] != "returned":
        comparison = "candidate_did_not_return_valid_facts"
    elif report["candidate"].get("facts") == expected:
        comparison = "same_as_current_baseline"
    else:
        comparison = "different_from_current_baseline"
    report["comparison"] = comparison
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal_dir", type=Path)
    parser.add_argument("--proposal-sha256", required=True)
    args = parser.parse_args()
    try:
        report = evaluate_repair_proposal(args.proposal_dir,
                                          expected_proposal_sha256=args.proposal_sha256)
    except (RepairBundleError, OSError, ValueError):
        print(json.dumps({"status": "evaluation_input_rejected"}))
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["comparison"] == "same_as_current_baseline" else 1


if __name__ == "__main__":
    raise SystemExit(main())
