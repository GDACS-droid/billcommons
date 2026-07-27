"""The public API advertises a per-IP rate limit (docs + methodology page).
This proves the limiter is actually *enforced* globally, not just configured —
the class of bug where an advertised control silently does nothing.

These tests pin their OWN small limit rather than firing `production limit + 5`
requests. Coupling them to the real number made them both slow and wrong: when
the ceiling rose from 60 to 300/minute, issuing 305 live requests took longer
than the 60-second window, so the window rolled over, nothing was ever refused,
and the suite reported the limiter as a no-op while it was working correctly.
What matters is that the mechanism refuses traffic beyond whatever the limit
is, and that buckets are per-IP — neither claim depends on the number.
"""
import pytest
from fastapi.testclient import TestClient

from billcommons_api.app import create_app
from billcommons_api.settings import get_settings

TEST_LIMIT = 5


@pytest.fixture()
def limited_client(monkeypatch):
    """An app whose rate limit is small enough to exercise in a few requests."""
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_DEFAULT", f"{TEST_LIMIT}/minute")
    return TestClient(create_app())


def test_global_rate_limit_is_enforced_per_ip(limited_client):
    headers = {"X-Forwarded-For": "203.0.113.42"}
    statuses = [
        limited_client.get("/api/v1/jurisdictions", headers=headers).status_code
        for _ in range(TEST_LIMIT + 3)
    ]
    assert 429 in statuses, "advertised rate limit never triggered — control is a no-op"
    # Everything up to the limit must have been allowed: a limiter that refuses
    # traffic too early is its own bug.
    assert statuses[: TEST_LIMIT - 1] == [200] * (TEST_LIMIT - 1)


def test_distinct_ips_are_limited_independently(limited_client):
    """One noisy client must not be able to lock everyone else out."""
    for _ in range(TEST_LIMIT + 3):
        limited_client.get(
            "/api/v1/jurisdictions", headers={"X-Forwarded-For": "203.0.113.1"}
        )
    fresh = limited_client.get(
        "/api/v1/jurisdictions", headers={"X-Forwarded-For": "198.51.100.7"}
    )
    assert fresh.status_code == 200


def test_the_configured_limit_is_the_one_actually_applied(limited_client):
    """Guards the seam the tests above rely on. If create_app stopped reading
    the setting, they would still pass against a hardcoded default while the
    advertised number meant nothing — exactly the no-op this file exists to
    catch, one level up."""
    assert get_settings().rate_limit_default == f"{TEST_LIMIT}/minute"
    headers = {"X-Forwarded-For": "203.0.113.99"}
    allowed = sum(
        limited_client.get("/api/v1/jurisdictions", headers=headers).status_code == 200
        for _ in range(TEST_LIMIT + 3)
    )
    assert allowed == TEST_LIMIT, f"expected exactly {TEST_LIMIT} allowed, got {allowed}"
