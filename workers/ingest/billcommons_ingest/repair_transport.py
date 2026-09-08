"""Fail-closed, SSRF-guarded transport for autonomous full-text repairs.

Ordinary ingestion retains ``fulltext.FullTextFetcher``'s historical httpx
transport and its robots fallback semantics.  Autonomous repairs use this
factory instead, so every new request is public-HTTPS admitted, connected to a
pinned vetted address, body-bounded, and protected by a fail-closed robots
policy.  This module intentionally contains no database or repair scheduling
logic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from billcommons_ingest.fulltext import FullTextFetcher
from billcommons_ingest.host_auth import HostAuth
from billcommons_shared.httpc import USER_AGENT
from billcommons_shared.official_tls import reviewed_context_for_host
from billcommons_shared.safe_http import SafeHttpError, SafeResponse, new_safe_http_client

DOCUMENT_MAX_BODY_BYTES = 8 * 1024 * 1024
ROBOTS_MAX_BODY_BYTES = 256 * 1024
ROBOTS_POLICY_TTL_SECONDS = 300.0


class SafeFetchClient(Protocol):
    """The small SafeHttpClient surface used by the repair transport."""

    def fetch(
        self,
        url: str,
        *,
        method: str = "POST",
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        require_body: bool = True,
    ) -> SafeResponse: ...


class SafeHttpGetAdapter:
    """Expose a SafeHttpClient as the minimal ``httpx.Client.get`` surface.

    The adapter itself never follows redirects.  Autonomous repairs use only
    direct reviewed HTTPS URLs: the production SafeHttpClient rejects a 3xx
    response before this adapter can return it.  A redirect therefore fails
    closed and requires a separately reviewed source change.
    """

    def __init__(self, client: SafeFetchClient) -> None:
        self._client = client

    def get(
        self,
        url: str,
        *,
        follow_redirects: bool = False,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> httpx.Response:
        request = httpx.Request("GET", url, headers={"User-Agent": USER_AGENT})
        if follow_redirects or headers or kwargs:
            raise httpx.RequestError("repair transport request rejected", request=request)
        try:
            response = self._client.fetch(
                url,
                method="GET",
                headers={"User-Agent": USER_AGENT},
                require_body=True,
            )
        except SafeHttpError:
            # Do not leak a SafeHttpError reason: it can encode peer-controlled
            # transport details and is not useful to the full-text retry path.
            raise httpx.RequestError("repair transport request failed", request=request) from None
        if response.body is None:
            raise httpx.RequestError("repair transport response body missing", request=request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            content=response.body,
            request=request,
        )


@dataclass(frozen=True)
class _RobotsPolicy:
    parser: RobotFileParser
    supports_request_cadence: bool
    expires_at: float


class StrictRobotsCache:
    """Robots cache for repairs: unavailable or denied policy means deny.

    Only a fetched HTTP 200 policy is cached, and only for five minutes.
    Explicit missing-policy statuses (404/410) are allowed according to
    robots semantics but are deliberately not cached.  Authentication is not
    supported by this class or by the factory below.
    """

    def __init__(self, client: SafeHttpGetAdapter, *, clock=time.monotonic) -> None:
        self._client = client
        self._clock = clock
        self._policies: dict[str, _RobotsPolicy] = {}

    def invalidate(self, origin: str) -> None:
        self._policies.pop(origin, None)

    def can_fetch(self, url: str) -> bool:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._policies.get(origin)
        if cached is not None and cached.expires_at > self._clock():
            return cached.supports_request_cadence and cached.parser.can_fetch(USER_AGENT, url)
        if cached is not None:
            self._policies.pop(origin, None)

        try:
            response = self._client.get(f"{origin}/robots.txt")
        except httpx.HTTPError:
            return False

        if response.status_code in (404, 410):
            return True
        if response.status_code != 200:
            # In particular, 401/403 are an access denial, not a missing file.
            return False

        parser = RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        parser.parse(response.text.splitlines())
        supports_request_cadence = (
            parser.crawl_delay(USER_AGENT) is None and parser.request_rate(USER_AGENT) is None
        )
        self._policies[origin] = _RobotsPolicy(
            parser=parser,
            supports_request_cadence=supports_request_cadence,
            expires_at=self._clock() + ROBOTS_POLICY_TTL_SECONDS,
        )
        # FullTextFetcher has one static rate limiter.  A repair must not
        # silently ignore a source-specified cadence it cannot represent.
        return supports_request_cadence and parser.can_fetch(USER_AGENT, url)


def new_repair_fetcher(
    *,
    document_client: SafeFetchClient | None = None,
    robots_client: SafeFetchClient | None = None,
) -> FullTextFetcher:
    """Return a public, fail-closed fetcher for bounded autonomous repairs.

    Optional SafeHttp-compatible clients exist only for deterministic tests.
    Production callers receive distinct request-size limits for documents and
    robots.txt.  Passing an explicit empty HostAuth prevents ambient
    configuration from attaching credentials or granting robots exemptions.
    """
    active_document_client = document_client or new_safe_http_client(
        max_body_bytes=DOCUMENT_MAX_BODY_BYTES,
        ssl_context_factory=reviewed_context_for_host,
    )
    active_robots_client = robots_client or new_safe_http_client(
        max_body_bytes=ROBOTS_MAX_BODY_BYTES,
        ssl_context_factory=reviewed_context_for_host,
    )
    document_adapter = SafeHttpGetAdapter(active_document_client)
    robots_adapter = SafeHttpGetAdapter(active_robots_client)
    return FullTextFetcher(
        client=document_adapter,
        robots_cache=StrictRobotsCache(robots_adapter),
        host_auth=HostAuth({}),
    )
