"""Recompute jurisdiction_coverage rows + write the public coverage report.

Per docs/SPEC.md "Coverage state machine + GREEN criteria": this module
recomputes `bill_count`/`full_text_count` for every jurisdiction_coverage
row from the actual `bills`/`bill_documents` tables and advances/holds the
status field along the state machine (never regresses past what's already
been verified by a validation run -- GREEN/DEGRADED/BLOCKED transitions
require a validation_runs row and are left to the (future) validation
worker, not this recompute pass).

Status transitions this function DOES make, based purely on counts:
    SOURCE_IDENTIFIED -> BOOTSTRAPPED        (bill_count > 0)
    BOOTSTRAPPED       -> METADATA_SEARCHABLE (every bill has identifier+title,
                                                which is always true post-bootstrap
                                                given NOT NULL constraints, so this
                                                is really "bootstrap completed")
    METADATA_SEARCHABLE -> FULL_TEXT_SEARCHABLE (full_text_count > 0)

It never sets GREEN/DEGRADED/BLOCKED/VALIDATING (those require a
validation_runs pass, out of scope here) and never regresses a row that's
already past FULL_TEXT_SEARCHABLE.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, or_ as sa_or, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest.fulltext import TERMINAL_STATUSES, license_note_matches_status
from billcommons_schema.models import (
    Bill,
    BillDocument,
    BillVersion,
    Jurisdiction,
    JurisdictionCoverage,
    Session as SessionModel,
)

# fulltext.py stamps a document's terminal outcome into license_note as
# `fulltext_status=<status>` (sometimes decorated, e.g. ` via=browser`);
# these are the ones that mean "text will never be obtainable from this
# URL", as opposed to ok/never-attempted. license_note_matches_status
# (fulltext.py) tolerates the decorated forms -- see R3-1.

_ADVANCEMENT_ORDER = [
    "NOT_STARTED",
    "SOURCE_IDENTIFIED",
    "BOOTSTRAPPED",
    "METADATA_SEARCHABLE",
    "FULL_TEXT_SEARCHABLE",
    "VALIDATING",
    "GREEN",
]
_TERMINAL_STATES_NOT_AUTO_ADVANCED = {"GREEN", "DEGRADED", "BLOCKED", "VALIDATING"}


def _rank(status: str) -> int:
    return _ADVANCEMENT_ORDER.index(status) if status in _ADVANCEMENT_ORDER else -1


def _next_status_for_counts(current: str, bill_count: int, full_text_count: int) -> str:
    if current in _TERMINAL_STATES_NOT_AUTO_ADVANCED:
        return current

    target = current
    if bill_count > 0 and _rank(target) < _rank("BOOTSTRAPPED"):
        target = "BOOTSTRAPPED"
    if bill_count > 0 and _rank(target) < _rank("METADATA_SEARCHABLE"):
        target = "METADATA_SEARCHABLE"
    if full_text_count > 0 and _rank(target) < _rank("FULL_TEXT_SEARCHABLE"):
        target = "FULL_TEXT_SEARCHABLE"
    return target


def recompute_coverage_row(db: OrmSession, coverage: JurisdictionCoverage) -> JurisdictionCoverage:
    """Recompute one jurisdiction_coverage row's counts + status in place.
    Caller commits."""
    if coverage.session_id is not None:
        bill_count = db.execute(
            select(func.count(Bill.id)).where(Bill.session_id == coverage.session_id)
        ).scalar_one()
        full_text_count = db.execute(
            select(func.count(func.distinct(Bill.id)))
            .select_from(Bill)
            .join(BillVersion, BillVersion.bill_id == Bill.id)
            .join(BillDocument, BillDocument.bill_version_id == BillVersion.id)
            .where(Bill.session_id == coverage.session_id, BillDocument.extracted_text.is_not(None))
        ).scalar_one()
        # SPEC GREEN #5's denominator: bills we could still legitimately get
        # text for. A document already extracted counts (we got it); one that
        # terminally failed does not (robots-disallowed, no text layer, ...);
        # one never attempted (NULL license_note) does. A bill whose every
        # document is terminally unfetchable therefore drops out entirely,
        # which is what makes an all-robots-blocked jurisdiction a documented
        # limitation rather than a permanent sub-100% score.
        full_text_available_count = db.execute(
            select(func.count(func.distinct(Bill.id)))
            .select_from(Bill)
            .join(BillVersion, BillVersion.bill_id == Bill.id)
            .join(BillDocument, BillDocument.bill_version_id == BillVersion.id)
            .where(
                Bill.session_id == coverage.session_id,
                BillDocument.url.is_not(None),
                BillDocument.url != "",
                sa_or(
                    BillDocument.extracted_text.is_not(None),
                    BillDocument.license_note.is_(None),
                    ~license_note_matches_status(BillDocument.license_note, TERMINAL_STATUSES),
                ),
            )
        ).scalar_one()
    else:
        bill_count = db.execute(
            select(func.count(Bill.id)).where(Bill.jurisdiction_id == coverage.jurisdiction_id)
        ).scalar_one()
        full_text_count = 0
        full_text_available_count = 0

    coverage.bill_count = bill_count
    coverage.full_text_count = full_text_count
    coverage.full_text_available_count = full_text_available_count
    coverage.status = _next_status_for_counts(coverage.status, bill_count, full_text_count)
    coverage.last_attempt_at = datetime.now(timezone.utc)
    if bill_count > 0:
        coverage.last_success_at = datetime.now(timezone.utc)
    db.flush()
    return coverage


def recompute_all_coverage(
    db: OrmSession, *, jurisdiction_ids: list | None = None
) -> list[JurisdictionCoverage]:
    """Recompute every jurisdiction_coverage row. Caller commits.

    `jurisdiction_ids` narrows the pass, and exists for the test suite. Tests
    run against the same live database as the production worker, which calls
    this on a loop; an unscoped call from a test takes a row lock on all 77
    coverage rows and contends with that worker on the same rows, so the test
    intermittently died on the 30s statement timeout with a failure that said
    nothing about the code under test. A test that created one jurisdiction
    only ever needed to recompute that one. Production passes nothing and
    recomputes everything, unchanged."""
    stmt = select(JurisdictionCoverage)
    if jurisdiction_ids is not None:
        stmt = stmt.where(JurisdictionCoverage.jurisdiction_id.in_(jurisdiction_ids))
    rows = db.execute(stmt).scalars().all()
    for row in rows:
        recompute_coverage_row(db, row)
    return rows


def build_coverage_report(db: OrmSession) -> dict:
    """Build the JSON structure written to coverage-latest.json: one entry
    per jurisdiction (51 expected), with its session(s) and coverage
    status -- matches the "public coverage matrix" fields from
    docs/SPEC.md (jurisdiction, session, bill count, full-text %, last
    update, source, status, known gaps)."""
    jurisdictions = db.execute(select(Jurisdiction).order_by(Jurisdiction.abbreviation)).scalars().all()

    report_rows = []
    for jurisdiction in jurisdictions:
        coverage_rows = db.execute(
            select(JurisdictionCoverage).where(JurisdictionCoverage.jurisdiction_id == jurisdiction.id)
        ).scalars().all()

        for coverage in coverage_rows:
            session_row = None
            if coverage.session_id is not None:
                session_row = db.get(SessionModel, coverage.session_id)

            full_text_pct = (
                round(100.0 * coverage.full_text_count / coverage.bill_count, 1)
                if coverage.bill_count
                else 0.0
            )
            # The GREEN-relevant number: share of the text that is actually
            # obtainable which we hold. `full_text_pct` (over ALL bills) reads
            # far worse for a jurisdiction whose source simply publishes few
            # documents, so both are published rather than either alone.
            # available == 0 means nothing is obtainable -- reported as null,
            # not 100%, so it can't be mistaken for full coverage.
            available = coverage.full_text_available_count
            full_text_of_available_pct = (
                round(100.0 * coverage.full_text_count / available, 1) if available else None
            )
            report_rows.append(
                {
                    "jurisdiction": jurisdiction.abbreviation,
                    "jurisdiction_name": jurisdiction.name,
                    "session": session_row.identifier if session_row else None,
                    "session_active": session_row.active if session_row else None,
                    "bill_count": coverage.bill_count,
                    "full_text_count": coverage.full_text_count,
                    "full_text_pct": full_text_pct,
                    "full_text_available_count": available,
                    "full_text_of_available_pct": full_text_of_available_pct,
                    "status": coverage.status,
                    "validation_pass_rate": float(coverage.validation_pass_rate)
                    if coverage.validation_pass_rate is not None
                    else None,
                    "last_attempt_at": coverage.last_attempt_at.isoformat()
                    if coverage.last_attempt_at
                    else None,
                    "last_success_at": coverage.last_success_at.isoformat()
                    if coverage.last_success_at
                    else None,
                    "known_gaps": coverage.known_gaps,
                    "notes": coverage.notes,
                }
            )

    jurisdiction_count = len(jurisdictions)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jurisdiction_count": jurisdiction_count,
        "rows": report_rows,
    }


def write_coverage_report(report: dict, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return output_path


def recompute_and_write(
    db: OrmSession, output_path: str | Path = "docs/state-coverage/coverage-latest.json"
) -> dict:
    """Full pass: recompute every coverage row, then build + write the
    report JSON. Caller commits the DB transaction; this function always
    writes the JSON file regardless (recompute is read-mostly and cheap to
    redo)."""
    recompute_all_coverage(db)
    report = build_coverage_report(db)
    write_coverage_report(report, output_path)
    return report
