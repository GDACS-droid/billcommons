"""Run bounded, offline factual checks against retained official fixtures.

This module deliberately does not fetch a source, query a Bill Commons corpus,
or write a schedule. It checks direct facts from reviewed, version-pinned
fixture bytes. The first adapter is intentionally narrow: one Florida Senate
bill-history detail-page parser.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date
from pathlib import Path, PurePath
from typing import Any

from billcommons_ingest import official_fl_senate_actions as fl_senate


RUNNER_VERSION = "official-factual-benchmark/1"
MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 256 * 1024
MAX_FIXTURE_BYTES = fl_senate.MAX_HTML_BYTES
MAX_REPORT_BYTES = 512 * 1024
MAX_CASES = 64
MAX_FACTS_PER_CASE = 32
MAX_JSON_DEPTH = 32
MAX_PATH_CHARS = 512
MAX_TEXT_CHARS = 16 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
_ADAPTER = "fl-senate-bill-history"
_JURISDICTION = "FL"
_FACT_FIELDS = frozenset({"identity", "title", "table_row_count", "action_count", "final_action", "contains_action"})


class OfficialFactualBenchmarkError(ValueError):
    """A manifest, fixture-boundary, or requested-fact contract failure."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OfficialFactualBenchmarkError(f"manifest contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise OfficialFactualBenchmarkError(f"manifest contains non-finite JSON number {value!r}")


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise OfficialFactualBenchmarkError(f"manifest exceeds JSON depth cap of {MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        for child in value.values():
            _check_json_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth + 1)


