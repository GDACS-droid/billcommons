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

`--jurisdiction` filters by abbreviation and is case-insensitive. Omit it (or
run a second time with no flag) to sweep every bill. The command is
idempotent and safe to re-run: only rows whose derived status actually
changes are written, and this path does not stamp `updated_at` (see
`recompute_status_for_bills`'s docstring for why), so re-running does not
manufacture a wave of `/changes` events.
