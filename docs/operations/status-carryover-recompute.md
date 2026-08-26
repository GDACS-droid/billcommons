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
