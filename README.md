# Bill Commons

Bill Commons is a public, open-source legislative search platform covering the
current session/biennium for all 50 U.S. states plus DC. It provides a web
search UI, a REST API, an MCP (Model Context Protocol) server, and a public
status/coverage page. Public infrastructure first: no paywall on ordinary
search or reasonable API use.

See [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md)
for the locked architecture and data model.

## Production

* Web: https://billcommons.org (search UI + public status/coverage page at
  https://status.billcommons.org)
* API: https://api.billcommons.org/api/v1 — interactive OpenAPI docs at
  https://api.billcommons.org/docs
* MCP (Streamable HTTP): https://mcp.billcommons.org/mcp

Hosted on Railway (project `billcommons`: `api`, `mcp`, `worker` services +
managed Postgres) and Vercel (project `billcommons-web`). See
[`docs/operations/deployment-runbook.md`](docs/operations/deployment-runbook.md)
for the full deploy/rollback procedure.

## Use with Claude (or any MCP client)

The hosted MCP server gives AI assistants direct access to all 209k+ bills —
no API key, no setup beyond one command:

```bash
claude mcp add bill-commons --transport http https://mcp.billcommons.org/mcp
```

Claude Desktop: **Settings → Connectors → Add custom connector** with URL
`https://mcp.billcommons.org/mcp`. Cursor and other clients: add the same URL
as a Streamable HTTP server in `mcp.json`.

Ten tools including `search_legislation`, `get_bill_record`,
`compare_bill_versions`, and `trace_legislative_history`. Full walkthrough
(including REST recipes for agents without MCP):
https://billcommons.org/docs/agents

## Architecture at a glance

```
                    ┌─────────────┐
   users ─────────▶ │  apps/web   │  Next.js, billcommons.org
                    │ (Vercel)    │  status.billcommons.org (rewrite → /coverage)
                    └──────┬──────┘
                           │ HTTPS (NEXT_PUBLIC_API_BASE)
                           ▼
                    ┌─────────────┐        ┌──────────────┐
                    │  apps/api   │◀──────▶│  apps/mcp    │  Streamable HTTP
                    │  FastAPI    │        │  10 MCP tools│  mcp.billcommons.org
                    │ /api/v1     │        └──────────────┘
                    │ api.billcommons.org
                    └──────┬──────┘
                           │ reads (SQLAlchemy)
                           ▼
                    ┌─────────────────────────────┐
                    │   Postgres 16 (Railway)      │
                    │  jurisdictions, sessions,     │
                    │  bills, actions, sponsorships,│
                    │  votes, ingest_jobs, coverage  │
                    └──────────────▲──────────────┘
                                   │ writes (idempotent upserts)
                    ┌──────────────┴──────────────┐
                    │  workers/ingest (worker svc)  │
                    │  autoboot: seed → bootstrap →│
                    │  schedule-refresh → job loop  │
                    │  sources: Open States bulk CSV│
                    │  (T2 bootstrap) + v3 API (T2  │
                    │  incremental, OPENSTATES_API_ │
                    │  KEY) + full-text fetcher     │
                    └──────────────┬──────────────┘
                                   ▼
                    RawStore (filesystem, RAWSTORE_ROOT
                    volume in prod) — sha256-addressed
                    raw payload archive
```

`packages/schema` (SQLAlchemy models + Alembic) is the single source of
truth every other package/app imports from — no service owns its own copy
of the data model.

## Monorepo layout

```
apps/web        Next.js 15 (App Router, TS) — search UI + status page
apps/api        FastAPI — REST API (/api/v1)
apps/mcp        MCP server (Streamable HTTP, mounted at /mcp)
workers/ingest  Ingestion workers, job queue, per-source adapters
packages/schema SQLAlchemy models + Alembic migrations (single source of truth)
packages/shared Shared Python utils: bill-number normalization, rawstore, http client
packages/source-registry  Per-jurisdiction source registry (data + loader)
packages/search Search SQL builders / query parsing
infra/docker    Dockerfiles + docker-compose.yml (local stack)
infra/deployment Railway/Vercel configs, DNS runbook
docs/           Architecture, API, sources, operations, state-coverage docs
data/registry   Machine-readable registry (sessions, sources)
```

