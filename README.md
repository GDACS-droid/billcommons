# Bill Commons

Bill Commons is a public, open-source legislative search platform covering the
current session/biennium for all 50 U.S. states plus DC. It provides a web
search UI, a REST API, an MCP (Model Context Protocol) server, and a public
status/coverage page. Public infrastructure first: no paywall on ordinary
search or reasonable API use.

See [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md)
for the locked architecture and data model.

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

### Database

Set `DATABASE_URL` in your environment (or in `~/.config/billcommons/.env`,
which is read as a fallback and is never committed):

```
DATABASE_URL=postgresql://user:password@host:port/dbname
```

Run migrations:

```bash
cd packages/schema
../../.venv/bin/alembic upgrade head
```

### Tests

```bash
.venv/bin/pytest packages/shared/tests
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

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for data attribution
(Open States / Plural Policy, public-domain legislative data).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## Security

See [SECURITY.md](SECURITY.md) for responsible disclosure.
