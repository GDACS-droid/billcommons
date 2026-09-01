"""PostgreSQL-backed immutable raw storage used only by Scout.

Scout is deployed independently from ingestion, so its evidence bytes cannot
depend on a cross-service filesystem volume. This store implements the shared
``RawStore`` protocol while keeping each operation in its own short database
session: callers never lend their queue/source transaction to blob I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from billcommons_schema.models import ScoutRawBlob
from billcommons_shared.scout import DEFAULT_MAX_RETAINED_RAWSTORE_BYTES


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_METADATA_BYTES = 4 * 1024
MAX_SCOUT_RAW_BLOB_BYTES = 2 * 1024 * 1024
_RAWSTORE_CAPACITY_LOCK_KEY = 81_420_903
_sqlite_rawstore_capacity_lock = threading.RLock()


class PostgresScoutRawStore:
    """Content-addressed Scout evidence blobs persisted in PostgreSQL.

    ``sha256`` is the sole identity authority. A unique primary key gives
    concurrent writers idempotent first-observation semantics; later metadata
    never rewrites the shared immutable blob record.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        max_retained_bytes: int = DEFAULT_MAX_RETAINED_RAWSTORE_BYTES,
    ) -> None:
        if (
            isinstance(max_retained_bytes, bool)
            or not isinstance(max_retained_bytes, int)
            or max_retained_bytes <= 0
        ):
            raise ValueError("scout_rawstore_retained_capacity_invalid")
        self.sessions = sessions
        self.max_retained_bytes = max_retained_bytes

    @contextmanager
    def _capacity_lock(self, db: Session):
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _RAWSTORE_CAPACITY_LOCK_KEY})
            yield
            return
        with _sqlite_rawstore_capacity_lock:
            yield

    @staticmethod
    def _retained_bytes(db: Session) -> int:
        length = func.octet_length(ScoutRawBlob.data)
        if db.bind is not None and db.bind.dialect.name != "postgresql":
            length = func.length(ScoutRawBlob.data)
        return int(db.scalar(select(func.coalesce(func.sum(length), 0))) or 0)

    @staticmethod
    def _key(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _metadata(meta: dict | None) -> dict[str, Any]:
        if meta is None:
            return {}
        if not isinstance(meta, dict):
            raise TypeError("rawstore_metadata_must_be_dict")
        encoded = json.dumps(meta, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_METADATA_BYTES:
            raise ValueError("rawstore_metadata_too_large")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):  # defensive: JSON object is required above
            raise TypeError("rawstore_metadata_must_be_object")
        return decoded

    @staticmethod
    def _valid_key(key: str) -> bool:
        return bool(_SHA256_RE.fullmatch(key))

    def put(self, data: bytes, meta: dict | None = None) -> str:
        payload = bytes(data)
        if len(payload) > MAX_SCOUT_RAW_BLOB_BYTES:
            raise ValueError("scout_rawstore_payload_too_large")
        key = self._key(payload)
        metadata = self._metadata(meta)
        with self.sessions() as db:
            with self._capacity_lock(db):
                existing = db.get(ScoutRawBlob, key)
                if existing is not None:
                    if self._key(bytes(existing.data)) != key:
                        raise RuntimeError("scout_rawstore_hash_conflict")
                    return key
                if self._retained_bytes(db) + len(payload) > self.max_retained_bytes:
                    raise ValueError("scout_rawstore_capacity_exceeded")
                db.add(ScoutRawBlob(sha256=key, data=payload, metadata_json=metadata))
                try:
                    db.commit()
                    return key
                except IntegrityError:
                    # The advisory lock covers normal Scout writers. Preserve
                    # idempotency if an external/legacy writer won the key.
                    db.rollback()
                    existing = db.get(ScoutRawBlob, key)
                    if existing is None or self._key(bytes(existing.data)) != key:
                        raise RuntimeError("scout_rawstore_hash_conflict")
                    return key

    def get(self, key: str) -> bytes:
        if not self._valid_key(key):
            raise FileNotFoundError(f"no Scout raw payload stored for key {key!r}")
        with self.sessions() as db:
            blob = db.get(ScoutRawBlob, key)
            if blob is None:
                raise FileNotFoundError(f"no Scout raw payload stored for key {key!r}")
            data = bytes(blob.data)
        if self._key(data) != key:
            raise RuntimeError("scout_rawstore_corrupt_blob")
        return data

    def exists(self, key: str) -> bool:
        if not self._valid_key(key):
            return False
        with self.sessions() as db:
            data = db.execute(
                select(ScoutRawBlob.data).where(ScoutRawBlob.sha256 == key)
            ).scalar_one_or_none()
        return data is not None and self._key(bytes(data)) == key

    def healthcheck(self) -> bool:
        """Prove database/table reachability without adding a permanent probe blob."""
        with self.sessions() as db:
            db.execute(select(ScoutRawBlob.sha256).limit(1))
        return True
