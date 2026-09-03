#!/usr/bin/env python3
"""Repair CA SB 957's gut-and-amend digest/subjects from a pinned CA ZIP.

This is intentionally a one-bill repair, not an alternate ingestion path.  It
accepts only the 2025--2026 regular-session CA official bill ID
``202520260SB957`` and refuses to write unless the local bill title and the
official latest-version subject/general-subject agree on the known post-amend
title.  The command is read-only by default; ``--apply`` is required to
replace the stale description and subject rows.

The caller supplies the SHA-256 of the already-pinned official archive.  No
network I/O occurs and no unpinned source can reach the database transaction.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from sqlalchemy import delete, select, text

from billcommons_schema.models import Bill, BillSubject, BillVersion, Jurisdiction, Session
from billcommons_shared.db import get_session


TARGET_OFFICIAL_BILL_ID = "202520260SB957"
TARGET_IDENTIFIER_NORM = "SB 957"
REGULAR_SESSION = "2025-2026 Regular Session"
EXPECTED_GENERAL_SUBJECT = "Civil detention facilities"
LOCK_NAME = "billcommons:ca:sb957:metadata-repair:20252026"

_BILL_COLUMN_COUNT = 19
_VERSION_COLUMN_COUNT = 18
_BILL_ID_INDEX = 0
_LATEST_VERSION_ID_INDEX = 10
_VERSION_ID_INDEX = 0
_VERSION_BILL_ID_INDEX = 1
_VERSION_SUBJECT_INDEX = 6
_VERSION_LOB_INDEX = 14


class MetadataRepairError(RuntimeError):
    """A fail-closed precondition or concurrent-write failure."""


@dataclass(frozen=True)
class OfficialMetadata:
    bill_id: str
    version_id: str
    subject: str
    digest: str
    lob_filename: str


@dataclass(frozen=True)
class LocalMetadata:
    bill_id: object
    title: str
    description: str | None
    subjects: tuple[str, ...]


@dataclass(frozen=True)
class RepairPlan:
    local: LocalMetadata
    official: OfficialMetadata

    @property
    def desired_subjects(self) -> tuple[str, ...]:
        return (self.official.subject,)

    @property
    def changed(self) -> bool:
        return self.local.description != self.official.digest or self.local.subjects != self.desired_subjects


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _read_rows(archive: zipfile.ZipFile, name: str, expected_columns: int) -> list[list[str]]:
    try:
        raw = archive.open(name)
    except KeyError as exc:
        raise MetadataRepairError(f"official ZIP is missing {name}") from exc
    with raw, io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="") as stream:
        rows = list(csv.reader(stream, delimiter="\t", quotechar="`", doublequote=True))
    for line_number, row in enumerate(rows, start=1):
        if len(row) != expected_columns:
            raise MetadataRepairError(f"{name}:{line_number} has {len(row)} fields; expected {expected_columns}")
    return rows


def _safe_lob_name(value: str) -> str:
    name = value.strip()
    path = PurePosixPath(name)
    if not name or name.upper() == "NULL" or path.name != name or name in {".", ".."}:
        raise MetadataRepairError("latest SB 957 version has an unsafe or missing XML LOB filename")
    return name


def _xml_field(root: ElementTree.Element, field_name: str) -> str:
    values = [
        _normal_text("".join(element.itertext()))
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == field_name
    ]
    values = [value for value in values if value]
    if len(values) != 1:
        raise MetadataRepairError(f"SB 957 XML must contain exactly one nonempty {field_name}")
    return values[0]


def load_official_metadata(zip_path: Path, expected_sha256: str) -> OfficialMetadata:
    """Read exactly the target's latest version metadata and XML from a pinned ZIP."""

    if re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
        raise MetadataRepairError("expected official ZIP SHA-256 must be exactly 64 hexadecimal characters")
    actual_sha256 = _sha256(zip_path)
    if actual_sha256.lower() != expected_sha256.lower():
        raise MetadataRepairError(f"official ZIP SHA-256 was {actual_sha256}; expected {expected_sha256}")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bills = _read_rows(archive, "BILL_TBL.dat", _BILL_COLUMN_COUNT)
            matches = [row for row in bills if row[_BILL_ID_INDEX] == TARGET_OFFICIAL_BILL_ID]
            if len(matches) != 1:
                raise MetadataRepairError("official ZIP must contain exactly one target SB 957 bill row")
            latest_version_id = matches[0][_LATEST_VERSION_ID_INDEX].strip()
            if not latest_version_id:
                raise MetadataRepairError("official target SB 957 has no latest_bill_version_id")

            versions = _read_rows(archive, "BILL_VERSION_TBL.dat", _VERSION_COLUMN_COUNT)
            matches = [
                row
                for row in versions
                if row[_VERSION_ID_INDEX] == latest_version_id
                and row[_VERSION_BILL_ID_INDEX] == TARGET_OFFICIAL_BILL_ID
            ]
            if len(matches) != 1:
                raise MetadataRepairError("official latest SB 957 version does not join exactly once")
            version = matches[0]
            version_subject = _normal_text(version[_VERSION_SUBJECT_INDEX])
            lob_filename = _safe_lob_name(version[_VERSION_LOB_INDEX])
            if archive.namelist().count(lob_filename) != 1:
                raise MetadataRepairError("official latest SB 957 XML LOB must occur exactly once")
            try:
                raw_lob = archive.read(lob_filename)
            except KeyError as exc:
                raise MetadataRepairError("official latest SB 957 XML LOB is missing") from exc
    except zipfile.BadZipFile as exc:
        raise MetadataRepairError("official ZIP is unreadable") from exc

    try:
        root = ElementTree.fromstring(raw_lob)
    except ElementTree.ParseError as exc:
        raise MetadataRepairError("official latest SB 957 XML is malformed") from exc
    general_subject = _xml_field(root, "GeneralSubject")
    digest = _xml_field(root, "DigestText")
    if general_subject != EXPECTED_GENERAL_SUBJECT or version_subject != EXPECTED_GENERAL_SUBJECT:
        raise MetadataRepairError(
            "official latest SB 957 subject/title shape does not match the expected post-amend target"
        )
    if not digest:
        raise MetadataRepairError("official latest SB 957 digest is empty")
    return OfficialMetadata(
        bill_id=TARGET_OFFICIAL_BILL_ID,
        version_id=latest_version_id,
        subject=general_subject,
        digest=digest,
        lob_filename=lob_filename,
    )


