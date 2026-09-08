"""Read-only public metadata and bytes for reviewed official evidence."""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import text

from billcommons_shared.db import get_session

router = APIRouter(prefix="/official-evidence", tags=["official evidence"])
_MAX_LIMIT = 100
_MAX_BLOB_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JURISDICTIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def _session():
    db = get_session()
    try:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        return db
    except Exception:
        db.rollback()
        db.close()
        raise


def _public_failure() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Official evidence is temporarily unavailable. Please retry shortly.",
        headers={"Retry-After": "30", "Cache-Control": "no-store"},
    )


def _jurisdiction(value: str) -> str:
    code = value.strip().upper()
    if code not in _JURISDICTIONS:
        raise HTTPException(status_code=400, detail="jurisdiction must be a US state or DC")
    return code


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/observations")
def observations(
    jurisdiction: str = Query(..., min_length=2, max_length=2),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT),
) -> dict:
    code = _jurisdiction(jurisdiction)
    db = None
    try:
        db = _session()
        rows = db.execute(
            text(
                """SELECT o.id, o.adapter_name, o.adapter_version, o.source_url,
                          o.scope, t.scope AS target_scope, o.retrieved_at,
                          o.upstream_updated_at, o.http_status, o.raw_sha256,
                          o.status, o.error_class, o.record_count, o.created_at
                     FROM official_source_observations o
                     JOIN official_source_targets t ON t.id = o.target_id
                     JOIN jurisdictions j ON j.id = t.jurisdiction_id
                    WHERE j.abbreviation = :jurisdiction
                    ORDER BY o.retrieved_at DESC, o.id DESC
                    LIMIT :limit"""
            ),
            {"jurisdiction": code, "limit": limit},
        ).mappings().all()
        return {
            "jurisdiction": code,
            "items": [
                {
                    "observation_id": str(row["id"]),
                    "adapter_name": row["adapter_name"],
                    "adapter_version": row["adapter_version"],
                    "source_url": row["source_url"],
                    "scope": row["scope"],
                    "target_scope": row["target_scope"],
                    "retrieved_at": _iso(row["retrieved_at"]),
                    "upstream_updated_at": _iso(row["upstream_updated_at"]),
                    "http_status": row["http_status"],
                    "raw_sha": row["raw_sha256"],
                    "raw_sha256": row["raw_sha256"],
                    "status": row["status"],
                    "error_class": row["error_class"],
                    "record_count": row["record_count"],
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows
            ],
        }
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            db.rollback()
            db.close()


@router.get("/reconciliations")
def reconciliations(
    observation_id: uuid.UUID = Query(...),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT),
) -> dict:
    db = None
    try:
        db = _session()
        rows = db.execute(
            text(
                """SELECT id, observation_id, bill_id, official_bill_id,
                          local_snapshot_at, comparator_version, status, summary,
                          local_snapshot_sha256, diff_sha256, error_class,
                          completed_at
                     FROM official_reconciliation_runs
                    WHERE observation_id = :observation_id
                    ORDER BY completed_at DESC, id DESC
                    LIMIT :limit"""
            ),
            {"observation_id": observation_id, "limit": limit},
        ).mappings().all()
        return {
            "observation_id": str(observation_id),
            "items": [
                {
                    "reconciliation_id": str(row["id"]),
                    "observation_id": str(row["observation_id"]),
                    "bill_id": str(row["bill_id"]) if row["bill_id"] else None,
                    "official_bill_id": row["official_bill_id"],
                    "local_snapshot_at": _iso(row["local_snapshot_at"]),
                    "comparator_version": row["comparator_version"],
                    "status": row["status"],
                    "summary": row["summary"],
                    "local_hash": row["local_snapshot_sha256"],
                    "diff_hash": row["diff_sha256"],
                    "local_snapshot_sha256": row["local_snapshot_sha256"],
                    "diff_sha256": row["diff_sha256"],
                    "error_class": row["error_class"],
                    "completed_at": _iso(row["completed_at"]),
                }
                for row in rows
            ],
        }
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            db.rollback()
            db.close()


@router.get("/blobs/{sha256}")
def blob(sha256: str) -> Response:
    if not _SHA256.fullmatch(sha256):
        raise HTTPException(status_code=422, detail="sha256 must be 64 lowercase hexadecimal characters")
    db = None
    try:
        db = _session()
        row = db.execute(
            text("SELECT data, content_type FROM official_raw_blobs WHERE sha256 = :sha256"),
            {"sha256": sha256},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Official evidence blob not found")
        data = bytes(row["data"])
        if len(data) > _MAX_BLOB_BYTES or hashlib.sha256(data).hexdigest() != sha256:
            raise _public_failure()
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{sha256}.bin"',
                "ETag": f'"{sha256}"',
                "X-Content-Type-Options": "nosniff",
            },
        )
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            db.rollback()
            db.close()
