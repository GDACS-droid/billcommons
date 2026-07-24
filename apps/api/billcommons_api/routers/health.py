from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from billcommons_api.deps import get_db
from billcommons_api.schemas import HealthOut, ReadyOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return HealthOut(status="ok" if db_status == "ok" else "degraded", database=db_status)


@router.get("/ready", response_model=ReadyOut)
def ready(db: Session = Depends(get_db)) -> ReadyOut:
    try:
        db.execute(text("SELECT 1"))
        return ReadyOut(ready=True, database="ok")
    except Exception:
        return ReadyOut(ready=False, database="error")
