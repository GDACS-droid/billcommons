"""In-flight request ceiling.

The 2026-08-02 outage happened at roughly 0.4 requests per second -- nowhere
near any rate limit -- because each request held a pooled connection for ~2.2
seconds. Arrival rate was never the problem; concurrent occupancy was. The
per-IP rate limiter cannot see that, so this is a separate ceiling.
"""
from __future__ import annotations

import asyncio

from billcommons_api.concurrency import ConcurrencyLimitMiddleware


def _scope(path: str = "/api/v1/bills") -> dict:
    return {"type": "http", "path": path, "headers": []}


def _run(mw, scope):
    """Drive the middleware once, capturing the response status."""
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(mw(scope, receive, send))
    starts = [m for m in sent if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


async def _noop_app(scope, receive, send):
    return None


def test_requests_pass_under_the_limit():
    mw = ConcurrencyLimitMiddleware(_noop_app, max_concurrent=2)
    assert _run(mw, _scope()) is None  # inner app sent nothing; not shed
    assert mw.stats()["shed_total"] == 0


def test_a_slot_is_released_even_when_the_app_raises():
    """A client disconnecting mid-response raises. If the slot leaked, enough
    of them would leave the service refusing every request while doing no work
    at all -- an outage manufactured entirely by its own protection."""

    async def boom(scope, receive, send):
        raise RuntimeError("client went away")

    mw = ConcurrencyLimitMiddleware(boom, max_concurrent=1)
    for _ in range(5):
        try:
            _run(mw, _scope())
        except RuntimeError:
            pass
    assert mw.stats()["in_flight"] == 0
    # Still serving: the limit was never permanently consumed.
    assert mw.stats()["shed_total"] == 0


def test_over_the_limit_sheds_with_503_and_retry_after():
    """Verified by holding real slots, not by faking the counter."""
    mw = ConcurrencyLimitMiddleware(_noop_app, max_concurrent=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_app(scope, receive, send):
        started.set()
        await release.wait()

    mw.app = slow_app

    async def scenario():
        holder = asyncio.create_task(mw(_scope(), None, lambda m: asyncio.sleep(0)))
        await started.wait()

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        await mw(_scope(), None, send)
        release.set()
        await holder
        return sent

    sent = asyncio.run(scenario())
    start = [m for m in sent if m["type"] == "http.response.start"][0]
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 503
    assert headers["retry-after"] == "2"
    # An error response must never be cached -- Next's Data Cache honours
    # no-store, and without it a single shed request poisons a page.
    assert headers["cache-control"] == "no-store"
    assert mw.stats()["shed_total"] == 1


def test_health_is_never_shed():
    """An overloaded service must still be able to say it is overloaded. A
    monitor that gets a 503 from /health cannot tell saturated from dead."""
    mw = ConcurrencyLimitMiddleware(_noop_app, max_concurrent=0)
    assert _run(mw, _scope("/api/v1/health")) is None
    assert mw.stats()["shed_total"] == 0
    # ...while a normal path at the same limit is shed.
    assert _run(mw, _scope("/api/v1/bills")) == 503


def test_a_malformed_env_value_does_not_take_the_service_down(monkeypatch):
    monkeypatch.setenv("BILLCOMMONS_MAX_CONCURRENT_REQUESTS", "not-a-number")
    mw = ConcurrencyLimitMiddleware(_noop_app)
    assert mw.max_concurrent > 0
