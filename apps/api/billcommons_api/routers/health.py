from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from billcommons_shared.db import get_engine

from billcommons_api.deps import get_db
from billcommons_api.schemas import HealthOut, ReadyOut

router = APIRouter(tags=["health"])


def _pool_census() -> dict | None:
    """A snapshot of the connection pool, or None if it cannot be read.

    Returns None rather than zeros on failure. Zeros here would render as a
    quiet, idle pool during precisely the incident this is meant to expose.

    Cheap by construction: every value is already tracked in memory by
    SQLAlchemy's QueuePool. No database round trip, so this stays honest even
    when the database is the thing that is broken.
    """
    try:
        pool = get_engine().pool
        size = pool.size()
        max_overflow = getattr(pool, "_max_overflow", 0) or 0
        checked_out = pool.checkedout()
        capacity = size + max_overflow
        # Deliberately NOT reporting pool.overflow(). It is an internal counter
        # that starts at -pool_size and reads as "-29" on a healthy service --
        # a number an operator has to stop and decode during an incident, which
        # is the worst possible moment to hand someone a puzzle.
        return {
            "in_use": checked_out,
            "idle": pool.checkedin(),
            "capacity": capacity,
            "pool_size": size,
            "max_overflow": max_overflow,
            # The one number that matters: 1.0 means every slot is held and the
            # next request queues for pool_timeout seconds before failing.
            "saturation": round(checked_out / capacity, 3) if capacity else None,
        }
    except Exception:  # noqa: BLE001 - health must never fail on its own telemetry
        return None


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    # NOTE for anyone writing a monitor against this: it returns HTTP 200 even
    # when database is "error". That is deliberate (a 5xx here would take the
    # service out of rotation on a transient blip) and it is also exactly what
    # made the 2026-08-02 outage invisible. Assert on the BODY.
    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        database=db_status,
        pool=_pool_census(),
    )


@router.get("/ready", response_model=ReadyOut)
def ready(db: Session = Depends(get_db)) -> ReadyOut:
    try:
        db.execute(text("SELECT 1"))
        return ReadyOut(ready=True, database="ok")
    except Exception:
        return ReadyOut(ready=False, database="error")
