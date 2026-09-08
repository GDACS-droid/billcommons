"""Durably observe one reviewed California official action-delta target.

This worker is deliberately an observer: it records exactly what California
published and a replayable comparison with already-local ``ca-history``
actions.  It never applies a reconciliation result to corpus records.

``observe_due_target`` does not commit.  Its caller owns the transaction,
which keeps the ``FOR UPDATE SKIP LOCKED`` claim, raw evidence, observation,
reconciliation records, and retry schedule atomic.  A process crash therefore
leaves the target due for another worker instead of stranding it as running.

California archives can contain more bills than one transaction may compare.
While that happens, the target's otherwise-reviewed ``scope`` carries one
strictly validated ``continuation`` object.  It binds an observation, raw
archive hash, parser version, and deterministic bill cursor; it is cleared
only after every archived bill has a run.  This narrowly scoped state avoids a
new schema table while ensuring continuation replays retained bytes rather
than fetching a potentially changed archive.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import official_ca_actions as ca_actions
from billcommons_ingest import official_diagnostics
from billcommons_ingest import official_discovery as discovery
from billcommons_ingest import official_fl_senate_actions as fl_actions
from billcommons_ingest import official_fl_senate_capture as fl_capture
from billcommons_schema.models import (
    Bill,
    BillAction,
    Jurisdiction,
    Organization,
    OfficialRawBlob,
    OfficialReconciliationRun,
    OfficialSourceObservation,
    OfficialSourceTarget,
    Session as SessionModel,
)
from billcommons_shared.normalize import normalize_bill_number
from billcommons_shared.reconciliation import ReconciliationInputError
from billcommons_ingest.official_ca_reconciliation import COMPARATOR_VERSION, reconcile_ca_action_content
from billcommons_ingest.official_fl_senate_reconciliation import (
    COMPARATOR_VERSION as FL_COMPARATOR_VERSION,
    reconcile_fl_senate_action_content,
)

ADAPTER_NAME = "ca_official_actions"
CA_JURISDICTION = "CA"
FL_ADAPTER_NAME = fl_capture.ADAPTER_NAME
FL_JURISDICTION = fl_capture.JURISDICTION
FL_COVERAGE = "bounded_bill_history"
CA_HISTORY_PREFIX = "ca-history:"
CA_HISTORY_NAMESPACE = "ca-leginfo-pubinfo-history"
CA_SCOPE_SESSIONS = frozenset({"20252026 regular", "special1"})
MAX_LOCAL_ACTIONS_PER_BILL = 1_000
FL_REGULAR_SESSION_SUFFIX = " Regular Session"
MAX_RECONCILIATIONS_PER_OBSERVATION = 500
MAX_BACKOFF_SECONDS = 604_800
MAX_BLOB_BYTES = 8 * 1024 * 1024
# The worker arms its non-recoverable transaction SIGALRM before calling this
# module.  Keep this cooperative budget materially below that 300s boundary
# so a CA continuation checkpoint and the caller-owned commit have headroom.
OBSERVATION_WORK_BUDGET_SECONDS = 270.0
OBSERVATION_DEADLINE_SECONDS = OBSERVATION_WORK_BUDGET_SECONDS
DB_STATEMENT_TIMEOUT_MS = 10_000
DB_LOCK_TIMEOUT_MS = 5_000
DB_IDLE_TRANSACTION_TIMEOUT_MS = 240_000
UNKNOWN_ADAPTER_VERSION = "unknown"
CONTINUATION_KEY = "continuation"
_CONTINUATION_FIELDS = frozenset({"observation_id", "raw_sha256", "next_bill_index", "adapter_version"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidOfficialTarget(ValueError):
    """A persisted target is not an approved CA adapter target."""


class ObservationDeadlineExceeded(RuntimeError):
    """One claimed target exceeded its bounded end-to-end observation window."""


@dataclass(frozen=True)
class _CaContinuation:
    observation_id: uuid.UUID
    raw_sha256: str
    next_bill_index: int
    adapter_version: str


@dataclass(frozen=True)
class _ReconciliationProgress:
    next_bill_index: int
    run_count: int
    deadline_reached: bool = False


@dataclass(frozen=True)
class OfficialObservationResult:
    """The durable result created by one caller-owned observation transaction."""

    target_id: uuid.UUID
    status: str
    record_count: int | None
    reconciliation_count: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


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


def _failure_scope(target: OfficialSourceTarget, error: BaseException, *, stage: str) -> dict[str, Any]:
    """Add a public-safe diagnosis to this observation only, never its target."""

    return {**_observation_scope(target), "failure": official_diagnostics.failure_diagnosis(error, stage=stage)}


def _observed_http_status(error: BaseException) -> int | None:
    """Keep a status only when the capture adapter safely observed one."""

    value = getattr(error, "http_status", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None


def _require_deadline(started_at: float) -> None:
    if _monotonic() - started_at > OBSERVATION_WORK_BUDGET_SECONDS:
        raise ObservationDeadlineExceeded("official observation exceeded work budget")


def _configure_transaction(db: OrmSession) -> None:
    """Bound DB work and preserve the target claim across the 180s fetch."""

    db.execute(text(f"SET LOCAL statement_timeout = '{DB_STATEMENT_TIMEOUT_MS}ms'"))
    db.execute(text(f"SET LOCAL lock_timeout = '{DB_LOCK_TIMEOUT_MS}ms'"))
    db.execute(text(f"SET LOCAL idle_in_transaction_session_timeout = '{DB_IDLE_TRANSACTION_TIMEOUT_MS}ms'"))


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


def _capture_fl_senate_detail(source_url: str):
    """Indirection retained for deterministic observer tests."""

    return fl_capture.capture_florida_senate_bill_detail(source_url)


def _target_day(target: OfficialSourceTarget, jurisdiction: Jurisdiction | None) -> str:
    if jurisdiction is None or jurisdiction.abbreviation != CA_JURISDICTION:
        raise InvalidOfficialTarget("target jurisdiction is not California")
    if target.adapter_name != ADAPTER_NAME:
        raise InvalidOfficialTarget("target adapter is not the CA action adapter")
    if not isinstance(target.scope, Mapping) or set(target.scope) - {"day", "sessions", CONTINUATION_KEY}:
        raise InvalidOfficialTarget("target scope is not the reviewed CA delta scope")
    if CONTINUATION_KEY in target.scope:
        _continuation_from_scope(target.scope)
    day = target.scope.get("day")
    sessions = target.scope.get("sessions")
    if not isinstance(day, str) or not isinstance(sessions, list) or not all(isinstance(item, str) for item in sessions):
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


def _target_fl_senate_detail(
    target: OfficialSourceTarget, jurisdiction: Jurisdiction | None,
) -> fl_capture.FloridaSenateDetailScope:
    """Validate the exact reviewed one-bill Florida target before HTTP."""

    if jurisdiction is None or jurisdiction.abbreviation != FL_JURISDICTION:
        raise InvalidOfficialTarget("target jurisdiction is not Florida")
    if target.adapter_name != FL_ADAPTER_NAME:
        raise InvalidOfficialTarget("target adapter is not the Florida Senate action adapter")
    try:
        source_scope = fl_capture.detail_scope(target.source_url)
    except fl_actions.OfficialFloridaSenateActionsError as exc:
        raise InvalidOfficialTarget("target source URL is not an exact Florida Senate detail URL") from exc
    expected = {
        "jurisdiction": FL_JURISDICTION,
        "source_session_year": source_scope.source_session_year,
        "source_bill_number": source_scope.source_bill_number,
        "coverage": FL_COVERAGE,
    }
    if not isinstance(target.scope, Mapping) or target.scope != expected:
        raise InvalidOfficialTarget("target scope is not the exact reviewed Florida bill-history scope")
    return source_scope


def _continuation_from_scope(scope: Mapping[str, Any]) -> _CaContinuation | None:
    """Validate the sole mutable field permitted alongside reviewed policy."""

    if CONTINUATION_KEY not in scope:
        return None
    raw = scope[CONTINUATION_KEY]
    if not isinstance(raw, Mapping) or set(raw) != _CONTINUATION_FIELDS:
        raise InvalidOfficialTarget("CA continuation state has an invalid shape")
    observation_id, raw_sha256, next_bill_index, adapter_version = (
        raw["observation_id"], raw["raw_sha256"], raw["next_bill_index"], raw["adapter_version"]
    )
    if (
        not isinstance(observation_id, str)
        or not isinstance(raw_sha256, str)
        or _SHA256_RE.fullmatch(raw_sha256) is None
        or not isinstance(next_bill_index, int)
        or isinstance(next_bill_index, bool)
        or next_bill_index < 0
        or adapter_version != ca_actions.ADAPTER_VERSION
    ):
        raise InvalidOfficialTarget("CA continuation state has invalid values")
    try:
        parsed_id = uuid.UUID(observation_id)
    except ValueError as exc:
        raise InvalidOfficialTarget("CA continuation observation ID is invalid") from exc
    return _CaContinuation(parsed_id, raw_sha256, next_bill_index, adapter_version)


def _set_continuation(target: OfficialSourceTarget, observation: OfficialSourceObservation, raw_sha256: str, next_bill_index: int) -> None:
    """Persist only an opaque replay cursor; policy fields remain unchanged."""

    target.scope = {
        "day": target.scope["day"],
        "sessions": list(target.scope["sessions"]),
        CONTINUATION_KEY: {
            "observation_id": str(observation.id),
            "raw_sha256": raw_sha256,
            "next_bill_index": next_bill_index,
            "adapter_version": ca_actions.ADAPTER_VERSION,
        },
    }


def _clear_continuation(target: OfficialSourceTarget) -> None:
    target.scope = {"day": target.scope["day"], "sessions": list(target.scope["sessions"])}


def store_official_raw_blob(db: OrmSession, data: bytes, content_type: str) -> str:
    """Content-address bytes and verify the stored record before referencing it."""

    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BLOB_BYTES:
        raise ValueError("official evidence must be non-empty bytes within the storage cap")
    if not isinstance(content_type, str) or not content_type.strip():
        raise ValueError("official evidence content type must be a non-empty string")
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
    if target.adapter_name == ADAPTER_NAME:
        coverage = "delta_only"
    elif target.adapter_name == FL_ADAPTER_NAME:
        coverage = FL_COVERAGE
    else:
        coverage = "not_established"
    result = {**target_scope, "coverage": coverage}
    if target.adapter_name == FL_ADAPTER_NAME:
        # A target or failed attempt does not establish a semantic snapshot.
        result["semantic_label"] = "not_established"
    if len(_canonical_json_bytes(result)) > 60000:
        fallback = {"coverage": coverage, "target_scope_retained_on_target": True}
        if target.adapter_name == FL_ADAPTER_NAME:
            fallback["semantic_label"] = "not_established"
        return fallback
    return result


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
    adapter_name: str = ADAPTER_NAME,
    adapter_version: str = ca_actions.ADAPTER_VERSION,
    http_status: int | None = None,
    source_response_observed: bool = True,
) -> OfficialSourceObservation:
    observation = OfficialSourceObservation(
        target_id=target.id,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        source_url=target.source_url,
        scope=scope,
        retrieved_at=retrieved_at,
        upstream_updated_at=upstream_updated_at,
        # A retained raw hash can be provenance from an earlier source
        # response (CA continuation replay), rather than a response received
        # during this observation attempt.  Never manufacture HTTP 200 there.
        http_status=(
            http_status if http_status is not None
            else 200 if raw_sha256 is not None and source_response_observed
            else None
        ),
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
            .where(BillAction.bill_id == bill.id)
            .order_by(BillAction.action_date.asc().nulls_first(), BillAction.upstream_id.asc())
            .limit(MAX_LOCAL_ACTIONS_PER_BILL + 1)
        ).scalars()
    )
    if len(actions) > MAX_LOCAL_ACTIONS_PER_BILL:
        raise ValueError("local bill action cap exceeded")
    events: list[dict[str, Any]] = []
    for action in actions:
        is_ca_history = isinstance(action.upstream_id, str) and action.upstream_id.startswith(CA_HISTORY_PREFIX)
        events.append(
            {
                # Preserve legacy IDs as evidence references. The versioned CA
                # comparator does not treat them as durable occurrence IDs.
                "occurrence_id": action.upstream_id if is_ca_history else None,
                "source_identity": action.upstream_id if is_ca_history else None,
                "source_namespace": CA_HISTORY_NAMESPACE if is_ca_history else None,
                "jurisdiction": CA_JURISDICTION,
                "session": mapping.session_identifier,
                "bill_id": mapping.official_bill_id,
                "description": action.description,
                "date": action.action_date.isoformat() if action.action_date else None,
                "date_precision": "day" if action.action_date else "unknown",
                "local_record_id": str(action.id),
                "local_source_name": action.source_name,
            }
        )
    return events


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
    started_at: float,
    start_bill_index: int = 0,
) -> _ReconciliationProgress:
    run_count = 0
    scoped_bill_ids = batch.scoped_bill_ids
    if tuple(scoped_bill_ids) != tuple(sorted(scoped_bill_ids)):
        raise ValueError("CA parser did not return deterministic official bill ordering")
    if not 0 <= start_bill_index <= len(scoped_bill_ids):
        raise ValueError("CA continuation cursor is outside the parsed archive")
    stop_bill_index = min(start_bill_index + MAX_RECONCILIATIONS_PER_OBSERVATION, len(scoped_bill_ids))
    for index in range(start_bill_index, stop_bill_index):
        official_bill_id = scoped_bill_ids[index]
        try:
            _require_deadline(started_at)
        except ObservationDeadlineExceeded:
            db.flush()
            return _ReconciliationProgress(index, run_count, deadline_reached=True)
        run_count += 1
        try:
            mapping = ca_actions.map_official_bill_id(official_bill_id)
            bill, mapping_error = _find_local_bill(db, target, mapping)
            if mapping_error is not None:
                _add_partial_run(db, observation, official_bill_id, now, mapping_error)
                continue
            official_fixture = {"events": _official_events(batch.events_by_official_bill_id[official_bill_id], mapping)}
            local_fixture = {"events": _local_events(db, bill, mapping)}
            local_sha256 = store_official_raw_blob(db, _canonical_json_bytes(local_fixture), "application/json")
            report = reconcile_ca_action_content(official_fixture, local_fixture, scope={
                "jurisdiction": CA_JURISDICTION, "session": mapping.session_identifier,
                "bill_id": mapping.official_bill_id,
            })
            diff_sha256 = store_official_raw_blob(db, _canonical_json_bytes(report), "application/json")
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
    return _ReconciliationProgress(stop_bill_index, run_count)


def _finish_or_continue_ca_observation(
    target: OfficialSourceTarget,
    observation: OfficialSourceObservation,
    raw_sha256: str,
    progress: _ReconciliationProgress,
    total_bills: int,
    observed_at: datetime,
) -> str:
    """Advance the cursor atomically with its page's reconciliation writes."""

    if progress.next_bill_index < total_bills:
        _set_continuation(target, observation, raw_sha256, progress.next_bill_index)
        # A page limit is not a successful cadence completion. Resume without
        # refetching the immutable archive; transient transaction errors still
        # roll the cursor back with this page.
        if progress.deadline_reached:
            # The cursor is durable but the local work budget was exhausted;
            # avoid a hot loop while retaining the exact replay point.
            _schedule_failure(target, observed_at)
        else:
            target.next_check_at = observed_at
            target.consecutive_failures = 0
        return "continuing"
    _clear_continuation(target)
    _schedule_success(target, observed_at)
    return "succeeded"


