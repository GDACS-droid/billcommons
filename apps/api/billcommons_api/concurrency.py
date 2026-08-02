"""A ceiling on simultaneously in-flight requests.

The per-IP rate limiter answers "how OFTEN may one caller ask?". It does not
answer "how many requests may be in the database at once?", and those are
different failure modes. On 2026-08-02 the API went down at roughly 0.4
requests per second -- nowhere near any rate limit -- because each request held
a pooled connection for ~2.2 seconds. Sustained arrival rate was irrelevant;
concurrent occupancy was everything.

Without a bound, excess requests queue inside SQLAlchemy's pool for
`pool_timeout` seconds and then fail anyway, having held a thread the whole
time. Failing immediately with a Retry-After is strictly better for the caller
(it can back off) and for the service (the pool stays reachable for everyone
else).

Deliberately NOT a WAF. A WAF in front of this would mean moving the domain's
nameservers to protect a service taking well under one request per second --
a large blast radius for a small problem. This is the part of that idea which
actually addresses the observed failure.

Health and readiness are exempt: an overloaded service must still be able to
say it is overloaded, and a monitor that gets a 503 from /health cannot
distinguish "saturated" from "dead".
"""
from __future__ import annotations

import os
import threading

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Sized against the pool, not guessed. Pool capacity on the API service is
# 30 + 20 = 50 connections; the AnyIO threadpool that runs sync endpoints is
# 40. A limit above either just recreates the queue one layer up, so this sits
# below both, leaving headroom for the exempt paths.
DEFAULT_MAX_CONCURRENT = 32

# Paths that must answer even while shedding load.
EXEMPT_SUFFIXES = ("/health", "/ready")

# The live middleware instance, so /health can report in_flight and shed_total.
# Starlette constructs middleware lazily inside add_middleware, so there is no
# handle to it at app-build time; the instance registers itself here instead.
# Module-level rather than app.state because one process serves one app, and a
# second app (tests build several) should simply take over the reference --
# stats belong to whichever app is actually serving.
_INSTANCE: "ConcurrencyLimitMiddleware | None" = None


def current_limiter() -> "ConcurrencyLimitMiddleware | None":
    return _INSTANCE


def _int_env(name: str, default: int) -> int:
    """A malformed env value must not take the service down at import time."""
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


class ConcurrencyLimitMiddleware:
    """Reject requests beyond a fixed number of in-flight ones.

    Raw ASGI rather than BaseHTTPMiddleware: the latter wraps every request in
    an extra task and an anyio stream, which is measurable overhead on a hot
    path whose whole purpose is being cheap when the service is under stress.
    """

    def __init__(self, app: ASGIApp, max_concurrent: int | None = None):
        self.app = app
        # `is not None`, not `or`: an explicit 0 is a deliberate kill switch
        # (shed everything except health), and the falsy-or idiom would
        # silently turn it into the default -- a configured limit quietly
        # ignored is worse than one that is wrong out loud.
        self.max_concurrent = (
            max_concurrent
            if max_concurrent is not None
            else _int_env("BILLCOMMONS_MAX_CONCURRENT_REQUESTS", DEFAULT_MAX_CONCURRENT)
        )
        self._lock = threading.Lock()
        self._in_flight = 0
        # Cumulative count of shed requests, surfaced on /health so a load-shed
        # event leaves a trace. An incident that produces no evidence gets
        # diagnosed twice.
        self.shed_total = 0
        global _INSTANCE
        _INSTANCE = self

    def _acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self.max_concurrent:
                self.shed_total += 1
                return False
            self._in_flight += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "max_concurrent": self.max_concurrent,
                "shed_total": self.shed_total,
            }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "").endswith(EXEMPT_SUFFIXES):
            await self.app(scope, receive, send)
            return

        if not self._acquire():
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "server_overloaded",
                        "message": (
                            "Too many requests in flight. This is a concurrency "
                            "limit, not a rate limit: retry shortly rather than "
                            "backing off for long."
                        ),
                    }
                },
                headers={"Retry-After": "2", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            # finally, not a plain call: a client disconnecting mid-response
            # raises, and a leaked slot is permanent. Enough of them and the
            # service refuses every request while doing no work at all.
            self._release()
