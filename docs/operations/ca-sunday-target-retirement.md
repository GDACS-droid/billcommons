# California Sunday delta target — retirement plan, 2026-09-12

## Evidence and local correction

The latest failed CA action observation is
`a6427588-93a3-470a-92c6-e6d64f67fae3`, captured September 11 at 06:28:38 UTC.
Its structured failure is `capture / http_status_unexpected / 404`, for
`https://downloads.leginfo.legislature.ca.gov/pubinfo_Sun.zip`. There are no
retained archive bytes to parse for this failure.

California's [publisher README](https://downloads.leginfo.legislature.ca.gov/pubinfo_Readme.pdf)
lists small delta archives Monday through Saturday and a different full-session
snapshot family for all seven days. The September 12
[directory](https://downloads.leginfo.legislature.ca.gov/) contains those six
small deltas and `pubinfo_daily_Sun.zip`, but no `pubinfo_Sun.zip`.
The full Sunday snapshot is about 1.1 GB and is outside this bounded delta
adapter's contract; substituting that URL is not a safe repair.

`official_worker --seed-ca` now registers only the six supported delta URLs.
It leaves any legacy Sunday target and its operational state untouched. The
URL/day parser and replay compatibility still accept historical Sunday values;
there is no planned removal of retained-evidence compatibility.

Local source evidence is retained under
`/home/alberto/.local/share/billcommons/reliability-release-20260908/`:
`ca-observations-readonly-20260912T0614Z.json`,
`ca-download-directory-20260912.html`, and its SHA-bound JSON companion.

The focused official-worker and CA action adapter checks passed 33 tests on an
owned disposable PostgreSQL 16 cluster, including six-target idempotent
registration and preservation of an existing Sunday target's ID, enabled
state, schedule and failure count. Log:
`/tmp/bc_ca_registration_root_pg_20260912.log`. The cluster was dropped.

## Proposed production action — not executed

Retire only target `4127f90f-63fc-47b7-b48e-ad0ed078c3e6` by setting `enabled`
to false. Preserve the row, its scope, schedule, backoff counters, observations,
raw evidence and reconciliations. This removes an unsupported polling endpoint;
it does not establish Sunday coverage or make California semantically current.
The public overview must continue to show the disabled target and its history.

Before applying, resolve the outstanding production migration/operator
coordination question. Obtain a fresh read of the target and observation above,
check the publisher directory still supports this diagnosis, and confirm no
active observation owns this target. The actual observer claims a target with
`FOR UPDATE SKIP LOCKED` and holds that row lock through its observation
transaction. Acquire the same row lock with a bounded timeout; if an observation
owns it, abort and let that observation finish. Once this transaction holds the
row lock, no competing observer can claim it, and setting `enabled=false`
prevents claims after commit. No worker termination is needed for this one row.

The mutation must be one short transaction with a bounded lock timeout. Require
the exact UUID, CA jurisdiction, `ca_official_actions` adapter, exact Sunday
delta URL, the expected scope `{"day":"Sun","sessions":["20252026 regular","special1"]}`,
and current `enabled=true`. Lock and compare the row before changing it; abort
on a mismatch or lock timeout. Record before/after state and require exactly
one changed row. Do not reset `next_check_at` or `consecutive_failures`.

After commit, verify the public overview reports that target disabled, all six
supported CA targets retain their previous settings, and historical observation
reads still work. No production API response is claimed here.

Rollback, only if a source-contract review establishes that re-enabling is
appropriate, restores the saved `enabled` value for the same unchanged target.
It must not overwrite newer operator changes or alter failure history. A code
rollback alone does not re-enable a retired target; rerunning an older
`--seed-ca --enable-seeded` would, so avoid that command during rollback.
