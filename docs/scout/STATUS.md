# Bill Commons Scout status

Updated 2026-09-01.

## Decision

- Challenge artifact: **SHIP** once the owner authorizes the post.
- Scout application: **PRODUCTION-READY; DARK DEPLOYED**.
- Named private production canary: **PASS**.
- Rollback rehearsal: **VERIFIED** against the final backend artifact.
- Public production enablement: **OFF; requires explicit owner authorization**.

## Completed milestones

- M0–M1: repository architecture and product/security plan documented.
- M2–M6: durable owned jobs, queue/events, structured-first router, bounded direct
  retrieval, isolated Solari provider, immutable provenance, partial outcomes.
- M7: native evidence-first `/scout` UI with restrained civic/legal visual language.
- M8: query/source dedupe, content hashes, cache, freshness, and change relations.
- M9–M10: SSRF/redirect/MIME/size/injection/IDOR/cost/cleanup tests and deterministic E2E.
- M11–M12: real Solari Online Sunshine navigation and live Florida Senate HB 625
  bill-page → analysis discovery.
- M13–M14: existing Vercel Analytics events and desktop/mobile production renders.
- M15–M17: guarded broad regression, independent adversarial/visual reviews,
  sanitized 20.16-second demo, and production release runbook.

## Current implementation facts

- New jobs require authentication and the server feature flag. They also require
  either a named `BILLCOMMONS_SCOUT_CANARY_EMAILS` cohort or the separate explicit
  `BILLCOMMONS_SCOUT_ALLOW_PUBLIC=true` acknowledgement.
- Equivalent active/fresh queries coalesce per owner/jurisdiction/cache key.
- Florida checks Bill Commons first, then admitted Senate sources. A bill page can
  discover same-session/same-bill official analyses and amendments; ordinary HTTP
  remains preferred.
- Browser escalation is allowlisted and quota-bound. Every Scout worker/durable-canary provider session has a
  durable row, recording state, usage, bounded cleanup, and reaper recovery.
- Scout evidence uses PostgreSQL `scout_raw_blobs`, not a cross-service filesystem
  mount. Blobs are capped at 2 MiB in application and schema, and expired unverified
  stages/orphans are collected without racing live finalization. Migration `0025` is
  additive and locally proven.
- PDF extraction never runs in the Scout worker process; a spawned child is bounded
  by wall time, CPU/address space, pages, and returned text.
- Findings render as plain text and retain official URL, retrieval mechanism, MIME/
  status, content hash, excerpt, retrieval time, confidence, and entity linkage.

## Latest verification

- Backend/operations suites total **862 passed, 8 skipped, 11 dependency/fixture
  deprecation warnings**: API 571/8, shared 165, Scout worker 96, monitoring 4,
  operator scripts 26.
- Web: **10 passed**; targeted ESLint, TypeScript, and Next production build passed.
- PostgreSQL store: concurrent two-instance put, database size constraint, and an
  API-created job read by a fresh store instance passed.
- Live Florida discovery: passed; HB 625 bill evidence and at least one official
  analysis were retained without browser fallback.
- Live public Solari: passed in 10.137s; 1 page, 2 actions, 38 routed requests;
  recording/replay available; cleanup confirmed.
- Live Bill Commons provider path after lifecycle repair: passed in 7.86s with one
  real Solari session and a released terminal ledger state.
- Production durable lifecycle canary: passed in 9.291s; 1 page, 1 action,
  19 routed requests, source/raw/hash retained, no finding, replay available,
  released terminal ledger, and zero cleanup exceptions.
- Production direct canary: completed with 3 findings/3 official sources; duplicate
  request reused the cached job without new retrieval or browser spend.
- Migration: production `0021` → additive `0025`; controlled runner now fail-closed.
- Backup/restore: pre-migration and post-canary full dumps restored to disposable
  PostgreSQL 18; the reconciled recovery set matches all corpus/Scout counts, usage,
  and five blob hashes.
- Operations: final `8dc0ce36` API/Scout deployments are `SUCCESS`; service monitor
  7/7 and read monitor 5/5 healthy; exact-image feature-off reconciliation/reaper
  counters all zero.
- Demo: H.264 1440×900, 20.16s, 504 frames, fully decoded and visually inspected.

## Remaining human actions

1. Explicitly authorize public API/web/navigation enablement. Platform admission now
   caps active jobs, daily jobs, daily browser runtime, and retained evidence globally,
   but the rollout flags remain deliberately false.
2. Separately authorize the challenge social publication when ready.

Stripe live-account webhook access is tracked separately and does not gate Scout.
See [PRODUCTION_CANARY.md](PRODUCTION_CANARY.md) for exact evidence and
[CLOSEOUT.md](CLOSEOUT.md) for every final disposition.
