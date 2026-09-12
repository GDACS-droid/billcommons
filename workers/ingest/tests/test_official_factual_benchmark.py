"""Offline contracts for the bounded official factual benchmark runner."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from billcommons_ingest import official_factual_benchmark as benchmark


FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST = FIXTURES / "official_factual_benchmark.json"
SOURCE_FIXTURE = FIXTURES / "fl_senate_detail_2025_7031.html"


def _copy_case(tmp_path: Path) -> Path:
    copied_fixture = tmp_path / SOURCE_FIXTURE.name
    shutil.copyfile(SOURCE_FIXTURE, copied_fixture)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path / "manifest.json"


def _manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(path: Path, content: dict) -> None:
    path.write_text(json.dumps(content), encoding="utf-8")


def test_real_captured_case_passes_with_scoped_coverage_and_honest_provenance():
    report = benchmark.run_official_factual_benchmark(MANIFEST)

    assert report["passed"] is True
    assert report["coverage"] == {
        "jurisdictions": ["FL"], "jurisdiction_count": 1, "case_count": 1, "adapters": ["fl-senate-bill-history"],
    }
    result = report["results"][0]
    assert result["source"] == {
        "url": "https://www.flsenate.gov/Session/Bill/2025/7031",
        "derived_fixture_sha256": hashlib.sha256(SOURCE_FIXTURE.read_bytes()).hexdigest(),
        "recorded_original_capture_sha256": "e57e81a8826451dba2b758771274715dd3d8377582fecec54272b3ba9bb73698",
        "original_capture_verification": "not_verified_original_bytes_not_retained",
    }
    assert "does not establish current-source freshness" in report["interpretation"]
    assert all(fact["passed"] for fact in result["facts"])


def test_altered_expected_fact_is_a_benchmark_failure(tmp_path):
    path = _copy_case(tmp_path)
    manifest = _manifest(path)
    manifest["cases"][0]["facts"][1]["expected"] = "Not Taxation"
    _write_manifest(path, manifest)

    report = benchmark.run_official_factual_benchmark(path)

    assert report["passed"] is False
    failed = [fact for fact in report["results"][0]["facts"] if not fact["passed"]]
    assert failed == [{"field": "title", "expected": "Not Taxation", "actual": "Taxation", "passed": False}]


def test_tampered_fixture_fails_its_pinned_hash(tmp_path):
    path = _copy_case(tmp_path)
    fixture = tmp_path / SOURCE_FIXTURE.name
    fixture.write_bytes(fixture.read_bytes() + b"\n<!-- tampered -->\n")

    report = benchmark.run_official_factual_benchmark(path)

    result = report["results"][0]
    assert report["passed"] is False
    assert result["error"]["code"] == "fixture_sha256_mismatch"
    assert result["fixture"]["actual_sha256"] != result["fixture"]["expected_sha256"]


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("adapter", "unsupported-adapter", "unsupported adapter"),
        ("jurisdiction", "CA", "unsupported jurisdiction"),
    ],
)
def test_unsupported_adapter_or_jurisdiction_is_rejected(tmp_path, key, value, message):
    path = _copy_case(tmp_path)
    manifest = _manifest(path)
    manifest["cases"][0][key] = value
    _write_manifest(path, manifest)

    with pytest.raises(benchmark.OfficialFactualBenchmarkError, match=message):
        benchmark.run_official_factual_benchmark(path)


def test_unknown_fact_field_is_rejected(tmp_path):
    path = _copy_case(tmp_path)
    manifest = _manifest(path)
    manifest["cases"][0]["facts"][0]["field"] = "canonical_status"
    _write_manifest(path, manifest)

    with pytest.raises(benchmark.OfficialFactualBenchmarkError, match="unsupported fact field"):
        benchmark.run_official_factual_benchmark(path)


@pytest.mark.parametrize("fixture_path", ["missing.html", "../fl_senate_detail_2025_7031.html", "/tmp/outside.html"])
def test_missing_or_unsafe_fixture_path_is_rejected(tmp_path, fixture_path):
    path = _copy_case(tmp_path)
    manifest = _manifest(path)
    manifest["cases"][0]["fixture"]["path"] = fixture_path
    _write_manifest(path, manifest)

    with pytest.raises(benchmark.OfficialFactualBenchmarkError, match="fixture"):
        benchmark.run_official_factual_benchmark(path)


def test_symlink_fixture_is_rejected_before_reading(tmp_path):
    path = _copy_case(tmp_path)
    fixture = tmp_path / SOURCE_FIXTURE.name
    fixture.unlink()
    fixture.symlink_to(SOURCE_FIXTURE)

    with pytest.raises(benchmark.OfficialFactualBenchmarkError, match="symlink"):
        benchmark.run_official_factual_benchmark(path)


def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(benchmark.OfficialFactualBenchmarkError, match="duplicate JSON key"):
        benchmark.run_official_factual_benchmark(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"schema_version":NaN}', encoding="utf-8")
    with pytest.raises(benchmark.OfficialFactualBenchmarkError, match="non-finite"):
        benchmark.run_official_factual_benchmark(nonfinite)


def test_report_is_deterministic_and_cli_smoke_is_offline(tmp_path):
    path = _copy_case(tmp_path)
    first = benchmark.run_official_factual_benchmark(path)
    second = benchmark.run_official_factual_benchmark(path)
    assert first == second

    root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.pop("BILLCOMMONS_TEST_DATABASE_URL", None)
    environment.pop("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE", None)
    environment["PYTHONPATH"] = str(root / "workers" / "ingest")
    completed = subprocess.run(
        [sys.executable, "-m", "billcommons_ingest.official_factual_benchmark", str(path)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == first
    assert completed.stderr == ""
