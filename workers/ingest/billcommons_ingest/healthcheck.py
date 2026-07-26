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

from .fulltext import TERMINAL_STATUSES

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

    def render(self) -> str:
        head = "HEALTHY" if self.healthy else "STALLED"
        since = (
            f"{self.minutes_since_text:.1f} min ago"
            if self.minutes_since_text is not None
            else "never"
        )
        return (
            f"[{head}] {self.reason}\n"
            f"  checked at        : {self.checked_at}\n"
            f"  last text landed  : {self.last_text_at} ({since})\n"
            f"  texted last hour  : {self.texted_last_hour:,}\n"
            f"  claimable now     : {self.claimable_now:,}\n"
            f"  queued / dead     : {self.queued_total:,} / {self.dead_total:,}\n"
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
    claimable_now = scalar(
        "select count(*) from ingest_jobs where kind='fetch_text' "
        "and status='queued' and run_after <= :now",
        now=now,
    )
    queued_total = scalar("select count(*) from ingest_jobs where kind='fetch_text' and status='queued'")
    dead_total = scalar("select count(*) from ingest_jobs where kind='fetch_text' and status='dead'")

    # Is there fetchable text left in the corpus that no job covers? An empty
    # queue only means "finished" if this is false. EXISTS with LIMIT 1 so the
    # cost is a partial scan, not a count over ~700k rows.
    terminal_notes = tuple(f"fulltext_status={status}" for status in TERMINAL_STATUSES)
    backlog_remains = bool(
        scalar(
            "select exists (select 1 from bill_documents "
            "where extracted_text is null and url is not null and url <> '' "
            "and (license_note is null or license_note <> all(:terminal)) limit 1)",
            terminal=list(terminal_notes),
        )
    )

    minutes_since = None
    if last_text_at is not None:
        if last_text_at.tzinfo is None:
            last_text_at = last_text_at.replace(tzinfo=timezone.utc)
        minutes_since = (now - last_text_at).total_seconds() / 60.0

    starved = queued_total == 0 and backlog_remains
    if starved and (minutes_since is None or minutes_since >= stall_minutes):
        # An EMPTY queue with work still to do means the TOP-UP is broken,
        # not that the crawl finished. This is the 2026-07-26 shape: the
        # enqueue query began failing (DiskFull on a parallel worker's shared
        # memory segment), the queue drained to zero, and the first version of
        # this check read that as "idle, not stalled" -- it sent a RECOVERED
        # alert while the crawl was dead, and the stall ran for two hours.
        # An empty queue is only good news when there is nothing left to fetch.
        #
        # Keyed on queued_total, not claimable_now: a queue full of backed-off
        # jobs is retrying, not starved, and must not read as a broken top-up.
        healthy = False
        reason = (
            "fetch_text queue is EMPTY but unfetched documents remain -- "
            "top-up is not producing jobs"
            + (
                f" (nothing extracted for {minutes_since:.0f} min)"
                if minutes_since is not None
                else " (nothing has EVER been extracted)"
            )
        )
    elif claimable_now == 0:
        healthy = True
        reason = (
            "no claimable fetch_text work, backlog pending -- top-up due, not stalled"
            if backlog_remains
            else "no claimable fetch_text work -- idle, not stalled"
        )
    elif minutes_since is None:
        healthy, reason = False, f"{claimable_now:,} jobs claimable but no document has EVER been extracted"
    elif minutes_since >= stall_minutes:
        healthy = False
        reason = (
            f"{claimable_now:,} jobs claimable but nothing extracted for "
            f"{minutes_since:.0f} min (threshold {stall_minutes})"
        )
    else:
        healthy = True
        reason = f"producing -- {texted_last_hour:,} documents extracted in the last hour"

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
