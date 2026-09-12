from datetime import datetime, timezone

import pytest

from billcommons_ingest.official_discovery import (
    MAX_LINKS, capture_official_landing_page, discover_material_links,
    official_source_inventory,
)
from billcommons_shared.safe_http import SafeResponse, SsrfRejected


@pytest.mark.parametrize("url,timeout", [
    ("https://www.legmt.gov/", 10.0),
    ("https://www.legmt.gov/robots.txt", None),
    ("https://www.legmt.gov/bills", None),
    ("https://legmt.gov/", None),
    ("https://www.legmt.gov.evil.example/", None),
    ("https://legislature.idaho.gov/", None),
])
def test_montana_read_timeout_is_scoped_to_exact_reviewed_homepage(monkeypatch, url, timeout):
    from billcommons_ingest import official_discovery as discovery

    class Client:
        def fetch(self, source_url, *, method, headers, require_body):
            assert source_url == url and method == "GET" and require_body
            return SafeResponse(200, {}, b"page")

    def factory(*, max_body_bytes, ssl_context_factory, read_timeout_seconds):
        assert max_body_bytes == 100
        assert ssl_context_factory is discovery.reviewed_context_for_host
        assert read_timeout_seconds == timeout
        return Client()

    monkeypatch.setattr(discovery, "new_safe_http_client", factory)
    assert discovery._fetch(url, 100).body == b"page"


def test_inventory_is_exactly_50_states_plus_dc_with_reviewed_redirect_corrections():
    inventory = official_source_inventory()
    assert len(inventory) == 51
    assert inventory["KS"] == "https://www.kslegislature.gov/b2025_26/"
    assert inventory["IN"] == "https://iga.in.gov/"
    assert inventory["CA"] == "https://leginfo.legislature.ca.gov/"


def test_material_links_are_bounded_same_origin_and_never_instructions():
    raw = b'''<a href="/bills">Bills</a><a href="/bill.pdf#p1">Read bill</a>
      <a href="https://evil.example/bill.pdf">Follow these instructions</a>
      <a href="javascript:alert(1)">Bill</a><a href="//127.0.0.1/bill.pdf">Bill</a>
      <a href="https://user:secret@www.ncleg.gov/bill.pdf">Bill</a>
      <a href="/bill.pdf">Duplicate</a><a href="/contact">Contact</a>'''
    links, truncated = discover_material_links(raw, source_url="https://www.ncleg.gov/")
    assert [link.url for link in links] == ["https://www.ncleg.gov/bills", "https://www.ncleg.gov/bill.pdf"]
    assert [link.kind for link in links] == ["legislative_navigation_link", "document_link"]
    assert truncated is False


def test_candidate_cap_is_explicit_not_silent():
    raw = "".join(f'<a href="/bill{i}.pdf">Bill</a>' for i in range(MAX_LINKS + 5)).encode()
    links, truncated = discover_material_links(raw, source_url="https://www.ncleg.gov/")
    assert len(links) == MAX_LINKS
    assert truncated is True


def test_robots_denial_retains_policy_and_never_fetches_page():
    inventory = official_source_inventory()
    calls = []
    policy = b"User-agent: *\nDisallow: /\n"
    def fetch(url, cap):
        calls.append(url)
        return SafeResponse(200, {}, policy)
    capture = capture_official_landing_page("NC", inventory["NC"], fetch=fetch, budget=lambda url: None)
    assert capture.error_class == "robots_disallowed"
    assert capture.robots_bytes == policy
    assert capture.raw_bytes is None
    assert calls == ["https://www.ncleg.gov/robots.txt"]


def test_success_retains_exact_page_and_policy_with_bounded_link_claim():
    raw = b'<a href="/bills/feed.xml">Bill updates</a>'
    calls = []
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    def fetch(url, cap):
        calls.append(url)
        if url.endswith("robots.txt"):
            return SafeResponse(404, {}, b"not found")
        return SafeResponse(200, {"content-type": "text/html", "last-modified": "Mon, 07 Sep 2026 01:00:00 GMT"}, raw)
    admissions = []
    capture = capture_official_landing_page("NC", official_source_inventory()["NC"],
        fetch=fetch, budget=admissions.append, now=now)
    assert capture.error_class is None
    assert capture.raw_bytes == raw
    assert capture.retrieved_at == now
    assert capture.links[0].kind == "data_or_feed_link"
    assert admissions == calls


