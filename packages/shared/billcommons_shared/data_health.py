"""Read-only ingestion reliability and reconciliation-capability report.

This is deliberately a control-plane *observer*.  It makes no outbound
requests and never writes the database.  The report separates evidence we do
have (local ingestion-run history, local queue state, and stored provenance)
from evidence we do not have (a just-fetched official source record).

In particular, a recent ``ingestion_runs`` timestamp means only that the
local pipeline reported a successful run.  It does not prove that the
upstream source was current, complete, or unchanged.  Until an adapter stores
an official differential reconciliation result, every jurisdiction's
``official_reconciliation`` state remains ``unavailable``.

The worker package supplies the command-line wrapper.  Keeping this module
free of worker imports lets API-only images expose the same report contract.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import (
    ApiSyncSnapshotBlocker,
    Bill,
    IngestJob,
    IngestionRun,
    Jurisdiction,
    JurisdictionCoverage,
)
from billcommons_schema.models import Session as SessionModel


REPORT_VERSION = 1
RECONCILIATION_UNAVAILABLE = "unavailable"
API_SYNC_SOURCE = "openstates_api_sync"

# The public control plane has a fixed 51-jurisdiction contract.  The set is
# derived from data/registry/sessions-2026.json (50 states plus DC), kept here
# as package data rather than a runtime file read because API-only images must
# not depend on the ingestion tree being installed.
PUBLIC_JURISDICTION_CODES = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
        "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
        "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
        "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    }
)

# These values mirror the documented refresh policy.  They live here rather
# than importing the ingestion scheduler so API-only images can expose the
# report without installing worker code.  The session fields are the same
# intentionally limited signals used by the scheduler: active with an end
# date is an active/special session; active without one is year-round; an
# inactive session that ended in the last 30 days is recently adjourned.
CADENCE_ACTIVE_MINUTES = 30
CADENCE_YEAR_ROUND_MINUTES = 60
CADENCE_RECENTLY_ADJOURNED_MINUTES = 24 * 60
CADENCE_DORMANT_MINUTES = 7 * 24 * 60
RECENTLY_ADJOURNED_WINDOW_DAYS = 30
# Ingestion runs and report reads normally use the same database clock. Keep a
# small allowance for clock propagation or a report captured across a boundary,
# but surface a materially future local success rather than treating it as fresh.
MAX_FUTURE_SYNC_SKEW_MINUTES = 5
MAX_SNAPSHOT_BLOCKER_SAMPLES = 5

SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}
FAIL_ON_ORDER = {"critical": 0, "error": 1, "warning": 2}


@dataclass(frozen=True)
class BillEvidence:
    """Counts derived from the local `bills` table only."""

    bill_count: int = 0
    missing_parser_version: int = 0
    missing_source_name: int = 0
    missing_source_url: int = 0
    missing_retrieved_at: int = 0


@dataclass(frozen=True)
class RunEvidence:
    source_name: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def observed_at(self) -> datetime | None:
        """The best local event timestamp; never an official freshness time."""
        return self.finished_at or self.started_at


@dataclass(frozen=True)
class CoverageEvidence:
    status: str
    bill_count: int
    full_text_count: int
    scope: str = "jurisdiction"
    session_identifier: str | None = None


@dataclass(frozen=True)
class RefreshTarget:
    session_id: Any
    session_identifier: str
    cadence_tier: str
    cadence_minutes: int


@dataclass(frozen=True)
class SnapshotBlockerEvidence:
    blocker_id: str
    bill_id: str | None
    component: str
    record_cap: int
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class JurisdictionEvidence:
    abbreviation: str
    name: str
    cadence_tier: str | None
    cadence_minutes: int | None
    bills: BillEvidence
    latest_run: RunEvidence | None
    latest_successful_run: RunEvidence | None
    latest_api_sync_run: RunEvidence | None
    latest_successful_api_sync: RunEvidence | None
    coverage: CoverageEvidence | None
    exists: bool = True
    aggregate_coverage_signal: CoverageEvidence | None = None
    dead_api_sync_jobs: int = 0
    queued_api_sync_jobs: int = 0
    running_api_sync_jobs: int = 0
    oldest_queued_api_sync_at: datetime | None = None
    oldest_running_api_sync_at: datetime | None = None
    deferred_api_sync_jobs: int = 0
    next_deferred_api_sync_at: datetime | None = None
    active_snapshot_blockers: int = 0
    snapshot_blockers_without_local_bill: int = 0
    snapshot_blocker_samples: tuple[SnapshotBlockerEvidence, ...] = ()


@dataclass(frozen=True)
class Defect:
    severity: str
    code: str
    jurisdiction: str
    message: str
    evidence: dict[str, Any]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _timestamp(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat(timespec="seconds") if value else None


def defects_for(evidence: JurisdictionEvidence, *, now: datetime) -> list[Defect]:
    """Turn local evidence into an operational defect ledger.

    No condition here concludes that an official source is fresh or stale.
    ``LOCAL_SYNC_OVERDUE`` is solely a scheduling/liveness signal: the local
    run history is older than the cadence configured for this jurisdiction.
    ``FUTURE_SUCCESSFUL_API_SYNC_TIME`` is a local timestamp-integrity signal,
    not a statement about official-source freshness.
    """
    defects: list[Defect] = []
    bills = evidence.bills
    jurisdiction = evidence.abbreviation

    if not evidence.exists:
        return [
            Defect(
                "error",
                "MISSING_JURISDICTION",
                jurisdiction,
                "This canonical jurisdiction has no local jurisdiction row.",
                {"jurisdiction": jurisdiction},
            )
        ]

    if evidence.cadence_minutes is None:
        defects.append(
            Defect(
                "error",
                "MISSING_REFRESH_CONFIGURATION",
                jurisdiction,
                "No session is available to derive a local refresh cadence.",
                {"cadence_tier": evidence.cadence_tier},
            )
        )

    # A configured jurisdiction/session with no local bills is not a healthy
    # empty state. The report deliberately says only that the local corpus is
    # empty; it does not infer anything about the official source.
    if evidence.cadence_minutes is not None and not bills.bill_count:
        defects.append(
            Defect(
                "error",
                "EMPTY_CORPUS",
                jurisdiction,
                "A refreshable jurisdiction has no local bills.",
                {
                    "cadence_tier": evidence.cadence_tier,
                    "local_bill_count": 0,
                },
            )
        )

    if bills.bill_count and evidence.latest_successful_run is None:
        defects.append(
            Defect(
                "critical",
                "LOCAL_DATA_WITHOUT_SUCCESSFUL_RUN",
                jurisdiction,
                "Local bills exist but no successful ingestion run is recorded.",
                {"local_bill_count": bills.bill_count},
            )
        )

    if evidence.latest_run is not None and evidence.latest_run.status == "failed":
        defects.append(
            Defect(
                "error",
                "LATEST_INGESTION_FAILED",
                jurisdiction,
                "The most recent local ingestion run failed.",
                {
                    "source_name": evidence.latest_run.source_name,
                    "observed_at": _timestamp(evidence.latest_run.observed_at),
                },
            )
        )

    # A more recent bulk import can make `latest_run` look healthy even while
    # the scheduler's own adapter is failing.  Keep this source-specific
    # signal independent so a broken incremental path cannot hide behind an
    # unrelated successful job.
    if evidence.latest_api_sync_run is not None and evidence.latest_api_sync_run.status == "failed":
        defects.append(
            Defect(
                "error",
                "LATEST_API_SYNC_FAILED",
                jurisdiction,
                "The most recent incremental API sync run failed.",
                {
                    "source_name": evidence.latest_api_sync_run.source_name,
                    "observed_at": _timestamp(evidence.latest_api_sync_run.observed_at),
                },
            )
        )

    if evidence.dead_api_sync_jobs:
        defects.append(
            Defect(
                "error",
                "DEAD_API_SYNC_JOBS",
                jurisdiction,
                "API sync jobs reached the dead-letter state.",
                {"dead_job_count": evidence.dead_api_sync_jobs},
            )
        )

    if evidence.active_snapshot_blockers:
        defects.append(
            Defect(
                "error",
                "API_SYNC_SNAPSHOT_BLOCKED",
                jurisdiction,
                "Some bills exceed the complete-evidence snapshot limit; incremental sync remains incomplete.",
                _snapshot_blockers_dict(evidence),
            )
        )

    if (
        evidence.latest_successful_api_sync is not None
        and evidence.latest_successful_api_sync.observed_at is None
    ):
        defects.append(
            Defect(
                "error",
                "UNKNOWN_SYNC_TIME",
                jurisdiction,
                "The latest successful incremental API sync has no usable timestamp.",
                {"source_name": evidence.latest_successful_api_sync.source_name},
            )
        )

    # Scheduler suppression intentionally avoids enqueueing duplicate work.
    # That makes a very old queued/running job a liveness concern: it can
    # suppress future scheduling indefinitely.  This observer only reports
    # the condition for human inspection; it must never reclaim or alter a
    # job automatically.
    pending_age_threshold = max((evidence.cadence_minutes or 0) * 2, 60)
    if evidence.deferred_api_sync_jobs:
        defects.append(Defect(
            "info", "API_SYNC_WAITING_FOR_ELIGIBILITY", jurisdiction,
            "API sync work is waiting for its scheduled eligibility time; duplicate dispatch is suppressed.",
            {"deferred_job_count": evidence.deferred_api_sync_jobs,
             "next_eligible_at": _timestamp(evidence.next_deferred_api_sync_at)},
        ))
    for status, oldest_at in (
        ("queued", evidence.oldest_queued_api_sync_at),
        ("running", evidence.oldest_running_api_sync_at),
    ):
        observed_at = _utc(oldest_at)
        if observed_at is None:
            continue
        age_minutes = (now - observed_at).total_seconds() / 60
        if age_minutes > pending_age_threshold:
            defects.append(
                Defect(
                    "error",
                    f"API_SYNC_{status.upper()}_STALLED_SUSPECTED",
                    jurisdiction,
                    f"An API sync job has remained {status} beyond the inspection threshold.",
                    {
                        "oldest_observed_at": _timestamp(observed_at),
                        "age_minutes": round(age_minutes, 1),
                        "inspection_threshold_minutes": pending_age_threshold,
                        "action": "inspect; this report performs no automatic recovery",
                    },
                )
            )

    # The scheduler's cadence applies to its incremental API adapter, not a
    # one-off bulk import, repair script, or full-text job.  A recent repair
    # must never mask an overdue incremental sync.
    if (
        evidence.cadence_minutes is not None
        and bills.bill_count
        and evidence.latest_successful_api_sync is None
    ):
        defects.append(
            Defect(
                "warning",
                "NO_SUCCESSFUL_API_SYNC",
                jurisdiction,
                "No successful incremental API sync is recorded; local scheduling recency cannot be assessed.",
                {"local_bill_count": bills.bill_count, "cadence_tier": evidence.cadence_tier},
            )
        )
    elif evidence.cadence_minutes is not None and evidence.latest_successful_api_sync is not None:
        observed_at = _utc(evidence.latest_successful_api_sync.observed_at)
        if observed_at is not None:
            age_minutes = (now - observed_at).total_seconds() / 60
            if age_minutes < -MAX_FUTURE_SYNC_SKEW_MINUTES:
                defects.append(
                    Defect(
                        "error",
                        "FUTURE_SUCCESSFUL_API_SYNC_TIME",
                        jurisdiction,
                        "The last successful local API sync timestamp is materially in the future.",
                        {
                            "last_successful_local_api_sync_at": _timestamp(observed_at),
                            "ahead_minutes": round(-age_minutes, 1),
                            "allowed_clock_skew_minutes": MAX_FUTURE_SYNC_SKEW_MINUTES,
                            "cadence_tier": evidence.cadence_tier,
                        },
                    )
                )
            elif age_minutes > evidence.cadence_minutes:
                defects.append(
                    Defect(
                        "warning",
                        "LOCAL_SYNC_OVERDUE",
                        jurisdiction,
                        "The last successful local API sync is older than its scheduling target.",
                        {
                            "last_successful_local_api_sync_at": _timestamp(observed_at),
                            "age_minutes": round(age_minutes, 1),
                            "target_minutes": evidence.cadence_minutes,
                            "cadence_tier": evidence.cadence_tier,
                        },
                    )
                )

    if bills.missing_parser_version:
        defects.append(
            Defect(
                "warning",
                "MISSING_PARSER_PROVENANCE",
                jurisdiction,
                "Some local bills have no parser-version provenance.",
                {
                    "affected_bill_count": bills.missing_parser_version,
                    "local_bill_count": bills.bill_count,
                },
            )
        )

    missing_source_provenance = max(
        bills.missing_source_name,
        bills.missing_source_url,
        bills.missing_retrieved_at,
    )
    if missing_source_provenance:
        defects.append(
            Defect(
                "warning",
                "MISSING_SOURCE_PROVENANCE",
                jurisdiction,
                "Some local bills lack enough source provenance for an evidence-backed audit.",
                {
                    "missing_source_name": bills.missing_source_name,
                    "missing_source_url": bills.missing_source_url,
                    "missing_retrieved_at": bills.missing_retrieved_at,
                    "local_bill_count": bills.bill_count,
                },
            )
        )

    if evidence.coverage is None and evidence.cadence_minutes is not None:
        defects.append(
            Defect(
                "error",
                "MISSING_JURISDICTION_COVERAGE",
                jurisdiction,
                "No coverage row exists for the selected session or jurisdiction aggregate.",
                {
                    "local_bill_count": bills.bill_count,
                },
            )
        )
    elif evidence.coverage is not None:
        severity = {"BLOCKED": "critical", "DEGRADED": "warning"}.get(evidence.coverage.status)
        if severity:
            defects.append(
                Defect(
                    severity,
                    f"COVERAGE_{evidence.coverage.status}",
                    jurisdiction,
                    f"Jurisdiction coverage is marked {evidence.coverage.status}.",
                    {
                        "coverage_status": evidence.coverage.status,
                        "coverage_scope": evidence.coverage.scope,
                        "coverage_session_identifier": evidence.coverage.session_identifier,
                        "coverage_bill_count": evidence.coverage.bill_count,
                        "coverage_full_text_count": evidence.coverage.full_text_count,
                    },
                )
            )

    if evidence.aggregate_coverage_signal is not None:
        aggregate = evidence.aggregate_coverage_signal
        severity = {"BLOCKED": "critical", "DEGRADED": "warning"}.get(aggregate.status)
        if severity:
            defects.append(
                Defect(
                    severity,
                    f"JURISDICTION_COVERAGE_{aggregate.status}",
                    jurisdiction,
                    f"The jurisdiction-wide coverage aggregate is marked {aggregate.status}.",
                    {
                        "coverage_status": aggregate.status,
                        "coverage_scope": aggregate.scope,
                        "coverage_bill_count": aggregate.bill_count,
                        "coverage_full_text_count": aggregate.full_text_count,
                    },
                )
            )

    return defects


def build_report(evidence: Iterable[JurisdictionEvidence], *, now: datetime | None = None) -> dict[str, Any]:
    """Build a JSON-safe, deterministic report without touching a database."""
    now = _utc(now) or datetime.now(timezone.utc)
    jurisdictions = sorted(evidence, key=lambda item: item.abbreviation)
    defects = [defect for item in jurisdictions for defect in defects_for(item, now=now)]
    defects.sort(key=lambda defect: (SEVERITY_ORDER[defect.severity], defect.jurisdiction, defect.code))
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for defect in defects:
        counts[defect.severity] += 1

    rows = []
    for item in jurisdictions:
        rows.append(
            {
                "jurisdiction": item.abbreviation,
                "name": item.name,
                "refresh_target": {
                    "cadence_tier": item.cadence_tier,
                    "target_minutes": item.cadence_minutes,
                },
                "local_ingestion": {
                    "last_run": _run_dict(item.latest_run),
                    "last_successful_run": _run_dict(item.latest_successful_run),
                    "last_api_sync": _run_dict(item.latest_api_sync_run),
                    "last_successful_api_sync": _run_dict(item.latest_successful_api_sync),
                    "interpretation": "Local pipeline history only; it is not official-source freshness.",
                },
                "source_health": {
                    "dead_api_sync_jobs": item.dead_api_sync_jobs,
                    "queued_api_sync_jobs": item.queued_api_sync_jobs,
                    "running_api_sync_jobs": item.running_api_sync_jobs,
                    "oldest_queued_api_sync_at": _timestamp(item.oldest_queued_api_sync_at),
                    "oldest_running_api_sync_at": _timestamp(item.oldest_running_api_sync_at),
                    "deferred_api_sync_jobs": item.deferred_api_sync_jobs,
                    "next_deferred_api_sync_at": _timestamp(item.next_deferred_api_sync_at),
                    "snapshot_blockers": _snapshot_blockers_dict(item),
                },
                "parser_health": asdict(item.bills),
                "coverage": asdict(item.coverage) if item.coverage else None,
                "additional_jurisdiction_coverage_signal": (
                    asdict(item.aggregate_coverage_signal)
                    if item.aggregate_coverage_signal
                    else None
                ),
                "official_reconciliation": {
                    "state": RECONCILIATION_UNAVAILABLE,
                    "reason": "This local ingestion report does not assess official observations or differential results.",
                    "evidence_url": f"/api/v1/official-evidence/observations?jurisdiction={item.abbreviation}",
                },
            }
        )

    return {
        "report_version": REPORT_VERSION,
        "generated_at": _timestamp(now),
        "scope": "read-only local ingestion reliability",
        "honesty": {
            "official_freshness": "unverified",
            "official_freshness_reason": "This command performs no official-source fetch and local timestamps cannot prove source freshness.",
            "official_reconciliation": RECONCILIATION_UNAVAILABLE,
            "official_evidence_overview_url": "/api/v1/official-evidence/overview",
        },
        "summary": {
            "jurisdiction_count": len(rows),
            "defect_count": len(defects),
            "defects_by_severity": counts,
        },
        "jurisdictions": rows,
        "defects": [asdict(defect) for defect in defects],
    }


def _snapshot_blockers_dict(item: JurisdictionEvidence) -> dict[str, Any]:
    samples = item.snapshot_blocker_samples[:MAX_SNAPSHOT_BLOCKER_SAMPLES]
    return {
        "active_count": item.active_snapshot_blockers,
        "without_local_bill_count": item.snapshot_blockers_without_local_bill,
        "sample_limit": MAX_SNAPSHOT_BLOCKER_SAMPLES,
        "samples_truncated": item.active_snapshot_blockers > len(samples),
        "samples": [
            {
                "blocker_id": sample.blocker_id,
                "bill_id": sample.bill_id,
                "component": sample.component,
                "record_cap": sample.record_cap,
                "first_seen_at": _timestamp(sample.first_seen_at),
                "last_seen_at": _timestamp(sample.last_seen_at),
            }
            for sample in samples
        ],
        "interpretation": (
            "Unresolved local ingestion blockers, not proof of duplicate source events. "
            "A null bill_id means the blocker has no current local bill reference."
        ),
    }


def _run_dict(run: RunEvidence | None) -> dict[str, str | None] | None:
    if run is None:
        return None
    return {
        "source_name": run.source_name,
        "status": run.status,
        "started_at": _timestamp(run.started_at),
        "finished_at": _timestamp(run.finished_at),
        "observed_at": _timestamp(run.observed_at),
    }


def _cadence_tier(*, active: bool, end_date: date | None, now: datetime) -> str:
    if active:
        return "year_round" if end_date is None else "active"
    if end_date is not None and 0 <= (now.date() - end_date).days <= RECENTLY_ADJOURNED_WINDOW_DAYS:
        return "recently_adjourned"
    return "dormant"


def _cadence_minutes(tier: str) -> int:
    return {
        "active": CADENCE_ACTIVE_MINUTES,
        "year_round": CADENCE_YEAR_ROUND_MINUTES,
        "recently_adjourned": CADENCE_RECENTLY_ADJOURNED_MINUTES,
        "dormant": CADENCE_DORMANT_MINUTES,
    }[tier]


def _session_targets(sessions: Iterable[SessionModel], *, now: datetime) -> dict[Any, RefreshTarget]:
    """Choose the same operational session priority as the refresh scheduler."""
    grouped: dict[Any, list[SessionModel]] = defaultdict(list)
    for session in sessions:
        grouped[session.jurisdiction_id].append(session)

    targets = {}
    for jurisdiction_id, candidates in grouped.items():
        selected = max(
            candidates,
            key=lambda session: (bool(session.active), session.start_date or date.min),
        )
        tier = _cadence_tier(active=bool(selected.active), end_date=selected.end_date, now=now)
        targets[jurisdiction_id] = RefreshTarget(
            selected.id, selected.identifier, tier, _cadence_minutes(tier)
        )
    return targets


def collect_evidence(db: OrmSession, *, now: datetime | None = None) -> list[JurisdictionEvidence]:
    """Read local operational facts in bounded aggregate queries.

    The caller must not commit this session.  This function has no writes and
    makes no external request, which keeps the CLI safe for incident use.
    """
    now = _utc(now) or datetime.now(timezone.utc)
    canonical_codes = tuple(sorted(PUBLIC_JURISDICTION_CODES))
    jurisdictions = db.execute(
        select(Jurisdiction).where(Jurisdiction.abbreviation.in_(canonical_codes))
    ).scalars().all()
    jurisdictions_by_code = {jurisdiction.abbreviation: jurisdiction for jurisdiction in jurisdictions}
    jurisdiction_ids = tuple(jurisdiction.id for jurisdiction in jurisdictions)

    # PostgreSQL DISTINCT ON gives the collector precisely one operational
    # session for each canonical jurisdiction. This matches scheduler priority
    # (active first, then latest start date) and adds a UUID tie-breaker so an
    # equal-date pair cannot make report scope depend on physical row order.
    selected_sessions = []
    if jurisdiction_ids:
        selected_sessions = db.execute(
            select(SessionModel)
            .where(SessionModel.jurisdiction_id.in_(jurisdiction_ids))
            .distinct(SessionModel.jurisdiction_id)
            .order_by(
                SessionModel.jurisdiction_id,
                SessionModel.active.desc(),
                SessionModel.start_date.desc().nulls_last(),
                SessionModel.id.desc(),
            )
        ).scalars().all()
    targets = {}
    for session in selected_sessions:
        tier = _cadence_tier(active=bool(session.active), end_date=session.end_date, now=now)
        targets[session.jurisdiction_id] = RefreshTarget(
            session.id, session.identifier, tier, _cadence_minutes(tier)
        )

    bill_rows = db.execute(
        select(
            Bill.jurisdiction_id,
            func.count(Bill.id).label("bill_count"),
            func.count(Bill.id)
            .filter(or_(Bill.parser_version.is_(None), Bill.parser_version == ""))
            .label("missing_parser_version"),
            func.count(Bill.id)
            .filter(or_(Bill.source_name.is_(None), Bill.source_name == ""))
            .label("missing_source_name"),
            func.count(Bill.id)
            .filter(or_(Bill.source_url.is_(None), Bill.source_url == ""))
            .label("missing_source_url"),
            func.count(Bill.id).filter(Bill.retrieved_at.is_(None)).label("missing_retrieved_at"),
        )
        .where(Bill.jurisdiction_id.in_(jurisdiction_ids))
        .group_by(Bill.jurisdiction_id)
    ).all()
    bills = {
        row.jurisdiction_id: BillEvidence(
            bill_count=int(row.bill_count or 0),
            missing_parser_version=int(row.missing_parser_version or 0),
            missing_source_name=int(row.missing_source_name or 0),
            missing_source_url=int(row.missing_source_url or 0),
            missing_retrieved_at=int(row.missing_retrieved_at or 0),
        )
        for row in bill_rows
    }

    # PostgreSQL DISTINCT ON scans bound this report to at most one latest
    # run for each required source/status combination per jurisdiction.
    # Reading every old run would become an unbounded incident-time query as
    # a 30-minute scheduler accumulates history.
    # Creation time is ordering evidence only, never a substitute for a sync
    # completion timestamp. A newer malformed run must not disappear behind
    # old dated history before UNKNOWN_SYNC_TIME/latest-failure checks run.
    run_time = func.coalesce(
        IngestionRun.finished_at, IngestionRun.started_at, IngestionRun.created_at
    )
    run_columns = (
        IngestionRun.jurisdiction_id,
        IngestionRun.source_name,
        IngestionRun.status,
        IngestionRun.started_at,
        IngestionRun.finished_at,
    )

    def latest_runs(
        status: str | None = None, *, source_name: str | None = None
    ) -> dict[Any, RunEvidence]:
        statement = select(*run_columns).where(IngestionRun.jurisdiction_id.in_(jurisdiction_ids))
        if status is not None:
            statement = statement.where(IngestionRun.status == status)
        if source_name is not None:
            statement = statement.where(IngestionRun.source_name == source_name)
        rows = db.execute(
            statement.distinct(IngestionRun.jurisdiction_id).order_by(
                IngestionRun.jurisdiction_id,
                run_time.desc().nulls_last(),
                IngestionRun.id.desc(),
            )
        ).all()
        return {
            row.jurisdiction_id: RunEvidence(
                row.source_name, row.status, row.started_at, row.finished_at
            )
            for row in rows
        }

    latest_runs_by_jurisdiction = latest_runs()
    latest_successes_by_jurisdiction = latest_runs("success")
    latest_api_sync_runs_by_jurisdiction = latest_runs(source_name=API_SYNC_SOURCE)
    latest_api_sync_successes_by_jurisdiction = latest_runs(
        "success", source_name=API_SYNC_SOURCE
    )

    selected_session_ids = tuple(target.session_id for target in targets.values())
    coverage_by_selected_session: dict[Any, CoverageEvidence] = {}
    aggregate_coverage_by_jurisdiction: dict[Any, CoverageEvidence] = {}
    if jurisdiction_ids:
        coverage_scope = JurisdictionCoverage.session_id.is_(None)
        if selected_session_ids:
            coverage_scope = or_(coverage_scope, JurisdictionCoverage.session_id.in_(selected_session_ids))
        for coverage in db.execute(
            select(JurisdictionCoverage).where(
                JurisdictionCoverage.jurisdiction_id.in_(jurisdiction_ids), coverage_scope
            )
        ).scalars():
            evidence = CoverageEvidence(
                coverage.status,
                int(coverage.bill_count or 0),
                int(coverage.full_text_count or 0),
                scope="session" if coverage.session_id is not None else "jurisdiction",
                session_identifier=(
                    next(
                        (
                            target.session_identifier
                            for target in targets.values()
                            if target.session_id == coverage.session_id
                        ),
                        None,
                    )
                    if coverage.session_id is not None
                    else None
                ),
            )
            if coverage.session_id is None:
                aggregate_coverage_by_jurisdiction[coverage.jurisdiction_id] = evidence
            else:
                coverage_by_selected_session[coverage.session_id] = evidence

    jobs_by_state: dict[str, dict[str, tuple[int, datetime | None]]] = defaultdict(dict)
    deferred_by_state: dict[str, tuple[int, datetime | None]] = {}
    job_state = func.upper(IngestJob.payload["state"].astext)
    queued_eligible_at = func.greatest(IngestJob.created_at, IngestJob.run_after)
    for row in db.execute(
        select(
            job_state.label("state"),
            IngestJob.status,
            func.count(IngestJob.id).label("count"),
            func.min(queued_eligible_at)
            .filter(IngestJob.status == "queued", IngestJob.run_after <= now)
            .label("oldest_queued_eligible_at"),
            func.count(IngestJob.id)
            .filter(IngestJob.status == "queued", IngestJob.run_after > now)
            .label("deferred_count"),
            func.min(IngestJob.run_after)
            .filter(IngestJob.status == "queued", IngestJob.run_after > now)
            .label("next_deferred_at"),
            func.min(func.coalesce(IngestJob.locked_at, IngestJob.created_at)).label(
                "oldest_running_at"
            ),
        )
        .where(
            IngestJob.kind == "api_sync",
            IngestJob.status.in_(("queued", "running", "dead")),
            job_state.in_(canonical_codes),
        )
        .group_by(job_state, IngestJob.status)
    ).all():
        if row.state:
            if row.status == "queued":
                deferred_by_state[str(row.state).upper()] = (int(row.deferred_count), row.next_deferred_at)
            oldest_at = (
                row.oldest_running_at
                if row.status == "running"
                else row.oldest_queued_eligible_at
            )
            jobs_by_state[str(row.state).upper()][row.status] = (int(row.count), oldest_at)

    blocker_counts: dict[Any, tuple[int, int]] = {}
    blocker_samples: dict[Any, list[SnapshotBlockerEvidence]] = defaultdict(list)
    if jurisdiction_ids:
        active_blockers = (
            ApiSyncSnapshotBlocker.jurisdiction_id.in_(jurisdiction_ids),
            ApiSyncSnapshotBlocker.source_name == API_SYNC_SOURCE,
            ApiSyncSnapshotBlocker.active.is_(True),
        )
        for row in db.execute(
            select(
                ApiSyncSnapshotBlocker.jurisdiction_id,
                func.count().label("active_count"),
                func.count().filter(ApiSyncSnapshotBlocker.bill_id.is_(None)).label("without_bill_count"),
            ).where(*active_blockers).group_by(ApiSyncSnapshotBlocker.jurisdiction_id)
        ):
            blocker_counts[row.jurisdiction_id] = (int(row.active_count), int(row.without_bill_count))

        # Bound returned rows per jurisdiction in SQL. Counts above retain the
        # complete signal even when only a small, deterministic sample is shown.
        ranked = select(
            ApiSyncSnapshotBlocker.id,
            ApiSyncSnapshotBlocker.jurisdiction_id,
            ApiSyncSnapshotBlocker.bill_id,
            ApiSyncSnapshotBlocker.component,
            ApiSyncSnapshotBlocker.record_cap,
            ApiSyncSnapshotBlocker.first_seen_at,
            ApiSyncSnapshotBlocker.last_seen_at,
            func.row_number().over(
                partition_by=ApiSyncSnapshotBlocker.jurisdiction_id,
                order_by=(ApiSyncSnapshotBlocker.first_seen_at, ApiSyncSnapshotBlocker.id),
            ).label("sample_rank"),
        ).where(*active_blockers).subquery()
        for row in db.execute(
            select(ranked).where(ranked.c.sample_rank <= MAX_SNAPSHOT_BLOCKER_SAMPLES)
            .order_by(ranked.c.jurisdiction_id, ranked.c.sample_rank)
        ):
            blocker_samples[row.jurisdiction_id].append(SnapshotBlockerEvidence(
                blocker_id=str(row.id), bill_id=str(row.bill_id) if row.bill_id is not None else None,
                component=row.component, record_cap=row.record_cap,
                first_seen_at=row.first_seen_at, last_seen_at=row.last_seen_at,
            ))

    result = []
    for abbreviation in canonical_codes:
        jurisdiction = jurisdictions_by_code.get(abbreviation)
        if jurisdiction is None:
            result.append(
                JurisdictionEvidence(
                    abbreviation=abbreviation,
                    name="Missing canonical jurisdiction record",
                    exists=False,
                    cadence_tier=None,
                    cadence_minutes=None,
                    bills=BillEvidence(),
                    latest_run=None,
                    latest_successful_run=None,
                    latest_api_sync_run=None,
                    latest_successful_api_sync=None,
                    coverage=None,
                )
            )
            continue
        refresh_target = targets.get(jurisdiction.id)
        tier = refresh_target.cadence_tier if refresh_target else None
        target = refresh_target.cadence_minutes if refresh_target else None
        aggregate_coverage = aggregate_coverage_by_jurisdiction.get(jurisdiction.id)
        selected_coverage = (
            coverage_by_selected_session.get(refresh_target.session_id)
            if refresh_target is not None
            else None
        ) or aggregate_coverage
        jobs = jobs_by_state[jurisdiction.abbreviation.upper()]
        result.append(
            JurisdictionEvidence(
                abbreviation=jurisdiction.abbreviation.upper(),
                name=jurisdiction.name,
                cadence_tier=tier,
                cadence_minutes=target,
                bills=bills.get(jurisdiction.id, BillEvidence()),
                latest_run=latest_runs_by_jurisdiction.get(jurisdiction.id),
                latest_successful_run=latest_successes_by_jurisdiction.get(jurisdiction.id),
                latest_api_sync_run=latest_api_sync_runs_by_jurisdiction.get(jurisdiction.id),
                latest_successful_api_sync=latest_api_sync_successes_by_jurisdiction.get(
                    jurisdiction.id
                ),
                coverage=selected_coverage,
                aggregate_coverage_signal=(
                    aggregate_coverage if selected_coverage is not aggregate_coverage else None
                ),
                dead_api_sync_jobs=jobs.get("dead", (0, None))[0],
                queued_api_sync_jobs=jobs.get("queued", (0, None))[0],
                running_api_sync_jobs=jobs.get("running", (0, None))[0],
                oldest_queued_api_sync_at=jobs.get("queued", (0, None))[1],
                oldest_running_api_sync_at=jobs.get("running", (0, None))[1],
                deferred_api_sync_jobs=deferred_by_state.get(abbreviation, (0, None))[0],
                next_deferred_api_sync_at=deferred_by_state.get(abbreviation, (0, None))[1],
                active_snapshot_blockers=blocker_counts.get(jurisdiction.id, (0, 0))[0],
                snapshot_blockers_without_local_bill=blocker_counts.get(jurisdiction.id, (0, 0))[1],
                snapshot_blocker_samples=tuple(blocker_samples.get(jurisdiction.id, ())),
            )
        )
    return result


def collect_report(db: OrmSession, *, now: datetime | None = None) -> dict[str, Any]:
    # Callers establish their own read-only transaction and statement timeout.
    # This keeps API policy authoritative and lets the CLI use the same pure,
    # side-effect-free query function.
    now = _utc(now) or datetime.now(timezone.utc)
    return build_report(collect_evidence(db, now=now), now=now)


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "Bill Commons Data Reliability Control Plane (read-only)",
        f"generated at: {report['generated_at']}",
        "official freshness: UNVERIFIED (no official-source fetch or stored differential)",
        (
            f"jurisdictions: {summary['jurisdiction_count']}; defects: {summary['defect_count']} "
            f"(critical={summary['defects_by_severity']['critical']}, "
            f"error={summary['defects_by_severity']['error']}, "
            f"warning={summary['defects_by_severity']['warning']})"
        ),
    ]
    for defect in report["defects"]:
        lines.append(
            f"[{defect['severity'].upper()}] {defect['jurisdiction']} {defect['code']}: {defect['message']}"
        )
    return "\n".join(lines)


def exit_code(report: dict[str, Any], fail_on: str | None) -> int:
    if fail_on is None:
        return 0
    threshold = FAIL_ON_ORDER[fail_on]
    return int(any(SEVERITY_ORDER[defect["severity"]] <= threshold for defect in report["defects"]))
