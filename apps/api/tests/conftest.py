"""Shared fixtures for API contract tests on an acknowledged local Postgres DB.

The API suite includes fixture writes and Postgres-only FTS/trigram paths. It
is intentionally *not* allowed to use the normal ``DATABASE_URL`` or its
local-env-file fallback.  Set ``BILLCOMMONS_TEST_DATABASE_URL`` to a migrated,
disposable local database ending in ``_test`` and acknowledge fixture writes
with ``BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1``.

`test_api_keys.py`/`test_quota.py`/`test_billing.py`/`test_rate_limit.py`
do NOT use this file's `app`/`client` fixtures -- they build their own app
via `_monetization_sqlite.py`, which defaults to an in-memory SQLite
engine and only touches a real Postgres instance (never this live one)
when `BILLCOMMONS_TEST_DATABASE_URL` is explicitly set AND
`BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE=1` opts into its DELETE-based reset.
See that module's docstring and `docs/operations/monetization-runbook.md`
before ever pointing that variable at anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Pytest places this conftest's directory on sys.path. Import the sibling
# helper directly so the guard works with the repository's documented
# ``PYTHONPATH=apps/api:...`` command as well as from a package-aware IDE.
test_directory = str(Path(__file__).resolve().parent)
if test_directory not in sys.path:
    sys.path.insert(0, test_directory)
from _regression_postgres import configure_disposable_postgres
from _regression_seed import seed_regression_corpus

# Do this at collection time, before any module-local TestClient(create_app())
# can open the shared engine.  Collection-only remains safe because this code
# opens no connection and does not need a DB target.
if "--collect-only" not in sys.argv:
    configure_disposable_postgres()

from billcommons_api.app import create_app


@pytest.fixture(scope="session", autouse=True)
def regression_corpus():
    seed_regression_corpus()


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c
