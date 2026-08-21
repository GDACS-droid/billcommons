"""FastAPI app factory for the Bill Commons public REST API."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from billcommons_api.rate_limit import RateLimitMiddleware
from slowapi.util import get_remote_address

from billcommons_api.concurrency import ConcurrencyLimitMiddleware
from billcommons_api.errors import register_exception_handlers
from billcommons_api.middleware import RequestIDMiddleware, SecureHeadersMiddleware
from billcommons_api.routers import (
    alerts,
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
    sources,
    stats,
    topics,
    webhooks,
)
from billcommons_api.settings import get_settings

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()

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
        RateLimitMiddleware,
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

    for router in (
        alerts.router,
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
    ):
        app.include_router(router, prefix=API_PREFIX)

    return app
