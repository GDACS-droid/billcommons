# Scout production dark-deploy record

Release window: 2026-09-01. Public Scout navigation and the public web route
remained disabled throughout. No social submission or pricing change was made.

## Pinned artifacts and topology

| Runtime | Exact production revision | Artifact | Replicas / command |
| --- | --- | --- | --- |
| Railway API (`d2b00b96-950a-4adc-8d82-87c09b1ce3d6`) | deployment `1e5cb460-a6ee-41a1-b36a-fe8d73e18144`; source commit `8dc0ce36d0b0407fc918610ad4a99f1f07adf3ba` | `sha256:e4036f693e221b484d5adedf616770d7d7d5a3f7bc8d59fb071afa56210574fb` | 1 replica; existing API entrypoint; 60-second drain |
| Railway Scout worker (`1f614208-b5dd-4f0e-b087-aa5d726bf3e7`) | deployment `2c5c21d6-4254-4c38-b56d-3f5a845f9ada`; source commit `8dc0ce36d0b0407fc918610ad4a99f1f07adf3ba` | `sha256:fb90487d80972f92da6462ebf232b489fc54d8e96dd3a76225823a963166ab1f` | 1 replica; Scout check then worker loop; 300-second drain |
| Vercel web | deployment `dpl_E3q37kAuv37CPMebU2Lme3hmviz5`; source commit `2cac01927c54e1df1a2d2f30e754255b37f5aaaf` | Vercel production artifact | Scout build/nav flags false; edge middleware returns a real 404 |

The API and worker have Scout enabled only for one normalized server-side canary
identity. `BILLCOMMONS_SCOUT_ALLOW_PUBLIC=false`. The worker's enforced ceilings are
2 active jobs, 20 jobs/day, 2 global browser sessions, 600 browser seconds/day,
5 external requests/job, 4 pages, and 12 actions. Platform ceilings additionally
limit all customers to 10 active jobs, 100 new jobs/day, 3,600 browser seconds/day,
and 512 MiB of retained Scout raw evidence. `/scout` returns HTTP 404 with a
9-byte plain response, no Scout metadata, and no public navigation link.

## Migration and durable state

- PostgreSQL source: 18.6.
- Pre-migration revision: `0021`; production and both restore checks are now `0025`.
- Migration `0025` is additive: six Scout tables, eight expected added columns,
  nineteen reviewed browser/raw constraints, and three job indexes.
- The single supported production runner is `scripts/controlled_migration.py`. It
  requires an explicit target/provenance/pre-revision acknowledgement, clears ambient
  fallback configuration, uses an isolated empty home, and invokes Alembic once.
- Sanitized post-fix target fingerprints: production
  `297fdbfd9685fac733d6`; disposable restore `42461319d8f038b99618`.

The first production migration invocation exposed a real control defect: an empty
public-URL variable let Alembic fall back to a local production configuration. The
authorized additive migration therefore reached `0025`, but not through the intended
proxy command. No destructive rollback was attempted. The new fail-closed runner and
26 operator-script tests were added immediately; controlled production and disposable
check-only runs now prove the correct target and revision without ambient fallback.

## Named canary

The API canary queried Florida `HB 625` through the production API and worker:

- completed, not partial; 3 findings from 3 official sources;
- 3 direct external requests, zero browser sessions;
- all three sources retained raw references and hashes;
- duplicate submission reused the same fresh cached job and spent no new retrieval.

The separate durable Solari lifecycle canary exercised the production Scout provider
path against an official Florida source without manufacturing a finding:

- job completed, not partial, with no error class and zero findings;
- 1 official source with retained raw bytes and content/document hashes;
- 1 browser session, 1 page, 1 action, 19 routed requests, **9,291 ms** runtime;
- terminal status `released`, `released_at` present, replay available but capability
  URL withheld;
- global `cleanup_failed=0`, `reaping=0`, `abandoned=0`;
- a repeat invocation returned `provider_call=skipped`; the reaper found zero session,
  staging, or raw-blob candidates.

At current published runtime rates, the measured session is approximately $0.00039
Free, $0.00026 Starter, or $0.00018 Professional. At identical runtime, $3 Free
credit is roughly 7,700 sessions and 200 Starter browser-hours roughly 77,000.
These are arithmetic estimates, not provider billing guarantees. A fresh cache reuse
spends no browser runtime.

## Monitoring and rollback

- `service_state_monitor.py`: 7/7 services healthy, including `scout-worker`.
- `read_path_monitor.py`: 5/5 probes healthy, including API, real bill read, web,
  MCP, and sponsor filtering.
