"""Compare a pinned CA proposal against separately pinned corpus expectations.

The corpus is a caller-owned correctness oracle, never generated from candidate
output. This module verifies its integrity, not the truth of its annotations.
Only the candidate source and fixture enter the existing native sandbox.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from billcommons_shared.ca_official_actions import OfficialCaActionsError, _require_delta_url
from billcommons_ingest.official_parser_provenance import is_sha256
from billcommons_ingest.official_repair_bundle import (
    RepairBundleError, _unique_json_object, _reject_nonfinite_json,
)
from billcommons_ingest.official_repair_evaluation import _fact_evidence, _verified_bytes
from billcommons_ingest.official_repair_proposal import validate_repair_proposal
from billcommons_ingest.official_repair_sandbox import (
    MAX_FIXTURE_BYTES, MAX_SOURCE_BYTES, run_ca_parser,
)

CORPUS_VERSION = "official-repair-corpus/1"
MAX_CASES = 16
MAX_CORPUS_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


def _load_corpus(root: Path, digest: str) -> tuple[dict, list[bytes]]:
    if not is_sha256(digest):
        raise RepairBundleError("a separately trusted corpus digest is required")
    raw = _verified_bytes(root / "corpus.json", digest=digest, maximum=MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw, object_pairs_hook=_unique_json_object,
                              parse_constant=_reject_nonfinite_json)
        if (not isinstance(manifest, dict)
                or set(manifest) != {"corpus_version", "review_reference", "cases"}
                or manifest["corpus_version"] != CORPUS_VERSION
                or not isinstance(manifest["review_reference"], str)
                or not 1 <= len(manifest["review_reference"].strip()) <= 2048
                or not isinstance(manifest["cases"], list)
                or not 1 <= len(manifest["cases"]) <= MAX_CASES):
            raise ValueError("invalid corpus manifest")
        identities = set()
        fixtures = []
        total = 0
        for case in manifest["cases"]:
            if (not isinstance(case, dict)
                    or set(case) != {"id", "fixture_sha256", "source_url", "retrieved_at", "expected"}
                    or not isinstance(case["id"], str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", case["id"])
                    or case["id"] in identities
                    or not is_sha256(case["fixture_sha256"])
                    or not isinstance(case["source_url"], str)
                    or not 1 <= len(case["source_url"]) <= 2048
                    or not isinstance(case["retrieved_at"], str)
                    or not 1 <= len(case["retrieved_at"]) <= 64):
                raise ValueError("invalid corpus case")
            retrieved = datetime.fromisoformat(case["retrieved_at"])
            if retrieved.tzinfo is None or retrieved.utcoffset() is None:
                raise ValueError("corpus timestamp requires timezone")
            if _require_delta_url(case["source_url"]) != case["source_url"]:
                raise ValueError("corpus requires the canonical CA source URL")
            request = {"source_url": case["source_url"], "retrieved_at": case["retrieved_at"]}
            if len(json.dumps(request, ensure_ascii=True).encode("utf-8")) > 8 * 1024:
                raise ValueError("corpus request exceeds native bootstrap limit")
            expected = case["expected"]
            if (not isinstance(expected, dict)
                    or set(expected) != {"facts_sha256", "bill_count", "event_count"}
                    or not is_sha256(expected["facts_sha256"])
                    or any(type(expected[key]) is not int or not 0 <= expected[key] <= 100_000
                           for key in ("bill_count", "event_count"))):
                raise ValueError("invalid expected facts")
            identities.add(case["id"])
            fixture = _verified_bytes(root / (case["id"] + ".zip"),
                digest=case["fixture_sha256"], maximum=min(MAX_FIXTURE_BYTES, MAX_CORPUS_BYTES - total))
            if not fixture:
                raise ValueError("empty corpus fixture")
            total += len(fixture)
            fixtures.append(fixture)
        return manifest, fixtures
    except (OfficialCaActionsError, ValueError, TypeError, RecursionError) as exc:
        raise RepairBundleError("corpus evidence is invalid") from exc


def evaluate_repair_corpus(proposal_dir: str | Path, *, expected_proposal_sha256: str,
                           corpus_dir: str | Path, expected_corpus_sha256: str) -> dict:
    """Preflight every case before executing; stop the sequence on host failure.

    Callers must select and pin the corpus independently of candidate authorship.
    A match establishes agreement with those expectations, not their correctness
    or statewide coverage. This report never authorizes promotion.
    """
    root = Path(proposal_dir).absolute()
    proposal = validate_repair_proposal(root, expected_proposal_sha256=expected_proposal_sha256)
    source = _verified_bytes(root / "candidate-parser.py.txt",
        digest=proposal["artifacts"]["candidate-parser.py.txt"]["sha256"], maximum=MAX_SOURCE_BYTES)
    manifest, fixtures = _load_corpus(Path(corpus_dir).absolute(), expected_corpus_sha256)
    report = {
        "evaluation_version": "official-repair-corpus-evaluation/1",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "proposal_sha256": expected_proposal_sha256,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "corpus_sha256": expected_corpus_sha256,
        "review_reference": manifest["review_reference"],
        "oracle_authority": "caller_pinned_expectations",
        "scope": "explicit_ca_corpus_cases",
        "case_count": len(fixtures),
        "promotion_authorized": False,
        "cases": [],
    }
    for case, fixture in zip(manifest["cases"], fixtures, strict=True):
        result = run_ca_parser(source, fixture, source_url=case["source_url"],
                               retrieved_at=case["retrieved_at"])
        entry = {"id": case["id"], "status": result.status,
                 "fixture_sha256": result.fixture_sha256,
                 "source_sha256": result.source_sha256,
                 "bootstrap_sha256": result.bootstrap_sha256,
                 "expected": case["expected"], "matches_expected": False}
        if result.status == "returned":
            try:
                entry["observed"] = _fact_evidence(result.payload)
            except (ValueError, TypeError, RecursionError):
                entry["status"] = "invalid_output"
            else:
                entry["matches_expected"] = entry["observed"] == case["expected"]
        report["cases"].append(entry)
        if result.status == "isolation_unavailable":
            entry["status_detail"] = "bootstrap_failure_or_candidate_exit_78"
        if result.status in {"cleanup_pending", "runner_busy", "isolation_unavailable", "supervisor_failed"}:
            if result.cleanup is not None:
                entry["cleanup"] = result.cleanup
            report["comparison"] = result.status
            report["unexecuted_case_ids"] = [item["id"] for item in manifest["cases"][len(report["cases"]):]]
            return report
    report["comparison"] = ("matches_pinned_expectations" if all(
        entry["matches_expected"] for entry in report["cases"]) else "corpus_mismatch")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal_dir", type=Path)
    parser.add_argument("--proposal-sha256", required=True)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--corpus-sha256", required=True)
    args = parser.parse_args()
    try:
        report = evaluate_repair_corpus(args.proposal_dir,
            expected_proposal_sha256=args.proposal_sha256,
            corpus_dir=args.corpus_dir, expected_corpus_sha256=args.corpus_sha256)
    except (RepairBundleError, OSError, ValueError):
        print(json.dumps({"status": "corpus_input_rejected"}))
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["comparison"] == "matches_pinned_expectations" else 1


if __name__ == "__main__":
    raise SystemExit(main())