def _resume_ca_continuation(
    db: OrmSession,
    *,
    target: OfficialSourceTarget,
    continuation: _CaContinuation,
    observed_at: datetime,
    started_at: float,
) -> OfficialObservationResult:
    """Replay retained bytes from a durable cursor without an upstream request."""

    observation = db.get(OfficialSourceObservation, continuation.observation_id)
    raw_blob = db.get(OfficialRawBlob, continuation.raw_sha256)
    try:
        if (
            observation is None
            or observation.target_id != target.id
            or observation.status != "succeeded"
            or observation.adapter_name != ADAPTER_NAME
            or observation.adapter_version != continuation.adapter_version
            or observation.source_url != target.source_url
            or observation.raw_sha256 != continuation.raw_sha256
            or raw_blob is None
            or hashlib.sha256(raw_blob.data).hexdigest() != continuation.raw_sha256
        ):
            raise InvalidOfficialTarget("CA continuation evidence does not bind to its target")
        _require_deadline(started_at)
        batch = ca_actions.parse_ca_official_actions_zip(
            raw_blob.data,
            source_url=target.source_url,
            retrieved_at=observation.retrieved_at,
            upstream_modified=None,
        )
        if batch.sha256 != continuation.raw_sha256 or batch.adapter_version != continuation.adapter_version:
            raise InvalidOfficialTarget("CA continuation parser identity differs from retained evidence")
        progress = _reconcile_batch(
            db,
            target=target,
            observation=observation,
            batch=batch,
            now=observed_at,
            started_at=started_at,
            start_bill_index=continuation.next_bill_index,
        )
    except (InvalidOfficialTarget, ValueError, TypeError, ca_actions.OfficialCaActionsError, ObservationDeadlineExceeded) as exc:
        # Preserve the cursor and retained archive for a bounded retry. This
        # separate observation records why the original source is not yet
        # fully reconciled without changing its immutable evidence.
        failure = _add_observation(
            db,
            target=target,
            scope=_failure_scope(target, exc, stage="replay"),
            retrieved_at=observed_at,
            status="failed",
            raw_sha256=continuation.raw_sha256 if raw_blob is not None else None,
            error_class=_safe_error_class(exc),
            source_response_observed=False,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, failure.status, None, 0)

    status = _finish_or_continue_ca_observation(
        target,
        observation,
        continuation.raw_sha256,
        progress,
        len(batch.scoped_bill_ids),
        observed_at,
    )
    return OfficialObservationResult(target.id, status, observation.record_count, progress.run_count)