- Final exact-image log review found zero API/worker error or traceback matches and
  zero API HTTP 5xx for the restored revisions.
- API admission was disabled first; after zero active jobs/sessions, the worker was
  disabled and restarted in explicit idle/no-claim mode.
- Pinned rollback reconciliation returned `reaped=0 terminalized=0`; the subsequent
  reaper returned zero eligible sessions/stages/blobs. Three completed jobs, seven
  sources, six findings, five raw blobs, and the released browser ledger remained.
- The private canary was restored worker-first, then API using the same immutable
  image digests. Both final Railway deployments are `SUCCESS`; public rollout and
  web flags remain false.

## Backup and restore

- Pre-migration full backup: 3,309,456,252 bytes, mode `0600`, SHA-256
  `64c6ea193d47baee61e465c5bcaa27e9398da174e576f5ce42193afd75e2f581`.
  Its disposable restore matched revision `0021` and all five core corpus counts,
  then migrated independently to `0025` and passed Scout readiness.
- Post-canary full backup:
  `/home/alberto/.local/share/billcommons/scout-release-20260901-fSp8Od/billcommons-production-post-canary-20260901.dump`;
  3,310,456,831 bytes, mode `0600`, SHA-256
  `118b55ba00266aff720f8fc34b98d7b1d64a49bab8b25acd7fa442315ade33a3`;
  PostgreSQL 18 custom-format catalog valid.
- The post-canary artifact restored cleanly into database
  `scout_post_canary_restore` on the disposable PostgreSQL 18 service in environment
  `scout-restore-20260901`.
- That full-base snapshot matched production at 52 jurisdictions, 78 sessions,
  210,370 bills, 1,668,296 actions, 738,428 documents, 2 API customers,
  2 completed Scout jobs, 4 sources, 3 findings, 22 events, 1 released browser
  session, and 4 raw blobs.
- A final API canary after the full dump created a new completed job before its
  duplicate submission proved cache reuse. The current recovery set therefore adds
  `/home/alberto/.local/share/billcommons/scout-release-20260901-fSp8Od/billcommons-production-scout-current-20260901.dump`,
  a data-only custom archive of all six `scout_*` tables: 976,993 bytes, mode `0600`,
  SHA-256 `f97421bd7c0ab209820e834163f2e1f270743720ba7ad163e022e4e83441277a`.
- Applying that current Scout archive over the proven full restored base produced
  exact live/restore parity: 3 completed jobs, 7 official/raw/hash sources,
  6 findings, 38 events, 1 released session, and 5 raw blobs. Usage matches at
  7 external requests, 1 browser page/action, 19 routed requests, and 9,291 ms.
  All 5 blobs re-hash to their keys; sorted key-set fingerprint
  `381cc174bef3c95cc7d39dac2b0b7bf6ec6c31b47ddee79820d2f1d4e7a00d66`.
- The final Scout worker readiness check against the restored database passed:
  database, Scout tables, PostgreSQL RawStore, Solari configuration, and SDK all OK.

## Verification

- Backend/operations: **862 passed, 8 skipped, 11 warnings** (571 API, 165 shared,
  96 Scout worker, 4 monitoring, 26 operator scripts).
- Web Scout contract: **10 passed**; targeted ESLint, sequential TypeScript check,
  and Next production build passed.
- Public dark proof: `/scout` HTTP 404; no metadata or navigation entry.
- No live operation printed a provider identifier, replay/capability URL, cookie,
  database URL, or secret.

## Deviations repaired during the window

1. Fail-open migration environment fallback: replaced by the controlled fail-closed
   runner described above.
2. A disposable database URL briefly appeared in local process arguments during the
   first restore attempt; it was not a production credential and was rotated before
   reuse. Later commands use proxy host/user/password environment fields without URL
   arguments.
3. The first dark Vercel artifact rendered a generic 404 page with HTTP 200 and Scout
   metadata. Edge middleware now returns a real no-store 404 with no metadata.
4. A local Railway run could not resolve the private database hostname; operator
   canary/read paths now use the Postgres service's public proxy binding without
   printing it.
5. A post-canary restore was initially started while `pg_dump` was still running
   because a yielded terminal was mistaken for process completion. The disposable
   partial restore was rejected, terminated, and recreated; only a completed dump
   plus clean restore may close the gate.
6. A final post-deploy API canary changed production after the full dump. Independent
   review caught the stale 2/4/3/4 documentation; a current Scout data archive was
   captured and restored over the proven base, producing exact 3/7/6/5 parity.
