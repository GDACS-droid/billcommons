# Bill Commons Scout architecture

Status: locked for the P0 vertical slice on 2026-09-01. This document may be amended only with evidence from code, tests, or current provider documentation.

## Goal

Scout investigates official government sources when Bill Commons' structured state-legislative corpus is insufficient, then returns reusable findings whose material claims link to retained primary evidence.

## Repository fit

Scout remains inside the existing monorepo and deploy topology:

- Next.js `/scout` is the native product surface.
- FastAPI owns authenticated job creation, owner-scoped reads, and cancellation.
- PostgreSQL/Alembic remains the only durable schema authority and queue.
- a dedicated Scout worker performs slow external I/O outside request transactions;
- `packages/shared` holds pure URL, normalization, hashing, diff, policy, and provider contracts importable by API and worker containers;
- Scout uses its own PostgreSQL content-addressed RawStore (`scout_raw_blobs`) for fetched evidence. It is independent of ingestion's filesystem RawStore, so the dedicated worker does not require a cross-service volume. Application and schema cap blobs at 2 MiB; expired unverified stages/orphans are collected behind a live-finalization barrier. Scout cannot be enabled unless the additive migration and durable store are configured, and no finding may commit before the exact extracted bytes do.

`ingest_jobs`, `BillEvent`, and entity-oriented `SourceRecord` are not public Scout job/result stores. Scout uses additive tables because it needs user ownership, progress events, partial success, browser usage, and evidence relationships.

## Request path

```text
authenticated request
  -> normalize query + jurisdiction + cache key
  -> atomically coalesce active/fresh equivalent job
  -> durable Scout job + queued event
  -> dedicated worker claim (SKIP LOCKED; short transactions)
  -> existing Bill Commons search
  -> admitted official-source direct retrieval
  -> Solari only for a source classified browser-required
  -> normalize/hash/compare/extract
  -> persist source, document, finding, usage, and progress incrementally
  -> complete | partial | failed | canceled
  -> owner-scoped polling UI renders evidence and replay when available
```

## Additive domain model

- `scout_research_jobs`: owner (`api_customers.id`), original/normalized query, jurisdiction, cache key, status, strategy, timestamps, claim owner/lease/heartbeat, retry/error class, limits/usage, partial-success and cache fields. A partial unique index over owner/cache key for non-terminal rows plus insert-on-conflict makes coalescing atomic.
- `scout_job_events`: append-only real progress events; no timer-generated progress.
- `scout_sources`: canonical URL, official-domain decision, retrieval mechanism, HTTP/MIME outcome, content/document hash, raw reference, retrieved/update time, prior-source relation.
- `scout_findings`: source/job link, title, what happened, why it matters, relevant date/excerpt, confidence, extraction version, structured Bill Commons entity link.
- `scout_browser_sessions`: provider/session ID, recording/replay fields, pages/actions/runtime, timestamps, terminal status, normalized error. Never secrets.
- `scout_raw_blobs`: SHA-256-keyed immutable bytes plus bounded first-observation metadata. The primary key makes concurrent `put` idempotent; sources retain their own URL/tenant provenance separately.

The Scout migrations are additive and production is now at revision `0025`. Runtime
rollback retains these objects and their evidence; it disables new work, drains and
reaps the worker, and restores the prior compatible application artifact. Dropping
Scout objects is destructive and is not part of the rollback procedure.

## Research router

