# Data Reliability Control Plane — 2026-09-08

Goal: make ingestion failures, overdue local refreshes, and missing provenance
visible and reproducible, while separating local activity from verified
agreement with official sources.

This is the first operational slice. It is not a claim of autonomous 50-state
repair, complete official reconciliation, or a 99.9% freshness SLA.

## Implemented surfaces

- `GET /api/v1/data-health`: public aggregate observation, generated through a
  read-only, repeatable-read transaction. Each statement has a five-second
  timeout. A five-minute process-local cache and nonblocking refresh lock
  prevent concurrent aggregate scans. Expired success is never returned after
  a refresh failure; unavailable/busy checks return 503 with retry guidance.
- `/data-health`: jurisdiction search, issue filter, evidence disclosures,
  observation timestamp, and explicit unavailable/recovery states. The page
  adds no cache on top of the API cache. Coverage and footer link to it.
- `python -m billcommons_ingest.data_health --json --fail-on error`: the same
  read-only report for an operator or bounded monitor. Exit 0 means the check
  completed below the requested severity threshold, 1 means the threshold was
  reached, and 2 means the check itself failed. Raw exceptions are redacted.
- `python -m billcommons_shared.reconciliation --official FILE --local FILE`:
  fixture-only differential comparison. See [the adapter lab](reconciliation-lab.md).
  It preserves occurrence ambiguity and evidence, records missing/local-only/
  mismatched/uncertain observations, and performs no database mutation.
- The scheduler now refuses to enqueue a second incremental sync while any
  matching job is queued/running, including delayed retries and pagination
  continuations. Dead jobs retain existing rescheduling behavior. Older pending
  work cannot be hidden by a newer completed job.

## Interpretation

`LOCAL_SYNC_OVERDUE` compares the last successful `openstates_api_sync` with
the scheduler policy, not the most recent bulk import or repair. A recent
one-off repair therefore cannot conceal a lapse in incremental synchronization.
Source-specific API failures remain visible even if another adapter succeeds.

Pending jobs older than `max(2 × scheduling target, 60 minutes)` are suspected
stalls requiring inspection. The report never reclaims a live job or treats a
long legitimate backoff as proof of a crashed worker. The sync worker has no
Scout-style lease/heartbeat recovery contract; safe recovery must establish
that the prior owner is no longer executing before changing job ownership.

Coverage selects the scheduler's active/relevant session, falling back to a
jurisdiction aggregate. It never sums aggregate and session counts. A degraded
aggregate remains a separately scoped signal. Initial live testing exposed and
repaired an incorrect assumption that every jurisdiction had a NULL-session
coverage row; the final snapshot below uses the repaired selection.

`official_freshness` remains `unverified`, and `official_reconciliation` remains
`unavailable`: no live official comparison is performed by this report. Stored
bill provenance counts describe the local corpus, not source completeness.

## Production observation, read-only

At **2026-09-08 01:11:03 UTC**, the corrected collector observed 51 jurisdictions
(50 states plus DC) and 17 overdue incremental-sync warnings. Review caught
an unsupported test jurisdiction inflating the earlier count; the public
collector now restricts its scope to the canonical registry codes, without
changing production rows. No critical/error-level defect was found
by this implemented check set. This is not a general production-health verdict.

The aggregate, per-jurisdiction evidence is retained in
[data-health-summary.json](evidence/reliability-20260908/data-health-summary.json).
It contains no customer identities, keys, raw job payloads, or credential values.

The actual deployed sync worker has `SYNC_WORKER_INTERVAL=86400` and
`SYNC_WORKER_MAX_JOBS=200`. Its scheduling policy contains finer 30-minute/hourly
targets, but execution happens at the loop's nightly cadence. The upstream
client defaults to a **225-request per-process daily brake**, not a shared quota
ledger. Increasing worker frequency or replica count alone is not a defensible
way to promise the finer targets.

California had 26 completed `api_sync` jobs in the prior 30 days and no queued
CA sync job at 00:38:59 UTC. The latest job was created at 2026-09-07 05:44:55 UTC.
The official-action sweep remains a standalone script; no checked-in timer
invokes it. Incremental metadata updates do not prove that this official sweep
will recur. Relevant execution paths:

- `workers/ingest/billcommons_ingest/autoboot.py`: initial bootstrap and crawl loop.
- `workers/ingest/billcommons_ingest/cli.py::cmd_sync_worker`: schedule, drain,
  continuation handling, status recomputation, session dates, coverage, sleep.
- `workers/ingest/billcommons_ingest/api_sync.py`: bounded pages and preserved
  update window; versions/documents included, votes/subjects excluded.
- `scripts/sync_ca_official_actions.py`: separate official-action sweep.

## Commercial and discovery findings

