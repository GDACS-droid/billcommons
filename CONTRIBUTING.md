# Contributing to Bill Commons

Thanks for your interest in contributing. Bill Commons is public
infrastructure — we welcome bug reports, source adapters for new
jurisdictions, documentation fixes, and feature contributions.

## Ground rules

* Be respectful. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
* No GPL (or otherwise copyleft-incompatible) code may be vendored into this
  repository. If a compliant scraper or library is GPL-licensed, it must be
  invoked as an external process, never imported/linked directly. See
  `docs/architecture/ARCHITECTURE.md` for the locked license policy.
* All original code contributions are licensed under Apache-2.0 (see
  [LICENSE](LICENSE)), consistent with Section 5 of that license.
* Preserve data attribution: any code that ingests or republishes third-party
  legislative data (e.g., Open States/Plural Policy) must keep provenance
  columns populated and must not strip attribution required by NOTICE.

## Development setup

See the "Local development setup" section of [README.md](README.md).

## Making a change

1. Fork the repo and create a feature branch off `main`.
2. Keep changes scoped — one concern per pull request.
3. Add or update tests for any behavior change. Tests should encode *why*
   the behavior matters, not just assert current output.
4. Run the relevant test suite locally before opening a PR:
   ```bash
   .venv/bin/pytest packages/shared/tests
   ```
5. Open a pull request describing the change and its motivation.

## Schema changes

`packages/schema` is the single source of truth for the data model. Schema
changes require:

* An Alembic migration (never hand-edit the database directly).
* Matching SQLAlchemy model changes in the same PR.
* A note on any generated/tsvector columns affected, since these require
  raw DDL rather than ORM-level column definitions.

## Adding a new jurisdiction / source adapter

Per-jurisdiction ingestion adapters live in `workers/ingest/adapters/` with a
matching entry in `packages/source-registry`. Please include:

* The source tier (T1 official API, T2 Open States, T3 LegiScan, T4 scrape
  fallback) and a link to that source's terms of use / licensing.
* A `jurisdiction_coverage` row update reflecting the new coverage state.

## Reporting bugs / requesting features

Open a GitHub issue. For security vulnerabilities, do **not** open a public
issue — see [SECURITY.md](SECURITY.md).
