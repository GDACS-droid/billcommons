"""Per-IP request rate limiting for the public API.

slowapi's ``application_limits`` + ``SlowAPIMiddleware`` did not reliably
enforce a global limit (no headers injected, requests never throttled), so we
use an explicit in-process fixed-window limiter — the same proven shape as the
MCP server's limiter. Single-process assumption: each API instance limits its
own traffic; horizontal scaling would need a shared store (Redis) but is not
in scope for the v1 public tier.
"""
from __future__ import annotations

import hmac
import os
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Endpoints that must never be throttled (uptime probes / load-balancer checks).
_EXEMPT_PATHS = frozenset({"/api/v1/health", "/api/v1/ready"})

# Header carrying the shared secret that identifies our own server-side
# renderer. See `is_trusted_client`.
TRUSTED_CLIENT_HEADER = "x-billcommons-internal"


def client_ip(request: Request) -> str:
    """First hop of X-Forwarded-For (Railway/reverse-proxy) else the peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_trusted_client(request: Request) -> bool:
    """True when the caller proves it is our own server-side renderer.

    The website is server-rendered on Vercel, so every page view reaches this
    API from one of a handful of Vercel egress addresses -- and `client_ip`
    keys on exactly that address. The entire public site therefore shared ONE
    per-IP bucket. At 7-10 API calls per bill page, a 300/minute limit is a
    hard ceiling of ~30-43 bill pages per minute *for all visitors combined*,
    and every visitor past it sees 429s caused by other visitors.

    Worse, the web app caches API responses: a 429 storm can be written into
    Next's Data Cache and served back for the full revalidate window, so a
    brief self-throttle outlives itself.

    The fix is identity, not a bigger number. The renderer sends a shared
    secret and skips the limiter; unauthenticated public traffic is unaffected
    and still limited per IP. Absent/blank secret => no bypass, so a
    misconfigured deploy fails closed (throttled) rather than open.
    """
    secret = os.environ.get("BILLCOMMONS_INTERNAL_CLIENT_SECRET", "")
    if not secret:
        return False
    presented = request.headers.get(TRUSTED_CLIENT_HEADER, "")
    if not presented:
        return False
    # compare_digest: the comparison must not leak the secret through timing.
    return hmac.compare_digest(presented, secret)


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
        if request.url.path in _EXEMPT_PATHS or is_trusted_client(request):
            return await call_next(request)
        allowed, retry_after, remaining, reset_in = self._allow(client_ip(request))
        if not allowed:
            request_id = request.headers.get("x-request-id", "")
            return JSONResponse(
                status_code=429,
                # no-store is load-bearing here, not boilerplate: a cached 429
                # is a self-inflicted outage that outlives its cause. Next's
                # Data Cache is deployment-persistent, and a CDN with a
                # cache-everything rule would pin this refusal at the edge for
                # every client behind it.
                headers={
                    "Retry-After": str(retry_after),
                    "Cache-Control": "no-store",
                    **self._headers(0, reset_in),
                },
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
