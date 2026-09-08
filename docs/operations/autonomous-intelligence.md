# Autonomous legislative intelligence — active implementation

## Authoritative objective

Turn Bill Commons from a large legislative database into an autonomous 50-state
legislative intelligence system that detects stale data, repairs ingestion
failures, discovers missing official material, and explains/proves every update.
Alberto explicitly authorized deployment in this session on 2026-09-08. The
objective remains active; the first control-plane release is not completion.

## Acceptance evidence required

| Requirement | Required proof | Current state |
| --- | --- | --- |
| All 50 states | Versioned source/adapter capability inventory and live, bounded per-state checks | Open States imports exist; official adapters incomplete |
| Detect stale data | Durable observations, per-source freshness policy, discrepancy history and failure visibility | Local-sync report implemented; official observations pending |
| Repair failures | Bounded retries, shared upstream quotas, crash recovery, fixture-replayed parser repair evidence and controlled promotion | Existing retries; durable quota and repair laboratory pending |
| Discover official material | Retained primary responses and tested extraction of missing actions/documents with explicit scope | FL Scout and CA repair are separate, limited paths |
| Explain every update | Immutable raw evidence, parser/comparator versions, occurrence identity and before/after records | Entity provenance exists; cross-source observation ledger pending |
| Operate autonomously | Recurring scheduling, durable checkpoints, bounded resource use and observable terminal outcomes | Nightly incremental loop; official reconciliation not recurring |
| Deploy | Pinned artifacts, applicable migration/backup gates, staged health and recovery proof | Explicit authority granted; first release review running |

## Current release arc

- First release source: `3f37ff8615d2bf865dbb86c7b9aa408b7b380130`.
- Exact aggregate review: `1171a6e4765f00bf2033a2476d71b1df8bf6adbf`,
  whose full binary diff from `26e99e2` equals the source diff.
- Initial rollout scope: API and dedicated sync worker; web follows its
  applicable checks. This stage changes no billing routes or database schema.
- Production project `92e10559-88b7-49ec-ae77-b0dc72b12752`, environment
  `78036c32-1cac-4fae-9a22-ef81c6f99772`, independently re-read this session.
- Existing API artifact `5828ea8b-6a85-4ca6-b453-f76e604ad374`; sync artifact
  `568fcd62-89b9-4e1c-bdb1-77c4ebae4759`. Both provider manifests specify
  one replica and the corresponding checked-in Dockerfile.
- Release and rollback owner: Codex executing Alberto's authorized session.
  Roll out during this active session only; recheck state immediately before
  each change. Hold a stage on failing review, repeated 5xx/503, unhealthy DB,
  unsafe active-work cutover, or absent artifact evidence. Roll back only the
  affected application artifact; preserve database rows and raw evidence.

## Test isolation incident and correction

During read-only exploration, an agent invoked three ingestion registry tests
without selecting a disposable database. The legacy ingestion conftest used
its local fallback connection, which an independent redacted check confirmed
was the known production target. Test bodies used rollback transactions, but
the session teardown also executed its fixture cleanup routine. That routine
can delete jurisdictions matching `ZZ_%` or `ZQ_%` and their related records.
It must never be treated as read-only or acceptable autonomous verification.

The post-incident read-only census found 52 jurisdiction rows, 51 with two-letter
codes, and no rows matching the cleanup patterns. The earlier census also had
52 jurisdictions. This does not prove whether any transient synthetic rows were
removed; there is no pre-test cleanup-pattern census. No restoration or further
production cleanup was attempted. The affected registry-test result is not
accepted as safe verification. The fail-closed test-target gate is now committed. Eleven subprocess cases
prove rejection before database-helper imports, including service/host overrides,
and the safe disposable harness passes. Normal checks use `pg_virtualenv`.

## Next implementation

The shared upstream quota and corpus-owned observation/reconciliation ledger
are implemented locally, with additive migrations `0026` and `0027`. The
official worker has a five-minute transaction deadline, target row locking,
durable retry schedules and graceful stop between claims. It registers seven
California daily-action delta targets and 51 reviewed official landing-page
targets. Registration and enablement are explicit operator switches.

California comparisons retain the exact archive, local action input and diff.
Each observation compares at most 500 bills and records an explicit partial
remainder when that limit is reached. That remainder still needs resumable
processing before claiming exhaustive reconciliation. Generic discovery retains
robots and page bytes plus a replayable, bounded same-origin link comparison;
it does not parse legislative facts or establish statewide freshness.

Next: complete staged deployment and health proof; make comparison remainders
resumable; retain before/after and source-response evidence on the existing
all-state incremental/document mutation paths; extend source-specific semantic
adapters and bounded repair workflows. The full user objective remains active.

