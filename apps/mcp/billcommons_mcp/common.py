"""Shared helpers for MCP tool implementations: coverage checks, canonical
JSON shaping (ids, official source URLs, freshness), and small serializers.

Every tool consults `jurisdiction_coverage` before answering substantively.
If coverage for the queried jurisdiction/session is BOOTSTRAPPED or worse
(i.e. not at least METADATA_SEARCHABLE), tools attach a structured
`coverage_warning` rather than silently returning empty/hallucinated results.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from billcommons_schema.models import Jurisdiction, JurisdictionCoverage, Session as SessionModel

# Coverage states in state-machine order (see ARCHITECTURE.md / SPEC.md).
#
# NOTE: this is LIFECYCLE order, not severity. DEGRADED and BLOCKED are
# terminal fault states appended after GREEN, so a jurisdiction can be at
# GREEN and then fall to DEGRADED -- position here says nothing about how
# trustworthy the data is. Use COVERAGE_SEVERITY for that; see the comment
# on it.
COVERAGE_STATES = [
    "NOT_STARTED",
    "SOURCE_IDENTIFIED",
    "BOOTSTRAPPED",
    "METADATA_SEARCHABLE",
    "FULL_TEXT_SEARCHABLE",
    "VALIDATING",
    "GREEN",
    "DEGRADED",
    "BLOCKED",
]

# How much the data can be TRUSTED, lowest first. Distinct from COVERAGE_STATES
# on purpose.
#
# Ranking severity by position in COVERAGE_STATES silently disabled the coverage
# warning. DEGRADED (index 7) and BLOCKED (index 8) sit after GREEN (index 6),
# so `min(..., key=COVERAGE_STATES.index)` -- documented as "least advanced" --
# scored them as MORE advanced than GREEN. Two consequences, both live:
#
#   * A wholly DEGRADED jurisdiction cleared the METADATA_SEARCHABLE threshold
#     and returned no warning at all. Massachusetts was in exactly this state.
#   * A BLOCKED session was masked by any non-BLOCKED sibling row, because the
#     GREEN row won the min().
#
# That is the precise failure this warning exists to prevent: a caller reading
# an empty or thin result as "no such legislation exists" rather than "we do
# not have this yet". DEGRADED ranks below METADATA_SEARCHABLE so it always
# warns; BLOCKED ranks lowest so it can never be masked.
COVERAGE_SEVERITY = {
    "BLOCKED": 0,
    "NOT_STARTED": 1,
    "SOURCE_IDENTIFIED": 2,
    "BOOTSTRAPPED": 3,
    "DEGRADED": 4,
    "METADATA_SEARCHABLE": 5,
    "FULL_TEXT_SEARCHABLE": 6,
    "VALIDATING": 7,
    "GREEN": 8,
}


def coverage_severity(status: str) -> int:
    """Trust rank for a coverage status. Unknown statuses are treated as the
    least trustworthy -- a status we do not recognise must never be assumed
    safe."""
    return COVERAGE_SEVERITY.get(status, 0)

# States at or beyond which metadata search is considered reliable.
_SUFFICIENT_FOR_METADATA = {
    "METADATA_SEARCHABLE",
    "FULL_TEXT_SEARCHABLE",
    "VALIDATING",
    "GREEN",
}
# States at or beyond which full-text search is considered reliable.
_SUFFICIENT_FOR_FULL_TEXT = {"FULL_TEXT_SEARCHABLE", "VALIDATING", "GREEN"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def to_str_id(value: uuid.UUID | str | None) -> str | None:
    return str(value) if value is not None else None


class ToolError(Exception):
    """Raised for meaningful, structured tool-level errors.

    Callers (tool functions) catch this at the boundary and return a
    structured JSON error payload rather than letting a raw traceback leak.
    """

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def find_jurisdiction(db: Session, state: str) -> Jurisdiction | None:
    """Look up a jurisdiction by two-letter abbreviation (case-insensitive)
    or by name (case-insensitive, exact match)."""
    state = state.strip()
    stmt = select(Jurisdiction).where(Jurisdiction.abbreviation.ilike(state))
    jurisdiction = db.execute(stmt).scalar_one_or_none()
    if jurisdiction is not None:
        return jurisdiction
    stmt = select(Jurisdiction).where(Jurisdiction.name.ilike(state))
    return db.execute(stmt).scalar_one_or_none()


def coverage_rows_for_jurisdiction(
    db: Session, jurisdiction_id: uuid.UUID
) -> list[JurisdictionCoverage]:
    stmt = select(JurisdictionCoverage).where(
        JurisdictionCoverage.jurisdiction_id == jurisdiction_id
    )
    return list(db.execute(stmt).scalars().all())


def worst_status(rows: list[JurisdictionCoverage]) -> str:
    """Return the LEAST TRUSTWORTHY status among coverage rows (or NOT_STARTED
    if there are none), used to decide whether to warn.

    Ranked by COVERAGE_SEVERITY, not by position in COVERAGE_STATES -- see the
    comment there for the bug that distinction fixes.
    """
    if not rows:
        return "NOT_STARTED"
    return min(rows, key=lambda r: coverage_severity(r.status)).status


def serialize_coverage_row(row: JurisdictionCoverage) -> dict[str, Any]:
    return {
        "jurisdiction_id": to_str_id(row.jurisdiction_id),
        "session_id": to_str_id(row.session_id),
        "status": row.status,
        "bill_count": row.bill_count,
        "full_text_count": row.full_text_count,
        "last_attempt_at": iso(row.last_attempt_at),
        "last_success_at": iso(row.last_success_at),
        "validation_pass_rate": (
            float(row.validation_pass_rate) if row.validation_pass_rate is not None else None
        ),
        "known_gaps": row.known_gaps,
        "notes": row.notes,
    }


def coverage_warning_for_jurisdiction(
    db: Session, jurisdiction: Jurisdiction, min_state: str = "METADATA_SEARCHABLE"
) -> dict[str, Any] | None:
    """Build a structured coverage_warning dict if the jurisdiction's coverage
    is below `min_state` (or has no coverage rows at all). Returns None if
    coverage is sufficient.
    """
    rows = coverage_rows_for_jurisdiction(db, jurisdiction.id)
    status = worst_status(rows)
    # Severity, not lifecycle position. The BLOCKED special-case that used to be
    # needed here is gone: BLOCKED is now simply the lowest severity, so it
    # fails this comparison on its own.
    if coverage_severity(status) >= coverage_severity(min_state):
        return None
    return {
        "jurisdiction": jurisdiction.abbreviation,
        "status": status,
        "message": (
            f"Coverage for {jurisdiction.abbreviation} is '{status}', below the "
            f"'{min_state}' threshold required for reliable results. Results below "
            "may be incomplete or absent; this is a data-coverage limitation, not "
            "necessarily an empty legislative record."
        ),
        "coverage_rows": [serialize_coverage_row(r) for r in rows],
    }


def full_text_warning_for_jurisdiction(
    db: Session, jurisdiction: Jurisdiction
) -> dict[str, Any] | None:
    return coverage_warning_for_jurisdiction(db, jurisdiction, min_state="FULL_TEXT_SEARCHABLE")


def meta_envelope(**kwargs: Any) -> dict[str, Any]:
    """Standard response metadata envelope: retrieved_at + tool-specific extras."""
    return {"retrieved_at": iso(utcnow()), **kwargs}
