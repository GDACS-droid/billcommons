# Guarded API regression testing

The API/shared regression suite is PostgreSQL integration coverage. Some
files deliberately insert and later delete fixture bills, subscriptions,
webhooks, and search records. It must never be pointed at Railway or at the
ordinary local Bill Commons `DATABASE_URL`.

## Required target

Use a freshly migrated, explicitly disposable local database whose name ends
in `_test`. The test fixture refuses every other target before opening a
database connection. It requires both a local TCP host (`localhost`,
`127.0.0.1`, or `::1`) or the `/var/run/postgresql` Unix socket, and an
explicit destructive-test acknowledgement.

Create/migrate the database using a local Postgres role that you control; do
not put a production URL or password in shell history or test output:

```bash
createdb billcommons_regression_test
DATABASE_URL='postgresql:///billcommons_regression_test?host=/var/run/postgresql' \
PYTHONPATH=packages/schema:packages/shared \
  .venv/bin/alembic -c packages/schema/alembic.ini upgrade head
```

Then run the integration suite:

```bash
BILLCOMMONS_TEST_DATABASE_URL='postgresql:///billcommons_regression_test?host=/var/run/postgresql' \
BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1 \
PYTHONPATH=apps/api:packages/schema:packages/shared \
.venv/bin/python -m pytest -q packages/shared/tests apps/api/tests
```

The suite does not create a database, truncate unrelated rows, or relax
assertions. A database must already have migrations through the revision being
tested. It is safe to run test collection without a target:

```bash
PYTHONPATH=apps/api:packages/schema:packages/shared \
.venv/bin/python -m pytest --collect-only -q packages/shared/tests apps/api/tests
```

## Corpus contracts versus deterministic coverage

The following legacy files contain corpus-shaped assertions and are now an
explicit corpus-contract layer, not evidence that a source checkout can test
against a live deployment: `test_benchmark_deterministic.py`,
`test_changes_and_batch.py`, `test_search.py`, `test_sponsor_filter.py`,
`test_stats_and_topics.py`, `test_evidence_endpoint.py`, and sitemap/detail
tests that select an existing bill. Run them against a seeded local corpus.

`apps/api/tests/_regression_seed.py` is an automatic, provenance-scoped seed
for the guarded database. It covers bill ambiguity, session labels, bill
events, sponsorships, FTS/trigram, and mortality status distributions. It is
not a replacement for an optional production-corpus performance exercise; it
makes the contract suite deterministic. Do not skip or weaken those assertions
merely to make an empty database green.

The narrower Scout PostgreSQL suite has its own independent guard in
`apps/api/tests/test_scout_postgres.py`; it remains separately runnable.
