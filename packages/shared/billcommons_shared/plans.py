"""Plan/tier limits for the Bill Commons API (2026-08-21 monetization spec,
`SPEC-LOCKED.md` R1). Single source of truth for both `billcommons_api.quota`
(enforcement) and any web/docs copy that needs to state a number.

Anonymous is NOT a plan here -- its daily caps are env-tunable
(`BILLCOMMONS_ANON_DAILY_LIMIT` / `BILLCOMMONS_ANON_DAILY_LIMIT_SUBNET`) and
live in `billcommons_api.quota`, not this module, because they are keyed on
IP/subnet rather than a customer row.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

PLAN_DEVELOPER = "developer"
PLAN_BUILDER = "builder"
PLAN_SCALE = "scale"
PLAN_ENTERPRISE = "enterprise"

PLANS = (PLAN_DEVELOPER, PLAN_BUILDER, PLAN_SCALE, PLAN_ENTERPRISE)


@dataclass(frozen=True)
class PlanLimits:
    requests_per_day: int
    heavy_per_day: int
    burst_per_minute: int


# R1's table. Enterprise has no fixed ceiling in the pricing sheet ("custom")
# -- these are a generous default so the enforcement code has SOMETHING to
# apply until an Enterprise customer's contract terms are set individually
# via `api_customers.extra_requests_per_day`/`extra_heavy_per_day`.
PLAN_LIMITS: dict[str, PlanLimits] = {
    PLAN_DEVELOPER: PlanLimits(requests_per_day=5_000, heavy_per_day=500, burst_per_minute=120),
    PLAN_BUILDER: PlanLimits(requests_per_day=50_000, heavy_per_day=5_000, burst_per_minute=600),
    PLAN_SCALE: PlanLimits(requests_per_day=500_000, heavy_per_day=100_000, burst_per_minute=2_400),
    PLAN_ENTERPRISE: PlanLimits(
        requests_per_day=2_000_000, heavy_per_day=400_000, burst_per_minute=6_000
    ),
}

# B6: the silent 10% grace. X-Quota-Limit headers always show the
# CONTRACTUAL limit; the ENFORCEMENT ceiling is floor(limit * 1.10) for both
# total and heavy requests. The grace never appears in a header.
GRACE_FACTOR = 1.10


def plan_limits(plan: str) -> PlanLimits:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS[PLAN_DEVELOPER])


def contractual_request_limit(plan: str, extra_requests_per_day: int = 0) -> int:
    """The number shown in X-Quota-Limit -- plan limit plus any active
    founder override (amendment A12e: `extra_requests_per_day` applies only
    while `override_expires_at > now()`; callers pass 0 when the override
    has lapsed)."""
    return plan_limits(plan).requests_per_day + max(0, extra_requests_per_day)


def contractual_heavy_limit(plan: str, extra_heavy_per_day: int = 0) -> int:
    return plan_limits(plan).heavy_per_day + max(0, extra_heavy_per_day)


def effective_request_limit(plan: str, extra_requests_per_day: int = 0) -> int:
    """The number actually enforced -- contractual limit + the silent grace
    (B6). Never surfaced in a response header."""
    return math.floor(contractual_request_limit(plan, extra_requests_per_day) * GRACE_FACTOR)


def effective_heavy_limit(plan: str, extra_heavy_per_day: int = 0) -> int:
    return math.floor(contractual_heavy_limit(plan, extra_heavy_per_day) * GRACE_FACTOR)


def burst_limit(plan: str) -> int:
    return plan_limits(plan).burst_per_minute
