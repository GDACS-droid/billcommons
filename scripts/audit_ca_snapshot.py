#!/usr/bin/env python3
"""Reconcile a frozen Bill Commons CA universe with an official CA bulk zip.

This is intentionally an offline, read-only audit tool.  It never opens a
database, performs network I/O, or opens any ``.lob`` member.  The caller
first pins an official archive (URL, ETag, Last-Modified value, and local
SHA-256) and separately exports a frozen production manifest containing the
CA ``bill_id=`` values already present in Bill Commons document URLs.

Only these four tab-delimited archive members are read:

* ``BILL_TBL.dat`` -- authoritative current bill state;
* ``BILL_HISTORY_TBL.dat`` -- action ledger;
* ``BILL_VERSION_TBL.dat`` -- version metadata and the current-version join;
* ``BILL_ANALYSIS_TBL.dat`` -- official-analysis metadata only.

The report deliberately distinguishes a measure that is official-only from a
production-only measure.  It does not treat either set as authorization to
write, delete, crawl, or otherwise change production state.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BILL_COLUMNS = (
    "bill_id", "session_year", "session_num", "measure_type", "measure_num",
    "measure_state", "chapter_year", "chapter_type", "chapter_session_num",
    "chapter_num", "latest_bill_version_id", "active_flg", "trans_uid",
    "trans_update", "current_location", "current_secondary_loc", "current_house",
    "current_status", "days_31st_in_print",
)
HISTORY_COLUMNS = (
    "bill_id", "bill_history_id", "action_date", "action", "trans_uid",
    "trans_update_dt", "action_sequence", "action_code", "action_status",
    "primary_location", "secondary_location", "ternary_location", "end_status",
)
VERSION_COLUMNS = (
    "bill_version_id", "bill_id", "version_num", "bill_version_action_date",
    "bill_version_action", "request_num", "subject", "vote_required",
    "appropriation", "fiscal_committee", "local_program", "substantive_changes",
    "urgency", "taxlevy", "bill_xml_lob_filename", "active_flg", "trans_uid",
    "trans_update",
)
ANALYSIS_COLUMNS = (
    "analysis_id", "bill_id", "house", "analysis_type", "committee_code",
    "committee_name", "amendment_author", "analysis_date", "amendment_date",
    "page_num", "source_doc_lob_filename", "released_floor", "active_flg",
    "trans_uid", "trans_update",
)

REQUIRED_MEMBERS = {
    "BILL_TBL.dat": BILL_COLUMNS,
    "BILL_HISTORY_TBL.dat": HISTORY_COLUMNS,
    "BILL_VERSION_TBL.dat": VERSION_COLUMNS,
    "BILL_ANALYSIS_TBL.dat": ANALYSIS_COLUMNS,
}


class SnapshotAuditError(RuntimeError):
    """An input or invariant failure safe to show an operator."""


@dataclass(frozen=True)
class SourceIdentity:
    url: str
    etag: str
    last_modified: str
    zip_sha256: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_text(value: str | None, name: str) -> str:
    result = (value or "").strip()
    if not result:
        raise SnapshotAuditError(f"{name} must be nonempty")
    return result


def _read_member_rows(archive: zipfile.ZipFile, name: str, columns: Sequence[str]) -> list[dict[str, str]]:
    """Stream one allowed CA TSV member and fail closed on a malformed row."""
    try:
        member = archive.open(name)
    except KeyError as exc:
        raise SnapshotAuditError(f"official zip is missing required member {name}") from exc
    rows: list[dict[str, str]] = []
    with member, io.TextIOWrapper(member, encoding="utf-8", errors="strict", newline="") as text:
        reader = csv.reader(text, delimiter="\t", quotechar="`", doublequote=True)
        for line_number, fields in enumerate(reader, start=1):
            if not fields:
                continue
            if len(fields) != len(columns):
                raise SnapshotAuditError(
                    f"{name}:{line_number} has {len(fields)} fields; expected {len(columns)}"
                )
            rows.append(dict(zip(columns, fields, strict=True)))
    return rows


def _integer_sort_key(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return -1


def _history_sort_key(row: Mapping[str, str]) -> tuple[int, str, str]:
    # CA's physical file order is not bill-history order.  The final ID makes
    # same-sequence/same-date rows deterministic without erasing either one.
    return (_integer_sort_key(row["action_sequence"]), row["action_date"], row["bill_history_id"])


def _select(row: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    return {name: row[name] for name in names}


_BILL_DIGEST_FIELDS = (
    "bill_id", "session_year", "session_num", "measure_type", "measure_num",
    "measure_state", "chapter_year", "chapter_type", "chapter_session_num",
    "chapter_num", "latest_bill_version_id", "active_flg", "trans_update",
    "current_location", "current_secondary_loc", "current_house", "current_status",
    "days_31st_in_print",
)
_HISTORY_DIGEST_FIELDS = (
    "bill_history_id", "action_date", "action", "trans_update_dt", "action_sequence",
    "action_code", "action_status", "primary_location", "secondary_location",
    "ternary_location", "end_status",
)
_VERSION_DIGEST_FIELDS = (
    "bill_version_id", "version_num", "bill_version_action_date", "bill_version_action",
    "subject", "vote_required", "appropriation", "fiscal_committee", "local_program",
    "substantive_changes", "urgency", "taxlevy", "bill_xml_lob_filename", "active_flg",
    "trans_update",
)
_ANALYSIS_DIGEST_FIELDS = (
    "analysis_id", "house", "analysis_type", "committee_code", "committee_name",
    "amendment_author", "analysis_date", "amendment_date", "page_num",
    "source_doc_lob_filename", "released_floor", "active_flg", "trans_update",
)


def _read_manifest(path: Path) -> dict[str, dict[str, str]]:
    """Read a frozen TSV manifest and require a one-to-one CA bill-id map."""
    try:
        source = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise SnapshotAuditError("could not read production manifest") from exc
    with source:
        reader = csv.DictReader(source, delimiter="\t")
        if not reader.fieldnames or "ca_bill_id" not in reader.fieldnames:
            raise SnapshotAuditError("production manifest must have a ca_bill_id TSV column")
        rows: dict[str, dict[str, str]] = {}
        for line_number, row in enumerate(reader, start=2):
            bill_id = (row.get("ca_bill_id") or "").strip()
            if not bill_id:
                raise SnapshotAuditError(f"production manifest:{line_number} has an empty ca_bill_id")
            if bill_id in rows:
                raise SnapshotAuditError(f"production manifest has duplicate ca_bill_id {bill_id}")
            rows[bill_id] = {key: value or "" for key, value in row.items() if key is not None}
    return rows


def _parse_expected_probe(value: str) -> tuple[str, str, str]:
    """Parse ``BILL_ID.FIELD=VALUE`` without a lossy delimiter split."""
    if "=" not in value or "." not in value.split("=", 1)[0]:
        raise SnapshotAuditError("--expect-probe must be BILL_ID.FIELD=VALUE")
    left, expected = value.split("=", 1)
    bill_id, field = left.rsplit(".", 1)
    if not bill_id or not field:
        raise SnapshotAuditError("--expect-probe must name a bill ID and field")
    return bill_id, field, expected


def _check_expected(actual: int, expected: int | None, name: str) -> None:
    if expected is not None and actual != expected:
        raise SnapshotAuditError(f"{name} was {actual}; expected {expected}")


def audit_snapshot(
    *,
    zip_path: Path,
    manifest_path: Path,
    source: SourceIdentity,
    probes: Sequence[str] = (),
    expected_probe_fields: Sequence[str] = (),
    expected_official_count: int | None = None,
    expected_production_count: int | None = None,
    expected_official_only_count: int | None = None,
    expected_production_only_count: int | None = None,
    expected_history_only_count: int | None = None,
) -> dict[str, Any]:
    """Return a deterministic offline reconciliation report or fail closed.

    Expected counts and expected probe fields are opt-in gates.  Thus the
    executable has no hidden dependency on one particular session's counts.
    """
    if not zip_path.is_file():
        raise SnapshotAuditError("--zip must name a readable local ZIP file")
    production = _read_manifest(manifest_path)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            missing = sorted(set(REQUIRED_MEMBERS) - names)
            if missing:
                raise SnapshotAuditError(f"official zip is missing required members: {', '.join(missing)}")
            # This is the only archive access.  No .lob name is ever opened.
            bill_rows = _read_member_rows(archive, "BILL_TBL.dat", BILL_COLUMNS)
            history_rows = _read_member_rows(archive, "BILL_HISTORY_TBL.dat", HISTORY_COLUMNS)
            version_rows = _read_member_rows(archive, "BILL_VERSION_TBL.dat", VERSION_COLUMNS)
            analysis_rows = _read_member_rows(archive, "BILL_ANALYSIS_TBL.dat", ANALYSIS_COLUMNS)
    except zipfile.BadZipFile as exc:
        raise SnapshotAuditError("--zip is not a readable ZIP archive") from exc

    bills: dict[str, dict[str, str]] = {}
    for row in bill_rows:
        bill_id = row["bill_id"]
        if not bill_id:
            raise SnapshotAuditError("BILL_TBL.dat contains an empty bill_id")
        if bill_id in bills:
            raise SnapshotAuditError(f"BILL_TBL.dat has duplicate bill_id {bill_id}")
        bills[bill_id] = row

    versions: dict[str, dict[str, str]] = {}
    versions_by_bill: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in version_rows:
        version_id = row["bill_version_id"]
        if not version_id or not row["bill_id"]:
            raise SnapshotAuditError("BILL_VERSION_TBL.dat contains an empty primary or bill ID")
        if version_id in versions:
            raise SnapshotAuditError(f"BILL_VERSION_TBL.dat has duplicate bill_version_id {version_id}")
        versions[version_id] = row
        versions_by_bill[row["bill_id"]].append(row)

    history_by_bill: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in history_rows:
        if not row["bill_id"] or not row["bill_history_id"]:
            raise SnapshotAuditError("BILL_HISTORY_TBL.dat contains an empty bill or history ID")
        history_by_bill[row["bill_id"]].append(row)

    analyses_by_bill: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in analysis_rows:
        if not row["bill_id"] or not row["analysis_id"]:
            raise SnapshotAuditError("BILL_ANALYSIS_TBL.dat contains an empty bill or analysis ID")
        analyses_by_bill[row["bill_id"]].append(row)

    version_join_failures: list[str] = []
    bill_records: dict[str, dict[str, Any]] = {}
    for bill_id, bill in bills.items():
        latest_version_id = bill["latest_bill_version_id"]
        latest_version = versions.get(latest_version_id)
        if latest_version is None or latest_version["bill_id"] != bill_id:
            version_join_failures.append(bill_id)
            continue
        ordered_history = sorted(history_by_bill[bill_id], key=_history_sort_key)
        ordered_versions = sorted(
            versions_by_bill[bill_id],
            key=lambda row: (row["bill_version_action_date"], row["bill_version_id"]),
        )
        ordered_analyses = sorted(
            analyses_by_bill[bill_id], key=lambda row: (row["analysis_date"], row["analysis_id"])
        )
        bill_records[bill_id] = {
            "bill_digest": _digest(_select(bill, _BILL_DIGEST_FIELDS)),
            "history_digest": _digest([_select(row, _HISTORY_DIGEST_FIELDS) for row in ordered_history]),
            "version_digest": _digest([_select(row, _VERSION_DIGEST_FIELDS) for row in ordered_versions]),
            "analysis_metadata_digest": _digest([_select(row, _ANALYSIS_DIGEST_FIELDS) for row in ordered_analyses]),
            "latest_version": _select(latest_version, _VERSION_DIGEST_FIELDS),
            "last_history": _select(ordered_history[-1], _HISTORY_DIGEST_FIELDS) if ordered_history else None,
        }
    if version_join_failures:
        raise SnapshotAuditError(
            "BILL_TBL latest_bill_version_id does not join to BILL_VERSION_TBL for "
            + ", ".join(sorted(version_join_failures)[:10])
        )

    official_ids = set(bills)
    production_ids = set(production)
    common_ids = official_ids & production_ids
    official_only_ids = sorted(official_ids - production_ids)
    production_only_ids = sorted(production_ids - official_ids)
    history_only_ids = sorted(set(history_by_bill) - official_ids)
    version_only_ids = sorted(set(versions_by_bill) - official_ids)

    _check_expected(len(official_ids), expected_official_count, "official bill count")
    _check_expected(len(production_ids), expected_production_count, "production bill count")
    _check_expected(len(official_only_ids), expected_official_only_count, "official-only bill count")
    _check_expected(len(production_only_ids), expected_production_only_count, "production-only bill count")
    _check_expected(len(history_only_ids), expected_history_only_count, "history-only bill count")

    probe_ids = set(probes)
    parsed_expectations = [_parse_expected_probe(item) for item in expected_probe_fields]
    probe_ids.update(bill_id for bill_id, _field, _value in parsed_expectations)
    probe_report: dict[str, dict[str, Any]] = {}
    for bill_id in sorted(probe_ids):
        if bill_id not in bills:
            raise SnapshotAuditError(f"probe bill_id is absent from BILL_TBL.dat: {bill_id}")
        record = bill_records[bill_id]
        bill = bills[bill_id]
        last_history = record["last_history"] or {}
        probe_report[bill_id] = {
            "current_status": bill["current_status"],
            "current_house": bill["current_house"],
            "current_location": bill["current_location"],
            "current_secondary_loc": bill["current_secondary_loc"],
            "latest_bill_version_id": bill["latest_bill_version_id"],
            "measure_state": bill["measure_state"],
            "trans_update": bill["trans_update"],
            "last_action_sequence": last_history.get("action_sequence"),
            "last_action_date": last_history.get("action_date"),
            "last_action": last_history.get("action"),
            "last_action_status": last_history.get("action_status"),
            "last_end_status": last_history.get("end_status"),
            "latest_version_action": record["latest_version"]["bill_version_action"],
            "latest_version_action_date": record["latest_version"]["bill_version_action_date"],
            "latest_version_subject": record["latest_version"]["subject"],
            "digest": _digest({"bill": _select(bill, _BILL_DIGEST_FIELDS), "record": record}),
        }
    for bill_id, field, expected in parsed_expectations:
        actual = probe_report[bill_id].get(field)
        if actual is None:
            raise SnapshotAuditError(f"--expect-probe uses unknown field {field!r} for {bill_id}")
        if actual != expected:
            raise SnapshotAuditError(
                f"probe {bill_id}.{field} was {actual!r}; expected {expected!r}"
            )

    official_only = [
        {
            "ca_bill_id": bill_id,
            "measure_type": bills[bill_id]["measure_type"],
            "measure_num": bills[bill_id]["measure_num"],
            "measure_state": bills[bill_id]["measure_state"],
            "current_status": bills[bill_id]["current_status"],
            "current_house": bills[bill_id]["current_house"],
            "current_location": bills[bill_id]["current_location"],
            "current_secondary_loc": bills[bill_id]["current_secondary_loc"],
            "latest_bill_version_id": bills[bill_id]["latest_bill_version_id"],
            "trans_update": bills[bill_id]["trans_update"],
        }
        for bill_id in official_only_ids
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "url": source.url,
            "etag": source.etag,
            "last_modified": source.last_modified,
            "zip_sha256": source.zip_sha256,
            "tables_read": sorted(REQUIRED_MEMBERS),
            "lob_members_opened": 0,
        },
        "counts": {
            "official_bills": len(official_ids),
            "production_bills": len(production_ids),
            "common_bills": len(common_ids),
            "official_only_bills": len(official_only_ids),
            "production_only_bills": len(production_only_ids),
            "history_only_bill_ids": len(history_only_ids),
            "version_only_bill_ids": len(version_only_ids),
            "official_versions": len(version_rows),
            "official_history_actions": len(history_rows),
            "official_analyses": len(analysis_rows),
        },
        "digests": {
            "production_manifest": _digest([production[bill_id] for bill_id in sorted(production)]),
            "official_bill_state": _digest({bill_id: bill_records[bill_id]["bill_digest"] for bill_id in sorted(bill_records)}),
            "official_history": _digest({bill_id: bill_records[bill_id]["history_digest"] for bill_id in sorted(bill_records)}),
            "official_versions": _digest({bill_id: bill_records[bill_id]["version_digest"] for bill_id in sorted(bill_records)}),
            "official_analysis_metadata": _digest({bill_id: bill_records[bill_id]["analysis_metadata_digest"] for bill_id in sorted(bill_records)}),
            "common_ca_bill_ids": _digest(sorted(common_ids)),
        },
        "official_only": official_only,
        "production_only": production_only_ids,
        "history_only": history_only_ids,
        "version_only": version_only_ids,
        "probes": probe_report,
    }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True, type=Path, help="pinned local official CA ZIP")
    parser.add_argument("--production-manifest", required=True, type=Path, help="frozen TSV with ca_bill_id column")
    parser.add_argument("--output", required=True, type=Path, help="JSON report path")
    parser.add_argument("--source-url", required=True, help="exact download URL used for --zip")
    parser.add_argument("--source-etag", required=True, help="HTTP ETag captured for --zip")
    parser.add_argument("--source-last-modified", required=True, help="HTTP Last-Modified captured for --zip")
    parser.add_argument("--probe", action="append", default=[], metavar="BILL_ID", help="include and validate a named official bill")
    parser.add_argument(
        "--expect-probe", action="append", default=[], metavar="BILL_ID.FIELD=VALUE",
        help="gate a selected probe field; may be repeated",
    )
    parser.add_argument("--expected-official-count", type=int)
    parser.add_argument("--expected-production-count", type=int)
    parser.add_argument("--expected-official-only-count", type=int)
    parser.add_argument("--expected-production-only-count", type=int)
    parser.add_argument("--expected-history-only-count", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source = SourceIdentity(
            url=_required_text(args.source_url, "--source-url"),
            etag=_required_text(args.source_etag, "--source-etag"),
            last_modified=_required_text(args.source_last_modified, "--source-last-modified"),
            zip_sha256=_file_sha256(args.zip),
        )
        report = audit_snapshot(
            zip_path=args.zip,
            manifest_path=args.production_manifest,
            source=source,
            probes=args.probe,
            expected_probe_fields=args.expect_probe,
            expected_official_count=args.expected_official_count,
            expected_production_count=args.expected_production_count,
            expected_official_only_count=args.expected_official_only_count,
            expected_production_only_count=args.expected_production_only_count,
            expected_history_only_count=args.expected_history_only_count,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except SnapshotAuditError as exc:
        print(f"CA snapshot audit failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    raise SystemExit(main())
