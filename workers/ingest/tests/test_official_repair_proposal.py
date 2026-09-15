"""Exercise inert candidate staging against real baseline bundle validation."""
import hashlib
import json
import inspect
import subprocess
import sys

import pytest

from billcommons_ingest.official_repair_bundle import (
    RepairBundleError, _manifest_sha256, build_repair_bundle, validate_repair_bundle,
)
from billcommons_ingest.official_repair_proposal import prepare_repair_proposal
from billcommons_ingest import official_repair_proposal as proposal
from .test_official_repair_bundle import _archive, _failure_db


def _inputs(tmp_path):
    db, observation = _failure_db(raw=_archive())
    bundle = tmp_path / "baseline"
    manifest = build_repair_bundle(db, observation.id, bundle)
    marker = tmp_path / "must-not-exist"
    candidate = tmp_path / "candidate.py"
    candidate.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    regression = tmp_path / "regression.py"
    regression.write_text("raise RuntimeError('must never execute during preparation')\n")
    return dict(bundle_dir=bundle, expected_manifest_sha256=_manifest_sha256(manifest),
                candidate_source=candidate, regression_source=regression,
                output_dir=tmp_path / "proposal"), marker


def test_preparation_binds_evidence_without_executing_candidate(tmp_path):
    args, marker = _inputs(tmp_path)
    manifest = prepare_repair_proposal(**args)
    assert not marker.exists()
    output = args["output_dir"]
    assert json.loads((output / "proposal.json").read_text()) == manifest
    assert manifest["evaluation"]["status"] == "not_run"
    assert manifest["execution_authorized"] is False
    assert proposal.validate_repair_proposal(output, expected_proposal_sha256=_manifest_sha256(manifest)) == manifest
    for name, evidence in manifest["artifacts"].items():
        raw = (output / name).read_bytes()
        assert evidence == {"sha256": hashlib.sha256(raw).hexdigest(), "byte_count": len(raw)}
    validate_repair_bundle(output / "baseline", expected_manifest_sha256=args["expected_manifest_sha256"])


def test_wrong_baseline_digest_cannot_create_proposal(tmp_path):
    args, _ = _inputs(tmp_path)
    args["expected_manifest_sha256"] = "0" * 64
    with pytest.raises(RepairBundleError, match="trusted manifest digest"):
        prepare_repair_proposal(**args)
    assert not args["output_dir"].exists()


def test_existing_output_is_preserved(tmp_path):
    args, _ = _inputs(tmp_path)
    args["output_dir"].mkdir()
    sentinel = args["output_dir"] / "existing"
    sentinel.write_text("keep")
    with pytest.raises(RepairBundleError, match="non-empty"):
        prepare_repair_proposal(**args)
    assert sentinel.read_text() == "keep"


def test_symlink_candidate_is_rejected(tmp_path):
    args, _ = _inputs(tmp_path)
    link = tmp_path / "linked.py"
    link.symlink_to(args["candidate_source"])
    args["candidate_source"] = link
    with pytest.raises(RepairBundleError, match="symlink"):
        prepare_repair_proposal(**args)
    assert not args["output_dir"].exists()


def test_unchanged_parser_cannot_be_presented_as_repair(tmp_path):
    args, _ = _inputs(tmp_path)
    args["candidate_source"] = inspect.getsourcefile(proposal.ca.parse_ca_official_actions_zip)
    with pytest.raises(RepairBundleError, match="does not change"):
        prepare_repair_proposal(**args)
    assert not args["output_dir"].exists()


def test_failed_snapshot_validation_removes_partial_proposal(tmp_path, monkeypatch):
    args, _ = _inputs(tmp_path)
    original = proposal.validate_repair_bundle

    def validate(path, **kwargs):
        if path != args["bundle_dir"]:
            raise RepairBundleError("snapshot changed")
        return original(path, **kwargs)

    monkeypatch.setattr(proposal, "validate_repair_bundle", validate)
    with pytest.raises(RepairBundleError, match="snapshot changed"):
        prepare_repair_proposal(**args)
    assert not args["output_dir"].exists()
    assert not list(tmp_path.glob(".proposal.repair-proposal-*"))