At **2026-09-08 00:38:59 UTC**, independently repeated production aggregate
queries found 2 customer rows, 1 usable live free key, 3 metered requests in the
last seven calendar days, zero paid keys, zero active/dunning subscriptions,
zero Stripe event ledger rows, and zero snapshot entitlements. These counts do
not establish two independent users or organic demand. No payment is recorded
through the application path; Stripe-account revenue remains unproven.

The deployed restricted Stripe key returned HTTP 403 for account, webhook-list,
and configured-price inspection. This establishes insufficient read scope,
not missing endpoints or invalid prices. The existing [Stripe activation gate](monetization-runbook.md)
still needs the correct account owner's Live Workbench evidence. No charge,
credential change, or payment configuration update was performed.

Web Analytics is now reported enabled. The Vercel CLI command used during the
audit (`project web-analytics`) is an enable operation and may have enabled it;
prior enablement was not independently established. No plan was upgraded.
A subsequent authenticated GET of `/v9/projects/billcommons-web` and `/v2/teams`
confirmed the owning team's **Pro** plan and enabled Analytics with no disabled
marker. Custom-event ingestion remains unverified; the observed plan is
eligible under Vercel's [Pro/Enterprise custom-event requirement](https://vercel.com/docs/analytics/custom-events).

New funnel events distinguish accepted magic-link requests, checkout intent,
created checkout redirects, and API-key reveal operations. Their property
allowlist excludes email, query text, keys, URLs, and session identifiers;
analytics exceptions cannot break product flows. These browser events are not
payment-completion evidence.

