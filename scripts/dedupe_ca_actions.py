#!/usr/bin/env python3
"""Remove only CA action rows proven excessive by a pinned official ledger.

The identity of a California action in this repair is the exact official bill
ID plus action date plus whitespace-normalized description.  The official
``BILL_HISTORY_TBL`` multiplicity is authoritative: if it contains the same
fact twice, two local rows remain.  A local fact absent from the official
snapshot is reported but never deleted.

The command is a dry run unless ``--apply`` is supplied.  It is intentionally
limited to the two named 2025-2026 CA sessions and requires the caller to pin
the local official ZIP by SHA-256.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from sqlalchemy import select, text

from billcommons_ingest import events
from billcommons_schema.models import Bill, BillAction, Jurisdiction, Session
from billcommons_shared.db import get_session


REGULAR_SESSION = "2025-2026 Regular Session"
SPECIAL_SESSION = "2025-2026 Special Session 1"
OFFICIAL_PREFIX = "20252026"
LOCK_NAME = "billcommons:ca:official-action-dedupe:20252026"


class DedupeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedDeletion:
    action_id: object
    bill_id: object
    official_bill_id: str
    action_date: str
    description: str


@dataclass(frozen=True)
class LocalBill:
    id: object
    session_id: object
    identifier: str


@dataclass(frozen=True)
class LocalAction:
    id: object
    bill_id: object
    description: str
    action_date: object
    classification: str | None
    source_name: str | None
    retrieved_at: datetime | None
    order: int | None


# The dedupe planner does not read action provenance blobs, bill descriptions,
# search vectors, or session provenance. Selecting those large columns once
# per action made a read-only proof exceed PostgreSQL's statement timeout over
# the remote proxy. Keep this projection tied to exactly the retention/key
# fields below.
ACTION_READ_COLUMNS = (
    BillAction.id,
    BillAction.bill_id,
    BillAction.description,
    BillAction.action_date,
    BillAction.classification,
    BillAction.source_name,
    BillAction.retrieved_at,
    BillAction.order,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal_description(value: str | None) -> str:
    return " ".join((value or "").split())


def _official_bill_id(session: Session, bill: Bill) -> str:
    if session.identifier == REGULAR_SESSION and session.classification in {"regular", "primary"}:
        session_number = "0"
    elif session.identifier == SPECIAL_SESSION and session.classification == "special":
        session_number = "1"
    else:
        raise DedupeError(
            f"bill {bill.id} belongs to unexpected CA session "
            f"{session.identifier!r}/{session.classification!r}"
        )
    measure = "".join(character for character in bill.identifier.upper() if character.isalnum())
    if not measure:
        raise DedupeError(f"bill {bill.id} has no usable identifier")
    return f"{OFFICIAL_PREFIX}{session_number}{measure}"


def _official_counts(zip_path: Path) -> tuple[Counter, set[str]]:
    counts: Counter = Counter()
    bill_ids: set[str] = set()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            with archive.open("BILL_TBL.dat") as raw, io.TextIOWrapper(
                raw, encoding="utf-8", errors="strict", newline=""
            ) as stream:
                for row in csv.reader(stream, delimiter="\t", quotechar="`"):
                    if len(row) != 19:
                        raise DedupeError("malformed BILL_TBL.dat row")
                    if row[0] in bill_ids:
                        raise DedupeError(f"duplicate official bill ID {row[0]}")
                    bill_ids.add(row[0])
            with archive.open("BILL_HISTORY_TBL.dat") as raw, io.TextIOWrapper(
                raw, encoding="utf-8", errors="strict", newline=""
            ) as stream:
                for row in csv.reader(stream, delimiter="\t", quotechar="`"):
                    if len(row) != 13:
                        raise DedupeError("malformed BILL_HISTORY_TBL.dat row")
                    counts[(row[0], row[2][:10], _normal_description(row[3]))] += 1
    except (KeyError, zipfile.BadZipFile) as exc:
        raise DedupeError("official ZIP is missing a required readable table") from exc
    return counts, bill_ids


def _retention_key(action: LocalAction) -> tuple:
    """Prefer the most useful row without pretending its UUID is source truth."""
    return (
        bool(action.classification),
        action.source_name == "openstates_api_sync",
        action.retrieved_at or datetime.min.replace(tzinfo=timezone.utc),
        action.order is not None,
        str(action.id),
    )


def choose_excess(actions: Iterable[LocalAction], official_count: int) -> list[LocalAction]:
    rows = sorted(actions, key=_retention_key, reverse=True)
    if official_count < 1 or len(rows) <= official_count:
        return []
    return rows[official_count:]


def plan(db, zip_path: Path) -> tuple[list[PlannedDeletion], dict]:
    official, official_bill_ids = _official_counts(zip_path)
    ca = db.execute(select(Jurisdiction).where(Jurisdiction.abbreviation == "CA")).scalar_one_or_none()
    if ca is None:
        raise DedupeError("California jurisdiction is missing")
    sessions = db.execute(
        select(Session).where(
            Session.jurisdiction_id == ca.id,
            Session.identifier.in_([REGULAR_SESSION, SPECIAL_SESSION]),
        )
    ).scalars().all()
    session_by_id = {session.id: session for session in sessions}
    if {session.identifier for session in sessions} != {REGULAR_SESSION, SPECIAL_SESSION}:
        raise DedupeError("both exact California regular and special session rows are required")

    bill_rows = db.execute(
        select(Bill.id, Bill.session_id, Bill.identifier).where(
            Bill.jurisdiction_id == ca.id,
            Bill.session_id.in_(session_by_id),
        )
    ).all()
    bill_by_id: dict[object, LocalBill] = {}
    official_by_bill_id: dict[object, str] = {}
    for bill_id, session_id, identifier in bill_rows:
        bill = LocalBill(bill_id, session_id, identifier)
        session = session_by_id.get(session_id)
        if session is None:
            raise DedupeError(f"local bill {bill_id} has no exact CA sweep session")
        official_bill_id = _official_bill_id(session, bill)
        if official_bill_id not in official_bill_ids:
            raise DedupeError(f"local bill {bill_id} maps to absent official ID {official_bill_id}")
        bill_by_id[bill_id] = bill
        official_by_bill_id[bill_id] = official_bill_id

    # Critical performance invariant: no Bill/Session ORM entity is joined to
    # every action. The 5k parent rows are already represented in maps above.
    action_rows = db.execute(
        select(*ACTION_READ_COLUMNS).where(BillAction.bill_id.in_(bill_by_id))
    ).all()
    local: dict[tuple, list[LocalAction]] = defaultdict(list)
    bill_for_key: dict[tuple, LocalBill] = {}
    local_bill_ids: set[str] = set()
    for row in action_rows:
        action = LocalAction(*row)
        bill = bill_by_id.get(action.bill_id)
        official_bill_id = official_by_bill_id.get(action.bill_id)
        if bill is None or official_bill_id is None:
            raise DedupeError(f"local action {action.id} has no exact CA sweep bill")
        local_bill_ids.add(official_bill_id)
        key = (
            official_bill_id,
            action.action_date.isoformat() if action.action_date else "",
            _normal_description(action.description),
        )
        local[key].append(action)
        bill_for_key[key] = bill

    deletions: list[PlannedDeletion] = []
    duplicate_groups = 0
    legitimate_duplicate_groups = 0
    unsupported_duplicate_groups = 0
    for key, actions in local.items():
        if len(actions) <= 1:
            continue
        duplicate_groups += 1
        expected = official[key]
        if expected == 0:
            unsupported_duplicate_groups += 1
            continue
        excess = choose_excess(actions, expected)
        if not excess:
            legitimate_duplicate_groups += 1
            continue
        for action in excess:
            deletions.append(
                PlannedDeletion(
                    action_id=action.id,
                    bill_id=bill_for_key[key].id,
                    official_bill_id=key[0],
                    action_date=key[1],
                    description=key[2],
                )
            )
    report = {
        "official_bill_count": len(official_bill_ids),
        "mapped_local_bill_count": len(local_bill_ids),
        "local_action_count": len(action_rows),
        "duplicate_groups": duplicate_groups,
        "legitimate_duplicate_groups": legitimate_duplicate_groups,
        "unsupported_duplicate_groups": unsupported_duplicate_groups,
        "planned_deletions": len(deletions),
        "touched_bills": len({item.bill_id for item in deletions}),
    }
    return deletions, report


def run(*, zip_path: Path, expected_sha256: str, apply: bool) -> dict:
    actual_sha256 = _sha256(zip_path)
    if actual_sha256.lower() != expected_sha256.lower():
        raise DedupeError(
            f"official ZIP SHA-256 was {actual_sha256}; expected {expected_sha256}"
        )
    db = get_session()
    try:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": LOCK_NAME})
        deletions, report = plan(db, zip_path)
        report.update({"official_zip_sha256": actual_sha256, "applied": apply})
        if not apply:
            db.rollback()
            return report

        by_bill: dict[object, int] = Counter(item.bill_id for item in deletions)
        action_ids = [item.action_id for item in deletions]
        if action_ids:
            actions = db.execute(select(BillAction).where(BillAction.id.in_(action_ids))).scalars().all()
            if len(actions) != len(action_ids):
                raise DedupeError("planned action set changed before lock-protected deletion")
            for action in actions:
                db.delete(action)
            now = datetime.now(timezone.utc)
            for bill_id, count in by_bill.items():
                bill = db.get(Bill, bill_id)
                if bill is None:
                    raise DedupeError(f"target bill disappeared: {bill_id}")
                if bill.updated_at is None or now > bill.updated_at:
                    bill.updated_at = now
                events.record_event(
                    db,
                    bill_id,
                    events.ACTIONS,
                    f"removed {count} duplicate action row(s) against pinned official multiplicity",
                )
        db.flush()
        remaining, after = plan(db, zip_path)
        if remaining:
            raise DedupeError(f"{len(remaining)} officially-proven excess action rows remain")
        report["post_duplicate_groups"] = after["duplicate_groups"]
        report["post_legitimate_duplicate_groups"] = after["legitimate_duplicate_groups"]
        report["post_unsupported_duplicate_groups"] = after["unsupported_duplicate_groups"]
        db.commit()
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(zip_path=args.zip, expected_sha256=args.expected_sha256, apply=args.apply)
    except DedupeError as exc:
        print(f"CA action dedupe failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
