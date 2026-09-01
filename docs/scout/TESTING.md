# Scout testing

Most coverage uses deterministic fixtures and the mock browser. Live tests are explicitly opt-in and never CI-default.

## Coverage

- Shared unit: query/jurisdiction normalization, cache keys, URL admission/canonicalization, DNS/redirect rejection, hashing/diff, source classification, retries/limits, hostile-text boundaries.
- API: feature flag, authentication/CSRF/IDOR, create/read/cancel, quotas, atomic active-query coalescing, partial/error/result contracts, owner-scoped prior evidence and replay.
- Worker: structured-first routing, direct response failures, malformed/oversize/MIME cases, mock Solari failures, cancellation/lease/reaping, cleanup, request/page/action/routed-request ceilings, content reuse/change history, and partial persistence.
- PostgreSQL: migration/E2E, simultaneous job admission, source-history advisory-lock concurrency, and Stripe reconciliation concurrency.
- Browser: real Chromium result assertions and desktop/mobile captures.

## Evidence recorded 2026-09-01

- Current focused command: **226 passed, 2 live skipped**, 9 warnings. This includes worker/shared/API coverage, outcome-unknown cleanup ownership, and guarded real-PostgreSQL source-history and daily-budget concurrency regressions.
- Web contract: **8 passed**; targeted ESLint passed; Next production build/typecheck passed with `/scout` emitted. Optional localhost API fetches failed closed during static generation and did not fail the build.
- PostgreSQL Scout default: **5 passed, 2 skipped**; live tests are skipped unless explicitly enabled.
- Live direct official-source finding: passed against Florida Senate HB 625; one direct source and one finding whose excerpt supports both the identifier and action; mock browser unused.
- Final live Solari smoke: passed; one page/action, 7,283 ms, recording/replay available, cleanup confirmed.
- Public live Solari visual proof: passed; one page/action, 3,412 ms, recording disabled, cleanup confirmed; derivative image contains only official robots text, fixed safe labels, and a non-reversible run reference.
- Chromium visual assertions/captures: passed at 1440x1100 and 390x844 with an explicitly labeled fixture.
- Final strengthened demo: H.264, 1440x900, 25 fps, 849 frames, 33.96 seconds, 3,076,083 bytes. External Sonnet returned **SHIP**. An independent reviewer found contradictory cleanup tense in the first derivative; after correction it fully decoded the replacement, sampled frames 716–747 and 816–848, verified both visible cleanup labels, scanned metadata/printable strings, matched hashes, and returned **SHIP**.
- Broad isolated shared/API gate: **617 passed, 30 skipped, 45 failed**. It is not green; failures are documented in `STATUS.md` and must not be presented as Scout-pass evidence.

## Reproducible commands

```bash
BILLCOMMONS_TEST_DATABASE_URL='postgresql:///billcommons_scout_verify_20260901?host=/var/run/postgresql' \
BILLCOMMONS_TEST_POSTGRES_URL='postgresql:///billcommons_scout_verify_20260901?host=/var/run/postgresql' \
BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=apps/api:packages/schema:packages/shared:workers/scout \
.venv/bin/pytest -q workers/scout/tests packages/shared/tests \
  apps/api/tests/test_scout.py apps/api/tests/test_scout_postgres.py

cd apps/web
npm run test:scout
npx eslint components/scout/ScoutExperience.tsx lib/scout.ts
npm run build

# Explicitly opt-in and billable; never run in default CI.
BILLCOMMONS_SCOUT_ENABLED=true BILLCOMMONS_SCOUT_SOLARI_CHECK=1 \
PYTHONPATH=apps/api:packages/schema:packages/shared:workers/scout \
.venv/bin/python -m billcommons_scout solari-check
```

The PostgreSQL URL must identify an acknowledged local disposable database matching the test guard. Never point these tests at production.

The documented `billcommons_scout_verify_20260901` and monetization test databases plus their temporary local role were removed after the recorded final run; recreate a newly dated disposable target before rerunning PostgreSQL coverage.
