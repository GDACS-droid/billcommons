"""Crawl liveness check: is the pipeline actually producing, or only busy?

Motivation, from two real incidents on this project:

* 2026-07-25, ~2 hours lost. The crawl claimed ~45 jobs/minute and extracted
  zero documents. Railway reported the service `Online`, the logs scrolled
  continuously, and `queued` sat at a healthy-looking 1,215. Every signal a
  human or a platform healthcheck would look at said "fine".
* Earlier the same week, the same shape: NUL bytes stalled extraction while
  the worker stayed up.

The lesson both times is that **uptime and queue depth are not liveness**.
The only trustworthy signal is whether new `extracted_text` is landing, and
nothing was watching it. This module provides that signal, so a stall is
caught in minutes instead of being discovered by someone asking for a status
update.

Deliberately read-only and side-effect free -- it reports, it does not
restart anything. Automatic remediation on a signal this coarse would risk
restart-looping a worker that is merely slow (large PDFs legitimately push
extraction rates down several-fold), and a stalled crawl is not an emergency
that cannot wait for a human decision.

Usage:
    python -m billcommons_ingest.healthcheck            # human-readable
    python -m billcommons_ingest.healthcheck --json     # machine-readable
    python -m billcommons_ingest.healthcheck --quiet    # exit code only

Exit codes: 0 healthy, 1 STALLED, 2 check itself failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from billcommons_shared.db import get_session

from .fulltext import AWAITING_UPSTREAM_STATUSES, MAX_FETCH_ATTEMPTS, TERMINAL_STATUSES

# A document is extracted every few seconds when healthy, so 30 minutes of
# complete silence is far outside normal variance -- including the slow tail
# where a worker is grinding through large scanned PDFs. Tuned to be
# unambiguous rather than sensitive: this should never cry wolf, because an
# alert that fires on ordinary slowness gets muted and then the next real
# stall goes unnoticed for two hours again.
DEFAULT_STALL_MINUTES = 30

# Independent of extraction: are jobs eligible RIGHT NOW but nothing is
# finishing? This catches the specific 2026-07-25 shape, where a poisoned
# queue was claimable and being claimed but could never complete.
DEFAULT_IDLE_MINUTES = 30


@dataclass
class CrawlHealth:
    healthy: bool
    reason: str
    checked_at: str
    last_text_at: str | None
    minutes_since_text: float | None
    texted_last_hour: int
    claimable_now: int
    queued_total: int
    dead_total: int
    backlog_remains: bool = False
    awaiting_upstream: int = 0
    actionable_queued: int = 0
    running_total: int = 0
    stale_running: int = 0
    status: str = "idle_or_backoff"

    def render(self) -> str:
        head = "HEALTHY" if self.healthy else "STALLED"
        since = (
            f"{self.minutes_since_text:.1f} min ago"
            if self.minutes_since_text is not None
            else "never"
        )
        return (
            f"[{head}] {self.reason}\n"
            f"  status            : {self.status}\n"
            f"  checked at        : {self.checked_at}\n"
            f"  last text landed  : {self.last_text_at} ({since})\n"
            f"  texted last hour  : {self.texted_last_hour:,}\n"
            f"  claimable now     : {self.claimable_now:,}\n"
            f"  queued / dead     : {self.queued_total:,} / {self.dead_total:,}\n"
            f"  actionable queued : {self.actionable_queued:,}\n"
            f"  running / stale   : {self.running_total:,} / {self.stale_running:,}\n"
            f"  awaiting upstream : {self.awaiting_upstream:,}\n"
            f"  backlog remains   : {self.backlog_remains}"
        )


def check_crawl_health(
    db,
    *,
    stall_minutes: int = DEFAULT_STALL_MINUTES,
    idle_minutes: int = DEFAULT_IDLE_MINUTES,
    now: datetime | None = None,
) -> CrawlHealth:
    """Assess whether the full-text crawl is making progress.

    STALLED is reported only when work is genuinely available and still
    nothing is being produced. A quiet crawl with an empty queue is idle, not
    stalled -- reporting that as a failure would make the check useless
    exactly when the corpus is finished.
    """
    now = now or datetime.now(timezone.utc)
    scalar = lambda sql, **p: db.execute(text(sql), p).scalar()  # noqa: E731

    last_text_at = scalar(
        "select max(updated_at) from bill_documents where extracted_text is not null"
    )
    texted_last_hour = scalar(
        "select count(*) from bill_documents where extracted_text is not null "
        "and updated_at > :cutoff",
        cutoff=now - timedelta(hours=1),
    )
    # The top-up and health checks must agree on what work remains.  In
    # particular, `_mark_status` decorates terminal/upstream notes, so a bare
    # equality check would resurrect a permanently finished document as a
    # healthcheck backlog item.  Upstream-awaiting documents are deliberately
    # excluded too: they cannot produce text until the source assigns them.
    terminal_notes = [f"fulltext_status={status}" for status in TERMINAL_STATUSES]
    awaiting_notes = [f"fulltext_status={status}" for status in AWAITING_UPSTREAM_STATUSES]
    terminal_predicate = (
        "split_part(coalesce(d.license_note, ''), ' ', 1) = any(:terminal)"
    )
    awaiting_predicate = (
        "split_part(coalesce(d.license_note, ''), ' ', 1) = any(:awaiting)"
    )
    actionable_document = (
        "d.extracted_text is null and d.url is not null and d.url <> '' "
        "and coalesce(d.fetch_attempts, 0) < :max_fetch_attempts "
        f"and (d.license_note is null or not {terminal_predicate} and not {awaiting_predicate})"
    )
    job_has_actionable_document = (
        "exists (select 1 from bill_documents d "
        "where j.payload->>'document_id' = d.id::text "
        f"and {actionable_document})"
    )
    claimable_now = scalar(
        "select count(*) from ingest_jobs j where j.kind='fetch_text' "
        "and j.status='queued' and j.run_after <= :now "
        f"and {job_has_actionable_document}",
        now=now,
        terminal=terminal_notes,
        awaiting=awaiting_notes,
        max_fetch_attempts=MAX_FETCH_ATTEMPTS,
    )
    awaiting_upstream = scalar(
        "select count(*) as awaiting_upstream from bill_documents d "
        "where d.extracted_text is null and d.url is not null and d.url <> '' "
        "and coalesce(d.fetch_attempts, 0) < :max_fetch_attempts "
        f"and {awaiting_predicate}",
        awaiting=awaiting_notes,
        max_fetch_attempts=MAX_FETCH_ATTEMPTS,
    )
    queued_total = scalar("select count(*) from ingest_jobs where kind='fetch_text' and status='queued'")
    dead_total = scalar("select count(*) from ingest_jobs where kind='fetch_text' and status='dead'")

    # A queued or running job covers its document.  Looking for uncovered work
    # instead of merely an empty queue prevents a freshly claimed job from
    # reading as a broken top-up, while an old locked job is handled below.
    backlog_remains = bool(
        scalar(
            "select exists (select 1 from bill_documents d "
            f"where {actionable_document} limit 1)",
            terminal=terminal_notes,
            awaiting=awaiting_notes,
            max_fetch_attempts=MAX_FETCH_ATTEMPTS,
        )
    )
    uncovered_actionable = bool(
        scalar(
            "select exists (select 1 from bill_documents d "
            f"where {actionable_document} and not exists (select 1 from ingest_jobs j "
            "where j.kind='fetch_text' and j.status in ('queued', 'running') "
            "and j.payload->>'document_id' = d.id::text) limit 1)",
            terminal=terminal_notes,
            awaiting=awaiting_notes,
            max_fetch_attempts=MAX_FETCH_ATTEMPTS,
        )
    )
    actionable_queued = scalar(
        "select count(*) as actionable_queued from ingest_jobs j "
        "where j.kind='fetch_text' and j.status='queued' "
        f"and {job_has_actionable_document}",
        terminal=terminal_notes,
        awaiting=awaiting_notes,
        max_fetch_attempts=MAX_FETCH_ATTEMPTS,
    )
    running_total = scalar(
        "select count(*) as running_total from ingest_jobs j "
        "where j.kind='fetch_text' and j.status='running' "
        f"and {job_has_actionable_document}",
        terminal=terminal_notes,
        awaiting=awaiting_notes,
        max_fetch_attempts=MAX_FETCH_ATTEMPTS,
    )
    stale_running = scalar(
        "select count(*) as stale_running from ingest_jobs j "
        "where j.kind='fetch_text' and j.status='running' "
        "and (j.locked_at is null or j.locked_at <= :running_cutoff) "
        f"and {job_has_actionable_document}",
        running_cutoff=now - timedelta(minutes=idle_minutes),
        terminal=terminal_notes,
        awaiting=awaiting_notes,
        max_fetch_attempts=MAX_FETCH_ATTEMPTS,
    )
    fresh_running = max(0, int(running_total or 0) - int(stale_running or 0))

    # A top-up may deliberately operate in bounded batches, so an uncovered
    # actionable document is not itself a failure while ANY actionable job is
    # queued or running.  Starvation means no actionable coverage at all.
    starved = (
        uncovered_actionable
        and int(actionable_queued or 0) == 0
        and int(running_total or 0) == 0
    )

    minutes_since = None
    if last_text_at is not None:
        if last_text_at.tzinfo is None:
            last_text_at = last_text_at.replace(tzinfo=timezone.utc)
        # The app clock is sampled before the database query.  A text commit
        # in that tiny interval can be a few milliseconds newer than ``now``;
        # do not turn that benign race into negative liveness time.
        minutes_since = max(0.0, (now - last_text_at).total_seconds() / 60.0)

    stale = minutes_since is None or minutes_since >= stall_minutes
    if stale_running and stale:
        healthy = False
        status = "stalled"
        reason = (
            f"{int(stale_running):,} fetch_text job(s) running longer than "
            f"{idle_minutes} min while extraction is stale"
        )
    elif claimable_now and stale:
        healthy = False
        status = "stalled"
        reason = (
            f"{claimable_now:,} jobs claimable but nothing extracted for "
            f"{minutes_since:.0f} min (threshold {stall_minutes})"
            if minutes_since is not None
            else f"{claimable_now:,} jobs claimable but no document has EVER been extracted"
        )
    elif starved and stale:
        # Uncovered work means the TOP-UP is broken,
        # not that the crawl finished. This is the 2026-07-26 shape: the
        # enqueue query began failing (DiskFull on a parallel worker's shared
        # memory segment), the queue drained to zero, and the first version of
        # this check read that as "idle, not stalled" -- it sent a RECOVERED
        # alert while the crawl was dead, and the stall ran for two hours.
        # An empty queue is only good news when there is nothing left to fetch.
        #
        healthy = False
        status = "stalled"
        reason = (
            "uncovered actionable documents remain -- top-up is not producing coverage"
            + (
                f" (nothing extracted for {minutes_since:.0f} min)"
                if minutes_since is not None
                else " (nothing has EVER been extracted)"
            )
        )
    elif int(texted_last_hour or 0) > 0 and not stale:
        # A newly landed document is the only affirmative liveness signal.
        # Keep it visible even when the queue has drained, remaining documents
        # await upstream assignment, or the last job has just been claimed.
        healthy = True
        status = "producing"
        reason = f"producing -- {texted_last_hour:,} documents extracted in the last hour"
    elif fresh_running and not claimable_now:
        healthy = True
        status = "running"
        reason = f"{fresh_running:,} actionable fetch_text job(s) running, not stalled"
    elif claimable_now:
        healthy = True
        status = "idle_or_backoff"
        reason = (
            f"{claimable_now:,} jobs claimable inside the {stall_minutes}-min "
            "liveness window; no new text observed"
        )
    elif awaiting_upstream and not backlog_remains:
        healthy = True
        status = "waiting_upstream"
        reason = (
            f"no actionable fetch_text work -- {awaiting_upstream:,} document(s) waiting on an "
            "upstream assignment, not stalled"
        )
    else:
        healthy = True
        status = "idle_or_backoff"
        reason = (
            "no actionable fetch_text work, backlog covered by queued work -- idle/backoff"
            if backlog_remains
            else "no actionable fetch_text work -- idle, not stalled"
        )

    return CrawlHealth(
        healthy=healthy,
        reason=reason,
        checked_at=now.isoformat(timespec="seconds"),
        last_text_at=last_text_at.isoformat(timespec="seconds") if last_text_at else None,
        minutes_since_text=round(minutes_since, 1) if minutes_since is not None else None,
        texted_last_hour=int(texted_last_hour or 0),
        claimable_now=int(claimable_now or 0),
        queued_total=int(queued_total or 0),
        dead_total=int(dead_total or 0),
        backlog_remains=backlog_remains,
        awaiting_upstream=int(awaiting_upstream or 0),
        actionable_queued=int(actionable_queued or 0),
        running_total=int(running_total or 0),
        stale_running=int(stale_running or 0),
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bill Commons crawl liveness check")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--quiet", action="store_true", help="exit code only")
    parser.add_argument("--stall-minutes", type=int, default=DEFAULT_STALL_MINUTES)
    args = parser.parse_args(argv)

    db = get_session()
    try:
        health = check_crawl_health(db, stall_minutes=args.stall_minutes)
    except Exception as exc:  # noqa: BLE001 - a failed check must not read as healthy
        if not args.quiet:
            print(f"[CHECK-FAILED] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()

    if not args.quiet:
        print(json.dumps(asdict(health), indent=2) if args.json else health.render())
    return 0 if health.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
