#!/usr/bin/env python3
"""Reconcile California action history from a pinned official ``pubinfo`` ZIP.

This is a deliberately narrow, one-off repair companion to the CA session
collision repair.  The official ``BILL_HISTORY_TBL`` ledger is the source of
truth for the two 2025--26 CA sessions.  It adds missing official facts,
removes only *excess copies of facts the ledger itself proves*, and leaves a
local singleton that is absent from the ledger alone for later investigation.

The command is read-only by default.  ``--apply`` is required to write and
the caller must pin the exact archive SHA-256.  It does no network I/O.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import signal
import sys
import threading
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import column, delete, func, select, text, update, values
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import events, status
from billcommons_schema.models import Bill, BillAction, Jurisdiction, Session
from billcommons_shared.db import get_session


CA_ABBREVIATION = "CA"
REGULAR_SESSION_IDENTIFIER = "2025-2026 Regular Session"
SPECIAL_SESSION_IDENTIFIER = "2025-2026 Special Session 1"
OFFICIAL_PREFIX = "20252026"
OFFICIAL_SOURCE_URL = "https://downloads.leginfo.legislature.ca.gov/pubinfo_daily_Wed.zip"
SOURCE_NAME = "ca_official_action_sweep/2026-09-02"
PARSER_VERSION = "ca-pubinfo-history-v1"
LOCK_NAME = "billcommons:ca:official-action-sweep:20252026"
TOTAL_TRANSACTION_TIMEOUT_SECONDS = 600
ACTION_UPDATE_CHUNK_SIZE = 500
_ACTION_UPDATE_FIELDS = (
    "order",
    "source_name",
    "source_url",
    "upstream_id",
    "retrieved_at",
    "raw_ref",
    "checksum",
    "parser_version",
)


class OfficialActionSweepError(RuntimeError):
    """An operator-safe failure; callers must not treat it as partial success."""


@contextmanager
def _total_transaction_timeout(seconds: int):
    """Interrupt the whole local transaction, not merely one SQL statement.

    PostgreSQL's ``statement_timeout`` resets between statements and cannot
    protect a multi-step reconcile.  The sweep runs as a foreground CLI on
    Linux, so a real-time alarm gives the transaction one bounded lifetime;
    the caller's exception path rolls it back.  The production runbook must
    also wrap this command with the same external timeout, covering an
    interpreter or driver that is unable to process a signal promptly.
    """
    if seconds < 1:
        raise ValueError("total transaction timeout must be positive")
    if threading.current_thread() is not threading.main_thread():
        # CLI production execution is always the main thread. Keep helper
        # tests/embedders safe rather than installing a process-global signal.
        yield
        return

    def expired(_signum, _frame):
        raise OfficialActionSweepError(
            f"official action sweep exceeded total transaction timeout of {seconds}s"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


@dataclass(frozen=True)
class OfficialAction:
    official_bill_id: str
    history_id: str
    action_date: date | None
    description: str
    sequence: int | None
    updated_at: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.official_bill_id, self.action_date.isoformat() if self.action_date else "", _normal_description(self.description))


@dataclass(frozen=True)
class LocalAction:
    id: object
    bill_id: object
    official_bill_id: str
    action_date: date | None
    description: str
    classification: str | None
    order: int | None
    row: Any | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.official_bill_id, self.action_date.isoformat() if self.action_date else "", _normal_description(self.description))


@dataclass(frozen=True)
class OrderUpdate:
    action_id: object
    official: OfficialAction


@dataclass(frozen=True)
class ActionPlan:
    additions: tuple[OfficialAction, ...]
    deletions: tuple[LocalAction, ...]
    order_updates: tuple[OrderUpdate, ...]
    official_by_bill: dict[str, tuple[OfficialAction, ...]]
    local_by_bill: dict[str, tuple[LocalAction, ...]]
    unsupported_local_singletons: int

    @property
    def missing_count(self) -> int:
        return len(self.additions)

    @property
    def excess_count(self) -> int:
        return len(self.deletions)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normal_description(value: str | None) -> str:
    return " ".join((value or "").split())


def _parse_date(value: str) -> date | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise OfficialActionSweepError(f"invalid official action date {value!r}") from exc


def _parse_sequence(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise OfficialActionSweepError(f"invalid official action sequence {value!r}") from exc


def _official_sort_key(action: OfficialAction) -> tuple[int, str, str]:
    # The official file is not physically ordered by bill history.  Sequence
    # is canonical; the other two fields only make malformed/tied sequences
    # deterministic without discarding a factual row.
    return (action.sequence if action.sequence is not None else -1, action.action_date.isoformat() if action.action_date else "", action.history_id)


def _official_bill_id(session: Session, bill: Bill) -> str:
    if session.identifier == REGULAR_SESSION_IDENTIFIER and session.classification in {"regular", "primary", None}:
        session_number = "0"
    elif session.identifier == SPECIAL_SESSION_IDENTIFIER and session.classification == "special":
        session_number = "1"
    else:
        raise OfficialActionSweepError(
            f"bill {bill.id} belongs to unexpected CA session {session.identifier!r}/{session.classification!r}"
        )
    measure = "".join(character for character in bill.identifier.upper() if character.isalnum())
    if not measure:
        raise OfficialActionSweepError(f"bill {bill.id} has no usable identifier")
    return f"{OFFICIAL_PREFIX}{session_number}{measure}"


def load_official_actions(zip_path: Path) -> dict[str, tuple[OfficialAction, ...]]:
    """Read only the bill and history TSVs necessary for action reconciliation."""
    bill_ids: set[str] = set()
    actions: dict[str, list[OfficialAction]] = defaultdict(list)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            with archive.open("BILL_TBL.dat") as raw, io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="") as stream:
                for line_number, row in enumerate(csv.reader(stream, delimiter="\t", quotechar="`"), start=1):
                    if len(row) != 19:
                        raise OfficialActionSweepError(f"BILL_TBL.dat:{line_number} has {len(row)} fields; expected 19")
                    # The archive can retain older history-only rows.  The
                    # present two-session scope is explicitly selected here.
                    if row[1] != OFFICIAL_PREFIX or row[2] not in {"0", "1"}:
                        continue
                    if row[0] in bill_ids:
                        raise OfficialActionSweepError(f"duplicate official bill ID {row[0]}")
                    bill_ids.add(row[0])
            with archive.open("BILL_HISTORY_TBL.dat") as raw, io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="") as stream:
                for line_number, row in enumerate(csv.reader(stream, delimiter="\t", quotechar="`"), start=1):
                    if len(row) != 13:
                        raise OfficialActionSweepError(f"BILL_HISTORY_TBL.dat:{line_number} has {len(row)} fields; expected 13")
                    if row[0] not in bill_ids:
                        continue
                    description = _normal_description(row[3])
                    if not description:
                        raise OfficialActionSweepError(f"BILL_HISTORY_TBL.dat:{line_number} has blank action text")
                    if not row[1].strip():
                        raise OfficialActionSweepError(f"BILL_HISTORY_TBL.dat:{line_number} has blank history ID")
                    actions[row[0]].append(
                        OfficialAction(
                            official_bill_id=row[0], history_id=row[1].strip(), action_date=_parse_date(row[2]),
                            description=description, sequence=_parse_sequence(row[6]), updated_at=row[5].strip(),
                        )
                    )
    except (KeyError, zipfile.BadZipFile) as exc:
        raise OfficialActionSweepError("official ZIP is missing a required readable table") from exc
    if not bill_ids:
        raise OfficialActionSweepError("official ZIP contains no CA 2025-26 regular/special bills")
    return {
        bill_id: tuple(sorted(actions[bill_id], key=_official_sort_key))
        for bill_id in bill_ids
    }


def _retention_key(action: LocalAction) -> tuple:
    row = action.row
    return (
        bool(getattr(row, "classification", action.classification)),
        getattr(row, "source_name", None) == SOURCE_NAME,
        getattr(row, "retrieved_at", None) or datetime.min.replace(tzinfo=timezone.utc),
        action.order is not None,
        str(action.id),
    )


def _official_upstream_id(action: OfficialAction) -> str:
    return f"ca-history:{action.history_id}"


def _pair_local_to_official(
    local_rows: Iterable[LocalAction], official_rows: Iterable[OfficialAction]
) -> list[tuple[LocalAction, OfficialAction]]:
    """Pair duplicate facts stably across a reload.

    A human-text/date key can legitimately occur more than once in the CA
    ledger.  On the first repair, retention ordering chooses a local survivor
    and associates it with an official history row.  On later repairs that
    association is durable in ``upstream_id``; reusing it prevents a changed
    retrieval timestamp or UUID ordering from swapping two otherwise equal
    copies and producing perpetual order updates.
    """
    remaining_local = list(local_rows)
    remaining_official = sorted(official_rows, key=_official_sort_key)
    pairs: list[tuple[LocalAction, OfficialAction]] = []

    for official in tuple(remaining_official):
        matching = [
            local
            for local in remaining_local
            if getattr(local.row, "upstream_id", None) == _official_upstream_id(official)
        ]
        if not matching:
            continue
        local = max(matching, key=_retention_key)
        pairs.append((local, official))
        remaining_local.remove(local)
        remaining_official.remove(official)

    pairs.extend(
        zip(
            sorted(remaining_local, key=_retention_key, reverse=True),
            remaining_official,
            strict=False,
        )
    )
    return pairs


def build_plan(
    official_by_bill: dict[str, tuple[OfficialAction, ...]], local_actions: Iterable[LocalAction]
) -> ActionPlan:
    """Build a side-effect-free multiplicity plan, suitable for dry-run output/tests."""
    local_by_key: dict[tuple[str, str, str], list[LocalAction]] = defaultdict(list)
    local_by_bill: dict[str, list[LocalAction]] = defaultdict(list)
    for local in local_actions:
        if local.official_bill_id not in official_by_bill:
            raise OfficialActionSweepError(f"local bill {local.bill_id} maps to absent official ID {local.official_bill_id}")
        local_by_key[local.key].append(local)
        local_by_bill[local.official_bill_id].append(local)

    official_by_key: dict[tuple[str, str, str], list[OfficialAction]] = defaultdict(list)
    for official_bill_id, actions in official_by_bill.items():
        for action in actions:
            official_by_key[action.key].append(action)

    additions: list[OfficialAction] = []
    deletions: list[LocalAction] = []
    order_updates: list[OrderUpdate] = []
    unsupported = 0
    for key, official_rows in official_by_key.items():
        local_rows = sorted(local_by_key.get(key, []), key=_retention_key, reverse=True)
        ordered_official_rows = sorted(official_rows, key=_official_sort_key)
        retained_local_rows = local_rows[:len(ordered_official_rows)]
        pairs = _pair_local_to_official(retained_local_rows, ordered_official_rows)
        if len(local_rows) < len(official_rows):
            # Provenance can establish that the existing local occurrence is
            # the *later* official duplicate. Positional slicing would add
            # that same history row again and leave the earlier one absent.
            paired_history_ids = {official.history_id for _local, official in pairs}
            additions.extend(
                official
                for official in ordered_official_rows
                if official.history_id not in paired_history_ids
            )
        elif len(local_rows) > len(official_rows):
            deletions.extend(local_rows[len(official_rows):])
        # The retention ordering selects which local duplicates survive; the
        # official history sequence orders the corresponding factual copies.
        # That creates an exact pairing even for repeated same-day text such
        # as AB 1546, rather than leaving arbitrary old orders in place.
        for local, official in pairs:
            if local.order != official.sequence:
                order_updates.append(OrderUpdate(local.id, official))

    for key, local_rows in local_by_key.items():
        if key not in official_by_key:
            # Unsupported local actions are intentionally preserved, including
            # duplicates.  The singular count is a report label, not a delete
            # eligibility rule.
            unsupported += len(local_rows)

    return ActionPlan(
        additions=tuple(sorted(additions, key=lambda item: (item.official_bill_id, _official_sort_key(item)))),
        deletions=tuple(sorted(deletions, key=lambda item: str(item.id))),
        order_updates=tuple(sorted(order_updates, key=lambda item: str(item.action_id))),
        official_by_bill=official_by_bill,
        local_by_bill={bill_id: tuple(rows) for bill_id, rows in local_by_bill.items()},
        unsupported_local_singletons=unsupported,
    )


def _load_local_actions(db: OrmSession, official_by_bill: dict[str, tuple[OfficialAction, ...]]) -> tuple[dict[object, Bill], list[LocalAction], dict[object, Session]]:
    ca = db.execute(select(Jurisdiction).where(Jurisdiction.abbreviation == CA_ABBREVIATION)).scalar_one_or_none()
    if ca is None:
        raise OfficialActionSweepError("California jurisdiction is missing")
    sessions = db.execute(
        select(Session).where(
            Session.jurisdiction_id == ca.id,
            Session.identifier.in_([REGULAR_SESSION_IDENTIFIER, SPECIAL_SESSION_IDENTIFIER]),
        )
    ).scalars().all()
    by_identifier = {session.identifier: session for session in sessions}
    if set(by_identifier) != {REGULAR_SESSION_IDENTIFIER, SPECIAL_SESSION_IDENTIFIER}:
        raise OfficialActionSweepError("both exact California regular and special session rows are required")
    if by_identifier[REGULAR_SESSION_IDENTIFIER].active is not True:
        raise OfficialActionSweepError("California regular session must remain active during this action sweep")

    bills = db.execute(
        select(Bill).where(Bill.jurisdiction_id == ca.id, Bill.session_id.in_([item.id for item in sessions]))
    ).scalars().all()
    bill_by_id = {bill.id: bill for bill in bills}
    session_by_bill_id = {bill.id: by_identifier[REGULAR_SESSION_IDENTIFIER] if bill.session_id == by_identifier[REGULAR_SESSION_IDENTIFIER].id else by_identifier[SPECIAL_SESSION_IDENTIFIER] for bill in bills}
    mapped_official_ids = {
        _official_bill_id(session_by_bill_id[bill.id], bill)
        for bill in bills
    }
    missing_bills = set(official_by_bill) - mapped_official_ids
    if missing_bills:
        preview = ", ".join(sorted(missing_bills)[:5])
        raise OfficialActionSweepError(
            f"{len(missing_bills)} official CA bills are absent locally (first: {preview})"
        )
    # Load each action once. Selecting Bill + Session alongside every one of
    # ~86k action rows repeats large bill text/search columns over the network
    # and can exceed the production statement timeout; both parent maps are
    # already resident above.
    action_rows = db.execute(
        select(BillAction).where(BillAction.bill_id.in_(list(bill_by_id)))
    ).scalars().all()
    local: list[LocalAction] = []
    for action in action_rows:
        bill = bill_by_id[action.bill_id]
        session = session_by_bill_id[bill.id]
        official_bill_id = _official_bill_id(session, bill)
        if official_bill_id not in official_by_bill:
            raise OfficialActionSweepError(f"local bill {bill.id} maps to absent official ID {official_bill_id}")
        local.append(LocalAction(
            id=action.id, bill_id=bill.id, official_bill_id=official_bill_id, action_date=action.action_date,
            description=action.description, classification=action.classification, order=action.order, row=action,
        ))
    return bill_by_id, local, session_by_bill_id


def _action_provenance(action: OfficialAction, zip_sha256: str, now: datetime) -> dict[str, Any]:
    checksum = hashlib.sha256(json.dumps({"bill": action.official_bill_id, "history": action.history_id, "date": action.action_date.isoformat() if action.action_date else None, "description": action.description, "sequence": action.sequence}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "source_name": SOURCE_NAME,
        "source_url": OFFICIAL_SOURCE_URL,
        "upstream_id": f"ca-history:{action.history_id}",
        "retrieved_at": now,
        "raw_ref": f"ca-pubinfo:{zip_sha256}:BILL_HISTORY_TBL:{action.history_id}",
        "checksum": checksum,
        "parser_version": PARSER_VERSION,
    }


def _report(plan: ActionPlan, *, zip_sha256: str, apply: bool, updates: int = 0, touched_bills: int = 0) -> dict[str, Any]:
    return {
        "applied": apply,
        "official_zip_sha256": zip_sha256,
        "official_bill_count": len(plan.official_by_bill),
        "official_action_count": sum(len(items) for items in plan.official_by_bill.values()),
        "mapped_local_bill_count": len(plan.local_by_bill),
        "local_action_count": sum(len(items) for items in plan.local_by_bill.values()),
        "missing_official_actions": plan.missing_count,
        "excess_official_actions": plan.excess_count,
        "order_updates": len(plan.order_updates),
        "unsupported_local_actions_preserved": plan.unsupported_local_singletons,
        "added_actions": plan.missing_count if apply else 0,
        "deleted_excess_actions": plan.excess_count if apply else 0,
        "applied_order_updates": updates if apply else 0,
        "touched_bills": touched_bills if apply else 0,
    }


def _latest(official_rows: tuple[OfficialAction, ...]) -> OfficialAction | None:
    return max(official_rows, key=_official_sort_key, default=None)


def _action_update_mappings(
    plan: ActionPlan, *, zip_sha256: str, now: datetime
) -> list[dict[str, Any]]:
    """Return one bulk-by-primary-key mapping per changed existing action.

    The first official reconciliation can legitimately attach primary-source
    provenance to tens of thousands of existing action rows.  Mutating every
    ORM instance made SQLAlchemy flush thousands of remote round trips inside
    one serializable transaction.  These mappings preserve the exact same
    pairing/retention rules but let the DBAPI execute them as a bounded bulk
    operation.  Caller still computes statuses from the already-loaded rows;
    classification and organization are intentionally not changed here.
    """
    updates: dict[object, dict[str, Any]] = {}

    def consider(local: LocalAction, official: OfficialAction) -> None:
        if local.row is None:
            raise OfficialActionSweepError("planned action row disappeared before provenance reconciliation")
        provenance = _action_provenance(official, zip_sha256, now)
        # Re-applying the exact same pinned ledger is an idempotent status
        # repair, not a new retrieval. Preserve the original retrieval time
        # when every immutable provenance field already identifies this exact
        # source row; otherwise a rerun would rewrite all 86k rows solely
        # because ``now`` changed.
        immutable_fields = tuple(field for field in provenance if field != "retrieved_at")
        if all(getattr(local.row, field) == provenance[field] for field in immutable_fields):
            provenance["retrieved_at"] = local.row.retrieved_at
        candidate = dict(provenance)
        # ``build_plan`` selects a deterministic surviving local copy for
        # every official duplicate. Always carry the paired sequence through
        # to the mutation mapping, not only the formerly one-to-one case.
        candidate["order"] = official.sequence
        if any(getattr(local.row, field) != value for field, value in candidate.items()):
            # SQLAlchemy ORM bulk UPDATE identifies a row using this primary
            # key; it is not an application field update.
            candidate["id"] = local.id
            updates[local.id] = candidate

    for local, official in _paired_local_official_rows(plan):
        consider(local, official)
    return [updates[action_id] for action_id in sorted(updates, key=str)]


def _paired_local_official_rows(
    plan: ActionPlan,
) -> list[tuple[LocalAction, OfficialAction]]:
    """Return the durable survivor-to-ledger pairing used by every apply step.

    Reconciliation of duplicate normalized facts cannot fall back to row load
    order after planning: a local row may already identify one exact official
    history occurrence through ``upstream_id``.  Both provenance updates and
    status reconstruction must use this same pairing or a classification can
    migrate to a different occurrence of otherwise identical text.
    """
    deleted_ids = {item.id for item in plan.deletions}
    pairs: list[tuple[LocalAction, OfficialAction]] = []
    for local_rows in plan.local_by_bill.values():
        by_key: dict[tuple[str, str, str], list[LocalAction]] = defaultdict(list)
        for local in local_rows:
            if local.id not in deleted_ids:
                by_key[local.key].append(local)
        for key, rows in by_key.items():
            official_rows = sorted(
                (row for row in plan.official_by_bill.get(key[0], ()) if row.key == key),
                key=_official_sort_key,
            )
            pairs.extend(_pair_local_to_official(rows, official_rows))
    return pairs


def _execute_action_updates(db: OrmSession, mappings: Sequence[dict[str, Any]]) -> int:
    """Apply action provenance/order with bounded set-based UPDATE statements.

    ORM ``executemany`` still sent one UPDATE message per action through the
    Railway TCP proxy.  A VALUES relation turns each 500-row chunk into one
    server-side ``UPDATE ... FROM`` statement while retaining bound values and
    exact primary-key targeting.
    """
    if not mappings:
        return 0
    table = BillAction.__table__
    names = ("id", *_ACTION_UPDATE_FIELDS)
    typed_columns = [column(name, table.c[name].type) for name in names]
    updated = 0
    for offset in range(0, len(mappings), ACTION_UPDATE_CHUNK_SIZE):
        chunk = mappings[offset : offset + ACTION_UPDATE_CHUNK_SIZE]
        source = values(*typed_columns, name="action_updates").data(
            [tuple(mapping[name] for name in names) for mapping in chunk]
        ).alias("action_updates")
        statement = (
            update(BillAction)
            .where(BillAction.id == source.c.id)
            .values({name: getattr(source.c, name) for name in _ACTION_UPDATE_FIELDS})
        )
        result = db.execute(statement)
        _require_exact_rowcount(result.rowcount, len(chunk), operation="bulk action update")
        updated += result.rowcount
    return updated


def _require_exact_rowcount(actual: int | None, expected: int, *, operation: str) -> None:
    """Reject a partial mutation instead of treating it as a successful sweep."""
    if actual != expected:
        raise OfficialActionSweepError(f"{operation} affected {actual} rows; expected {expected}")


def _apply_plan(
    db: OrmSession, *, plan: ActionPlan, bill_by_id: dict[object, Bill], session_by_bill_id: dict[object, Session], zip_sha256: str, now: datetime
) -> tuple[int, int]:
    """Apply a precomputed plan; caller owns transaction/lock and commits."""
    by_id = {item.id: item for rows in plan.local_by_bill.values() for item in rows}
    touched: set[object] = set()
    updates = 0
    if plan.deletions:
        deletion_ids = [local.id for local in plan.deletions]
        # The serializable advisory-locked plan names every exact primary key.
        # A single set delete provides an observable rowcount; ORM deletion
        # cannot distinguish a stale/missing target from success reliably.
        deleted = db.execute(delete(BillAction).where(BillAction.id.in_(deletion_ids)))
        _require_exact_rowcount(deleted.rowcount, len(deletion_ids), operation="bulk action delete")
        touched.update(local.bill_id for local in plan.deletions)
    action_updates = _action_update_mappings(plan, zip_sha256=zip_sha256, now=now)
    if action_updates:
        # Bounded server-side set updates, rather than one ORM/driver UPDATE
        # per action. Below we need only the immutable classification and
        # organization already loaded on each row, never this provenance/order.
        applied_updates = _execute_action_updates(db, action_updates)
        if applied_updates != len(action_updates):
            raise OfficialActionSweepError(
                f"bulk action update applied {applied_updates} rows; expected {len(action_updates)}"
            )
        for mapping in action_updates:
            local = by_id.get(mapping["id"])
            if local is None:
                raise OfficialActionSweepError("planned action row disappeared before bulk reconciliation")
            touched.add(local.bill_id)
    updates = len(plan.order_updates)
    bill_by_official = {
        _official_bill_id(session_by_bill_id[bill_id], bill): bill
        for bill_id, bill in bill_by_id.items()
    }
    for official in plan.additions:
        bill = bill_by_official.get(official.official_bill_id)
        if bill is None:
            raise OfficialActionSweepError(f"official action is missing a local bill {official.official_bill_id}")
        new_row = BillAction(
            bill_id=bill.id, description=official.description, action_date=official.action_date,
            classification=None, order=official.sequence, **_action_provenance(official, zip_sha256, now),
        )
        db.add(new_row)
        touched.add(bill.id)
    db.flush()

    # Latest scalar and status deliberately use the official ledger, while
    # retaining any structured classification available on the exact paired
    # local history occurrence. Do not use DB load/FIFO order here: CA can
    # legitimately publish the same normalized text more than once per day.
    metadata_by_history = {
        (official.official_bill_id, official.history_id): (
            local.classification,
            getattr(local.row, "organization_id", None),
        )
        for local, official in _paired_local_official_rows(plan)
    }
    for official_bill_id, official_rows in plan.official_by_bill.items():
        bill = bill_by_official.get(official_bill_id)
        if bill is None:
            continue  # Official-only bills belong to the collision repair scope.
        latest = _latest(official_rows)
        if latest is None:
            continue
        old_latest = (bill.latest_action_date, bill.latest_action_text)
        bill.latest_action_date = latest.action_date
        bill.latest_action_text = latest.description
        action_rows: list[status.ActionRow] = []
        for official in official_rows:
            classification, organization_id = metadata_by_history.get(
                (official_bill_id, official.history_id), (None, None)
            )
            action_rows.append(
                status.ActionRow(
                    official.action_date,
                    classification,
                    official.description,
                    organization_id,
                    official.sequence,
                    SOURCE_NAME,
                )
            )
        session = session_by_bill_id[bill.id]
        old_status = bill.status
        bill.status = status.apply_session_outcome(
            status.derive_status(action_rows), session.end_date, today=now.date(), session_active=bool(session.active),
        )
        bill.status_date = latest.action_date
        if old_latest != (bill.latest_action_date, bill.latest_action_text) or old_status != bill.status:
            touched.add(bill.id)
            if old_status != bill.status:
                events.record_event(db, bill.id, events.STATUS, f"{old_status or 'none'} -> {bill.status or 'none'} from pinned CA official action ledger")
    for bill_id in touched:
        bill = bill_by_id[bill_id]
        if bill.updated_at is None or now > bill.updated_at:
            bill.updated_at = now
        events.record_event(db, bill_id, events.ACTIONS, "reconciled against pinned CA official action ledger")
    return updates, len(touched)


def run(
    *, zip_path: Path, expected_sha256: str, apply: bool,
    total_timeout_seconds: int = TOTAL_TRANSACTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    actual_sha256 = _sha256(zip_path)
    if actual_sha256.lower() != expected_sha256.lower():
        raise OfficialActionSweepError(f"official ZIP SHA-256 was {actual_sha256}; expected {expected_sha256}")
    official = load_official_actions(zip_path)
    db = get_session()
    try:
        with _total_transaction_timeout(total_timeout_seconds):
            # Must be first: an action reconciliation cannot observe a moving
            # local ledger and then write a conclusion from that mixed snapshot.
            db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": LOCK_NAME})
            bill_by_id, local_actions, session_by_bill_id = _load_local_actions(db, official)
            plan = build_plan(official, local_actions)
            if not apply:
                report = _report(plan, zip_sha256=actual_sha256, apply=False)
                db.rollback()
                return report
            updates, touched = _apply_plan(
                db, plan=plan, bill_by_id=bill_by_id, session_by_bill_id=session_by_bill_id,
                zip_sha256=actual_sha256, now=datetime.now(timezone.utc),
            )
            # Exact rowcount checks above plus the locked, serializable plan
            # establish that this transaction applied every intended mutation.
            # Rehydrating every action a second time through the Railway proxy
            # is not a stronger proof: it pushed a bounded transaction past
            # its 10-minute lifetime. Independently rerun this command in dry
            # mode after commit for the authoritative database reconciliation.
            expected_action_count = len(local_actions) - plan.excess_count + plan.missing_count
            actual_action_count = db.execute(
                select(func.count(BillAction.id)).where(BillAction.bill_id.in_(list(bill_by_id)))
            ).scalar_one()
            if actual_action_count != expected_action_count:
                raise OfficialActionSweepError(
                    f"post-apply CA action count is {actual_action_count}; expected {expected_action_count}"
                )
            report = _report(plan, zip_sha256=actual_sha256, apply=True, updates=updates, touched_bills=touched)
            report.update({
                # This is a locked cardinality proof, not a second full
                # identity/multiplicity reconciliation. Operators must run
                # the documented dry command after commit before claiming an
                # exact post-sweep match.
                "post_scoped_action_count": actual_action_count,
                "post_scoped_action_count_expected": expected_action_count,
                "post_exact_reconciliation": "required_dry_run",
            })
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the locked transaction; rerun without --apply afterward for exact post-commit reconciliation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(zip_path=args.zip, expected_sha256=args.expected_sha256, apply=args.apply)
    except OfficialActionSweepError as exc:
        print(f"CA official action sweep failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
