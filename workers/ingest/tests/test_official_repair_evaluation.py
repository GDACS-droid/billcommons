"""Pin proposal evidence, then exercise real isolated candidate comparisons."""
import inspect
import hashlib
import io
import zipfile

import pytest

from billcommons_ingest import official_repair_evaluation as evaluation
from billcommons_ingest.official_repair_bundle import (
    RepairBundleError, _manifest_sha256, build_repair_bundle,
)
from billcommons_ingest.official_repair_proposal import prepare_repair_proposal
from billcommons_ingest.official_repair_sandbox import SandboxResult
from .test_official_repair_bundle import _archive, _failure_db
from .test_official_repair_sandbox import isolated_supervisor_state as isolated_supervisor_state


def _proposal(tmp_path, transform, regression_text="raise RuntimeError('authored regression must remain inert')\n"):
    db, observation = _failure_db(raw=_archive())
    bundle = tmp_path / "bundle"
    baseline = build_repair_bundle(db, observation.id, bundle)
    source = inspect.getsourcefile(evaluation.ca.parse_ca_official_actions_zip)
    original = open(source, "rb").read()
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(transform(original))
    regression = tmp_path / "regression.py"
    regression.write_text(regression_text)
    output = tmp_path / "proposal"
    manifest = prepare_repair_proposal(bundle,
        expected_manifest_sha256=_manifest_sha256(baseline),
        candidate_source=candidate, regression_source=regression, output_dir=output)
    return output, _manifest_sha256(manifest), manifest


def test_changed_source_with_identical_facts_is_a_regression_observation_only(tmp_path):
    root, digest, manifest = _proposal(tmp_path, lambda raw: raw + b"\n# candidate review probe\n")
    original_manifest = (root / "proposal.json").read_bytes()
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest)
    assert report["comparison"] == "same_as_current_baseline"
    assert report["candidate"]["facts"]["event_count"] == 1
    assert report["candidate"]["facts"] == report["trusted_baseline"]
    assert report["candidate"]["source_sha256"] == manifest["artifacts"]["candidate-parser.py.txt"]["sha256"]
    assert report["candidate"]["source_sha256"] != report["baseline"]["source_sha256"]
    assert report["regression_tests"]["status"] == "not_run"
    assert report["promotion_authorized"] is False
    assert (root / "proposal.json").read_bytes() == original_manifest


def test_same_counts_with_changed_action_content_cannot_pass(tmp_path):
    def alter(raw):
        before = b"description=action.description,"
        assert before in raw
        return raw.replace(before, b"description=action.description + ' altered',")
    root, digest, _ = _proposal(tmp_path, alter)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest)
    assert report["comparison"] == "different_from_current_baseline"
    assert report["candidate"]["facts"]["event_count"] == report["trusted_baseline"]["event_count"]
    assert report["candidate"]["facts"]["facts_sha256"] != report["trusted_baseline"]["facts_sha256"]


def test_forged_empty_result_does_not_become_success(tmp_path):
    forged = b'import os\nos.write(1, b\'{"status":"parsed","bills":[]}\')\nos._exit(0)\n'
    root, digest, _ = _proposal(tmp_path, lambda raw: forged)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest)
    assert report["candidate"]["status"] == "returned"
    assert report["comparison"] == "different_from_current_baseline"
    assert report["promotion_authorized"] is False


def test_malformed_nested_facts_are_rejected_by_parent(tmp_path):
    forged = b'import os\nos.write(1, b\'{"status":"parsed","bills":[{}]}\')\nos._exit(0)\n'
    root, digest, _ = _proposal(tmp_path, lambda raw: forged)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest)
    assert report["candidate"]["status"] == "invalid_output"
    assert report["comparison"] == "candidate_did_not_return_valid_facts"


def test_wrong_trusted_digest_stops_before_execution(tmp_path, monkeypatch):
    root, _, _ = _proposal(tmp_path, lambda raw: raw + b"\n# candidate\n")
    def forbidden(*args, **kwargs):
        pytest.fail("unbound proposal must not execute")
    monkeypatch.setattr(evaluation, "run_ca_parser", forbidden)
    with pytest.raises(RepairBundleError):
        evaluation.evaluate_repair_proposal(root, expected_proposal_sha256="0" * 64)


