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
| Prove updates | Atomic Open States API, document extraction and local derived-status before/after records with retained inputs, hashes, versions and download APIs; offline CA comparison replay | Historical updates have no retroactive proof; external session-activity and survivor-status snapshots remain explicit inputs |
| Operate autonomously | Dedicated recurring worker, explicit registration/enablement, durable target schedule and transaction deadline; resumable 500-bill CA pages | Production activation and measured recurring cycles |
| Deploy | Pinned first-stage source, restored production backup, safe local checks | Canonical review, staged rollout and post-deploy proof |

## Release ownership and gates

- Owner: Codex executing Alberto's authorized session. No new approval is needed
  for the scoped deployment. Stop at a failed required check or unsafe cutover.
- First-stage source: `bdd5b61194b018b29af59f8bb18157126de1c41c` in
  `billcommons-control-plane-fixed-20260908`. Initial rollout is API plus dedicated
  sync worker; no new schema or official worker in that stage. Web follows its
  applicable checks.
- First-stage aggregate review: `c3ec3c5`, exact binary diff from baseline
  `26e99e2` checked against the release source. Canonical nine-family run:
  `/home/alberto/verify-runs/20260908T031416Z-c3ec3c5` (completed: eight SHIP,
  one BLOCK, zero dead families; owner adjudication below).
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
- Follow-on schema revisions `0026`–`0030` are additive and currently undeployed.
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
- Local status and substituted-by relationship changes retain separate derived
  evidence with algorithm version, exact consulted local inputs, before/after
  values and an optional causal API-update record. Bill pages link to both
  source/document and derived change histories. No evidence history is implied
  for changes before tracking began.
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

## Integration check — 2026-09-08 03:10 UTC

The integrated derived-evidence implementation passed 427 worker/shared tests
and 30 API tests against disposable PostgreSQL with migrations through `0029`.
Two subsequent failure-isolation regressions are undergoing the final combined
run. The production web build and TypeScript checks passed. Desktop/mobile
fixtures exercised filters, unavailable reports, evidence links, German-locale
hydration, and mobile overflow; browser errors were zero. Bill evidence links
were checked against a retained public API fixture. An initial browser selector
used an incorrect heading name; it was corrected to the existing Attribution
heading and the check passed. These are local checks, not deployment proof.

### Repair and replay follow-up

The integrated TLS repair module reserves and commits one of at most two
attempts before HTTP, verifies the source dead-job fingerprint/document link,
and records admitted/outcome events. No-text outcomes are skipped rather than
reported repaired. A crash after admission remains reserved for explicit
operator review. `OFFICIAL_TLS_REPAIR_ENABLED=1` enables a separate, two-repair
batch per recurring official-worker cycle; observations commit independently.
The integrated test run through `0030` passed 440 worker/shared and 32 API tests.
The latest recurring-worker wiring is being checked separately.

The derived-input audit found missing substitution candidate/order inputs and
an effective-date mismatch. Version 2 now retains deterministic candidate ranks,
relation order, an explicit effective date, and versioned session-activity and
survivor-status snapshots. Pure offline replay reproduces status and semantic
substitution relations; the writer rejects any mismatch before inserting a
ledger record. Source fingerprints cover the CLI, status/replay modules and
shared normalization/enrollment dependencies, cached per immutable process.
Replay requires that exact source; retain the release archive with its records.
Version 1 remains readable without an offline-replay claim. This proves the
local derivation from declared snapshots, not the snapshots' recursive origin.
The integrated disposable PostgreSQL run passed 459 worker/shared and 32 API
tests through revision `0030` at approximately 03:34 UTC.

First-stage `ff83068` canonical review returned BLOCK and its advocate was
cancelled. Confirmed observation-age/UI waiting/checkout-url findings were
repaired; the current exact aggregate is `c3ec3c5`. It has 89 worker/shared and
12 API checks, TypeScript/lint/production build, and real browser checks for
info-only filtering and six intercepted malformed checkout responses. None
created a payment, navigated, or emitted redirect success. Current review is
pending; no production artifact has changed.

### Live scope check — 2026-09-08 03:19 UTC

