#!/usr/bin/env python3
"""Export a deterministic, read-only CA 2025--26 post-sweep manifest.

The output is deliberately small and contains no connection information or
credentials.  It is the production-side input to ``audit_ca_snapshot.py``:
one canonical official CA bill identifier per local Bill Commons record.
"""
from __future__ import annotations

import argparse
import csv
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import Bill, Jurisdiction, Session
from billcommons_shared.db import get_session


CA_ABBREVIATION = "CA"
REGULAR_SESSION_IDENTIFIER = "2025-2026 Regular Session"
SPECIAL_SESSION_IDENTIFIER = "2025-2026 Special Session 1"
OFFICIAL_PREFIX = "20252026"
FIELDNAMES = (
    "production_bill_id",
    "ca_bill_id",
    "session_identifier",
    "identifier",
    "status",
    "latest_action_date",
    "latest_action_text",
    "openstates_id",
    "source_url",
)


class ManifestExportError(RuntimeError):
    """An operator-safe failure; no partial manifest is left behind."""


@dataclass(frozen=True)
class ManifestRow:
    production_bill_id: str
    ca_bill_id: str
    session_identifier: str
    identifier: str
    status: str
    latest_action_date: str
    latest_action_text: str
    openstates_id: str
    source_url: str

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in FIELDNAMES}


def official_bill_id(session: Session, bill: Bill) -> str:
    """Map one exact seeded CA session/local identifier to official ID."""
    if session.identifier == REGULAR_SESSION_IDENTIFIER and session.classification in {"regular", "primary", None}:
        session_number = "0"
    elif session.identifier == SPECIAL_SESSION_IDENTIFIER and session.classification == "special":
        session_number = "1"
    else:
        raise ManifestExportError(
            f"bill {bill.id} belongs to unexpected CA session "
            f"{session.identifier!r}/{session.classification!r}"
        )
    measure = "".join(character for character in bill.identifier.upper() if character.isalnum())
    if not measure:
        raise ManifestExportError(f"bill {bill.id} has no usable identifier")
    return f"{OFFICIAL_PREFIX}{session_number}{measure}"


def collect_rows(db: OrmSession) -> list[ManifestRow]:
    ca = db.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation == CA_ABBREVIATION)
    ).scalar_one_or_none()
    if ca is None:
        raise ManifestExportError("California jurisdiction is missing")
    sessions = db.execute(
        select(Session).where(
            Session.jurisdiction_id == ca.id,
            Session.identifier.in_([REGULAR_SESSION_IDENTIFIER, SPECIAL_SESSION_IDENTIFIER]),
        )
    ).scalars().all()
    session_by_id = {session.id: session for session in sessions}
    expected = {REGULAR_SESSION_IDENTIFIER, SPECIAL_SESSION_IDENTIFIER}
    if {session.identifier for session in sessions} != expected:
        raise ManifestExportError("both exact California regular and special session rows are required")

    bills = db.execute(
        select(Bill).where(Bill.jurisdiction_id == ca.id, Bill.session_id.in_(session_by_id))
    ).scalars().all()
    rows: list[ManifestRow] = []
    seen: set[str] = set()
    for bill in bills:
        session = session_by_id.get(bill.session_id)
        if session is None:
            raise ManifestExportError(f"bill {bill.id} was not in an exact CA sweep session")
        ca_bill_id = official_bill_id(session, bill)
        if ca_bill_id in seen:
            raise ManifestExportError(f"duplicate local CA official ID {ca_bill_id}")
        seen.add(ca_bill_id)
        rows.append(
            ManifestRow(
                production_bill_id=str(bill.id),
                ca_bill_id=ca_bill_id,
                session_identifier=session.identifier,
                identifier=bill.identifier,
                status=bill.status or "",
                latest_action_date=bill.latest_action_date.isoformat() if bill.latest_action_date else "",
                latest_action_text=bill.latest_action_text or "",
                openstates_id=bill.openstates_id or "",
                source_url=bill.source_url or "",
            )
        )
    if not rows:
        raise ManifestExportError("no CA bills found in the exact sweep sessions")
    return sorted(rows, key=lambda row: row.ca_bill_id)


def write_manifest(path: Path, rows: Iterable[ManifestRow]) -> None:
    """Atomically create a new TSV; never overwrite prior release evidence."""
    if path.exists():
        raise ManifestExportError(f"refusing to overwrite existing manifest {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDNAMES, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row.as_dict())
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publish fails rather than replacing a pre-existing evidence
        # artifact on supported filesystems.
        os.link(temporary_path, path)
    except FileExistsError as exc:
        raise ManifestExportError(f"refusing to overwrite existing manifest {path}") from exc
    except OSError as exc:
        raise ManifestExportError(f"could not write manifest {path}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def run(output: Path) -> int:
    db = get_session()
    try:
        # This proof command must be incapable of changing the production
        # database even if a future query is edited incorrectly.
        db.execute(text("SET TRANSACTION READ ONLY"))
        rows = collect_rows(db)
        write_manifest(output, rows)
        db.rollback()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new TSV evidence path; must not already exist")
    args = parser.parse_args(argv)
    try:
        count = run(args.output)
    except ManifestExportError as exc:
        parser.error(str(exc))
    print(f"wrote {count} CA bill rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
