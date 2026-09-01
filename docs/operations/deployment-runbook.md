# Production deployment runbook

This is the required operator procedure for Bill Commons production changes,
including Scout. It is a **staged rollout checklist**, not authority to deploy.
The repo supports Railway for Python services/Postgres and Vercel for the web
application, but does not contain Railway project/service bindings, Vercel
project bindings, a deployment token, or a release automation workflow. Treat
every value marked `<OPERATOR_INPUT>` as live control-plane state to verify at
the time of the release; do not guess it from a branch name or this document.

Related procedures: [backup and restore](backup-restore.md),
[incident response](incident-runbook.md), [Stripe and API billing](monetization-runbook.md),
[Scout architecture](../scout/ARCHITECTURE.md), and
[Railway/Vercel DNS](../../infra/deployment/DNS.md).

## 0. Authority and release record

Do not start until a designated release owner has explicitly approved the
deployment window and the following record is filled in. A code merge,
successful CI run, or a Railway "Online" badge is not release authority.

| Field | Required value/evidence |
| --- | --- |
| Change owner / approver / window | `<OPERATOR_INPUT>` |
| Git commit to ship | immutable full SHA: `<RELEASE_SHA>` |
| Previous known-good API/MCP/ingest/Scout deployment IDs | `<OPERATOR_INPUT>` for every service changed |
| Previous known-good Vercel deployment URL/ID | `<OPERATOR_INPUT>` |
| Railway linked directory/project/environment | `<OPERATOR_INPUT>`; confirm, do not relink an arbitrary worktree |
| Railway service handles | API `<API_SERVICE>`; MCP `<MCP_SERVICE>`; ingest `<INGEST_SERVICE>`; Scout `<SCOUT_SERVICE>` |
| Vercel project/environment | `<VERCEL_PROJECT>` / `<VERCEL_ENVIRONMENT>` |
| Database target and current Alembic revision | `<OPERATOR_INPUT>`; record only hostname/database label and revision, never URL/password |
| Scout evidence backend and migration proof | PostgreSQL `scout_raw_blobs`; record migration `0025` and readiness output |
| Backup artifact, checksum, restore-drill evidence | `<OPERATOR_INPUT>` |
| Rollback decision owner and communications contact | `<OPERATOR_INPUT>` |

Pin the exact source before building. The tree must be reproducible; do not
ship a dirty checkout or silently include unrelated local files.

```bash
git fetch --tags origin
git rev-parse --verify "<RELEASE_SHA>^{commit}"
git diff --check "<RELEASE_SHA>^" "<RELEASE_SHA>"
git status --short
git show --no-ext-diff --format=fuller --stat "<RELEASE_SHA>"
```

Record the image digest or Railway build/deployment ID actually produced from
`<RELEASE_SHA>` for each Python service and the immutable Vercel deployment
URL/ID for the web build. A branch name, `latest` tag, or merely matching
source SHA is insufficient artifact pinning.

The repository supplies these build targets; it does **not** prove which
Railway service currently uses each one:

| Component | Repository artifact | Runtime command |
| --- | --- | --- |
| API | `infra/docker/Dockerfile.api` | `uvicorn main:app --app-dir apps/api ...` |
| MCP | `infra/docker/Dockerfile.mcp` | `python apps/mcp/server.py` |
| Ingest | `infra/docker/Dockerfile.worker` | `python -m billcommons_ingest.autoboot` |
| Scout | `infra/docker/Dockerfile.scout-worker` | image entrypoint runs `python -m billcommons_scout check`, then `python -m billcommons_scout worker` |
| Web | `apps/web` | Vercel build from the pinned source revision |

## 1. Preflight: live state, capacity, and secrets

Run only read-only inventory commands first. `npx @railway/cli` is used by
the existing monitor and incident runbook; its exact login/project linkage is
an operator input. Do not paste or print secret values.

