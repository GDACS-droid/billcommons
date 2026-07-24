# Bill Commons — Locked Architecture (v1, 2026-07-23)

Mission: public, open-source legislative search covering the current session/biennium
for all 50 states + DC. Web + REST API + MCP (Streamable HTTP) + status/coverage.
Public infrastructure first; no paywall on ordinary search or reasonable API use.

## Locked decisions

| Concern | Decision | Notes |
|---|---|---|
| Language split | Python 3.12 for api/mcp/workers; TypeScript/Next.js for web | one Python package tree shared via `packages/schema` |
| API | FastAPI, `/api/v1`, OpenAPI 3.1 at `/docs` | uvicorn; ETags; request IDs; 60 rpm/IP public tier (slowapi) |
| MCP | official `mcp` Python SDK, Streamable HTTP mounted at `/mcp` | separate Railway service (own process, shares DB layer) |
| DB | Railway managed PostgreSQL 16 | extensions: pg_trgm, unaccent (created in migration 0001) |
| Search | Postgres FTS (`tsvector` generated columns) + trigram; normalized bill-number column | no external search engine in v1; pgvector deferred |
| ORM/migrations | SQLAlchemy 2.0 + Alembic | |
| Raw source storage | `RawStore` interface → v1 backend: Railway volume (filesystem, sha256-addressed); S3 backend stub for later | spec prefers S3; volume is the v1 tradeoff, documented limitation |
| Queue | Postgres-backed job table (SELECT ... FOR UPDATE SKIP LOCKED) | no Redis in v1 |
| Ingestion tiers | T1 official adapters (later, per-state) · T2 Open States bulk CSV + v3 API (bootstrap + incremental) · T3 LegiScan (optional, key-gated) · T4 compliant direct scrape fallback | Open States is the 51-jurisdiction bootstrap |
| Deploy | Railway: api, mcp, worker (+ cron), Postgres · Vercel: web | domains: billcommons.org (web), api./mcp./status.billcommons.org |
| Status page | status.billcommons.org = route on web app reading `/api/v1/coverage` | |
| License | Apache-2.0 for original code; Open States data attribution preserved; NO GPL scraper code vendored into this repo (invoke as external process only if ever used) | |
| Tests | pytest (unit + API contract), Playwright (web), MCP integration tests against deployed endpoint | |

## Monorepo layout

```
apps/web        Next.js 15 (App Router, TS)
apps/api        FastAPI (routers per resource)
apps/mcp        MCP server (Streamable HTTP)
workers/ingest  ingestion workers + job queue + adapters/
packages/schema SQLAlchemy models + Alembic migrations (single source of truth)
packages/shared shared Python utils (bill-number normalization, rawstore, http client w/ rate limits)
packages/source-registry  per-jurisdiction source registry (data + loader)
packages/search search SQL builders / query parsing
infra/docker    Dockerfiles + docker-compose.yml (local stack)
infra/deployment Railway/Vercel configs, DNS runbook
docs/           architecture, api, sources, operations, state-coverage
data/registry   sessions-2026.json, sources.json (machine-readable registry)
```

## Canonical data model (packages/schema)

jurisdictions, legislative_bodies, sessions, bills, bill_identifiers, bill_versions,
bill_documents, bill_actions, bill_subjects, people, organizations, committees,
sponsorships, vote_events, vote_records, legislative_events (hearings),
related_bills, source_records, ingestion_runs, validation_runs,
jurisdiction_coverage, search_documents (materialized denormalized search rows),
ingest_jobs (queue).

Provenance columns on every imported entity: source_name, source_url, upstream_id,
retrieved_at, upstream_updated_at, raw_ref (rawstore key), checksum, parser_version,
license_note. Ingestion is idempotent: upsert on (jurisdiction, session, upstream_id)
natural keys; unchanged checksum ⇒ no write.

Bill-number normalization: `normalize_bill_number("H.B. 123") == "HB 123"`; store
`identifier` (as-published) + `identifier_norm` (uppercase, no punctuation, single
space) + trigram index on both.

## Coverage state machine

NOT_STARTED → SOURCE_IDENTIFIED → BOOTSTRAPPED → METADATA_SEARCHABLE →
FULL_TEXT_SEARCHABLE → VALIDATING → GREEN | DEGRADED | BLOCKED
(per-jurisdiction row in jurisdiction_coverage; GREEN criteria per docs/SPEC.md)

## Vertical slice definition (phase 1 gate)

One jurisdiction (NC) end-to-end: bulk bootstrap → normalized DB → /api/v1/search
returns "HB 123" style lookups → bill page renders on web → MCP search_legislation
returns cited results → coverage row METADATA_SEARCHABLE. Then fan out to 51.

## Refresh policy (worker cron)

active/special: 30 min · year-round: hourly · recently adjourned: daily ·
dormant: weekly session-status check · calendars: daily. Conditional requests,
backoff, circuit breakers, dead-letter table, bill-count-drop alerts.
