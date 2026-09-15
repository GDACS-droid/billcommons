"""Run authored checks through the real sandbox without trusting their verdicts."""
import hashlib

import pytest

from billcommons_ingest import official_repair_evaluation as evaluation
from billcommons_ingest.official_repair_bundle import RepairBundleError
from billcommons_ingest.official_repair_sandbox import SandboxResult
from .test_official_repair_evaluation import _proposal
from .test_official_repair_sandbox import isolated_supervisor_state as isolated_supervisor_state


CHECK = '''def run_regression(parser, fixture, *, source_url, retrieved_at):
    batch = parser.parse_ca_official_actions_zip(fixture, source_url=source_url, retrieved_at=retrieved_at)
    assert len(batch.scoped_bill_ids) == 1
    assert sum(len(events) for events in batch.events_by_official_bill_id.values()) == 1
'''


def test_real_regression_calls_both_parsers_and_remains_non_authoritative(tmp_path):
    root, digest, manifest = _proposal(tmp_path, lambda raw: raw + b"\n# equivalent\n", CHECK)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest, run_regressions=True)
    checks = report["regression_tests"]
    assert report["comparison"] == "same_as_current_baseline"
    assert checks["baseline"]["status"] == checks["candidate"]["status"] == "returned_without_error"
    assert checks["source_sha256"] == manifest["artifacts"]["candidate-regression.py.txt"]["sha256"]
    assert checks["baseline"]["program_sha256"] != checks["candidate"]["program_sha256"]
    assert checks["trusted_correctness_proof"] is False
    assert report["promotion_authorized"] is False


def test_differential_assertion_observes_baseline_failure_candidate_return(tmp_path):
    regression = CHECK + "    assert parser.REPAIR_MARKER == 'present'\n"
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\nREPAIR_MARKER = 'present'\n", regression)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest, run_regressions=True)
    assert report["regression_tests"]["baseline"]["status"] == "child_failed"
    assert report["regression_tests"]["candidate"]["status"] == "returned_without_error"
    assert report["promotion_authorized"] is False


@pytest.mark.parametrize("regression", [
    "raise RuntimeError('top-level failure')\n",
    "def run_regression(*args, **kwargs):\n    return False\n",
    "def run_regression(*args, **kwargs):\n    assert False\n",
    "def unrelated():\n    pass\n",
    "invalid python syntax!\n",
])
def test_invalid_or_failing_authored_code_never_returns_success(tmp_path, regression):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# changed\n", regression)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest, run_regressions=True)
    assert report["comparison"] == "same_as_current_baseline"
    assert report["regression_tests"]["baseline"]["status"] == "child_failed"
    assert report["regression_tests"]["candidate"]["status"] == "child_failed"


def test_regression_cannot_write_host_file(tmp_path):
    forbidden = tmp_path / "forbidden-output"
    regression = f"def run_regression(*args, **kwargs):\n    open({str(forbidden)!r}, 'w').write('escape')\n"
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# changed\n", regression)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest, run_regressions=True)
    assert report["regression_tests"]["candidate"]["status"] == "child_failed"
    assert not forbidden.exists()


def test_forged_regression_output_is_explicitly_not_correctness_proof(tmp_path):
    regression = 'import os\nos.write(1, b\'{"status":"parsed","bills":[]}\')\nos._exit(0)\n'
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# changed\n", regression)
    report = evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest, run_regressions=True)
    assert report["regression_tests"]["candidate"]["status"] == "returned_without_error"
    assert report["regression_tests"]["trusted_correctness_proof"] is False
    assert report["promotion_authorized"] is False


@pytest.mark.parametrize("status", sorted(evaluation._HOST_STOP))
def test_regression_host_failure_stops_before_second_run(status, monkeypatch):
    calls = []
    def stopped(source, fixture, **kwargs):
        calls.append(source)
        return SandboxResult(status, hashlib.sha256(source).hexdigest(),
                             hashlib.sha256(fixture).hexdigest(), "f" * 64)
    monkeypatch.setattr(evaluation, "run_ca_parser", stopped)
    report = evaluation._run_regressions({"baseline": b"before", "candidate": b"after"},
                                         b"fixture", {"source_url": "x", "retrieved_at": "x"}, "a" * 64)
    assert calls == [b"before"]
    assert report["status"] == status
    assert report["candidate"]["status"] == "not_run"


def test_regression_substitution_rejected_before_any_execution(tmp_path, monkeypatch):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# changed\n", CHECK)
    validate = evaluation.validate_repair_proposal
    def changed(*args, **kwargs):
        result = validate(*args, **kwargs)
        (root / "candidate-regression.py.txt").write_text("substituted")
        return result
    monkeypatch.setattr(evaluation, "validate_repair_proposal", changed)
    monkeypatch.setattr(evaluation, "run_ca_parser", lambda *a, **kw: pytest.fail("must remain inert"))
    with pytest.raises(RepairBundleError, match="changed after validation"):
        evaluation.evaluate_repair_proposal(root, expected_proposal_sha256=digest, run_regressions=True)


def test_combined_source_limit_is_checked_without_compilation():
    with pytest.raises(RepairBundleError, match="combined regression"):
        evaluation._regression_program(b"x" * evaluation.MAX_SOURCE_BYTES, b"y")


@pytest.mark.parametrize("status,code", [("returned_without_error", 0), ("child_failed", 1),
                                        ("cleanup_pending", 1), ("invalid_output", 1)])
def test_cli_nonzero_when_requested_regression_does_not_return(status, code, monkeypatch, capsys):
    monkeypatch.setattr(evaluation.sys, "argv", ["evaluate", "/unused", "--proposal-sha256",
                                               "a" * 64, "--run-regressions"])
    def report(*args, **kwargs):
        assert kwargs["run_regressions"] is True
        return {"comparison": "same_as_current_baseline", "regression_tests": {
            "status": "observed", "baseline": {"status": "returned_without_error"},
            "candidate": {"status": status}}}
    monkeypatch.setattr(evaluation, "evaluate_repair_proposal", report)
    assert evaluation.main() == code
    assert "same_as_current_baseline" in capsys.readouterr().out
