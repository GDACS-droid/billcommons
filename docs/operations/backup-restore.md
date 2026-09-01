# Backup & restore

Honest current state: **Bill Commons has no automated scheduled database
dump today.** This doc documents what Railway's managed Postgres provides,
a manual `pg_dump`/restore procedure, and how ingestion's public-source files
can be reconstructed. Scout evidence bytes live in PostgreSQL
`scout_raw_blobs`; they are part of the database backup/restore contract and
must not be described as a disposable cache.

## What Railway's managed Postgres provides

Railway's managed Postgres plugin takes automatic volume-level snapshots on
its own schedule/retention (visible under the Postgres service's Backups
tab in the Railway dashboard) and supports point-in-time restore through
the dashboard UI. This is Railway's platform-level safety net — it is not
something this repo configures or controls, and it is **not** a substitute
for an application-level `pg_dump` if you need a portable, inspectable, or
off-Railway copy of the data (e.g. before a risky migration, or to seed a
local dev DB with production-shaped data).

## Manual `pg_dump` backup

```bash
# Never echo DATABASE_URL to a shell history file or log — pull it from
# Railway directly into the command.
railway run --service api pg_dump "$DATABASE_URL" \
  --format=custom --no-owner --no-acl \
  --file="billcommons-$(date +%Y%m%d).dump"
```

(Or, if you already have `DATABASE_URL` exported locally for a one-off
task per the local-dev setup in the README: `pg_dump "$DATABASE_URL"
--format=custom --no-owner --no-acl --file=backup.dump` — same command,
just without the `railway run` wrapper.)

`--format=custom` produces a compressed, `pg_restore`-only file (not plain
SQL) — smaller and supports selective/parallel restore. `--no-owner
--no-acl` drop Railway-specific role/grant statements that won't exist
(and don't need to) in a restore target.

## Restore procedure (to a fresh DB)

1. **Provision a fresh Postgres** (a new Railway Postgres plugin, or any
   Postgres 16 instance with `pg_trgm`/`unaccent` extensions available —
   see `packages/schema/alembic` migration `0001` which creates them).
2. **Restore the dump**:
   ```bash
   pg_restore --format=custom --no-owner --no-acl \
     --dbname="$NEW_DATABASE_URL" \
     billcommons-YYYYMMDD.dump
   ```
3. **Stamp Alembic** so future migrations apply correctly against the
   restored DB (a `pg_dump --format=custom` restore already includes the
   `alembic_version` table's data, so this step is typically a no-op
   verification, not a real stamp — confirm before assuming):
   ```bash
   cd packages/schema
   DATABASE_URL="$NEW_DATABASE_URL" ../../.venv/bin/alembic current
   # if the table is somehow missing/empty (e.g. you restored a
   # schema-only dump), stamp it explicitly at the version matching the
   # dump's actual schema state:
   DATABASE_URL="$NEW_DATABASE_URL" ../../.venv/bin/alembic stamp head
   ```
4. **Point a service at it**: update `DATABASE_URL` (Railway variable or
   local `~/.config/billcommons/.env`) and restart the service.
5. **Sanity-check**: `GET /api/v1/health` and `/api/v1/ready` against the
   restored target, plus a spot search (`/api/v1/search?q=...`) to confirm
   the FTS/trigram indexes came through intact (they're part of the schema,
   not separately maintained — `pg_restore` recreates them from the dump's
   DDL).

## Re-fetching ingestion raw data if its filesystem volume is lost

Unlike the database, the ingestion file-based stores are **not** the
authoritative copy of anything — they're either public upstream mirrors or
a cache, so total loss is an inconvenience (re-download time), not a data
loss:

- **`data/bulkzips/*.zip`** — every file here is a straight download from a
  public `data.openstates.org` URL (see `data/registry/bulk-urls.json` for
  the exact URLs, and the source-failure runbook if that registry itself
  needs regenerating). Re-fetch any/all of them:
  ```bash
  python3 workers/ingest/download_bulk.py            # all states
  python3 workers/ingest/download_bulk.py --only NC,FL   # just these
  ```
  This script already skips files that exist and pass a zip-integrity
  check, so re-running after a partial loss only re-downloads what's
  actually missing/corrupt.
- **RawStore volume (`RAWSTORE_ROOT`, prod: `/data/rawstore` on the
  `worker` service)** — content-addressed (sha256-keyed) archive of every
  raw payload ingestion has fetched (bulk zips, full-text documents). If
  this volume is lost, nothing is unrecoverable: bulk zips re-download from
  the public URLs above, and full-text documents re-fetch the next time
  `enqueue-fulltext` + the worker loop process `fetch_text` jobs for any
  `bill_documents` row whose `extracted_text` is still populated in the DB
  but whose `raw_ref` now points at a missing file (the DB's
  `extracted_text` column is unaffected by losing the raw archive — only
  the ability to re-derive text with a different parser version, or to
  re-verify the original bytes, is lost until re-fetched).

Scout does not use that volume. Its bounded, content-addressed source payloads
are in `scout_raw_blobs` and are restored with the database. A restore drill
must verify at least one known blob's stored bytes still hash to its `sha256`
key before Scout is enabled.

## TODO — automated scheduled backups

There is currently **no cron/scheduled job** in this repo that runs
`pg_dump` on a schedule or ships a dump anywhere off-Railway. This is a
known gap, not a design decision. A future implementation should:
- Add a scheduled `pg_dump` (Railway cron service, or a job inside the
  existing `worker` service's loop) on a daily or weekly cadence.
- Ship the resulting dump to off-Railway storage (S3-compatible bucket —
  note `packages/shared` already anticipates an S3 `RawStore` backend for
  the same reason raw payloads eventually want off-platform storage; a
  Postgres dump target could live alongside it).
- Define and document a retention policy (e.g. 7 daily + 4 weekly).
- Add a periodic restore-drill (restore the latest dump to a scratch DB and
  run the smoke checklist from the deployment runbook against it) so backup
  validity is actually verified, not just assumed.
