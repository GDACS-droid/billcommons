from __future__ import annotations

import csv
import hashlib
import io
import sys
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

scripts_directory = str(Path(__file__).resolve().parents[1])
if scripts_directory not in sys.path:
    sys.path.insert(0, scripts_directory)

import repair_ca_sb957_metadata as repair


def _row(*values: str) -> str:
    stream = io.StringIO()
    csv.writer(stream, delimiter="\t", quotechar="`", lineterminator="\n").writerow(values)
    return stream.getvalue()


def _bill(latest_version_id: str) -> str:
    return _row(
        repair.TARGET_OFFICIAL_BILL_ID,
        "20252026",
        "0",
        "SB",
        "957",
        "Amended Senate",
        "NULL",
        "NULL",
        "NULL",
        "NULL",
        latest_version_id,
        "Y",
        "SOURCE",
        "2026-08-31 00:00:00",
        "Assembly",
        "Floor",
        "Assembly",
        "Passed",
        "2026-08-01 00:00:00",
    )


def _version(version_id: str, *, subject: str, lob_filename: str) -> str:
    return _row(
        version_id,
        repair.TARGET_OFFICIAL_BILL_ID,
        "92",
        "2026-08-31 00:00:00",
        "Amended",
        "NULL",
        subject,
        "Majority",
        "No",
        "No",
        "No",
        "No",
        "No",
        "No",
        lob_filename,
        "Y",
        "SOURCE",
        "2026-08-31 00:00:00",
    )


