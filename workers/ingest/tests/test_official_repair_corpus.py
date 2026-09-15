"""Pinned expectations are authored independently of the evaluated parser."""
import hashlib
import io
import json
import zipfile

import pytest

from billcommons_ingest import official_repair_corpus as corpus
from billcommons_ingest.official_repair_bundle import RepairBundleError
from billcommons_ingest.official_repair_sandbox import SandboxResult
from .test_official_repair_bundle import _archive
from .test_official_repair_evaluation import _proposal
from .test_official_repair_sandbox import isolated_supervisor_state as isolated_supervisor_state

URL = "https://downloads.leginfo.legislature.ca.gov/pubinfo_Mon.zip"


def _expected(with_event):
    # Explicit source-row annotations; neither baseline nor candidate output
    # supplies the oracle. Hashing uses the documented canonical JSON format.
    events = [{"occurrence_id": "ca-history:101", "official_bill_id": "202520260AB12",
        "history_id": "101", "action_date": "2026-09-01", "description": "Read first time.",
        "sequence": 1, "updated_at": "2026-09-01 12:00:00", "source_url": URL,
        "raw_fields": {"bill_id": "202520260AB12", "bill_history_id": "101",
            "action_date": "2026-09-01 00:00:00", "action": "Read first time.",
            "trans_uid": "source", "trans_update_dt": "2026-09-01 12:00:00",
            "action_sequence": "1", "action_code": "x", "action_status": "x",
            "primary_location": "x", "secondary_location": "x", "ternary_location": "x",
            "end_status": "x"}}] if with_event else []
    facts = {"status": "parsed", "bills": [{"bill_id": "202520260AB12", "events": events}]}
    raw = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {"bill_count": 1, "event_count": int(with_event),
            "facts_sha256": hashlib.sha256(raw).hexdigest()}


def _corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    full = _archive()
    empty = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(full)) as before, zipfile.ZipFile(empty, "w") as after:
        after.writestr("BILL_TBL.dat", before.read("BILL_TBL.dat"))
        after.writestr("BILL_HISTORY_TBL.dat", b"")
    manifest = {"corpus_version": corpus.CORPUS_VERSION,
                "review_reference": "synthetic TSV rows explicitly annotated in test", "cases": []}
    for name, fixture, with_event in (("one-action", full, True), ("no-actions", empty.getvalue(), False)):
        (root / (name + ".zip")).write_bytes(fixture)
        manifest["cases"].append({"id": name, "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
            "source_url": URL, "retrieved_at": "2026-09-15T00:00:00+00:00", "expected": _expected(with_event)})
    return root, _write_manifest(root, manifest), manifest


def _write_manifest(root, manifest):
    raw = json.dumps(manifest, sort_keys=True).encode()
    (root / "corpus.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _evaluate(root, digest, oracle, pin):
    return corpus.evaluate_repair_corpus(root, expected_proposal_sha256=digest,
        corpus_dir=oracle, expected_corpus_sha256=pin)


def test_real_candidate_matches_independently_annotated_multicase_corpus(tmp_path):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# corpus candidate\n")
    oracle, pin, _ = _corpus(tmp_path)
    report = _evaluate(root, digest, oracle, pin)
    assert report["comparison"] == "matches_pinned_expectations"
    assert [case["observed"]["event_count"] for case in report["cases"]] == [1, 0]
    assert report["case_count"] == 2
    assert report["corpus_sha256"] == pin
    assert report["promotion_authorized"] is False


def test_same_counts_with_wrong_facts_fails_corpus(tmp_path):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw.replace(
        b"description=action.description,", b"description=action.description + ' wrong',"))
    oracle, pin, _ = _corpus(tmp_path)
    report = _evaluate(root, digest, oracle, pin)
    assert report["comparison"] == "corpus_mismatch"
    assert report["cases"][0]["observed"]["event_count"] == 1
    assert report["cases"][0]["matches_expected"] is False
    assert report["cases"][1]["matches_expected"] is True


@pytest.mark.parametrize("mutation", ["wrong_pin", "late_fixture", "duplicate_id", "path_escape",
    "empty", "too_many", "boolean_count", "naive_time", "bad_oracle", "size_limit",
    "http_url", "nonascii_url", "noncanonical_url"])
def test_all_evidence_preflight_precedes_candidate_execution(tmp_path, monkeypatch, mutation):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# corpus candidate\n")
    oracle, pin, manifest = _corpus(tmp_path)
    if mutation == "wrong_pin":
        pin = "0" * 64
    elif mutation == "late_fixture":
        (oracle / "no-actions.zip").write_bytes(b"substituted second fixture")
    else:
        if mutation == "duplicate_id": manifest["cases"][1]["id"] = "one-action"
        if mutation == "path_escape": manifest["cases"][0]["id"] = "../outside"
        if mutation == "empty": manifest["cases"] = []
        if mutation == "too_many": manifest["cases"] *= 9
        if mutation == "boolean_count": manifest["cases"][0]["expected"]["bill_count"] = True
        if mutation == "naive_time": manifest["cases"][0]["retrieved_at"] = "2026-09-15"
        if mutation == "bad_oracle": manifest["cases"][0]["expected"]["facts_sha256"] = "invalid"
        if mutation == "size_limit": monkeypatch.setattr(corpus, "MAX_CORPUS_BYTES", 1)
        if mutation == "http_url": manifest["cases"][0]["source_url"] = URL.replace("https:", "http:")
        if mutation == "nonascii_url": manifest["cases"][0]["source_url"] = "\U0001f600" * 2048
        if mutation == "noncanonical_url": manifest["cases"][0]["source_url"] = "\n" + URL
        pin = _write_manifest(oracle, manifest)
    monkeypatch.setattr(corpus, "run_ca_parser", lambda *a, **k: pytest.fail("unbound input executed"))
    with pytest.raises(RepairBundleError):
        _evaluate(root, digest, oracle, pin)


@pytest.mark.parametrize("status", ["cleanup_pending", "runner_busy", "isolation_unavailable", "supervisor_failed"])
def test_host_failure_stops_remaining_cases(tmp_path, monkeypatch, status):
    root, digest, _ = _proposal(tmp_path, lambda raw: raw + b"\n# corpus candidate\n")
    oracle, pin, _ = _corpus(tmp_path)
    calls = []
    def stopped(source, fixture, **kwargs):
        calls.append(fixture)
        return SandboxResult(status, hashlib.sha256(source).hexdigest(),
            hashlib.sha256(fixture).hexdigest(), "f" * 64, cleanup={"status": "pending"})
    monkeypatch.setattr(corpus, "run_ca_parser", stopped)
    report = _evaluate(root, digest, oracle, pin)
    assert len(calls) == 1
    assert report["comparison"] == status
    assert report["unexecuted_case_ids"] == ["no-actions"]
    assert report["cases"][0]["cleanup"] == {"status": "pending"}
