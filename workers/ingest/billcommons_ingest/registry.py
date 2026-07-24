"""Seed jurisdictions, sessions, and jurisdiction_coverage rows from
data/registry/sessions-2026.json.

The registry JSON is the machine-readable source-of-truth for "what is the
current session per jurisdiction" (per docs/SPEC.md "Scope" +
ARCHITECTURE.md's source registry). This module upserts:

    - one `jurisdictions` row per `state_code` entry in `jurisdictions[]`
      (51: 50 states + DC)
    - one `sessions` row per jurisdiction, keyed on
      (jurisdiction_id, identifier) using `session_identifier` as `identifier`
    - one `jurisdiction_coverage` row per jurisdiction/session pair,
      status SOURCE_IDENTIFIED (a session has been identified from an
      authoritative source, per the GREEN-criteria state machine, but no
      bills have been imported yet)
    - special sessions from the top-level `special_sessions[]` array also
      get their own `sessions` row (identifier = "<state> special: <topic>
      (<convened_at>)") + coverage row, since SPEC explicitly prioritizes
      "active specials first" as part of the seed queue.

Idempotent: re-running upserts on natural keys and never creates duplicate
jurisdictions/sessions/coverage rows.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from billcommons_schema.models import Jurisdiction, JurisdictionCoverage, Session as SessionModel

SOURCE_NAME = "sessions-2026-registry"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_registry(path: str | Path) -> dict:
    """Load and return the parsed registry JSON. Raises if the file is
    missing/malformed -- registry seeding should never silently proceed on
    bad input."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _upsert_jurisdiction(db: Session, entry: dict, retrieved_at: datetime) -> Jurisdiction:
    abbreviation = entry["state_code"]
    jurisdiction = db.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation == abbreviation)
    ).scalar_one_or_none()

    if jurisdiction is None:
        jurisdiction = Jurisdiction(abbreviation=abbreviation)
        db.add(jurisdiction)

    jurisdiction.name = entry["jurisdiction_name"]
    jurisdiction.classification = "state" if abbreviation != "DC" else "district"
    jurisdiction.source_name = SOURCE_NAME
    jurisdiction.source_url = entry.get("source_url")
    jurisdiction.retrieved_at = retrieved_at
    jurisdiction.license_note = "NCSL / Ballotpedia session-calendar aggregation, see source_url"
    db.flush()
    return jurisdiction


def _upsert_session(
    db: Session,
    jurisdiction: Jurisdiction,
    identifier: str,
    entry: dict,
    retrieved_at: datetime,
    *,
    active: bool,
) -> SessionModel:
    session_row = db.execute(
        select(SessionModel).where(
            SessionModel.jurisdiction_id == jurisdiction.id,
            SessionModel.identifier == identifier,
        )
    ).scalar_one_or_none()

    if session_row is None:
        session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier=identifier)
        db.add(session_row)

    session_row.name = entry.get("legislature_name")
    session_row.classification = entry.get("regular_or_special")
    session_row.start_date = _parse_date(entry.get("convened_at"))
    session_row.end_date = _parse_date(entry.get("expected_adjournment"))
    session_row.active = active
    session_row.source_name = SOURCE_NAME
    session_row.source_url = entry.get("source_url")
    session_row.retrieved_at = retrieved_at
    db.flush()
    return session_row


def _upsert_coverage(
    db: Session,
    jurisdiction: Jurisdiction,
    session_row: SessionModel,
    *,
    notes: str | None = None,
) -> JurisdictionCoverage:
    coverage = db.execute(
        select(JurisdictionCoverage).where(
            JurisdictionCoverage.jurisdiction_id == jurisdiction.id,
            JurisdictionCoverage.session_id == session_row.id,
        )
    ).scalar_one_or_none()

    if coverage is None:
        coverage = JurisdictionCoverage(
            jurisdiction_id=jurisdiction.id,
            session_id=session_row.id,
            status="SOURCE_IDENTIFIED",
        )
        db.add(coverage)
    elif coverage.status == "NOT_STARTED":
        # Session source has now been identified -- advance the state
        # machine, but never regress a jurisdiction that's already
        # progressed further (bootstrap/coverage runs own that transition).
        coverage.status = "SOURCE_IDENTIFIED"

    coverage.last_attempt_at = datetime.now(timezone.utc)
    if notes:
        coverage.notes = notes
    db.flush()
    return coverage


def seed_registry(db: Session, registry_path: str | Path) -> dict[str, int]:
    """Upsert jurisdictions + sessions + coverage rows for every entry in
    the registry JSON, including special_sessions. Returns counts for
    logging/verification. Caller is responsible for committing.
    """
    data = load_registry(registry_path)
    retrieved_at = datetime.now(timezone.utc)

    jurisdictions_seen = 0
    sessions_seen = 0
    coverage_seen = 0

    by_state: dict[str, Jurisdiction] = {}

    for entry in data.get("jurisdictions", []):
        jurisdiction = _upsert_jurisdiction(db, entry, retrieved_at)
        by_state[entry["state_code"]] = jurisdiction
        jurisdictions_seen += 1

        session_row = _upsert_session(
            db,
            jurisdiction,
            entry["session_identifier"],
            entry,
            retrieved_at,
            active=entry.get("session_status") in ("active", "year_round"),
        )
        sessions_seen += 1

        _upsert_coverage(db, jurisdiction, session_row, notes=entry.get("notes") or None)
        coverage_seen += 1

    for special in data.get("special_sessions", []):
        state_code = special["state_code"]
        jurisdiction = by_state.get(state_code)
        if jurisdiction is None:
            # Registry listed a special session for a state not present in
            # jurisdictions[] -- surface it rather than silently skip.
            raise ValueError(
                f"special_sessions entry references unknown state_code {state_code!r}"
            )

        identifier = (
            f"{jurisdiction.name} special: {special['topic']} "
            f"({special.get('convened_at', 'undated')})"
        )
        special_entry = {
            "legislature_name": jurisdiction.name,
            "regular_or_special": "special",
            "convened_at": special.get("convened_at"),
            "expected_adjournment": special.get("expected_adjournment"),
            "source_url": special.get("source_url"),
        }
        session_row = _upsert_session(
            db,
            jurisdiction,
            identifier,
            special_entry,
            retrieved_at,
            active=str(special.get("status", "")).startswith("active"),
        )
        sessions_seen += 1

        _upsert_coverage(db, jurisdiction, session_row, notes=special.get("status"))
        coverage_seen += 1

    return {
        "jurisdictions": jurisdictions_seen,
        "sessions": sessions_seen,
        "coverage_rows": coverage_seen,
    }
