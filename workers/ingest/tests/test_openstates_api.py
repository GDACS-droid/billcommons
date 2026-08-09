"""Tests for openstates_api.OpenStatesClient using an injected httpx
MockTransport -- never touches the real network or requires a real
OPENSTATES_API_KEY, per BRIEF-wave2.md ("the client must be constructible
and testable without a key via injected fixtures")."""
from __future__ import annotations

import httpx
import pytest

from billcommons_ingest.openstates_api import (
    OpenStatesAPIError,
    OpenStatesAuthError,
    OpenStatesClient,
)


def _client_with_handler(handler, api_key="test-key") -> OpenStatesClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="https://v3.openstates.org")
    return OpenStatesClient(client=http_client, api_key=api_key)


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
        include=["sponsorships", "actions", "sources", "versions", "documents"],
    )

    assert "jurisdiction=nc" in captured["url"]
    assert "session=2025-2026" in captured["url"]
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
