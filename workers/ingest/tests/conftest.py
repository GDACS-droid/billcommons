"""Shared pytest fixtures for billcommons_ingest tests.

`db_session` runs each test inside an outer transaction + SAVEPOINT that is
always rolled back, so tests exercise the real live schema (per the
project's "0001 schema already applied" live DB) without leaving any rows
behind. This mirrors the standard SQLAlchemy "join a session to an external
transaction" test pattern.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from billcommons_shared.db import get_engine
from billcommons_shared.rawstore import FilesystemRawStore


@pytest.fixture()
def db_session(tmp_path):
    engine = get_engine()
    connection = engine.connect()
    trans = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture()
def rawstore(tmp_path):
    return FilesystemRawStore(root=tmp_path / "rawstore")
