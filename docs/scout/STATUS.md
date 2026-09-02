# Bill Commons Scout status

Updated 2026-09-02.

## Decision

- Challenge artifact: **SHIP** once the owner authorizes the post.
- Scout application: **PRODUCTION-READY; LIMITED PUBLIC BETA ENABLED**.
- Named private production canary: **PASS**.
- Rollback: **VERIFIED IN USE** after a provenance-label defect was found during staged rollout.
- Public production enablement: **ON; authentication, quotas, cache namespace, and kill switches retained**.

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

- Final broad rerun: API **572 passed, 8 skipped**; shared **166 passed**; Scout
  worker **96 passed, 3 skipped**. Web Scout **13 passed**; ESLint, TypeScript,
  and Next production build passed.
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
- Operations: deployed source `8d5dbec3`; API `5828ea8b-6a85-4ca6-b453-f76e604ad374`,
  Scout worker `e71aa668-4dc1-4d57-9045-3dd51f3a568d`, and Vercel
  `dpl_2ajAm7J2TBkdnZjQsqsp1PMC3wfj` are healthy. Service monitor 7/7 and read
  monitor 5/5 pass; reaper counters and unreleased sessions are zero.
- Staged rollout found and repaired a material trust defect: House analyses
  discovered through a Senate host were mislabeled as Senate documents. Public
  admission was disabled and the prior dark web artifact restored before repair.
  Evidence-derived chamber/type/date labels, non-fabricated significance, cache
  namespace invalidation, and mobile Beta disclosure were then verified in a new
  private canary and actual deployed desktop/mobile renders.
- Second multi-family review: eight independent legs returned **SHIP**. The ninth
  configured image leg was unavailable because its endpoint does not accept images;
  it returned no product verdict.
- Demo: H.264 1440×900, 20.16s, 504 frames, fully decoded and visually inspected.

## Remaining human actions

1. Review the final challenge screenshots/video and proposed post.
2. Separately authorize the challenge social publication when ready.

Stripe live-account webhook access is tracked separately and does not gate Scout.
See [PRODUCTION_CANARY.md](PRODUCTION_CANARY.md) for exact evidence and
[CLOSEOUT.md](CLOSEOUT.md) for every final disposition.