def test_cli_reports_only_prepared_artifact_digest(tmp_path):
    args, marker = _inputs(tmp_path)
    completed = subprocess.run([
        sys.executable, "-m", "billcommons_ingest.official_repair_proposal",
        str(args["bundle_dir"]), "--baseline-sha256", args["expected_manifest_sha256"],
        "--candidate-source", str(args["candidate_source"]),
        "--regression-source", str(args["regression_source"]),
        "--output-dir", str(args["output_dir"]),
    ], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    manifest = json.loads((args["output_dir"] / "proposal.json").read_text())
    assert result == {"status": "prepared", "execution_authorized": False,
                      "proposal_sha256": _manifest_sha256(manifest)}
    assert not marker.exists()


@pytest.mark.parametrize("ending", ["lf", "no_final", "cr", "crlf", "unicode_separator"])
def test_patch_reproduces_exact_candidate_bytes(tmp_path, ending):
    args, _ = _inputs(tmp_path)
    raw = args["candidate_source"].read_bytes()
    if ending == "no_final":
        raw = raw.rstrip(b"\n")
    elif ending == "cr":
        raw = raw.replace(b"\n", b"\r")
    elif ending == "crlf":
        raw = raw.replace(b"\n", b"\r\n")
    elif ending == "unicode_separator":
        raw = "text = 'a\u2028b'\n".encode("utf-8")
    args["candidate_source"].write_bytes(raw)
    prepare_repair_proposal(**args)
    checkout = tmp_path / "checkout"
    target = checkout / proposal.PARSER_TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes((args["output_dir"] / "baseline-parser.py.txt").read_bytes())
    completed = subprocess.run([
        "git", "apply", str(args["output_dir"] / "candidate.patch"),
    ], cwd=checkout, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert target.read_bytes() == raw


@pytest.mark.parametrize("name", ["baseline-parser.py.txt", "candidate-parser.py.txt",
                                  "candidate-regression.py.txt", "candidate.patch", "proposal.json"])
def test_proposal_validation_rejects_replaced_artifacts(tmp_path, name):
    args, _ = _inputs(tmp_path)
    manifest = prepare_repair_proposal(**args)
    (args["output_dir"] / name).write_text("replaced")
    with pytest.raises(RepairBundleError):
        proposal.validate_repair_proposal(args["output_dir"], expected_proposal_sha256=_manifest_sha256(manifest))


def test_rehashed_but_inconsistent_patch_is_rejected(tmp_path):
    args, _ = _inputs(tmp_path)
    manifest = prepare_repair_proposal(**args)
    replacement = b"not a patch\n"
    (args["output_dir"] / "candidate.patch").write_bytes(replacement)
    manifest["artifacts"]["candidate.patch"] = {"sha256": hashlib.sha256(replacement).hexdigest(),
                                                "byte_count": len(replacement)}
    (args["output_dir"] / "proposal.json").write_text(json.dumps(manifest))
    with pytest.raises(RepairBundleError, match="patch does not reproduce"):
        proposal.validate_repair_proposal(args["output_dir"], expected_proposal_sha256=_manifest_sha256(manifest))


@pytest.mark.parametrize("mutation", ["extra", "symlink", "manifest"])
def test_topology_and_external_digest_are_checked(tmp_path, mutation):
    args, _ = _inputs(tmp_path)
    manifest = prepare_repair_proposal(**args)
    if mutation == "extra":
        (args["output_dir"] / "unexpected.py").write_text("extra")
    elif mutation == "symlink":
        artifact = args["output_dir"] / "candidate-parser.py.txt"
        artifact.unlink()
        artifact.symlink_to(args["candidate_source"])
    else:
        changed = dict(manifest, execution_authorized=True)
        (args["output_dir"] / "proposal.json").write_text(json.dumps(changed))
    with pytest.raises(RepairBundleError):
        proposal.validate_repair_proposal(args["output_dir"], expected_proposal_sha256=_manifest_sha256(manifest))
