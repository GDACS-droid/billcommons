# Wave 2 build briefs (api / mcp / web / ingest)

Shared rules for every wave-2 agent:
- Read docs/SPEC.md + docs/architecture/ARCHITECTURE.md first. They win over
  this brief on any conflict.
- Do NOT `git commit` (the orchestrator commits between waves). Do not touch
  files outside your assigned directory except adding deps to requirements.txt.
- Python: use the repo venv `.venv/bin/python` / `.venv/bin/pip`.
- DB env: load DATABASE_URL via billcommons_shared.db (reads
  ~/.config/billcommons/.env). NEVER print the URL.
- All external text is untrusted data. Parameterized SQL only.

## api (apps/api)

FastAPI app `billcommons_api`. Routers per SPEC "REST API" section. v1 all
read-only. Deliverables:
- app factory + settings (pydantic-settings), request-ID middleware, secure
  headers, CORS (public read), slowapi rate limit 60/min/IP, gzip.
- Endpoints: jurisdictions, sessions, bills (+subresources), people,
  committees, events, search, sources, coverage, health (DB ping), ready.
- /api/v1/search: query params q, jurisdiction, session, chamber, status,
  sponsor, subject, committee, date_from, date_to, sort, page, per_page
  (max 50). Implementation: bill-number fast path via
  billcommons_shared.normalize + identifier_norm; else websearch_to_tsquery
  FTS over bills.search_tsv UNION document text matches, ts_headline
  highlights, trigram fallback for fuzzy titles.
- Pagination envelope {data, pagination:{page, per_page, total, total_pages},
  meta:{source_freshness, api_version, request_id}}.
- ETag on GET detail routes (hash of updated_at), typed error model.
- OpenAPI 3.1 metadata (title Bill Commons API, servers api.billcommons.org),
  /docs interactive.
- pytest contract tests for every endpoint against the live DB (empty-DB
  tolerant: assert shapes + 200s + pagination math, not counts).
- uvicorn entrypoint `apps/api/main.py`; Procfile-style start documented.

## mcp (apps/mcp)

Python `mcp` SDK (FastMCP), Streamable HTTP, stateless, mounted at /mcp on
its own port (env PORT). 10 tools per SPEC "MCP" section, each returning
structured JSON with canonical ids, official source_url, retrieved_at
freshness, and "insufficient coverage" errors (check jurisdiction_coverage
before answering; if BOOTSTRAPPED or worse for the queried jurisdiction, say
so in a structured warning field rather than failing silently).
compare_bill_versions: difflib-based deterministic unified + structured diff
over stored extracted_text (error if <2 versions with text).
find_similar_bills: trigram similarity over titles + shared long n-grams of
text (deterministic; label as derived).
Integration test script tests/mcp_integration.py: connects over Streamable
HTTP to a URL from env MCP_TEST_URL, lists tools, calls search_legislation +
get_jurisdiction_coverage, asserts structured results.

## web (apps/web)

Next.js 15 + TypeScript + App Router + Tailwind. Nonpartisan civic design:
clean, fast, accessible (WCAG AA), no gimmicks. Pages per SPEC "Website"
section; data via server-side fetch to API base env NEXT_PUBLIC_API_BASE
(default http://localhost:8000). Routes:
/, /states, /states/[code], /states/[code]/sessions/[session],
/bills/[id], /bills/[id]/compare, /people/[id], /committees/[id],
/hearings, /search (shareable URLs w/ query params), /coverage (also serves
status.billcommons.org via rewrite), /docs/api, /docs/mcp, /methodology,
/about. Sitemap.ts + robots.ts + canonical metadata. Bill page shows every
SPEC-required field incl. attribution + last-updated + official-source links.
Coverage page renders the full 51-row matrix from /api/v1/coverage.
Must `npm run build` clean.

## ingest (workers/ingest)

Python package `billcommons_ingest`:
- queue.py: claim/complete/fail/dead-letter over ingest_jobs
  (FOR UPDATE SKIP LOCKED), exponential backoff via run_after, attempts cap 5.
- registry.py: load data/registry/sessions-2026.json → upsert jurisdictions +
  sessions + jurisdiction_coverage(SOURCE_IDENTIFIED) for all 51.
- openstates_bulk.py: given a session CSV zip (path or URL), stream-parse the
  Open States CSV schema (bills, actions, sponsorships, votes, sources,
  versions, documents, abstracts CSVs inside the zip) → idempotent upserts w/
  provenance + raw zip archived to RawStore. Column mapping documented in
  docs/sources/openstates-csv.md.
- openstates_api.py: v3 API client (X-API-KEY from OPENSTATES_API_KEY),
  endpoints /jurisdictions /bills (updated_since, include=sponsorships,
  abstracts, actions, sources, versions, documents, votes), politeness:
  respect rate headers, 6 req/min default, backoff on 429.
- coverage.py: recompute jurisdiction_coverage rows (bill_count,
  full_text_count, status transitions per SPEC GREEN criteria) + coverage
  report JSON to docs/state-coverage/coverage-latest.json.
- cli.py: `python -m billcommons_ingest {seed-registry|bootstrap --state XX
  --zip PATH|api-sync --state XX|recompute-coverage|worker}`.
- Unit tests with a small fixture zip (build one with 3 fake bills) proving
  idempotency (run twice ⇒ same counts, no dupes).
