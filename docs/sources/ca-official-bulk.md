# California official bulk full-text source (Tier-1, confidence: HIGH)

## Why this adapter exists

California's live bill site, `leginfo.legislature.ca.gov`, publishes
`Disallow: /` in its robots.txt. Bill Commons' polite per-document
`fulltext.py` fetcher correctly honors that and dead-letters every CA
`bill_documents` row it tries as `fulltext_status=robots_disallowed` --
that is correct, ToS-respecting behavior, not a bug to route around by
ignoring robots.txt.

Instead, California separately publishes its own official bulk download of
the exact same bill text, intended for bulk consumers, at a DIFFERENT host
with no such restriction:

```
https://downloads.leginfo.legislature.ca.gov/
```

Confirmed 2026-07-24: `curl .../robots.txt` on that host returns a plain
`404 Not Found` -- i.e. no robots.txt exists there at all, which is
standard robots.txt semantics for "no restriction stated" (an absent
robots.txt is not an implicit disallow). This is the sanctioned Tier-1
source for CA full text; `ca_bulk_fulltext.py` implements the adapter.

## File listing at the downloads host

Plain Apache directory index (`Index of /`), confirmed files as of
2026-07-24:

| File | Contents | Size (2026-07-24) |
|---|---|---|
| `pubinfo_<odd-year>.zip` (e.g. `pubinfo_2025.zip`) | **ALL** data for that 2-year session (session years are odd -> even, e.g. 2025-2026) | ~1.0 GB |
| `pubinfo_Mon.zip` / `_Tue.zip` / ... `_Sat.zip` | Only the day's **new/changed records since the last extract**, and only a SUBSET of tables (confirmed by inspection: `BILL_ANALYSIS_TBL`, `BILL_HISTORY_TBL`, `BILL_TBL` + occasionally `COMMITTEE_AGENDA_TBL` -- notably **no `BILL_VERSION_TBL`**, so these alone cannot populate full text) | 100KB-2MB |
| `pubinfo_daily_Mon.zip` / ... `_Sun.zip` | **ALL current-session data except the `CODES_TBL` lookup table** -- i.e. a full current-session snapshot refreshed daily, same table set as the annual zip | ~800MB-1GB (same order of magnitude as the annual dump) |
| `pubinfo_1989.zip` ... `pubinfo_2023.zip` | Historical session archives (one per odd year back to 1989) | 16MB-1.2GB |
| `pubinfo_Readme.pdf` / `pubinfo_load.zip` | Documentation + a MySQL loader script bundle (`.sql`/`.bat` files) documenting the exact table DDL and column order used below | 362KB / 15KB |

**Adapter's default choice: the annual `pubinfo_<year>.zip`.** The small
weekly delta files (`pubinfo_<Day>.zip`) do not carry `BILL_VERSION_TBL` at
all, so they cannot be used alone to populate full text. The
`pubinfo_daily_<Day>.zip` full-snapshot files are viable future-refresh
candidates (same table set, refreshed daily) but are the same order of
magnitude in size as the annual dump, so there is no bandwidth advantage to
preferring them for a first run; `run_ca_fulltext()`'s default derives the
current odd session-year's annual zip URL. A future refresh cadence could
switch to `pubinfo_daily_<today's-weekday>.zip` for smaller day-to-day
deltas once the initial backfill is done, without any parser change (same
`BILL_VERSION_TBL.dat` + `.lob` layout).

## Zip internal structure

