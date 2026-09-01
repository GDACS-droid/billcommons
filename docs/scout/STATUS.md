# Scout status / durable checkpoint

## DONE

- Repository identified: `/home/alberto/codingProjects/billcommons`, public Apache-2.0 monorepo on `main`.
- Read `AGENTS.md`, README, locked architecture, manifests, API/web/schema/job/security/payment paths.
- Confirmed Next.js 15 + FastAPI + PostgreSQL/Alembic + Postgres queues + Railway/Vercel; no Redis/Celery or existing Solari integration.
- Confirmed existing magic-link `ApiCustomer` identity, Stripe billing, Resend, Vercel Analytics, RawStore/evidence/hash and SSRF foundations.
- Verified current first-party Solari and Congress.gov documentation on 2026-09-01. Congress.gov API v3 requires an API key; its public documentation describes a default 5,000 request/hour limit, while this account's live response reported 20,000.
- Completed Gate 1 architecture review with independent Claude and Grok reviewers. Both returned REVISE; accepted findings added crash leases, bounded cleanup, atomic coalescing, DB-backed browser concurrency, provider reaping, mandatory immutable evidence, DNS pinning/browser navigation policy, owner-proxied replay/evidence, explicit Florida candidate routing, and non-blocking replay.
- Locked the revised P0 architecture in `docs/scout/ARCHITECTURE.md`.
- Registered and locally stored a Congress.gov API key without committing it, then authenticated a live API v3 request. The live account reported Congress 119 and an hourly limit of 20,000.
- Completed the separately requested customer-account review and outreach without placing customer identity, usage, or delivery identifiers in this public engineering document.
- Verified production Stripe can create both a Builder subscription Checkout Session and a full-snapshot Checkout Session. No purchase or charge was completed. Replaced the legacy `/docs/bulk` external Payment Links with the native Bill Commons Checkout components so webhook metadata/provisioning follows the owned path.
- Implemented the additive Scout schema/API/worker/provider and native `/scout` web slice behind dark-launch flags. Added safe local Solari setup and Congress authentication-check helpers.
- Completed a disposable-PostgreSQL migration and E2E gate: API-created job -> queue claim -> structured lookup -> fixture retrieval -> retained RawStore evidence -> finding -> enriched owner response. Ten identical concurrent requests coalesced to one job; ten distinct requests respected the two-active-job quota.
- Added claim-token fencing, cancellation checks, incremental partial results, immutable source hashes/prior-version links, bounded PDF/text extraction, browser concurrency locking, provider cleanup/reaping, and truthful replay availability.
- Fixed two verified Bill Commons data-access defects: bill-record coverage warnings are now scoped to the served session, and `get_bill_record` accepts a same-jurisdiction session UUID while returning `invalid_session` for invalid/cross-jurisdiction values. Focused MCP regressions pass.
- Ran one conservative direct request to the official Florida Senate source for SB 1344: HTTP 200, `text/html`, 51,922 bytes, SHA-256 prefix `0b35ec1d4654da6c`. No legislative claim was inferred from that smoke.
- Completed external Gemini visual review (REVISE), accepted the user-facing jargon/replay findings, and recaptured production-mode desktop/mobile screenshots after fixes. The post-fix browser assertions passed at 1440x1100 and 390x844.
- Published a sanitized public cookbook example on `GDACS-droid/solari-cookbook`, branch `billcommons-scout-challenge`; the post-live hardened head is commit `1095a02`. Its deterministic fixture, compile/diff, and explicitly opt-in live paths pass.
- Ran the hardened public cookbook example itself through Solari: official Online Sunshine robots policy, deterministic marker, one page/action, 4,935 ms, content SHA-256 `2eea2058576ce8bf11c5f93d987ee2d6eb44e046aef4e0904f57cddfc2b387a1`, session fingerprint `6ec77bde219c`, replay available, cleanup complete.
- Completed final adversarial repair: Scout now has credentialed production CORS; generic/login/maintenance/JS-shell HTML cannot create findings; retained text must support the exact bill and action/status; request/retry/daily spend budgets are enforced; failed runtime is counted; cleanup-failed sessions consume capacity; replay probes terminate without repeated release; expired claims exhaust; and persisted provider IDs are cleaned after mid-capture failure.
- Completed the opt-in Solari gate against Florida's official Online Sunshine domain (`www.leg.state.fl.us`): live authentication passed; a recorded browser navigated to the bounded robots resource; the deterministic `User-agent` marker was extracted; one action completed in 3,650 ms; replay became available; and cleanup was independently confirmed. Operator output exposes only session fingerprint `cebada2bd753`, never the signed session capability or replay URL.
- Repaired a lifecycle defect exposed by the first live attempt: the provider now creates the remote session explicitly, durably records its ID before Patchright connects, keeps drive and cleanup timeouts separate, releases known sessions after pre-connect cancellation/navigation failure, classifies failures without SDK text, and fingerprints signed IDs in CLI output. Focused provider/security coverage is now 27 passing tests.
- Conservatively tested `www.flsenate.gov` through Solari and observed a repeatable provider-side browser `connection_reset`; those diagnostic attempts were released, and the source-specific failure is documented rather than hidden. The direct HTTP source remains healthy.
- A pre-repair independent Gate 5 Grok review returned REVISE: interview-worthy, but missing live proof and production operability/full legacy regression were material. The live proof gap is now repaired; the independent release guard remains NOT READY for production.
- Post-live independent Claude Gate 5 review returned REVISE. Accepted findings were repaired: candidate lookup is session-scoped, displayed evidence must support the exact bill/action claim, and prior-source linking is tenant-scoped. No live government finding has been claimed. Rejected one medium claim that URL admission performs blocking DNS in the route callback: `admit_url` is syntactic and does not resolve DNS.
- Completed an opt-in live PostgreSQL/API product-path test against real MyFloridaHouse `BillId=84174`: safe direct fetch preserved a rejected `302`, the fixed-host policy admitted exactly one Solari browser session, and the durable session ended `released`. The fixture metadata intentionally did not describe the live bill, so no finding was required or fabricated.
- Independent browser-path red-team found and drove repairs for redirect interception, service-worker/WebSocket/popup egress, final-URL provenance, false pre-create usage, global-cap request accounting, and unreapable failed cleanup. Its final rerun returned PASS. The hardened live product-path test then passed again in 5.57 seconds with one session and durable release.

