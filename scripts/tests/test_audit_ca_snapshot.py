from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import audit_ca_snapshot as audit


def _row(*values: str) -> str:
    stream = io.StringIO()
    csv.writer(stream, delimiter="\t", quotechar="`", lineterminator="\n").writerow(values)
    return stream.getvalue()


def _bill(bill_id: str, *, latest: str, status: str = "Passed") -> str:
    return _row(
        bill_id, "20252026", "0", "AB", bill_id.removeprefix("202520260AB"), "Amended Assembly",
        "NULL", "NULL", "NULL", "NULL", latest, "Y", "SOURCE", "2026-09-01 12:00:00",
        "Enrollment", "E&E", "Assembly", status, "2026-02-01 00:00:00",
    )


def _version(version_id: str, bill_id: str, *, action: str = "Enrolled") -> str:
    return _row(
        version_id, bill_id, "92", "2026-09-01 00:00:00", action, "NULL", "Test subject",
        "Majority", "No", "Yes", "No", "NULL", "No", "No", "not-opened.lob", "Y", "SOURCE",
        "2026-09-01 12:00:00",
    )


def _history(bill_id: str, history_id: str, sequence: str, action: str, *, status: str = "Applied") -> str:
    return _row(
        bill_id, history_id, "2026-09-01 00:00:00", action, "SOURCE", "2026-09-01 12:00:00",
        sequence, "77", status, "Assembly", "E&E", "Enrollment", "Passed",
    )


def _analysis(bill_id: str, analysis_id: str) -> str:
    return _row(
        analysis_id, bill_id, "A", "FLOOR", "CZ01", "Assembly Floor", "Author",
        "2026-09-01 00:00:00", "2026-09-01 00:00:00", "1", "not-opened-analysis.lob", "Y", "Y",
        "SOURCE", "2026-09-01 12:00:00",
    )


def _zip(path: Path) -> None:
    first = "202520260AB1609"
    second = "202520260AB1610"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("BILL_TBL.dat", _bill(first, latest="20250AB160992ENR") + _bill(second, latest="20250AB161092ENR"))
        # Deliberately reverse the physical history order.  The audit must
        # select sequence 2, preserve the rescinded action, and never open LOBs.
        archive.writestr(
            "BILL_HISTORY_TBL.dat",
            _history(first, "2", "2", "Concurred in amendments.")
            + _history(first, "1", "1", "Prior action.", status="Rescinded")
            + _history(second, "3", "1", "Introduced.")
            + _history("202520260SB2510", "4", "1", "History-only."),
        )
        archive.writestr(
            "BILL_VERSION_TBL.dat",
            _version("20250AB160992ENR", first) + _version("20250AB161092ENR", second),
        )
        archive.writestr("BILL_ANALYSIS_TBL.dat", _analysis(first, "10") + _analysis(second, "11"))
        # If the tool opened this, parsing would not be necessary and this
        # test's audit contract would be violated.  Its content is irrelevant.
        archive.writestr("not-opened.lob", b"this LOB must not be read")
        archive.writestr("not-opened-analysis.lob", b"this analysis LOB must not be read")


def _manifest(path: Path, *bill_ids: str) -> None:
    rows = ["production_bill_id\tca_bill_id\tidentifier\n"]
    rows.extend(f"prod-{index}\t{bill_id}\tAB {1609 + index}\n" for index, bill_id in enumerate(bill_ids))
    path.write_text("".join(rows), encoding="utf-8")


def _source() -> audit.SourceIdentity:
    return audit.SourceIdentity(
        url="https://downloads.example.test/pubinfo_daily_Wed.zip",
        etag='"fixture"',
        last_modified="Wed, 02 Sep 2026 11:23:52 GMT",
        zip_sha256="fixture-sha256",
    )