1. Query the local corpus through shared search logic/SQL, not a loopback HTTP call. For P0, Florida results yield candidate bill identifiers and their already-retained official URLs; Scout supplements the corpus result by checking those official pages/documents rather than stopping at the database hit.
2. Canonicalize those candidates through one Florida adapter and fetch only registry/adapter-derived official URLs with per-hop URL admission, robots/politeness, byte/time/redirect caps, an identifying user agent, and deterministic HTML extraction plus isolated, resource-bounded PDF extraction. A query with no structured match ends as `partial`/`unsupported_query`; a structured record lacking an official source ends as `partial`/`official_source_missing`. Neither becomes an evidence-free finding or generic crawl.
3. Classify direct responses as usable, blocked, browser-required, or failed. Usable requires an admitted MIME plus required extracted fields. 404/410, 429, login/challenge/captcha markers, off-registry redirects, and garbage HTML never escalate to a browser. Browser-required is an explicit allowlisted host/path behavior, not a fallback for every extraction failure.
4. Use `ResearchBrowserProvider` only for browser-required sources. `MockResearchBrowserProvider` drives deterministic tests; `SolariResearchBrowserProvider` owns all provider-specific calls.
5. Normalize, hash, compare, extract, and persist each useful source independently. One failure cannot erase earlier findings.

The first real adapter is Florida-focused. P0 does not pretend to be a generic 50-state autonomous crawler.

## Provider lifecycle

Every browser run has centrally configured ceilings for wall time, pages, actions, routed network requests, retries, response bytes, per-user concurrent jobs, and global browser concurrency. Global concurrency is enforced with a PostgreSQL advisory lock and durable browser-session rows, not an in-process semaphore. The provider calls Solari `sessions.create(recording=True)`, records the signed provider ID durably before Patchright connects, and attempts bounded release independently of the drive timeout, including cancellation and extraction failures. P0 opens one allowlisted page and intercepts that page's navigation and subresource requests; it blocks image/media/font requests, WebSockets, and popups, and does not click arbitrary links or initiate downloads. Those interactions require additional policy tests before broader planning is enabled. Direct fetching connects to the DNS address admitted for that hop rather than re-resolving at socket time.

Release has its own hard caller-wait timeout. A provider exception records `cleanup_failed` for the reaper. A call that merely outlives the wait remains `reaping`: a daemon heartbeat plus process-local registry keeps that durable claim non-retryable until the exact call settles, preventing duplicate release within a live worker process even if its heartbeat write fails. If the process dies, the heartbeat/registry disappear and the stale claim becomes recoverable after the cleanup timeout. Reapers atomically claim cleanup before external I/O and do not touch a fresh running job with an unexpired lease. Distributed crash recovery is at-least-once—the provider contract therefore requires idempotent release; Solari treats already-released/not-found as success. ID-less stale reservations become `abandoned` rather than falsely `released` and no longer consume the browser cap; provider-ID failures/outcome-unknown calls continue to consume it. The reaper is also the recovery path for SIGKILL/OOM/deploy interruption. The provider performs a bounded best-effort replay probe after release and the reaper can fill a delayed replay later; unavailable replay does not convert a useful research result to failure. Replay URLs/session IDs are served only through an owner-authorized API action and are never treated as primary evidence.

Current Solari docs (checked 2026-09-01) support both `launch()` and the lower-level `sessions.create()` plus Patchright connection flow, require explicit release when using the latter, and document asynchronous replay availability. The final post-repair opt-in gate passed against `www.leg.state.fl.us` with recording, replay, and cleanup; `www.flsenate.gov` separately produced a repeatable browser `connection_reset`, confirming why partial-source behavior is required. A separate opt-in PostgreSQL/API run against real MyFloridaHouse `BillId=84174` classified a safe direct-fetch `302` as browser-required, admitted exactly one Solari session, and persisted terminal status `released`; it validated lifecycle integration while intentionally making no bill-level finding claim. A direct live PostgreSQL/API check against the official Florida Senate HB 625 page retained one finding only after the fetched evidence supported both `HB 625` and `Chapter No. 2026-141`.

## Trust boundaries

- user queries and all fetched text are untrusted data;
- the user cannot supply an arbitrary fetch URL in P0;
- source discovery admits only HTTPS public destinations and revalidates every redirect and DNS result;
- private, loopback, link-local, metadata, NAT64-private, userinfo, non-default-port, `file:`, and `data:` destinations are rejected;
- displayed excerpts are plain React text, never injected HTML;
- extractors have no shell/tool/credential capability;
- prompts, if an optional model extractor is later added, delimit source text as data and require citation spans; model output alone cannot upgrade a source to official;
- every job read/cancel/replay request verifies the signed account session and `customer_id`; a UUID is not authorization.
- raw artifacts and replays are reached only through owner-authorized API responses/proxies; raw keys and third-party bearer URLs are not exposed as durable public links;
- source versions are immutable: raw-byte hash, RawStore reference, extractor ID/version, and excerpt hash/offsets bind each finding to the exact bytes used; stored HTML is never rendered as HTML.