Tab-delimited `.dat` files (no header row) plus separate `.lob` files
holding large text/binary blobs referenced BY FILENAME from a column in
the corresponding `.dat` row (same convention `BILL_ANALYSIS_TBL` uses for
analysis document attachments, confirmed by inspecting a live sample:
`BILL_ANALYSIS_TBL.dat` row 1's last-but-3 field is literally
`BILL_ANALYSIS_TBL_1.lob`, the sibling file's exact name in the same zip).

### `BILL_VERSION_TBL.dat` — the table that carries full bill text

Column order, confirmed 2026-07-24 directly from CA's own MySQL loader
script (`pubinfo_load.zip` → `bill_version_tbl.sql`, a `LOAD DATA LOCAL
INFILE` statement — the authoritative column-order source, not guessed):

```
BILL_VERSION_ID, BILL_ID, VERSION_NUM, BILL_VERSION_ACTION_DATE,
BILL_VERSION_ACTION, REQUEST_NUM, SUBJECT, VOTE_REQUIRED, APPROPRIATION,
FISCAL_COMMITTEE, LOCAL_PROGRAM, SUBSTANTIVE_CHANGES, URGENCY, TAXLEVY,
@var1 (-> BILL_XML lob filename), ACTIVE_FLG, TRANS_UID, TRANS_UPDATE
```

The loader's own `SET BILL_XML=LOAD_FILE(concat('c:\pubinfo\', @var1))`
confirms the 15th tab-delimited field is NOT the XML text itself — it's
the filename of a sibling `.lob` file in the zip holding the actual bill
version's XML text. `capublic.sql`'s `CREATE TABLE bill_version_tbl`
confirms the destination column name (`bill_xml LONGTEXT`) and that the
whole file's content becomes that column's value once loaded.

Parsing rule (per this codebase's standing defensive-parsing convention):
a `.dat` line with fewer than the expected 18 tab-delimited fields is
skipped, never fabricated; a version row whose referenced `.lob` file is
absent from the zip is skipped (not an error) — same "missing entity ->
zero rows for it" convention as `openstates_bulk.py`.

### The join key: CA's own `BILL_ID` format

`BILL_ID` values look like `202520260AB1` (confirmed on live rows:
`202520260SB623`, `202520260ACR98`) — i.e.
`<session_year_start><session_year_end><session_num><chamber><measure_num>`.

Bill Commons' OWN `bill_documents.url` values for CA bills (populated by
`openstates_bulk.py` from Open States' own CA scrape) already carry this
exact CA `BILL_ID` as the `bill_id=` query-string parameter, e.g.:

```
http://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202520260AB1
https://leginfo.legislature.ca.gov/faces/billPdf.xhtml?bill_id=202520260SB876&version=20250SB87695AMD
```

Confirmed live 2026-07-24 by querying production `bill_documents` for
`jurisdiction.abbreviation = 'CA'`: sampled URLs' `bill_id=` params match
the `BILL_ID` format/values seen in the live `pubinfo_Fri.zip` sample's
`BILL_TBL.dat` (e.g. `202520260SB623`). **This is the adapter's join key**
— `ca_bulk_fulltext._ca_bill_id_from_url()` extracts it from
`bill_documents.url` via `urllib.parse.parse_qs`, and
`apply_ca_bulk_fulltext` looks it up directly in the parsed
`{BILL_ID: latest_version_text}` map. No fuzzy matching, no title
matching — a direct, already-present-in-our-own-data string key.

At the time of this recon, `bill_documents.url` also frequently carries a
per-version `version=` param (e.g. `20250SB87695AMD`) matching CA's
`BILL_VERSION_ID` format directly — a future refinement could join on that
instead of always taking "latest version" per bill, but the adapter's
first cut intentionally keeps it simple: one full-text blob per CA bill
(the latest version by `(bill_version_action_date, version_num)`), written
onto every one of that bill's `bill_documents` rows that lacks real text.

## Extraction

`BILL_XML` `.lob` files are (per a live sample check) genuine bill-text XML
(not OOXML/docx, unlike `BILL_ANALYSIS_TBL`'s `.lob` attachments, which
were confirmed to be `.docx` zip containers on inspection). Extraction
strips XML tags to plain text via the same tag-to-linebreak approach as
`fulltext.extract_text_from_xml` (each closing tag becomes a line break,
preserving section/paragraph structure best-effort for future diffing),
reimplemented locally in `ca_bulk_fulltext.py` rather than imported, to
keep this module's only dependency on `fulltext.py` be "compatible
convention," not a hard import (this module doesn't touch the ingest job
queue at all).

## Refresh approach

- **Initial backfill**: the annual `pubinfo_<current-session-year>.zip`
  (a full one-time ~1GB download), matched against every existing CA
  `bill_documents` row.
- **Ongoing refresh** (future work, not yet wired into a scheduler):
  `pubinfo_daily_<weekday>.zip` carries the same table set refreshed daily
  at much smaller marginal bandwidth than re-pulling the full annual file
  every time — same parser, different `zip_url`.
- Idempotent either way: `apply_ca_bulk_fulltext` compares
  `hashlib.sha256(text)` against the stored `bill_documents.checksum`
  before writing, so re-running the same (or a newer) zip against
  already-populated rows is a no-op unless the text actually changed (an
  amended bill version, a corrected re-export, etc).

## Licensing / attribution

California legislative bill text, as a work of the California state
government, is public record / not subject to copyright restriction for
this kind of civic redistribution use (California Government Code §6254.9
governs computer software, not legislative text; the Legislature's own
`downloads.leginfo.legislature.ca.gov` mirror is explicitly published FOR
bulk redistribution). Bill Commons still records real provenance on every
row this adapter touches rather than treating it as un-sourced:
`bill_documents.source_name = "CA leginfo official bulk"`,
`source_url = <the exact pubinfo_<...>.zip URL fetched>`,
`parser_version = "ca_bulk/1"` — so any downstream consumer of Bill
Commons' own data can trace CA full text back to this exact official
source and file, not to the (robots-blocked) live site URL alone.
