# Autonomous legislative intelligence — active implementation

## Objective and scope

Turn Bill Commons into an autonomous 50-state legislative intelligence system
that detects stale data, repairs ingestion failures, discovers official material,
and explains/proves updates. Alberto explicitly authorized deployment in this
session on 2026-09-08. The objective remains active. A control-plane deployment
alone does not satisfy the full objective.

Implementation is isolated in `mission-reliability-20260908`; pre-existing changes
in the original checkout are preserved. The canonical inventory covers 50 states
plus DC. Generic website observation is never treated as proof of statewide
semantic freshness or completeness.

## Acceptance and present limits

| Requirement | Implemented locally | Remaining acceptance gap |
| --- | --- | --- |
| Detect stale data | Canonical 51-jurisdiction report, bounded history queries, sync cadence/queue/provenance defects, fail-closed report cache; durable official observation status | Semantic freshness adapters beyond the limited CA archive |
| Repair failures | Existing retry queues, shared durable request quotas, official-worker backoff/deadlines/checkpoints, reviewed missing-intermediate TLS repair for five hosts | Fixture-replayed parser repair and controlled promotion; discrepancy-driven corpus repair |
| Discover official material | Robots-first official landing-page checks for all 51 jurisdictions, bounded same-origin links, exact retained response bytes | State-specific parsing of discovered facts; remaining explicit source-access failures |
| Prove updates | Atomic Open States API and document extraction before/after records with source bytes, hashes, versions and download API; offline CA comparison replay | Derived-status path audit; historical updates have no retroactive proof |
| Operate autonomously | Dedicated recurring worker, explicit registration/enablement, durable target schedule and transaction deadline; resumable 500-bill CA pages | Production activation and measured recurring cycles |
| Deploy | Pinned first-stage source, restored production backup, safe local checks | Canonical review, staged rollout and post-deploy proof |

## Release ownership and gates

- Owner: Codex executing Alberto's authorized session. No new approval is needed
  for the scoped deployment. Stop at a failed required check or unsafe cutover.
- First-stage source: `1a2277cb97f4fdf0219d8e5ce0c5d492340e5e40` in
  `billcommons-control-plane-fixed-20260908`. Initial rollout is API plus dedicated
  sync worker; no new schema or official worker in that stage. Web follows its
  applicable checks.
- First-stage aggregate review: `f8ea48c`, exact binary diff from baseline
  `26e99e2` checked against the release source. Canonical nine-family run:
  `/home/alberto/verify-runs/20260908T023417Z-f8ea48c` (pending).
- Earlier reviews found real case-normalization, undated-run ordering,
  quota-delayed queue age, canonical-inventory, bounded-query, cache and test
  routing defects. These are repaired and have focused regression coverage.
  Earlier BLOCK verdicts are not represented as passes.
- Production project `92e10559-88b7-49ec-ae77-b0dc72b12752`, environment
  `78036c32-1cac-4fae-9a22-ef81c6f99772`. Last observed successful API artifact:
  `5828ea8b-6a85-4ca6-b453-f76e604ad374`; sync:
  `568fcd62-89b9-4e1c-bdb1-77c4ebae4759`. Both have one replica.
- Re-read provider artifact/config and live work state immediately before each
  cutover. No invented drain: the sync service is changed only at its observed
  idle boundary. Hold on failed review, repeated 5xx/503, unhealthy database,
  active-work risk or missing artifact evidence. Roll back the affected app
  artifact; preserve database rows and evidence.
- Follow-on schema revisions `0026`–`0028` are additive and currently undeployed.
  Controlled migration requires the exact revision acknowledgement. Before the
  shared Open States quota is activated, seed current-day consumption
  conservatively so the new ledger cannot reset the upstream day's allowance.

## Recovery and live evidence

The snapshot-consistent PostgreSQL 18 dump finished at 01:56:38 UTC on
2026-09-08: 3,211,108,251 bytes, mode `0600`, SHA-256
`1125ea521522c7034e13c1f8a267d79175992549339642f35190fc332632d0a6`.
The isolated PG18 restore completed at 02:14:04 UTC. All 13 snapshot counts,
revision `0025`, all ten Scout raw hashes and actual API health/readiness/search
checks passed. Evidence is in
`~/.local/share/billcommons/reliability-release-20260908/restore-evidence.json`.
The temporary server and its owned data directory were removed; existing system
clusters were not restarted. Matching server binaries came from a privately
unpacked official PGDG package; no system package installation was required.

