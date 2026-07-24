"""ETag helper for GET detail routes: hash of the entity's updated_at (+ id)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime


def make_etag(entity_id: uuid.UUID, updated_at: datetime | None) -> str:
    raw = f"{entity_id}:{updated_at.isoformat() if updated_at else ''}"
    return f'W/"{hashlib.sha256(raw.encode()).hexdigest()[:32]}"'