```bash
# Run inside the established, deliberately linked Railway directory.
cd <RAILWAY_LINKED_DIRECTORY>
npx -y @railway/cli status
npx -y @railway/cli deployment list -s <API_SERVICE> --json --limit 5
npx -y @railway/cli deployment list -s <MCP_SERVICE> --json --limit 5
npx -y @railway/cli deployment list -s <INGEST_SERVICE> --json --limit 5
npx -y @railway/cli deployment list -s <SCOUT_SERVICE> --json --limit 5
```

If the Scout service is not present, stop the Scout rollout. Creating or
naming it is a control-plane change and must be explicitly recorded. The
service-state monitor currently has no Scout entry; add and validate Scout
monitoring before enabling public traffic rather than assuming the existing
monitor covers it.

Confirm the API remains one replica before public traffic. The current
in-process API-key/quota counters do not remain globally authoritative across
multiple API replicas; see [the monetization runbook](monetization-runbook.md#runway-numreplicas1-assertion).

### Required configuration ownership

Secrets belong only in the deployment secret manager (or the ignored local
Bill Commons env file for local work), never source, images, Vercel public
variables, browser logs, or this evidence log.

| Scope | Required configuration / proof |
| --- | --- |
| API | `DATABASE_URL`; account/billing/Resend configuration in [the monetization runbook](monetization-runbook.md#required-environment-variables); `BILLCOMMONS_SCOUT_ENABLED=false` for dark launch |
| MCP | `DATABASE_URL`; any existing MCP telemetry configuration; no Scout provider key needed |
| Ingest | `DATABASE_URL`, configured host-auth secrets if applicable, and durable `RAWSTORE_ROOT` |
| Scout worker | `DATABASE_URL`, `BILLCOMMONS_SCOUT_RAWSTORE_BACKEND=postgres`, `SOLARI_API_KEY`, all `BILLCOMMONS_SCOUT_*` ceilings, and `BILLCOMMONS_SCOUT_ENABLED=false` for dark launch |
| Vercel web | `NEXT_PUBLIC_API_BASE=https://api.billcommons.org`, intended `NEXT_PUBLIC_SITE_URL`, and both `NEXT_PUBLIC_SCOUT_ENABLED=false` / `NEXT_PUBLIC_SCOUT_NAV_ENABLED=false` for dark launch. `NEXT_PUBLIC_*` values are public by design; never put `SOLARI_API_KEY`, Stripe secret material, `DATABASE_URL`, or internal-client secrets there. |

### Scout evidence storage is a hard gate

Scout only commits a finding after retaining the exact bytes. Migration `0025`
adds the Scout-only, SHA-256-addressed `scout_raw_blobs` table; the worker uses
that PostgreSQL backend by default. This deliberately separates Scout evidence
from ingestion's filesystem `RAWSTORE_ROOT`, so separate Railway services do
not need a shared volume.

Before enabling Scout, record all of the following:

1. migration `0025` is current on the exact production database;
2. the worker has `BILLCOMMONS_SCOUT_RAWSTORE_BACKEND=postgres` or leaves the
   default intact (filesystem requires a second explicit local-only opt-in);
3. `python -m billcommons_scout check` reports `rawstore=ok`; and
4. the production backup/restore proof includes `scout_raw_blobs`, because
   retained Scout evidence is part of the auditable record.

The repository's guarded PostgreSQL test proves concurrent idempotent writes
and retrieval through a recreated store instance. Application and database
constraints both cap a blob at 2 MiB; the worker reaps expired unverified stages
and old unreferenced blobs after a 24-hour default retention barrier. Record the
chosen `BILLCOMMONS_SCOUT_STAGING_RETENTION_SECONDS` and monitor table growth. A
real deployment restart is still post-deploy evidence, not a reason to reintroduce
a cross-service mount.

### Pre-deploy health and capacity evidence

Use the body of `/health`, not its HTTP status alone. The endpoint returns
HTTP 200 while the database is degraded. Also verify a real API read, the
public web path, and MCP as separate services:

```bash
curl -fsS --max-time 10 https://api.billcommons.org/api/v1/health
curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 10 \
  'https://api.billcommons.org/api/v1/bills?per_page=1'
curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 15 \
  https://billcommons.org/states/NC
.venv/bin/python infra/monitoring/read_path_monitor.py
```

The health JSON must say `"database":"ok"`. Follow the sustained-probe and
MCP tool-call procedure in [the incident runbook](incident-runbook.md), not a
single lucky response. Review the database pool census and existing worker
queue/freshness monitors before increasing service count or DB pool settings.

## 2. Backup and restore proof before migrations

All Scout migrations (`0022`–`0025`) are additive, but an additive migration
can still exhaust capacity, block DDL, or be applied to the wrong database.
Do not run any migration until this section is complete.

1. Inspect the managed Postgres snapshot/restore state in the Railway
   dashboard and record its timestamp and retention policy.
2. Create a portable custom-format dump using the established Railway service
   context. Keep the dump outside the repository and record its restricted
   storage location and SHA-256 checksum, not credentials.

   ```bash
   cd <RAILWAY_LINKED_DIRECTORY>
   railway run --service <API_SERVICE> pg_dump "$DATABASE_URL" \
     --format=custom --no-owner --no-acl \
     --file="billcommons-<UTC_DATE>-<RELEASE_SHA>.dump"
   sha256sum "billcommons-<UTC_DATE>-<RELEASE_SHA>.dump"
   ```

3. Prove that the exact artifact restores into a **fresh disposable
   non-production Postgres 16 target**. A restore test is required evidence;
   a successful `pg_dump` alone is not proof of recoverability.

   ```bash
   pg_restore --format=custom --no-owner --no-acl \
     --dbname="$RESTORE_DRILL_DATABASE_URL" \
     "billcommons-<UTC_DATE>-<RELEASE_SHA>.dump"

   cd packages/schema
   DATABASE_URL="$RESTORE_DRILL_DATABASE_URL" ../../.venv/bin/alembic current
   ```

4. Point only a disposable API instance at the restored target and verify
   `/api/v1/health`, `/api/v1/ready`, and a representative search. Destroy
   only the explicitly identified disposable target after the evidence has
   been retained.

For the exact restore cautions and RawStore re-fetch limits, follow
[backup-restore.md](backup-restore.md). There is no repository-managed
scheduled off-Railway `pg_dump`; do not claim this runbook closes that gap.

## 3. Migration and dark deploy ordering

This order keeps an additive schema compatible with old readers. No
destructive downgrade is permitted in a production rollback.

1. Keep both flags false:
   `BILLCOMMONS_SCOUT_ENABLED=false` / `BILLCOMMONS_SCOUT_ALLOW_PUBLIC=false`
   in API and Scout worker, and `NEXT_PUBLIC_SCOUT_ENABLED=false` /
   `NEXT_PUBLIC_SCOUT_NAV_ENABLED=false` in Vercel.
2. Deploy the pinned API/MCP/ingest/Scout artifacts to their respective
   services **without enabling Scout traffic**. Confirm each deployment ID
   maps to `<RELEASE_SHA>` and each service command matches the table above.
3. Apply the schema once, from a single controlled migration runner against
   the verified production database. The repo does not declare which Railway
   service is the migration runner; `<MIGRATION_RUNNER>` is an operator input.

   ```bash
   cd packages/schema
   DATABASE_URL="$DATABASE_URL" ../../.venv/bin/alembic current
   DATABASE_URL="$DATABASE_URL" ../../.venv/bin/alembic upgrade head
   DATABASE_URL="$DATABASE_URL" ../../.venv/bin/alembic current
   ```

   Record the pre/post revision. Do not execute this from more than one
   service, do not rely on app startup to migrate, and do not run a downgrade
   as a routine rollback action.
4. Recheck API and MCP read paths with flags still dark. Confirm no Scout job
   can be created while the API flag is false, that the Scout image readiness
   check succeeds before the process enters the worker, and that the disabled
   long-running worker remains healthy/idling without consuming jobs. A
   one-shot `worker --once` intentionally returns its disabled/no-work result.
5. Deploy the pinned Vercel artifact with `NEXT_PUBLIC_SCOUT_ENABLED=false`.
   Confirm `/scout` is unavailable or communicates the disabled state and the
   global navigation does not advertise Scout.

Database schema is deliberately retained during rollback. Previous API/MCP/
ingest code must tolerate the additional Scout tables; that compatibility is
a release precondition, not an excuse to drop data.

## 4. Scout cohort/canary enablement

Scout has a server-side email allowlist for private canaries. When
`BILLCOMMONS_SCOUT_CANARY_EMAILS` is non-empty, only those normalized account
emails may create new jobs; existing owners can still read/cancel their jobs.
An empty allowlist denies new jobs unless the separate
`BILLCOMMONS_SCOUT_ALLOW_PUBLIC=true` acknowledgement is set. The global flag
alone can therefore never accidentally create an all-account rollout.

1. Verify the evidence-store gate, Solari secret scope, browser ceilings, and
   set `BILLCOMMONS_SCOUT_CANARY_EMAILS` to the reviewed canary accounts.
2. Enable `BILLCOMMONS_SCOUT_ENABLED=true` with
   `BILLCOMMONS_SCOUT_ALLOW_PUBLIC=false` only for the API/Scout environment
   serving the controlled canary. Build Vercel with
   `NEXT_PUBLIC_SCOUT_ENABLED=true` and `NEXT_PUBLIC_SCOUT_NAV_ENABLED=false`:
   the direct `/scout` entry works for the canary, while global navigation does
   not advertise it and the API returns 404 to non-allowlisted accounts.
3. Verify worker readiness without exposing secrets. With the PostgreSQL backend,
   the check performs read-only database/table/RawStore queries; it does not create
   a blob or browser session:

   ```bash
   BILLCOMMONS_SCOUT_ENABLED=1 \
     python -m billcommons_scout check
   ```

   A healthy check requires database/tables and RawStore `ok`; if Solari is
   configured, the SDK must be available. It must not print a key or provider
   session capability.
4. Run one controlled, evidence-supported job. Inspect durable job events,
   source/finding provenance, RawStore retention, usage ceilings, owner
   isolation, and browser-session terminal state. A provider browser session
   is billable; run the separate `solari-check` only with its explicit opt-in
   flag and log the safe fingerprint/output.
5. Observe a full browser ceiling/window plus the Scout lease and cleanup
   intervals. Review durable rows for `cleanup_failed`, `reaping`,
   `abandoned`, failed/partial jobs, and unexpected spend. Do not expand the
   cohort if any unknown terminal state, unreleased provider session, or
   provenance failure appears.
6. Only after the canary evidence is accepted and a global queue/storage
   capacity policy is recorded, deliberately clear the server allowlist, set
   `BILLCOMMONS_SCOUT_ALLOW_PUBLIC=true`, and deploy a new immutable Vercel
   artifact with `NEXT_PUBLIC_SCOUT_ENABLED=true`. Re-run the public web,
   API, worker, and owner-isolation checks with
   `NEXT_PUBLIC_SCOUT_NAV_ENABLED=true`. Expansion beyond the defined
   cohort needs a written cost and support decision; no percentage ramp is
   encoded, but the allowlist supports a controlled account-by-account ramp.

### Drain, reap, and rollback semantics

Current behavior is deliberately conservative:

- Setting `BILLCOMMONS_SCOUT_ENABLED=false` blocks new API job creation and
  new worker claims. A long-running dark-deployed worker remains alive for
  health/log observation without constructing a claim runner; `worker --once`
  returns its disabled/no-work result. It does not kill a fresh already-claimed
  browser action.
- `SIGTERM`/`SIGINT` asks the worker loop to drain: it stops before its next
  claim and lets the in-progress `run_once` follow its normal cleanup path.
  Set a platform termination grace period that exceeds the configured browser
  wall/cleanup budget; the repository cannot enforce a Railway stop timeout.
- A running job has a lease/heartbeat. Existing work should be allowed to
  reach a terminal state, or become lease-expired; operator timing must cover
  the configured browser wall time, cleanup timeout, and lease.
- `python -m billcommons_scout reap` remains available while Scout is dark.
  It retries eligible `cleanup_failed` sessions, terminal/expired live
  sessions, stale reaping attempts, and bounded delayed replay probes.
- `python -m billcommons_scout rollback` first runs the reaper and then marks
  queued and lease-expired running jobs `failed` with `error_class=rolled_back`.
  It intentionally does **not** kill a fresh claim. Completed and partial
  evidence remains readable by its owner.

During rollback: turn off the API flag first, send the Scout worker a graceful
termination signal only after it can no longer claim new work, wait for fresh
work to finish or expire, then run the pinned Scout artifact's rollback
command with the same database/RawStore/Solari configuration:

```bash
python -m billcommons_scout rollback
python -m billcommons_scout reap
```

Record both counters and repeat `reap` until no eligible session remains. If
the provider reports an unreleaseable remote session, preserve the durable row
and escalate with its non-secret session fingerprint/telemetry; never delete
the row to make capacity look healthy. Keep the additive schema in place and
redeploy the previous compatible application artifact only after evidence is
captured.

## 5. Stripe/configuration preflight (separate from Scout)

If the release touches API billing, account routes, public checkout UI, or
Stripe environment variables, complete this independent gate before taking
traffic. Scout does not make a Stripe release safe.

- Verify every required API variable at startup without exposing values:
  account session/reveal secrets, Resend, operator alert email, restricted
  Stripe key, webhook signing secret, and all six configured Price IDs.
- Verify `BILLCOMMONS_PUBLIC_SITE_URL` and allowed origins point to the
  intended production domain.
- In the Stripe Dashboard, verify the webhook endpoint is exactly
  `https://api.billcommons.org/api/v1/billing/webhook`, uses API version
  `2025-03-31.basil`, and is subscribed to the event set documented in
  [monetization-runbook.md](monetization-runbook.md#3-webhook-endpoint).
- Verify the restricted key and Price IDs are from the same intended Stripe
  mode; do not mix Test and Live values.
- Run a controlled test-mode end-to-end checkout/webhook/provisioning check
  before any live checkout configuration change. Production proof requires an
  explicitly authorized controlled live transaction; a created Checkout
  Session is not a completed purchase or webhook proof.
- Keep API replicas at one until quota/key caches are made shared, per the
  monetization runbook.

## 6. Sustained post-deploy proof and monitoring

Immediately after each stage and again after the observation window, capture:

```bash
# The health body must include database=ok.
curl -fsS --max-time 10 https://api.billcommons.org/api/v1/health
curl -fsS --max-time 10 https://api.billcommons.org/api/v1/ready

for p in health "bills?per_page=1" "coverage?per_page=5" "changes?limit=5" \
         "stats/mortality"; do
  printf '%-28s ' "$p"
  curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 20 \
    "https://api.billcommons.org/api/v1/$p"
done

.venv/bin/python infra/monitoring/read_path_monitor.py
.venv/bin/python infra/monitoring/freshness_monitor.py
```

Run the documented MCP tool probe from [incident-runbook.md](incident-runbook.md#4-verify-recovery--five-endpoints-not-one).
For background services, verify the latest deployment is `SUCCESS` and that
the expected service list includes the new Scout service before calling the
rollout healthy. Existing `service_state_monitor.py` does not currently list
Scout; that is a blocking monitoring gap, not a green signal.

For a Scout-enabled cohort, also collect without leaking source text, replay
URLs, session IDs, or customer identities:

- job counts by terminal status and partial rate;
- direct/browser strategy count, cache-hit rate, provider runtime/pages/actions;
- `cleanup_failed`/`reaping`/`abandoned` counts and reaper output;
- browser and external-request budgets versus configured ceilings;
- API error/429/503 rates, DB pool saturation, and worker deployment status;
- evidence/open and replay/open analytics only under existing privacy rules.

One passing request does not close the release. Keep the release owner on the
observation window defined in the release record and require sustained API,
web, MCP, database, ingest freshness, and Scout worker evidence.

## 7. Rollback decision tree

| Condition | Immediate action | Preserve |
| --- | --- | --- |
| API/MCP/web regression immediately after deploy | Capture evidence, redeploy the recorded compatible prior artifact, then run sustained probes | additive database schema; deployment IDs/logs |
| Scout cost, session cleanup, provenance, or owner-isolation concern | disable Scout API flag; prevent new claims; drain/expire fresh work; run `rollback` then `reap` | all jobs, raw artifacts, browser rows, usage, non-secret diagnostics |
| Migration failure before app traffic | stop at dark flags; do not downgrade or drop Scout tables; restore only through the proven restore procedure if the data state itself is unsafe | backup checksum, revision, DDL/error evidence |
| Ingest/freshness failure | do not call public read health sufficient; use freshness/queue evidence and follow the incident/source runbooks | queue state, logs, source error class |
| Stripe webhook/config failure | disable affected checkout exposure if needed; do not fabricate payment status; use Stripe event IDs and the monetization runbook | Stripe event/idempotency evidence and API logs |

Never use `alembic downgrade`, table drops, `TRUNCATE ... CASCADE`, or ad hoc
deletion of Scout/session rows as a release rollback. Those operations are
destructive, do not release a remote browser, and are outside this runbook.

## 8. Release evidence log template

Store this completed record in the approved private release system. Keep
credentials, raw provider capabilities, customer PII, and unredacted replay
URLs out of it.

```text
Release: <name>
UTC window: <start> to <end>
Owner / approver: <names>
Source SHA: <full immutable SHA>
Artifacts: API <deployment ID + digest>; MCP <...>; ingest <...>; Scout <...>; Vercel <deployment URL/ID>
Previous artifacts: <per-service IDs and Vercel deployment>
Railway project/environment and service handles: <operator-verified values>
DB target label + Alembic revision: before <rev>; after <rev>
Backup: <restricted location>; SHA-256 <checksum>; restore drill <timestamp/result>
RawStore: owner <...>; root/backend <...>; persistence/redeploy proof <...>
Flags: API Scout <false/true>; Scout worker <false/true>; Vercel Scout <false/true>
Canary/cohort and authorization boundary: <...>
Stripe gate (if applicable): <N/A or test/live proof + webhook configuration check>
Preflight: API health body <...>; API read <...>; web <...>; MCP <...>; pool/capacity <...>
Migration: runner <...>; command <...>; revision evidence <...>
Post-deploy observation: <timestamps, monitor output, error/429/503, worker/freshness state>
Scout observations: <safe aggregate status/usage/reaper evidence, or N/A>
Decision: <ship / hold / rollback>; decision owner <...>
Rollback actions/evidence, if any: <...>
Follow-ups with owner/date: <...>
```

## Preconditions that currently block a general production Scout launch

This runbook makes gaps visible; it does not make them disappear. Before a
general public Scout launch, resolve and prove at least these conditions:

1. the real Railway Scout service, its pinned artifact mapping, and proof that it
   uses the migrated PostgreSQL Scout RawStore backend;
2. Scout inclusion in service-state/alerting and an agreed canary mechanism;
3. a current production backup plus successful isolated restore drill;
4. a green, isolated full regression gate or documented triage of unrelated
   failures; and
5. Stripe webhook and completed paid-provisioning proof for any billing
   release in scope.
