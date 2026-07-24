# Coverage methodology

How Bill Commons decides — honestly, not aspirationally — whether a
jurisdiction's legislative data is trustworthy enough to call "covered."
This mirrors `docs/SPEC.md`'s "Coverage state machine + GREEN criteria" and
documents the actual code (`workers/ingest/billcommons_ingest/coverage.py`,
`validation.py`) that implements it.

## The state machine

```
NOT_STARTED → SOURCE_IDENTIFIED → BOOTSTRAPPED → METADATA_SEARCHABLE
            → FULL_TEXT_SEARCHABLE → VALIDATING → GREEN | DEGRADED | BLOCKED
```

One row per `(jurisdiction, session)` pair in `jurisdiction_coverage`.
Transitions come from two independent passes, run separately, each with a
clearly scoped authority:

### Count-driven transitions (`coverage.recompute_and_write`, purely mechanical)

Run via `python -m billcommons_ingest recompute-coverage`. Reads live
`bills`/`bill_documents` counts and can only push a row **forward** through
the count-based part of the ladder:

| From | To | Trigger |
|---|---|---|
| `SOURCE_IDENTIFIED` | `BOOTSTRAPPED` | `bill_count > 0` |
| `BOOTSTRAPPED` | `METADATA_SEARCHABLE` | (same condition — bootstrap having run at all implies bills have identifier+title, which are NOT NULL columns) |
| `METADATA_SEARCHABLE` | `FULL_TEXT_SEARCHABLE` | `full_text_count > 0` |

This function **never** sets `GREEN`/`DEGRADED`/`BLOCKED`/`VALIDATING` and
never regresses a row already past `FULL_TEXT_SEARCHABLE` — those
transitions require a real validation pass, which is a categorically
different kind of evidence (an independent check, not just "did rows get
inserted").

### Validation-driven transitions (`validation.apply_validation_result`)

Run via `python -m billcommons_ingest validate --state XX [--all]`. Samples
≥5 (default) random bills and checks each against sources the write path
did **not** just populate from:

1. **`structural`** — internal consistency of the already-ingested row
   (identifier/title/session/source_url present, `latest_action_date`
   matches the real `max(bill_actions.action_date)`). Catches a different
   failure mode than "did the ingest write path run" — a subtly wrong value
   slipping through, not just absence.
2. **`search_retrieval`** — a real HTTP round-trip to the **deployed
   production** `https://api.billcommons.org/api/v1/search` endpoint,
   proving the search index actually matches the DB, not just that a row
   exists somewhere. Checks both bill-number lookup and a keyword probe
   built from a distinctive word in the title.
3. **`cross_source`** — a robots-aware, rate-limited fetch of the bill's
   own official `source_url`, checking a surface form of its identifier
   (`"HB 123"`, `"HB123"`, `"H.B. 123"`, etc.) appears on the page. Proves
   the stored link is live and actually names this bill, not a stale or
   wrong URL.

Each leg's outcome is one of `pass` / `fail` / `unverifiable` (network
error — excluded from the pass-rate denominator, not counted as a failure)
/ `skipped_robots` (an honored robots.txt disallow — also excluded, since a
compliant refusal to fetch isn't the bill's fault). `pass_rate` is the
fraction of **checkable** legs (excludes the two non-checkable outcomes)
that passed; it's `None` (not 0%, not 100%) if literally nothing was
checkable in a run — a distinct "we learned nothing" state.

**Threshold:** `GREEN_PASS_RATE_THRESHOLD = 0.80`. Below it →
`DEGRADED` (unless already `BLOCKED`, which is never auto-changed by a
validation pass) and `known_gaps` records the pass rate. At/above it, the
ceiling depends on full-text coverage:

## GREEN's honest ceiling

**GREEN requires full text for at least
`GREEN_FULLTEXT_COVERAGE_THRESHOLD = 0.80` of the bills whose text is
actually obtainable, in addition to a passing validation sample.** A
jurisdiction can have a perfectly clean validation run (100% pass rate on
every checkable leg) and still be capped at `VALIDATING` — never `GREEN` —
while its full-text crawl is still filling in.

The denominator is `full_text_available_count`, **not** `bill_count`. It
counts bills with at least one document whose text we could still
legitimately obtain, so two categories never count against a jurisdiction:

* bills whose source publishes no document at all, and
* documents that are terminally unfetchable — `robots_disallowed`,
  `scanned_pdf_no_text`, and the other `fulltext.TERMINAL_STATUSES`.

```python
available = coverage.full_text_available_count
if available is None:                 # never recomputed -> unknown, don't promote
    ...
elif available == 0:                   # nothing obtainable -> criterion #5 vacuous
    coverage.status = "GREEN"
    coverage.known_gaps = "no full text obtainable from source ..."
elif coverage.full_text_count >= GREEN_FULLTEXT_COVERAGE_THRESHOLD * available:
    coverage.status = "GREEN"
else:                                  # crawl in progress -- NOT a fault
    coverage.status = "VALIDATING"
```

