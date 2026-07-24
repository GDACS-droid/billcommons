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

    def _allow(self, ip: str) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            start, count = self._buckets.get(ip, (now, 0))
            if now - start >= self._window:
                start, count = now, 0
            count += 1
            self._buckets[ip] = (start, count)
            if count > self._limit:
                retry_after = max(1, int(self._window - (now - start)))
                return False, retry_after
            return True, 0

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        allowed, retry_after = self._allow(client_ip(request))
        if not allowed:
            request_id = request.headers.get("x-request-id", "")
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
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
        return await call_next(request)
