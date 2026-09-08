# Saved Scout monitors

A saved monitor is an account-owned, recurring Scout query. It starts only
from a completed or partial user Scout job that retained a finding and source
evidence. Operator lifecycle/canary jobs cannot be saved.

The initial saved job becomes a baseline `scout_monitor_runs` record. Later
runs retain source IDs, canonical URLs, hashes, raw references, and finding
IDs; they do not copy source bytes or excerpts. A comparison can report
new, changed, and unchanged observed sources. It never reports a removal,
because an incomplete fetch cannot prove that an upstream source disappeared.

The Scout worker selects one due active monitor with `FOR UPDATE SKIP LOCKED`.
It sends the saved request through `admit_scout_job`, the exact shared path
used by `POST /api/v1/scout/jobs`. This preserves the customer lock, platform
advisory lock, active/daily job limits, browser reservations, and raw-store
reservations. A fresh or active matching job is associated with the monitor
run; a worker does not insert a Scout job itself.

Default limits are three monitors per owner and a cadence between six hours
and seven days. Admission refusal is a durable `deferred` run with bounded
exponential retry. It does not use alert email or webhook delivery.

`GET /api/v1/scout/monitors/{monitor_id}/runs` returns newest-first history
in pages of at most 100. Pass its `next_cursor` as `cursor` to continue after
the final run returned by the previous page.

## Rollout

1. Apply Alembic revision `0031` to the API and Scout worker database before
deploying either application revision.
2. Deploy API and worker code together. A worker that lacks the new tables
cannot run its scheduler; an API that lacks them cannot save monitors.
3. Keep Scout's existing feature and public/canary controls enabled only for
approved capacity. Resuming a monitor requires the current rollout policy;
pause and owner history remain available during a dark rollback.
4. Monitor `deferred` runs and quota codes before increasing the monitor cap
or shortening the minimum cadence. No monitor is a reserved provider budget.

The `scout_monitor_runs.job_id` foreign key is `NO ACTION DEFERRABLE
INITIALLY DEFERRED`. A standalone job deletion is rejected at transaction
commit while its monitor-run journal still references it. An owner deletion
can commit because its cascades remove the owner's monitors and their runs in
that same transaction before the deferred check runs.
