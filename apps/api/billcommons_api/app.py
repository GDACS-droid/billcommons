"""FastAPI app factory for the Bill Commons public REST API."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from billcommons_api.rate_limit import RateLimitMiddleware
from slowapi.util import get_remote_address

from billcommons_api.errors import register_exception_handlers
from billcommons_api.middleware import RequestIDMiddleware, SecureHeadersMiddleware
from billcommons_api.routers import (
    alerts,
    bills,
    changes,
    committees,
    coverage,
    events,
    health,
    jurisdictions,
    people,
    search,
    sessions,
    sitemap,
    sources,
    stats,
    topics,
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
    app.add_middleware(RateLimitMiddleware, limit=rate_limit_num, window=60.0)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        # POST exists for exactly two browser-facing writes: /bills/lookup
        # (large watchlists overflow a query string) and /alerts/subscribe.
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    for router in (
        alerts.router,
        health.router,
        jurisdictions.router,
        sessions.router,
        bills.router,
        people.router,
        committees.router,
        events.router,
        search.router,
        changes.router,
        sources.router,
        coverage.router,
        sitemap.router,
        stats.router,
        topics.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    return app
