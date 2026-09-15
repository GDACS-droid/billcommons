# Saved Scout monitors and bounded Florida bill text — release candidate

This candidate adds owner-scoped saved research monitors with pause/resume,
configured cadence limits, shared quota/cache admission, retained baseline/run
history, and conservative observed-source comparisons. Florida research may
also attach one directly retrieved bill-text version. Missing text remains an
explicit partial outcome; it is not guaranteed. No email delivery is added.

## Artifact boundary

The base is production API commit `7d6ad1b`. The selected Scout API, shared
admission/monitor settings, worker, frontend, and tests come from `e7d095e`,
including the fixes for admission timing, configured limits, actionable errors,
request races, and UUID research links. The earlier Florida bill-text scope was
already part of the pending `e415bc7` monitor release. Later meeting discovery
and native ingest repair work are excluded. Production source-health, safe HTTP,
TLS, and California parser code remain identical to the API base.

Both unchanged sibling migrations (`0031` and `0031_snapshot_blockers`) are
included to preserve the existing `0032_intelligence_merge` lineage. The
snapshot-blocker table is inert in this candidate: this release does not deploy
its ingestion producer or claim that feature is available.

## Rollout and rollback gates

Current status: NOT READY until final review, current backup, compatibility,
controlled migration, worker handoff, owner canary and sustained health gates
are recorded. The user approved the pending rollout on September 15 and
confirmed no concurrent operator. The missing revision marker has separately
been restored and verified as `0030`; that repair did not apply these migrations.

1. Pin and review this entire candidate. Verify a fresh read-only backup and
   exact production identity/revision before targeting `0032_intelligence_merge`.
   Upgrade through the controlled runner with its explicit target and matching
   acknowledgement, never an ambient database or an unqualified `head`.
2. Preserve data on rollback. Exercise additive upgrade and local downgrade
   on an owned disposable database; production rollback retains all new tables
   and monitor/evidence rows. Do not restore an older database for an app rollback.
3. Deploy the capable Scout worker first. Its existing SIGTERM handler finishes
   the current bounded claim and stops before the next; provider grace is 300
   seconds with zero overlap. Require positive old-worker absence, candidate
   image identity, entrypoint readiness and two clean readiness observations
   before promoting the API. The generic crawl worker is outside this rollout.
4. Promote the API with its existing data-health readiness and 60-second drain.
   Require health, readiness, real bill reads, source-health contract, owner
   monitor operations, and one bounded scheduler canary. Canary monitors finish
   paused. Keep API and worker quota/raw-storage ceilings in force.
5. Promote the frontend only after API/worker proof; verify real browser save,
   pause/resume and history on desktop and mobile, then sustained public health.
6. If monitor admission, ownership, evidence, cost, or cleanup checks fail, stop
   rollout. Disable new Scout admission and allow candidate-namespace jobs to
   finish with the capable worker before reverting to a prior worker. Preserve
   owner research, monitor history, raw evidence and additive schema. Previous
   deployed artifact IDs remain in the private release record.

## Local evidence

The isolated release candidate passed PostgreSQL 16 upgrade/rollback from
`0030`, either sibling revision, and the merged target. Scout contention tests
passed 35 checks with two opt-in live-source/provider checks skipped; focused
API/shared/worker/migration checks passed 276 with one skipped. Frontend tests
passed 33 checks and the production build passed. These are candidate-local
checks, not a production rollout claim. Raw evidence and source mapping are in
the private `saved-monitor-release-20260915` release directory.
