# Crawl alert flapping — September 15, 2026

This document records the pre-fix diagnosis and the installed alert-policy
correction. The incident concerned the internal full-text ingester; it was
separate from website/API availability and external bot traffic. The correction
changes health classification and notification wording. It does not remediate
the underlying crawler, upstream assignment delays, or a broken top-up worker.

## Observed pattern

Read-only inspection before the correction of the active systemd unit, its
journal, the active Python module, and production aggregates established:

- The timer runs the original checkout's `infra/monitoring/crawl_stall_monitor.py`
  every ten minutes. Its healthcheck resolves to that checkout and its database
  binding matches the production database.
- Across 2,000 logged checks from September 1 through September 15, there were
  144 status switches: 72 red transitions whose reason was an empty fetch queue,
  and 72 green transitions whose reason was waiting on an upstream assignment.
  None of those green transition messages reported production of text.
- On September 13 at 16:20 Eastern, red reported an empty queue and no text for
  812 minutes. At 16:30, green reported 133 jobs waiting on upstream. The same
  pattern repeated at 16:50/17:00 and through the evening.
- At September 15, 19:42 UTC, all 176 queued fetch jobs were Massachusetts
  `ma_docket_no_bill_number` documents. The check reported zero actionable jobs,
  zero newly texted documents in the last hour, and 770 minutes since its last
  text timestamp, while returning `healthy: true` because the jobs awaited an
  upstream assignment.

Journal state switches are not delivery receipts; this diagnosis does not claim
that every attempted Telegram message was delivered.

## Pre-fix execution path and cause

The pre-fix `billcommons_ingest.healthcheck.check_crawl_health` excluded
upstream-awaiting queued jobs from its `claimable_now` count. If those were the
only queued jobs, it returned healthy with a waiting-upstream reason.

However, its separate `backlog_remains` query still includes those same
upstream-awaiting documents. Its starvation condition is
`queued_total == 0 and backlog_remains`. When no jobs remain queued, that path
returns stalled once the last text timestamp is old enough—even though the
documents still await the same external event.

The queue lifecycle permits this empty interval. A fetch job can reach its job
attempt limit and become dead while the Massachusetts grace rule leaves its
document retryable. A later top-up can create a new job for the document.
The pre-fix health predicates interpreted the empty phase as red and the
re-enqueued upstream-waiting phase as green. The journal pattern is consistent
with this lifecycle; it does not retain a complete per-job trace for every
historical switch.

The pre-fix notification wrapper turned any healthy result after stalled into
“crawl recovered.” It did not require a newer text timestamp or a productive
result. That wording converted a queue classification change into a misleading
recovery claim.

Two adjacent weaknesses matter when correcting this:

- Starvation counts queued jobs but does not account for currently running jobs.
  An all-running queue is not by itself evidence that top-up is broken.
- Backlog terminal-status matching uses exact strings, while the full-text
  enqueue path also recognizes decorated notes such as
  `fulltext_status=permanently_failed browser_attempted_at=...`.

## Pre-fix notification noise defect

The wrapper compared the six-hour reminder threshold with the original incident
start, preserved that start after each reminder, and stored no last-alert time.
Once an uninterrupted stall passed six hours, every subsequent ten-minute run
qualified for a reminder. This was separate from the observed red/green pairs.

The pre-fix crawl wrapper also had no two-consecutive-failure gate, despite the
incident runbook's general description of the monitors.

## Installed corrected behavior

The reviewed runtime was installed at 21:10:17 UTC from source commit
`f61e3f677fb4a3c9cb4545bcbd1625257a058b83`. The timer was resumed and verified active.
Its installation dry run exited zero, sent no notification, and wrote no state.
The first scheduled run completed at 21:20:17 UTC with exit zero and recorded
`waiting_upstream` with zero stalled samples. See the
[sanitized installation evidence](evidence/crawl-alert-fix-20260915.json).

The installed behavior:

1. Reports `waiting_upstream` separately from `producing`, `running`,
   `idle_or_backoff`, and `stalled`. An upstream wait is never a crawl recovery.
2. Uses the actionable-document definition for starvation: a usable, untexted,
   retry-eligible document outside terminal and upstream-wait status notes. It
   accounts for actionable queued and running coverage, reports stale running
   work separately, and treats decorated status notes consistently.
3. Calls a crawl recovery only after a productive observation with a text
   timestamp strictly newer than the incident baseline. A recovery notification
   records when text was observed; it does not claim that production continues.
4. Requires two consecutive stalled samples before the initial red alert.
   Stall, recovery, and notification delivery retries use bounded six-hour
   attempt spacing.
5. Treats healthcheck failure as a separate yellow monitoring incident. A later
   valid sample can send a distinct monitoring-restored notice, which explicitly
   does not claim crawl recovery.

Focused unit coverage passed 38 tests. A disposable PostgreSQL 16 run passed 16 query cases, covering empty, upstream, decorated-status, terminal, uncovered,
queued/running, stale-running, clock-race, and recent-output classifications;
it did not mutate production data.

The correction reduces false alert transitions and misleading recovery wording.
It does not establish that the crawler is currently producing text or repair any
underlying extraction, upstream, queue, or top-up failure.
