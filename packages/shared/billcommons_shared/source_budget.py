"""Durable request admission, independent of ingestion commit/rollback.

A reservation is conservative: a process can die after admission but before
the HTTP request, and that reservation stays spent. No failed source call,
parser rollback, or restart refunds an admission. Database errors fail closed.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from billcommons_schema.models import SourceRequestBudget
from billcommons_shared.db import get_session


class RequestBudgetUnavailable(RuntimeError):
    """Admission could not be established; no network request is allowed."""


class RequestBudgetExhausted(RuntimeError):
    """The upstream scope has no remaining admissions today."""


@dataclass(frozen=True)
class Admission:
    admitted: bool
    exhausted: bool
    retry_after_seconds: float
    requests_reserved: int
    request_limit: int
    budget_date: str


def reserve_request(
    db: Session,
    *,
    scope: str,
    daily_limit: int,
    minimum_interval_seconds: int,
    now: datetime | None = None,
) -> Admission:
    """Reserve at most one request while holding the scope row lock.

    Caller commits before sending HTTP. Production uses the database clock;
    ``now`` exists for deterministic clock-boundary tests. A lower limit or
    slower caller tightens the current day's shared policy, never loosens it.
    Tightening pacing also moves a pending admission out to the new interval
    once; repeated calls at the same interval do not keep sliding it. The
    global pacing timestamp survives midnight independently of the count.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,79}", scope):
        raise ValueError("invalid public request-budget scope")
    if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or not 1 <= daily_limit <= 1_000_000:
        raise ValueError("invalid daily request limit")
    if not isinstance(minimum_interval_seconds, int) or isinstance(minimum_interval_seconds, bool) or not 1 <= minimum_interval_seconds <= 3600:
        raise ValueError("invalid request interval")
    if now is not None and now.tzinfo is None:
        raise ValueError("request-budget clock must include a timezone")

    initial_time = now or db.scalar(select(func.clock_timestamp()))
    initial_time = initial_time.astimezone(timezone.utc)
    db.execute(insert(SourceRequestBudget).values(
        scope=scope,
        budget_date=initial_time.date(),
        requests_reserved=0,
        request_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        next_request_at=initial_time,
        updated_at=initial_time,
    ).on_conflict_do_nothing(index_elements=["scope"]))
    row = db.execute(select(SourceRequestBudget).where(
        SourceRequestBudget.scope == scope,
    ).with_for_update()).scalar_one()
    # Lock acquisition can cross midnight; do not charge its earlier date.
    observed_at = (now or db.scalar(select(func.clock_timestamp()))).astimezone(timezone.utc)
    if observed_at.date() < row.budget_date:
        raise RequestBudgetUnavailable("request-budget clock moved backwards")
    interval_tightened = minimum_interval_seconds > row.minimum_interval_seconds
    if observed_at.date() > row.budget_date:
        row.budget_date = observed_at.date()
        row.requests_reserved = 0
        row.request_limit = daily_limit
        row.minimum_interval_seconds = minimum_interval_seconds
    else:
        row.request_limit = min(row.request_limit, daily_limit)
        row.minimum_interval_seconds = max(row.minimum_interval_seconds, minimum_interval_seconds)
    if interval_tightened:
        row.next_request_at = max(
            row.next_request_at,
            observed_at + timedelta(seconds=row.minimum_interval_seconds),
        )
    row.updated_at = observed_at

    exhausted = row.requests_reserved >= row.request_limit
    if exhausted:
        tomorrow = datetime.combine(observed_at.date() + timedelta(days=1), day_time(), timezone.utc)
        delay = (tomorrow - observed_at).total_seconds()
    else:
        delay = max(0.0, (row.next_request_at - observed_at).total_seconds())
    admitted = not exhausted and delay == 0
    if admitted:
        row.requests_reserved += 1
        row.next_request_at = observed_at + timedelta(seconds=row.minimum_interval_seconds)
    db.flush()
    return Admission(admitted, exhausted, delay, row.requests_reserved, row.request_limit, row.budget_date.isoformat())


def consume_request(
    *,
    scope: str,
    daily_limit: int,
    minimum_interval_seconds: int,
    session_factory: Callable[[], Session] = get_session,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    maximum_wait_seconds: float = 120,
) -> None:
    """Commit an admission before returning to the HTTP caller.

    Pacing waits hold no connection or transaction. Contention is bounded;
    callers retry their durable job if admission cannot be established.
    """
    deadline = monotonic() + maximum_wait_seconds
    while True:
        try:
            with session_factory() as db:
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                decision = reserve_request(
                    db, scope=scope, daily_limit=daily_limit,
                    minimum_interval_seconds=minimum_interval_seconds,
                )
                db.commit()
        except Exception:
            # Driver diagnostics can contain connection material. Admission
            # failure must never fall back to a process-local quota.
            raise RequestBudgetUnavailable("shared request budget is unavailable") from None
        if decision.admitted:
            return
        if decision.exhausted:
            raise RequestBudgetExhausted(
                f"daily request budget for {scope} exhausted ({decision.request_limit})"
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RequestBudgetUnavailable("shared request pacing wait exceeded its bound")
        sleep(min(decision.retry_after_seconds, remaining, 10.0))
