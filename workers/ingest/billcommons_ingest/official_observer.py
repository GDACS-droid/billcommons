"""Durably observe one reviewed California official action-delta target.

This worker is deliberately an observer: it records exactly what California
published and a replayable comparison with already-local ``ca-history``
actions.  It never applies a reconciliation result to corpus records.

``observe_due_target`` does not commit.  Its caller owns the transaction,
which keeps the ``FOR UPDATE SKIP LOCKED`` claim, raw evidence, observation,
reconciliation records, and retry schedule atomic.  A process crash therefore
leaves the target due for another worker instead of stranding it as running.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import official_ca_actions as ca_actions
from billcommons_schema.models import (
    Bill,
    BillAction,
    Jurisdiction,
    OfficialRawBlob,
    OfficialReconciliationRun,
    OfficialSourceObservation,
    OfficialSourceTarget,
    Session as SessionModel,
)
from billcommons_shared.normalize import normalize_bill_number
from billcommons_shared.reconciliation import ReconciliationInputError, reconcile_events

ADAPTER_NAME = "ca_official_actions"
COMPARATOR_VERSION = "reconcile-events/1"
CA_JURISDICTION = "CA"
CA_HISTORY_PREFIX = "ca-history:"
CA_HISTORY_NAMESPACE = "ca-leginfo-pubinfo-history"
CA_SCOPE_SESSIONS = frozenset({"20252026 regular", "special1"})
MAX_LOCAL_ACTIONS_PER_BILL = 1_000
MAX_BACKOFF_SECONDS = 604_800


class InvalidOfficialTarget(ValueError):
    """A persisted target is not an approved CA adapter target."""


@dataclass(frozen=True)
class OfficialObservationResult:
    """The durable result created by one caller-owned observation transaction."""

    target_id: uuid.UUID
    status: str
    record_count: int | None
    reconciliation_count: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe_error_class(error: BaseException) -> str:
    """Persist a bounded class name, never an upstream response/error message."""

    name = type(error).__name__
    return name[:120] if name else "UnknownError"


def _upstream_updated_at(value: str | None) -> datetime | None:
    """Convert a valid HTTP Last-Modified value without inventing freshness."""

    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _capture_ca_response(day: str):
    """Indirection retained for deterministic tests; production uses the adapter."""

    return ca_actions.fetch_ca_official_actions_response(day)


def _parse_ca_response(captured):
    """Indirection retained for deterministic tests; production uses the adapter."""

    return ca_actions.parse_ca_official_actions_zip(
        captured.raw_bytes,
        source_url=captured.source_url,
        retrieved_at=captured.retrieved_at,
        upstream_modified=captured.upstream_modified,
    )


def _target_day(target: OfficialSourceTarget, jurisdiction: Jurisdiction | None) -> str:
    if jurisdiction is None or jurisdiction.abbreviation != CA_JURISDICTION:
        raise InvalidOfficialTarget("target jurisdiction is not California")
    if target.adapter_name != ADAPTER_NAME:
        raise InvalidOfficialTarget("target adapter is not the CA action adapter")
    if not isinstance(target.scope, Mapping) or set(target.scope) != {"day", "sessions"}:
        raise InvalidOfficialTarget("target scope is not the reviewed CA delta scope")
    day = target.scope.get("day")
    sessions = target.scope.get("sessions")
    if not isinstance(day, str) or not isinstance(sessions, list):
        raise InvalidOfficialTarget("target scope has invalid CA delta fields")
    if len(sessions) != len(CA_SCOPE_SESSIONS) or set(sessions) != CA_SCOPE_SESSIONS:
        raise InvalidOfficialTarget("target scope has unsupported CA sessions")
    try:
        expected_url = ca_actions.ca_delta_url(day)
    except ValueError as exc:
        raise InvalidOfficialTarget("target scope has an unsupported delta day") from exc
    if target.source_url != expected_url:
        raise InvalidOfficialTarget("target URL is not the reviewed CA delta URL")
    return day


def store_official_raw_blob(db: OrmSession, data: bytes, content_type: str) -> str:
    """Content-address bytes and verify the stored record before referencing it."""

    if not isinstance(data, bytes) or not data:
        raise ValueError("official evidence must be non-empty bytes")
    sha256 = hashlib.sha256(data).hexdigest()
    db.execute(
        insert(OfficialRawBlob)
        .values(sha256=sha256, data=data, content_type=content_type)
        .on_conflict_do_nothing(index_elements=(OfficialRawBlob.sha256,))
    )
    stored = db.get(OfficialRawBlob, sha256)
    if stored is None or hashlib.sha256(stored.data).hexdigest() != sha256:
        raise RuntimeError("official raw blob integrity check failed")
    return sha256


# Keep the implementation name available to narrowly-scoped tests while
# discovery and later adapters use the explicit shared persistence helper.
_store_blob = store_official_raw_blob


def _schedule_failure(target: OfficialSourceTarget, now: datetime) -> None:
    failures = target.consecutive_failures + 1
    delay = min(target.cadence_seconds * (2 ** min(failures - 1, 16)), MAX_BACKOFF_SECONDS)
    target.consecutive_failures = failures
    target.next_check_at = now + timedelta(seconds=delay)


def _schedule_success(target: OfficialSourceTarget, now: datetime) -> None:
    target.consecutive_failures = 0
    target.next_check_at = now + timedelta(seconds=target.cadence_seconds)


def _observation_scope(target: OfficialSourceTarget) -> dict[str, Any]:
    # This exact adapter observes a weekday delta, not a full CA history.
    target_scope = dict(target.scope) if isinstance(target.scope, Mapping) else {}
    return {**target_scope, "coverage": "delta_only"}


def _add_observation(
    db: OrmSession,
    *,
    target: OfficialSourceTarget,
    scope: dict[str, Any],
    retrieved_at: datetime,
    status: str,
    raw_sha256: str | None = None,
    upstream_updated_at: datetime | None = None,
    error_class: str | None = None,
    record_count: int | None = None,
) -> OfficialSourceObservation:
    observation = OfficialSourceObservation(
        target_id=target.id,
        adapter_name=ADAPTER_NAME,
        adapter_version=ca_actions.ADAPTER_VERSION,
        source_url=target.source_url,
        scope=scope,
        retrieved_at=retrieved_at,
        upstream_updated_at=upstream_updated_at,
        http_status=200 if raw_sha256 is not None else None,
        raw_sha256=raw_sha256,
        status=status,
        error_class=error_class,
        record_count=record_count,
    )
    db.add(observation)
    db.flush()
    return observation


def _official_events(events, mapping) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for event in events:
        # The archive URL identifies this observation, not an individual
        # historical action.  Leaving it in event evidence would manufacture
        # an uncertainty against older local provenance URLs.
        output.append(
            {
                "occurrence_id": event.occurrence_id,
                "source_identity": event.occurrence_id,
                "source_namespace": CA_HISTORY_NAMESPACE,
                "jurisdiction": CA_JURISDICTION,
                "session": mapping.session_identifier,
                "bill_id": mapping.official_bill_id,
                "description": event.description,
                "date": event.action_date.isoformat() if event.action_date else None,
                "date_precision": "day" if event.action_date else "unknown",
            }
        )
    return output


def _find_local_bill(db: OrmSession, target: OfficialSourceTarget, mapping):
    session_rows = list(
        db.execute(
            select(SessionModel).where(
                SessionModel.jurisdiction_id == target.jurisdiction_id,
                SessionModel.identifier == mapping.session_identifier,
            )
        ).scalars()
    )
    if len(session_rows) != 1:
        return None, "local_session_missing_or_ambiguous"
    try:
        identifier_norm = normalize_bill_number(mapping.identifier)
    except ValueError:
        return None, "official_identifier_invalid"
    bills = list(
        db.execute(
            select(Bill).where(
                Bill.jurisdiction_id == target.jurisdiction_id,
                Bill.session_id == session_rows[0].id,
                Bill.identifier_norm == identifier_norm,
            )
        ).scalars()
    )
    if len(bills) != 1:
        return None, "local_bill_missing_or_ambiguous"
    return bills[0], None


def _local_events(db: OrmSession, bill: Bill, mapping) -> list[dict[str, Any]]:
    actions = list(
        db.execute(
            select(BillAction)
            .where(BillAction.bill_id == bill.id, BillAction.upstream_id.like(f"{CA_HISTORY_PREFIX}%"))
            .order_by(BillAction.action_date.asc().nulls_first(), BillAction.upstream_id.asc())
            .limit(MAX_LOCAL_ACTIONS_PER_BILL + 1)
        ).scalars()
    )
    if len(actions) > MAX_LOCAL_ACTIONS_PER_BILL:
        raise ValueError("local CA history action cap exceeded")
    return [
        {
            "occurrence_id": action.upstream_id,
            "source_identity": action.upstream_id,
            "source_namespace": CA_HISTORY_NAMESPACE,
            "jurisdiction": CA_JURISDICTION,
            "session": mapping.session_identifier,
            "bill_id": mapping.official_bill_id,
            "description": action.description,
            "date": action.action_date.isoformat() if action.action_date else None,
            "date_precision": "day" if action.action_date else "unknown",
        }
        for action in actions
    ]


def _add_partial_run(db: OrmSession, observation: OfficialSourceObservation, official_bill_id: str, now: datetime, reason: str) -> None:
    db.add(
        OfficialReconciliationRun(
            observation_id=observation.id,
            bill_id=None,
            official_bill_id=official_bill_id,
            local_snapshot_at=now,
            comparator_version=COMPARATOR_VERSION,
            status="partial",
            summary={"reason": reason},
            error_class=None,
        )
    )


def _reconcile_batch(
    db: OrmSession,
    *,
    target: OfficialSourceTarget,
    observation: OfficialSourceObservation,
    batch,
    now: datetime,
) -> int:
    run_count = 0
    for official_bill_id in batch.scoped_bill_ids:
        run_count += 1
        try:
            mapping = ca_actions.map_official_bill_id(official_bill_id)
            bill, mapping_error = _find_local_bill(db, target, mapping)
            if mapping_error is not None:
                _add_partial_run(db, observation, official_bill_id, now, mapping_error)
                continue
            official_fixture = {"events": _official_events(batch.events_by_official_bill_id[official_bill_id], mapping)}
            local_fixture = {"events": _local_events(db, bill, mapping)}
            local_sha256 = _store_blob(db, _canonical_json_bytes(local_fixture), "application/json")
            report = reconcile_events(official_fixture, local_fixture)
            diff_sha256 = _store_blob(db, _canonical_json_bytes(report), "application/json")
            db.add(
                OfficialReconciliationRun(
                    observation_id=observation.id,
                    bill_id=bill.id,
                    official_bill_id=official_bill_id,
                    local_snapshot_at=now,
                    comparator_version=COMPARATOR_VERSION,
                    status="completed",
                    summary=report["summary"],
                    local_snapshot_sha256=local_sha256,
                    diff_sha256=diff_sha256,
                )
            )
        except (ValueError, TypeError, ca_actions.OfficialCaActionsError, ReconciliationInputError) as exc:
            # One malformed or oversized comparison cannot discard durable raw
            # evidence for the rest of this successfully parsed observation.
            db.add(
                OfficialReconciliationRun(
                    observation_id=observation.id,
                    bill_id=None,
                    official_bill_id=official_bill_id,
                    local_snapshot_at=now,
                    comparator_version=COMPARATOR_VERSION,
                    status="failed",
                    summary={"reason": "comparison_failed"},
                    error_class=_safe_error_class(exc),
                )
            )
    db.flush()
    return run_count


def observe_due_target(db: OrmSession, *, now: datetime | None = None) -> OfficialObservationResult | None:
    """Observe one due, enabled reviewed CA target without committing.

    Returns ``None`` when no due target was claimable.  Fetch/parse failures
    are durable observations and advance the target's bounded retry schedule;
    unexpected transaction failures still propagate, so the entire claim rolls
    back and another worker can retry it.
    """

    observed_at = _require_aware_utc(now or _utc_now())
    target = db.execute(
        select(OfficialSourceTarget)
        .where(OfficialSourceTarget.enabled.is_(True), OfficialSourceTarget.next_check_at <= observed_at)
        .order_by(OfficialSourceTarget.next_check_at, OfficialSourceTarget.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if target is None:
        return None

    jurisdiction = db.get(Jurisdiction, target.jurisdiction_id)
    try:
        day = _target_day(target, jurisdiction)
    except InvalidOfficialTarget as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_observation_scope(target),
            retrieved_at=observed_at,
            status="invalid",
            error_class=_safe_error_class(exc),
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    try:
        captured = _capture_ca_response(day)
    except Exception as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_observation_scope(target),
            retrieved_at=observed_at,
            status="failed",
            error_class=_safe_error_class(exc),
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    # Validate the evidence identity independently of the adapter helper.
    # This makes a changed injected capture contract fail closed in tests too.
    try:
        if captured.source_url != target.source_url:
            raise ValueError("captured response URL does not match its target")
        raw_sha256 = _store_blob(db, captured.raw_bytes, "application/zip")
        if raw_sha256 != captured.sha256:
            raise ValueError("captured response SHA-256 mismatch")
        parsed = _parse_ca_response(captured)
        if (
            parsed.source_url != target.source_url
            or parsed.sha256 != raw_sha256
            or parsed.raw_bytes != captured.raw_bytes
        ):
            raise ValueError("parsed response does not match captured evidence")
    except Exception as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_observation_scope(target),
            retrieved_at=getattr(captured, "retrieved_at", observed_at),
            status="invalid",
            raw_sha256=raw_sha256 if "raw_sha256" in locals() else None,
            upstream_updated_at=_upstream_updated_at(getattr(captured, "upstream_modified", None)),
            error_class=_safe_error_class(exc),
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    observation = _add_observation(
        db,
        target=target,
        scope=_observation_scope(target),
        retrieved_at=parsed.retrieved_at,
        status="succeeded",
        raw_sha256=raw_sha256,
        upstream_updated_at=_upstream_updated_at(parsed.upstream_modified),
        record_count=parsed.event_count,
    )
    reconciliation_count = _reconcile_batch(
        db,
        target=target,
        observation=observation,
        batch=parsed,
        now=observed_at,
    )
    _schedule_success(target, observed_at)
    return OfficialObservationResult(target.id, observation.status, parsed.event_count, reconciliation_count)