### Why a ratio, and not `full_text_count > 0`

The rule was previously `full_text_count > 0`. That satisfied the literal
words of SPEC criterion #5 while letting a jurisdiction holding text for
1–2% of its obtainable bills wear a GREEN badge — on 2026-07-24, 19
jurisdictions were GREEN at a median of **2%** full-text coverage (e.g. PA
at 37/4,876 bills). Criterion #5 says "**wherever** technically available",
which is a ratio, not an existence check. The threshold is what makes the
badge mean what the SPEC says.

Three deliberate consequences:

1. **An existing GREEN is demoted** when measured below the threshold.
   Otherwise the pre-existing badges would outlive the facts behind them.
2. **Falling short reads as `VALIDATING`, not `DEGRADED`.** A crawl still in
   progress is not a fault, and `known_gaps` states the actual ratio
   (`"full text for 37/4876 obtainable bills (0.8%) ..."`).
3. **`full_text_available_count` is NULLable**, and NULL blocks promotion.
   NULL means "not yet measured"; 0 means "nothing obtainable". Backfilling
   0 in migration `0003` would have read as the vacuous-criterion case and
   handed every un-recomputed row a free GREEN.

A jurisdiction whose sources are entirely robots-blocked (the DC/TN shape)
lands at `available == 0`, where criterion #5 is vacuously satisfied. It may
be GREEN, but `known_gaps` must say the text isn't obtainable and the bills
are metadata-searchable only — GREEN must never imply full-text search a
user will not actually get.

## What "not provided by source" means

Several fields are legitimately `NULL` for reasons that have nothing to do
with ingestion failing — SPEC's rule is "capture when available, never
fabricate; missing = null," and the bulk-CSV adapter documents exactly
which fields fall into this bucket (see
`docs/sources/openstates-csv.md`, "Fields NEVER populated from this
source"):

- `bills.description`, `.status`, `.status_date` — the Open States bulk
  CSV bill export simply doesn't carry these columns (they exist in Open
  States' richer v3 JSON/API, not the CSV bulk export); a future T1/T3
  adapter may fill them in, but their absence today is not a data-quality
  bug in this pipeline.
- `bills.introduced_date` — derived only when an `introduction`-classified
  action exists in `bill_actions`; otherwise `NULL`, never guessed.
- `people.*` FK resolution on sponsors/voters — the bulk CSV has no
  legislator-roster file, so sponsor/voter identity is captured as free
  text (`name`/`voter_name`) with `person_id` populated only when that
  person happens to already exist from another source. This is why
  MCP tools and API responses distinguish `name` (always present) from
  `person_id` (often `null`).
- Full text (`bill_documents.extracted_text`) — `NULL` until the
  full-text pipeline runs for that document, and can stay `NULL`
  permanently for a `robots_disallowed` or `scanned_pdf_no_text` outcome
  (see the source-failure runbook) — those are recorded honestly via
  `bill_documents.license_note` (`fulltext_status=...`), not silently
  hidden.

None of the above blocks a jurisdiction from reaching `BOOTSTRAPPED` /
`METADATA_SEARCHABLE` — those only require identifier + title + session +
source_url, which the bulk CSV always supplies for every ingested bill.

## Refresh targets (feeding into "is this coverage row fresh")

Per SPEC and `scheduler.py` (see the ingestion runbook for the full detail):
active/special sessions refresh every 30 min, year-round hourly, recently
adjourned daily, dormant weekly. `jurisdiction_coverage.last_success_at` /
`last_attempt_at` reflect when a recompute/bootstrap pass last actually
ran for that row — the coverage report's `last_update` field (surfaced at
`/api/v1/coverage`) is this timestamp, so a coverage row with a stale
`last_update` relative to its expected cadence is itself a signal something
needs attention (see the source-failure runbook).

## Attribution

Every jurisdiction's `source_name` in `jurisdiction_coverage`/`sessions`/
`bills` currently reads `"Open States / Plural"` (T2 in the ingestion-tiers
table) or `"sessions-2026-registry"` (the NCSL/Ballotpedia-aggregated
session-calendar registry used to seed which session is "current" — see
`registry.py`). Official state/federal legislative sites are the ultimate
origin of the underlying bill text/actions/votes (public domain under U.S.
law in general — see `NOTICE`); Open States aggregates and republishes it,
preserving the official `source_url` on every imported entity so a reader
can always verify against the primary source directly. `NOTICE` documents
the full attribution chain (Open States/Plural, official state/federal
sites, optional LegiScan under CC BY 4.0 if ever enabled) — nothing in this
methodology changes that; it only governs when a jurisdiction is
*labeled* GREEN, not who gets attribution credit for its data (attribution
applies from the moment any data from a source is imported, regardless of
coverage status).
