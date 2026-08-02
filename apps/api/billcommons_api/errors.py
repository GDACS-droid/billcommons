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
