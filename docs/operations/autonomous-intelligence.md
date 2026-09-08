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

## Acceptance and present limits — 2026-09-08 06:53 UTC

The deployed and hash-bound release status is recorded in
[data-reliability-control-plane.md](data-reliability-control-plane.md) and its
[sanitized deployment summary](evidence/reliability-20260908/deployment-summary.json).
API and sync run `c05ae42`; web runs the data-only `3758f51`; the recurring
official worker runs the separately accepted CA cap fix `815be4a` on `c05ae42`.
The additive migrations through `0030` are applied, and the initial UTC-day
source budget was conservatively seeded at its full 225-request allowance.

| Requirement | Current evidence | Remaining acceptance gap |
| --- | --- | --- |
| Detect stale data | Live 51-jurisdiction report, 17 overdue sync warnings, explicit source outcomes and future retry eligibility | Semantic freshness/completeness adapters beyond bounded CA archives; finer worker cadence |
| Repair failures | Shared quota/backoff, bounded observation transactions; confirmed CA parser cap repaired and five failed targets retried successfully | Autonomous fixture-based parser-repair proposal/promotion; discrepancy-driven corpus repair; TX gate below |
| Discover official material | 58 enabled targets across 51 jurisdictions, every target attempted, 48 latest successes and 10 explicit failures | State-specific facts beyond landing pages; remaining source-access failures; deeper FL/CA Scout |
| Prove updates | Public exact-byte/hash APIs and versioned comparison/update ledgers; 1,418 completed CA bill/archive comparisons with sampled public replay | Generic-worker forward evidence rollout; no retroactive proof for untracked historical updates; declared external snapshot inputs |
| Operate autonomously | Dedicated recurring worker, durable schedule/backoff, 300-second hard observation deadline, real claim pause and graceful shutdown; two post-activation cycles | Broader semantic state coverage, automatic discrepancy remediation, durable saved-topic product workflow |
| Deploy | API, web, sync and official worker deployed with immutable source/archive/image bindings, restored backup and post-deploy runtime/browser proof | Generic-worker cutover and held TX repair; the full objective remains active |

## Current release gates

Codex owns this authorized deployment. The old generic crawl worker has no
existing drain control or graceful shutdown handler and holds its job claim
transaction across outbound work. It remains running; a one-time replacement
approval has been requested rather than inventing a drain or forcing a cutover.

The TX nested-path candidate `8570cdd` remains held. Its required full-diff
DeepSeek review ended DEAD/HALT after a concrete bounded recovery and the
canonical automatic retry. A passing small ASCII-fix review does not replace
that full-diff result. Both cloud document-repair flags remain off. The separate
five-family CA-only review of `815be4a` passed and does not waive the TX gate.

The initial observer pass and cap repair preserve source failure evidence.
Robots restrictions, unavailable robots, a denied redirect, JavaScript-only
content and three timeouts remain explicit; a CA Sunday capture has no retained
archive. These do not establish source agreement or statewide completeness.

## Historical preflight, implementation and review record

The dated sections below preserve the earlier evidence, decisions and test
isolation incident. Their old pending/deployment statements are superseded by
the current release status above; they are not current release gates.

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

## Failure repair lab — local follow-up, not yet deployed

`python -m billcommons_ingest.official_repair_plan OBSERVATION_UUID` requires an
explicit database target and opens a repeatable-read, read-only transaction.
The planner verifies retained blob hashes and size limits before replaying a
failed CA archive under the current parser. Output records the parser source
hash, current replay outcome, and a bounded recommendation. It never fetches
upstream material, advances a retry, changes a target or writes corpus data.
A successful local replay is a candidate for review and a bounded canary.
Superseded failures become regression fixtures; they are not retry candidates.
The old Sunday failure has no retained response or HTTP status, so its cause
remains unknown until a scheduled capture provides stronger evidence.

New observations will carry bounded structured diagnoses in `scope.failure`:
a version, processing stage, code, recommendation, and optional whitelisted
numeric measurements. Exception text, headers and raw response bodies are not
copied into diagnostic metadata. This preserves historical observations and
uses the existing schema. Production still runs the earlier observer artifact.

CA history IDs and sequence numbers are not durable cross-archive identities.
The deployed comparator's missing/local-only counts must not authorize action
mutation. The follow-up must preserve old recorded comparisons and replay,
version its new content semantics, and explicitly separate content agreement
from proof that two records represent the same occurrence.

## Next actions for the full objective

1. Resolve the generic-worker handoff and the separate TX review gate without
   weakening either requirement or repeating blind verifier calls.
2. Build the fixture-based parser failure diagnosis, bounded patch proposal,
   sample replay and controlled promotion workflow; retain failed inputs and
   actionable safe error classifications.
3. Turn CA comparisons into bounded discrepancy-repair proposals with before/
   after proof, then deepen FL and CA Scout beyond landing-page links.
4. Expand semantic source adapters and a factual cross-state benchmark; preserve
   source identity limitations, ambiguity, lineage and scope.
5. Productize saved issue monitoring and evidence-backed team/CRM workflows for
   the intended government-technology buyer. Current usage is not proof of demand.

The full evidence graph, richer search, historical consistency/backfill work and
50-state semantic freshness remain in scope and are not marked complete.

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

### Evidence pagination contract

The observations, reconciliations, corpus-updates and derived-updates endpoints
accept offsets through 10,000 and return at most 100 items. `has_more` means
`next_offset` is a fetchable continuation under the same filters and limit.
If additional records exist beyond the allowed offset, the response instead
sets `has_more: false`, `next_offset: null`, and `pagination_limited: true`.
Clients must disclose that limit rather than interpret the page as a complete
history. `pagination_limited: false` means the current page was not stopped by
that offset cap; it does not establish historical evidence completeness.
