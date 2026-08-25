"""Request-ID, secure-headers, and credentialed-CORS middleware."""
from __future__ import annotations

import os
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attaches a request_id (from X-Request-ID if provided, else generated)
    to request.state and echoes it back in the response headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class SecureHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline secure headers + API-version header for a public, read-only API."""

    def __init__(self, app, api_version: str) -> None:
        super().__init__(app)
        self._api_version = api_version

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
        response.headers["API-Version"] = self._api_version
        return response


class AccountCorsMiddleware(BaseHTTPMiddleware):
    """Round-2 amendment C10: `/api/v1/account/*` and `/api/v1/billing/*`
    are cookie-authenticated (the session cookie, `routers/account.py`),
    which needs CORS *with credentials* -- and the Fetch spec forbids a
    credentialed response from ever carrying `Access-Control-Allow-Origin:
    *` (browsers reject it client-side no matter what the server sends).
    The REST of this API stays open (`allow_origins=["*"]`, no
    credentials) via the app's own `CORSMiddleware` -- this middleware
    only overrides behavior for the two cookie-authenticated prefixes,
    echoing back the caller's `Origin` (only if it is on the
    `BILLCOMMONS_ALLOWED_ORIGINS` allowlist) instead of `*`.

    Registered OUTSIDE (added after, so outermost around) the app's
    `CORSMiddleware`: for a matched prefix, this strips whatever
    `Access-Control-Allow-Origin` the inner `CORSMiddleware` already set
    and replaces it with the restricted value, and answers the CORS
    preflight (`OPTIONS`) directly rather than letting it reach any inner
    middleware or route.
    """

    _PREFIXES = ("/api/v1/account", "/api/v1/billing")

    def _matches(self, path: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for prefix in self._PREFIXES)

    def _allowed_origins(self) -> set[str]:
        raw = os.environ.get(
            "BILLCOMMONS_ALLOWED_ORIGINS", "https://billcommons.org,https://www.billcommons.org"
        )
        return {o.strip() for o in raw.split(",") if o.strip()}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._matches(request.url.path):
            return await call_next(request)

        origin = request.headers.get("origin")
        allowed = origin is not None and origin in self._allowed_origins()

        if request.method == "OPTIONS":
            headers = {"Cache-Control": "no-store", "Vary": "Origin"}
            if allowed:
                headers.update(
                    {
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type",
                    }
                )
            return Response(status_code=204, headers=headers)

        response = await call_next(request)
        if "access-control-allow-origin" in response.headers:
            del response.headers["access-control-allow-origin"]
        # Item 15 fix: `.append`, not assignment -- GZipMiddleware (innermost,
        # runs inside `call_next` above) already set `Vary: Accept-Encoding`
        # on this response. Assigning `response.headers["Vary"] = "Origin"`
        # REPLACED that value outright, destroying `Accept-Encoding` on every
        # `/account` and `/billing` response (only masked today by the
        # separate `Cache-Control: no-store` on those routes). `.append` on
        # Starlette's `MutableHeaders` merges into the existing header
        # instead of overwriting it.
        response.headers.append("Vary", "Origin")
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response
