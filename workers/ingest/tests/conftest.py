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

# Test-fixture jurisdiction abbreviations use these prefixes (see unique_abbr).
# Most tests roll back via db_session, but any test that drives the REAL
# `bootstrap`/ingest path opens its own committing sessions, so those rows
# survive rollback and must be swept at end of the run — otherwise a
# test jurisdiction (with bills/votes/coverage) leaks into the shared live DB.
_TEST_ABBR_PREFIXES = ("ZZ_", "ZQ_")


def _purge_test_jurisdictions() -> None:
    from billcommons_shared.db import get_session

    like = " OR ".join(f"abbreviation LIKE '{p}%'" for p in _TEST_ABBR_PREFIXES)
    with get_session() as s:
        ids = [
            r[0]
            for r in s.execute(text(f"SELECT id FROM jurisdictions WHERE {like}")).fetchall()
        ]
        for jid in ids:
            p = {"j": jid}
            bsub = "(SELECT id FROM bills WHERE jurisdiction_id=:j)"
            ve = f"(SELECT id FROM vote_events WHERE bill_id IN {bsub})"
            bv = f"(SELECT id FROM bill_versions WHERE bill_id IN {bsub})"
            for stmt in (
                f"DELETE FROM vote_records WHERE vote_event_id IN {ve}",
                f"DELETE FROM vote_events WHERE bill_id IN {bsub}",
                f"DELETE FROM bill_actions WHERE bill_id IN {bsub}",
                f"DELETE FROM sponsorships WHERE bill_id IN {bsub}",
                f"DELETE FROM bill_documents WHERE bill_version_id IN {bv}",
                f"DELETE FROM bill_versions WHERE bill_id IN {bsub}",
                f"DELETE FROM bill_subjects WHERE bill_id IN {bsub}",
                f"DELETE FROM bill_identifiers WHERE bill_id IN {bsub}",
                f"DELETE FROM related_bills WHERE bill_id IN {bsub}",
                "DELETE FROM ingestion_runs WHERE jurisdiction_id=:j",
                "DELETE FROM validation_runs WHERE jurisdiction_id=:j",
                "DELETE FROM jurisdiction_coverage WHERE jurisdiction_id=:j",
                "DELETE FROM bills WHERE jurisdiction_id=:j",
                "DELETE FROM sessions WHERE jurisdiction_id=:j",
                "DELETE FROM people WHERE jurisdiction_id=:j",
                "DELETE FROM jurisdictions WHERE id=:j",
            ):
                try:
                    s.execute(text(stmt), p)
                    s.commit()
                except Exception:
                    s.rollback()


@pytest.fixture(scope="session", autouse=True)
def _sweep_leaked_test_jurisdictions():
    """Belt-and-suspenders: sweep any ZZ_/ZQ_ test jurisdictions that a
    committing bootstrap/ingest test left behind, at the end of the session,
    so test data never accumulates in the shared live DB."""
    yield
    _purge_test_jurisdictions()


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
