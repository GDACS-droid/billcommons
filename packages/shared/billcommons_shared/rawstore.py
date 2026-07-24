"""RawStore: content-addressed storage for raw upstream source payloads.

v1 backend is the filesystem (Railway volume in production); an S3 backend is
the preferred long-term target per docs/architecture/ARCHITECTURE.md but is
deferred — this is a documented v1 tradeoff, not a design decision to build on
top of blindly. Keys are sha256 hex digests of the stored bytes, so identical
upstream payloads always dedupe to the same key regardless of source.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol


class RawStore(Protocol):
    """Content-addressed raw-payload store used by ingestion adapters."""

    def put(self, data: bytes, meta: dict | None = None) -> str:
        """Store `data`, returning its sha256-hex key. If `meta` is provided,
        it is stored alongside the payload (e.g. source URL, retrieved_at)."""
        ...

    def get(self, key: str) -> bytes:
        """Return the raw bytes previously stored under `key`."""
        ...

    def exists(self, key: str) -> bool:
        """Return True if `key` is already present in the store."""
        ...


def _sha256_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FilesystemRawStore:
    """Filesystem-backed RawStore.

    Payloads are sharded by the first two hex characters of their key to
    avoid dumping millions of files into a single directory, e.g.:
        <root>/ab/ab34...ef.bin
        <root>/ab/ab34...ef.meta.json   (if meta was provided)
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.environ.get("RAWSTORE_ROOT", "./data/rawstore"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: str) -> tuple[Path, Path]:
        shard = self.root / key[:2]
        return shard / f"{key}.bin", shard / f"{key}.meta.json"

    def put(self, data: bytes, meta: dict | None = None) -> str:
        key = _sha256_key(data)
        data_path, meta_path = self._paths(key)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        if not data_path.exists():
            data_path.write_bytes(data)
        if meta is not None:
            meta_path.write_text(json.dumps(meta, default=str))
        return key

    def get(self, key: str) -> bytes:
        data_path, _ = self._paths(key)
        if not data_path.exists():
            raise FileNotFoundError(f"no raw payload stored for key {key!r}")
        return data_path.read_bytes()

    def exists(self, key: str) -> bool:
        data_path, _ = self._paths(key)
        return data_path.exists()
