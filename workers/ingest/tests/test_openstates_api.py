"""Tests for openstates_api.OpenStatesClient using an injected httpx
MockTransport -- never touches the real network or requires a real
OPENSTATES_API_KEY, per BRIEF-wave2.md ("the client must be constructible
and testable without a key via injected fixtures")."""
from __future__ import annotations

import httpx
import pytest

from billcommons_ingest import openstates_api as openstates_api_mod
from billcommons_ingest.openstates_api import (
    OpenStatesAPIError,
    OpenStatesAuthError,
    OpenStatesClient,
    OpenStatesDailyBudgetExceeded,
)


@pytest.fixture(autouse=True)
def _reset_daily_budget_counter():
    """The daily-budget counter is a module-global dict so it survives
    across tests in the same process -- reset it before AND after every
    test so test order can never leak counts between tests."""
    openstates_api_mod._daily_request_counts.clear()
    yield
    openstates_api_mod._daily_request_counts.clear()


def _client_with_handler(handler, api_key="test-key") -> OpenStatesClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://v3.openstates.org")
    return OpenStatesClient(client=http_client, api_key=api_key, consume_budget=openstates_api_mod._check_and_consume_budget)


def test_client_constructs_without_api_key_env_var(monkeypatch):
    monkeypatch.delenv("OPENSTATES_API_KEY", raising=False)
    # Construction alone must never require a key.
    client = OpenStatesClient()
    assert client.base_url == "https://v3.openstates.org"


def test_request_without_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("OPENSTATES_API_KEY", raising=False)

    def handler(request):  # pragma: no cover - should never be reached
        raise AssertionError("should not make a request without a key")

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://v3.openstates.org")
    client = OpenStatesClient(client=http_client)

    with pytest.raises(OpenStatesAuthError):
        client.get_jurisdictions()


def test_get_jurisdictions_sends_api_key_header():
    seen_headers = {}

    def handler(request):
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"results": []})

    client = _client_with_handler(handler)
    client.get_jurisdictions()
    assert seen_headers.get("x-api-key") == "test-key"


def test_search_bills_builds_query_params():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["params"] = request.url.params
        return httpx.Response(200, json={"results": [], "pagination": {"max_page": 1}})

    client = _client_with_handler(handler)
    client.search_bills(
        jurisdiction="nc",
        session="2025-2026",
        identifier="HB 42",
        include=["sponsorships", "actions", "sources", "versions", "documents"],
    )

    assert "jurisdiction=nc" in captured["url"]
    assert "session=2025-2026" in captured["url"]
    assert "identifier=HB+42" in captured["url"]
    assert "include=sponsorships" in captured["url"]
    assert "include=actions" in captured["url"]
    # `include` is v3's repeated query param, not a comma-joined single value
    # -- api_sync.py's INCLUDE list (sponsorships/actions/sources/versions/
    # documents, per the versions/documents repair) must serialize as one
    # `include=` entry PER value, all present simultaneously, not the last
    # one clobbering the rest.
    assert "include=versions" in captured["url"]
    assert "include=documents" in captured["url"]
    assert captured["params"].get_list("include") == [
        "sponsorships",
        "actions",
        "sources",
        "versions",
        "documents",
    ]


def test_search_bills_omits_optional_filters_by_default():
    captured = {}

    def handler(request):
        captured["params"] = request.url.params
        return httpx.Response(200, json={"results": [], "pagination": {"max_page": 1}})

    client = _client_with_handler(handler)
    client.search_bills()

    assert "session" not in captured["params"]
    assert "identifier" not in captured["params"]


def test_retained_bill_response_keeps_entity_bytes_and_excludes_credentials_from_metadata():
    raw = b'{\n  "results": [], "pagination": {"max_page": 1}\n}'

    def handler(request):
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        transport=transport,
        base_url="https://ignored.invalid",
    )
    client = OpenStatesClient(
        base_url="https://user:secret@v3.openstates.org/api?api_key=not-recorded",
        client=http_client,
        api_key="test-key",
        consume_budget=lambda: None,
    )

    response = client.search_bills_with_response(
        jurisdiction="nc", page=3, per_page=7, updated_since="2026-09-01T00:00:00+00:00", include=["actions"]
    )

    assert response.is_retained_response is True
    assert response.raw_bytes == raw
    assert response.payload == {"results": [], "pagination": {"max_page": 1}}
    assert response.source_url == "https://v3.openstates.org/api/bills"
    assert response.request_scope == {
        "jurisdiction": "nc",
        "session": None,
        "identifier": None,
        "page": 3,
        "per_page": 7,
        "updated_since": "2026-09-01T00:00:00+00:00",
        "includes": ["actions"],
    }


def test_iter_bills_paginates_across_pages():
    pages = {
        1: {"results": [{"id": "a"}], "pagination": {"max_page": 2}},
        2: {"results": [{"id": "b"}], "pagination": {"max_page": 2}},
    }

    def handler(request):
        page = int(dict(request.url.params).get("page", "1"))
        return httpx.Response(200, json=pages[page])

    client = _client_with_handler(handler)
    bills = list(client.iter_bills(jurisdiction="nc"))
    assert [b["id"] for b in bills] == ["a", "b"]