@pytest.mark.parametrize("status", ["cleanup_pending", "runner_busy", "isolation_unavailable", "supervisor_failed"])
def test_baseline_host_failure_stops_candidate_execution(tmp_path, monkeypatch, status):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# candidate\n")
    calls = []
    def stopped(source, fixture, **kwargs):
        calls.append(source)
        return SandboxResult(status, hashlib.sha256(source).hexdigest(),
            hashlib.sha256(fixture).hexdigest(), "f" * 64)
    monkeypatch.setattr(evaluation, "run_ca_parser", stopped)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest)
    assert len(calls) == 1
    assert report["comparison"] == status
    assert report["candidate"]["status"] == "not_run"
    if status == "isolation_unavailable":
        assert report["baseline"]["status_detail"] == "bootstrap_failure_or_candidate_exit_78"


def test_baseline_output_failure_is_distinct_from_a_factual_mismatch(tmp_path, monkeypatch):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# candidate\n")
    monkeypatch.setattr(evaluation, "run_ca_parser", lambda source, fixture, **kwargs:
        SandboxResult("invalid_output", hashlib.sha256(source).hexdigest(),
                      hashlib.sha256(fixture).hexdigest(), "f" * 64))
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest)
    assert report["comparison"] == "baseline_did_not_return_valid_facts"


def test_candidate_changed_after_validation_stops_before_execution(tmp_path, monkeypatch):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# candidate\n")
    original = evaluation.validate_repair_proposal
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (root / "candidate-parser.py.txt").write_text("raise RuntimeError('substitution')")
        return result
    def forbidden(*args, **kwargs):
        pytest.fail("substituted candidate must not execute")
    monkeypatch.setattr(evaluation, "validate_repair_proposal", changed)
    monkeypatch.setattr(evaluation, "run_ca_parser", forbidden)
    with pytest.raises(RepairBundleError, match="changed after validation"):
        evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest)


def test_verified_read_is_pinned_when_parent_path_is_replaced(tmp_path, monkeypatch):
    parent = tmp_path / "original"
    parent.mkdir()
    (parent / "source").write_bytes(b"approved bytes")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source").write_bytes(b"different synthetic bytes")
    original_open = evaluation.os.open
    def swap(path, flags, *args, **kwargs):
        if path == "source":
            parent.rename(tmp_path / "renamed")
            parent.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(evaluation.os, "open", swap)
    assert evaluation._verified_bytes(parent / "source",
        digest=hashlib.sha256(b"approved bytes").hexdigest(), maximum=100) == b"approved bytes"


def test_verified_read_rejects_existing_ancestor_symlink(tmp_path):
    parent = tmp_path / "original"
    parent.mkdir()
    (parent / "source").write_bytes(b"approved bytes")
    linked = tmp_path / "linked"
    linked.symlink_to(parent, target_is_directory=True)
    with pytest.raises(RepairBundleError, match="unreadable"):
        evaluation._verified_bytes(linked / "source",
            digest=hashlib.sha256(b"approved bytes").hexdigest(), maximum=100)


def test_real_parser_preserves_empty_event_mapping():
    # Reconcile the review's alleged missing-key case using the real parser.
    # Scoped IDs and the event map are built from the same parsed dictionary.
    raw = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_archive())) as before:
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as after:
            after.writestr("BILL_TBL.dat", before.read("BILL_TBL.dat"))
            after.writestr("BILL_HISTORY_TBL.dat", b"")
    from datetime import datetime, timezone
    batch = evaluation.ca.parse_ca_official_actions_zip(raw.getvalue(),
        source_url="https://downloads.leginfo.legislature.ca.gov/pubinfo_Mon.zip",
        retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert evaluation._facts(batch) == {"status": "parsed", "bills": [
        {"bill_id": "202520260AB12", "events": []}]}
