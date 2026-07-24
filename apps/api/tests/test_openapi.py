"""OpenAPI contract: version pin + every SPEC-required endpoint present.
A missing route here means a consumer (web/mcp) generated a client against
a promise this API doesn't keep."""
from __future__ import annotations

REQUIRED_PATHS = {
    "/api/v1/jurisdictions",
    "/api/v1/jurisdictions/{jurisdiction_id}",
    "/api/v1/sessions",
    "/api/v1/bills",
    "/api/v1/bills/{bill_id}",
    "/api/v1/bills/{bill_id}/versions",
    "/api/v1/bills/{bill_id}/actions",
    "/api/v1/bills/{bill_id}/sponsors",
    "/api/v1/bills/{bill_id}/votes",
    "/api/v1/bills/{bill_id}/documents",
    "/api/v1/people",
    "/api/v1/people/{person_id}",
    "/api/v1/committees",
    "/api/v1/committees/{committee_id}",
    "/api/v1/events",
    "/api/v1/search",
    "/api/v1/sources",
    "/api/v1/coverage",
    "/api/v1/health",
    "/api/v1/ready",
}


def test_openapi_is_valid_3_1_and_covers_every_spec_endpoint(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert schema["openapi"].startswith("3.1")
    assert schema["info"]["title"] == "Bill Commons API"

    present = set(schema["paths"].keys())
    missing = REQUIRED_PATHS - present
    assert not missing, f"OpenAPI is missing required paths: {missing}"


def test_docs_ui_is_live(client):
    resp = client.get("/docs")
    assert resp.status_code == 200
