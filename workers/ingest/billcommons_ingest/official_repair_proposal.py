"""Stage proposed CA parser code and tests as inert, evidence-bound artifacts."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import uuid

from billcommons_ingest import official_ca_actions as ca
from billcommons_ingest.official_repair_bundle import (
    FIXTURE_NAME, MANIFEST_NAME, TEST_NAME, RepairBundleError,
    _canonical_json_bytes, _output_path, _reject_symlink_path,
    _unique_json_object, _reject_nonfinite_json, validate_repair_bundle,
)
from billcommons_ingest.official_parser_provenance import is_sha256

MAX_PROPOSAL_BYTES = 256 * 1024
PARSER_TARGET = "packages/shared/billcommons_shared/ca_official_actions.py"


def _read_source(path: Path, maximum: int = MAX_PROPOSAL_BYTES) -> bytes:
    _reject_symlink_path(path.absolute())
    if not path.is_file() or path.stat().st_size > maximum:
        raise RepairBundleError("proposal source must be a bounded regular file")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if not raw.strip() or len(raw) > maximum:
        raise RepairBundleError("proposal source is empty or too large")
    # Decode only. Importing, compiling, or running model output belongs to the
    # isolated evaluator, not the evidence preparation process.
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepairBundleError("proposal source must be UTF-8") from exc
    return raw


def _patch(baseline: bytes, candidate: bytes) -> bytes:
    lines = difflib.unified_diff(
        io.StringIO(baseline.decode("utf-8"), newline="\n").readlines(),
        io.StringIO(candidate.decode("utf-8"), newline="\n").readlines(),
        fromfile=f"a/{PARSER_TARGET}", tofile=f"b/{PARSER_TARGET}",
    )
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in lines).encode("utf-8")


def _manifest(baseline_digest: str, files: dict[str, bytes]) -> dict:
    return {
        "proposal_version": "official-repair-proposal/1",
        "baseline_manifest_sha256": baseline_digest,
        "parser_target": PARSER_TARGET,
        "artifacts": {name: {"sha256": hashlib.sha256(raw).hexdigest(), "byte_count": len(raw)}
                      for name, raw in files.items()},
        "evaluation": {"status": "not_run", "requires": "isolated_candidate_runner"},
        "promotion_state": "requires_human_review",
        "execution_authorized": False,
    }


def validate_repair_proposal(proposal_dir: str | Path, *, expected_proposal_sha256: str) -> dict:
    """Authenticate an inert proposal against a separately retained digest."""
    if not is_sha256(expected_proposal_sha256):
        raise RepairBundleError("a separately trusted proposal digest is required")
    root = Path(proposal_dir).absolute()
    _reject_symlink_path(root)
    try:
        manifest = json.loads(_read_source(root / "proposal.json", 8192),
                              object_pairs_hook=_unique_json_object,
                              parse_constant=_reject_nonfinite_json)
        if hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest() != expected_proposal_sha256:
            raise RepairBundleError("proposal does not match the trusted digest")
        if not isinstance(manifest, dict) or not is_sha256(manifest.get("baseline_manifest_sha256")):
            raise RepairBundleError("proposal baseline digest is invalid")
        names = {"baseline-parser.py.txt", "candidate-parser.py.txt",
                 "candidate-regression.py.txt", "candidate.patch"}
        if {path.name for path in root.iterdir()} != names | {"baseline", "proposal.json"}:
            raise RepairBundleError("proposal file topology is invalid")
        evidence = root / "baseline"
        _reject_symlink_path(evidence)
        if {path.name for path in evidence.iterdir()} != {FIXTURE_NAME, MANIFEST_NAME, TEST_NAME}:
            raise RepairBundleError("baseline file topology is invalid")
        baseline = validate_repair_bundle(evidence, expected_manifest_sha256=manifest["baseline_manifest_sha256"])
        files = {name: _read_source(root / name, MAX_PROPOSAL_BYTES * 4 if name == "candidate.patch"
                                   else MAX_PROPOSAL_BYTES) for name in names}
        if manifest != _manifest(manifest["baseline_manifest_sha256"], files):
            raise RepairBundleError("proposal artifact evidence does not verify")
        before, after = files["baseline-parser.py.txt"], files["candidate-parser.py.txt"]
        if hashlib.sha256(before).hexdigest() != baseline["candidate_parser"]["source_sha256"]:
            raise RepairBundleError("proposal baseline source evidence does not verify")
        if before == after or files["candidate.patch"] != _patch(before, after):
            raise RepairBundleError("proposal patch does not reproduce its candidate")
        return manifest
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, RepairBundleError):
            raise
        raise RepairBundleError("proposal evidence is unreadable or invalid") from exc


def prepare_repair_proposal(
    bundle_dir: str | Path, *, expected_manifest_sha256: str,
    candidate_source: str | Path, regression_source: str | Path,
    output_dir: str | Path,
) -> dict:
    """Publish a complete local proposal without loading candidate Python."""
    if not is_sha256(expected_manifest_sha256):
        raise RepairBundleError("a separately trusted baseline digest is required")
    destination = _output_path(output_dir)
    root = Path(bundle_dir).absolute()
    baseline = validate_repair_bundle(root, expected_manifest_sha256=expected_manifest_sha256)
    candidate = _read_source(Path(candidate_source))
    regression = _read_source(Path(regression_source))
    baseline_path = inspect.getsourcefile(ca.parse_ca_official_actions_zip)
    if baseline_path is None:
        raise RepairBundleError("baseline parser source is unavailable")
    baseline_source = _read_source(Path(baseline_path))
    baseline_hash = hashlib.sha256(baseline_source).hexdigest()
    if baseline_hash != baseline["candidate_parser"]["source_sha256"]:
        raise RepairBundleError("baseline parser changed during proposal preparation")
    if candidate == baseline_source:
        raise RepairBundleError("proposal does not change the parser")
    files = {
        "baseline-parser.py.txt": baseline_source,
        "candidate-parser.py.txt": candidate,
        "candidate-regression.py.txt": regression,
        "candidate.patch": _patch(baseline_source, candidate),
    }
    manifest = _manifest(expected_manifest_sha256, files)
    stage = destination.parent / f".{destination.name}.repair-proposal-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    try:
        evidence = stage / "baseline"
        evidence.mkdir(mode=0o700)
        for name in (FIXTURE_NAME, MANIFEST_NAME, TEST_NAME):
            shutil.copyfile(root / name, evidence / name, follow_symlinks=False)
        # Validate the actual snapshot, catching changes between read and copy.
        validate_repair_bundle(evidence, expected_manifest_sha256=expected_manifest_sha256)
        for name, raw in files.items():
            (stage / name).write_bytes(raw)
        (stage / "proposal.json").write_bytes(_canonical_json_bytes(manifest) + b"\n")
        validate_repair_proposal(stage, expected_proposal_sha256=hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest())
        _output_path(destination)
        os.replace(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--baseline-sha256", required=True)
    parser.add_argument("--candidate-source", type=Path, required=True)
    parser.add_argument("--regression-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = prepare_repair_proposal(
            args.bundle_dir, expected_manifest_sha256=args.baseline_sha256,
            candidate_source=args.candidate_source, regression_source=args.regression_source,
            output_dir=args.output_dir,
        )
        print(json.dumps({"status": "prepared", "execution_authorized": False,
                          "proposal_sha256": hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_class": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
