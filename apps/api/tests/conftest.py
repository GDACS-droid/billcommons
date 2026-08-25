"""Shared pytest fixtures for API contract tests.

These tests run against the live Railway Postgres DB (0001 schema applied,
mostly-empty tables). Tests must be empty-DB tolerant: assert response
shape/status/pagination math, never row counts.

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

import pytest
from fastapi.testclient import TestClient

from billcommons_api.app import create_app


@pytest.fixture(scope="session")
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c
