"""Focused contracts for API middleware behavior."""
from __future__ import annotations

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from billcommons_api.middleware import AccountCorsMiddleware


_ALLOWED_ORIGIN = "https://billcommons.org"
_DENIED_ORIGIN = "https://evil.example"
_SCOUT_PREFLIGHT_PATHS = (
    "/api/v1/scout/jobs",
    "/api/v1/scout/jobs/00000000-0000-0000-0000-000000000001",
    "/api/v1/scout/jobs/00000000-0000-0000-0000-000000000001/cancel",
    "/api/v1/scout/jobs/00000000-0000-0000-0000-000000000001/browser-sessions/"
    "00000000-0000-0000-0000-000000000002/replay",
)


@pytest.fixture()
def cors_client():
    async def endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/v1/scout/jobs/{job_id}", endpoint)])
    # Match the production registration order: the specialized middleware is
    # outermost and must suppress this permissive middleware on Scout paths.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "DELETE"])
    app.add_middleware(AccountCorsMiddleware)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", _SCOUT_PREFLIGHT_PATHS)
def test_scout_preflight_all_job_paths_allow_credentials_only_for_allowlisted_origin(cors_client, path):
    allowed = cors_client.options(
        path,
        headers={
            "Origin": _ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )

    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == _ALLOWED_ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in allowed.headers["vary"]

    denied = cors_client.options(
        path,
        headers={"Origin": _DENIED_ORIGIN, "Access-Control-Request-Method": "POST"},
    )

    assert denied.status_code == 204
    assert "access-control-allow-origin" not in denied.headers
    assert "access-control-allow-credentials" not in denied.headers


@pytest.mark.parametrize(
    ("origin", "expected_origin", "expected_credentials"),
    [
        (_ALLOWED_ORIGIN, _ALLOWED_ORIGIN, "true"),
        (_DENIED_ORIGIN, None, None),
    ],
)
def test_scout_actual_response_uses_restricted_credentialed_cors(
    cors_client, origin, expected_origin, expected_credentials
):
    response = cors_client.get(
        "/api/v1/scout/jobs/00000000-0000-0000-0000-000000000001",
        headers={"Origin": origin},
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == expected_origin
    assert response.headers.get("access-control-allow-credentials") == expected_credentials
    assert "Origin" in response.headers["vary"]
