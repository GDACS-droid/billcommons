# API-sync evidence snapshot overflows

An Open States bill can have more child records than the fixed corpus-evidence
snapshot cap. The normal `api-sync` job isolates that one bill in a database
savepoint: its bill, children, events, evidence rows, and evidence blobs are
rolled back, while healthy bills on the same page remain committed.

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

## Migration coordination

This migration is `0031_snapshot_blockers` and revises `0030`. The separate,
undeployed saved-monitor branch also owns a `0031` revision. Before a combined
deployment, rebase one revision or create the appropriate Alembic merge
revision; do not edit either branch's migration in place after deployment.