def _safe_capture_error_class(value: object) -> str:
    """Persist only a bounded capture class label, never capture text."""

    if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,119}", value):
        return value
    return "CaptureFailure"


def _captured_http_status(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None


def _fl_observation_scope(
    source_scope: fl_capture.FloridaSenateDetailScope,
    captured: discovery.OfficialDiscoveryCapture,
    robots_sha256: str | None,
) -> dict[str, Any]:
    """Build bounded semantic evidence for exactly one observed bill page."""

    return {
        "jurisdiction": FL_JURISDICTION,
        "source_session_year": source_scope.source_session_year,
        "source_bill_number": source_scope.source_bill_number,
        "coverage": FL_COVERAGE,
        # This remains deliberately unearned until parsing and snapshot
        # storage both finish. One target or capture attempt says nothing
        # semantic about the source bill, much less the statewide corpus.
        "semantic_label": "not_established",
        "robots": {
            "source_url": f"https://{fl_actions.FL_SENATE_HOST}/robots.txt",
            "http_status": _captured_http_status(captured.robots_status),
            "raw_sha256": robots_sha256,
            "body_bytes": len(captured.robots_bytes) if isinstance(captured.robots_bytes, bytes) else None,
        },
    }


def _fl_snapshot(
    parsed: fl_actions.ParsedFloridaSenateBillHistory,
    source_scope: fl_capture.FloridaSenateDetailScope,
    raw_sha256: str,
) -> bytes:
    """Return canonical one-page fact evidence without a matching claim."""

    snapshot = {
        "schema_version": 1,
        "adapter_version": fl_actions.ADAPTER_VERSION,
        "source_url": parsed.source_url,
        "source_raw_sha256": raw_sha256,
        "scope": {
            "jurisdiction": FL_JURISDICTION,
            "source_session_year": source_scope.source_session_year,
            "source_bill_number": source_scope.source_bill_number,
            "coverage": FL_COVERAGE,
        },
        "bill": {"identifier": parsed.bill_identifier, "title": parsed.bill_title},
        "actions": [action.as_evidence() for action in parsed.actions],
        "interpretation": (
            "Observed Bill History snapshot for one Florida Senate detail URL. "
            "Source row and bullet positions are observation-local evidence, not action occurrence identities; "
            "this snapshot makes no local-session, statewide freshness, or completeness claim."
        ),
    }
    payload = _canonical_json_bytes(snapshot)
    if not 1 <= len(payload) <= MAX_BLOB_BYTES:
        raise ValueError("Florida Senate parsed snapshot violates evidence byte bounds")
    return payload


def _fl_regular_session_identifier(source_session_year: str) -> str:
    """Return the only reviewed local session identity for a Senate source year."""

    if not isinstance(source_session_year, str) or re.fullmatch(r"20\d{2}", source_session_year) is None:
        raise ValueError("Florida Senate source session year is invalid")
    return f"{source_session_year}{FL_REGULAR_SESSION_SUFFIX}"


def _fl_official_bill_id(parsed: fl_actions.ParsedFloridaSenateBillHistory) -> str:
    """Keep the source year and parsed bill identifier as the run identity."""

    return f"fl-senate:{parsed.session_year}:{parsed.bill_identifier}"


def _fl_official_events(
    parsed: fl_actions.ParsedFloridaSenateBillHistory,
    *,
    session_identifier: str,
) -> list[dict[str, Any]]:
    return [
        {
            "jurisdiction": FL_JURISDICTION,
            "session": session_identifier,
            "bill_id": parsed.bill_identifier,
            "date": action.action_date.isoformat(),
            "date_precision": action.date_precision,
            "description": action.description,
            # These positions locate facts in this captured page only. The
            # Florida comparator retains them as evidence and never uses them
            # as an occurrence key across snapshots.
            "chamber": action.chamber,
            "source_row_position": action.source_row_position,
            "source_bullet_position": action.source_bullet_position,
        }
        for action in parsed.actions
    ]


def _fl_local_identifier(parsed_bill_identifier: str) -> str:
    """Map only the parser's anchored CS/ prefix form to the base local bill."""

    match = re.fullmatch(r"(?:CS/)*(HB|SB) ([1-9]\d*)", parsed_bill_identifier)
    if match is None:
        raise ValueError("parsed Florida bill identifier has unsupported local mapping shape")
    return f"{match.group(1)} {match.group(2)}"


def _find_fl_local_bill(
    db: OrmSession,
    target: OfficialSourceTarget,
    parsed: fl_actions.ParsedFloridaSenateBillHistory,
) -> tuple[Bill | None, str | None, str]:
    """Resolve only the reviewed regular-session identity; never fall back by bill number."""

    session_identifier = _fl_regular_session_identifier(parsed.session_year)
    sessions = list(
        db.execute(
            select(SessionModel).where(
                SessionModel.jurisdiction_id == target.jurisdiction_id,
                SessionModel.identifier == session_identifier,
                SessionModel.classification == "regular",
            )
        ).scalars()
    )
    if len(sessions) != 1:
        return None, "local_fl_regular_session_missing_or_ambiguous", session_identifier
    try:
        identifier_norm = normalize_bill_number(_fl_local_identifier(parsed.bill_identifier))
    except ValueError:
        return None, "official_fl_identifier_invalid", session_identifier
    bills = list(
        db.execute(
            select(Bill).where(
                Bill.jurisdiction_id == target.jurisdiction_id,
                Bill.session_id == sessions[0].id,
                Bill.identifier_norm == identifier_norm,
            )
        ).scalars()
    )
    if len(bills) != 1:
        return None, "local_fl_bill_missing_or_ambiguous", session_identifier
    return bills[0], None, session_identifier


def _fl_local_events(
    db: OrmSession,
    bill: Bill,
    *,
    jurisdiction_id: uuid.UUID,
    session_identifier: str,
    bill_identifier: str,
) -> list[dict[str, Any]]:
    """Snapshot local actions and exact same-jurisdiction chamber evidence in one query."""

    rows = list(
        db.execute(
            select(BillAction, Organization)
            .outerjoin(Organization, BillAction.organization_id == Organization.id)
            .where(BillAction.bill_id == bill.id)
            .order_by(BillAction.action_date.asc().nulls_first(), BillAction.upstream_id.asc())
            .limit(MAX_LOCAL_ACTIONS_PER_BILL + 1)
        ).all()
    )
    if len(rows) > MAX_LOCAL_ACTIONS_PER_BILL:
        raise ValueError("local bill action cap exceeded")
    events: list[dict[str, Any]] = []
    for action, organization in rows:
        if action.organization_id is not None:
            if organization is None:
                raise ValueError("local Florida action organization is missing")
            if organization.jurisdiction_id != jurisdiction_id:
                raise ValueError("local Florida action organization belongs to another jurisdiction")
            chamber = {("House", "lower"): "House", ("Senate", "upper"): "Senate"}.get(
                (organization.name, organization.classification)
            )
        else:
            chamber = None
        events.append(
            {
                "jurisdiction": FL_JURISDICTION,
                "session": session_identifier,
                "bill_id": bill_identifier,
                "date": action.action_date.isoformat() if action.action_date else None,
                "date_precision": "day" if action.action_date else "unknown",
                # Do not infer a chamber from the bill. A missing or
                # non-chamber organization remains explicit ambiguous input.
                "chamber": chamber,
                "description": action.description,
                "local_record_id": str(action.id),
                "local_source_name": action.source_name,
                "local_upstream_id": action.upstream_id,
            }
        )
    return events


def _reconcile_fl_history(
    db: OrmSession,
    *,
    target: OfficialSourceTarget,
    observation: OfficialSourceObservation,
    parsed: fl_actions.ParsedFloridaSenateBillHistory,
    now: datetime,
    started_at: float,
) -> int:
    """Store one replayable Florida content comparison or an explicit partial run."""

    official_bill_id = _fl_official_bill_id(parsed)
    try:
        _require_deadline(started_at)
        bill, mapping_error, session_identifier = _find_fl_local_bill(db, target, parsed)
        if mapping_error is not None:
            _add_fl_partial_run(db, observation, official_bill_id, now, mapping_error)
            return 1
        assert bill is not None
        official_fixture = {"events": _fl_official_events(parsed, session_identifier=session_identifier)}
        local_fixture = {"events": _fl_local_events(
            db, bill, jurisdiction_id=target.jurisdiction_id,
            session_identifier=session_identifier, bill_identifier=parsed.bill_identifier,
        )}
        local_sha256 = store_official_raw_blob(db, _canonical_json_bytes(local_fixture), "application/json")
        report = reconcile_fl_senate_action_content(
            official_fixture,
            local_fixture,
            scope={"jurisdiction": FL_JURISDICTION, "session": session_identifier, "bill_id": parsed.bill_identifier},
        )
        _require_deadline(started_at)
        diff_sha256 = store_official_raw_blob(db, _canonical_json_bytes(report), "application/json")
        db.add(
            OfficialReconciliationRun(
                observation_id=observation.id,
                bill_id=bill.id,
                official_bill_id=official_bill_id,
                local_snapshot_at=now,
                comparator_version=FL_COMPARATOR_VERSION,
                status="completed",
                summary=report["summary"],
                local_snapshot_sha256=local_sha256,
                diff_sha256=diff_sha256,
            )
        )
    except (ValueError, TypeError, ReconciliationInputError, ObservationDeadlineExceeded) as exc:
        db.add(
            OfficialReconciliationRun(
                observation_id=observation.id,
                bill_id=None,
                official_bill_id=official_bill_id,
                local_snapshot_at=now,
                comparator_version=FL_COMPARATOR_VERSION,
                status="failed",
                summary={"reason": "comparison_failed"},
                error_class=_safe_error_class(exc),
            )
        )
    return 1


def _add_fl_partial_run(
    db: OrmSession,
    observation: OfficialSourceObservation,
    official_bill_id: str,
    now: datetime,
    reason: str,
) -> None:
    db.add(
        OfficialReconciliationRun(
            observation_id=observation.id,
            bill_id=None,
            official_bill_id=official_bill_id,
            local_snapshot_at=now,
            comparator_version=FL_COMPARATOR_VERSION,
            status="partial",
            summary={"reason": reason},
            error_class=None,
        )
    )


def _observe_fl_senate_target(
    db: OrmSession,
    target: OfficialSourceTarget,
    jurisdiction: Jurisdiction | None,
    observed_at: datetime,
    started_at: float,
) -> OfficialObservationResult:
    """Observe one exact FL detail target; never find or mutate a local bill."""

    try:
        source_scope = _target_fl_senate_detail(target, jurisdiction)
    except InvalidOfficialTarget as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_failure_scope(target, exc, stage="evidence_validation"),
            retrieved_at=observed_at,
            status="invalid",
            error_class=_safe_error_class(exc),
            adapter_name=FL_ADAPTER_NAME,
            adapter_version=fl_actions.ADAPTER_VERSION,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    try:
        captured = _capture_fl_senate_detail(source_scope.source_url)
        if captured.source_url != source_scope.source_url:
            raise ValueError("captured Florida Senate response URL differs from target")
        retrieved_at = _require_aware_utc(captured.retrieved_at)
        _require_deadline(started_at)
    except Exception as exc:
        observation = _add_observation(
            db,
            target=target,
            scope={**_observation_scope(target), "failure": official_diagnostics.failure_diagnosis(exc, stage="capture")},
            retrieved_at=observed_at,
            status="failed",
            error_class=_safe_error_class(exc),
            adapter_name=FL_ADAPTER_NAME,
            adapter_version=fl_actions.ADAPTER_VERSION,
            http_status=_observed_http_status(exc),
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    # Persist capture bytes before acting on response status or parser output.
    # Storage failures intentionally remain outside outcome handlers so the
    # caller rolls the target claim, raw evidence, and schedule back together.
    raw_bytes = captured.raw_bytes
    robots_bytes = captured.robots_bytes
    try:
        if raw_bytes is not None and (not isinstance(raw_bytes, bytes) or not 1 <= len(raw_bytes) <= MAX_BLOB_BYTES):
            raise ValueError("Florida Senate raw response violates evidence byte bounds")
    except (AttributeError, TypeError, ValueError) as exc:
        observation = _add_observation(
            db,
            target=target,
            scope={**_observation_scope(target), "failure": official_diagnostics.failure_diagnosis(exc, stage="capture")},
            retrieved_at=retrieved_at,
            status="invalid",
            error_class=_safe_error_class(exc),
            adapter_name=FL_ADAPTER_NAME,
            adapter_version=fl_actions.ADAPTER_VERSION,
            http_status=_captured_http_status(captured.http_status),
            source_response_observed=_captured_http_status(captured.http_status) is not None,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    raw_sha256 = store_official_raw_blob(db, raw_bytes, "text/html") if raw_bytes else None
    try:
        # An empty robots.txt is a valid allow-all policy. It cannot be stored
        # in OfficialRawBlob because that shared table intentionally rejects
        # empty payloads, so retain its zero-byte fact in scope with no hash.
        if robots_bytes is not None and (not isinstance(robots_bytes, bytes) or len(robots_bytes) > MAX_BLOB_BYTES):
            raise ValueError("Florida Senate robots response violates evidence byte bounds")
    except (AttributeError, TypeError, ValueError) as exc:
        scope = _fl_observation_scope(source_scope, captured, None)
        scope["failure"] = official_diagnostics.failure_diagnosis(exc, stage="capture")
        observation = _add_observation(
            db,
            target=target,
            scope=scope,
            retrieved_at=retrieved_at,
            status="invalid",
            raw_sha256=raw_sha256,
            upstream_updated_at=_upstream_updated_at(captured.upstream_modified),
            error_class=_safe_error_class(exc),
            adapter_name=FL_ADAPTER_NAME,
            adapter_version=fl_actions.ADAPTER_VERSION,
            http_status=_captured_http_status(captured.http_status),
            source_response_observed=_captured_http_status(captured.http_status) is not None,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    robots_sha256 = store_official_raw_blob(db, robots_bytes, "text/plain") if robots_bytes else None
    scope = _fl_observation_scope(source_scope, captured, robots_sha256)
    http_status = _captured_http_status(captured.http_status)
    if captured.error_class is not None:
        scope["failure"] = {
            "version": official_diagnostics.VERSION,
            "stage": "capture",
            "code": "adapter_failure",
            "recommended_action": "inspect_adapter_failure",
        }
        observation = _add_observation(
            db,
            target=target,
            scope=scope,
            retrieved_at=retrieved_at,
            status="failed",
            raw_sha256=raw_sha256,
            upstream_updated_at=_upstream_updated_at(captured.upstream_modified),
            error_class=_safe_capture_error_class(captured.error_class),
            adapter_name=FL_ADAPTER_NAME,
            adapter_version=fl_actions.ADAPTER_VERSION,
            http_status=http_status,
            source_response_observed=http_status is not None,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)
    if raw_bytes is None:
        # A successful semantic observation cannot be constructed without the
        # exact response bytes, even if an adapter implementation regresses.
        observation = _add_observation(
            db,
            target=target,
            scope={**scope, "failure": official_diagnostics.failure_diagnosis(ValueError(), stage="capture")},
            retrieved_at=retrieved_at,
            status="invalid",
            error_class="CaptureFailure",
            adapter_name=FL_ADAPTER_NAME,
            adapter_version=fl_actions.ADAPTER_VERSION,
            http_status=http_status,
            source_response_observed=http_status is not None,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    try:
        parsed = fl_actions.parse_florida_senate_bill_history(raw_bytes, source_url=source_scope.source_url)
        _require_deadline(started_at)
        if (
            parsed.source_url != source_scope.source_url
            or parsed.source_sha256 != raw_sha256
            or parsed.session_year != source_scope.source_session_year
            or parsed.bill_number != source_scope.source_bill_number
        ):
            raise ValueError("parsed Florida Senate result differs from captured one-bill evidence")
        snapshot = _fl_snapshot(parsed, source_scope, raw_sha256)
    except (AttributeError, TypeError, ValueError, fl_actions.OfficialFloridaSenateActionsError, ObservationDeadlineExceeded) as exc:
        observation = _add_observation(
            db,
            target=target,
            scope={**scope, "failure": official_diagnostics.failure_diagnosis(exc, stage="parse")},
            retrieved_at=retrieved_at,
            status="invalid",
            raw_sha256=raw_sha256,
            upstream_updated_at=_upstream_updated_at(captured.upstream_modified),
            error_class=_safe_error_class(exc),
            adapter_name=FL_ADAPTER_NAME,
            adapter_version=fl_actions.ADAPTER_VERSION,
            http_status=http_status,
            source_response_observed=http_status is not None,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    snapshot_sha256 = store_official_raw_blob(db, snapshot, "application/json")
    scope["parsed_snapshot_sha256"] = snapshot_sha256
    # This label is intentionally narrower than freshness or corpus coverage:
    # one retained page says nothing about another Florida bill.
    scope["semantic_label"] = "observed_bill_history_snapshot"
    observation = _add_observation(
        db,
        target=target,
        scope=scope,
        retrieved_at=retrieved_at,
        status="succeeded",
        raw_sha256=raw_sha256,
        upstream_updated_at=_upstream_updated_at(captured.upstream_modified),
        record_count=len(parsed.actions),
        adapter_name=FL_ADAPTER_NAME,
        adapter_version=fl_actions.ADAPTER_VERSION,
        http_status=http_status,
        source_response_observed=http_status is not None,
    )
    reconciliation_count = _reconcile_fl_history(
        db,
        target=target,
        observation=observation,
        parsed=parsed,
        now=observed_at,
        started_at=started_at,
    )
    _schedule_success(target, observed_at)
    return OfficialObservationResult(target.id, observation.status, observation.record_count, reconciliation_count)


def _observe_discovery_target(db, target, jurisdiction, observed_at) -> OfficialObservationResult:
    expected_scope = {
        "jurisdiction": jurisdiction.abbreviation if jurisdiction else None,
        "inventory_version": discovery.INVENTORY_VERSION,
        "coverage": "bounded_link_discovery",
    }
    try:
        if jurisdiction is None or target.scope != expected_scope:
            raise InvalidOfficialTarget("discovery scope differs from reviewed inventory")
        captured = discovery.capture_official_landing_page(jurisdiction.abbreviation, target.source_url)
    except Exception as exc:
        observation = _add_observation(db, target=target, scope={"coverage": "not_established"},
            retrieved_at=observed_at, status="invalid", error_class=_safe_error_class(exc),
            adapter_name=discovery.ADAPTER_NAME, adapter_version=discovery.ADAPTER_VERSION)
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    # Storage errors deliberately escape: no source outcome is allowed to
    # commit with missing evidence or an aborted transaction.
    raw_sha = (store_official_raw_blob(db, captured.raw_bytes, captured.content_type or "application/octet-stream")
               if captured.raw_bytes else None)
    robots_sha = (store_official_raw_blob(db, captured.robots_bytes, "text/plain")
                  if captured.robots_bytes else None)
    scope = {**expected_scope,
        "robots": {"source_url": captured.robots_url, "http_status": captured.robots_status,
                   "raw_sha256": robots_sha,
                   "body_bytes": len(captured.robots_bytes) if captured.robots_bytes is not None else None},
        "candidate_links": [vars(link) for link in captured.links], "truncated": captured.truncated,
        "legislative_freshness": "not_established"}
    if captured.error_class is None:
        previous = db.scalar(select(OfficialSourceObservation).where(
            OfficialSourceObservation.target_id == target.id,
            OfficialSourceObservation.status == "succeeded",
        ).order_by(OfficialSourceObservation.retrieved_at.desc(), OfficialSourceObservation.id.desc()).limit(1))
        old_links = {link["url"]: link for link in previous.scope.get("candidate_links", [])} if previous else {}
        new_links = {link.url: vars(link) for link in captured.links}
        diff = {
            "comparator_version": "official-link-set/1", "scope": "bounded_same_origin_links",
            "previous_observation_id": str(previous.id) if previous else None,
            "previous_source_sha256": previous.raw_sha256 if previous else None,
            "current_source_sha256": raw_sha,
            "newly_observed_links": [new_links[url] for url in sorted(new_links.keys() - old_links.keys())],
            "not_observed_this_time": [old_links[url] for url in sorted(old_links.keys() - new_links.keys())],
            "truncated_input": captured.truncated or bool(previous and previous.scope.get("truncated")),
            "absence_is_not_removal": True,
        }
        scope["discovery_diff_sha256"] = store_official_raw_blob(db, _canonical_json_bytes(diff), "application/json")
        scope["previous_observation_id"] = str(previous.id) if previous else None
        scope["content_changed"] = (previous.raw_sha256 != raw_sha) if previous else None
    succeeded = captured.error_class is None
    observation = _add_observation(db, target=target, scope=scope,
        retrieved_at=captured.retrieved_at, status="succeeded" if succeeded else "failed",
        raw_sha256=raw_sha, upstream_updated_at=_upstream_updated_at(captured.upstream_modified),
        error_class=captured.error_class, record_count=len(captured.links) if succeeded else None,
        adapter_name=discovery.ADAPTER_NAME, adapter_version=discovery.ADAPTER_VERSION,
        http_status=captured.http_status)
    if succeeded:
        _schedule_success(target, observed_at)
    else:
        _schedule_failure(target, observed_at)
    return OfficialObservationResult(target.id, observation.status, observation.record_count, 0)


def observe_due_target(db: OrmSession, *, now: datetime | None = None) -> OfficialObservationResult | None:
    """Observe one due, enabled reviewed CA target without committing.

    Returns ``None`` when no due target was claimable.  Fetch/parse failures
    are durable observations and advance the target's bounded retry schedule;
    unexpected transaction failures still propagate, so the entire claim rolls
    back and another worker can retry it.
    """

    started_at = _monotonic()
    observed_at = _require_aware_utc(now or _utc_now())
    _configure_transaction(db)
    _require_deadline(started_at)
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
    if target.adapter_name == discovery.ADAPTER_NAME:
        return _observe_discovery_target(db, target, jurisdiction, observed_at)
    if target.adapter_name == FL_ADAPTER_NAME:
        return _observe_fl_senate_target(db, target, jurisdiction, observed_at, started_at)
    try:
        day = _target_day(target, jurisdiction)
    except InvalidOfficialTarget as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_failure_scope(target, exc, stage="evidence_validation"),
            retrieved_at=observed_at,
            status="invalid",
            error_class=_safe_error_class(exc),
            adapter_name=target.adapter_name,
            adapter_version=UNKNOWN_ADAPTER_VERSION,
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    continuation = _continuation_from_scope(target.scope)
    if continuation is not None:
        return _resume_ca_continuation(
            db,
            target=target,
            continuation=continuation,
            observed_at=observed_at,
            started_at=started_at,
        )

    try:
        captured = _capture_ca_response(day)
    except Exception as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_failure_scope(target, exc, stage="capture"),
            retrieved_at=observed_at,
            status="failed",
            error_class=_safe_error_class(exc),
            http_status=_observed_http_status(exc),
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    # Validate capture metadata before a DB write. Database persistence
    # failures intentionally propagate and roll back the target claim.
    try:
        if captured.source_url != target.source_url:
            raise ValueError("captured response URL does not match its target")
        if (
            not isinstance(captured.raw_bytes, bytes)
            or not 1 <= len(captured.raw_bytes) <= MAX_BLOB_BYTES
            or not isinstance(captured.sha256, str)
            or hashlib.sha256(captured.raw_bytes).hexdigest() != captured.sha256
        ):
            raise ValueError("captured response SHA-256 mismatch")
        _require_deadline(started_at)
    except (AttributeError, TypeError, ValueError, ObservationDeadlineExceeded) as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_failure_scope(target, exc, stage="evidence_validation"),
            retrieved_at=getattr(captured, "retrieved_at", observed_at),
            status="invalid",
            error_class=_safe_error_class(exc),
        )
        _schedule_failure(target, observed_at)
        return OfficialObservationResult(target.id, observation.status, None, 0)

    raw_sha256 = store_official_raw_blob(db, captured.raw_bytes, "application/zip")
    # Parsing malformed bytes is an expected durable invalid observation;
    # storage and transaction errors remain outside this handler.
    try:
        parsed = _parse_ca_response(captured)
        _require_deadline(started_at)
        if (
            parsed.source_url != target.source_url
            or parsed.sha256 != raw_sha256
            or parsed.raw_bytes != captured.raw_bytes
        ):
            raise ValueError("parsed response does not match captured evidence")
    except (AttributeError, TypeError, ValueError, ca_actions.OfficialCaActionsError, ObservationDeadlineExceeded) as exc:
        observation = _add_observation(
            db,
            target=target,
            scope=_failure_scope(target, exc, stage="parse"),
            retrieved_at=getattr(captured, "retrieved_at", observed_at),
            status="invalid",
            raw_sha256=raw_sha256,
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
    progress = _reconcile_batch(
        db,
        target=target,
        observation=observation,
        batch=parsed,
        now=observed_at,
        started_at=started_at,
    )
    status = _finish_or_continue_ca_observation(
        target,
        observation,
        raw_sha256,
        progress,
        len(parsed.scoped_bill_ids),
        observed_at,
    )
    return OfficialObservationResult(target.id, status, parsed.event_count, progress.run_count)
