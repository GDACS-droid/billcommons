# Scout testing

Tests must record only checks actually run. Most coverage uses fixtures and the mock browser; live tests are explicit opt-in.

- Shared unit: query/jurisdiction normalization, cache keys, URL canonicalization/admission, DNS/redirect rejection, hashing/diff, official-source and browser-required classification, retries/limits, hostile-text boundaries.
- API: feature flag, authenticated create/read/cancel, CSRF origin, IDOR, job quotas, active-query coalescing, status/partial result contracts.
- Worker/integration: structured-first routing, direct success/garbage/404/429/500/timeout, duplicate/pagination/malformed/oversize/bad MIME, mock Solari unavailable/auth/runtime/crash/replay delay/cleanup failure, incremental partial persistence.
- Deterministic E2E: user request -> job -> router -> fixture -> finding/provenance -> rendered result.
- Adversarial: 10 concurrent jobs, simultaneous identical query, DB failure after research, cancellation, stale cache, source changes mid-run, prompt injection, private redirect, duplicate browser billing.
- Live: one opt-in Solari session and one conservative official Florida source. Never CI-default.
- Gates: focused pytest, API container-boundary tests, web lint/typecheck/build, application runtime, browser screenshots, then full regressions.

## Evidence recorded on 2026-09-01

- Focused unit/API/worker/guard gate: 23 passed, one TestClient deprecation warning.
- Pre-live combined Scout gate after adversarial repair: 101 passed. The final post-hardening focused regression expanded to 144 passed; warnings were limited to the known TestClient and SQLite adapter deprecations.
- Disposable PostgreSQL Scout E2E + contention + migration constraints: 4 passed, 1 skipped (the explicit live product-path test). Ten same-query submissions produced one job; ten distinct submissions produced two jobs and eight `429` responses.
- MCP served-session coverage/session-UUID gate: 29 passed.
- Web contract tests: 4 passed. ESLint passed. Production Next build/typecheck passed with `/scout` present.
- Production-mode Playwright capture/assertions passed at 1440x1100 and 390x844 using an explicitly labeled visual fixture; this is rendering evidence, not a live government finding.
- `git diff --check`, `compileall`, setup-helper shell syntax, and Congress helper compilation passed.
- Post-live lifecycle/diagnostic gate: 27 focused shared/provider/worker tests passed.
- Opt-in live Solari gate passed: official Florida Online Sunshine source, one page/action, deterministic marker, 3,650 ms, recording enabled, replay available, cleanup confirmed. Session output was fingerprinted.
- Opt-in live PostgreSQL/API product-path test passed again after the final redirect/egress hardening against real MyFloridaHouse `BillId=84174`: safe direct fetch retained the rejected `302` status, the fixed-host policy admitted exactly one Solari session, and the durable browser-session record ended `released`. The final pytest invocation completed in 5.57 seconds. The test intentionally asserted no government finding.
- Independent adversarial reruns reproduced the formerly false request accounting and lost cleanup-ledger cases, then confirmed both repaired: global-cap denial charges no unissued browser request, and failed cleanup retains a provider-ID-bearing `cleanup_failed` row that consumes capacity and remains reapable.
- The public cookbook example independently passed live in 4,935 ms with replay available and cleanup complete; hardened branch head `1095a02` is pushed.
- Final web rerun passed 4 tests, ESLint, typecheck, and production build. Build-time optional fetches logged nonfatal localhost `ECONNREFUSED` warnings because no API process was running; Next completed successfully and emitted `/scout`.

Key reproducible commands:

```bash
BILLCOMMONS_TEST_DATABASE_URL='postgresql:///billcommons_scout_verify_20260901?host=/var/run/postgresql' \
BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
PYTHONPATH=apps/api:packages/schema:packages/shared:workers/scout \
.venv/bin/pytest -q apps/api/tests/test_scout_postgres.py

PYTHONPATH=apps/mcp:packages/schema:packages/shared .venv/bin/pytest -q \
  apps/mcp/tests/test_bill_record_session_scope.py \
  apps/mcp/tests/test_coverage_warning_severity.py

cd apps/web && npm run test:scout && npm run lint && \
  NEXT_PUBLIC_SCOUT_ENABLED=true npm run build

# Explicitly opt-in and billable; never run in default CI.
BILLCOMMONS_SCOUT_ENABLED=1 BILLCOMMONS_SCOUT_SOLARI_CHECK=1 \
  python -m billcommons_scout solari-check
```

The repository-wide shared/API run is not green: 641 passed, 3 skipped, 3 failed, and 4 errored. It also revealed that legacy tests can fall back to the configured live database. Do not call the regression green and do not repeat it without explicit isolated-database wiring.

The disposable database `billcommons_scout_verify_20260901` and the local-only `alberto` PostgreSQL role created for this gate were dropped after the final run.
