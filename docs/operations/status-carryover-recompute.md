# Post-deploy: recompute status after the carryover/substitution fix

`status.py`'s `derive_status` and `cli.py`'s `recompute_status_for_bills` no
longer trust a stale carryover `DIED IN [CHAMBER]` action over later progress
(R1), can produce `passed_both` when two chambers each recorded a `passage`
action (R2), and propagate a substituted bill's status from its survivor
(R3). None of this is retroactive on its own -- `bills.status` only moves when
something re-derives it.

Run the backfill once after deploying this change. Scope to the jurisdiction
the fix targets first (NY, the carryover/substitution source), then the rest:

```bash
billcommons-ingest recompute-status --jurisdiction NY
billcommons-ingest recompute-status
```

Run each command until it reports 0 changed rows (normally twice). A
substituted bill whose survivor sits in a later chunk reads the survivor's
pre-recompute status on the first pass; the second pass converges.

Survivor lookup also tries the identifier with a trailing print/amendment
letter stripped (e.g. `A 10008C` -> `A 10008`), NY only -- elsewhere (FL's
`HB 1A`, CA's `AB 1X`) that trailing letter is part of the bill's identity,
not a print version. The exact form is always resolved first, across both
the in-progress chunk and the database, before the stripped fallback is
tried at all -- so which chunk a survivor happens to land in never changes
which bill wins.

Detection is case-insensitive and tolerates NJ's mixed-case `Substituted by
A1516 (1R)` wording and its trailing `(1R)`/`(2R)` reprint marker, not just
NY's `SUBSTITUTED BY A10008C` shape -- run `--jurisdiction NJ` too when
backfilling for the first time.

NJ's enactment wording (`Approved P.L.2025, c.34.`, distinct from
"signed/approved BY the Governor") and passage wording (`Passed by the
Senate (40-0)`, `Passed Assembly (Passed Both Houses) (75-0-0)`) are also
recognized -- re-run `recompute-status --jurisdiction NJ` after deploying
that change to pick up the `enacted`/`passed_both`/`passed_one_chamber`
backfill.

`--jurisdiction` filters by abbreviation and is case-insensitive. Omit it (or
run a second time with no flag) to sweep every bill. The command is
idempotent and safe to re-run: only rows whose derived status actually
changes are written, and this path does not stamp `updated_at` (see
`recompute_status_for_bills`'s docstring for why), so re-running does not
manufacture a wave of `/changes` events.

Every substitution target resolved above is also persisted, not just used to
derive a status: a `related_bills` row with `relation_type = 'substituted-by'`
is written for the substituted print, with `related_bill_id` set when the
survivor resolved and left `NULL` (with `related_identifier` holding the raw
target) when it did not. Without it, a substituted print had a correct status
but no persisted link to its survivor. This relation is consumed by the API's
substitution links (api-identifier-search-replaces branch) -- and it is ALSO
read back by this same ingest path: `recompute_status_for_bills`'s survivor
resolution loop treats an existing `substituted-by` row as the same signal as
the in-text form, so a bill whose only substitution evidence is a persisted
`related_bills` row (no in-text match this pass, or a prior run's resolved
`related_bill_id`) still resolves correctly on every later pass. The upsert is
keyed on `(bill_id,
related_identifier, relation_type)`, so a re-run never duplicates the row, and
a later pass that resolves a previously-`NULL` survivor updates that same row
in place. If a later pass derives a DIFFERENT target for the same bill (a
corrected print, re-parsed text, etc.), the stale `related_identifier` row is
deleted and replaced -- a bill can only ever be substituted by one survivor.
Reconciliation only ever runs for a bill whose target came from its OWN
action text in this pass. Existing `related_bills` rows are read as lookup
evidence for status derivation (a resolved row still drives the survivor's
status) but are never written back or deleted on their own account, so a
bill with no substitution text this pass keeps every stored row untouched.
Among duplicate rows for the same identifier, the resolved one is kept.
The `DONE` summary line reports the upsert count as `related_upserted=N` and
the count of stale rows deleted during reconciliation as
`related_removed=M`.

`related_bills` has no database-level unique constraint on
`(bill_id, related_identifier, relation_type)` -- the dedup above is enforced
entirely in application code (`recompute_status_for_bills`, mirroring the same
key `openstates_bulk.py` already uses for this table). That means the backfill
sweep (`recompute-status`, run wholesale over the whole corpus) must not run
CONCURRENTLY with the sync worker's own per-cycle `recompute_status_for_bills`
call: two processes racing on the same bill's `substituted-by` row can each
pass the "does this already exist" check before either commits and insert a
duplicate. If that happens, the one-line cleanup is:

```sql
DELETE FROM related_bills a USING related_bills b
 WHERE a.bill_id = b.bill_id
   AND a.relation_type = 'substituted-by'
   AND b.relation_type = 'substituted-by'
   AND a.related_identifier IS NOT DISTINCT FROM b.related_identifier
   AND (
     -- Keep the row that already has a resolved related_bill_id over one
     -- that is still NULL -- an unconditional "keep the highest id" can
     -- throw away the resolved row and put the survivor link back to
     -- unresolved. Only once neither/both rows are resolved does id break
     -- the tie.
     (a.related_bill_id IS NULL AND b.related_bill_id IS NOT NULL)
     OR (
       (a.related_bill_id IS NOT NULL) = (b.related_bill_id IS NOT NULL)
       AND a.id < b.id
     )
   );
```

Retraction is out of scope: if a bill stops being reported as substituted (its
action text no longer matches, or it drops out of every recompute chunk), the
existing `related_bills` row is left in place rather than deleted. Only a
CHANGE in target is reconciled, never a bill going from "substituted" to "not
substituted" -- there is currently no code path that removes a `substituted-by`
row once the text signal that produced it disappears.
