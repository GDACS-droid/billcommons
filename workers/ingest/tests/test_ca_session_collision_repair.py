"""Focused contracts for the fail-closed CA session-collision repair."""
from __future__ import annotations

import csv
import io
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from billcommons_ingest import ca_session_collision_repair as repair


def _patched_hashes(monkeypatch, zip_path: Path, manifest_path: Path) -> None:
    monkeypatch.setattr(repair, "OFFICIAL_ZIP_SHA256", repair._sha256_file(zip_path))
    monkeypatch.setattr(repair, "MANIFEST_SHA256", repair._sha256_file(manifest_path))


def _zip(path: Path) -> tuple[list[str], list[str]]:
    rows = []
    special = [f"AB {n}" for n in range(1, 25)]
    regular = special + [f"SB {n}" for n in range(100, 113)]
    for identifier in regular:
        typ, number = identifier.split()
        rows.append([f"202520260{typ}{number}", "20252026", "0", typ, number] + [""] * 14)
    for identifier in special:
        typ, number = identifier.split()
        rows.append([f"202520261{typ}{number}", "20252026", "1", typ, number] + [""] * 14)
    with zipfile.ZipFile(path, "w") as zf:
        out = io.StringIO(newline="")
        writer = csv.writer(out, delimiter="\t", quotechar="`", lineterminator="\n")
        writer.writerows(rows)
        zf.writestr("BILL_TBL.dat", out.getvalue())
    return special, regular