## Implementation and release checkpoint — 2026-09-08 02:03 UTC

- Main integration branch: `mission-reliability-20260908`, implementation
  through `baaf830`. Original user worktree remains separate.
- The second canonical nine-family review of aggregate `1171a6e` completed
  with seven SHIP and two BLOCK verdicts. The BLOCKs were substantive:
  missing/empty canonical jurisdictions, undated successful syncs, wrong age
  for quota-delayed queues, unbounded historical ORM materialization, and no
  cooldown after failed public report scans. These were repaired and tested.
- Revised first-stage source: `5fc1b07e725c0f92c4e77939f65018bd0e90e8f3`.
  Aggregate `4590b5fcef61b67470d0ae0f0e929795378df55d` has the exact same full
  binary diff from `26e99e2`. Its canonical review is running at
  `/home/alberto/verify-runs/20260908T015852Z-4590b5f`.
- First-stage checkout independently passed 80 worker/shared and 8 API tests
  on disposable PostgreSQL 16. Main integration independently passed 206
  worker/shared and 17 API tests, including real PostgreSQL evidence pagination,
  exact-byte HTTP retrieval, failure cleanup, lock contention and deadline tests.
  The existing Starlette/httpx deprecation warning remains.
- Live read-only recheck at 02:03:38 UTC: revision `0025`, 51 public
  jurisdictions, 17 warnings, no critical/error defects, and no pending
  API-sync jobs. This report is local ingestion evidence, not proof of official
  source freshness. Recheck immediately before production cutover.
- No production deployment or migration has occurred in this implementation arc.

## Retained all-state source probe

One bounded read-only probe completed at 01:57:18 UTC: all 51 jurisdictions,
85 HTTP requests, 30 observable pages and 547 candidate links. The remaining
outcomes were 10 rejected redirects, 5 TLS failures, 2 robots denials,
2 unavailable robots policies, 1 slow crawl-policy restriction and 1 JavaScript
requirement. No production database was accessed. Exact public page/policy
bytes and hashes are retained in restricted local evidence storage at
`/home/alberto/.local/share/billcommons/official-inventory-probe-20260908`.

The checked-in override registry corrects Kansas and Indiana destinations only
after following their primary government endpoints: the
[Kansas legislature](https://www.kslegislature.org/) redirects to its `.gov`
session site, and [Indiana's state legislature link](https://www.in.gov/legislative/)
redirects to `iga.in.gov`. Other rejected redirects remain visible failures;
the transport does not follow unreviewed destinations or weaken TLS.

## Backup proof in progress

The snapshot-consistent PostgreSQL 18 dump completed at 01:56:38 UTC:
3,211,108,251 bytes, mode `0600`, catalog valid, SHA-256
`1125ea521522c7034e13c1f8a267d79175992549339642f35190fc332632d0a6`.
Snapshot counts include 210,504 bills, 1,673,590 actions, 741,902 documents and
10 Scout raw blobs. Restore verification is still running and is not yet a pass.

The host had PostgreSQL 18 clients but no matching server. An official PGDG
18.6 server package and its liburing dependency were unpacked into private
task storage; no system package installation or existing cluster restart was
needed. The restore runs in a disposable loopback-only server and will check
snapshot counts, Alembic revision, every Scout raw hash and actual API reads.

## Live release preflight evidence

At 2026-09-08 01:29 UTC: production PostgreSQL 18.6, revision `0025`, approximately
14.4 GB; 46 connections, one active connection, one idle-in-transaction connection.
No queued/running API-sync jobs and no active Scout jobs were visible. Queue-table
locks also serve other workers, so their count alone is not a sync activity proof.
The sync service's latest completed cycle is timestamped 2026-09-07 05:46:37 UTC.
API health/ready, one bill read, the NC web page and an MCP tool call all passed
in under 0.4 seconds each. Revalidate immediately before the cutover.

The current provider backup query found snapshots dated 2026-08-29 and
2026-07-24, but **zero configured volume backup schedules**. A fresh,
snapshot-consistent PostgreSQL 18 portable backup is running in restricted
local storage; it is not accepted until dump exit, catalog/hash and disposable
restore proof all pass. The existing 2026-09-01 restore evidence remains the
previous known-good recovery record.

The first expanded quota/sync test run found four assertions comparing timezone
spellings of the same instant. The watermark behavior was correct; assertions
now compare parsed instants. Fixture clients also use a no-wait limiter because
their transport never sends HTTP. Shared pacing has separate real-PostgreSQL
concurrency and UTC-boundary coverage; production pacing is unchanged.
