# API-sync evidence snapshot overflows

An Open States bill can have more child records than the fixed corpus-evidence
snapshot cap. The normal `api-sync` job isolates that one bill in a database
savepoint: its bill, children, events, evidence rows, and evidence blobs are
rolled back, while healthy bills on the same page can commit with the enclosing
job. Unexpected storage or application errors still fail the whole job and
require the caller to roll back.

Each isolated overflow creates or refreshes one active
`api_sync_snapshot_blockers` row. The row contains only structured operational
fields: jurisdiction, optional existing Bill UUID, a SHA-256 source identity,
component, cap, first/last-seen timestamps, processing version, and hashed
scan-window context. It deliberately stores no source payload, URL, or raw
exception text.

An active blocker prevents every API-sync run for that jurisdiction and source
from becoming successful, including a later pagination continuation whose page
does not contain the original bill. Therefore the ordinary `updated_since`
watermark cannot advance across unresolved evidence. A later successful full
snapshot of the same session/normalized bill identity resolves the blocker and
allows a completed cycle to succeed.

Data Health reports active counts and at most five samples per jurisdiction;
the complete count remains visible when samples are truncated. A new bill
rolled back before insertion has no local bill UUID. Its identity fingerprint
is enough to match a subsequent successful sync, but is not a retained source
fixture or a reversible bill label. Investigation of that case may require a
new bounded source capture. No historical occurrence or duplicate-row claim
is inferred from an overflow.

## Operator response

Inspect active blockers through the control-plane health view, identify the
named component and cap, and repair the evidence representation or source
record through an approved code/data-repair path. Do not delete or deduplicate
bill history, truncate evidence, reset queue attempts, or mark the blocker
resolved by hand. Re-run the bounded sync after the underlying condition has
been fixed; successful evidence capture resolves the matching row.

`sync_state` remains fail-closed for direct callers such as
`backfill-api-versions`. Only `run_api_sync_job` opts into isolation, because
it also persists and checks the durable blocker before recording a successful
watermark.

The manual `api-sync` command commits healthy progress and returns a nonzero
exit status with `INCOMPLETE` when unresolved blockers remain. The scheduled
worker completes that queue attempt while retaining the failed ingestion run,
so retries do not repeatedly spend attempts on the same deterministic cap.

## Local validation

The integrated worker, scheduler, quota, Data Health database/CLI, shared
report and API checks passed 158 tests on an owned disposable PostgreSQL 16
cluster. The new migration upgraded, downgraded to `0030`, and upgraded again
in that same disposable cluster. Log:
`/tmp/bc_snapshot_isolation_root_pg_20260912.log`. One existing FastAPI/httpx
deprecation warning remains. This is local validation, not a deployment or
proof that the production Alaska corpus has been repaired.

## Migration coordination

This migration is `0031_snapshot_blockers` and revises `0030`. The separate,
undeployed saved-monitor branch also owns a `0031` revision. Before a combined
deployment, rebase one revision or create the appropriate Alembic merge
revision; do not edit either branch's migration in place after deployment.
