"""/api/v1/admin -- founder-only usage reporting (2026-08-21 monetization
spec, R13/base spec §7). `Authorization: Bearer $BILLCOMMONS_ADMIN_TOKEN`,
constant-time compare, **404 (not 401) on a bad token** so the endpoint is
invisible to anything probing for it. No admin UI; SQL snippets live in
`docs/operations/monetization-runbook.md`.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from starlette.responses import JSONResponse

from billcommons_schema.models import ApiCustomer, ApiKey, ApiKeyUsage, ApiKeyUsageSubnet
from billcommons_shared import plans
from billcommons_shared.db import get_session

router = APIRouter(prefix="/admin", tags=["admin"])


def _not_found() -> JSONResponse:
    # Deliberately the SAME shape a genuinely missing route would return --
    # a bad token must not be distinguishable from "this path doesn't exist".
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "not_found", "message": "Not found."}},
        headers={"Cache-Control": "no-store"},
    )


def _check_admin_token(request: Request) -> bool:
    expected = os.environ.get("BILLCOMMONS_ADMIN_TOKEN", "")
    if not expected:
        return False
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    # Item 11 fix: `hmac.compare_digest(str, str)` raises `TypeError` if
    # either argument contains a non-ASCII-compatible character --
    # `Authorization: Bearer café` 500'd instead of returning this module's
    # own deliberate "bad token looks like a missing route" 404. Comparing
    # the UTF-8 bytes instead accepts any input without ever raising.
    return hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass")
    )


@router.get("/usage")
def usage_report(request: Request, days: int = Query(default=7, ge=1, le=90)):
    if not _check_admin_token(request):
        return _not_found()

    db = get_session()
    try:
        # Item 13 fix: `usage_date >= since` is inclusive of `since` itself,
        # so `today - timedelta(days=days)` spanned `days + 1` calendar days
        # (e.g. `days=7` counted today plus the 7 days before it = 8 days)
        # while the denominator below is `requests_per_day * days` -- at the
        # default 7 that inflated `pct_of_quota` (and the `distinct_subnets`
        # key-sharing flag) by ~14%. `days - 1` makes the window exactly
        # `days` calendar days inclusive of today.
        since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        rows = db.execute(
            select(
                ApiKey.id,
                ApiKey.key_prefix,
                ApiKey.plan,
                ApiKey.status,
                ApiCustomer.email,
                func.coalesce(func.sum(ApiKeyUsage.requests), 0).label("requests"),
                func.coalesce(func.sum(ApiKeyUsage.heavy_requests), 0).label("heavy_requests"),
            )
            .join(ApiCustomer, ApiCustomer.id == ApiKey.customer_id)
            .outerjoin(
                ApiKeyUsage,
                (ApiKeyUsage.key_id == ApiKey.id) & (ApiKeyUsage.usage_date >= since),
            )
            .group_by(ApiKey.id, ApiKey.key_prefix, ApiKey.plan, ApiKey.status, ApiCustomer.email)
        ).all()

        # Window-total distinct subnets (unchanged) -- reporting only, not
        # what the flag is based on (see below).
        subnet_counts = dict(
            db.execute(
                select(ApiKeyUsageSubnet.key_id, func.count(func.distinct(ApiKeyUsageSubnet.subnet)))
                .where(ApiKeyUsageSubnet.usage_date >= since)
                .group_by(ApiKeyUsageSubnet.key_id)
            ).all()
        )

        # Fixlist item 12 fix: `SPEC-LOCKED.md` R13 defines the key-sharing
        # signal as ">20 distinct /24 per key PER DAY", and A6 says admin
        # usage reads `count(distinct subnet)` against the per-(key, day,
        # subnet) table -- i.e. per-day, then take the max across the
        # window, not one `count(distinct subnet)` over the WHOLE window.
        # Pre-fix, a legitimate key seen from 4 disjoint /24s each day
        # (no single day over the 20 threshold) could still accumulate
        # `days * 4` distinct subnets across a 7-day window and trip a
        # FALSE key-sharing flag. Two-step aggregation: per-(key, day)
        # distinct count first, then MAX per key across the window.
        per_day_subquery = (
            select(
                ApiKeyUsageSubnet.key_id.label("key_id"),
                ApiKeyUsageSubnet.usage_date.label("usage_date"),
                func.count(func.distinct(ApiKeyUsageSubnet.subnet)).label("daily_distinct"),
            )
            .where(ApiKeyUsageSubnet.usage_date >= since)
            .group_by(ApiKeyUsageSubnet.key_id, ApiKeyUsageSubnet.usage_date)
            .subquery()
        )
        max_daily_subnet_counts = dict(
            db.execute(
                select(
                    per_day_subquery.c.key_id,
                    func.max(per_day_subquery.c.daily_distinct),
                ).group_by(per_day_subquery.c.key_id)
            ).all()
        )

        result = []
        for key_id, key_prefix, plan, status, email, requests, heavy_requests in rows:
            limit = plans.plan_limits(plan).requests_per_day * days
            pct = round(100.0 * requests / limit, 1) if limit else 0.0
            distinct_subnets = subnet_counts.get(key_id, 0)
            max_daily_distinct_subnets = max_daily_subnet_counts.get(key_id, 0)
            result.append(
                {
                    "key_prefix": key_prefix,
                    "email": email,
                    "plan": plan,
                    "status": status,
                    "requests": int(requests),
                    "heavy_requests": int(heavy_requests),
                    "pct_of_quota": pct,
                    # Window total (reporting only -- NOT the flag basis).
                    "distinct_subnets": distinct_subnets,
                    # R13's actual signal: the single worst day in the window.
                    "max_daily_distinct_subnets": max_daily_distinct_subnets,
                    # A6/R13: flag only, never auto-blocked.
                    "key_sharing_flag": max_daily_distinct_subnets > 20,
                }
            )
        return {"days": days, "keys": result}
    finally:
        db.close()
