"""Detail (GET .../{id}) routes across resources: consistent 404 + ETag
contract. ETag must be present on every detail 200 so caching/If-None-Match
works for web/mcp clients (SPEC: "ETags/caching")."""
from __future__ import annotations

import pytest

NIL_UUID = "00000000-0000-0000-0000-000000000000"

DETAIL_404_CASES = [
    ("/api/v1/jurisdictions/{}", "jurisdiction_not_found"),
    ("/api/v1/people/{}", "person_not_found"),
    ("/api/v1/committees/{}", "committee_not_found"),
    ("/api/v1/bills/{}", "bill_not_found"),
]


@pytest.mark.parametrize("path_tpl,code", DETAIL_404_CASES)
def test_detail_route_404_has_expected_error_code(client, path_tpl, code):
    resp = client.get(path_tpl.format(NIL_UUID))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == code
