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
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from billcommons_schema.models import ScoutRawBlob


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_METADATA_BYTES = 4 * 1024
MAX_SCOUT_RAW_BLOB_BYTES = 2 * 1024 * 1024


class PostgresScoutRawStore:
    """Content-addressed Scout evidence blobs persisted in PostgreSQL.

    ``sha256`` is the sole identity authority. A unique primary key gives
    concurrent writers idempotent first-observation semantics; later metadata
    never rewrites the shared immutable blob record.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

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
            db.add(ScoutRawBlob(sha256=key, data=payload, metadata_json=metadata))
            try:
                db.commit()
                return key
            except IntegrityError:
                # A concurrent identical put won the primary-key race. Roll
                # back only this independent blob transaction, then verify the
                # authoritative stored bytes before accepting its key.
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