def _write_zip(
    path: Path,
    *,
    subject: str = repair.EXPECTED_GENERAL_SUBJECT,
    general_subject: str = repair.EXPECTED_GENERAL_SUBJECT,
    digest: str = "An act relating to current civil detention facilities.",
    lob_filename: str = "SB_957_92_AMD.xml",
    duplicate_lob: bool = False,
) -> str:
    version_id = "20250SB95792AMD"
    xml = (
        "<Bill><GeneralSubject>"
        + general_subject
        + "</GeneralSubject><DigestText>"
        + digest
        + "</DigestText></Bill>"
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("BILL_TBL.dat", _bill(version_id))
        archive.writestr("BILL_VERSION_TBL.dat", _version(version_id, subject=subject, lob_filename=lob_filename))
        archive.writestr(lob_filename, xml)
        if duplicate_lob:
            archive.writestr(lob_filename, xml)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_loads_only_the_pinned_latest_sb957_metadata(tmp_path: Path):
    zip_path = tmp_path / "official.zip"
    expected_sha = _write_zip(zip_path)

    metadata = repair.load_official_metadata(zip_path, expected_sha)

    assert metadata.bill_id == repair.TARGET_OFFICIAL_BILL_ID
    assert metadata.version_id == "20250SB95792AMD"
    assert metadata.subject == repair.EXPECTED_GENERAL_SUBJECT
    assert metadata.digest == "An act relating to current civil detention facilities."


def test_rejects_wrong_hash_subject_shape_and_duplicate_lob(tmp_path: Path):
    zip_path = tmp_path / "official.zip"
    expected_sha = _write_zip(zip_path)
    with pytest.raises(repair.MetadataRepairError, match="64 hexadecimal"):
        repair.load_official_metadata(zip_path, "not-a-sha")
    with pytest.raises(repair.MetadataRepairError, match="SHA-256"):
        repair.load_official_metadata(zip_path, "0" * 64)

    bad_subject = tmp_path / "bad-subject.zip"
    bad_subject_sha = _write_zip(bad_subject, general_subject="Social media subpoenas")
    with pytest.raises(repair.MetadataRepairError, match="subject/title shape"):
        repair.load_official_metadata(bad_subject, bad_subject_sha)

    duplicate_lob = tmp_path / "duplicate-lob.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate_lob_sha = _write_zip(duplicate_lob, duplicate_lob=True)
    with pytest.raises(repair.MetadataRepairError, match="occur exactly once"):
        repair.load_official_metadata(duplicate_lob, duplicate_lob_sha)


def test_plan_is_idempotent_only_for_exact_digest_and_subject_set():
    official = repair.OfficialMetadata(
        bill_id=repair.TARGET_OFFICIAL_BILL_ID,
        version_id="20250SB95792AMD",
        subject=repair.EXPECTED_GENERAL_SUBJECT,
        digest="Current official digest",
        lob_filename="SB_957_92_AMD.xml",
    )
    current = repair.RepairPlan(
        local=repair.LocalMetadata(
            bill_id=uuid.uuid4(),
            title=repair.EXPECTED_GENERAL_SUBJECT,
            description="Current official digest",
            subjects=(repair.EXPECTED_GENERAL_SUBJECT,),
        ),
        official=official,
    )
    stale = repair.RepairPlan(
        local=repair.LocalMetadata(
            bill_id=uuid.uuid4(),
            title=repair.EXPECTED_GENERAL_SUBJECT,
            description="Old social-media digest",
            subjects=("Social media", "Administrative subpoenas"),
        ),
        official=official,
    )
    assert current.changed is False
    assert stale.changed is True


def test_local_title_guard_allows_only_the_known_terminal_period_difference():
    assert repair._title_matches_official_subject(
        "Civil detention facilities", repair.EXPECTED_GENERAL_SUBJECT
    )
    assert repair._title_matches_official_subject(
        "Civil detention facilities.", repair.EXPECTED_GENERAL_SUBJECT
    )
    assert not repair._title_matches_official_subject(
        "Social media administrative subpoenas", repair.EXPECTED_GENERAL_SUBJECT
    )


class _Result:
    def __init__(self, *, rows=None, scalar=None, scalars=None, rowcount=None):
        self._rows = rows or []
        self._scalar = scalar
        self._scalars = scalars or []
        self.rowcount = rowcount

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self._scalars


class _ApplyDb:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.flush_count = 0

    def execute(self, _statement, *_args):
        return self.results.pop(0)

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flush_count += 1


def test_apply_replaces_only_description_and_subjects_with_exact_rowcount():
    bill = SimpleNamespace(
        id=uuid.uuid4(),
        title=repair.EXPECTED_GENERAL_SUBJECT,
        description="Old social-media digest",
    )
    official = repair.OfficialMetadata(
        bill_id=repair.TARGET_OFFICIAL_BILL_ID,
        version_id="20250SB95792AMD",
        subject=repair.EXPECTED_GENERAL_SUBJECT,
        digest="Current official digest",
        lob_filename="SB_957_92_AMD.xml",
    )
    plan = repair.RepairPlan(
        local=repair.LocalMetadata(
            bill_id=bill.id,
            title=bill.title,
            description=bill.description,
            subjects=("Administrative subpoenas", "Social media"),
        ),
        official=official,
    )
    db = _ApplyDb(
        [
            _Result(scalar=bill),
            _Result(scalars=list(plan.local.subjects)),
            _Result(rowcount=2),
            _Result(scalars=list(plan.desired_subjects)),
        ]
    )

    repair._apply_locked_plan(db, plan)

    assert bill.description == official.digest
    assert db.flush_count == 1
    assert [row.subject for row in db.added] == [official.subject]
    assert all(row.bill_id == bill.id for row in db.added)


def test_apply_fails_closed_when_subject_delete_rowcount_changes():
    bill = SimpleNamespace(
        id=uuid.uuid4(),
        title=repair.EXPECTED_GENERAL_SUBJECT,
        description="Old social-media digest",
    )
    official = repair.OfficialMetadata(
        bill_id=repair.TARGET_OFFICIAL_BILL_ID,
        version_id="20250SB95792AMD",
        subject=repair.EXPECTED_GENERAL_SUBJECT,
        digest="Current official digest",
        lob_filename="SB_957_92_AMD.xml",
    )
    plan = repair.RepairPlan(
        local=repair.LocalMetadata(
            bill_id=bill.id,
            title=bill.title,
            description=bill.description,
            subjects=("Social media",),
        ),
        official=official,
    )
    db = _ApplyDb(
        [
            _Result(scalar=bill),
            _Result(scalars=list(plan.local.subjects)),
            _Result(rowcount=0),
        ]
    )

    with pytest.raises(repair.MetadataRepairError, match="rowcount"):
        repair._apply_locked_plan(db, plan)


def test_dry_run_rolls_back_without_apply(monkeypatch, tmp_path: Path):
    zip_path = tmp_path / "official.zip"
    expected_sha = _write_zip(zip_path)
    official = repair.load_official_metadata(zip_path, expected_sha)
    plan = repair.RepairPlan(
        local=repair.LocalMetadata(
            bill_id=uuid.uuid4(),
            title=official.subject,
            description="Old digest",
            subjects=("Old subject",),
        ),
        official=official,
    )

    class DryDb:
        def __init__(self):
            self.rollback_count = 0
            self.commit_count = 0
            self.closed = False

        def execute(self, *_args, **_kwargs):
            return _Result()

        def rollback(self):
            self.rollback_count += 1

        def commit(self):
            self.commit_count += 1

        def close(self):
            self.closed = True

    db = DryDb()
    monkeypatch.setattr(repair, "_load_locked_plan", lambda _db, _official: plan)
    result = repair.run(
        zip_path=zip_path,
        expected_sha256=expected_sha,
        apply=False,
        session_factory=lambda: db,
    )
    assert result["applied"] is False
    assert result["changed"] is True
    assert db.rollback_count == 1
    assert db.commit_count == 0
    assert db.closed is True