def _load_locked_plan(db, official: OfficialMetadata) -> RepairPlan:
    rows = db.execute(
        select(Bill, Session, Jurisdiction)
        .join(Session, Session.id == Bill.session_id)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .where(
            Jurisdiction.abbreviation == "CA",
            Session.identifier == REGULAR_SESSION,
            Session.classification.in_(("regular", "primary")),
            Bill.identifier_norm == TARGET_IDENTIFIER_NORM,
        )
        .with_for_update()
    ).all()
    if len(rows) != 1:
        raise MetadataRepairError("local target must be exactly one CA regular-session SB 957 bill")
    bill, _session, _jurisdiction = rows[0]
    if _normal_text(bill.title) != official.subject:
        raise MetadataRepairError("local SB 957 title does not match the pinned official current subject")
    subjects = tuple(
        sorted(
            db.execute(
                select(BillSubject.subject)
                .where(BillSubject.bill_id == bill.id)
                .order_by(BillSubject.subject)
                .with_for_update()
            ).scalars()
        )
    )
    return RepairPlan(
        local=LocalMetadata(
            bill_id=bill.id,
            title=bill.title,
            description=bill.description,
            subjects=subjects,
        ),
        official=official,
    )


def _report(plan: RepairPlan, *, zip_sha256: str, applied: bool) -> dict[str, object]:
    return {
        "target_official_bill_id": plan.official.bill_id,
        "official_version_id": plan.official.version_id,
        "official_subject": plan.official.subject,
        "official_digest_sha256": hashlib.sha256(plan.official.digest.encode("utf-8")).hexdigest(),
        "official_zip_sha256": zip_sha256,
        "existing_subject_count": len(plan.local.subjects),
        "desired_subject_count": len(plan.desired_subjects),
        "description_changed": plan.local.description != plan.official.digest,
        "subjects_changed": plan.local.subjects != plan.desired_subjects,
        "changed": plan.changed,
        "applied": applied,
    }


def _apply_locked_plan(db, plan: RepairPlan) -> None:
    """Write only the target description and exact target subject rows."""

    bill = db.execute(select(Bill).where(Bill.id == plan.local.bill_id).with_for_update()).scalar_one_or_none()
    if bill is None or bill.description != plan.local.description or _normal_text(bill.title) != plan.official.subject:
        raise MetadataRepairError("local SB 957 changed after the lock-protected plan")
    current_subjects = tuple(
        sorted(
            db.execute(
                select(BillSubject.subject)
                .where(BillSubject.bill_id == bill.id)
                .order_by(BillSubject.subject)
                .with_for_update()
            ).scalars()
        )
    )
    if current_subjects != plan.local.subjects:
        raise MetadataRepairError("local SB 957 subject rows changed after the lock-protected plan")
    if not plan.changed:
        return
    bill.description = plan.official.digest
    deletion = db.execute(delete(BillSubject).where(BillSubject.bill_id == bill.id))
    if deletion.rowcount != len(plan.local.subjects):
        raise MetadataRepairError("local SB 957 subject rowcount changed during replacement")
    for subject in plan.desired_subjects:
        db.add(BillSubject(bill_id=bill.id, subject=subject))
    db.flush()
    after_subjects = tuple(
        sorted(db.execute(select(BillSubject.subject).where(BillSubject.bill_id == bill.id)).scalars())
    )
    if bill.description != plan.official.digest or after_subjects != plan.desired_subjects:
        raise MetadataRepairError("SB 957 metadata postcondition failed")


def run(
    *,
    zip_path: Path,
    expected_sha256: str,
    apply: bool,
    session_factory: Callable[[], object] = get_session,
) -> dict[str, object]:
    """Plan or atomically apply the one-bill repair against a pinned archive."""

    official = load_official_metadata(zip_path, expected_sha256)
    actual_sha256 = _sha256(zip_path)
    db = session_factory()
    try:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": LOCK_NAME})
        plan = _load_locked_plan(db, official)
        report = _report(plan, zip_sha256=actual_sha256, applied=apply)
        if not apply:
            db.rollback()
            return report
        _apply_locked_plan(db, plan)
        db.commit()
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True, type=Path, help="pinned local official CA ZIP")
    parser.add_argument("--expected-sha256", required=True, help="exact SHA-256 of --zip")
    parser.add_argument("--apply", action="store_true", help="write the repair; default is a rollback-only plan")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(
            json.dumps(
                run(zip_path=args.zip, expected_sha256=args.expected_sha256, apply=args.apply),
                sort_keys=True,
            )
        )
    except MetadataRepairError as exc:
        print(f"CA SB 957 metadata repair failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
