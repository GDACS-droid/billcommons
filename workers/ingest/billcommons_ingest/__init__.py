"""Bill Commons ingestion workers.

Sub-modules:
    queue           Postgres-backed job queue (ingest_jobs), SKIP LOCKED claim.
    registry        Seed jurisdictions/sessions/coverage from data/registry.
    openstates_bulk Session bulk CSV zip -> idempotent DB upserts.
    openstates_api  Open States v3 API client (X-API-KEY).
    coverage        Recompute jurisdiction_coverage + write coverage-latest.json.
    cli             `python -m billcommons_ingest {subcommand}` entrypoint.
"""
