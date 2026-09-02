# Scout testing

Deterministic fixtures and a mock provider cover the normal suite. Live government
and Solari checks are explicit opt-ins and never CI defaults.

## Final evidence — 2026-09-01

| Gate | Result |
| --- | --- |
| Guarded API suite | **572 passed, 8 skipped** |
| Shared suite | **166 passed** |
| Scout worker suite (including Postgres proofs) | **96 passed, 3 skipped** |
| Monitoring suite | **4 passed** |
| Production operator scripts | **26 passed** |
| Backend/operations passed assertions | **864 passed** across the five rows above |
| Focused source/session repair | **28 passed** |
| Public cookbook deterministic contracts | **12 passed** |
| Web Scout contract | **16 passed** |
| Targeted web ESLint | pass |
| TypeScript `--noEmit` | pass |
| Next production build | pass; `/scout` emitted |
| PostgreSQL RawStore concurrent/restart proof | **1 passed** |
| Live Florida HB 625 bill → analysis workflow | **1 passed** |
| Live Bill Commons Solari product path | **1 passed** in 7.86s; released terminal state |
| Final demo decode / metadata | pass; 17.80s, H.264, 1440×900 |

The broad suite's eight skips are explicitly optional integrations/live checks. Its
warnings are Starlette/httpx, future cryptography certificate parsing, and Python
3.12 SQLite fixture date/datetime adapter deprecations; no assertion failed.

These counts were reacquired after the dark-route, durable Solari lifecycle,
strict-limit, platform-capacity, migration-runner, monitoring, and drain hardening commits. The web
lint, typecheck, and build gate was also rerun sequentially after the contract tests;
running TypeScript and Next build concurrently is intentionally avoided because both
mutate/read `.next` generated state.

## Covered behavior

- normalization, cache keys, coalescing, URL canonicalization/admission, public DNS
  pinning, redirect revalidation, private-network rejection, hashing and diffs;
- authentication, CSRF, owner-scoped create/read/cancel/evidence/replay, canary
  allowlist, quotas, simultaneous submissions, partial/error truthfulness;
- structured-first lookup, usable/direct/browser-required classification, Florida
  analysis/amendment discovery, URL/source dedupe, PDF MIME/signature plus isolated
  child-process wall/CPU/memory/page/text bounds;
- browser success/failure/cancel/timeout, session persistence, page/action/routed-
  request/runtime limits, cleanup uncertainty, delayed replay, reaping and drain;
- unchanged/changed related documents, stale refresh, request-time immutable limits,
  cache reuse, and content-addressed evidence retention;
- desktop/mobile rendering, safe evidence controls, terminal polling, analytics
  normalization, and no unsafe HTML rendering.

## Reproducible commands

```bash
# API suite. This refuses remote or non-_test databases.
BILLCOMMONS_TEST_DATABASE_URL='postgresql:///billcommons_scout_test_20260901_test?host=/var/run/postgresql' \
BILLCOMMONS_TEST_POSTGRES_URL='postgresql:///billcommons_scout_test_20260901_test?host=/var/run/postgresql' \
BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
PYTHONPATH=apps/api:workers/scout:packages/schema:packages/shared \
.venv/bin/python -m pytest -q apps/api/tests

# Shared package suite.
PYTHONPATH=packages/shared:packages/schema \
.venv/bin/python -m pytest -q packages/shared/tests

# Scout worker suite, including guarded PostgreSQL storage/concurrency proofs.
BILLCOMMONS_TEST_POSTGRES_URL='postgresql:///billcommons_scout_test_20260901_test?host=/var/run/postgresql' \
BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
PYTHONPATH=workers/scout:packages/schema:packages/shared \
.venv/bin/python -m pytest -q workers/scout/tests

cd apps/web
npm run test:scout
npx eslint components/scout/ScoutExperience.tsx components/SiteHeader.tsx lib/scoutAccess.ts middleware.ts
npx tsc --noEmit
npm run build
```

The two named local databases were disposable verification targets, not production.
Recreate a newly dated `_test` database and migrate it to head before a future run.
