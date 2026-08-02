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
