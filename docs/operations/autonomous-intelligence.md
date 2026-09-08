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

Implement a durable shared upstream quota and source observation/reconciliation
ledger. Retain exact bounded response bytes independently of Scout tenancy.
Declare official adapter capability only where a source-specific parser and
fixtures exist. Use CA then FL to exercise the common contract, then extend
source-by-source until all-state acceptance is actually proven.

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
