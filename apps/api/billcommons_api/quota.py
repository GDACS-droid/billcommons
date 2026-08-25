"""API-key metering + quota enforcement (2026-08-21 monetization spec,
`SPEC-LOCKED.md` §3 as amended). `QuotaMiddleware` REPLACES
`billcommons_api.rate_limit.RateLimitMiddleware` at the same registration
slot in `app.py` -- middleware ordering (CORS -> Concurrency -> Quota ->
RequestID -> SecureHeaders -> GZip) is unchanged.

Reuses the bleed-stop primitives from `rate_limit.py` without editing them:
`client_ip`, `quota_bucket`, `subnet_bucket`, `_is_heavy_route`,
`is_trusted_client`, `_FixedWindowCounter`, `_RouteTier`. The anonymous
path is the SAME two-tier (default + heavy) enforcement `RateLimitMiddleware`
already did, plus a new third tier: a daily cap per IP/subnet (R1), which
did not exist before this branch and is the whole reason "no paywall on
ordinary search" now needs a number attached to it (README, amendment R15).

**Per-request order** (base spec §3, amended by A5/A11/A12/B2/B3/B6/B7):

  1. Exempt path (`_EXEMPT_PATHS` + A5's prefixes) or OPTIONS -> pass.
  2. `is_trusted_client` -> pass. Unchanged from `RateLimitMiddleware`.
  3. No presented key -> anonymous path (existing IP/subnet/heavy buckets
     + new daily caps) -> 429 on refusal. Response SHAPE is byte-identical
     to before this branch; new BEHAVIOR (the daily cap) is additive.
  4. Key malformed/unknown/revoked -> 401 `invalid_api_key` +
     `WWW-Authenticate: Bearer`. A malformed-but-`bc_`-prefixed token is
     NEVER downgraded to anonymous (R5) -- only a bearer token that does not
     even look like ours (no `bc_live_`/`bc_test_` marker) falls through to
     the anonymous path, which is how the webhooks router's own
     `manage_token` bearer auth keeps working unmetered by this middleware
     (amendment A5).
  5. `suspended_at` set on the customer -> 403 `account_suspended`
     (amendment A12e).
  6. Subscription past the 7-day dunning window, or canceled/unpaid -> 402
     `payment_required` (amendment A3; with zero subscription rows in
     Phase 1 this branch of the code never actually fires yet).
  7. Per-CUSTOMER burst bucket (round-2 amendment D6: keyed on
     `cust:{customer_id}`, sized by plan -- two keys belonging to one
     customer share the same burst budget, not two independent ones)
     -> 429 `rate_limited`.
  8. A11's pre-check `SELECT` against `api_customer_usage` for today
     (round-2 amendment C1: quota is per CUSTOMER, not per key -- two keys
     belonging to one customer share one daily budget), compared against
     B6's EFFECTIVE limit (`floor(contractual * 1.10)`) -> 429
     `quota_exceeded`, `Retry-After` = seconds to the next UTC midnight.
  9. `call_next`.
 10. Post-response: ONE upsert into `api_customer_usage` (the C1/B6
     enforcement counter) + one into `api_key_usage` (a SECOND, per-key
     upsert in the same transaction, admin-usage reporting only) + one
     into `api_key_usage_subnets` (A6) -- skipped entirely for
     5xx/429/401/402/403 responses (a caller must not pay for our
     failures, and a request this middleware itself refused was never
     counted to begin with).

Headers (R4): keyed responses get the union of both header families --
`X-RateLimit-*` (burst) and `X-Quota-*` (daily quota) plus `X-Plan`.
Anonymous responses keep `X-RateLimit-*` only, exactly as before.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from billcommons_api.api_keys import parse_presented_key, resolve_key
from billcommons_schema.models import ApiKey, ApiKeyUsage, ApiKeyUsageSubnet
from billcommons_api.rate_limit import (
    _BULK_ACCESS_DOCS_URL,
    _BULK_ACCESS_MESSAGE,
    _EXEMPT_PATHS,
    _BoundedFixedWindowCounter,
    _FixedWindowCounter,
    _RouteTier,
    _is_heavy_route,
    client_ip,
    is_trusted_client,
    quota_bucket,
    subnet_bucket,
)
from billcommons_shared import plans
from billcommons_shared.db import get_session

logger = logging.getLogger(__name__)

# A5: path PREFIXES that do their own auth (cookie session / admin bearer
# token / Stripe signature) and must never be metered or rate-limited here.
_EXEMPT_PREFIXES = (
    "/api/v1/billing/webhook",
    "/api/v1/account",
    "/api/v1/billing",
    "/api/v1/admin",
    "/docs",
    "/openapi.json",
)

# R1: anonymous daily caps, env-tunable. Named to match the ops
# runbook/spec verbatim, same convention `rate_limit_subnet` etc. use in
# settings.py (no BILLCOMMONS_API_ prefix).
_DEFAULT_ANON_DAILY_LIMIT = 2_000
_DEFAULT_ANON_DAILY_SUBNET_LIMIT = 5_000
_ANON_DAILY_WINDOW_SECONDS = 86400.0

# R10: bounded dict, same "oldest-by-insertion eviction" idiom as the MCP
# server's own limiter (apps/mcp/billcommons_mcp/rate_limit.py) -- the
# daily window's sweep only runs once per day, so without a cap the dict
# grows once per distinct IP/subnet seen and is never reclaimed.
_MAX_TRACKED_ANON_BUCKETS = 100_000

# Looked up via module attribute so tests can point this module at a
# throwaway session factory (see apps/api/tests/test_quota.py).
_session_factory = get_session


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _next_utc_midnight(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)


def _seconds_to_utc_midnight(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return max(1, int((_next_utc_midnight(now) - now).total_seconds()))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_PATHS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _EXEMPT_PREFIXES)


def _extract_presented_key(request: Request) -> str | None:
    """R5/A12(h): `Authorization: Bearer` wins when both headers are
    present -- `X-Api-Key` is IGNORED whenever the Authorization header is
    present and uses the `Bearer` scheme, whether or not that bearer token
    happens to carry one of our own key markers. `X-Api-Key` is only
    consulted when Authorization is absent entirely OR uses some OTHER
    scheme (e.g. `Basic`) -- not merely when a Bearer token fails the
    marker check. This is what lets a bearer token that isn't
    `bc_live_`/`bc_test_`-prefixed (e.g. the webhooks router's own opaque
    `manage_token`) fall through to the anonymous path here (returning
    `None`, never a malformed-API-key 401) while leaving that other
    router's own auth untouched (amendment A5) -- without this function
    ever peeking at `X-Api-Key` in that case.

    (Item 21, doc-only: an earlier version of this docstring said
    `X-Api-Key` is consulted whenever the Bearer token "does not carry one
    of our own key markers" -- that's wrong; three independent reviewer
    legs read that sentence and filed the CODE as the bug. The code below
    already matches A12(h) as written above; only this comment was
    incorrect.)
    """
    auth = request.headers.get("authorization", "")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer":
            token = token.strip()
            return token if parse_presented_key(token) else None
    api_key_header = request.headers.get("x-api-key", "").strip()
    if api_key_header and parse_presented_key(api_key_header):
        return api_key_header
    return None


def _error_body(code: str, message: str, request_id: str, **extra) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id, **extra}}


class QuotaMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        limit: int,
        subnet_limit: int,
        heavy_limit: int,
        heavy_subnet_limit: int,
        window: float = 60.0,
        clock=time.monotonic,
        anon_daily_limit: int | None = None,
        anon_daily_subnet_limit: int | None = None,
    ):
        super().__init__(app)
        self._clock = clock
        self._default = _RouteTier("default", limit, subnet_limit, window, clock)
        self._heavy = _RouteTier("heavy", heavy_limit, heavy_subnet_limit, window, clock)

        anon_daily_limit = anon_daily_limit or _env_int(
            "BILLCOMMONS_ANON_DAILY_LIMIT", _DEFAULT_ANON_DAILY_LIMIT
        )
        anon_daily_subnet_limit = anon_daily_subnet_limit or _env_int(
            "BILLCOMMONS_ANON_DAILY_LIMIT_SUBNET", _DEFAULT_ANON_DAILY_SUBNET_LIMIT
        )
        self._anon_daily_ip = _BoundedFixedWindowCounter(
            anon_daily_limit, _ANON_DAILY_WINDOW_SECONDS, clock, _MAX_TRACKED_ANON_BUCKETS
        )
        self._anon_daily_subnet = _BoundedFixedWindowCounter(
            anon_daily_subnet_limit, _ANON_DAILY_WINDOW_SECONDS, clock, _MAX_TRACKED_ANON_BUCKETS
        )

        # Per-CUSTOMER burst bucket, one shared counter per plan (R1's
        # burst column). Amendment D6: keyed on `cust:{customer_id}`, NOT
        # key_id -- two keys belonging to one customer share one burst
        # budget, sized by whichever plan currently applies to that
        # customer's keys.
        self._burst_by_plan: dict[str, _FixedWindowCounter] = {
            plan: _FixedWindowCounter(plans.plan_limits(plan).burst_per_minute, 60.0, clock)
            for plan in plans.PLANS
        }

    # ---- anonymous path -----------------------------------------------------

    def _anon_rate_limit_response(
        self, request: Request, retry_after: int, counter: "_FixedWindowCounter | None" = None
    ) -> JSONResponse:
        # Item 14 fix: `counter` is the SPECIFIC tier that actually failed
        # (default-ip, default-subnet, an anon-daily-* tier, or a heavy
        # tier) -- the pre-fix version always rendered
        # `self._default.ip.headers(...)`, so a refusal from the DAILY tier
        # (2,000/day) still claimed `X-RateLimit-Limit` was the PER-MINUTE
        # ceiling, an internally incoherent pair (a per-minute limit
        # advertised alongside a ~21-hour `Retry-After`). Defaults to
        # `self._default.ip` only for callers that don't have a specific
        # failing counter to hand (there are none left in this module, but
        # keeping the default makes this safe to call generically).
        counter = counter or self._default.ip
        return JSONResponse(
            status_code=429,
            headers={
                "Retry-After": str(retry_after),
                "Cache-Control": "no-store",
                **counter.headers(0, retry_after),
            },
            content=_error_body(
                "rate_limited",
                _BULK_ACCESS_MESSAGE,
                request.headers.get("x-request-id", ""),
                retry_after=retry_after,
                docs=_BULK_ACCESS_DOCS_URL,
            ),
        )

    def _anon_tier_check(
        self, request: Request
    ) -> tuple[JSONResponse | None, tuple["_FixedWindowCounter", tuple[bool, int, int, int]]]:
        """Runs every anonymous IP/subnet/daily/heavy tier for this request
        and returns `(429_response_or_None, (binding_counter, its_result))` --
        the binding counter being the lowest-remaining bucket this request
        passed (see the success-headers comment below).

        Item 14 fix: tiers are now evaluated in order and STOP at the first
        failure. The pre-fix version built the whole `results` list eagerly
        (every `.allow()` called unconditionally), so a client already
        being 429'd by the per-minute tier kept burning its 2,000/day IP
        and 5,000/24 subnet daily budgets on every subsequent request in
        the same minute -- exhausting them (and everyone sharing its /24)
        before the minute-level throttle even lifted.

        Item 1 fix (other half, `api_keys.py` holds the cache-bounding
        half): this same method is now ALSO called from `_dispatch_keyed`
        before an unauthenticated 401 is returned, so probing garbage
        `bc_live_`/`bc_test_`-shaped bearer tokens costs the same anonymous
        IP/subnet budget as any other unauthenticated traffic, instead of
        being free to retry unboundedly.
        """
        ip = client_ip(request)
        ip_key = quota_bucket(ip)
        subnet_key = subnet_bucket(ip)
        path = request.url.path

        tiers = [
            ("default-ip", self._default.ip, ip_key),
            ("default-subnet", self._default.subnet, subnet_key),
            ("anon-daily-ip", self._anon_daily_ip, ip_key),
            ("anon-daily-subnet", self._anon_daily_subnet, subnet_key),
        ]
        if _is_heavy_route(path):
            tiers.append(("heavy-ip", self._heavy.ip, ip_key))
            tiers.append(("heavy-subnet", self._heavy.subnet, subnet_key))

        allowed_pairs: list[tuple["_FixedWindowCounter", tuple[bool, int, int, int]]] = []
        for name, counter, key in tiers:
            result = counter.allow(key)
            allowed, retry_after, _, _ = result
            if not allowed:
                # Item 14 fix: pass THIS tier's own counter so the response
                # headers describe the tier that actually refused, not
                # always the per-minute default-ip one.
                # (allowed_pairs[0] is default-ip when at least one tier ran;
                # on a first-tier refusal the caller never reads it.)
                first = allowed_pairs[0] if allowed_pairs else (counter, result)
                return self._anon_rate_limit_response(request, retry_after, counter), first
            allowed_pairs.append((counter, result))
        # Success headers must advertise the BINDING bucket -- the one with
        # the LOWEST remaining (a heavy route's own 60/minute bucket is what
        # a caller needs to see, not the 300/minute default it also passed).
        # Same rule as the pre-monetization RateLimitMiddleware's
        # success_binding (verify round 8155c04), which this middleware
        # replaces wholesale in app.py.
        binding = min(allowed_pairs, key=lambda pair: pair[1][2])
        return None, binding

    def _anon_tier_peek(self, request: Request) -> JSONResponse | None:
        """Fixlist item 4: non-mutating mirror of `_anon_tier_check`'s tier
        list, used by `_dispatch_keyed` to short-circuit BEFORE
        `resolve_key`'s DB round trip. Round-1 item 1 already stopped an
        unauthenticated garbage `bc_live_`/`bc_test_`-shaped bearer from
        being cached (so it can never occupy memory), and this middleware
        already charges the anonymous buckets for it once resolution
        fails -- but the ORDERING left every probe still costing one
        indexed SELECT even after that same IP/subnet was already over its
        anonymous limit and therefore guaranteed a 429 regardless. Peeking
        first (no bucket mutation, so it never double-charges) lets an
        already-saturated caller be refused for free."""
        ip = client_ip(request)
        ip_key = quota_bucket(ip)
        subnet_key = subnet_bucket(ip)
        path = request.url.path

        tiers = [
            (self._default.ip, ip_key),
            (self._default.subnet, subnet_key),
            (self._anon_daily_ip, ip_key),
            (self._anon_daily_subnet, subnet_key),
        ]
        if _is_heavy_route(path):
            tiers.append((self._heavy.ip, ip_key))
            tiers.append((self._heavy.subnet, subnet_key))

        for counter, key in tiers:
            # Deployed `peek` (check-all-then-increment rework, round
            # 8155c04) returns the same 4-tuple shape as `allow`:
            # (allowed, retry_after, remaining, reset_in). "Would a hit be
            # admitted right now" is the question this short-circuit wants.
            allowed, retry_after, _, _ = counter.peek(key)
            if not allowed:
                return self._anon_rate_limit_response(request, retry_after, counter)
        return None

    async def _dispatch_anonymous(self, request: Request, call_next):
        failed_response, default_result = self._anon_tier_check(request)
        if failed_response is not None:
            return failed_response

        response = await call_next(request)
        binding_counter, (_, _, remaining, reset_in) = default_result
        response.headers.update(binding_counter.headers(remaining, reset_in))
        return response

    # ---- keyed path ---------------------------------------------------------

    def _read_usage(self, db, customer_id) -> tuple[int, int]:
        # C1: quota is enforced per CUSTOMER, not per key -- two keys
        # belonging to the same customer share one daily budget.
        row = db.execute(
            text(
                "SELECT requests, heavy_requests FROM api_customer_usage "
                "WHERE customer_id = :customer_id AND usage_date = :usage_date"
            ),
            {"customer_id": str(customer_id), "usage_date": _utc_today().isoformat()},
        ).first()
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])

    def _read_usage_threadpool(self, customer_id) -> tuple[int, int]:
        """Item 3 fix: owns its own session open/close, run entirely
        inside `run_in_threadpool` so the pre-check SELECT never blocks
        the event-loop thread `QuotaMiddleware.dispatch` runs on."""
        db = _session_factory()
        try:
            return self._read_usage(db, customer_id)
        finally:
            db.close()

    # Item 16: throttle -- only touch `api_keys.last_used_at` when it's
    # stale by more than this, so a busy key doesn't UPDATE that row on
    # every single request.
    _LAST_USED_AT_STALE_AFTER = timedelta(minutes=5)

    def _record_usage(self, db, customer_id, key_id, heavy: bool) -> tuple[int, int]:
        # C1/B6: the CUSTOMER-level counter is the one enforcement and the
        # X-Quota-* headers read -- one statement, both columns move
        # atomically. The per-key row (api_key_usage) is a SECOND upsert in
        # the SAME transaction/session, for admin-usage reporting only; it
        # never gates anything.
        #
        # Item 19 fix: `RETURNING requests, heavy_requests` on the
        # CUSTOMER-level upsert -- the pre-fix version instead incremented
        # local Python counters (`requests_today += 1`) in the caller, so
        # under concurrency (two requests for the same customer racing this
        # upsert) the X-Quota-* headers on either response could report a
        # remaining count that was already stale by the time it was sent.
        # Reading the actual post-upsert row is exact.
        params = {
            "usage_date": _utc_today().isoformat(),
            "heavy": 1 if heavy else 0,
        }
        customer_row = db.execute(
            text(
                "INSERT INTO api_customer_usage (customer_id, usage_date, requests, heavy_requests) "
                "VALUES (:customer_id, :usage_date, 1, :heavy) "
                "ON CONFLICT (customer_id, usage_date) DO UPDATE "
                "SET requests = api_customer_usage.requests + 1, "
                "heavy_requests = api_customer_usage.heavy_requests + EXCLUDED.heavy_requests "
                "RETURNING requests, heavy_requests"
            ),
            {**params, "customer_id": str(customer_id)},
        ).first()
        # Fixlist item 7: this upsert used to be the same raw `text()`
        # form as the customer-level one above, binding `str(key_id)` --
        # the DASHED UUID string. Postgres's native `uuid` column accepts
        # either form so this was invisible in production, but under the
        # SQLite test harness `postgresql.UUID(as_uuid=True)` degrades to
        # SQLAlchemy's generic `Uuid`, which stores (and, via its type
        # processor, only matches) the HEX form -- exactly like the
        # `last_used_at` fix's own comment above explains for `ApiKey.id`.
        # `api_key_usage.key_id` never joined an ORM-inserted `ApiKey.id`
        # under SQLite, so `GET /account/me`'s per-key `usage_today` and
        # the admin report's per-key `requests` always read back 0.
        # Fixed the same way item 19 already fixed `last_used_at`: a
        # typed SQLAlchemy Core construct (dialect-dispatched
        # `insert(...).on_conflict_do_update(...)`, the same pattern
        # `_upsert_customer_by_email` already uses) instead of raw SQL, so
        # the UUID bind goes through the column's own type processor.
        dialect_name = db.get_bind().dialect.name
        insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
        key_usage_stmt = insert_fn(ApiKeyUsage).values(
            key_id=key_id,
            usage_date=_utc_today(),
            requests=1,
            heavy_requests=1 if heavy else 0,
        )
        key_usage_stmt = key_usage_stmt.on_conflict_do_update(
            index_elements=["key_id", "usage_date"],
            set_={
                "requests": ApiKeyUsage.requests + 1,
                "heavy_requests": ApiKeyUsage.heavy_requests + key_usage_stmt.excluded.heavy_requests,
            },
        )
        db.execute(key_usage_stmt)
        # Item 16 fix: `api_keys.last_used_at` is surfaced by `GET /me` and
        # rendered by the account page, but was written by nothing anywhere
        # in the tree -- it read `null` forever. Written here, in the same
        # already-existing post-response accounting transaction, throttled
        # to once per 5 minutes per key so a busy key doesn't add an UPDATE
        # to every single request.
        #
        # Uses SQLAlchemy Core's `update()` (typed, ORM-mapped), not a raw
        # `text()` string, specifically so the `ApiKey.id` UUID bind param
        # goes through that column's own type processor -- a raw `text()`
        # query comparing against a plain `str(key_id)` would use the
        # dashed UUID string form, which never matches this codebase's
        # character-based (SQLite) UUID storage format (`.hex`, no dashes;
        # see `_monetization_sqlite.py`'s own `gen_random_uuid` comment).
        # Postgres's native `uuid` column accepts either form, so this only
        # bites under the SQLite test harness -- exactly where this fix's
        # own regression test lives.
        now = datetime.now(timezone.utc)
        stale_before = now - self._LAST_USED_AT_STALE_AFTER
        db.execute(
            sa_update(ApiKey)
            .where(ApiKey.id == key_id)
            .where((ApiKey.last_used_at.is_(None)) | (ApiKey.last_used_at < stale_before))
            .values(last_used_at=now)
        )
        return int(customer_row[0]), int(customer_row[1])

    def _record_subnet(self, db, key_id, subnet: str) -> None:
        # A6: key-sharing telemetry, upserted alongside the usage row.
        # Fixlist item 7: same dashed-vs-hex UUID bind hazard as
        # `_record_usage`'s `api_key_usage` upsert above -- fixed the same
        # way (dialect-dispatched Core `insert(...).on_conflict_do_update`,
        # UUID bind goes through the column's type processor). Without
        # this, `distinct_subnets` (A6/R13's key-sharing signal) always
        # read back 0 under the SQLite harness.
        dialect_name = db.get_bind().dialect.name
        insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
        subnet_stmt = insert_fn(ApiKeyUsageSubnet).values(
            key_id=key_id,
            usage_date=_utc_today(),
            subnet=subnet,
            requests=1,
        )
        subnet_stmt = subnet_stmt.on_conflict_do_update(
            index_elements=["key_id", "usage_date", "subnet"],
            set_={"requests": ApiKeyUsageSubnet.requests + 1},
        )
        db.execute(subnet_stmt)

    def _record_usage_and_subnet_threadpool(
        self, customer_id, key_id, heavy: bool, subnet: str
    ) -> tuple[int, int]:
        """Item 3 fix: owns its own session open/commit/close, run
        entirely inside `run_in_threadpool` -- see `_read_usage_threadpool`
        above for why. Item 2's error isolation (the caller's try/except
        around this call) is unaffected: an exception here still
        propagates to that same `except Exception` block."""
        db = _session_factory()
        try:
            requests_today, heavy_today = self._record_usage(db, customer_id, key_id, heavy)
            self._record_subnet(db, key_id, subnet)
            db.commit()
            return requests_today, heavy_today
        finally:
            db.close()

    async def _dispatch_keyed(self, request: Request, call_next, presented: str):
        request_id = request.headers.get("x-request-id", "")
        try:
            resolved = await run_in_threadpool(resolve_key, presented)
        except Exception:
            logger.exception("API-key resolution failed -- failing closed")
            return JSONResponse(
                status_code=503,
                headers={"Cache-Control": "no-store"},
                content=_error_body(
                    "quota_unavailable",
                    "Quota check is temporarily unavailable. Please retry shortly.",
                    request_id,
                ),
            )
        if resolved is None or not resolved.is_usable():
            # A valid key must never be gated by anonymous saturation. The
            # peek is solely a probe-DoS optimization after resolution fails.
            already_over = self._anon_tier_peek(request)
            if already_over is not None:
                return already_over
            # Item 1 fix (other half -- api_keys.py holds the cache-bounding
            # half): an unauthenticated bearer that looks like one of our
            # keys is charged against the SAME anonymous IP/subnet buckets
            # a keyless caller would hit, before the 401 is returned. Pre-fix,
            # this branch never touched a single counter -- a remote party
            # could probe unlimited garbage `bc_live_`/`bc_test_`-shaped
            # tokens at full request rate, unmetered by construction. Now,
            # once probing exceeds the anonymous rate limits, it gets 429
            # like everything else instead of an unlimited stream of 401s.
            anon_failed, _ = self._anon_tier_check(request)
            if anon_failed is not None:
                return anon_failed
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                content=_error_body(
                    "invalid_api_key", "Unknown or revoked API key.", request_id
                ),
            )
        if resolved.customer_suspended_at is not None:
            return JSONResponse(
                status_code=403,
                headers={"Cache-Control": "no-store"},
                content=_error_body(
                    "account_suspended",
                    "This account has been suspended. Contact sales@billcommons.org.",
                    request_id,
                ),
            )
        if resolved.payment_required():
            return JSONResponse(
                status_code=402,
                headers={"Cache-Control": "no-store"},
                content=_error_body(
                    "payment_required",
                    "Your subscription is past due. Visit https://billcommons.org/account "
                    "to update billing.",
                    request_id,
                ),
            )

        # D6: per-CUSTOMER burst bucket, sized by the key's current plan.
        #
        # Fixlist item 17 (documented, deliberately left as-is): the burst
        # bucket is charged HERE, before the daily-quota pre-check below.
        # A customer already over their daily quota therefore keeps
        # consuming burst on every rejected call and can see `rate_limited`
        # interleaved with `quota_exceeded` until the burst window clears.
        # Both are 429 and neither is metered against `api_customer_usage`,
        # so the only customer-visible effect is a confusing error code
        # while waiting for UTC midnight -- not a correctness or billing
        # issue. Left in this order on purpose: throttling a client that
        # is spinning on 429s (which is exactly what "already over daily
        # quota" traffic tends to do) is arguably the more useful behavior
        # of the two, and swapping the order would cost an extra DB round
        # trip (the pre-check SELECT) on every burst-limited request
        # instead of stopping it in-process first.
        burst_counter = self._burst_by_plan.get(resolved.plan, self._burst_by_plan[plans.PLAN_DEVELOPER])
        allowed, retry_after, remaining_burst, reset_in_burst = burst_counter.allow(
            f"cust:{resolved.customer_id}"
        )
        if not allowed:
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "Cache-Control": "no-store",
                    **burst_counter.headers(0, retry_after),
                },
                content=_error_body(
                    "rate_limited",
                    "Per-key burst limit exceeded.",
                    request_id,
                    retry_after=retry_after,
                ),
            )

        contractual_req = plans.contractual_request_limit(
            resolved.plan, resolved.active_extra_requests_per_day
        )
        contractual_heavy = plans.contractual_heavy_limit(
            resolved.plan, resolved.active_extra_heavy_per_day
        )
        effective_req = plans.effective_request_limit(
            resolved.plan, resolved.active_extra_requests_per_day
        )
        effective_heavy = plans.effective_heavy_limit(
            resolved.plan, resolved.active_extra_heavy_per_day
        )
        is_heavy = _is_heavy_route(request.url.path)

        # Item 3 fix: `run_in_threadpool` -- this pre-check SELECT is
        # synchronous libpq/SQLAlchemy I/O running inside `async def
        # dispatch` (`BaseHTTPMiddleware` runs the whole middleware chain
        # on the event-loop thread). Every route handler in this codebase
        # is a sync `def` and therefore ALREADY runs in AnyIO's
        # threadpool via Starlette's own dispatch machinery -- this
        # middleware was the one place still blocking the loop thread
        # itself, so one slow/contended keyed request stalled anonymous
        # traffic, `/health`, and every other keyed caller behind it on
        # `numReplicas=1`.
        #
        # Item 13 fix: this pre-check read had a bare `try/finally` with
        # no `except` -- a transient DB failure here (unlike the
        # post-response accounting block below, which item 2 already
        # isolated) raised OUT of `dispatch`, past Starlette's
        # `ExceptionMiddleware` (where `register_exception_handlers`
        # lives), and surfaced as a bare un-enveloped 500. Fail CLOSED
        # instead: a clean `503 quota_unavailable` in this API's own error
        # shape. Anonymous traffic is unaffected either way -- only the
        # keyed path touches this table.
        try:
            requests_today, heavy_today = await run_in_threadpool(
                self._read_usage_threadpool, resolved.customer_id
            )
        except Exception:
            logger.exception(
                "pre-check quota read failed for customer %s -- failing closed",
                resolved.customer_id,
            )
            return JSONResponse(
                status_code=503,
                headers={"Cache-Control": "no-store"},
                content=_error_body(
                    "quota_unavailable",
                    "Quota check is temporarily unavailable. Please retry shortly.",
                    request_id,
                ),
            )

        if requests_today >= effective_req or (is_heavy and heavy_today >= effective_heavy):
            retry_after = _seconds_to_utc_midnight()
            quota_reset = int(_next_utc_midnight().timestamp())
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "Cache-Control": "no-store",
                    "X-Quota-Limit": str(contractual_req),
                    "X-Quota-Remaining": "0",
                    "X-Quota-Reset": str(quota_reset),
                    "X-Quota-Heavy-Limit": str(contractual_heavy),
                    "X-Quota-Heavy-Remaining": str(max(0, contractual_heavy - heavy_today)),
                    "X-Plan": resolved.plan,
                },
                content=_error_body(
                    "quota_exceeded",
                    _BULK_ACCESS_MESSAGE,
                    request_id,
                    retry_after=retry_after,
                    docs=_BULK_ACCESS_DOCS_URL,
                ),
            )

        response = await call_next(request)
        status = response.status_code
        if status < 500 and status not in (429, 401, 402, 403):
            # Item 2 fix: the whole post-response accounting block is now
            # wrapped in try/except. Pre-fix, `_record_usage`/`_record_subnet`/
            # `db.commit()` ran with NO error isolation after `call_next` --
            # a transient DB failure here (deadlock, pool exhaustion,
            # failover) raised OUT of `dispatch` and replaced an
            # already-computed, already-served-to-the-caller successful
            # response with a 500. The handler's side effects had already
            # happened and the customer wasn't even metered for it either.
            # Metering must never fail the request it is metering.
            subnet = subnet_bucket(client_ip(request))
            try:
                # Item 3 fix: same `run_in_threadpool` treatment as the
                # pre-check read above -- two upserts plus a commit is
                # synchronous I/O that must not run on the event-loop
                # thread either.
                requests_today, heavy_today = await run_in_threadpool(
                    self._record_usage_and_subnet_threadpool,
                    resolved.customer_id,
                    resolved.key_id,
                    is_heavy,
                    subnet,
                )
            except Exception:
                logger.exception(
                    "post-response metering failed for customer %s -- response "
                    "already served, not counted against quota",
                    resolved.customer_id,
                )

        quota_reset = int(_next_utc_midnight().timestamp())
        response.headers.update(burst_counter.headers(remaining_burst, reset_in_burst))
        response.headers.update(
            {
                "X-Quota-Limit": str(contractual_req),
                "X-Quota-Remaining": str(max(0, contractual_req - requests_today)),
                "X-Quota-Reset": str(quota_reset),
                "X-Quota-Heavy-Limit": str(contractual_heavy),
                "X-Quota-Heavy-Remaining": str(max(0, contractual_heavy - heavy_today)),
                "X-Plan": resolved.plan,
            }
        )
        return response

    # ---- entry point ----------------------------------------------------------

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or _is_exempt(path) or is_trusted_client(request):
            return await call_next(request)

        presented = _extract_presented_key(request)
        if presented is None:
            return await self._dispatch_anonymous(request, call_next)
        return await self._dispatch_keyed(request, call_next, presented)
