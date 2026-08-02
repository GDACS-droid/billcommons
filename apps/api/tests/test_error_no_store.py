"""Every error response must be uncacheable — no exceptions."""
from fastapi.testclient import TestClient
from billcommons_api.app import create_app

client = TestClient(create_app(), raise_server_exceptions=False)


def test_404_is_no_store():
    r = client.get("/api/v1/bills/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.headers.get("cache-control") == "no-store"


def test_422_validation_error_is_no_store():
    r = client.get("/api/v1/bills?per_page=notanumber")
    assert r.status_code == 422
    assert r.headers.get("cache-control") == "no-store"


def test_400_bad_request_is_no_store():
    r = client.get("/api/v1/bills/not-a-uuid")
    assert r.status_code in (400, 404, 422)
    assert r.headers.get("cache-control") == "no-store"


def test_pool_exhaustion_returns_503_not_500():
    """Overload must be reported as overload, with a Retry-After.

    Falling through to the 500 handler told callers the server was broken when
    it was merely busy, gave them no reason to back off, and printed a full
    traceback per request -- which is how the 2026-08-02 incident buried its own
    evidence under Railway's log rate limit.
    """
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
    from fastapi import FastAPI
    from billcommons_api.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom():
        raise SQLAlchemyTimeoutError("QueuePool limit of size 5 overflow 10 reached")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/boom")
    assert r.status_code == 503, f"got {r.status_code}, expected 503"
    assert r.headers.get("retry-after") == "5"
    assert r.headers.get("cache-control") == "no-store"
    assert r.json()["error"]["code"] == "overloaded"