def test_429_triggers_backoff_then_succeeds(monkeypatch):
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"results": []})

    client = _client_with_handler(handler)
    # Avoid real sleeping delay in the rate limiter itself for test speed.
    client.rate_limiter.acquire = lambda host: None  # type: ignore[method-assign]
    result = client.get_jurisdictions()
    assert result == {"results": []}
    assert calls["count"] == 2


def test_exhausting_429_retries_raises_api_error():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = _client_with_handler(handler)
    client.rate_limiter.acquire = lambda host: None  # type: ignore[method-assign]
    client.max_retries_on_429 = 1

    with pytest.raises(OpenStatesAPIError):
        client.get_jurisdictions()


def test_4xx_error_raises_api_error():
    def handler(request):
        return httpx.Response(404, text="not found")

    client = _client_with_handler(handler)
    with pytest.raises(OpenStatesAPIError):
        client.get_bill("ocd-bill/nonexistent")


def test_read_timeout_then_success_retries_and_paces_the_limiter():
    calls = {"count": 0, "acquires": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"results": []})

    client = _client_with_handler(handler)

    def counting_acquire(host):
        # Counts calls without the real limiter's throttling delay (burst=1
        # means a second real acquire would sleep ~10s waiting for a token).
        calls["acquires"] += 1

    client.rate_limiter.acquire = counting_acquire  # type: ignore[method-assign]
    result = client.get_jurisdictions()

    assert result == {"results": []}
    assert calls["count"] == 2
    assert calls["acquires"] == 2  # each retry re-acquires the limiter token


def test_three_consecutive_502s_raises_after_three_requests():
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return httpx.Response(502, text="bad gateway")

    client = _client_with_handler(handler)
    client.rate_limiter.acquire = lambda host: None  # type: ignore[method-assign]

    with pytest.raises(OpenStatesAPIError):
        client.get_jurisdictions()
    assert calls["count"] == 3


def test_429_retry_is_independent_of_the_http_retry_loop():
    """A 429 followed by two 502s followed by success must still succeed --
    the 429 backoff counter and the timeout/5xx retry counter must not
    share state, or a 429 would silently eat into the 5xx retry budget (or
    vice versa)."""
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if calls["count"] in (2, 3):
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"results": []})

    client = _client_with_handler(handler)
    client.rate_limiter.acquire = lambda host: None  # type: ignore[method-assign]

    result = client.get_jurisdictions()
    assert result == {"results": []}
    assert calls["count"] == 4


def test_daily_budget_exhausted_raises_without_sending(monkeypatch):
    monkeypatch.setenv("OPENSTATES_DAILY_BUDGET", "3")
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        return httpx.Response(200, json={"results": []})

    client = _client_with_handler(handler)
    client.rate_limiter.acquire = lambda host: None  # type: ignore[method-assign]

    client.get_jurisdictions()
    client.get_jurisdictions()
    client.get_jurisdictions()
    assert calls["count"] == 3

    with pytest.raises(OpenStatesDailyBudgetExceeded):
        client.get_jurisdictions()
    assert calls["count"] == 3  # the 4th call never reached the transport


def test_retries_count_against_the_daily_budget(monkeypatch):
    monkeypatch.setenv("OPENSTATES_DAILY_BUDGET", "2")
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"results": []})

    client = _client_with_handler(handler)
    client.rate_limiter.acquire = lambda host: None  # type: ignore[method-assign]

    result = client.get_jurisdictions()
    assert result == {"results": []}
    assert calls["count"] == 2  # ReadTimeout attempt + the retry that succeeded

    # The budget (2) is now fully consumed by that one retry cycle.
    with pytest.raises(OpenStatesDailyBudgetExceeded):
        client.get_jurisdictions()
    assert calls["count"] == 2


def test_default_client_requires_shared_admission_before_http(monkeypatch):
    calls = []

    def denied(**kwargs):
        raise openstates_api_mod.RequestBudgetUnavailable("private details")

    monkeypatch.setattr(openstates_api_mod, "consume_request", denied)
    client = OpenStatesClient(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))),
        api_key="fixture-key",
    )
    client.rate_limiter.acquire = lambda host: None
    with pytest.raises(openstates_api_mod.OpenStatesBudgetUnavailable) as error:
        client.get_jurisdictions()
    assert calls == []
    assert "private details" not in str(error.value)


def test_default_client_retries_each_require_committed_admission(monkeypatch):
    events = []

    def handler(request):
        events.append("http")
        if events.count("http") == 1:
            return httpx.Response(502)
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(openstates_api_mod, "consume_request", lambda **kwargs: events.append("admitted"))
    client = OpenStatesClient(
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://v3.openstates.org"),
        api_key="fixture-key",
    )
    client.rate_limiter.acquire = lambda host: None
    assert client.get_jurisdictions() == {"results": []}
    assert events == ["admitted", "http", "admitted", "http"]
