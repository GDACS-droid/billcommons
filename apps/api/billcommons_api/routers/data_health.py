"""Public, aggregate data-health evidence with bounded database demand.

This describes ingestion operations, never legislative merit or importance.
The report contains no customer data, raw errors, or job payloads. A short
process-local cache and nonblocking refresh lock prevent crawler stampedes.
"""
from __future__ import annotations

import threading
import time
import math
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import text

from billcommons_shared.data_health import collect_report
from billcommons_shared.db import get_session

router = APIRouter(prefix="/data-health", tags=["coverage"])
REPORT_TTL_SECONDS = 300
FAILURE_RETRY_SECONDS = 30


class ReportCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.report: dict | None = None
        self.expires_at = 0.0
        self.retry_at = 0.0

    def _check_retry(self) -> None:
        remaining = self.retry_at - self.clock()
        if remaining > 0:
            raise HTTPException(
                status_code=503,
                detail="Data health is temporarily unavailable. Please retry shortly.",
                headers={"Retry-After": str(math.ceil(remaining)), "Cache-Control": "no-store"},
            )

    def get(self, loader: Callable[[], dict]) -> dict:
        if self.report is not None and self.clock() < self.expires_at:
            return self.report
        self._check_retry()
        if not self.lock.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail="Data health is being refreshed. Please retry shortly.",
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
        try:
            # A refresh could have completed between the first read and lock.
            if self.report is not None and self.clock() < self.expires_at:
                return self.report
            self._check_retry()
            try:
                report = loader()
            except Exception:
                # Failed scans are also single-flight over time. Public
                # traffic cannot immediately restart a timed-out scan.
                self.report = None
                self.retry_at = self.clock() + FAILURE_RETRY_SECONDS
                raise
            self.report = report
            self.retry_at = 0.0
            self.expires_at = self.clock() + REPORT_TTL_SECONDS
            return report
        finally:
            self.lock.release()


_cache = ReportCache()


def _load_report() -> dict:
    db = get_session()
    try:
        # The SQL guard makes accidental writes fail, even if a future report
        # implementation grows beyond its present SELECT-only contract.
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        return collect_report(db)
    finally:
        try:
            db.rollback()
        finally:
            db.close()


@router.get("")
def data_health(response: Response) -> dict:
    """Local ingestion evidence; official-source reconciliation may be unknown.

    generated_at is the observation time. Cached observations can be up to
    five minutes old; this is not a guarantee of legislative freshness.
    """
    try:
        report = _cache.get(_load_report)
    except HTTPException:
        raise
    except Exception:
        # Database exception strings can contain connection details. Keep the
        # public error deliberately generic; never return stale success.
        raise HTTPException(
            status_code=503,
            detail="Data health is temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "30", "Cache-Control": "no-store"},
        ) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Data-Health-Max-Age"] = str(REPORT_TTL_SECONDS)
    return report