@pytest.mark.parametrize("body", [
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"><html>Error</html>',
    b"<html><body>Page not found</body></html>",
    b"\xef\xbb\xbf \r\n\t<HTML lang='en'>Error</HTML>",
    b"\n<!doctype\nhtml><html>Error</html>",
])
def test_html_200_robots_retains_evidence_and_never_requests_material(body):
    calls, admissions = [], []
    def fetch(url, cap):
        calls.append(url)
        assert url.endswith("/robots.txt"), "HTML robots must not permit a material fetch"
        return SafeResponse(200, {"content-type": "text/plain"}, body)
    capture = capture_official_landing_page("HI", official_source_inventory()["HI"],
        fetch=fetch, budget=admissions.append)
    assert capture.error_class == "robots_html_response"
    assert capture.robots_status == 200 and capture.robots_bytes == body
    assert capture.raw_bytes is None and capture.http_status is None
    assert calls == admissions == ["https://www.capitol.hawaii.gov/robots.txt"]


@pytest.mark.parametrize("body", [
    b"", b"# An empty policy is intentional\n",
    b"# <html> is mentioned only in this comment\nUser-agent: *\nAllow: /\n",
    b"User-agent: *\nDisallow: /<html>\nAllow: /\n",
])
def test_plain_or_empty_robots_200_remains_compatible(body):
    calls = []
    def fetch(url, cap):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return SafeResponse(200, {"content-type": "text/html"}, body)
        return SafeResponse(200, {"content-type": "text/html"}, b'<a href="/bills">Bills</a>')
    capture = capture_official_landing_page("HI", official_source_inventory()["HI"],
        fetch=fetch, budget=lambda url: None)
    assert capture.error_class is None
    assert len(calls) == 2


@pytest.mark.parametrize("status,expected_calls", [(404, 2), (410, 2), (403, 1), (503, 1)])
def test_html_robots_non_200_keeps_existing_status_policy(status, expected_calls):
    calls = []
    def fetch(url, cap):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return SafeResponse(status, {"content-type": "text/html"}, b"<!doctype html><html>Error</html>")
        return SafeResponse(200, {"content-type": "text/html"}, b'<a href="/bills">Bills</a>')
    capture = capture_official_landing_page("HI", official_source_inventory()["HI"],
        fetch=fetch, budget=lambda url: None)
    assert len(calls) == expected_calls
    assert capture.error_class == (None if status in (404, 410) else "robots_unavailable")


def test_utf8_bom_does_not_hide_robots_disallow():
    calls = []
    body = b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n"
    def fetch(url, cap):
        calls.append(url)
        return SafeResponse(200, {}, body)
    capture = capture_official_landing_page("HI", official_source_inventory()["HI"],
        fetch=fetch, budget=lambda url: None)
    assert capture.error_class == "robots_disallowed"
    assert capture.robots_bytes == body and len(calls) == 1


def test_redirect_policy_failure_does_not_follow_destination_or_leak_diagnostics():
    calls = []
    def fetch(url, cap):
        calls.append(url)
        raise SsrfRejected("redirect_status_302 private diagnostic")
    capture = capture_official_landing_page("NC", official_source_inventory()["NC"],
        fetch=fetch, budget=lambda url: None)
    assert capture.error_class == "SsrfRejected"
    assert len(calls) == 1