def _read_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    """Read a bounded regular file without accepting a symlink at its leaf."""

    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise OfficialFactualBenchmarkError(f"{label} is missing or inaccessible") from exc
    if stat.S_ISLNK(mode):
        raise OfficialFactualBenchmarkError(f"{label} must not be a symlink")
    if not stat.S_ISREG(mode):
        raise OfficialFactualBenchmarkError(f"{label} must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfficialFactualBenchmarkError(f"{label} is missing or inaccessible") from exc
    try:
        file_mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(file_mode):
            raise OfficialFactualBenchmarkError(f"{label} must be a regular file")
        size = os.fstat(descriptor).st_size
        if not 1 <= size <= maximum_bytes:
            raise OfficialFactualBenchmarkError(f"{label} violates byte cap of {maximum_bytes}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum_bytes + 1)
        if len(data) != size or len(data) > maximum_bytes:
            raise OfficialFactualBenchmarkError(f"{label} changed while being read or violates byte cap")
        return data
    except OSError as exc:
        raise OfficialFactualBenchmarkError(f"{label} could not be read") from exc
    finally:
        os.close(descriptor)


def _read_manifest(path: Path) -> dict[str, Any]:
    raw = _read_regular_file(path, maximum_bytes=MAX_MANIFEST_BYTES, label="manifest")
    try:
        loaded = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OfficialFactualBenchmarkError("manifest must be bounded UTF-8 JSON") from exc
    _check_json_depth(loaded)
    if not isinstance(loaded, dict):
        raise OfficialFactualBenchmarkError("manifest root must be a JSON object")
    return loaded


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    extra = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if extra or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise OfficialFactualBenchmarkError(f"{label} has " + "; ".join(details) + " fields")


def _string(value: Any, label: str, *, pattern: re.Pattern[str] | None = None, maximum: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise OfficialFactualBenchmarkError(f"{label} must be a non-empty string within {maximum} characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise OfficialFactualBenchmarkError(f"{label} has an invalid value")
    return value


def _sha256(value: Any, label: str) -> str:
    return _string(value, label, pattern=_SHA256, maximum=64)


def _action_tuple(value: Any, label: str) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise OfficialFactualBenchmarkError(f"{label} must be an action object")
    _exact_keys(value, {"date", "chamber", "description"}, label)
    raw_date = _string(value["date"], f"{label}.date", maximum=10)
    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise OfficialFactualBenchmarkError(f"{label}.date must be an exact ISO day") from exc
    if parsed_date.isoformat() != raw_date:
        raise OfficialFactualBenchmarkError(f"{label}.date must be an exact ISO day")
    chamber = value["chamber"]
    if chamber is not None and chamber not in ("House", "Senate"):
        raise OfficialFactualBenchmarkError(f"{label}.chamber must be House, Senate, or null")
    return {"date": raw_date, "chamber": chamber, "description": _string(value["description"], f"{label}.description")}


def _validate_fact(fact: Any, case_label: str) -> dict[str, Any]:
    if not isinstance(fact, dict):
        raise OfficialFactualBenchmarkError(f"{case_label} facts must be objects")
    _exact_keys(fact, {"field", "expected"}, f"{case_label} fact")
    field = _string(fact["field"], f"{case_label} fact field", maximum=64)
    if field not in _FACT_FIELDS:
        raise OfficialFactualBenchmarkError(f"{case_label} has unsupported fact field {field!r}")
    expected = fact["expected"]
    if field == "identity":
        if not isinstance(expected, dict):
            raise OfficialFactualBenchmarkError(f"{case_label} identity expected value must be an object")
        _exact_keys(expected, {"session_year", "bill_number", "bill_identifier"}, f"{case_label} identity")
        expected = {
            "session_year": _string(expected["session_year"], f"{case_label} identity.session_year", maximum=4),
            "bill_number": _string(expected["bill_number"], f"{case_label} identity.bill_number", maximum=16),
            "bill_identifier": _string(expected["bill_identifier"], f"{case_label} identity.bill_identifier", maximum=64),
        }
    elif field == "title":
        expected = _string(expected, f"{case_label} title expected value")
    elif field in {"table_row_count", "action_count"}:
        if isinstance(expected, bool) or not isinstance(expected, int) or not 0 <= expected <= 5_000:
            raise OfficialFactualBenchmarkError(f"{case_label} {field} expected value must be a bounded integer")
    else:
        expected = _action_tuple(expected, f"{case_label} {field} expected value")
    return {"field": field, "expected": expected}


def _safe_fixture_path(manifest_dir: Path, raw_path: Any) -> Path:
    value = _string(raw_path, "fixture.path", maximum=MAX_PATH_CHARS)
    if "\x00" in value:
        raise OfficialFactualBenchmarkError("fixture.path must not contain a NUL character")
    candidate_path = PurePath(value)
    if candidate_path.is_absolute() or any(part in {"", ".", ".."} for part in candidate_path.parts):
        raise OfficialFactualBenchmarkError("fixture.path must be a safe relative path inside the manifest directory")
    current = manifest_dir
    try:
        mode = current.lstat().st_mode
    except OSError as exc:
        raise OfficialFactualBenchmarkError("manifest directory is missing or inaccessible") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise OfficialFactualBenchmarkError("manifest directory must be a non-symlink directory")
    for position, part in enumerate(candidate_path.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise OfficialFactualBenchmarkError("fixture is missing or inaccessible") from exc
        if stat.S_ISLNK(mode):
            raise OfficialFactualBenchmarkError("fixture path must not contain a symlink")
        if position < len(candidate_path.parts) - 1 and not stat.S_ISDIR(mode):
            raise OfficialFactualBenchmarkError("fixture path intermediate component must be a directory")
    return current


def _validate_manifest(manifest: dict[str, Any], manifest_dir: Path) -> list[dict[str, Any]]:
    _exact_keys(manifest, {"schema_version", "benchmark_version", "cases"}, "manifest")
    schema_version = manifest["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != MANIFEST_SCHEMA_VERSION
    ):
        raise OfficialFactualBenchmarkError(f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}")
    if manifest["benchmark_version"] != RUNNER_VERSION:
        raise OfficialFactualBenchmarkError(f"manifest benchmark_version must be {RUNNER_VERSION!r}")
    cases = manifest["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise OfficialFactualBenchmarkError(f"manifest cases must contain 1 through {MAX_CASES} cases")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(cases, start=1):
        label = f"case {index}"
        if not isinstance(raw_case, dict):
            raise OfficialFactualBenchmarkError(f"{label} must be an object")
        _exact_keys(raw_case, {"case_id", "adapter", "jurisdiction", "source", "fixture", "facts"}, label)
        case_id = _string(raw_case["case_id"], f"{label}.case_id", pattern=_CASE_ID, maximum=96)
        if case_id in seen_ids:
            raise OfficialFactualBenchmarkError(f"manifest has duplicate case_id {case_id!r}")
        seen_ids.add(case_id)
        adapter = _string(raw_case["adapter"], f"{label}.adapter", maximum=64)
        if adapter != _ADAPTER:
            raise OfficialFactualBenchmarkError(f"{label} has unsupported adapter {adapter!r}")
        jurisdiction = _string(raw_case["jurisdiction"], f"{label}.jurisdiction", maximum=8)
        if jurisdiction != _JURISDICTION:
            raise OfficialFactualBenchmarkError(f"{label} has unsupported jurisdiction {jurisdiction!r}")
        source = raw_case["source"]
        if not isinstance(source, dict):
            raise OfficialFactualBenchmarkError(f"{label}.source must be an object")
        _exact_keys(source, {"url", "recorded_original_capture_sha256"}, f"{label}.source")
        fixture = raw_case["fixture"]
        if not isinstance(fixture, dict):
            raise OfficialFactualBenchmarkError(f"{label}.fixture must be an object")
        _exact_keys(fixture, {"path", "sha256", "kind"}, f"{label}.fixture")
        if fixture["kind"] != "derived_public_fixture":
            raise OfficialFactualBenchmarkError(f"{label}.fixture.kind must be 'derived_public_fixture'")
        facts = raw_case["facts"]
        if not isinstance(facts, list) or not 1 <= len(facts) <= MAX_FACTS_PER_CASE:
            raise OfficialFactualBenchmarkError(f"{label}.facts must contain 1 through {MAX_FACTS_PER_CASE} facts")
        result.append({
            "case_id": case_id,
            "adapter": adapter,
            "jurisdiction": jurisdiction,
            "source_url": _string(source["url"], f"{label}.source.url", maximum=1024),
            "recorded_original_capture_sha256": _sha256(source["recorded_original_capture_sha256"], f"{label}.source.recorded_original_capture_sha256"),
            "fixture_path": _safe_fixture_path(manifest_dir, fixture["path"]),
            "fixture_sha256": _sha256(fixture["sha256"], f"{label}.fixture.sha256"),
            "facts": [_validate_fact(fact, label) for fact in facts],
        })
    return result


def _parsed_action(action: fl_senate.FloridaSenateAction) -> dict[str, str | None]:
    return {"date": action.action_date.isoformat(), "chamber": action.chamber, "description": action.description}


def _fact_result(fact: dict[str, Any], parsed: fl_senate.ParsedFloridaSenateBillHistory) -> dict[str, Any]:
    field = fact["field"]
    expected = fact["expected"]
    if field == "identity":
        actual: Any = {"session_year": parsed.session_year, "bill_number": parsed.bill_number, "bill_identifier": parsed.bill_identifier}
    elif field == "title":
        actual = parsed.bill_title
    elif field == "table_row_count":
        actual = parsed.table_row_count
    elif field == "action_count":
        actual = len(parsed.actions)
    elif field == "final_action":
        actual = _parsed_action(parsed.actions[-1])
    else:
        found = next((action for action in parsed.actions if _parsed_action(action) == expected), None)
        actual = _parsed_action(found) if found is not None else None
    return {"field": field, "expected": expected, "actual": actual, "passed": actual == expected}


def _case_report(case: dict[str, Any]) -> dict[str, Any]:
    fixture_bytes = _read_regular_file(case["fixture_path"], maximum_bytes=MAX_FIXTURE_BYTES, label="fixture")
    actual_fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    source = {
        "url": case["source_url"],
        "derived_fixture_sha256": actual_fixture_sha256,
        "recorded_original_capture_sha256": case["recorded_original_capture_sha256"],
        "original_capture_verification": "not_verified_original_bytes_not_retained",
    }
    report: dict[str, Any] = {
        "case_id": case["case_id"],
        "adapter": case["adapter"],
        "jurisdiction": case["jurisdiction"],
        "source": source,
        "fixture": {"path": case["fixture_path"].name, "expected_sha256": case["fixture_sha256"], "actual_sha256": actual_fixture_sha256},
    }
    if actual_fixture_sha256 != case["fixture_sha256"]:
        report.update({"passed": False, "facts": [], "error": {"code": "fixture_sha256_mismatch", "message": "derived fixture bytes do not match the manifest hash"}})
        return report
    try:
        parsed = fl_senate.parse_florida_senate_bill_history(fixture_bytes, source_url=case["source_url"])
    except (fl_senate.OfficialFloridaSenateActionsError, TypeError) as exc:
        report.update({"passed": False, "facts": [], "error": {"code": "adapter_parse_failed", "message": str(exc)}})
        return report
    facts = [_fact_result(fact, parsed) for fact in case["facts"]]
    report.update({"passed": all(fact["passed"] for fact in facts), "facts": facts})
    return report


def _bounded_report(report: dict[str, Any]) -> dict[str, Any]:
    if len(_canonical_json(report)) > MAX_REPORT_BYTES:
        raise OfficialFactualBenchmarkError(f"benchmark report exceeds byte cap of {MAX_REPORT_BYTES}")
    return report


def run_official_factual_benchmark(manifest_path: str | Path) -> dict[str, Any]:
    """Run a valid manifest entirely from local retained fixture bytes."""

    path = Path(manifest_path)
    manifest = _read_manifest(path)
    cases = _validate_manifest(manifest, path.parent)
    results = [_case_report(case) for case in cases]
    passed = all(result["passed"] for result in results)
    report = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "interpretation": "This offline fixture regression checks directly asserted facts in retained derived official-source fixtures. A pass does not establish current-source freshness, agreement with a Bill Commons corpus, or that a nightly operational run occurred.",
        "coverage": {"jurisdictions": [_JURISDICTION], "jurisdiction_count": 1, "case_count": len(results), "adapters": [_ADAPTER]},
        "passed": passed,
        "summary": {"cases": len(results), "passed_cases": sum(result["passed"] for result in results), "failed_cases": sum(not result["passed"] for result in results)},
        "results": results,
    }
    return _bounded_report(report)


def _error_report(message: str) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "valid": False,
        "passed": False,
        "error": {"code": "invalid_manifest_or_fixture", "message": message[:512]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded offline official factual benchmark fixtures.")
    parser.add_argument("manifest", help="versioned JSON manifest; fixture paths are relative to its directory")
    arguments = parser.parse_args(argv)
    try:
        report = run_official_factual_benchmark(arguments.manifest)
        exit_code = 0 if report["passed"] else 1
    except OfficialFactualBenchmarkError as exc:
        report = _error_report(str(exc))
        exit_code = 2
    try:
        output = _canonical_json(report)
    except (TypeError, ValueError, RecursionError):
        output = _canonical_json(_error_report("benchmark could not encode a bounded JSON report"))
        exit_code = 2
    if len(output) > MAX_REPORT_BYTES:
        output = _canonical_json(_error_report("benchmark report exceeds byte cap"))
        exit_code = 2
    sys.stdout.buffer.write(output + b"\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