At 02:22:27 UTC production was still revision `0025`, approximately 14.4 GB,
with no queued/running/dead API-sync jobs. Public health, readiness, bill read,
NC website and MCP probes passed at approximately 02:23 UTC. These are preflight
observations, not post-deploy evidence. Provider volume backups dated August 29
and July 24 existed, but no scheduled volume backups were configured.

## Evidence implementation

- Corpus-owned raw blobs and official target/observation/reconciliation tables
  are separate from owner-scoped Scout data. Blobs are SHA-256 addressed and
  bounded to 8 MiB; an empty successful document response is valid evidence.
- CA action archives retain exact raw, canonical local input and comparison
  bytes with versions. A validated observation/hash/version/index cursor resumes
  the same archive across bounded 500-bill pages without refetching. Cursor and
  comparison records commit together. Offline replay checks hashes, versions,
  exact diff bytes and summary without HTTP or current action reads.
- The source overview includes missing targets, failures, overdue observations,
  next retry and exact evidence pointers for all 51 canonical jurisdictions.
- API-sync retains the exact Open States response before JSON decoding. Actual
  core/child changes retain bounded before/after snapshots in the same transaction.
  No-op updates add no evidence. Open States is labeled as an aggregator.
- Ordinary and browser-assisted successful extraction share the evidence tail.
  Database evidence failure rolls back semantic document changes. Optional
  filesystem archival remains a best-effort compatibility cache.
- Reviewed redirect destinations are explicit registry data. CT, MI, MS, OH and
  VT use four fingerprint-pinned public intermediates only for their exact hosts,
  with certifi roots, hostname/expiry validation, no partial-chain trust, pinned
  public-IP connections, original-host SNI, TLS 1.2 minimum and HTTP/1.1 ALPN.
  Publisher rotations deliberately fail closed until reviewed.

## Verification completed locally

- Main combined disposable PostgreSQL 16 harness: **385 worker/shared tests and
  28 API tests passed**, migrations through `0028`. Covers continuation beyond
  500 bills, rollback/retry, raw byte downloads, evidence atomicity, quota
  concurrency, report boundaries and failure paths.
- Transport/TLS and test-runner guards: **71 passed** in the integration checkout.
  The transport test server printed a handled connection teardown exception;
  pytest reported no failed tests. Existing Starlette/httpx deprecation remains.
- First-stage isolated checkout: **85 worker/shared + 10 API tests**, plus four
  runner-guard tests passed.
- Web TypeScript/lint and optimized production build passed. Rendered desktop/mobile evidence view, filters, clear action, raw link, optional/core
  API failure states and de-DE hydration passed against explicit source fixtures.
  Hosting-provided Vercel analytics was stubbed in this local run after tracing
  its expected local 404; no other browser errors or failed responses remained.
  This is not a live official-source verdict.

## Test isolation incident

An earlier read-only exploration agent invoked legacy registry tests without a
selected disposable database. The fallback was independently confirmed as the
production target. Test bodies rolled back, but fixture teardown could delete
synthetic `ZZ_%`/`ZQ_%` jurisdictions and related rows. The later read-only census
found 52 total jurisdictions, 51 public two-letter codes and no matching synthetic
rows; the earlier census also had 52. This cannot prove whether any transient
synthetic records were removed. No production cleanup/restoration was attempted.
That test result is rejected. A fail-closed conftest gate and runner guards now
reject ambient targets and service/host overrides before database helpers load;
normal verification uses disposable `pg_virtualenv` clusters.

## Next actions

Finish rendered UI checks and the derived-update audit. Complete the first-stage
canonical review and deploy only the accepted source with bounded health proof.
Then run canonical review on the complete follow-on diff, migrate exact revisions,
activate quotas/observation targets, deploy the recurring worker and establish
production evidence. Semantic state adapters and controlled discrepancy repair
remain explicit work until their required evidence exists.