def test_offline_audit_reports_set_differences_sorts_history_and_never_needs_lobs(tmp_path: Path, monkeypatch):
    zip_path = tmp_path / "official.zip"
    manifest_path = tmp_path / "production.tsv"
    _zip(zip_path)
    _manifest(manifest_path, "202520260AB1609")

    original_open = zipfile.ZipFile.open

    def refuse_lob(self, name, *args, **kwargs):
        if str(name).endswith(".lob"):
            raise AssertionError("the offline audit must never open a LOB")
        return original_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", refuse_lob)

    report = audit.audit_snapshot(
        zip_path=zip_path,
        manifest_path=manifest_path,
        source=_source(),
        probes=["202520260AB1609"],
        expected_probe_fields=[
            "202520260AB1609.current_status=Passed",
            "202520260AB1609.last_action_sequence=2",
            "202520260AB1609.last_action_status=Applied",
        ],
        expected_official_count=2,
        expected_production_count=1,
        expected_official_only_count=1,
        expected_production_only_count=0,
        expected_history_only_count=1,
    )

    assert report["source"]["lob_members_opened"] == 0
    assert report["counts"]["common_bills"] == 1
    assert report["history_only"] == ["202520260SB2510"]
    assert report["official_only"] == [{
        "ca_bill_id": "202520260AB1610", "measure_type": "AB", "measure_num": "1610",
        "measure_state": "Amended Assembly", "current_status": "Passed", "current_house": "Assembly",
        "current_location": "Enrollment", "current_secondary_loc": "E&E",
        "latest_bill_version_id": "20250AB161092ENR", "trans_update": "2026-09-01 12:00:00",
    }]
    assert report["probes"]["202520260AB1609"]["last_action"] == "Concurred in amendments."


def test_expected_count_and_probe_gates_fail_closed(tmp_path: Path):
    zip_path = tmp_path / "official.zip"
    manifest_path = tmp_path / "production.tsv"
    _zip(zip_path)
    _manifest(manifest_path, "202520260AB1609")

    with pytest.raises(audit.SnapshotAuditError, match="official-only bill count"):
        audit.audit_snapshot(
            zip_path=zip_path, manifest_path=manifest_path, source=_source(), expected_official_only_count=37
        )
    with pytest.raises(audit.SnapshotAuditError, match="probe 202520260AB1609.current_status"):
        audit.audit_snapshot(
            zip_path=zip_path,
            manifest_path=manifest_path,
            source=_source(),
            expected_probe_fields=["202520260AB1609.current_status=Enrolled"],
        )


def test_manifest_and_latest_version_integrity_fail_closed(tmp_path: Path):
    zip_path = tmp_path / "official.zip"
    manifest_path = tmp_path / "production.tsv"
    _zip(zip_path)
    _manifest(manifest_path, "202520260AB1609", "202520260AB1609")
    with pytest.raises(audit.SnapshotAuditError, match="duplicate ca_bill_id"):
        audit.audit_snapshot(zip_path=zip_path, manifest_path=manifest_path, source=_source())

    _manifest(manifest_path, "202520260AB1609")
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr("BILL_TBL.dat", _bill("202520260AB1609", latest="MISSING"))
    with pytest.raises(audit.SnapshotAuditError, match="latest_bill_version_id"):
        audit.audit_snapshot(zip_path=zip_path, manifest_path=manifest_path, source=_source())


def test_main_writes_pinned_json_report(tmp_path: Path):
    zip_path = tmp_path / "official.zip"
    manifest_path = tmp_path / "production.tsv"
    output_path = tmp_path / "report.json"
    _zip(zip_path)
    _manifest(manifest_path, "202520260AB1609")

    assert audit.main([
        "--zip", str(zip_path), "--production-manifest", str(manifest_path), "--output", str(output_path),
        "--source-url", "https://downloads.example.test/pubinfo_daily_Wed.zip",
        "--source-etag", '"fixture"', "--source-last-modified", "Wed, 02 Sep 2026 11:23:52 GMT",
        "--expected-official-count", "2", "--expected-production-count", "1",
        "--expected-official-only-count", "1", "--expected-production-only-count", "0",
        "--expected-history-only-count", "1", "--probe", "202520260AB1609",
    ]) == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["source"]["zip_sha256"] != "fixture-sha256"
    assert report["probes"]["202520260AB1609"]["latest_bill_version_id"] == "20250AB160992ENR"
