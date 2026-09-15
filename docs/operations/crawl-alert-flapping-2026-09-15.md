# Crawl alert flapping — September 15, 2026

The repeated red/green crawl alerts reflect inconsistent treatment of
upstream-waiting documents. A green “crawl recovered” message does not currently
require that extraction resumed. This alert concerns the internal full-text
ingester; it is separate from website/API availability and external bot traffic.

## Observed pattern

Read-only inspection of the active systemd unit, its journal, the active Python
module and production aggregates established:

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

## Execution path and cause

`billcommons_ingest.healthcheck.check_crawl_health` excludes upstream-awaiting
queued jobs from its `claimable_now` count. If those are the only queued jobs,
it returns healthy with a waiting-upstream reason.

However, its separate `backlog_remains` query still includes those same
upstream-awaiting documents. Its starvation condition is
`queued_total == 0 and backlog_remains`. When no jobs remain queued, that path
returns stalled once the last text timestamp is old enough—even though the
documents still await the same external event.

The queue lifecycle permits this empty interval. A fetch job can reach its job
attempt limit and become dead while the Massachusetts grace rule leaves its
document retryable. A later top-up can create a new job for the document.
The active health predicates interpret the empty phase as red and the
re-enqueued upstream-waiting phase as green. The journal pattern is consistent
with this lifecycle; it does not retain a complete per-job trace for every
historical switch.

The notification wrapper turns any healthy result after stalled into “crawl
recovered.” It does not require a newer text timestamp or a productive result.
That wording converts a queue classification change into a misleading recovery
claim.

Two adjacent weaknesses matter when correcting this:

- Starvation counts queued jobs but does not account for currently running jobs.
  An all-running queue is not by itself evidence that top-up is broken.
- Backlog terminal-status matching uses exact strings, while the full-text
  enqueue path also recognizes decorated notes such as
  `fulltext_status=permanently_failed browser_attempted_at=...`.

## Separate notification noise defect

The wrapper compares the six-hour reminder threshold with the original incident
start, preserves that start after each reminder, and stores no last-alert time.
Once an uninterrupted stall passes six hours, every subsequent ten-minute run
qualifies for a reminder. This is separate from the observed red/green pairs.

The active crawl wrapper also has no two-consecutive-failure gate, despite the
incident runbook's general description of the monitors. Documentation should
describe the deployed rule accurately.

## Corrective design

1. Represent `waiting_upstream` separately from productive, idle, and stalled.
   Preserve the upstream reason and last actual text timestamp. Do not call
   red-to-waiting “recovered.”
2. Use the enqueue path's definition of an actionable pending document for
   starvation, including exact/decorated terminal and upstream-wait statuses.
   Account for queued and running coverage. Detect stale running work separately
   so a stuck running job cannot hide a real failure.
3. Emit green recovery only after evidence of resumed productive work. Apply
   a bounded persistence requirement to new red states as a secondary noise
   control, while retaining prompt detection of sustained failures.
4. Track incident start and last reminder separately, with explicit send-failure
   semantics. Test the six-hour boundary and subsequent ten-minute run.

Required regressions include upstream-only documents with an empty queue;
real uncovered actionable backlog; fresh versus stale running jobs; exact and
decorated status notes; red-to-waiting versus red-to-producing notifications;
and reminder suppression after the first six-hour reminder.

This is a diagnosis and corrective design. No alert policy, timer, worker or
production queue was changed during this investigation, and no test notification
was sent.