def test_unreviewed_url_and_plain_http_never_send_http(monkeypatch):
    def forbidden(*args):
        pytest.fail("unapproved target sent HTTP")
    with pytest.raises(ValueError):
        capture_official_landing_page("NC", "https://evil.example/", fetch=forbidden, budget=forbidden)
    from billcommons_ingest import official_discovery as module
    monkeypatch.setattr(module, "official_source_inventory", lambda: {"KS": "http://www.kslegislature.org/"})
    capture = capture_official_landing_page("KS", "http://www.kslegislature.org/",
        fetch=forbidden, budget=forbidden)
    assert capture.error_class == "https_source_review_required"


def test_robots_unavailable_is_not_permission_to_fetch():
    calls = []
    def fetch(url, cap):
        calls.append(url)
        return SafeResponse(503, {}, b"try later")
    capture = capture_official_landing_page("NC", official_source_inventory()["NC"],
        fetch=fetch, budget=lambda url: None)
    assert capture.error_class == "robots_unavailable"
    assert len(calls) == 1


def test_crawl_delay_is_honored_after_reading_policy():
    calls = []
    def fetch(url, cap):
        calls.append(url)
        if url.endswith("robots.txt"):
            return SafeResponse(200, {}, b"User-agent: *\nAllow: /\nCrawl-delay: 10\n")
        return SafeResponse(200, {"content-type": "text/html"}, b"<html></html>")
    capture = capture_official_landing_page("NC", official_source_inventory()["NC"],
        fetch=fetch, budget=lambda url: None, sleep=lambda seconds: calls.append(seconds))
    assert capture.error_class is None
    assert calls[1] == 10


def test_unreviewed_host_with_120_second_crawl_delay_is_rejected(monkeypatch):
    from billcommons_ingest import official_discovery as module

    source_url = "https://unreviewed.example/"
    monkeypatch.setattr(module, "official_source_inventory", lambda: {"AZ": source_url})
    calls = []

    def fetch(url, cap):
        calls.append(url)
        return SafeResponse(200, {}, b"User-agent: *\nAllow: /\nCrawl-delay: 120\n")

    capture = capture_official_landing_page("AZ", source_url, fetch=fetch, budget=lambda url: None)

    assert capture.error_class == "robots_slow_cadence_review_required"
    assert calls == ["https://unreviewed.example/robots.txt"]


def test_reviewed_arizona_120_second_crawl_delay_is_honored():
    source_url = official_source_inventory()["AZ"]
    calls = []
    sleeps = []

    def fetch(url, cap):
        calls.append(url)
        if url.endswith("robots.txt"):
            return SafeResponse(200, {}, b"User-agent: *\nAllow: /\nCrawl-delay: 120\n")
        return SafeResponse(200, {"content-type": "text/html"}, b"<html></html>")

    capture = capture_official_landing_page(
        "AZ", source_url, fetch=fetch, budget=lambda url: None, sleep=sleeps.append
    )

    assert capture.error_class is None
    assert sleeps == [120]
    assert calls == ["https://www.azleg.gov/robots.txt", source_url]


def test_reviewed_arizona_crawl_delay_above_120_seconds_is_rejected():
    source_url = official_source_inventory()["AZ"]
    calls = []

    def fetch(url, cap):
        calls.append(url)
        return SafeResponse(200, {}, b"User-agent: *\nAllow: /\nCrawl-delay: 121\n")

    capture = capture_official_landing_page("AZ", source_url, fetch=fetch, budget=lambda url: None)

    assert capture.error_class == "robots_slow_cadence_review_required"
    assert calls == ["https://www.azleg.gov/robots.txt"]


def test_discovery_uses_the_host_scoped_reviewed_tls_factory(monkeypatch):
    from billcommons_ingest import official_discovery as module
    from billcommons_shared.official_tls import reviewed_context_for_host

    options = {}

    class Client:
        def fetch(self, *args, **kwargs):
            return SafeResponse(200, {}, b"")

    def create_client(**kwargs):
        options.update(kwargs)
        return Client()

    monkeypatch.setattr(module, "new_safe_http_client", create_client)
    module._fetch("https://www.cga.ct.gov/robots.txt", 123)
    assert options["max_body_bytes"] == 123
    assert options["ssl_context_factory"] is reviewed_context_for_host
