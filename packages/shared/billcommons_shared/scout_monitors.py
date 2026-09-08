"""Durable Scout-monitor snapshot and comparison helpers.

The journal contains only durable evidence references and hashes.  It never
copies source bodies or treats a missing result as evidence of a removal.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from billcommons_schema.models import ScoutFinding, ScoutMonitor, ScoutMonitorRun, ScoutResearchJob, ScoutSource

_MAX_SNAPSHOT_SOURCES = 32


def is_operator_strategy(job: ScoutResearchJob) -> bool:
    strategy = job.strategy if isinstance(job.strategy, dict) else {}
    mode = strategy.get("mode")
    return strategy.get("user_finding") is False or (isinstance(mode, str) and mode.startswith("operator_"))


def source_snapshot(db: Session, job_id) -> dict:
    sources = list(db.scalars(select(ScoutSource).where(ScoutSource.job_id == job_id).order_by(
        ScoutSource.canonical_url, ScoutSource.id
    )).all())
    if len(sources) > _MAX_SNAPSHOT_SOURCES:
        raise ValueError("monitor_snapshot_source_limit")
    findings = list(db.scalars(select(ScoutFinding).where(ScoutFinding.job_id == job_id)).all())
    finding_ids: dict[object, list[str]] = {}
    for finding in findings:
        finding_ids.setdefault(finding.source_id, []).append(str(finding.id))
    return {"sources": [
        {
            "source_id": str(source.id),
            "canonical_url": source.canonical_url,
            "content_hash": source.content_hash,
            "raw_ref": source.raw_ref,
            "finding_ids": sorted(finding_ids.get(source.id, [])),
        }
        for source in sources
    ]}


def compare_snapshots(previous: dict | None, current: dict, *, complete: bool) -> dict:
    """Compare observed URL/hash pairs without ever asserting a removal."""
    old_sources = (previous or {}).get("sources", [])
    current_sources = current.get("sources", [])
    old_by_url = {item.get("canonical_url"): item for item in old_sources if item.get("canonical_url")}
    current_by_url = {item.get("canonical_url"): item for item in current_sources if item.get("canonical_url")}
    new = [
        {"canonical_url": url, "content_hash": item.get("content_hash")}
        for url, item in current_by_url.items() if url not in old_by_url
    ]
    changed = [
        {"canonical_url": url, "previous_content_hash": old_by_url[url].get("content_hash"), "content_hash": item.get("content_hash")}
        for url, item in current_by_url.items()
        if url in old_by_url and old_by_url[url].get("content_hash") != item.get("content_hash")
    ]
    unchanged_count = sum(
        1 for url, item in current_by_url.items()
        if url in old_by_url and old_by_url[url].get("content_hash") == item.get("content_hash")
    )
    return {
        "comparison_complete": complete,
        "absence_evaluated": False,
        "new_sources": new,
        "changed_sources": changed,
        "unchanged_source_count": unchanged_count,
        "observed_source_count": len(current_sources),
    }


def finalize_monitor_run(
    db: Session,
    monitor: ScoutMonitor,
    run: ScoutMonitorRun,
    job: ScoutResearchJob,
    *,
    status: str,
    completed_at: datetime,
) -> None:
    # The production sessionmaker deliberately uses ``autoflush=False``.
    # A terminal worker can have just-added sources/findings, and a cached
    # monitor run has no generated UUID until it is flushed.  Make both
    # durable before the evidence snapshot and baseline pointer are derived.
    db.flush()
    run.status = status
    run.completed_at = completed_at
    run.error_class = job.error_class
    if status in {"completed", "partial"}:
        snapshot = source_snapshot(db, job.id)
        baseline = db.get(ScoutMonitorRun, run.baseline_run_id) if run.baseline_run_id else None
        run.source_snapshot = snapshot
        run.change_summary = compare_snapshots(
            baseline.source_snapshot if baseline is not None else None,
            snapshot,
            complete=status == "completed",
        )
        monitor.last_completed_run_id = run.id
        monitor.consecutive_deferrals = 0


def defer_delay_seconds(cadence_seconds: int, consecutive_deferrals: int) -> int:
    """Exponential, bounded retry; no scheduler loop may spin on exhausted quota."""
    exponent = min(max(consecutive_deferrals, 0), 5)
    return min(cadence_seconds, 15 * 60 * (2**exponent))
