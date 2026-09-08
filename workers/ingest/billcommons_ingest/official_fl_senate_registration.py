"""Reviewed registration and one-time activation for Florida Senate evidence.

This module owns only the narrow, reviewed Florida Senate bill-history target.
It deliberately validates the complete existing official-target inventory before
adding the fifty-ninth row, and it never commits the caller's transaction or
looks up a corpus bill.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from billcommons_ingest import official_ca_actions as ca_actions
from billcommons_ingest import official_discovery as discovery
from billcommons_ingest import official_fl_senate_capture as fl_capture
from billcommons_schema.models import (
    Jurisdiction,
    OfficialSourceObservation,
    OfficialSourceTarget,
)


REGISTRATION_ADVISORY_LOCK_SQL = (
    "SELECT pg_try_advisory_xact_lock("
    "hashtext('billcommons-reviewed-official-registration'))"
)
CADENCE_SECONDS = 86_400
FLORIDA_SOURCE_URL = "https://www.flsenate.gov/Session/Bill/2025/7031"
FLORIDA_SCOPE = {
    "jurisdiction": "FL",
    "source_session_year": "2025",
    "source_bill_number": "7031",
    "coverage": "bounded_bill_history",
}
CA_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CA_SCOPE_SESSIONS = ["20252026 regular", "special1"]


class FloridaSenateRegistrationError(ValueError):
    """The durable official-source registry is not the reviewed inventory."""


class FloridaSenateActivationError(FloridaSenateRegistrationError):
    """The reviewed Florida target cannot safely be enabled."""


@dataclass(frozen=True)
class FloridaSenateRegistrationResult:
    """The target identity and reviewed scope returned by registration."""

    initial_target_count: int
    target_count: int
    target_id: UUID
    jurisdiction_id: UUID
    scope: Mapping[str, str]


@dataclass(frozen=True)
class FloridaSenateActivationResult:
    """The target identity and reviewed scope returned by activation."""

    initial_target_count: int
    target_count: int
    target_id: UUID
    jurisdiction_id: UUID
    scope: Mapping[str, str]


def _require_aware_utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise FloridaSenateActivationError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _expected_initial_targets() -> dict[tuple[str, str], tuple[str, dict[str, object]]]:
    expected: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}
    for abbreviation, source_url in discovery.official_source_inventory().items():
        expected[(discovery.ADAPTER_NAME, source_url)] = (
            abbreviation,
            {
                "jurisdiction": abbreviation,
                "inventory_version": discovery.INVENTORY_VERSION,
                "coverage": "bounded_link_discovery",
            },
        )
    for day in CA_DAYS:
        source_url = ca_actions.ca_delta_url(day)
        expected[("ca_official_actions", source_url)] = (
            "CA",
            {"day": day, "sessions": list(CA_SCOPE_SESSIONS)},
        )
    if len(expected) != 58:
        raise FloridaSenateRegistrationError("reviewed initial inventory must contain exactly 58 targets")
    return expected


def _expected_florida_target() -> tuple[tuple[str, str], tuple[str, dict[str, object]]]:
    source_scope = fl_capture.detail_scope(FLORIDA_SOURCE_URL)
    if source_scope.source_url != FLORIDA_SOURCE_URL:
        raise FloridaSenateRegistrationError("reviewed Florida source URL is not canonical")
    return (
        (fl_capture.ADAPTER_NAME, source_scope.source_url),
        ("FL", dict(FLORIDA_SCOPE)),
    )


def _lock_registry(db: Session) -> None:
    if db.execute(text(REGISTRATION_ADVISORY_LOCK_SQL)).scalar_one() is not True:
        raise FloridaSenateRegistrationError("another reviewed official registration holds the advisory lock")


def _jurisdictions_by_abbreviation(db: Session, abbreviations: set[str]) -> dict[str, Jurisdiction]:
    rows = db.scalars(
        select(Jurisdiction).where(Jurisdiction.abbreviation.in_(tuple(abbreviations)))
    ).all()
    jurisdictions = {row.abbreviation: row for row in rows}
    if len(rows) != len(jurisdictions) or set(jurisdictions) != abbreviations:
        raise FloridaSenateRegistrationError("reviewed target jurisdictions must exist exactly once")
    return jurisdictions


def _validate_inventory(
    db: Session, *, include_florida: bool, require_florida_disabled: bool,
) -> tuple[dict[tuple[str, str], OfficialSourceTarget], dict[str, Jurisdiction]]:
    expected = _expected_initial_targets()
    if include_florida:
        florida_key, florida_value = _expected_florida_target()
        expected[florida_key] = florida_value

    targets = db.scalars(select(OfficialSourceTarget)).all()
    actual_keys = Counter((target.adapter_name, target.source_url) for target in targets)
    expected_keys = Counter(expected.keys())
    if len(targets) != len(expected) or actual_keys != expected_keys:
        raise FloridaSenateRegistrationError("official target inventory differs from the reviewed registry")

    jurisdictions = _jurisdictions_by_abbreviation(
        db, {abbreviation for abbreviation, _scope in expected.values()}
    )
    targets_by_key = {(target.adapter_name, target.source_url): target for target in targets}
    for key, (abbreviation, scope) in expected.items():
        target = targets_by_key[key]
        jurisdiction = jurisdictions[abbreviation]
        if target.jurisdiction_id != jurisdiction.id:
            raise FloridaSenateRegistrationError("official target jurisdiction differs from the reviewed registry")
        if target.scope != scope:
            raise FloridaSenateRegistrationError("official target scope differs from the reviewed registry")
        if target.cadence_seconds != CADENCE_SECONDS:
            raise FloridaSenateRegistrationError("official target cadence differs from the reviewed registry")
        is_florida = include_florida and key == _expected_florida_target()[0]
        if target.enabled and (not is_florida or require_florida_disabled):
            raise FloridaSenateRegistrationError("reviewed official targets must be disabled before registration")
    return targets_by_key, jurisdictions


def _result(result_type, target: OfficialSourceTarget, *, initial_target_count: int) -> FloridaSenateRegistrationResult | FloridaSenateActivationResult:
    return result_type(
        initial_target_count=initial_target_count,
        target_count=59,
        target_id=target.id,
        jurisdiction_id=target.jurisdiction_id,
        scope=MappingProxyType(dict(FLORIDA_SCOPE)),
    )


def _require_unobserved(db: Session, target: OfficialSourceTarget) -> None:
    observed = db.scalar(
        select(OfficialSourceObservation.id)
        .where(OfficialSourceObservation.target_id == target.id)
        .limit(1)
    )
    if observed is not None:
        raise FloridaSenateRegistrationError("reviewed Florida target was previously observed")


def register_reviewed_fl_senate_target(db: Session) -> FloridaSenateRegistrationResult:
    """Add the exact reviewed Florida target, disabled, in the caller transaction.

    An exact pre-existing, disabled target is returned unchanged.  Every other
    registry shape is rejected; the function never repairs or replaces rows.
    """

    _lock_registry(db)
    existing = db.scalars(select(OfficialSourceTarget)).all()
    if len(existing) == 58:
        _targets, jurisdictions = _validate_inventory(
            db, include_florida=False, require_florida_disabled=True
        )
        florida = jurisdictions["FL"]
        target = OfficialSourceTarget(
            jurisdiction_id=florida.id,
            adapter_name=fl_capture.ADAPTER_NAME,
            source_url=FLORIDA_SOURCE_URL,
            scope=dict(FLORIDA_SCOPE),
            enabled=False,
            cadence_seconds=CADENCE_SECONDS,
        )
        db.add(target)
        db.flush()
    elif len(existing) == 59:
        targets, _jurisdictions = _validate_inventory(
            db, include_florida=True, require_florida_disabled=True
        )
        target = targets[_expected_florida_target()[0]]
    else:
        raise FloridaSenateRegistrationError("official target inventory must contain exactly 58 or 59 targets")

    _require_unobserved(db, target)
    return _result(FloridaSenateRegistrationResult, target, initial_target_count=len(existing))


def activate_reviewed_fl_senate_target(
    db: Session, *, now: datetime,
) -> FloridaSenateActivationResult:
    """Enable one unobserved Florida target at an explicit UTC time.

    Only the Florida target itself receives a row lock.  The other reviewed
    rows are inspected without locks and must remain disabled.
    """

    activated_at = _require_aware_utc(now)
    _lock_registry(db)
    targets, _jurisdictions = _validate_inventory(
        db, include_florida=True, require_florida_disabled=False
    )
    florida_key = _expected_florida_target()[0]
    target_id = targets[florida_key].id
    target = db.scalar(
        select(OfficialSourceTarget)
        .where(OfficialSourceTarget.id == target_id)
        .with_for_update()
    )
    if target is None:
        raise FloridaSenateActivationError("reviewed Florida target disappeared before activation")
    if target.enabled:
        raise FloridaSenateActivationError("reviewed Florida target is already enabled")
    try:
        _require_unobserved(db, target)
    except FloridaSenateRegistrationError as exc:
        raise FloridaSenateActivationError("reviewed Florida target was previously observed") from exc
    target.enabled = True
    target.next_check_at = activated_at
    db.flush()
    return _result(FloridaSenateActivationResult, target, initial_target_count=59)
