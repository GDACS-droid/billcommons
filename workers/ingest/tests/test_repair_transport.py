from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from billcommons_ingest import host_auth, repair_transport
from billcommons_ingest.fulltext import UnfetchableDocument
from billcommons_ingest.repair_transport import (
    SafeHttpGetAdapter,
    StrictRobotsCache,
    new_repair_fetcher,
)

from billcommons_shared.httpc import USER_AGENT
from billcommons_shared.safe_http import SafeHttpError, SafeResponse


# Exact public robots bytes retained on 2026-09-08, whose SHA-256 is
# c4dccb65e1059c10446e5890ae66f57bc490b606aaa5e881df0bfa95e4d9c714.
_TX_ROBOTS = """User-agent: *
Disallow: /_private
Disallow: /_vti_cnf
Disallow: /_vti_log
Disallow: /_vti_pvt
Disallow: /_vti_script
Disallow: /_vti_txt
Disallow: /aspnet_client
Disallow: /Global.asax
Disallow: /AccessDenied.aspx
Disallow: /BasePage.aspx
Disallow: /ErrorPage.aspx
Disallow: /PageNotFound.aspx
Disallow: /ViewServerVar.aspx
Disallow: /_vti_inf.html
Disallow: /postinfo.html
Disallow: /sitemap.xml
Disallow: /web.config
Disallow: /BillLookup/
Disallow: /Reports/
Disallow: /Search/
Disallow: /bin/
Disallow: /Controls/
Disallow: /Help/
Disallow: /ig_common/
Disallow: /Images/
Disallow: /MyTLO/Login/
Disallow: /MyTLO/Alerts/
Disallow: /MyTLO/Billlist/
Disallow: /MyTLO/Search/
Disallow: /Prototype/
Disallow: /TLODOCS/
Disallow: /TLOWebServices/
Disallow: /Scripts/
Disallow: /Web References/
"""


@dataclass
class MockSafeClient:
    responses: dict[str, SafeResponse]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str] | None, bool]] = []

    def fetch(self, url, *, method="POST", body=b"", headers=None, require_body=True):
        self.calls.append((url, method, headers, require_body))
        response = self.responses.get(url)
        if response is None:
            raise AssertionError(f"unexpected SafeHttp request: {url}")
        return response


class _SafeFailure(SafeHttpError):
    pass


def _robots_response(text: str, status: int = 200) -> SafeResponse:
    return SafeResponse(status=status, headers={"content-type": "text/plain"}, body=text.encode())


def _document_response(body: bytes = b"<p>Texas witness list</p>") -> SafeResponse:
    return SafeResponse(status=200, headers={"content-type": "text/html"}, body=body)


def test_adapter_uses_bounded_safe_get_and_wraps_a_real_httpx_response():
    url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    client = MockSafeClient({url: _document_response(b"source bytes")})

    response = SafeHttpGetAdapter(client).get(url, follow_redirects=False)

    assert response.status_code == 200
    assert response.content == b"source bytes"
    assert response.request.method == "GET"
    assert client.calls == [(url, "GET", {"User-Agent": USER_AGENT}, True)]


def test_adapter_rejects_redirect_following_and_custom_headers_before_io():
    client = MockSafeClient({})
    adapter = SafeHttpGetAdapter(client)
    url = "https://capitol.texas.gov/document"

    with pytest.raises(httpx.RequestError):
        adapter.get(url, follow_redirects=True)
    with pytest.raises(httpx.RequestError):
        adapter.get(url, headers={"Authorization": "not-permitted"})
    assert client.calls == []


def test_adapter_redacts_safe_transport_error():
    url = "https://capitol.texas.gov/document"

    class FailingClient:
        def fetch(self, *_args, **_kwargs):
            raise _SafeFailure("peer-controlled-secret-detail")

    with pytest.raises(httpx.RequestError, match="repair transport request failed") as caught:
        SafeHttpGetAdapter(FailingClient()).get(url)

    assert "peer-controlled-secret-detail" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_adapter_does_not_catch_deadline_baseexception():
    class Deadline(BaseException):
        pass

    class DeadlineClient:
        def fetch(self, *_args, **_kwargs):
            raise Deadline()

    with pytest.raises(Deadline):
        SafeHttpGetAdapter(DeadlineClient()).get("https://capitol.texas.gov/document")


def test_adapter_contract_does_not_autofollow_a_stubbed_redirect_response():
    source_url = "https://capitol.texas.gov/source"
    target_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    document_client = MockSafeClient(
        {
            source_url: SafeResponse(status=302, headers={"location": target_url}, body=b""),
            target_url: _document_response(),
        }
    )
    robots_client = MockSafeClient(
        {"https://capitol.texas.gov/robots.txt": _robots_response("User-agent: *\nAllow: /\n")}
    )
    fetcher = new_repair_fetcher(document_client=document_client, robots_client=robots_client)

    assert fetcher.fetch(source_url).request.url == target_url
    assert [call[0] for call in document_client.calls] == [source_url, target_url]
    assert len(robots_client.calls) == 1


