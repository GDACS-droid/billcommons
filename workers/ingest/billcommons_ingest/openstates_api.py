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

Daily request budget: a module-level counter (keyed by UTC date) caps the
number of actual HTTP requests this process will send today at
`DAILY_REQUEST_BUDGET` (override via `OPENSTATES_DAILY_BUDGET`). This is a
PER-PROCESS brake, not a cross-process ledger -- the backfill CLI and the
sync-worker each run in their own process and each get their own budget.
There is deliberately no shared storage backing this; it exists to stop a
single runaway process (many pages x many jobs) from blowing through the
~250/day allowance on its own, not to coordinate across processes.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from billcommons_shared.httpc import DEFAULT_TIMEOUT, RateLimiter, new_client

DEFAULT_BASE_URL = "https://v3.openstates.org"
DEFAULT_RATE_PER_MINUTE = 6
API_KEY_ENV_VAR = "OPENSTATES_API_KEY"

# How many extra attempts _request makes (beyond the first) on a read/connect
# timeout, other transport error, or 5xx response -- 2 extra == 3 total.
# Big-state payloads (versions+documents includes) can legitimately take a
# while; a bare ReadTimeout/502 today burns a whole job attempt instead of
# just retrying the one HTTP call.
MAX_HTTP_RETRIES = 2

DAILY_REQUEST_BUDGET = 225
DAILY_BUDGET_ENV_VAR = "OPENSTATES_DAILY_BUDGET"


class OpenStatesAPIError(RuntimeError):
    """Raised for non-2xx, non-429 responses (429 is handled via backoff)."""


class OpenStatesAuthError(OpenStatesAPIError):
    """Raised when OPENSTATES_API_KEY is required but not configured."""


class OpenStatesDailyBudgetExceeded(OpenStatesAPIError):
    """Raised when this process has already sent `DAILY_REQUEST_BUDGET`
    requests today. See the module docstring: this is a per-process brake,
    not a cross-process ledger."""


# utc_date.isoformat() -> requests sent so far today, this process only.
# Guarded by _budget_lock; resets implicitly whenever the UTC date advances
# (a new date is simply a key this dict hasn't seen yet).
_daily_request_counts: dict[str, int] = {}
_budget_lock = threading.Lock()


def _daily_budget() -> int:
    raw = os.environ.get(DAILY_BUDGET_ENV_VAR)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DAILY_REQUEST_BUDGET


def _check_and_consume_budget() -> None:
    """Raise OpenStatesDailyBudgetExceeded if today's budget is already
    spent; otherwise record one more request against it. Called once per
    actual HTTP request (including retries), immediately before it is sent
    -- i.e. AFTER the rate limiter's (possibly blocking) acquire, not
    before. The UTC date is sampled inside the lock, right at the count, so
    a request that was admitted before midnight but whose limiter wait
    crossed into the next UTC day is charged to the day it actually goes
    out on, not the day it was queued."""
    budget = _daily_budget()
    with _budget_lock:
        today = datetime.now(timezone.utc).date().isoformat()
        count = _daily_request_counts.get(today, 0)
        if count >= budget:
            raise OpenStatesDailyBudgetExceeded(
                f"daily Open States request budget ({budget}) exhausted for {today}"
            )
        _daily_request_counts[today] = count + 1


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
            # v3 responses with versions+documents includes on big states
            # (OH/CA/MA/PA/AZ) got bigger; 30s reads were observed
            # insufficient. Only widen read -- connect/write/pool keep the
            # shared default. Injected (test) clients are left alone.
            self.client = new_client(
                base_url=self.base_url,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT, read=90.0),
            )
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

        attempt = 0  # 429 attempts -- unchanged from before this change
        http_retry = 0  # timeout/transport-error/5xx attempts (Change 1)
        while True:
            self.rate_limiter.acquire(self.base_url)
            # Checked/counted AFTER the (possibly blocking) limiter wait so
            # the UTC date used is the day the request actually goes out on
            # -- see _check_and_consume_budget's docstring.
            _check_and_consume_budget()
            try:
                response = self.client.request(method, path, params=params, headers=headers)
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TransportError) as exc:
                http_retry += 1
                if http_retry > MAX_HTTP_RETRIES:
                    raise
                # Paced, not a tight loop: the next iteration re-acquires
                # the rate limiter before retrying.
                continue
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
            if response.status_code >= 500:
                http_retry += 1
                if http_retry > MAX_HTTP_RETRIES:
                    raise OpenStatesAPIError(
                        f"{method} {path} failed: {response.status_code} {response.text[:500]}"
                    )
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

    def get_jurisdiction(self, jurisdiction_id: str, *, include: list[str] | None = None) -> dict:
        params = {}
        if include:
            params["include"] = include
        return self._request("GET", f"/jurisdictions/{jurisdiction_id}", params=params)

    def get_legislative_sessions(self, jurisdiction_id: str) -> list[dict]:
        """Session metadata including start_date/end_date.

        The end date is the authority on whether a bill still has a chance:
        anything short of the governor's desk in a session that has adjourned
        is dead, however alive its own action record looks. Taken from
        upstream rather than researched by hand because sine die dates move,
        special sessions appear mid-year, and a hand-curated table would be
        wrong within a month.
        """
        payload = self.get_jurisdiction(jurisdiction_id, include=["legislative_sessions"])
        return payload.get("legislative_sessions") or []

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
