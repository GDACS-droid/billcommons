# Bill Commons Scout status

Updated 2026-09-01.

## Decision

- Challenge artifact: **SHIP** once the owner authorizes the post.
- Scout application code: **READY FOR DARK DEPLOY / PRIVATE CANARY**.
- Public production enablement: **CONDITIONAL** on authorized Railway inventory,
  one production migration runner, and a current backup/restore drill. No deployment
  was attempted.

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
- Browser escalation is allowlisted and quota-bound. Every provider session has a
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

- Native isolated suites total **801 passed, 8 skipped, 11 dependency/fixture
  deprecation warnings**: API 563 passed / 8 skipped, shared 153 passed, Scout
  worker 85 passed.
- Web: **8 passed**; targeted ESLint, TypeScript, and Next production build passed.
- PostgreSQL store: concurrent two-instance put, database size constraint, and an
  API-created job read by a fresh store instance passed.
- Live Florida discovery: passed; HB 625 bill evidence and at least one official
  analysis were retained without browser fallback.
- Live public Solari: passed in 10.137s; 1 page, 2 actions, 38 routed requests;
  recording/replay available; cleanup confirmed.
- Live Bill Commons provider path after lifecycle repair: passed in 7.86s with one
  real Solari session and a released terminal ledger state.
- Demo: H.264 1440×900, 20.16s, 504 frames, fully decoded and visually inspected.

## Remaining human/external release evidence

1. Confirm the real Railway Scout service, command, artifact, replicas, termination
   grace, and dark environment variables.
2. Name one authorized migration runner and record production pre/post revision.
3. Create and restore a current production dump to a fresh disposable target; verify
   a Scout blob hashes to its key.
4. Before all-account expansion, record global queue/blob capacity and enable the
   separate public/nav flags deliberately.
5. Explicitly authorize deployment and later social publication.

Stripe live-account webhook access is tracked separately and does not gate Scout.
See [CLOSEOUT.md](CLOSEOUT.md) for every final disposition.
