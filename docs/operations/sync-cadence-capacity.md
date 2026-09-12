# Incremental sync cadence and request capacity

## September 12, 2026 observation

The 05:22 UTC public Data Health capture contains 51 jurisdictions: 12 use the
30-minute active-session target, five use the hourly year-round target, and
34 use the weekly dormant target. These are scheduling classifications derived
from stored session fields, not newly verified legislative-session facts.

At one request per jurisdiction per sync, maintaining those targets requires:

| Tier | Jurisdictions | Syncs per day each | Minimum requests per day |
| --- | ---: | ---: | ---: |
| Active | 12 | 48 | 576 |
| Year-round | 5 | 24 | 120 |
| Dormant | 34 | 1/7 | 34/7 |
| Total, averaged over a week | 51 | — | 700.86 |

The read-only production budget check at 05:42 UTC showed an OpenStates ledger
limit of 225 requests per UTC day and a ten-second minimum request interval.
The ledger still referred to September 11: 35 requests reserved, last updated
at 06:12:24 UTC. This is a prior-day observation, not today's consumed balance;
the admission implementation rolls the date when a new request is reserved.
The configured limit is a local safety policy, not proof of the provider's
current account entitlement.

The minimum demand is 3.11 times the configured allowance. Pagination, retries,
session-date refreshes, backfills and other account consumers add cost. An
empty change result still costs a request. A shorter worker sleep by itself
cannot deliver the targets within this budget.

## Existing execution behavior

`infra/docker/Dockerfile.sync-worker` starts the dedicated metadata worker.
`cmd_sync_worker` defaults to an 86,400-second interval, runs the scheduling
pass, drains eligible jobs, recomputes touched bill status and coverage, and
tops up session dates. The release record confirms that daily interval for
the running service. Thus a scheduler target of 30 minutes does not imply a
worker runs every 30 minutes.

The shared budget commits reservations independently of ingestion, so a failed
bill or rolled-back job does not refund requests already sent. Pending jobs
and pagination continuations suppress duplicate scheduling. Quota exhaustion
defers queue work rather than spending its failure-attempt allowance. Preserve
all three behaviors when changing cadence.

## Next operational decision

First obtain a complete, bounded observation of per-cycle request demand,
including pages, retries and session-date calls. The retained daily ledger
cannot attribute the 35 requests to individual runs or establish a worst-case
future cost. Test the chosen policy against backlog, continuation and quota
exhaustion before rollout.

A policy must explicitly choose which jurisdictions receive the finest
cadence, account for all shared consumers, and leave retry/backfill capacity.
Meeting the current targets for every jurisdiction requires more than the
701-request daily average floor; the required headroom remains unmeasured.
If the allowance stays 225, select a slower or prioritized policy and display
its execution commitment separately from desired freshness. Keep actual
lateness visible. Do not change targets simply to clear warnings.

No request allowance, service interval, session flag, queue job or production
record was changed during this analysis. Any production rollout remains held
by the migration-ownership issue recorded in `autonomous-intelligence.md`.

The restricted release evidence directory contains
`public-data-health-20260912T052225Z.json`,
`openstates-cadence-budget-20260912.json`, and
`sync-cadence-capacity-analysis-20260912.json` with the captures and calculation.
