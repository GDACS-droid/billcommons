"""Shared pytest fixtures for billcommons_ingest tests.

`db_session` runs each test inside an outer transaction + SAVEPOINT that is
always rolled back, so tests exercise the real live schema (per the
project's "0001 schema already applied" live DB) without leaving any rows
behind. This mirrors the standard SQLAlchemy "join a session to an external
transaction" test pattern.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from billcommons_shared.db import get_engine
from billcommons_shared.rawstore import FilesystemRawStore

# Tests run against the real live DB (per the module docstring above), which
# can carry orphaned idle-in-transaction connections holding row locks (see
# FIX 2 in billcommons_shared/db.py). A 30s statement_timeout means a test
# that collides with a pre-existing lock fails fast with a clear DB error
# instead of hanging indefinitely.
TEST_STATEMENT_TIMEOUT_MS = 30_000


@pytest.fixture()
def db_session(tmp_path):
    engine = get_engine()
    connection = engine.connect()
    trans = connection.begin()
    connection.execute(text(f"SET LOCAL statement_timeout = {TEST_STATEMENT_TIMEOUT_MS}"))
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture()
def unique_abbr():
    """A fresh, collision-proof jurisdiction abbreviation for tests that
    insert a Jurisdiction row -- fixed fake abbreviations (e.g. "ZQ_COV")
    can hang indefinitely if a pre-existing orphaned transaction on the
    shared live DB holds a lock on that exact row (see FIX 3)."""

    def _make(prefix: str = "ZZ") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:8].upper()}"

    return _make


@pytest.fixture()
def rawstore(tmp_path):
    return FilesystemRawStore(root=tmp_path / "rawstore")


@pytest.fixture()
def unique_kind():
    """A fresh, collision-proof ingest_jobs `kind` string for queue tests
    (test_queue.py) that need to scope `claim_job`/`enqueue` to just their
    own fixture rows -- this test suite runs against a live, shared
    `ingest_jobs` table the production worker is concurrently
    enqueueing/claiming real jobs against (see test_queue.py's module
    docstring), so a fixed kind like "bootstrap" is not enough isolation on
    its own."""

    def _make(prefix: str = "test_kind") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    return _make
