"""Bounded, context-local request accounting; never an admission authority."""
from __future__ import annotations

import json
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone


_counts: ContextVar[Counter | None] = ContextVar("openstates_request_counts", default=None)


def count_request_event(event: str) -> None:
    counts = _counts.get()
    if counts is not None:
        counts[event] += 1


@contextmanager
def observe_openstates_requests(*, cycle_id: str, phase: str, state: str | None = None):
    """Emit one phase record even when ingestion rolls back or is interrupted.

    Call sites supply fixed metric names. No URLs, headers, query parameters,
    response bodies, or exception messages are retained. This observes this
    synchronous context only; it is not the shared account's usage ledger.
    """
    counts: Counter = Counter()
    token = _counts.set(counts)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    completed = False
    try:
        yield
        completed = True
    finally:
        _counts.reset(token)
        safe_state = state if isinstance(state, str) and len(state) == 2 and state.isascii() and state.isalpha() else None
        print(json.dumps({
            "event": "openstates_request_usage", "schema_version": 1,
            "cycle_id": cycle_id, "phase": phase, "state": safe_state,
            "started_at": started_at, "elapsed_seconds": round(time.monotonic() - started, 3),
            "phase_returned": completed, "counts": dict(sorted(counts.items())),
        }, sort_keys=True), flush=True)
