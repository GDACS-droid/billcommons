"""Typed error model + exception handlers.

Every error response has the shape:
    {"error": {"code": str, "message": str, "request_id": str}}

Every error response is also explicitly uncacheable. See NO_STORE.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# Errors must never be stored by ANY cache -- a CDN, a proxy, or Next's Data
# Cache. This project has already been burned twice by a cached failure
# outliving its cause: a 404 captured before an endpoint shipped kept being
# served for the full revalidate window and survived redeploys, because Next's
# Data Cache is deployment-persistent. Put a CDN in front with a
# "cache everything" rule and the same class of bug moves to a layer that
# cannot be fixed by redeploying at all.
#
# `no-store` (not merely `no-cache`) is the correct directive: no-cache permits
# storing and revalidating, which still leaves a copy to serve if revalidation
# fails. This is a prerequisite for putting any edge cache in front of the API.
NO_STORE = {"Cache-Control": "no-store"}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_headers(extra: dict | None = None) -> dict:
    """NO_STORE, merged over any handler-supplied headers.

    NO_STORE wins on conflict: nothing an exception carries should be able to
    make an error response cacheable.
    """
    headers = dict(extra or {})
    headers.update(NO_STORE)
    return headers


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            code = detail["code"]
            message = detail.get("message", "")
        else:
            code = "http_error"
            message = str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=ErrorDetail(code=code, message=message, request_id=_request_id(request))
            ).model_dump(),
            headers=_error_headers(exc.headers),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error=ErrorDetail(
                    code="validation_error",
                    message=str(exc.errors()),
                    request_id=_request_id(request),
                )
            ).model_dump(),
            headers=NO_STORE,
        )

    @app.exception_handler(SQLAlchemyTimeoutError)
    async def pool_timeout_handler(
        request: Request, exc: SQLAlchemyTimeoutError
    ) -> JSONResponse:
        """Pool exhaustion is overload, not a bug -- say so, and say when to retry.

        Without this it fell through to the 500 handler, which is wrong in three
        ways. It told the caller the server was broken when it was merely busy,
        so a well-behaved client had no reason to back off. It gave no
        Retry-After, so every client retried immediately and deepened the
        overload. And because each one printed a full traceback, the incident on
        2026-08-02 generated tracebacks fast enough that Railway rate-limited
        logging and dropped ~11,000 lines -- destroying the evidence needed to
        diagnose it.

        503 + Retry-After is the honest signal: shed load, tell the client when
        to come back, and log one line instead of a stack trace.
        """
        return JSONResponse(
            status_code=503,
            content=ErrorResponse(
                error=ErrorDetail(
                    code="overloaded",
                    message=(
                        "The API is at capacity and could not get a database "
                        "connection in time. Retry shortly."
                    ),
                    request_id=_request_id(request),
                )
            ).model_dump(),
            headers={"Retry-After": "5", **NO_STORE},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error=ErrorDetail(
                    code="internal_error",
                    message="An unexpected error occurred.",
                    request_id=_request_id(request),
                )
            ).model_dump(),
            headers=NO_STORE,
        )


def not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": code, "message": message})


def conflict(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})
