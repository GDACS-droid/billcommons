"""Open States v3 REST API client.

Read-only client for https://v3.openstates.org -- used for incremental
sync (`updated_since`) between bulk-CSV bootstraps, per
docs/architecture/ARCHITECTURE.md's ingestion-tiers table ("T2 Open States
bulk CSV + v3 API (bootstrap + incremental)").

The client must be constructible and testable WITHOUT an API key: real
requests will 401 without one, but object construction, URL building, and
response-parsing all need to be exercisable against injected fixtures (a
fake httpx transport / a canned response object) in unit tests that never
touch the network. `OPENSTATES_API_KEY` may not exist yet in this
environment -- the client reads it lazily (only at request time), not at
import or construction time.

Politeness: v3's default free-tier rate limit is documented as ~6
requests/minute (250/day); we default to that via
`billcommons_shared.httpc.RateLimiter` and respect `Retry-After` /
`X-RateLimit-*` response headers when present, backing off on 429s.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from billcommons_shared.httpc import RateLimiter, new_client

DEFAULT_BASE_URL = "https://v3.openstates.org"
DEFAULT_RATE_PER_MINUTE = 6
API_KEY_ENV_VAR = "OPENSTATES_API_KEY"


class OpenStatesAPIError(RuntimeError):
    """Raised for non-2xx, non-429 responses (429 is handled via backoff)."""


class OpenStatesAuthError(OpenStatesAPIError):
    """Raised when OPENSTATES_API_KEY is required but not configured."""


@dataclass
class OpenStatesClient:
    """Thin v3 API client. Construct with an injected httpx.Client (or the
    default real one) so tests can pass a `httpx.MockTransport`-backed
    client without ever needing a real API key or network access.
    """

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    client: httpx.Client | None = None
    rate_limiter: RateLimiter | None = None
    max_retries_on_429: int = 5

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = new_client(base_url=self.base_url)
        if self.rate_limiter is None:
            self.rate_limiter = RateLimiter(rate_per_sec=DEFAULT_RATE_PER_MINUTE / 60.0, burst=1)

    def _resolve_api_key(self) -> str:
        key = self.api_key or os.environ.get(API_KEY_ENV_VAR)
        if not key:
            raise OpenStatesAuthError(
                f"{API_KEY_ENV_VAR} is not set; required to call the Open States v3 API"
            )
        return key

    def _request(self, method: str, path: str, *, params: dict | None = None) -> dict:
        api_key = self._resolve_api_key()
        headers = {"X-API-KEY": api_key}

        attempt = 0
        while True:
            self.rate_limiter.acquire(self.base_url)
            response = self.client.request(method, path, params=params, headers=headers)
            if response.status_code == 429:
                attempt += 1
                if attempt > self.max_retries_on_429:
                    raise OpenStatesAPIError(
                        f"exceeded max retries on 429 for {method} {path}"
                    )
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2.0 ** attempt
                time.sleep(delay)
                continue
            if response.status_code >= 400:
                raise OpenStatesAPIError(
                    f"{method} {path} failed: {response.status_code} {response.text[:500]}"
                )
            return response.json()

    def get_jurisdictions(self, *, classification: str | None = None) -> dict:
        params = {}
        if classification:
            params["classification"] = classification
        return self._request("GET", "/jurisdictions", params=params)

    def get_jurisdiction(self, jurisdiction_id: str) -> dict:
        return self._request("GET", f"/jurisdictions/{jurisdiction_id}")

    def search_bills(
        self,
        *,
        jurisdiction: str | None = None,
        session: str | None = None,
        updated_since: str | None = None,
        include: list[str] | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> dict:
        """List/search bills. `include` mirrors v3's repeated `include=`
        query params (e.g. sponsorships, abstracts, actions, sources,
        versions, documents, votes)."""
        params: dict = {"page": page, "per_page": per_page}
        if jurisdiction:
            params["jurisdiction"] = jurisdiction
        if session:
            params["session"] = session
        if updated_since:
            params["updated_since"] = updated_since
        if include:
            params["include"] = include
        return self._request("GET", "/bills", params=params)

    def get_bill(self, openstates_id: str, *, include: list[str] | None = None) -> dict:
        params = {"include": include} if include else None
        return self._request("GET", f"/bills/{openstates_id}", params=params)

    def iter_bills(
        self,
        *,
        jurisdiction: str | None = None,
        session: str | None = None,
        updated_since: str | None = None,
        include: list[str] | None = None,
        per_page: int = 20,
    ):
        """Yield individual bill dicts across all pages, following v3's
        pagination envelope (`results`, `pagination.max_page`)."""
        page = 1
        while True:
            payload = self.search_bills(
                jurisdiction=jurisdiction,
                session=session,
                updated_since=updated_since,
                include=include,
                page=page,
                per_page=per_page,
            )
            results = payload.get("results", [])
            for bill in results:
                yield bill
            pagination = payload.get("pagination", {})
            max_page = pagination.get("max_page", page)
            if page >= max_page or not results:
                return
            page += 1