## Cost controls

All limits live in one Scout settings object. Equivalent in-flight queries coalesce atomically by owner, jurisdiction, normalized query, and freshness bucket through a database constraint/retry path. Canonical URL plus immutable raw-byte hash deduplicates sources/documents, and prior-source links retain changes. PostgreSQL source finalization is serialized per tenant and canonical URL so concurrent observations form one immediate-predecessor chain. Active jobs, daily new jobs, every external attempt, and concurrent/cleanup-failed browser sessions are durably bounded. Job creation reserves worst-case browser runtime under the customer-row lock; terminal actual runtime replaces that reservation, and the worker honors the stored request-time wall ceiling. Provider-reported pages, actions, and routed requests are checked again before evidence is admitted. Reservation is intentionally conservative: capacity is returned only when a job terminalizes without a live browser slot.

## Evidence storage tradeoff

Scout P0 stores bounded source payloads in PostgreSQL so its worker, queue, and retained evidence share one durable service rather than depending on a mounted filesystem across deployments. This is intentionally scoped to Scout's strict document-size ceiling and content-address dedupe; it increases managed-Postgres storage, backup, and I/O cost compared with object storage. Before payload volume makes that tradeoff unattractive, introduce an S3-compatible `RawStore` implementation behind the existing protocol, migrate immutable blobs by SHA-256 with read-after-write verification, retain PostgreSQL keys/provenance, and keep the database implementation as a backward-compatible reader during cutover. Ingest storage is not changed by this decision.

## Runtime and rollout

`BILLCOMMONS_SCOUT_ENABLED` gates only new API creation, web navigation, and worker claims. Existing owner-authenticated job reads, evidence/replay reads, and cancellation remain available during dark rollback. A separate worker command consumes only Scout jobs; it never holds a database transaction across network/browser I/O. On SIGTERM/SIGINT it stops taking new claims and lets the currently claimed run reach its normal `finally` release path; an uncooperative provider call is bounded by a daemon helper and durable reaping is the remaining safety net. Claims have a bounded lease and heartbeat; expired running jobs are reclaimable, and retries idempotently skip already-persisted source hashes. Cancellation is a versioned durable state checked between every source/action and triggers bounded browser release.

The route can be deployed dark, migration first, then API/worker, then web flag. `billcommons-scout rollback` is intentionally available while disabled: it reaps only eligible sessions and marks queued plus lease-expired running jobs `failed` with `rolled_back`, while preserving fresh in-flight, completed, partial, and canceled jobs for owner reads. It does not drain a live process by itself; deployment orchestration must first send TERM and wait for the documented worker drain boundary.

## P0 acceptance evidence

- deterministic job creation -> structured lookup -> direct fixture retrieval -> finding -> provenance -> UI;
- mock browser fallback with release proven on success, exception, cancellation, and cleanup-error paths;
- worker crash/expired-lease reclaim and independent session reaping;
- owner isolation (including evidence/replay), atomic query coalescing, partial success, stale cache, DNS-pinned SSRF redirects, intercepted browser navigation, injection text, huge/bad MIME, retries, and concurrent submissions covered by tests;
- opt-in one-session Solari smoke with real session/replay/cleanup telemetry;
- conservative Florida official-source smoke;
- locally rendered desktop/mobile screenshots reviewed after functional checks.

## Deferred

Saved watches, scheduled monitoring, notifications, generic model planning/extraction, broad adapters, federal corpus ingestion, and automated knowledge promotion are P1/P2. The fetch/hash/compare primitive ships now so those additions do not require redesign.