Google/Bing verification metadata is configurable through server environment
variables `GOOGLE_SITE_VERIFICATION` and `BING_SITE_VERIFICATION`. Deploying tags
does not complete console ownership verification; follow the actual
[Google](https://support.google.com/webmasters/answer/9008080?hl=en-GB) and
[Bing](https://www2.bing.com/webmasters/help/add-and-verify-site-12184f8b) procedures.
Neither console was accessed or a sitemap submitted during this mission.
The sitemap now includes pricing/access/integration documentation and omits the
unpopulated hearings page. Existing robots, llms.txt, JSON-LD and Analytics
instrumentation were preserved. Indexing, rankings, and AI-answer inclusion
are not established by these code changes.

## Validation

Run deterministic checks against a disposable PostgreSQL cluster, without
falling back to production credentials:

```bash
pg_virtualenv -v 16 /path/to/venv/bin/python scripts/test_data_reliability.py
cd apps/web
node --test app/discovery-surface.test.mjs lib/funnel.test.mjs lib/scout.test.mjs lib/scout-access.test.mjs components/SiteHeader.test.mjs components/scout/ScoutExperience.test.mjs
npm run lint -- --quiet
npm run build
```

The earlier integration run passed 74 shared/ingest tests and 7 API tests;
these are historical results, not counts for the current release candidate.
Current candidate results and review adjudication follow below. The API tests
include cache expiry, single-flight behavior, redacted failure, endpoint
registration, read-only SQL, and connection cleanup. The 22 targeted web tests,
lint, TypeScript and production build passed. The installed Starlette test
client reports a dependency deprecation warning; it is not suppressed.

Local Chromium interactions exercised the production-observation fixture:
51-row rendering, state search, evidence expansion, issue filtering, no-match
and clear-filter behavior, a 390px viewport without horizontal overflow,
upstream 503 unavailable state, and recovery. No JavaScript page error occurred.
Screenshots in `evidence/reliability-20260908` are local development renders,
not proof of deployment. The Impeccable detector returned no findings.

## Independent review and adjudication

One canonical `verify-ship` pass reviewed aggregate commit
`8c0eb4cf6503c2b8c250ecb1854096cdfb754fad`, byte-equivalent to the mission diff
through integration commit `2047c4b`. Its verdict was **HALT**: Codex, Opus and
DeepSeek returned BLOCK, and OX was unavailable after its built-in retry.
The advocate phase was skipped. The final repaired revision has not received
a passing multi-model verdict; no second fan-out was started.

Confirmed issues were repaired and the affected deterministic checks rerun:
missing refresh configuration now produces an error; unsupported jurisdiction
rows are excluded; missing job lock timestamps fall back to creation time;
null run timestamps sort last; duplicated identities stay ambiguous even with
an empty counterpart; database cleanup errors remain redacted and sessions are
closed after rollback failure; the existing feedback sitemap route is retained.

The reported crash-orphan concern was checked against the actual execution
path. `queue.claim_job` only flushes; `cmd_sync_worker` commits the claim, sync
writes, continuation and completion together. A crash rolls that transaction
back to queued. Therefore this path does not leave a committed running row on
crash. A historical/manually committed running row can still suppress new work
and needs explicit owner-death investigation; age never authorizes a duplicate.
The current deployment remains single-replica.

The busy-cache 503 is deliberate and tested: it bounds database demand without
serving expired success. The login event's fixed surface matches its sole
current caller, and API link configuration uses the existing absolute-base-URL
contract. These observations do not replace the missing independent verdict.

## Release preflight

Verdict: **INSUFFICIENT EVIDENCE for production release**. Implementation and
local validation do not supply production authorization or resolve the
pre-existing billing activation gate. No production deployment, migration,
bulk ingest, official sweep, notification, or outreach was performed.

An independent release-guard pass reached **NOT READY** for the executable
release decision: it requires a release owner/window, refreshed artifact and
health evidence, applicable backup/restore proof, the checkout-specific Stripe
gate, and an actual safe sync-worker boundary. A nightly worker's complete-cycle
proof does not fit inside a 45-minute session without a separately authorized,
bounded cycle. This is a release hold, not a claim that deployment occurred.

Affected release units are API (shared report and new route), Vercel web, and
sync worker (scheduler). MCP, Scout and other workers need no rollout for this
slice. No schema change is required; production remains at Alembic `0025`.
Older clients retain their routes and schemas. Web deployed ahead of API would
show an honest unavailable state, so deploy API before web.

Observed rollback handles (reconfirm immediately before an authorized release):

| Unit | Previous active artifact |
| --- | --- |
| API | `5828ea8b-6a85-4ca6-b453-f76e604ad374` |
| Sync worker | `568fcd62-89b9-4e1c-bdb1-77c4ebae4759` |
| Vercel web | `dpl_CQFDoxFWuZYvacaLEqiEKex5YnRG` |

Provider metadata did not attest immutable source SHAs for these artifacts.
Pin the new source and resulting deployment IDs per the [deployment runbook](deployment-runbook.md).
Keep the API and sync worker at their currently observed single replicas.

Proposed ordering after the existing gates are satisfied:

1. Deploy the pinned API artifact; check `/api/v1/health` body and
   `/api/v1/data-health` schema/time, ordinary search, and error/DB-pool metrics.
2. Deploy web; check the data-health unavailable/recovery path, source links,
   console metadata, and privacy-safe analytics. Verify Stripe configuration
   under the existing checkout-UI release gate before taking traffic.
3. Release the scheduler separately at a verified sync cycle boundary. The
   current sync loop only catches KeyboardInterrupt, so do not borrow Scout's
   graceful drain guarantee or terminate active sync work. If an idle boundary
   cannot be established, hold the worker change.
4. Observe API/web for 15 minutes and a complete authorized sync cycle. Roll
   back the affected application artifact on repeated new 5xx, query timeouts,
   materially worse pool saturation, duplicate job dispatch, or evidence
   misrepresentation. Preserve all existing database rows and raw archives.

## Next bounded engineering slices

1. Persist source-specific official observations and reconciliation reports,
   starting with California, with source timestamps and retained checksums.
2. Add a durable shared upstream quota ledger and source-aware cadence before
   making stronger freshness promises; preserve continuation checkpoints.
3. Establish sync-job leases/heartbeats and a tested owner-death recovery path.
4. Expand the adapter laboratory with recorded California and Florida fixtures,
   then generate bounded repair patches for human review rather than mutating
   production from a parser failure alone.
5. Productize saved monitoring: evidence-backed changes into an authenticated
   team inbox and signed webhooks, with duplicate suppression and delivery
   receipts. For the supplied GTM persona, this connects source research to a
   team's existing workflows. Buyer demand is a hypothesis until measured.

### First-stage observation-age review fixes

The canonical aggregate `ff83068` review returned BLOCK. Confirmed issues:
report collection time was added to the 300-second cache lifetime, and the UI
counted informational waiting as an issue. Cache expiry now begins before
collection and an already-expired collection is rejected. Only non-info
defects affect issue counts and filtering; the deferred queue count is labeled
as included in the total. Checkout redirect events now require a non-empty,
valid HTTPS destination before navigation. No payment configuration changed.

The scheduler case claim was disproved by its actual comparison: both operands
are uppercased. IngestJob timestamps use DateTime(timezone=True), and the report
route has a real PostgreSQL read-only transaction test. The clock is monotonic
in production; arbitrary backwards test clocks are not a supported contract.
CLI cleanup failure remains a controlled check failure because cleanup was not
completed. Per-statement limits bound the finite report query sequence, but an
overall short HTTP deadline is not claimed. Version-1 ambiguous comparison
records retain their reason-discriminated shapes, now explicitly documented.
The advocate step was cancelled (rc=1), so this review is not a verification
pass. A fresh canonical review is required after the confirmed fixes.
