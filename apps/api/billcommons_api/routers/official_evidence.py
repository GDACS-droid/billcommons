"""Read-only public metadata and bytes for reviewed official evidence."""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated

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
        _close(db)
        raise _public_failure() from None


def _close(db) -> None:
    try:
        try:
            db.rollback()
        finally:
            db.close()
    except Exception:
        raise _public_failure() from None


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


@router.get("/overview")
def overview() -> dict:
    """Bounded inventory; discovery success never proves statewide freshness."""
    db = None
    try:
        db = _session()
        rows = db.execute(text("""
            SELECT j.abbreviation, t.id, t.adapter_name, t.source_url,
                   t.scope, t.enabled, t.cadence_seconds, t.next_check_at,
                   t.consecutive_failures, o.id AS observation_id,
                   o.retrieved_at, o.status, o.error_class, o.raw_sha256,
                   o.record_count
              FROM official_source_targets t
              JOIN jurisdictions j ON j.id = t.jurisdiction_id
              LEFT JOIN LATERAL (
                  SELECT id, retrieved_at, status, error_class, raw_sha256,
                         record_count
                    FROM official_source_observations
                   WHERE target_id = t.id
                   ORDER BY retrieved_at DESC, id DESC LIMIT 1
              ) o ON true
             WHERE j.abbreviation = ANY(:codes)
             ORDER BY j.abbreviation, t.adapter_name, t.id
             LIMIT 1001
        """), {"codes": sorted(_JURISDICTIONS)}).mappings().all()
        if len(rows) > 1000:
            raise _public_failure()
        now = datetime.now(timezone.utc)
        states = {code: {"jurisdiction": code, "official_freshness": "unverified",
                         "targets": []} for code in sorted(_JURISDICTIONS)}
        for row in rows:
            if not row["enabled"]:
                state = "disabled"
            elif row["observation_id"] is None:
                state = "not_observed"
            elif row["status"] != "succeeded":
                state = "failed"
            elif (now - row["retrieved_at"]).total_seconds() > row["cadence_seconds"]:
                state = "observation_overdue"
            else:
                state = "observed"
            states[row["abbreviation"]]["targets"].append({
                "target_id": str(row["id"]), "adapter_name": row["adapter_name"],
                "source_url": row["source_url"], "scope": row["scope"],
                "enabled": row["enabled"], "state": state,
                "cadence_seconds": row["cadence_seconds"],
                "next_check_at": _iso(row["next_check_at"]),
                "consecutive_failures": row["consecutive_failures"],
                "observation_id": str(row["observation_id"]) if row["observation_id"] else None,
                "retrieved_at": _iso(row["retrieved_at"]),
                "status": row["status"], "error_class": row["error_class"],
                "raw_sha256": row["raw_sha256"], "record_count": row["record_count"],
            })
        return {"generated_at": now.isoformat(), "jurisdiction_count": len(states),
                "interpretation": "Source observations have adapter-specific scope; they do not establish statewide freshness or completeness.",
                "items": list(states.values())}
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            _close(db)


@router.get("/observations")
def observations(
    jurisdiction: str = Query(..., min_length=2, max_length=2),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT),
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
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
                    LIMIT :limit OFFSET :offset"""
            ),
            {"jurisdiction": code, "limit": limit + 1, "offset": offset},
        ).mappings().all()
        return {
            "jurisdiction": code,
            "has_more": len(rows) > limit,
            "next_offset": offset + limit if len(rows) > limit and offset + limit <= 10000 else None,
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
                    "raw_sha256": row["raw_sha256"],
                    "status": row["status"],
                    "error_class": row["error_class"],
                    "record_count": row["record_count"],
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows[:limit]
            ],
        }
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            _close(db)


@router.get("/reconciliations")
def reconciliations(
    observation_id: uuid.UUID = Query(...),
    limit: int = Query(20, ge=1, le=_MAX_LIMIT),
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
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
                    LIMIT :limit OFFSET :offset"""
            ),
            {"observation_id": observation_id, "limit": limit + 1, "offset": offset},
        ).mappings().all()
        return {
            "observation_id": str(observation_id),
            "has_more": len(rows) > limit,
            "next_offset": offset + limit if len(rows) > limit and offset + limit <= 10000 else None,
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
                    "local_snapshot_sha256": row["local_snapshot_sha256"],
                    "diff_sha256": row["diff_sha256"],
                    "error_class": row["error_class"],
                    "completed_at": _iso(row["completed_at"]),
                }
                for row in rows[:limit]
            ],
        }
    except HTTPException:
        raise
    except Exception:
        raise _public_failure() from None
    finally:
        if db is not None:
            _close(db)


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
            _close(db)
