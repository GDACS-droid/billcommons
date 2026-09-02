from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest


scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import export_ca_post_sweep_manifest as exporter


def test_write_manifest_is_deterministic_and_refuses_to_replace_evidence(tmp_path: Path):
    output = tmp_path / "ca-post.tsv"
    rows = [
        exporter.ManifestRow("b2", "202520260AB2", "regular", "AB 2", "passed_both", "2026-08-31", "Two", "id2", "https://example/2"),
        exporter.ManifestRow("b1", "202520260AB1", "regular", "AB 1", "introduced", "", "", "id1", "https://example/1"),
    ]

    exporter.write_manifest(output, sorted(rows, key=lambda row: row.ca_bill_id))
    with output.open(newline="", encoding="utf-8") as stream:
        parsed = list(csv.DictReader(stream, delimiter="\t"))
    assert [row["ca_bill_id"] for row in parsed] == ["202520260AB1", "202520260AB2"]
    assert list(parsed[0]) == list(exporter.FIELDNAMES)

    with pytest.raises(exporter.ManifestExportError, match="refusing to overwrite"):
        exporter.write_manifest(output, rows)


def test_official_bill_id_requires_exact_session_identity():
    class Session:
        identifier = exporter.REGULAR_SESSION_IDENTIFIER
        classification = "regular"

    class Bill:
        id = "local"
        identifier = "AB 1609"

    assert exporter.official_bill_id(Session(), Bill()) == "202520260AB1609"
    Session.identifier = "2025-2026 Special Session 2"
    Session.classification = "special"
    with pytest.raises(exporter.ManifestExportError, match="unexpected CA session"):
        exporter.official_bill_id(Session(), Bill())
