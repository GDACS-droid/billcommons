"""/api/v1/coverage must serve the public coverage MATRIX shape (SPEC.md
"Coverage"): jurisdiction_code/name, session info, bill counts, full-text %,
last update, source, validation, status, known_gaps -- never raw
jurisdiction_coverage ORM rows (jurisdiction_id UUIDs). Tests are row-count
tolerant since the DB has real, growing seeded data."""
from __future__ import annotations


def test_coverage_envelope_shape(client):
    resp = client.get("/api/v1/coverage", params={"per_page": 51})
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body and "pagination" in body and "meta" in body
    assert isinstance(body["data"], list)


def test_coverage_rows_are_matrix_shaped_not_raw_orm_rows(client):
    """Regression test for the bug where the endpoint returned raw
    JurisdictionCoverage rows (jurisdiction_id UUID, no name) and the web
    coverage page 500'd calling row.jurisdiction_name.localeCompare()."""
    resp = client.get("/api/v1/coverage", params={"per_page": 51})
    assert resp.status_code == 200
    rows = resp.json()["data"]

    required_keys = {
        "jurisdiction_code",
        "jurisdiction_name",
        "session_identifier",
        "session_status",
        "bill_count",
        "full_text_count",
        "full_text_pct",
        "last_update",
        "source_name",
        "validation_sample",
        "validation_pass_rate",
        "status",
        "known_gaps",
    }
    for row in rows:
        assert required_keys.issubset(row.keys())
        # The bug this guards against: raw ORM rows exposed jurisdiction_id
        # (a UUID) instead of a human-readable name/code.
        assert "jurisdiction_id" not in row
        assert isinstance(row["jurisdiction_code"], str) and row["jurisdiction_code"]
        assert isinstance(row["jurisdiction_name"], str) and row["jurisdiction_name"]
        assert isinstance(row["known_gaps"], list)


def test_coverage_full_text_pct_is_none_when_bill_count_zero(client):
    resp = client.get("/api/v1/coverage", params={"per_page": 51})
    assert resp.status_code == 200
    for row in resp.json()["data"]:
        if row["bill_count"] == 0:
            assert row["full_text_pct"] is None
        elif row["full_text_pct"] is not None:
            assert 0 <= row["full_text_pct"] <= 100


def test_coverage_filter_by_jurisdiction(client):
    resp = client.get("/api/v1/coverage", params={"jurisdiction": "NC"})
    assert resp.status_code == 200
    for row in resp.json()["data"]:
        assert row["jurisdiction_code"] == "NC"


def test_coverage_bad_status_filter_returns_empty_not_error(client):
    resp = client.get("/api/v1/coverage", params={"status": "NOT_A_REAL_STATUS"})
    assert resp.status_code == 200
    assert resp.json()["data"] == []
