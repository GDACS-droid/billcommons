"""Per-IP request rate limiting for the public API.

slowapi's ``application_limits`` + ``SlowAPIMiddleware`` did not reliably
enforce a global limit (no headers injected, requests never throttled), so we
use an explicit in-process fixed-window limiter — the same proven shape as the
MCP server's limiter. Single-process assumption: each API instance limits its
own traffic; horizontal scaling would need a shared store (Redis) but is not
in scope for the v1 public tier.
"""
from __future__ import annotations

import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Endpoints that must never be throttled (uptime probes / load-balancer checks).
_EXEMPT_PATHS = frozenset({"/api/v1/health", "/api/v1/ready"})


def client_ip(request: Request) -> str:
    """First hop of X-Forwarded-For (Railway/reverse-proxy) else the peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-IP limiter. `limit` requests per `window` seconds."""

    def __init__(self, app, *, limit: int, window: float = 60.0, clock=time.monotonic):
        super().__init__(app)
        self._limit = limit
        self._window = window
        self._clock = clock
        self._lock = threading.Lock()
        # ip -> (window_start, count)
        self._buckets: dict[str, tuple[float, int]] = {}
        self._last_sweep = 0.0

    def _sweep(self, now: float) -> None:
        """Drop buckets whose window has expired. Caller holds the lock.

        Without this the dict grows once per distinct client IP and is never
        reclaimed, which is both an unbounded memory leak and a cheap way for
        anyone to exhaust the process by rotating source addresses. Swept at
        most once per window, so the cost is amortized to near nothing.
        """
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        stale = [ip for ip, (start, _) in self._buckets.items() if now - start >= self._window]
        for ip in stale:
            del self._buckets[ip]

    def _allow(self, ip: str) -> tuple[bool, int, int, int]:
        """Returns (allowed, retry_after, remaining, reset_in_seconds)."""
        now = self._clock()
        with self._lock:
            self._sweep(now)
            start, count = self._buckets.get(ip, (now, 0))
            if now - start >= self._window:
                start, count = now, 0
            count += 1
            self._buckets[ip] = (start, count)
            reset_in = max(1, int(self._window - (now - start)))
            remaining = max(0, self._limit - count)
            if count > self._limit:
                return False, reset_in, 0, reset_in
            return True, 0, remaining, reset_in

    def _headers(self, remaining: int, reset_in: int) -> dict[str, str]:
        # Advertised on EVERY response, not just 429s. A client that can only
        # discover the limit by hitting it has to either guess or get throttled
        # on purpose -- and an integrator sizing a nightly sync needs the
        # budget up front, which is exactly the gap a consumer reported.
        return {
            "X-RateLimit-Limit": str(self._limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_in),
        }

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        allowed, retry_after, remaining, reset_in = self._allow(client_ip(request))
        if not allowed:
            request_id = request.headers.get("x-request-id", "")
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after), **self._headers(0, reset_in)},
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": (
                            f"Rate limit of {self._limit} requests per "
                            f"{int(self._window)}s exceeded. Retry in {retry_after}s."
                        ),
                        "request_id": request_id,
                    }
                },
            )
        response = await call_next(request)
        response.headers.update(self._headers(remaining, reset_in))
        return response