## Local development setup

### Prerequisites

* Python 3.12
* PostgreSQL 16 (with `pg_trgm`, `unaccent`, `pgcrypto` extensions available)
* Node.js 20+ (for `apps/web`)

### Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

This installs `packages/schema` and `packages/shared` as editable installs
(single source of truth for the data model + shared utils), plus the
worker package: `.venv/bin/pip install -e workers/ingest`. Install the API
package too if you're working on it: `.venv/bin/pip install -e apps/api`.

### Database

Set `DATABASE_URL` in your environment (or in `~/.config/billcommons/.env`,
which is read as a fallback and is never committed):

```
DATABASE_URL=postgresql://user:password@host:port/dbname
```

Requires Postgres 16 with the `pg_trgm` and `unaccent` extensions available
(created by migration `0001`). Run migrations:

```bash
cd packages/schema
../../.venv/bin/alembic upgrade head
```

### Seeding data locally

```bash
# Seed all 51 jurisdictions/sessions/coverage rows from the registry:
.venv/bin/python -m billcommons_ingest seed-registry

# Download a state's Open States bulk-CSV zip and ingest it (see
# docs/operations/ingestion-runbook.md for the full command reference):
python3 workers/ingest/download_bulk.py --only NC
.venv/bin/python -m billcommons_ingest bootstrap --state NC --zip data/bulkzips/NC_2025.zip
.venv/bin/python -m billcommons_ingest recompute-coverage
```

### Running the apps locally

```bash
# API (FastAPI, http://localhost:8000, docs at /docs)
.venv/bin/uvicorn main:app --app-dir apps/api --reload --port 8000

# MCP server (Streamable HTTP, http://localhost:8400/mcp by default)
.venv/bin/python apps/mcp/server.py

# Ingestion worker (long-running queue loop; runs schedule-refresh
# periodically inside the same process)
.venv/bin/python -m billcommons_ingest worker

# Web app (Next.js — separate from the Python stack)
cd apps/web
npm install
NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

### Tests

```bash
.venv/bin/pytest packages/shared/tests
.venv/bin/pytest workers/ingest/tests
.venv/bin/pytest apps/api/tests
```

### Running the stack locally with Docker

```bash
cd infra/docker
docker compose up --build
```

This brings up Postgres, the API, the ingestion worker, and the MCP server.
The web app (`apps/web`) is run separately via `npm run dev` during local
development (see `infra/docker/docker-compose.yml` for the placeholder
service definition).

## Documentation

* [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) — locked architecture + data model
* [`docs/SPEC.md`](docs/SPEC.md) — requirements digest / acceptance gate
* [`docs/operations/deployment-runbook.md`](docs/operations/deployment-runbook.md) — Railway/Vercel deploy, rollback, smoke checklist
* [`docs/operations/ingestion-runbook.md`](docs/operations/ingestion-runbook.md) — CLI reference, job queue, refresh cadence
* [`docs/operations/source-failure-runbook.md`](docs/operations/source-failure-runbook.md) — stale zips, 401/429, robots blocks
* [`docs/operations/backup-restore.md`](docs/operations/backup-restore.md) — pg_dump/restore, raw-data re-fetch
* [`docs/operations/add-a-jurisdiction.md`](docs/operations/add-a-jurisdiction.md) — onboarding a new territory/state
* [`docs/state-coverage/methodology.md`](docs/state-coverage/methodology.md) — coverage state machine, GREEN criteria
* [`docs/api/examples.md`](docs/api/examples.md) — curl/Python/JavaScript examples against the live API
* [`docs/sources/openstates-csv.md`](docs/sources/openstates-csv.md) — Open States bulk CSV column mapping

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for data attribution
(Open States / Plural Policy, public-domain legislative data).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## Security

See [SECURITY.md](SECURITY.md) for responsible disclosure.
