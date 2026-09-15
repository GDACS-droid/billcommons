"""Pin proposal evidence, then exercise real isolated candidate comparisons."""
import inspect

import pytest

from billcommons_ingest import official_repair_evaluation as evaluation
from billcommons_ingest.official_repair_bundle import (
    RepairBundleError, _manifest_sha256, build_repair_bundle,
)
from billcommons_ingest.official_repair_proposal import prepare_repair_proposal
from .test_official_repair_bundle import _archive, _failure_db


def _proposal(tmp_path, transform):
    db, observation = _failure_db(raw=_archive())
    bundle = tmp_path / "bundle"
    baseline = build_repair_bundle(db, observation.id, bundle)
    source = inspect.getsourcefile(evaluation.ca.parse_ca_official_actions_zip)
    original = open(source, "rb").read()
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(transform(original))
    regression = tmp_path / "regression.py"
    regression.write_text("raise RuntimeError('authored regression must remain inert')\n")
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
