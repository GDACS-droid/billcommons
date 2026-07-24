"""Per-IP token-bucket rate limiting for the public, anonymous-callable MCP
server (Finding 2 hardening -- see docs/SPEC.md security notes).

Pure in-process ASGI middleware, no Redis/external dependency: a dict of
IP -> bucket protected by a single lock. This server is expected to run as
one process behind Railway's edge (no multi-process/multi-replica sharing of
limiter state is assumed); if that changes, this needs a shared store.

Rate is env-tunable:
    MCP_RATE_LIMIT_PER_MINUTE (default 30) -- steady-state refill rate.
    MCP_RATE_LIMIT_BURST (default 10)      -- extra burst capacity on top of
                                               the steady rate, i.e. bucket
                                               capacity = per_minute + burst.

Client IP resolution: Railway fronts this service, so we trust the FIRST
value in X-Forwarded-For (the original client, per Railway's edge proxy
convention) when present, falling back to the ASGI connection's peer address.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

DEFAULT_PER_MINUTE = 30
DEFAULT_BURST = 10


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class TokenBucket:
    """Simple thread-safe token bucket: refills continuously at `rate` tokens
    per second, holds at most `capacity` tokens."""

    __slots__ = ("capacity", "rate", "tokens", "last_refill")

    def __init__(self, capacity: float, rate: float):
        self.capacity = capacity
        self.rate = rate
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def try_consume(self, amount: float = 1.0) -> tuple[bool, float]:
        """Attempt to consume `amount` tokens. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.last_refill = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0.0
        deficit = amount - self.tokens
        retry_after = deficit / self.rate if self.rate > 0 else 1.0
        return False, retry_after


class RateLimiter:
    """Owns one TokenBucket per client IP. New buckets are created lazily
    with the same capacity/rate; a simple size cap prevents unbounded memory
    growth from an IP-spoofing/enumeration attempt (oldest-by-insertion
    eviction, good enough for a single-process best-effort limiter)."""

    MAX_TRACKED_IPS = 50_000

    def __init__(self, per_minute: int | None = None, burst: int | None = None):
        self.per_minute = per_minute if per_minute is not None else _env_int(
            "MCP_RATE_LIMIT_PER_MINUTE", DEFAULT_PER_MINUTE
        )
        self.burst = burst if burst is not None else _env_int(
            "MCP_RATE_LIMIT_BURST", DEFAULT_BURST
        )
        self.capacity = float(self.per_minute + self.burst)
        self.rate = self.per_minute / 60.0  # tokens per second
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, client_ip: str) -> tuple[bool, float]:
        with self._lock:
            bucket = self._buckets.get(client_ip)
            if bucket is None:
                if len(self._buckets) >= self.MAX_TRACKED_IPS:
                    self._buckets.pop(next(iter(self._buckets)))
                bucket = TokenBucket(self.capacity, self.rate)
                self._buckets[client_ip] = bucket
            return bucket.try_consume()


def _client_ip(scope: dict[str, Any]) -> str:
    """Resolve the caller's IP: first value of X-Forwarded-For (Railway's
    edge sets this to the real client) if present, else the ASGI transport's
    peer address."""
    headers = dict(scope.get("headers") or [])
    xff = headers.get(b"x-forwarded-for")
    if xff:
        first = xff.decode("latin-1").split(",")[0].strip()
        if first:
            return first
    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"


class RateLimitMiddleware:
    """ASGI middleware: rejects requests over the per-IP token-bucket rate
    with a structured 429 JSON body (not a bare HTTP error page), so MCP
    clients get a machine-readable reason rather than a generic transport
    failure."""

    def __init__(self, app, limiter: RateLimiter | None = None):
        self.app = app
        self.limiter = limiter or RateLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_ip = _client_ip(scope)
        allowed, retry_after = self.limiter.check(client_ip)
        if not allowed:
            body = (
                b'{"error": {"code": "rate_limited", "message": '
                b'"Too many requests. This is a public, rate-limited MCP '
                b'server (per-IP token bucket)."}, "retry_after_seconds": '
                + str(round(retry_after, 1)).encode("ascii")
                + b"}"
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(max(1, int(retry_after) + 1)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