Arizona now honors its reviewed, exact-source 120-second robots cadence. Its
live probe waited 120.000 seconds, retained robots/page bytes and 25 links;
root recomputed both hashes. Generic landing-page success is now 44/51.
The remaining seven are CA/TN robots disallow, HI/NY robots denial, IN
JavaScript rendering, DE robots redirect review, and MT timeout/backoff.
These results do not establish statewide semantic completeness.

The live corpus has zero dead fetch jobs retaining either exact missing-issuer
TLS marker, and zero eligible documents for the narrow TLS repair. It is a
capability for evidence-matched repairs, not a claim of recovered production
text. Before deployment, read-only inventory still found revision `0025`,
1,131 done API-sync jobs and no eligible/running API-sync jobs. The crawl
worker continues processing document work; its safe cutover boundary is under
separate release review.

### Concrete recovery inventory — 2026-09-08

Read-only aggregate and robots-respecting probes identified 3,917 Texas
unsupported-FTP witness-list documents. Three representative 89R/891/892
files succeeded at corresponding official HTTPS locations with retained raw
bytes and hashes. A strict resolver is being implemented; no requeue has run.
Indiana's 883 terminal records reflect 3,444 HTTP 403 and four HTTP 401 job
outcomes. Its unauthenticated robots endpoint also returns 403. No document
probe followed that denial. Recovery requires provisioned official API
credentials, a successful authorized probe, then a scoped reset.

API readiness is prepared as an environment-specific `environmentPatchCommit`
patch for `/api/v1/data-health`, a 300-second readiness timeout and 60-second
draining allowance. Existing `/health` and `/ready` retain compatibility.
The new data-health endpoint fails with HTTP 503 when the fresh-process
database read fails. Exact before/apply/rollback metadata is retained in the
release evidence directory. No config mutation has run. The separate restore
environment is excluded. Canonical first-stage review remains pending its
last family and advocate; eight completed families currently report SHIP.

## First API rollout — 2026-09-08 03:44 UTC

The canonical tool returned BLOCK: eight SHIP, one DeepSeek BLOCK, no dead
families; the advocate returned CONSENSUS-STANDS. The owner checked all
dissenting mechanisms against exact source. Cache writers share one lock;
PostgreSQL permits transaction attributes as the first statement after BEGIN;
all 503 paths already set no-store; zero deferred count cannot enter the JSX
branch. Trusted checkout responses come directly from Stripe, and magic-link
success is actually HTTP 202. The hypothetical compromised-API/error-200 cases
are not demonstrated regressions. The advocate's NULL cases are excluded by
actual production NOT NULL constraints. Two exact-source live, read-only
report loads passed in 3.266/1.616 seconds; the slowest SQL took 0.436 seconds.
The current bounds have measured headroom; future corpus growth remains an
operational risk. Owner adjudication accepted the source without pretending
the tool's BLOCK was a unanimous pass. The complete safe adjudication and live
proof are retained in the release evidence directory.

Only the API is deploying in this action. Production-specific readiness
configuration was applied and read back: `/api/v1/data-health`, timeout 300,
draining 60, overlap 0. Restore environment config ETag remained unchanged.
The exact frozen Git archive contained 476 verified tracked files, SHA-256
`cdc7343e4656a675b1b9ae28e165b9ed7c0172fb27893e7f4aee2a41256d2885`.
Railway accepted API deployment `c2537036-b510-48a8-8fcc-20e1aaa2135c`.
A deterministic monitor watches that ID through startup and 900 seconds of
repeated health/readiness/report/bill/web/MCP proof. Its handle is
`api-c2537036-b510-48a8-8fcc-20e1aaa2135c`; state is
`~/.local/share/billcommons/reliability-release-20260908/api-c2537036-monitor.json`.
Rollback restores the readiness fields from the recorded null baseline before
redeploying prior API artifact `5828ea8b-6a85-4ca6-b453-f76e604ad374`.
No schema migration or worker cutover occurred with this API action.

The current integrated web production build, TypeScript and lint passed.
Rendered desktop/mobile tests passed with zero browser errors or failed
responses, including German locale, deferred/info-only reporting, optional
source failure, core report failure, filters and both bill evidence links.
A stale browser assertion expected text without the new “Includes” prefix;
inspection confirmed the intended text and the corrected assertion passed.