## RUNNING

- Final immutable Scout commit/reconciliation and submission handoff.

## BLOCKED

- None for deterministic implementation.
- Stripe Dashboard webhook registration and a completed paid provisioning round trip remain unproven. Existing live checkout creation is not equivalent to a purchase.
- Production schema migration, worker/API rollout, feature-flag enablement, and deployment are not authorized.

## Constraints / invariants

- Never inspect protected voter data or credential values.
- Preserve pre-existing modified/untracked ingest, monitoring, outreach, and `.claude` files.
- Schema changes are additive; no migration or production deploy without explicit authorization and preflight.
- API container must not import worker code.
- Fetched content is hostile data; every job access is owner-scoped; Solari use is bounded and cleanup is attempted in all terminal paths.

## Git state at final handoff

- Branch: `billcommons-scout`, tracking the public `origin/billcommons-scout` branch.
- Immutable feature commits: `3eb4832` (Scout), `aeee726` (served-session MCP fixes), and `3a814c7` (native Stripe checkout links).
- Pre-existing changes before Scout: modified `workers/ingest/billcommons_ingest/fulltext.py`, `healthcheck.py`, `workers/ingest/tests/test_healthcheck.py`; untracked `.claude/`, operations/outreach/rendered/monitoring files.
- Scout changes now include additive schema/migration, shared policy, owner-scoped API, dedicated worker/provider, web route/components/client/tests, `docs/scout/*`, `.env.example`, and safe setup/check helpers. `/docs/bulk` is the only Stripe-adjacent application edit. MCP session/coverage fixes are separately scoped.

## Verification actually run

- `apps/web`: `npm run test:scout` (4 passed), `npm run lint`, and `NEXT_PUBLIC_SCOUT_ENABLED=true npm run build` passed; the production build emitted `/scout`.
- Pre-live combined Scout Python gate: 101 passed. The final post-hardening focused regression expanded to 145 passed, with only the known TestClient and SQLite adapter deprecations. These cover shared URL/security tests, API/CORS/owner contracts, MCP session behavior, worker adversarial/lifecycle tests, and reaping while the feature flag is disabled; disposable-PostgreSQL E2E is recorded separately.
- Disposable PostgreSQL Scout E2E/concurrency/migration gate: 4 passed, 1 live test skipped by default, one TestClient deprecation warning. The explicit live product-path test passed after the final browser-egress hardening with the Solari key configured.
- MCP session/coverage gate: 29 passed.
- `git diff --check` and Python `compileall` passed.
- Broader shared/API regression: 641 passed, 3 skipped, 3 failed, 4 setup errors. The failures are existing production-corpus/status/performance assertions and a pre-existing mortality bucket vocabulary mismatch; the errors are legacy fixtures attempting duplicate NY/NE jurisdictions. This default path falls back to the configured live database and must not be repeated without an isolated test database.
- `scripts/congress_api_check.py` passed live authentication without exposing the key.
- Production Stripe checkout creation and the separately requested outreach operation ran successfully. The final live Solari browser/recording/replay/cleanup gate passed; no production schema migration/deploy or completed Stripe payment has run.
- Billing regression gate: 71 passed. The `/docs/bulk` page now uses the owned Builder/snapshot Checkout flows instead of legacy external Payment Links.
- Post-repair focused provider/security gate: 27 passed. Live Solari result: `session_ref=cebada2bd753`, one action, 3,650 ms, deterministic marker present, replay available, cleanup confirmed.
- Final web gate rerun after the hardened live pass: 4 tests, ESLint, typecheck, and production build passed with `/scout` emitted. Static generation logged nonfatal `ECONNREFUSED 127.0.0.1:8000` warnings for pages whose optional local API was absent.

## NEXT

Record the truthful demo, open/merge the prepared public branches, then publish the prepared social post. Production rollout remains a separate explicitly authorized operation and is still NOT READY under the release preflight.
