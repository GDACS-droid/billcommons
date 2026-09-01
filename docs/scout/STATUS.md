# Scout status / durable checkpoint

Updated 2026-09-01. This records checks actually run; it is not production-deployment authority.

## Completed

- Inspected the public Apache-2.0 Bill Commons monorepo and followed its Next.js 15, FastAPI, PostgreSQL/Alembic, Postgres-queue, Railway/Vercel, `ApiCustomer`, RawStore, Resend, Stripe, and Vercel Analytics conventions.
- Implemented native authenticated `/scout`, owner-scoped API/job lifecycle, dedicated worker, structured-data-first routing, conservative Florida direct retrieval, an isolated Solari provider plus deterministic mock, immutable evidence/provenance, cache/dedupe/change primitives, analytics, partial results, and dark-launch controls.
- Added bounded requests/retries/bytes/time/pages/actions/routed requests/concurrency/daily usage, DNS-pinned URL admission, private-network and redirect rejection, plain-text rendering, claim fencing, cancellation, browser cleanup/reaping, and replay authorization.
- Repaired adversarial findings covering false terminal success after request exhaustion, late callback revival of abandoned browser slots, concurrent provenance forks, one-shot polling after transient errors, pre-provider usage inflation, provider-reported routed-request overruns, and false login-page classification.
- Verified current first-party Solari and Congress.gov documentation. Congress.gov v3 live authentication passed without exposing the key; the account response reported a 20,000 request/hour limit.
- Published the sanitized cookbook example at `GDACS-droid/solari-cookbook`, branch `billcommons-scout-challenge`, commit `1095a02`.
- The production API-only Stripe billing hotfix was separately preflighted and deployed successfully. It did not deploy Scout, run a migration, change prices, or configure a webhook.

## Live evidence

- Final post-repair Solari smoke: pass; official `www.leg.state.fl.us/robots.txt`; one page/action; 7,283 ms; recording enabled; replay available; cleanup confirmed; non-reversible session fingerprint `186a068f3858`.
- Earlier product-path Solari check: real MyFloridaHouse `302` escalation; exactly one durable browser session; terminal state `released`; deliberately no unsupported bill finding.
- Live direct product check: the official Florida Senate HB 625 page supported both `HB 625` and `Chapter No. 2026-141`; Scout retained one direct source and one exact evidence-backed finding without launching the mock browser.
- Chromium visual assertions and full-page captures passed at 1440x1100 and 390x844 using an explicitly labeled result fixture. This is rendering proof, not a claimed live discovery.
- A 28.96-second H.264 challenge demo was recorded at 1440x900 with backend-driven deterministic job states, the separately live-verified HB 625 evidence contract, and explicit on-screen boundaries between that direct result and the separately verified Solari run. It is a local product/demo artifact, not a production recording.

## Verification actually run

- Focused current Scout Python gate, including guarded real-PostgreSQL provenance and daily-budget concurrency tests: **226 passed, 2 live skipped**, 9 warnings.
- Web: **8 passed**; targeted ESLint passed; TypeScript/Next production build passed and emitted `/scout`. Optional build-time localhost API fetches logged nonfatal connection refusals.
- Guarded PostgreSQL Scout suite: **5 passed, 2 live skipped** by default. The opt-in live direct HB 625 test separately passed.
- Billing/API-key/quota/rate-limit verification on disposable PostgreSQL: **227 passed**, 1 warning. Billing unit gate: **78 passed, 1 skipped**; real PostgreSQL reconciliation concurrency: **1 passed**.
- Broad isolated shared/API regression is **not green**: **617 passed, 30 skipped, 45 failed**. Most failures require a populated legislative corpus or expose monolithic rate-limit state; a legacy `substituted` mortality-status mismatch also remains. Do not report this gate as passing.
- Congress.gov authentication helper passed live. The final bounded Solari check passed as recorded above.
- The explicitly named local disposable PostgreSQL databases and temporary `alberto` test role were dropped after the final guarded gates.

## Git / preservation

- Public feature branch: `billcommons-scout`; hardened code head `22a6376` (code repair `51e2588`). Remote head was independently confirmed after push. The replacement demo and sanitized verification record received an independent **SHIP** verdict and are ready to push.
- Preserve unrelated modified ingest health/full-text files and untracked `.claude`, operations, outreach, rendered, monitoring, and data-request files.
- Scout production schema migration, worker service creation, RawStore topology, monitoring/canary, backup/restore proof, feature enablement, and web deployment have not run.

## Remaining

- The independent post-repair review's cancellation/finalization, duplicate cleanup, terminal-reason, usage-audit, daily-spend admission, expired-lease, bounded-polling, and outcome-unknown cleanup findings are repaired and locally green. The final adversarial reproduce-or-ship verdict is **SHIP**.
- Publish the reviewed demo commit, then have a human send the prepared X or LinkedIn post tagging `@harrychow_` and `@getsolari`. The public source artifact and reviewed recording are ready; `https://billcommons.org/scout` is not deployed and must not be presented as a live product demo.
- General production Scout rollout remains a separately authorized operation and is **not ready** until the deployment runbook's service, storage, migration, backup, monitoring, canary, and rollback gates are satisfied.
- Stripe webhook state in the correct Bill Commons live Stripe account remains unknown; an authorized account operator must inspect/configure it and safely place the signing secret in Railway before paid provisioning can be called end-to-end verified.
