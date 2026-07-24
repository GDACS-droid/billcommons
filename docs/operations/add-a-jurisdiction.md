# Adding a jurisdiction (e.g. a new territory like Puerto Rico)

Bill Commons' schema and registry are built for the current 51 (50 states +
DC), but `Jurisdiction.classification` is a free-text field and nothing in
the ingestion pipeline hardcodes "51" — the SPEC explicitly notes "schema
permits territories later." This walks through adding one, using PR as the
worked example. Nothing here requires a schema migration unless the new
jurisdiction needs a genuinely new *kind* of source adapter.

## 1. Registry JSON entry (`data/registry/sessions-2026.json`)

Add an entry to the top-level `jurisdictions[]` array, matching the shape
every existing entry uses (read a couple of existing entries first —
`registry.py`'s `seed_registry`/`_upsert_jurisdiction`/`_upsert_session`
read these exact keys):

```json
{
  "state_code": "PR",
  "jurisdiction_name": "Puerto Rico",
  "session_identifier": "2025-2026 Regular Session",
  "regular_or_special": "regular",
  "session_status": "active",
  "convened_at": "2025-01-13",
  "expected_adjournment": null,
  "source_url": "https://www.oslpr.org/",
  "notes": "Territory, not a state — added <date>"
}
```

Required keys (per `registry._upsert_jurisdiction`/`_upsert_session`):
`state_code`, `jurisdiction_name`, `session_identifier`. Everything else
(`regular_or_special`, `session_status`, `convened_at`,
`expected_adjournment`, `source_url`, `notes`) is read defensively (missing
→ `NULL`, never fabricated) but should be filled in honestly from an
authoritative source (NCSL, the territory's own legislative site) the same
way the existing 51 entries were sourced.

`seed_registry` sets `jurisdiction.classification` to `"state"` for every
`state_code != "DC"` and `"district"` for `DC` — a jurisdiction like PR that
is neither will need that one line in `registry.py`'s
`_upsert_jurisdiction` adjusted (e.g. `"territory"` for anything not in a
small state/DC set), since the current logic is a two-way branch, not
data-driven. This is the one place today that assumes the 51-jurisdiction
shape; note it if you hit it rather than silently forcing a
`"state"`/`"district"` mislabel.

## 2. Bulk zip mapping (`data/registry/bulk-urls.json`)

Check first whether Open States/Plural actually covers the new
jurisdiction at all — not every U.S. territory is in their bulk-CSV
coverage. If it is:

1. Follow the source-failure runbook's procedure to find/confirm the zip
   URL(s) for the jurisdiction on the (login-gated) session-CSV download
   page.
2. Add an entry under `jurisdictions` in `bulk-urls.json`:
   ```json
   "PR": {
     "current": [
       { "slug": "2025-2026", "display_name": "Puerto Rico 2025-2026", "url": "https://data.openstates.org/csv/latest/PR_..._csv_....zip", "kind": "regular" }
     ],
     "notes": ""
   }
   ```

If Open States does **not** cover it, this jurisdiction can't be
bulk-bootstrapped via T2 — it needs a genuine T1 (official territory
API/bulk export) or T4 (compliant direct scrape) adapter instead, which is
new-code work beyond registry/data entries (see ARCHITECTURE.md's
ingestion-tiers table); this walkthrough covers the T2 (Open-States-covered)
path, which is what every currently-bootstrapped jurisdiction uses.

## 3. Session seeding

```bash
.venv/bin/python -m billcommons_ingest seed-registry
```
Confirm the new jurisdiction/session/coverage rows landed:
```sql
SELECT * FROM jurisdictions WHERE abbreviation = 'PR';
SELECT * FROM sessions WHERE jurisdiction_id = (SELECT id FROM jurisdictions WHERE abbreviation='PR');
SELECT * FROM jurisdiction_coverage WHERE jurisdiction_id = (SELECT id FROM jurisdictions WHERE abbreviation='PR');
```
Status should be `SOURCE_IDENTIFIED` immediately after seeding.

## 4. Bootstrap

```bash
python3 workers/ingest/download_bulk.py --only PR   # downloads into data/bulkzips/
.venv/bin/python -m billcommons_ingest bootstrap --state PR --zip data/bulkzips/PR_....zip
```
Watch the printed session-resolution path (`exact identifier` / `fuzzy` /
`create`) and the created/updated/unchanged/warnings summary. Re-run
`recompute-coverage` afterward:
```bash
.venv/bin/python -m billcommons_ingest recompute-coverage
```
Status should now be `BOOTSTRAPPED`/`METADATA_SEARCHABLE` (per
`coverage._next_status_for_counts`, purely count-driven).

## 5. Validation

```bash
.venv/bin/python -m billcommons_ingest validate --state PR
```
This runs the three independent verification legs (structural,
search-retrieval against the **deployed** production API, and a
robots-aware cross-source fetch of each sampled bill's official
`source_url`) and records a `validation_runs` row. Since this hits the live
`https://api.billcommons.org/api/v1` search endpoint, run it only after
whatever DB you bootstrapped into has actually been deployed/synced to
production — otherwise the search-retrieval leg will correctly report
`fail` (the bill genuinely isn't in the deployed index yet), not because
anything is wrong with your bootstrap.

## 6. Coverage

After validation, `jurisdiction_coverage.status` will be `VALIDATING` (if
the pass rate is ≥80% but no full text yet) or `DEGRADED` (below 80%) —
never `GREEN` until the full-text pipeline (`enqueue-fulltext` + the worker
processing `fetch_text` jobs) has actually populated `full_text_count > 0`
for this jurisdiction too. See `docs/state-coverage/methodology.md` for the
full GREEN-criteria breakdown. Confirm the new row shows up in the public
matrix:
```bash
curl -s https://api.billcommons.org/api/v1/coverage | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print([r for r in d['data'] if r['jurisdiction_code']=='PR'])"
```

## Summary checklist

- [ ] `data/registry/sessions-2026.json` — new `jurisdictions[]` entry
- [ ] `registry.py`'s classification branch handles the new jurisdiction's
      `classification` correctly (state/district/territory) if it isn't a
      state or DC
- [ ] `data/registry/bulk-urls.json` — zip URL(s) added (or T1/T4 adapter
      scoped as separate work if Open States doesn't cover it)
- [ ] `seed-registry` run, rows confirmed
- [ ] Zip downloaded, `bootstrap` run, warnings reviewed
- [ ] `recompute-coverage` run
- [ ] `validate --state XX` run against a deployed environment, pass rate
      reviewed
- [ ] New row visible in `/api/v1/coverage` and
      `docs/state-coverage/coverage-latest.json`