def _manifest(path: Path, special: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=["production_bill_id", "ca_bill_id", "identifier_norm", "openstates_id"], delimiter="\t")
        writer.writeheader()
        for index, identifier in enumerate(special, 1):
            typ, number = identifier.split()
            writer.writerow({"production_bill_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identifier)), "ca_bill_id": f"202520261{typ}{number}", "identifier_norm": identifier, "openstates_id": f"{repair.SPECIAL_OPENSTATES_SESSION}:{identifier}"})


class _Client:
    def search_bills(self, *, session, identifier, **kwargs):
        return {"results": [self.get_bill(f"{session}:{identifier}", include=kwargs.get("include"))], "pagination": {"max_page": 1}}

    def get_bill(self, openstates_id, *, include):
        session, identifier = openstates_id.split(":", 1)
        return {"id": openstates_id, "session": session, "identifier": identifier, "title": f"Title {identifier}", "classification": ["bill"], "sources": [{"url": "https://example.gov/bill"}], "actions": [{"description": "Introduced", "date": "2026-01-01", "classification": ["introduction"], "order": 1}], "sponsorships": [{"name": "Jane Doe", "classification": "primary", "primary": True}], "versions": [{"id": f"v-{openstates_id}", "note": "Introduced", "date": "2026-01-01", "links": [{"url": "https://example.gov/text.pdf", "media_type": "application/pdf"}]}], "documents": [], "subject": ["Civic data"], "votes": [], "abstracts": [{"abstract": "Verified summary"}], "other_identifiers": [], "related_bills": []}


def test_scope_is_derived_from_pinned_inputs_and_rejects_count_drift(tmp_path, monkeypatch):
    zip_path, manifest_path = tmp_path / "official.zip", tmp_path / "manifest.tsv"
    special, _ = _zip(zip_path)
    _manifest(manifest_path, special)
    _patched_hashes(monkeypatch, zip_path, manifest_path)
    scope = repair.derive_scope(zip_path=zip_path, manifest_path=manifest_path)
    assert len(scope.contaminated_regular) == 24
    assert len(scope.special) == 24
    assert len(scope.missing_regular) == 13
    assert scope.total == 61
    assert len(scope.retained_uuid_by_identifier) == 24
    manifest_path.write_text("wrong", encoding="utf-8")
    with pytest.raises(repair.CollisionRepairError, match="SHA-256"):
        repair.derive_scope(zip_path=zip_path, manifest_path=manifest_path)


def test_stage_requires_complete_unambiguous_payloads(tmp_path, monkeypatch):
    zip_path, manifest_path = tmp_path / "official.zip", tmp_path / "manifest.tsv"
    special, _ = _zip(zip_path)
    _manifest(manifest_path, special)
    _patched_hashes(monkeypatch, zip_path, manifest_path)
    plan = repair.stage_repair(_Client(), repair.derive_scope(zip_path=zip_path, manifest_path=manifest_path))
    assert len(plan.bills) == 61
    assert {bill.kind for bill in plan.bills} == {"retained_regular", "missing_regular", "special"}

    bundle = tmp_path / "staged.json"
    digest = repair.save_repair_plan(plan, bundle)
    assert len(digest) == 64
    assert bundle.stat().st_mode & 0o077 == 0
    loaded = repair.load_repair_plan(bundle, plan.scope)
    assert [(item.kind, item.measure.ca_bill_id, item.payload["id"]) for item in loaded.bills] == [
        (item.kind, item.measure.ca_bill_id, item.payload["id"]) for item in plan.bills
    ]
    with pytest.raises(repair.CollisionRepairError, match="overwrite"):
        repair.save_repair_plan(plan, bundle)


def test_stage_rejects_multipage_identity_lookup(tmp_path, monkeypatch):
    zip_path, manifest_path = tmp_path / "official.zip", tmp_path / "manifest.tsv"
    special, _ = _zip(zip_path)
    _manifest(manifest_path, special)
    _patched_hashes(monkeypatch, zip_path, manifest_path)

    class Multipage(_Client):
        def search_bills(self, **kwargs):
            result = super().search_bills(**kwargs)
            result["pagination"]["max_page"] = 2
            return result

    with pytest.raises(repair.CollisionRepairError, match="single result page"):
        repair.stage_repair(Multipage(), repair.derive_scope(zip_path=zip_path, manifest_path=manifest_path))


def test_stage_rejects_missing_source_or_subject_contract(tmp_path, monkeypatch):
    zip_path, manifest_path = tmp_path / "official.zip", tmp_path / "manifest.tsv"
    special, _ = _zip(zip_path)
    _manifest(manifest_path, special)
    _patched_hashes(monkeypatch, zip_path, manifest_path)

    class MissingSource(_Client):
        def get_bill(self, *args, **kwargs):
            payload = super().get_bill(*args, **kwargs)
            payload["sources"] = []
            return payload

    with pytest.raises(repair.CollisionRepairError, match="source URL"):
        repair.stage_repair(MissingSource(), repair.derive_scope(zip_path=zip_path, manifest_path=manifest_path))


def test_stage_accepts_frozen_row_already_rekeyed_to_exact_regular_id(tmp_path, monkeypatch):
    zip_path, manifest_path = tmp_path / "official.zip", tmp_path / "manifest.tsv"
    special, _ = _zip(zip_path)
    _manifest(manifest_path, special)
    _patched_hashes(monkeypatch, zip_path, manifest_path)
    scope = repair.derive_scope(zip_path=zip_path, manifest_path=manifest_path)
    first_identifier, first_uuid, _old_special_id = scope.retained_regular_identities[0]
    identities = list(scope.retained_regular_identities)
    identities[0] = (
        first_identifier,
        first_uuid,
        f"{repair.REGULAR_OPENSTATES_SESSION}:{first_identifier}",
    )
    scope = replace(scope, retained_regular_identities=tuple(identities))

    plan = repair.stage_repair(_Client(), scope)

    assert len(plan.bills) == 61


def test_apply_rejects_incomplete_plan_before_opening_a_database_transaction(tmp_path, monkeypatch):
    zip_path, manifest_path = tmp_path / "official.zip", tmp_path / "manifest.tsv"
    special, _ = _zip(zip_path)
    _manifest(manifest_path, special)
    _patched_hashes(monkeypatch, zip_path, manifest_path)
    scope = repair.derive_scope(zip_path=zip_path, manifest_path=manifest_path)
    with pytest.raises(repair.CollisionRepairError, match="staged count"):
        repair.apply_repair(None, repair.RepairPlan(scope=scope, bills=()))


def test_subject_and_vote_count_shapes_match_openstates_contract():
    assert repair._subjects({"subject": ["Budget", {"name": "Insurance"}]}) == ["Budget", "Insurance"]
    assert repair._vote_tally({"counts": [{"option": "yes", "value": 7}, {"option": "no", "value": "2"}]}, []) == {"yes": 7, "no": 2, "other": 0}
    assert repair._normalise_alternate_identifier("ocd-bill-ca-20252026-ab1") == "OCD-BILL-CA-20252026-AB1"
