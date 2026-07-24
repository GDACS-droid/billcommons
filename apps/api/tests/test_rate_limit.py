"""The public API advertises a per-IP rate limit (docs + methodology page).
This proves the limiter is actually *enforced* globally, not just configured —
the class of bug where an advertised control silently does nothing.
"""
from fastapi.testclient import TestClient

from billcommons_api.app import create_app
from billcommons_api.settings import get_settings


def _limit_count() -> int:
    # settings default is "<n>/minute" — parse the numerator
    return int(get_settings().rate_limit_default.split("/")[0])


def test_global_rate_limit_is_enforced_per_ip():
    app = create_app()
    client = TestClient(app)
    limit = _limit_count()
    # one fixed client IP; limiter keys on X-Forwarded-For first hop
    headers = {"X-Forwarded-For": "203.0.113.42"}
    statuses = [
        client.get("/api/v1/jurisdictions", headers=headers).status_code
        for _ in range(limit + 5)
    ]
    assert 429 in statuses, "advertised rate limit never triggered — control is a no-op"
    # everything up to the limit must have been allowed
    assert statuses[: limit - 1] == [200] * (limit - 1)


def test_distinct_ips_are_limited_independently():
    app = create_app()
    client = TestClient(app)
    limit = _limit_count()
    # a second IP gets its own bucket and is not punished for the first IP's load
    for _ in range(limit + 5):
        client.get("/api/v1/jurisdictions", headers={"X-Forwarded-For": "203.0.113.1"})
    fresh = client.get("/api/v1/jurisdictions", headers={"X-Forwarded-For": "198.51.100.7"})
    assert fresh.status_code == 200
