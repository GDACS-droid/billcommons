from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from billcommons_schema.base import Base
from billcommons_schema.models import ScoutRawBlob
from billcommons_shared.db import _use_psycopg3
from billcommons_scout.rawstore import MAX_SCOUT_RAW_BLOB_BYTES, PostgresScoutRawStore


def _store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scout-rawstore.sqlite'}")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["scout_raw_blobs"]])
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return PostgresScoutRawStore(sessions), PostgresScoutRawStore(sessions), sessions, engine


def test_postgres_scout_rawstore_is_content_addressed_cross_instance_and_restart_safe(tmp_path):
    first, second, sessions, engine = _store(tmp_path)
    try:
        payload = b"official-source-bytes"
        key = first.put(payload, {"source_url": "https://www.flsenate.gov/example", "first": True})
        assert key == hashlib.sha256(payload).hexdigest()
        assert second.exists(key)
        assert second.get(key) == payload
        # Recreating the service store uses fresh sessions but the same durable
        # table, proving no process-local or filesystem dependency remains.
        recreated = PostgresScoutRawStore(sessions)
        assert recreated.get(key) == payload
        recreated.put(payload, {"source_url": "https://www.flsenate.gov/other", "first": False})
        with sessions() as db:
            row = db.get(ScoutRawBlob, key)
            assert row is not None and row.metadata_json == {
                "first": True, "source_url": "https://www.flsenate.gov/example",
            }
    finally:
        engine.dispose()


def test_postgres_scout_rawstore_rejects_invalid_keys_and_oversized_metadata(tmp_path):
    store, _second, sessions, engine = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="metadata_too_large"):
            store.put(b"payload", {"large": "x" * 4096})
        with pytest.raises(ValueError, match="payload_too_large"):
            store.put(b"x" * (MAX_SCOUT_RAW_BLOB_BYTES + 1))
        assert not store.exists("not-a-sha")
        with pytest.raises(FileNotFoundError):
            store.get("not-a-sha")
        with sessions() as db:
            assert db.query(ScoutRawBlob).count() == 0
    finally:
        engine.dispose()


def test_postgres_scout_rawstore_healthcheck_is_read_only(tmp_path):
    store, _second, sessions, engine = _store(tmp_path)
    try:
        assert store.healthcheck()
        with sessions() as db:
            assert db.query(ScoutRawBlob).count() == 0
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("BILLCOMMONS_TEST_POSTGRES_URL"),
    reason="set BILLCOMMONS_TEST_POSTGRES_URL to run PostgreSQL Scout RawStore restart/concurrency coverage",
)
def test_postgres_scout_rawstore_two_instances_concurrent_put_and_restart():
    """Requires a migrated, explicitly disposable local PostgreSQL database."""
    postgres_url = os.environ["BILLCOMMONS_TEST_POSTGRES_URL"]
    parsed = urlsplit(postgres_url)
    database = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    query = parse_qs(parsed.query, keep_blank_values=True)
    query_hosts = query.get("host", [])
    ambiguous_target = (
        len(query_hosts) > 1
        or bool(parsed.hostname and query_hosts)
        or any(query.get(key) for key in ("hostaddr", "service", "servicefile"))
    )
    socket_host = query_hosts[0] if len(query_hosts) == 1 else ""
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"} or (
        not parsed.hostname and socket_host == "/var/run/postgresql"
    )
    if (
        ambiguous_target
        or not local
        or not re.fullmatch(r"billcommons_scout_(?:test|verify|closeout)_\d{8}_test", database)
        or os.environ.get("BILLCOMMONS_TEST_DB_ALLOW_DESTRUCTIVE") != "1"
    ):
        raise RuntimeError("refusing Scout RawStore PostgreSQL coverage outside an acknowledged local disposable database")
    engine = create_engine(_use_psycopg3(postgres_url))
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    payload = f"scout-rawstore-{uuid.uuid4().hex}".encode()
    expected_key = hashlib.sha256(payload).hexdigest()
    first = PostgresScoutRawStore(sessions)
    second = PostgresScoutRawStore(sessions)
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def put(store: PostgresScoutRawStore, ordinal: int) -> None:
        try:
            barrier.wait(timeout=5)
            assert store.put(payload, {"ordinal": ordinal}) == expected_key
        except BaseException as exc:
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=put, args=(first, 1)),
            threading.Thread(target=put, args=(second, 2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert not errors
        # A recreated process/store must read the same durable bytes.
        restarted = PostgresScoutRawStore(sessions)
        assert restarted.exists(expected_key)
        assert restarted.get(expected_key) == payload
        assert restarted.healthcheck()
        with sessions() as db:
            rows = db.execute(select(ScoutRawBlob).where(ScoutRawBlob.sha256 == expected_key)).scalars().all()
            assert len(rows) == 1
    finally:
        with sessions() as db:
            row = db.get(ScoutRawBlob, expected_key)
            if row is not None:
                db.delete(row)
                db.commit()
        engine.dispose()
