"""Shared httpx client factory: standard User-Agent, timeout, retry-with-backoff
helper, and a per-host token-bucket rate limiter.

Every outbound request Bill Commons makes to a state legislature site or
upstream API should go through `new_client()` / `request_with_retry()` so
we present a consistent, identifiable, well-behaved User-Agent and respect
per-host rate limits (per docs/architecture/ARCHITECTURE.md refresh policy
and general politeness toward public infrastructure we don't own).
"""
from __future__ import annotations

import asyncio
import threading
import time

import httpx

USER_AGENT = "BillCommons/0.1 (+https://billcommons.org; contact@billcommons.org)"
DEFAULT_TIMEOUT = 30.0


def new_client(**kwargs) -> httpx.Client:
    """Return a synchronous httpx.Client configured with the standard
    Bill Commons User-Agent and a 30s timeout. Extra kwargs are passed through
    to httpx.Client (e.g. base_url)."""
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    return httpx.Client(headers=headers, timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT), **kwargs)


def new_async_client(**kwargs) -> httpx.AsyncClient:
    """Async counterpart to new_client()."""
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    return httpx.AsyncClient(
        headers=headers, timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT), **kwargs
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying up to `max_attempts` times with exponential
    backoff (base_delay * 2**attempt) on network errors or 5xx responses.
    Raises the last exception / returns the last (non-raising) response if
    all attempts are exhausted.
    """
    last_exc: Exception | None = None
    response: httpx.Response | None = None
    for attempt in range(max_attempts):
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code < 500:
                return response
        except httpx.HTTPError as exc:
            last_exc = exc
        if attempt < max_attempts - 1:
            time.sleep(base_delay * (2**attempt))
    if response is not None:
        return response
    assert last_exc is not None
    raise last_exc


class RateLimiter:
    """Simple per-host token-bucket rate limiter, thread-safe.

    Usage:
        limiter = RateLimiter(rate_per_sec=2.0, burst=5)
        limiter.acquire("api.example.com")  # blocks until a token is available
    """

    def __init__(self, rate_per_sec: float, burst: int | None = None) -> None:
        self.rate_per_sec = rate_per_sec
        self.burst = burst or max(1, int(rate_per_sec))
        self._buckets: dict[str, tuple[float, float]] = {}  # host -> (tokens, last_ts)
        self._lock = threading.Lock()

    def acquire(self, host: str) -> None:
        while True:
            with self._lock:
                tokens, last_ts = self._buckets.get(host, (float(self.burst), time.monotonic()))
                now = time.monotonic()
                elapsed = now - last_ts
                tokens = min(self.burst, tokens + elapsed * self.rate_per_sec)
                if tokens >= 1.0:
                    self._buckets[host] = (tokens - 1.0, now)
                    return
                self._buckets[host] = (tokens, now)
                wait = (1.0 - tokens) / self.rate_per_sec
            time.sleep(wait)

    async def acquire_async(self, host: str) -> None:
        while True:
            with self._lock:
                tokens, last_ts = self._buckets.get(host, (float(self.burst), time.monotonic()))
                now = time.monotonic()
                elapsed = now - last_ts
                tokens = min(self.burst, tokens + elapsed * self.rate_per_sec)
                if tokens >= 1.0:
                    self._buckets[host] = (tokens - 1.0, now)
                    return
                self._buckets[host] = (tokens, now)
                wait = (1.0 - tokens) / self.rate_per_sec
            await asyncio.sleep(wait)