def test_strict_robots_200_policy_is_cached_and_allows_document_fetch():
    doc_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    robots_url = "https://capitol.texas.gov/robots.txt"
    document_client = MockSafeClient({doc_url: _document_response()})
    robots_client = MockSafeClient({robots_url: _robots_response("User-agent: *\nAllow: /\n")})

    fetcher = new_repair_fetcher(document_client=document_client, robots_client=robots_client)
    assert fetcher.fetch(doc_url).status_code == 200
    assert fetcher.fetch(doc_url).status_code == 200

    assert len(robots_client.calls) == 1
    assert len(document_client.calls) == 2
    assert fetcher.last_fetch_robots_exempt is False


@pytest.mark.parametrize("status", [401, 403, 500])
def test_strict_robots_unavailable_or_denied_status_blocks_document_fetch(status):
    doc_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    robots_client = MockSafeClient({"https://capitol.texas.gov/robots.txt": _robots_response("denied", status)})
    document_client = MockSafeClient({doc_url: _document_response()})
    fetcher = new_repair_fetcher(document_client=document_client, robots_client=robots_client)

    with pytest.raises(UnfetchableDocument, match="robots.txt disallows"):
        fetcher.fetch(doc_url)
    assert document_client.calls == []


@pytest.mark.parametrize("status", [404, 410])
def test_strict_robots_missing_policy_allows_document_fetch(status):
    doc_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    robots_client = MockSafeClient({"https://capitol.texas.gov/robots.txt": _robots_response("", status)})
    document_client = MockSafeClient({doc_url: _document_response()})
    fetcher = new_repair_fetcher(document_client=document_client, robots_client=robots_client)

    assert fetcher.fetch(doc_url).status_code == 200
    assert len(document_client.calls) == 1


def test_retained_tx_robots_policy_allows_the_reviewed_lowercase_tlodocs_path():
    doc_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    robots_client = MockSafeClient({"https://capitol.texas.gov/robots.txt": _robots_response(_TX_ROBOTS)})
    document_client = MockSafeClient({doc_url: _document_response()})
    fetcher = new_repair_fetcher(document_client=document_client, robots_client=robots_client)

    assert fetcher.fetch(doc_url).status_code == 200
    assert len(document_client.calls) == 1


@pytest.mark.parametrize("directive", ["Crawl-delay: 5", "Request-rate: 1/10"])
def test_strict_robots_refuses_unrepresentable_cadence_before_document_fetch(directive):
    doc_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    robots_client = MockSafeClient(
        {"https://capitol.texas.gov/robots.txt": _robots_response(f"User-agent: *\n{directive}\nAllow: /\n")}
    )
    document_client = MockSafeClient({doc_url: _document_response()})
    fetcher = new_repair_fetcher(document_client=document_client, robots_client=robots_client)

    with pytest.raises(UnfetchableDocument, match="robots.txt disallows"):
        fetcher.fetch(doc_url)
    assert document_client.calls == []


def test_strict_robots_network_error_fails_closed_before_document_fetch():
    doc_url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"

    class FailingRobotsClient:
        def fetch(self, *_args, **_kwargs):
            raise _SafeFailure("unavailable")

    document_client = MockSafeClient({doc_url: _document_response()})
    fetcher = new_repair_fetcher(document_client=document_client, robots_client=FailingRobotsClient())

    with pytest.raises(UnfetchableDocument, match="robots.txt disallows"):
        fetcher.fetch(doc_url)
    assert document_client.calls == []


def test_factory_disables_ambient_host_auth_and_robots_exemptions(monkeypatch):
    monkeypatch.setattr(host_auth, "robots_exempt", lambda _url: True)
    url = "https://capitol.texas.gov/tlodocs/89R/witlistbill/html/HB00576H.htm"
    fetcher = new_repair_fetcher(
        document_client=MockSafeClient({url: _document_response()}),
        robots_client=MockSafeClient({"https://capitol.texas.gov/robots.txt": _robots_response("User-agent: *\nAllow: /\n")}),
    )

    assert fetcher._robots_exempt(url) is False
    assert fetcher._headers_for(url) == {}


def test_strict_robots_cache_expires_after_five_minutes():
    now = [100.0]
    url = "https://capitol.texas.gov/document"
    robots_url = "https://capitol.texas.gov/robots.txt"
    client = MockSafeClient({robots_url: _robots_response("User-agent: *\nAllow: /\n")})
    cache = StrictRobotsCache(SafeHttpGetAdapter(client), clock=lambda: now[0])

    assert cache.can_fetch(url) is True
    assert cache.can_fetch(url) is True
    now[0] += 300.0
    assert cache.can_fetch(url) is True
    assert len(client.calls) == 2


def test_production_factory_uses_distinct_body_caps_and_reviewed_tls(monkeypatch):
    document_client = MockSafeClient({})
    robots_client = MockSafeClient({})
    calls = []

    def new_safe_http_client(**kwargs):
        calls.append(kwargs)
        return document_client if len(calls) == 1 else robots_client

    monkeypatch.setattr(repair_transport, "new_safe_http_client", new_safe_http_client)

    new_repair_fetcher()

    assert calls == [
        {
            "max_body_bytes": repair_transport.DOCUMENT_MAX_BODY_BYTES,
            "ssl_context_factory": repair_transport.reviewed_context_for_host,
        },
        {
            "max_body_bytes": repair_transport.ROBOTS_MAX_BODY_BYTES,
            "ssl_context_factory": repair_transport.reviewed_context_for_host,
        },
    ]
