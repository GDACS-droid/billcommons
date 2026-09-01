"""FastAPI app factory for the Bill Commons public REST API."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from billcommons_api.quota import QuotaMiddleware
from slowapi.util import get_remote_address

from billcommons_api.concurrency import ConcurrencyLimitMiddleware
from billcommons_api.errors import register_exception_handlers
from billcommons_api.middleware import (
    AccountCorsMiddleware,
    RequestIDMiddleware,
    SecureHeadersMiddleware,
)
from billcommons_api.routers import (
    account,
    admin,
    alerts,
    billing,
    bills,
    changes,
    committees,
    coverage,
    events,
    feedback,
    feeds,
    health,
    jurisdictions,
    people,
    search,
    sessions,
    sitemap,
    scout,
    sources,
    stats,
    topics,
    webhooks,
)
from billcommons_api.settings import get_settings

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)

# Item 20 fix: `ACCOUNT_SESSION_SECRET` and `BILLCOMMONS_REVEAL_KEY` were
# previously read lazily, at first use (`routers.account._session_secret`,
# `api_keys._get_fernet`) -- a missing env var surfaced as a runtime 500 on
# the first login/mint/reveal request rather than at boot. Both already
# fail CLOSED (no weak-secret fallback), so this is an availability fix,
# not a security one: validated here so the operator finds out from a
# startup log at deploy time, not a customer's bug report. Logs at ERROR
# rather than raising -- some processes that import `create_app` (this
# repo's own test suite; a future read-only worker) legitimately never
# touch the account/key-reveal paths.
_REQUIRED_MONETIZATION_ENV = (
    "ACCOUNT_SESSION_SECRET",
    "BILLCOMMONS_REVEAL_KEY",
    # Fixlist item 23 (2026-08-21 fix pass): these were previously only
    # validated lazily inside `billing._configure_stripe`/`_price_id`,
    # which -- worse than the account/key case above -- raised an
    # HTTPException(400) from INSIDE the Stripe webhook handler, bypassing
    # the `_PermanentWebhookError` classification entirely (see billing.py
    # item 23's own fix) and leaving Stripe retrying a misconfiguration
    # that can never fix itself without an operator noticing. Same shape
    # of fix: log at ERROR at boot so the operator finds out from a
    # startup log, not a Stripe retry storm.
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_BUILDER_MONTHLY",
    "STRIPE_PRICE_SCALE_MONTHLY",
    # Item 11 fix (round-2 fixlist): the annual price envs were missing
    # here -- round-1 item 20's new toggle (`PricingTiersTable.tsx`) made
    # them reachable from the UI for the first time, but boot never warned
    # about a missing one, so every annual "Subscribe" click silently
    # 500'd (E7: `_price_id` now raises `_ConfigurationError`, not a 400)
    # instead of the operator finding out from a startup log.
    "STRIPE_PRICE_BUILDER_ANNUAL",
    "STRIPE_PRICE_SCALE_ANNUAL",
    "STRIPE_PRICE_SNAPSHOT_STATE",
    "STRIPE_PRICE_SNAPSHOT_FULL",
)


def _validate_monetization_env() -> None:
    for name in _REQUIRED_MONETIZATION_ENV:
        if not os.environ.get(name):
            logger.error(
                "%s is not set -- magic-link login / API key minting / key "
                "reveal will fail at runtime the first time they are used. "
                "Set it before this instance takes traffic.",
                name,
            )


def create_app() -> FastAPI:
    settings = get_settings()
    _validate_monetization_env()

    # Keep a slowapi Limiter available for any future per-route decorators, but
    # global enforcement is handled by RateLimitMiddleware below (slowapi's
    # application_limits + middleware did not reliably throttle in practice).
    limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit_default])
    rate_limit_num = int(settings.rate_limit_default.split("/")[0])
    rate_limit_subnet_num = int(settings.rate_limit_subnet.split("/")[0])
    rate_limit_heavy_num = int(settings.rate_limit_heavy.split("/")[0])
    rate_limit_heavy_subnet_num = int(settings.rate_limit_heavy_subnet.split("/")[0])

    app = FastAPI(
        title=settings.title,
        description=settings.description,
        version=settings.api_version,
        openapi_version="3.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        servers=[
            {"url": "https://api.billcommons.org", "description": "production"},
            {"url": "http://localhost:8000", "description": "local development"},
        ],
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    register_exception_handlers(app)

    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(SecureHeadersMiddleware, api_version=settings.api_version)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        QuotaMiddleware,
        limit=rate_limit_num,
        subnet_limit=rate_limit_subnet_num,
        heavy_limit=rate_limit_heavy_num,
        heavy_subnet_limit=rate_limit_heavy_subnet_num,
        window=60.0,
    )
    # Outermost of the two limiters: a request rejected for overload should not
    # first consume a rate-limit token, and shedding should happen before any
    # per-request setup. Starlette applies middleware in reverse order of
    # registration, so registering this AFTER the rate limiter puts it in front.
    app.add_middleware(ConcurrencyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        # POST exists for exactly three browser-facing writes: /bills/lookup
        # (large watchlists overflow a query string), /alerts/subscribe and
        # /feedback. DELETE is for /webhooks/{id} -- a bearer-token-authed
        # management action, not anonymous like the other three, but still a
        # browser fetch() if a subscriber manages it from a page.
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )
    # C10: /api/v1/account/* and /api/v1/billing/* are cookie-authenticated
    # and need CORS WITH credentials, which the global CORSMiddleware above
    # (allow_origins=["*"], no credentials) cannot serve -- see
    # AccountCorsMiddleware's own docstring. Registered LAST so it is
    # OUTERMOST, wrapping (and, for its two prefixes, overriding) the global
    # CORSMiddleware's headers.
    app.add_middleware(AccountCorsMiddleware)

    for router in (
        account.router,
        admin.router,
        alerts.router,
        billing.router,
        feedback.router,
        health.router,
        jurisdictions.router,
        sessions.router,
        bills.router,
        people.router,
        committees.router,
        events.router,
        search.router,
        changes.router,
        feeds.router,
        sources.router,
        coverage.router,
        sitemap.router,
        stats.router,
        topics.router,
        webhooks.router,
        scout.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    return app
