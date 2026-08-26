"""Tests for the full-bill-text pipeline (billcommons_ingest.fulltext).

Business intent per docs/SPEC.md ("Version diffing", "Refresh", GREEN
criteria #5): every document with an official source URL should get its
text extracted deterministically, robots.txt must actually gate fetches
(politeness is non-negotiable, not merely aspirational), scanned PDFs must
never masquerade as extracted text, and enqueueing must never double-queue
a document that already has a pending fetch job.

All network access is via an injected httpx.MockTransport / fake
robots-cache -- no real network calls in this file.
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pypdf import PdfWriter
from sqlalchemy import select, text

from billcommons_ingest import cli as cli_mod
from billcommons_ingest import fulltext as fulltext_mod
from billcommons_ingest.cli import (
    _fetch_text_document_id,
    build_parser,
    classify_job_failure,
    cmd_reset_fetch_attempts,
    record_job_failure,
)
from billcommons_ingest.fulltext import (
    STATUS_FETCH_ERROR,
    STATUS_MA_DOCKET_NO_BILL_NUMBER,
    STATUS_MA_DOCKET_NOT_FOUND,
    STATUS_MALFORMED_URL,
    STATUS_OK,
    STATUS_OK_PARTIAL_PDF,
    STATUS_PERMANENTLY_FAILED,
    STATUS_ROBOTS_DISALLOWED,
    STATUS_SCANNED_PDF_NO_TEXT,
    STATUS_TOO_MANY_REDIRECTS,
    STATUS_UNSUPPORTED_REDIRECT_SCHEME,
    STATUS_WORKER_ERROR,
    MAX_FETCH_ATTEMPTS,
    TERMINAL_STATUSES,
    FETCH_TEXT_KIND,
    DocumentFetchError,
    FullTextFetcher,
    RobotsCache,
    UnfetchableDocument,
    _fetch_best_candidate,
    _resolve_ma_document,
    is_document_specific_failure,
    enqueue_fulltext_jobs,
    extract_document_text,
    extract_text_from_html,
    extract_text_from_pdf,
    extract_text_from_plain,
    extract_text_from_xml,
    process_fetch_text_job,
    sniff_content_type,
)
from billcommons_ingest.queue import claim_job, enqueue
from billcommons_ingest.url_resolvers import MaDocumentUrl, ma_docket_from_url
from billcommons_schema.models import Bill, BillDocument, BillVersion, IngestJob, Jurisdiction, Session as SessionModel
from billcommons_shared.db import get_session


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_bill_document(
    db_session, *, url="https://example-legislature.gov/bill.pdf", abbr=None, fetch_attempts: int = 0
):
    if abbr is None:
        abbr = f"ZQ_FT_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = Jurisdiction(name="Fulltext Test State", abbreviation=abbr, classification="state")
    db_session.add(jurisdiction)
    db_session.flush()
    session_row = SessionModel(jurisdiction_id=jurisdiction.id, identifier="2026 Session", active=True)
    db_session.add(session_row)
    db_session.flush()
    bill = Bill(
        jurisdiction_id=jurisdiction.id,
        session_id=session_row.id,
        identifier="HB 1",
        identifier_norm="HB 1",
        title="A test bill",
    )
    db_session.add(bill)
    db_session.flush()
    version = BillVersion(bill_id=bill.id, note="introduced")
    db_session.add(version)
    db_session.flush()
    document = BillDocument(
        bill_version_id=version.id, url=url, media_type=None, fetch_attempts=fetch_attempts
    )
    db_session.add(document)
    db_session.flush()
    return document


def _add_version(db_session, bill) -> BillVersion:
    """An extra BillVersion on an existing bill, for tests that need a bill
    carrying more than one document."""
    version = BillVersion(bill_id=bill.id, note="engrossed")
    db_session.add(version)
    db_session.flush()
    return version


def _fetch_text_jobs_for(db_session, document_ids) -> list[IngestJob]:
    """fetch_text ingest_jobs rows for SPECIFICALLY these document ids.

    This suite runs against a real, shared, live-schema Postgres DB (see
    conftest.py) that the production worker is concurrently reading/writing
    while tests run. An unscoped `IngestJob.kind == FETCH_TEXT_KIND` query
    (the pre-fix pattern) picks up the production worker's own in-flight
    fetch_text jobs alongside this test's fixture rows, which is flaky:
    `.all()` returns extra rows the test never created, and `.one()` raises
    MultipleResultsFound outright. Scoping by this test's own document ids
    (via the job payload, the same JSON-path pattern enqueue_fulltext_jobs
    itself already uses to detect already-queued jobs) makes the result set
    deterministic regardless of what else is happening on the shared DB.
    """
    document_id_strs = [str(d) for d in document_ids]
    return (
        db_session.query(IngestJob)
        .filter(
            IngestJob.kind == FETCH_TEXT_KIND,
            IngestJob.payload["document_id"].astext.in_(document_id_strs),
        )
        .all()
    )


def _build_tiny_pdf(*, with_text: bool) -> bytes:
    """Build a minimal valid single-page PDF via pypdf: a real page with
    extractable text (`with_text=True`), or a blank page with no text
    content at all (simulating a scanned/no-OCR-layer PDF)."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if with_text:
        # pypdf's writer has no direct "draw text" API; reportlab would add
        # a real dependency, so we build a minimal content stream by hand
        # for a page that pypdf's own reader can extract text from.
        page = writer.pages[0]
        content = (
            b"BT /F1 12 Tf 10 150 Td "
            b"(Section 1. This is the full bill text used for extraction testing.) Tj "
            b"0 -20 Td (It must be long enough to clear the scanned-PDF character threshold.) Tj "
            b"ET"
        )
        from pypdf.generic import ContentStream, DictionaryObject, NameObject, ArrayObject
        from pypdf.generic import IndirectObject

        cs = ContentStream(None, writer._objects and writer)
        cs.set_data(content)
        cs_ref = writer._add_object(cs)
        page[NameObject("/Contents")] = cs_ref

        # Minimal font resource so viewers/pypdf don't choke on /F1 (pypdf's
        # text extraction doesn't actually require valid font metrics to
        # find the literal string operands, but keep resources sane).
        resources = DictionaryObject()
        font_dict = DictionaryObject()
        font_dict[NameObject("/Type")] = NameObject("/Font")
        font_dict[NameObject("/Subtype")] = NameObject("/Type1")
        font_dict[NameObject("/BaseFont")] = NameObject("/Helvetica")
        font_ref = writer._add_object(font_dict)
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font_ref
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Extraction: HTML / XML / plain text
# ---------------------------------------------------------------------------


def test_extract_text_from_html_strips_tags_preserves_lines():
    html = b"""
    <html><head><title>ignored</title><style>.x{}</style></head>
    <body>
      <h1>SECTION 1</h1>
      <p>This bill amends the code.</p>
      <script>alert('should not appear')</script>
      <p>Second paragraph.</p>
    </body></html>
    """
    text = extract_text_from_html(html)
    assert "SECTION 1" in text
    assert "This bill amends the code." in text
    assert "Second paragraph." in text
    assert "alert" not in text
    assert "ignored" not in text
    # Block-level structure preserved: the two paragraphs are on different lines.
    lines = [l for l in text.split("\n") if l.strip()]
    assert any("amends the code" in l for l in lines)
    assert any("Second paragraph" in l for l in lines)


def test_extract_text_from_xml_preserves_section_breaks():
    xml = b"<?xml version='1.0'?><bill><section>Section one text.</section><section>Section two text.</section></bill>"
    text = extract_text_from_xml(xml)
    assert "Section one text." in text
    assert "Section two text." in text
    lines = [l for l in text.split("\n") if l.strip()]
    assert len(lines) >= 2


def test_extract_text_from_plain_normalizes_line_endings():
    raw = b"Line one\r\nLine two\rLine three\n"
    text = extract_text_from_plain(raw)
    assert "\r" not in text
    assert text.split("\n") == ["Line one", "Line two", "Line three"]


@pytest.mark.parametrize(
    "extractor, raw",
    [
        (extract_text_from_plain, b"An act concerning\x00 transportation"),
        (extract_text_from_html, b"<p>An act concerning\x00 transportation</p>"),
        (extract_text_from_xml, b"<section>An act concerning\x00 transportation</section>"),
    ],
)
def test_extractors_strip_nul_bytes(extractor, raw):
    """Extracted text must never contain NUL (0x00).

    Business intent: Postgres text columns reject NUL outright, so a document
    whose text carried one failed its `UPDATE bill_documents SET
    extracted_text` with psycopg.DataError, got retried, and was
    dead-lettered -- a fetchable document turned permanently dead, and its
    bill never counted toward full-text coverage, capping the jurisdiction
    below the GREEN bar forever. This happened in production: 215 jobs in 20
    minutes, collapsing crawl throughput from ~2,718/hr to ~66/hr. The
    surrounding text must survive; only the NUL is dropped.
    """
    text = extractor(raw)
    assert "\x00" not in text
    assert "An act concerning" in text
    assert "transportation" in text


# ---------------------------------------------------------------------------
# Extraction: PDF
# ---------------------------------------------------------------------------


def test_extract_text_from_pdf_with_real_text():
    pdf_bytes = _build_tiny_pdf(with_text=True)
    result = extract_text_from_pdf(pdf_bytes)
    assert result.page_count == 1
    assert result.scanned_no_text is False
    assert "bill text" in result.text.lower()


def test_extract_text_from_pdf_scanned_flags_no_text():
    pdf_bytes = _build_tiny_pdf(with_text=False)
    result = extract_text_from_pdf(pdf_bytes)
    assert result.page_count == 1
    assert result.scanned_no_text is True, "a page-having PDF with ~0 extractable chars must be flagged scanned"


def test_extract_text_from_pdf_salvages_pages_around_a_crashing_page(monkeypatch):
    """pypdf dies on malformed page internals (prod: "unsupported operand
    type(s) for +: 'float' and 'IndirectObject'"). A single broken page must
    not throw away the readable rest of the bill."""

    class _GoodPage:
        def extract_text(self):
            return "Section 1. The readable bill text survives. " * 10

    class _BrokenPage:
        def extract_text(self):
            raise TypeError("unsupported operand type(s) for +: 'float' and 'IndirectObject'")

    class _FakeReader:
        def __init__(self, *_a, **_k):
            self.pages = [_GoodPage(), _BrokenPage(), _GoodPage()]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
    result = extract_text_from_pdf(b"%PDF-fake")
    assert result.page_count == 3
    assert result.scanned_no_text is False
    assert result.broken_pages == 1
    assert "readable bill text survives" in result.text


def test_extract_document_text_partial_pdf_is_not_presented_as_fully_ok(monkeypatch):
    """Salvaged-but-incomplete text must carry STATUS_OK_PARTIAL_PDF, never
    plain STATUS_OK -- a consumer diffing versions against a silently-partial
    text would see a phantom "removed" section (codex verify finding)."""

    class _GoodPage:
        def extract_text(self):
            return "Section 1. The readable bill text survives. " * 10

    class _BrokenPage:
        def extract_text(self):
            raise TypeError("unsupported operand type(s) for +: 'float' and 'IndirectObject'")

    class _FakeReader:
        def __init__(self, *_a, **_k):
            self.pages = [_GoodPage(), _BrokenPage()]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
    outcome = extract_document_text("pdf", b"%PDF-fake")
    assert outcome.status == STATUS_OK_PARTIAL_PDF
    assert "readable bill text survives" in outcome.extracted_text
    assert "1/2 pages unreadable" in outcome.error


def test_extract_text_from_pdf_all_pages_crashing_flags_scanned(monkeypatch):
    """If EVERY page crashes, no text is salvageable -- the document must be
    downgraded via the scanned_no_text flag, never crash the worker and never
    present empty garbage as extracted text."""

    class _BrokenPage:
        def extract_text(self):
            raise TypeError("unsupported operand type(s) for +: 'float' and 'IndirectObject'")

    class _FakeReader:
        def __init__(self, *_a, **_k):
            self.pages = [_BrokenPage(), _BrokenPage()]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
    result = extract_text_from_pdf(b"%PDF-fake")
    assert result.page_count == 2
    assert result.scanned_no_text is True


def test_process_fetch_text_job_persists_partial_pdf_status(db_session, rawstore, monkeypatch):
    """End-to-end: a malformed-but-salvageable PDF must persist the salvaged
    text with fulltext_status=ok_partial_pdf (never plain ok) and reset
    fetch_attempts like any successful extraction."""

    class _GoodPage:
        def extract_text(self):
            return "Section 1. The readable bill text survives. " * 10

    class _BrokenPage:
        def extract_text(self):
            raise TypeError("unsupported operand type(s) for +: 'float' and 'IndirectObject'")

    class _FakeReader:
        def __init__(self, *_a, **_k):
            self.pages = [_GoodPage(), _BrokenPage()]

    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)

    routes = {
        "https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "https://origin.gov/partial.pdf": httpx.Response(
            200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"}
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    document = _make_bill_document(db_session, url="https://origin.gov/partial.pdf")
    document.fetch_attempts = 3

    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK_PARTIAL_PDF
    db_session.refresh(document)
    assert document.license_note == f"fulltext_status={STATUS_OK_PARTIAL_PDF}"
    assert "readable bill text survives" in document.extracted_text
    assert document.fetch_attempts == 0, "partial salvage is a success -- must reset the budget"


def test_extract_document_text_scanned_pdf_never_presents_garbage_as_text():
    pdf_bytes = _build_tiny_pdf(with_text=False)
    outcome = extract_document_text("pdf", pdf_bytes)
    assert outcome.status == STATUS_SCANNED_PDF_NO_TEXT
    assert outcome.extracted_text is None, "scanned PDFs must never write near-empty text as if authoritative"


def test_extract_document_text_real_pdf_ok():
    pdf_bytes = _build_tiny_pdf(with_text=True)
    outcome = extract_document_text("pdf", pdf_bytes)
    assert outcome.status == STATUS_OK
    assert "bill text" in outcome.extracted_text.lower()


# ---------------------------------------------------------------------------
# Content-type sniffing
# ---------------------------------------------------------------------------


def test_sniff_content_type_prefers_header():
    assert sniff_content_type("application/pdf", "https://x.gov/doc", b"") == "pdf"
    assert sniff_content_type("text/html; charset=utf-8", "https://x.gov/doc", b"") == "html"


def test_sniff_content_type_falls_back_to_url_extension():
    assert sniff_content_type(None, "https://x.gov/bill.pdf", b"") == "pdf"
    assert sniff_content_type(None, "https://x.gov/bill.htm", b"") == "html"


def test_sniff_content_type_falls_back_to_magic_bytes():
    assert sniff_content_type(None, "https://x.gov/doc", b"%PDF-1.4 ...") == "pdf"
    assert sniff_content_type(None, "https://x.gov/doc", b"<html><body>hi</body></html>") == "html"


# ---------------------------------------------------------------------------
# robots.txt gating
# ---------------------------------------------------------------------------


def _robots_client(robots_txt: str, doc_body: bytes = b"hello", doc_content_type: str = "text/plain"):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots_txt)
        return httpx.Response(200, content=doc_body, headers={"content-type": doc_content_type})

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="https://example-legislature.gov")


def test_robots_cache_disallows_blocked_path():
    client = _robots_client("User-agent: *\nDisallow: /private/\n")
    cache = RobotsCache(client=client)
    assert cache.can_fetch("https://example-legislature.gov/private/bill.pdf") is False
    assert cache.can_fetch("https://example-legislature.gov/public/bill.pdf") is True


def test_robots_cache_allows_all_when_robots_txt_missing():
    def handler(request):
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://example-legislature.gov")
    cache = RobotsCache(client=client)
    assert cache.can_fetch("https://example-legislature.gov/anything.pdf") is True


def test_fetcher_raises_unfetchable_when_robots_disallows():
    client = _robots_client("User-agent: *\nDisallow: /\n")
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    with pytest.raises(UnfetchableDocument):
        fetcher.fetch("https://example-legislature.gov/bill.pdf")


def _multi_host_transport(routes: dict) -> httpx.MockTransport:
    """A MockTransport that dispatches on the FULL URL (scheme+host+path),
    for tests that need more than one host in the same fetch (redirect
    chains) -- `_robots_client` above is single-host (uses `base_url`), which
    can't express a hop to a different origin.

    `routes` maps a full URL string to either an httpx.Response, or a
    2-tuple (status_code, {header: value}) shorthand for a redirect (e.g.
    (301, {"location": "https://other-host.gov/x"})).
    """

    def handler(request):
        url = str(request.url)
        route = routes.get(url)
        if route is None:
            raise AssertionError(f"no canned route for {url!r} (known routes: {list(routes)})")
        if isinstance(route, tuple):
            status_code, headers = route
            return httpx.Response(status_code, headers=headers, request=request)
        return httpx.Response(route.status_code, headers=route.headers, content=route.content, request=request)

    return httpx.MockTransport(handler)


def test_fetcher_rechecks_robots_on_cross_host_redirect():
    """Regression for Finding C: a redirect from an ALLOWED host to a
    DIFFERENT host whose robots.txt disallows the target path must still be
    blocked -- httpx's own `follow_redirects=True` would silently chase the
    redirect inside one client.get() call without ever consulting the
    second host's robots.txt."""
    routes = {
        "https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "https://origin.gov/bill.pdf": httpx.Response(
            302, headers={"location": "https://cdn-mirror.gov/bill.pdf"}
        ),
        "https://cdn-mirror.gov/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument, match="robots.txt disallows"):
        fetcher.fetch("https://origin.gov/bill.pdf")


def test_fetcher_consumes_second_hosts_rate_limit_token_on_redirect():
    """A cross-host redirect must consume the SECOND host's own rate-limit
    bucket, not just the first host's -- proves the second hop doesn't get
    a free pass on politeness just because the first hop was already
    rate-limited."""
    routes = {
        "https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "https://origin.gov/bill.pdf": httpx.Response(
            302, headers={"location": "https://cdn-mirror.gov/bill.pdf"}
        ),
        "https://cdn-mirror.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "https://cdn-mirror.gov/bill.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"hello from the mirror"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))

    from billcommons_shared.httpc import RateLimiter

    rate_limiter = RateLimiter(rate_per_sec=0.5, burst=1)
    acquired_hosts = []
    original_acquire = rate_limiter.acquire

    def _tracking_acquire(host):
        acquired_hosts.append(host)
        return original_acquire(host)

    rate_limiter.acquire = _tracking_acquire

    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client), rate_limiter=rate_limiter)
    response = fetcher.fetch("https://origin.gov/bill.pdf")

    assert response.status_code == 200
    assert response.content == b"hello from the mirror"
    assert acquired_hosts == ["origin.gov", "cdn-mirror.gov"], (
        "the rate limiter must be acquired for EACH hop's own host, not just the first"
    )


def test_fetcher_follows_redirect_chain_to_final_content():
    """A multi-hop same-and-cross-host redirect chain within the hop budget
    must still resolve to the final response's content -- the hop-by-hop
    rewrite must not break the ordinary "it eventually works" case."""
    routes = {
        "https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "https://origin.gov/a": httpx.Response(301, headers={"location": "https://origin.gov/b"}),
        "https://origin.gov/b": httpx.Response(302, headers={"location": "https://cdn-mirror.gov/c"}),
        "https://cdn-mirror.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "https://cdn-mirror.gov/c": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"final content"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response = fetcher.fetch("https://origin.gov/a")
    assert response.status_code == 200
    assert response.content == b"final content"


def test_fetcher_raises_on_too_many_redirects():
    """A redirect chain longer than MAX_REDIRECT_HOPS must raise
    UnfetchableDocument rather than looping/hanging or silently following an
    unbounded chain."""
    routes = {"https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n")}
    hop_count = 10
    for i in range(hop_count):
        routes[f"https://origin.gov/hop{i}"] = httpx.Response(
            302, headers={"location": f"https://origin.gov/hop{i + 1}"}
        )
    routes[f"https://origin.gov/hop{hop_count}"] = httpx.Response(200, content=b"never reached")

    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument, match="too many redirects"):
        fetcher.fetch("https://origin.gov/hop0")


# ---------------------------------------------------------------------------
# Finding 1 regression: distinct status per raise site + honest terminal/
# retriable classification (never all collapsed to STATUS_ROBOTS_DISALLOWED)
# ---------------------------------------------------------------------------


def test_fetcher_raises_too_many_redirects_carries_that_status():
    routes = {"https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n")}
    hop_count = 10
    for i in range(hop_count):
        routes[f"https://origin.gov/hop{i}"] = httpx.Response(
            302, headers={"location": f"https://origin.gov/hop{i + 1}"}
        )
    routes[f"https://origin.gov/hop{hop_count}"] = httpx.Response(200, content=b"never reached")

    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument) as excinfo:
        fetcher.fetch("https://origin.gov/hop0")
    assert excinfo.value.status == STATUS_TOO_MANY_REDIRECTS


def test_fetcher_raises_unsupported_redirect_scheme_carries_that_status():
    routes = {
        "https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        "https://origin.gov/bill.pdf": httpx.Response(
            302, headers={"location": "ftp://origin.gov/bill.pdf"}
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument) as excinfo:
        fetcher.fetch("https://origin.gov/bill.pdf")
    assert excinfo.value.status == STATUS_UNSUPPORTED_REDIRECT_SCHEME


def test_fetcher_raises_malformed_url_carries_that_status():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument) as excinfo:
        fetcher.fetch("not-a-url-at-all")
    assert excinfo.value.status == STATUS_MALFORMED_URL


def test_terminal_statuses_include_malformed_and_unsupported_scheme_but_not_redirects():
    """Documents honest terminal-vs-retriable status classification:
    robots/empty-url/malformed/unsupported-scheme are permanent facts about
    the source and must be dead-lettered/skipped forever; too_many_redirects
    is a transient condition (the target's redirect chain today doesn't
    guarantee its chain tomorrow) and must NOT be treated as terminal, so a
    transient redirect loop gets retried instead of permanently dead-lettered."""
    assert STATUS_MALFORMED_URL in TERMINAL_STATUSES
    assert STATUS_UNSUPPORTED_REDIRECT_SCHEME in TERMINAL_STATUSES
    assert STATUS_TOO_MANY_REDIRECTS not in TERMINAL_STATUSES
    assert STATUS_PERMANENTLY_FAILED in TERMINAL_STATUSES
    assert STATUS_FETCH_ERROR not in TERMINAL_STATUSES


def test_process_fetch_text_job_persists_actual_status_not_always_robots_disallowed(db_session, rawstore):
    """Regression for Finding 1: process_fetch_text_job's single `except
    UnfetchableDocument` used to hardcode STATUS_ROBOTS_DISALLOWED for EVERY
    fetch()-raised condition, mislabeling a too-many-redirects loop as a
    robots disallow. The persisted license_note (and the re-raised
    exception's .status) must reflect the ACTUAL condition fetcher.fetch hit."""
    routes = {"https://origin.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n")}
    hop_count = 10
    for i in range(hop_count):
        routes[f"https://origin.gov/hop{i}"] = httpx.Response(
            302, headers={"location": f"https://origin.gov/hop{i + 1}"}
        )
    routes[f"https://origin.gov/hop{hop_count}"] = httpx.Response(200, content=b"never reached")
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    document = _make_bill_document(db_session, url="https://origin.gov/hop0")

    with pytest.raises(UnfetchableDocument) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_TOO_MANY_REDIRECTS, (
        "a too-many-redirects loop must NOT be mislabeled as robots_disallowed"
    )
    db_session.refresh(document)
    assert document.license_note == f"fulltext_status={STATUS_TOO_MANY_REDIRECTS}"


def test_process_fetch_text_job_marks_robots_disallowed_not_bypassed(db_session, rawstore):
    document = _make_bill_document(db_session, url="https://example-legislature.gov/bill.pdf")
    client = _robots_client("User-agent: *\nDisallow: /\n")
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument):
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    db_session.refresh(document)
    assert document.extracted_text is None, "a robots-disallowed document must never have text written"
    assert document.license_note == "fulltext_status=robots_disallowed"


def test_unfetchable_document_carries_document_id_and_status_for_rollback_recovery(db_session, rawstore):
    """The worker loop (cli.py cmd_worker) must roll back the transaction
    process_fetch_text_job ran in (to clear any partial job-processing
    state) before dead-lettering the job -- but that rollback also discards
    the status write process_fetch_text_job already flushed. The exception
    must carry enough (document_id + status) for the caller to durably
    re-apply the SAME status in a fresh transaction; otherwise the
    dead-lettered document looks "never attempted" and gets re-enqueued
    forever (see the reproduction below)."""
    document = _make_bill_document(db_session, url="https://example-legislature.gov/bill.pdf")
    client = _robots_client("User-agent: *\nDisallow: /\n")
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.document_id == str(document.id)
    assert excinfo.value.status == STATUS_ROBOTS_DISALLOWED


def test_rollback_then_reapply_status_persists_and_stops_reenqueue(db_session, rawstore):
    """Reproduces the exact bug + proves the fix, using the same session
    fixture (a real, live-schema Postgres session under a SAVEPOINT, so this
    exercises the real rollback semantics cli.py relies on -- not a mock):

    1. process_fetch_text_job flushes a terminal status then raises
       UnfetchableDocument (robots disallow).
    2. The caller rolls back (mirrors cli.py's `db.rollback()` before
       dead-lettering) -- this must undo the status write with the OLD
       (buggy) code path, since it was never committed.
    3. The FIX: re-apply the same status from the exception and flush it
       (mirrors cli.py's fresh-session re-apply, done here in the same
       session/transaction for test isolation -- the mechanism under test
       is "does a rollback lose an unflushed-after-rollback status", which
       this reproduces faithfully).
    4. enqueue_fulltext_jobs must then skip this document forever (terminal
       status), proving finding 1's "re-enqueued forever" failure mode is
       closed.
    """
    document = _make_bill_document(db_session, url="https://example-legislature.gov/bill.pdf")
    document_id = document.id
    client = _robots_client("User-agent: *\nDisallow: /\n")
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    # An explicit inner SAVEPOINT scoped to just the process-and-rollback
    # sequence -- mirrors the real cli.py worker where the fixture rows
    # (jurisdiction/session/bill/document) already exist as committed state
    # from a prior job, and only THIS job's transaction (status write +
    # dead-letter attempt) gets rolled back, not the document row itself.
    nested = db_session.begin_nested()
    with pytest.raises(UnfetchableDocument) as excinfo:
        process_fetch_text_job(db_session, str(document_id), fetcher=fetcher, rawstore=rawstore)
    exc = excinfo.value

    # Step 2: rollback wipes the status write made inside this savepoint.
    nested.rollback()
    db_session.expire_all()
    rolled_back_document = db_session.get(BillDocument, document_id)
    assert rolled_back_document.license_note is None, (
        "sanity check: rollback really does wipe the status write when nothing "
        "re-applies it afterward -- this is the bug this test guards against"
    )

    # Step 3: the fix -- re-apply the SAME status the exception reports,
    # durably, in a transaction that survives (mirrors cli.py's fresh
    # get_session() + commit before/around dead_letter_job).
    assert exc.document_id and exc.status
    fixed_document = db_session.get(BillDocument, exc.document_id)
    fixed_document.license_note = f"fulltext_status={exc.status}"
    db_session.flush()

    # Step 4: enqueue_fulltext_jobs must never re-enqueue a document marked
    # with a terminal status, even though it has no extracted_text and a
    # non-null url (the two conditions that would otherwise make it eligible).
    count = enqueue_fulltext_jobs(db_session, document_ids=[document_id])
    assert count == 0, "a document marked with a terminal fulltext_status must never be re-enqueued"


# ---------------------------------------------------------------------------
# End-to-end fetch + extract (allowed path)
# ---------------------------------------------------------------------------


def test_process_fetch_text_job_html_end_to_end(db_session, rawstore, monkeypatch):
    # Archival is opt-in (FULLTEXT_ARCHIVE_RAW); enable it here to exercise the
    # raw-store path end-to-end. Extraction + checksum must work regardless.
    monkeypatch.setenv("FULLTEXT_ARCHIVE_RAW", "1")
    document = _make_bill_document(db_session, url="https://example-legislature.gov/bill.html")
    body = b"<html><body><p>Section 1. Hello legislature.</p></body></html>"
    client = _robots_client("User-agent: *\nAllow: /\n", doc_body=body, doc_content_type="text/html")
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK
    db_session.refresh(document)
    assert document.extracted_text is not None
    assert "Hello legislature." in document.extracted_text
    assert document.raw_ref is not None
    assert rawstore.exists(document.raw_ref)
    assert rawstore.get(document.raw_ref) == body
    assert document.checksum is not None
    assert document.parser_version == "fulltext/1"


def test_process_fetch_text_job_extracts_without_archival_by_default(db_session, rawstore):
    # Default (archival off): text + checksum must still land; raw_ref stays None
    # and nothing is written to the raw store (the volume-full safety path).
    document = _make_bill_document(db_session, url="https://example-legislature.gov/noarchive.html")
    body = b"<html><body><p>Section 2. No archival needed.</p></body></html>"
    client = _robots_client("User-agent: *\nAllow: /\n", doc_body=body, doc_content_type="text/html")
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK
    db_session.refresh(document)
    assert "No archival needed." in (document.extracted_text or "")
    assert document.raw_ref is None
    assert document.checksum is not None


# ---------------------------------------------------------------------------
# Idempotent enqueue
# ---------------------------------------------------------------------------


def test_enqueue_fulltext_jobs_skips_documents_already_queued(db_session):
    # NOTE: enqueue_fulltext_jobs has no per-jurisdiction scope -- it queries
    # bill_documents across the WHOLE live DB (this suite runs against a
    # real shared DB, not a throwaway test DB; see conftest.py). Passing
    # `document_ids=` restricts the scan to this fixture's own rows so the
    # test's counts stay meaningful without also enqueueing/flushing tens of
    # thousands of unrelated real-production job rows, which is slow enough
    # over the live DB's network latency to make the test hang (FIX 3).
    doc_with_url = _make_bill_document(db_session, url="https://example-legislature.gov/a.pdf")
    doc_no_url = _make_bill_document(db_session, url=None)
    doc_has_text = _make_bill_document(db_session, url="https://example-legislature.gov/b.pdf")
    doc_has_text.extracted_text = "already extracted"
    db_session.flush()
    fixture_ids = [doc_with_url.id, doc_no_url.id, doc_has_text.id]

    count = enqueue_fulltext_jobs(db_session, document_ids=fixture_ids)
    assert count == 1

    # Scoped to THIS fixture's document ids -- an unscoped query here also
    # picks up unrelated fetch_text jobs the live production worker is
    # concurrently creating/claiming against the shared DB (flaky under
    # load: see FIX round 6b finding 4).
    jobs = _fetch_text_jobs_for(db_session, fixture_ids)
    assert len(jobs) == 1
    assert jobs[0].payload["document_id"] == str(doc_with_url.id)


def test_enqueue_fulltext_jobs_is_idempotent_no_duplicate_jobs(db_session):
    document = _make_bill_document(db_session, url="https://example-legislature.gov/a.pdf")

    first_count = enqueue_fulltext_jobs(db_session, document_ids=[document.id])
    second_count = enqueue_fulltext_jobs(db_session, document_ids=[document.id])

    assert first_count == 1
    assert second_count == 0, "a document with a job already queued must not be enqueued again"

    # Scoped to this fixture's document id -- see comment in the previous
    # test for why an unscoped query is flaky against the live shared DB.
    jobs = _fetch_text_jobs_for(db_session, [document.id])
    assert len(jobs) == 1


def test_enqueue_fulltext_jobs_respects_limit(db_session):
    docs = [
        _make_bill_document(db_session, url=f"https://example-legislature.gov/{i}.pdf")
        for i in range(3)
    ]

    count = enqueue_fulltext_jobs(db_session, limit=2, document_ids=[d.id for d in docs])
    assert count == 2


def test_enqueue_prefers_a_bill_with_no_text_over_another_version_of_a_covered_one(db_session):
    """A limited batch must spend its slots on bills that have NO text yet.

    Business intent: a bill counts as covered once ANY of its documents has
    text, and bills carry ~3.6 documents. Draining them in created_at order
    spent ~3.6 fetches per bill of coverage gained, which is the difference
    between reaching the GREEN full-text bar in ~2 days and ~8. Revert the
    ordering to plain created_at and this fails: `covered_second` was created
    first, so it would win the single slot.
    """
    # One jurisdiction so the round-robin partition can't decide this -- the
    # only thing separating the two candidates is their bill's coverage.
    covered_doc = _make_bill_document(db_session, url="https://example-legislature.gov/covered-v1.pdf")
    covered_doc.extracted_text = "this bill already has text"
    covered_version = db_session.get(BillVersion, covered_doc.bill_version_id)
    covered_bill = db_session.get(Bill, covered_version.bill_id)

    # Second version of the SAME (already covered) bill -- lowest value.
    # created_at is set EXPLICITLY: Postgres now() is transaction-start time,
    # so rows inserted by one test all share an identical created_at and any
    # ordering assertion against it is a coin flip that passes either way.
    covered_second = BillDocument(
        bill_version_id=_add_version(db_session, covered_bill).id,
        url="https://example-legislature.gov/covered-v2.pdf",
        media_type=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(covered_second)
    db_session.flush()

    # A different bill in the same jurisdiction with no text at all -- the
    # one a limited batch should actually spend its slot on.
    uncovered_bill = Bill(
        jurisdiction_id=covered_bill.jurisdiction_id,
        session_id=covered_bill.session_id,
        identifier="HB 2",
        identifier_norm="HB 2",
        title="A bill with no text yet",
    )
    db_session.add(uncovered_bill)
    db_session.flush()
    uncovered_doc = BillDocument(
        bill_version_id=_add_version(db_session, uncovered_bill).id,
        url="https://example-legislature.gov/uncovered-v1.pdf",
        media_type=None,
        # NEWER than covered_second, so plain created_at ordering would pass
        # this over -- only the bill-coverage key promotes it.
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    db_session.add(uncovered_doc)
    db_session.flush()

    count = enqueue_fulltext_jobs(
        db_session, limit=1, document_ids=[covered_second.id, uncovered_doc.id]
    )

    assert count == 1
    jobs = _fetch_text_jobs_for(db_session, [covered_second.id, uncovered_doc.id])
    assert len(jobs) == 1
    assert jobs[0].payload["document_id"] == str(uncovered_doc.id)


def test_enqueue_fulltext_jobs_reenqueues_after_job_completes(db_session):
    """Once a fetch_text job is done/dead (no longer queued/running) and the
    document STILL lacks extracted_text, it should be eligible again --
    idempotency prevents duplicate in-flight jobs, not all future retries."""
    document = _make_bill_document(db_session, url="https://example-legislature.gov/a.pdf")
    enqueue_fulltext_jobs(db_session, document_ids=[document.id])
    # Scoped to this fixture's document id -- an unscoped `.one()` here
    # raises MultipleResultsFound against the live shared DB once the
    # production worker's own fetch_text jobs exist alongside this test's.
    job = _fetch_text_jobs_for(db_session, [document.id])[0]
    job.status = "dead"
    db_session.flush()

    count = enqueue_fulltext_jobs(db_session, document_ids=[document.id])
    assert count == 1


# ---------------------------------------------------------------------------
# TLS-intermediate repair (billcommons_shared.aia integration)
# ---------------------------------------------------------------------------

def _missing_issuer_error() -> httpx.ConnectError:
    import ssl
    inner = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1010)"
    )
    outer = httpx.ConnectError("connection failed")
    outer.__cause__ = inner
    return outer


class _FakeClient:
    """Client that raises `error` on the first N gets, then returns `response`."""

    def __init__(self, error=None, response=None):
        self.error = error
        self.response = response
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.response


class _AllowAllRobots:
    def __init__(self, allow=True):
        self.allow = allow
        self.invalidated: list[str] = []

    def can_fetch(self, url):
        return self.allow

    def invalidate(self, origin):
        self.invalidated.append(origin)


def test_missing_tls_intermediate_is_repaired_and_the_fetch_retried(monkeypatch):
    """MI, MS and CT serve only their leaf certificate. Unhandled, that made
    their ENTIRE full-text corpus permanently unfetchable (0 of 3,884 bills for
    MI, 0 of 4,006 for MS) while looking like an ordinary transient network
    error. A recovered intermediate must transparently rescue the fetch."""
    import ssl as _ssl

    url = "https://legislature.mi.gov/doc.htm"
    ok = httpx.Response(200, content=b"<html>bill text</html>", request=httpx.Request("GET", url))
    repaired_client = _FakeClient(response=ok)
    monkeypatch.setattr(fulltext_mod, "new_client", lambda **kw: repaired_client)

    fetcher = fulltext_mod.FullTextFetcher(
        client=_FakeClient(error=_missing_issuer_error()),
        rate_limiter=fulltext_mod.RateLimiter(rate_per_sec=1000.0, burst=1000),
        robots_cache=_AllowAllRobots(),
    )
    fetcher.aia_cache = type(
        "Cache", (), {"get": staticmethod(lambda host, port=443: _ssl.create_default_context())}
    )()

    response = fetcher.fetch(url)
    assert response.status_code == 200
    assert repaired_client.calls == [url]


def test_unrepairable_tls_failure_keeps_failing(monkeypatch):
    """When no genuine intermediate can be recovered, the original TLS error
    must surface. Silently succeeding here would mean verification had been
    downgraded rather than repaired."""
    fetcher = fulltext_mod.FullTextFetcher(
        client=_FakeClient(error=_missing_issuer_error()),
        rate_limiter=fulltext_mod.RateLimiter(rate_per_sec=1000.0, burst=1000),
        robots_cache=_AllowAllRobots(),
    )
    fetcher.aia_cache = type("Cache", (), {"get": staticmethod(lambda host, port=443: None)})()

    with pytest.raises(httpx.HTTPError):
        fetcher.fetch("https://broken.example/doc.htm")


def test_expired_certificate_is_never_treated_as_repairable():
    """Only a missing intermediate is repairable. An expired certificate is a
    real trust failure and must not trigger a repair attempt at all."""
    expired = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired (_ssl.c:1010)"
    )
    probed: list[str] = []
    fetcher = fulltext_mod.FullTextFetcher(
        client=_FakeClient(error=expired),
        rate_limiter=fulltext_mod.RateLimiter(rate_per_sec=1000.0, burst=1000),
        robots_cache=_AllowAllRobots(),
    )
    fetcher.aia_cache = type(
        "Cache", (), {"get": staticmethod(lambda host, port=443: probed.append(host))}
    )()

    with pytest.raises(httpx.HTTPError):
        fetcher.fetch("https://expired.example/doc.htm")
    assert probed == [], "an expired certificate must not even be probed for repair"


def test_robots_is_re_read_after_a_repair_and_still_obeyed(monkeypatch):
    """The allow-all robots fallback fires when robots.txt itself can't be
    fetched -- which is exactly what a broken cert chain causes. CT
    (www.cga.ct.gov) publishes a REAL robots.txt with Disallow paths, so the
    cert bug had us failing open on a host that genuinely restricts parts of
    itself. Once the connection works, its rules must be re-read and honoured.
    """
    import ssl as _ssl

    repaired_client = _FakeClient(response=httpx.Response(200, content=b"x"))
    monkeypatch.setattr(fulltext_mod, "new_client", lambda **kw: repaired_client)

    robots = _AllowAllRobots(allow=True)

    class _DisallowAfterRepair(_AllowAllRobots):
        def invalidate(self, origin):
            self.invalidated.append(origin)
            self.allow = False  # the real file, once readable, disallows this path

    robots = _DisallowAfterRepair(allow=True)
    fetcher = fulltext_mod.FullTextFetcher(
        client=_FakeClient(error=_missing_issuer_error()),
        rate_limiter=fulltext_mod.RateLimiter(rate_per_sec=1000.0, burst=1000),
        robots_cache=robots,
    )
    fetcher.aia_cache = type(
        "Cache", (), {"get": staticmethod(lambda host, port=443: _ssl.create_default_context())}
    )()

    with pytest.raises(fulltext_mod.UnfetchableDocument) as excinfo:
        fetcher.fetch("https://www.cga.ct.gov/html/secret.htm")
    assert excinfo.value.status == fulltext_mod.STATUS_ROBOTS_DISALLOWED
    assert robots.invalidated == ["https://www.cga.ct.gov"]
    assert repaired_client.calls == [], "must not fetch a disallowed URL after repair"


# ---------------------------------------------------------------------------
# Bounded retry (fetch_attempts / MAX_FETCH_ATTEMPTS / STATUS_PERMANENTLY_FAILED)
# ---------------------------------------------------------------------------


def test_fetch_error_status_survives_rollback_via_record_job_failure(unique_kind):
    """Proves (c): a fetch_text job claimed then rolled back must still land
    a durable STATUS_FETCH_ERROR license_note + an incremented fetch_attempts
    once record_job_failure runs in a fresh session -- mirrors cli.py's
    generic `except Exception` handler in cmd_worker."""
    kind = unique_kind()
    setup = get_session()
    try:
        document = _make_bill_document(setup, url="https://example-legislature.gov/rollback.pdf")
        document_id = document.id
        job = enqueue(setup, kind, {"document_id": str(document_id)})
        setup.commit()
        job_id = job.id
    finally:
        setup.close()

    claiming = get_session()
    try:
        claimed = claim_job(claiming, "worker-under-test", kind=kind)
        assert claimed is not None
        claimed_attempts = claimed.attempts
        claiming.rollback()
    finally:
        claiming.close()

    try:
        record_job_failure(
            job_id,
            IngestJob,
            claimed_attempts=claimed_attempts,
            error="connection reset",
            document_id=str(document_id),
            document_status=STATUS_FETCH_ERROR,
            count_attempt=True,
            session_factory=get_session,
        )

        check = get_session()
        try:
            doc = check.get(BillDocument, document_id)
            assert doc.license_note == "fulltext_status=fetch_error"
            assert doc.fetch_attempts == 1
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            row = cleanup.get(IngestJob, job_id)
            if row is not None:
                cleanup.delete(row)
                cleanup.commit()
        finally:
            cleanup.close()


def test_fetch_attempts_increments_on_each_recorded_failure(unique_kind):
    kind = unique_kind()
    setup = get_session()
    try:
        document = _make_bill_document(setup, url="https://example-legislature.gov/increments.pdf")
        document_id = document.id
        job = enqueue(setup, kind, {"document_id": str(document_id)})
        setup.commit()
        job_id = job.id
    finally:
        setup.close()

    try:
        for attempt in range(1, 4):
            record_job_failure(
                job_id,
                IngestJob,
                claimed_attempts=attempt,
                error="transient failure",
                document_id=str(document_id),
                document_status=STATUS_FETCH_ERROR,
                count_attempt=True,
                session_factory=get_session,
            )

        check = get_session()
        try:
            doc = check.get(BillDocument, document_id)
            assert doc.fetch_attempts == 3
            assert doc.license_note == "fulltext_status=fetch_error"
            job = check.get(IngestJob, job_id)
            assert job.status != "dead"
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            row = cleanup.get(IngestJob, job_id)
            if row is not None:
                cleanup.delete(row)
                cleanup.commit()
        finally:
            cleanup.close()


def test_document_at_fetch_attempt_cap_is_marked_permanently_failed_and_dead_lettered(unique_kind):
    kind = unique_kind()
    setup = get_session()
    try:
        document = _make_bill_document(
            setup,
            url="https://example-legislature.gov/atcap.pdf",
            fetch_attempts=MAX_FETCH_ATTEMPTS - 1,
        )
        document_id = document.id
        job = enqueue(setup, kind, {"document_id": str(document_id)})
        setup.commit()
        job_id = job.id
    finally:
        setup.close()

    try:
        record_job_failure(
            job_id,
            IngestJob,
            claimed_attempts=1,
            error="one more failure to tip it over the cap",
            document_id=str(document_id),
            document_status=STATUS_FETCH_ERROR,
            count_attempt=True,
            session_factory=get_session,
        )

        check = get_session()
        try:
            doc = check.get(BillDocument, document_id)
            assert doc.license_note == "fulltext_status=permanently_failed"
            assert doc.fetch_attempts == MAX_FETCH_ATTEMPTS
            job = check.get(IngestJob, job_id)
            assert job.status == "dead"
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            row = cleanup.get(IngestJob, job_id)
            if row is not None:
                cleanup.delete(row)
                cleanup.commit()
        finally:
            cleanup.close()


def test_enqueue_fulltext_jobs_skips_document_at_fetch_attempt_cap(db_session):
    """Proves (a): fetch_attempts alone is enough to stop re-enqueueing, even
    with a null license_note (never marked terminal by status)."""
    document = _make_bill_document(
        db_session,
        url="https://example-legislature.gov/capped.pdf",
        fetch_attempts=MAX_FETCH_ATTEMPTS,
    )

    count = enqueue_fulltext_jobs(db_session, document_ids=[document.id])
    assert count == 0


def test_enqueue_fulltext_jobs_still_retries_transient_failure_under_cap(db_session):
    """Proves (b): a document one attempt under the cap, marked fetch_error,
    is still eligible for another try."""
    document = _make_bill_document(
        db_session,
        url="https://example-legislature.gov/undercap.pdf",
        fetch_attempts=MAX_FETCH_ATTEMPTS - 1,
    )
    document.license_note = f"fulltext_status={STATUS_FETCH_ERROR}"
    db_session.flush()

    count = enqueue_fulltext_jobs(db_session, document_ids=[document.id])
    assert count == 1
    jobs = _fetch_text_jobs_for(db_session, [document.id])
    assert len(jobs) == 1


def test_oversized_extracted_text_is_stored_whole_and_indexed_within_tsvector_limit(db_session):
    """Bug 2: a document whose extracted_text would produce >1,048,575 bytes
    of lexeme data under the OLD unbounded tsvector expression must still
    flush without error under migration 0018's bounded expression, and the
    full (untruncated) extracted_text must be stored -- only the tsvector
    input is bounded, never extracted_text itself."""
    document = _make_bill_document(db_session, url="https://example-legislature.gov/huge.txt")
    big_text = " ".join(f"w{i}" for i in range(200_000))  # ~1.4MB, all-distinct lexemes
    document.extracted_text = big_text
    db_session.flush()

    db_session.refresh(document)
    assert len(document.extracted_text) == len(big_text)

    has_tsv = db_session.execute(
        text("select text_tsv is not null from bill_documents where id = :id"),
        {"id": document.id},
    ).scalar()
    assert has_tsv is True


# ---------------------------------------------------------------------------
# Which failures may spend a document's retry budget (review finding 1)
# ---------------------------------------------------------------------------


def test_only_document_specific_failures_are_classified_as_budget_burning():
    """The whole safety property in one assertion set: a failure that is the
    DOCUMENT's (its host, its bytes, a data error on its own row) may cost it
    an attempt; a failure that is OURS (connection dropped, disk full, a bug
    in this worker) may not -- otherwise an infra outage marks every in-flight
    document permanently_failed, which nothing re-enqueues."""

    class _FakeDriverError(Exception):
        def __init__(self, sqlstate):
            super().__init__(sqlstate)
            self.sqlstate = sqlstate

    assert is_document_specific_failure(DocumentFetchError("host 500s", document_id="x")) is True
    assert is_document_specific_failure(UnfetchableDocument("robots", status=STATUS_ROBOTS_DISALLOWED)) is True
    # 54000 program_limit_exceeded == "string is too long for tsvector", the
    # failure that cost 309 documents their text: that IS this document's data.
    assert is_document_specific_failure(_FakeDriverError("54000")) is True
    assert is_document_specific_failure(_FakeDriverError("22021")) is True  # invalid byte sequence
    assert is_document_specific_failure(_FakeDriverError("23505")) is True  # unique violation
    # ...and the ones that must never be charged to a document:
    assert is_document_specific_failure(_FakeDriverError("08006")) is False  # connection failure
    assert is_document_specific_failure(_FakeDriverError("53100")) is False  # disk full
    assert is_document_specific_failure(_FakeDriverError("57014")) is False  # statement timeout
    assert is_document_specific_failure(RuntimeError("some new bug of ours")) is False
    assert is_document_specific_failure(MemoryError()) is False


def test_dbapi_error_is_classified_from_the_wrapped_driver_exception():
    """SQLAlchemy wraps the driver exception; the SQLSTATE lives on .orig."""
    from sqlalchemy.exc import DBAPIError

    class _Orig(Exception):
        sqlstate = "54000"

    wrapped = DBAPIError("UPDATE bill_documents ...", {}, _Orig())
    assert is_document_specific_failure(wrapped) is True

    class _OrigConn(Exception):
        sqlstate = "08006"

    assert is_document_specific_failure(DBAPIError("stmt", {}, _OrigConn())) is False


def test_infrastructure_failure_records_worker_error_without_spending_the_budget(unique_kind):
    """The outage scenario from the review: the failure is recorded (so it is
    visible and greppable) but fetch_attempts does NOT move, so an hour of
    dead credentials cannot exhaust a document's budget, and the document
    stays eligible for the next enqueue pass."""
    kind = unique_kind()
    setup = get_session()
    try:
        document = _make_bill_document(setup, url="https://example-legislature.gov/outage.pdf")
        document_id = document.id
        job = enqueue(setup, kind, {"document_id": str(document_id)})
        setup.commit()
        job_id = job.id
    finally:
        setup.close()

    try:
        for _ in range(MAX_FETCH_ATTEMPTS + 5):
            record_job_failure(
                job_id,
                IngestJob,
                claimed_attempts=1,
                error="connection pool exhausted",
                document_id=str(document_id),
                document_status=STATUS_WORKER_ERROR,
                count_attempt=False,
                session_factory=get_session,
            )

        check = get_session()
        try:
            doc = check.get(BillDocument, document_id)
            assert doc.fetch_attempts == 0, "an infra failure must not spend the document's budget"
            assert doc.license_note == f"fulltext_status={STATUS_WORKER_ERROR}"
            assert STATUS_WORKER_ERROR not in TERMINAL_STATUSES
            assert enqueue_fulltext_jobs(check, document_ids=[document_id]) == 1
            check.rollback()
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            row = cleanup.get(IngestJob, job_id)
            if row is not None:
                cleanup.delete(row)
                cleanup.commit()
        finally:
            cleanup.close()


def test_http_failure_raises_document_fetch_error_carrying_the_document(db_session, rawstore):
    """A dead host is the document's own problem, so the worker must be able
    to tell -- a bare RuntimeError was indistinguishable from our own crash."""
    document = _make_bill_document(db_session, url="https://example-legislature.gov/dead.pdf")

    def _handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_FETCH_ERROR
    assert excinfo.value.document_id == str(document.id)
    assert is_document_specific_failure(excinfo.value) is True


def test_extraction_crash_is_attributed_to_the_document(db_session, rawstore, monkeypatch):
    """The unhandled pypdf crash behind most of the 84k dead fetch_text rows:
    it used to escape as a bare exception into the worker's generic handler,
    which had no document identity, so nothing was ever recorded and the
    document was re-enqueued for ever."""
    document = _make_bill_document(db_session, url="https://example-legislature.gov/poison.pdf")
    client = _robots_client("User-agent: *\nAllow: /\n", doc_body=b"%PDF-1.4 broken", doc_content_type="application/pdf")
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    def _boom(content_type, raw):
        raise ValueError("pypdf exploded on this document")

    monkeypatch.setattr(fulltext_mod, "extract_document_text", _boom)

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.document_id == str(document.id)
    assert excinfo.value.status == STATUS_FETCH_ERROR


def test_mixed_candidate_error_and_empty_response_charges_and_preserves_text(rawstore, monkeypatch, unique_kind):
    """T2-2: process-level accounting still sees the error, so the worker
    charges it instead of overwriting an existing document with empty text."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ia", "HF 1"))
    source_url = "https://www.legis.iowa.gov/docs/publications/LGEG/91/HF1.pdf"
    replacement_url = "https://www.legis.iowa.gov/docs/publications/LGI/91/HF1.pdf"
    routes = {
        "https://www.legis.iowa.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        source_url: httpx.Response(404),
        replacement_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=source_url)
        document.extracted_text = "previously stored official text"
        document_id = document.id
        job = enqueue(setup, unique_kind(), {"document_id": str(document_id)})
        job_id = job.id
        setup.commit()
    finally:
        setup.close()

    try:
        worker_db = get_session()
        try:
            with pytest.raises(DocumentFetchError) as excinfo:
                process_fetch_text_job(worker_db, str(document_id), fetcher=fetcher, rawstore=rawstore)
            worker_db.rollback()
        finally:
            worker_db.close()

        assert excinfo.value.status == STATUS_FETCH_ERROR
        assert classify_job_failure(str(document_id), excinfo.value) == (STATUS_FETCH_ERROR, True)
        record_job_failure(
            job_id,
            IngestJob,
            claimed_attempts=1,
            error=str(excinfo.value),
            document_id=str(document_id),
            document_status=excinfo.value.status,
            count_attempt=True,
            session_factory=get_session,
        )

        check = get_session()
        try:
            document = check.get(BillDocument, document_id)
            assert document.fetch_attempts == 1
            assert document.extracted_text == "previously stored official text"
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            job = cleanup.get(IngestJob, job_id)
            if job is not None:
                cleanup.delete(job)
                cleanup.commit()
        finally:
            cleanup.close()


def test_ma_fallback_extraction_crash_is_a_charged_document_failure(rawstore, monkeypatch, unique_kind):
    """T2-3: a bare parser crash in the MA fallback page is converted before
    it can escape to the worker's unclassified-error path."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "S 2045"))
    monkeypatch.setattr(fulltext_mod, "extract_document_text", lambda content_type, raw: (_ for _ in ()).throw(ValueError("pypdf exploded")))
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H2045": _json_response(
            {"DocketNumber": "HD99", "BillNumber": "H2045", "DocumentText": ""}
        ),
        "https://malegislature.gov/Bills/194/H2045.pdf": httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4 broken"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    setup = get_session()
    try:
        document = _make_bill_document(setup, url="https://malegislature.gov/Bills/194/H2045.pdf")
        document_id = document.id
        job = enqueue(setup, unique_kind(), {"document_id": str(document_id)})
        job_id = job.id
        setup.commit()
    finally:
        setup.close()

    try:
        worker_db = get_session()
        try:
            with pytest.raises(DocumentFetchError) as excinfo:
                process_fetch_text_job(worker_db, str(document_id), fetcher=fetcher, rawstore=rawstore)
            worker_db.rollback()
        finally:
            worker_db.close()

        assert excinfo.value.status == STATUS_FETCH_ERROR
        assert classify_job_failure(str(document_id), excinfo.value) == (STATUS_FETCH_ERROR, True)
        record_job_failure(
            job_id,
            IngestJob,
            claimed_attempts=1,
            error=str(excinfo.value),
            document_id=str(document_id),
            document_status=excinfo.value.status,
            count_attempt=True,
            session_factory=get_session,
        )

        check = get_session()
        try:
            document = check.get(BillDocument, document_id)
            assert document.fetch_attempts == 1
            assert document.license_note == f"fulltext_status={STATUS_FETCH_ERROR}"
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            job = cleanup.get(IngestJob, job_id)
            if job is not None:
                cleanup.delete(job)
                cleanup.commit()
        finally:
            cleanup.close()


def test_document_failure_survives_a_purged_job_row(unique_kind):
    """Review finding 5: the 84k dead ingest_jobs rows are slated for a purge,
    so a cleanup can race a failure record. If record_job_failure returns
    early on the missing job WITHOUT committing, the increment and the
    permanently_failed transition are silently discarded and the document
    keeps its poison-loop eligibility."""
    setup = get_session()
    try:
        document = _make_bill_document(
            setup,
            url="https://example-legislature.gov/purged-job.pdf",
            fetch_attempts=MAX_FETCH_ATTEMPTS - 1,
        )
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    record_job_failure(
        uuid.uuid4(),  # a job row that does not exist (already purged)
        IngestJob,
        claimed_attempts=1,
        error="host is gone",
        document_id=str(document_id),
        document_status=STATUS_FETCH_ERROR,
        count_attempt=True,
        session_factory=get_session,
    )

    check = get_session()
    try:
        doc = check.get(BillDocument, document_id)
        assert doc.fetch_attempts == MAX_FETCH_ATTEMPTS
        assert doc.license_note == f"fulltext_status={STATUS_PERMANENTLY_FAILED}"
    finally:
        check.close()


def test_recent_ma_docket_without_bill_number_does_not_spend_fetch_attempts(unique_kind):
    """T2-6: recent pending dockets stay free while MA assigns a bill number."""
    kind = unique_kind()
    setup = get_session()
    try:
        document = _make_bill_document(setup, url="https://malegislature.gov/Bills/194/HD177.pdf")
        document_id = document.id
        job = enqueue(setup, kind, {"document_id": str(document_id)})
        job_id = job.id
        setup.commit()
    finally:
        setup.close()

    try:
        for _ in range(MAX_FETCH_ATTEMPTS + 5):
            record_job_failure(
                job_id,
                IngestJob,
                claimed_attempts=1,
                error="MA docket has not yet been assigned a bill number",
                document_id=str(document_id),
                document_status=STATUS_MA_DOCKET_NO_BILL_NUMBER,
                count_attempt=True,
                session_factory=get_session,
            )

        check = get_session()
        try:
            document = check.get(BillDocument, document_id)
            assert document.fetch_attempts == 0
            assert document.license_note == f"fulltext_status={STATUS_MA_DOCKET_NO_BILL_NUMBER}"
            assert document.license_note != f"fulltext_status={STATUS_PERMANENTLY_FAILED}"
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            job = cleanup.get(IngestJob, job_id)
            if job is not None:
                cleanup.delete(job)
                cleanup.commit()
        finally:
            cleanup.close()


def test_old_ma_docket_without_bill_number_starts_spending_fetch_attempts(unique_kind):
    """T3-5: the no-charge retry exemption expires after 180 days."""
    kind = unique_kind()
    setup = get_session()
    try:
        document = _make_bill_document(setup, url="https://malegislature.gov/Bills/194/HD177.pdf")
        document.created_at = datetime.now(timezone.utc) - timedelta(days=181)
        document_id = document.id
        job = enqueue(setup, kind, {"document_id": str(document_id)})
        job_id = job.id
        setup.commit()
    finally:
        setup.close()

    try:
        for _ in range(MAX_FETCH_ATTEMPTS):
            record_job_failure(
                job_id,
                IngestJob,
                claimed_attempts=1,
                error="MA docket has not yet been assigned a bill number",
                document_id=str(document_id),
                document_status=STATUS_MA_DOCKET_NO_BILL_NUMBER,
                count_attempt=True,
                session_factory=get_session,
            )

        check = get_session()
        try:
            document = check.get(BillDocument, document_id)
            assert document.fetch_attempts == MAX_FETCH_ATTEMPTS
            assert document.license_note == f"fulltext_status={STATUS_PERMANENTLY_FAILED}"
        finally:
            check.close()
    finally:
        cleanup = get_session()
        try:
            job = cleanup.get(IngestJob, job_id)
            if job is not None:
                cleanup.delete(job)
                cleanup.commit()
        finally:
            cleanup.close()


def test_record_job_failure_treats_naive_created_at_as_utc():
    document = SimpleNamespace(
        created_at=datetime.now() - timedelta(days=181), fetch_attempts=0, license_note=None
    )

    class FakeSession:
        def get(self, _model, _id, *, with_for_update=False):
            return document if with_for_update else None

        def commit(self):
            pass

        def close(self):
            pass

    record_job_failure(
        "missing-job",
        object,
        claimed_attempts=1,
        error="MA docket has not yet been assigned a bill number",
        document_id="document-id",
        document_status=STATUS_MA_DOCKET_NO_BILL_NUMBER,
        count_attempt=True,
        session_factory=FakeSession,
    )

    assert document.fetch_attempts == 1


def test_record_job_failure_charges_missing_created_at_outside_grace():
    document = SimpleNamespace(created_at=None, fetch_attempts=0, license_note=None)

    class FakeSession:
        def get(self, _model, _id, *, with_for_update=False):
            return document if with_for_update else None

        def commit(self):
            pass

        def close(self):
            pass

    record_job_failure(
        "missing-job",
        object,
        claimed_attempts=1,
        error="MA docket has not yet been assigned a bill number",
        document_id="document-id",
        document_status=STATUS_MA_DOCKET_NO_BILL_NUMBER,
        count_attempt=True,
        session_factory=FakeSession,
    )

    assert document.fetch_attempts == 1


def test_claimed_document_id_helper_never_raises_on_a_malformed_payload():
    """Review finding 4: this runs OUTSIDE the worker's per-job try/except, so
    an AttributeError here escapes the worker loop entirely and leaves the job
    `running` for ever, holding a queue slot above the top-up floor."""

    class _Job:
        def __init__(self, kind, payload):
            self.kind = kind
            self.payload = payload

    assert _fetch_text_document_id(_Job(FETCH_TEXT_KIND, None)) is None
    assert _fetch_text_document_id(_Job(FETCH_TEXT_KIND, ["not", "a", "dict"])) is None
    assert _fetch_text_document_id(_Job(FETCH_TEXT_KIND, {})) is None
    assert _fetch_text_document_id(_Job("api_sync", {"document_id": "x"})) is None
    assert _fetch_text_document_id(_Job(FETCH_TEXT_KIND, {"document_id": "abc"})) == "abc"


# ---------------------------------------------------------------------------
# reset-fetch-attempts: the operational way back (review finding 1)
# ---------------------------------------------------------------------------


def _reset_args(**kw):
    import argparse

    return argparse.Namespace(
        **{
            "document_id": None,
            "url_like": None,
            "status": None,
            "jurisdiction": None,
            "only_permanently_failed": False,
            "all": False,
            "limit": None,
            "dry_run": False,
            **kw,
        }
    )


def test_reset_fetch_attempts_makes_a_permanently_failed_document_eligible_again():
    marker = f"https://reset-test-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=MAX_FETCH_ATTEMPTS)
        document.license_note = f"fulltext_status={STATUS_PERMANENTLY_FAILED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    check = get_session()
    try:
        assert enqueue_fulltext_jobs(check, document_ids=[document_id]) == 0, "precondition: excluded"
        check.rollback()
    finally:
        check.close()

    assert cmd_reset_fetch_attempts(_reset_args(url_like=marker)) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0
        assert doc.license_note is None
        assert enqueue_fulltext_jobs(after, document_ids=[document_id]) == 1
        after.rollback()
    finally:
        after.close()


def test_reset_fetch_attempts_clears_stamped_permanently_failed_note():
    """R3-1: browser-fetch stamps a bounded-retry `permanently_failed` row
    with a trailing `browser_attempted_at=...` suffix. The old exact-match
    `license_note.in_(notes)` cleared `fetch_attempts` back to 0 for such a
    row (via the outer `fetch_attempts > 0` OR-clause) but never cleared its
    license_note -- so it stayed excluded from enqueue_fulltext_jobs by the
    `LIKE 'fulltext_status=permanently_failed %'` clause even after the
    documented recovery lever ran, a silent no-op for exactly the rows
    browser-fetch touched."""
    marker = f"https://reset-stamped-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=MAX_FETCH_ATTEMPTS)
        document.license_note = (
            f"fulltext_status={STATUS_PERMANENTLY_FAILED} browser_attempted_at=2026-08-01T00:00:00+00:00"
        )
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    check = get_session()
    try:
        assert enqueue_fulltext_jobs(check, document_ids=[document_id]) == 0, "precondition: excluded"
        check.rollback()
    finally:
        check.close()

    assert cmd_reset_fetch_attempts(_reset_args(url_like=marker)) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0
        assert doc.license_note is None
        assert enqueue_fulltext_jobs(after, document_ids=[document_id]) == 1
        after.rollback()
    finally:
        after.close()


def test_reset_fetch_attempts_refuses_to_run_unfiltered():
    """An unfiltered reset hands ~690k documents a fresh budget and re-opens
    the poison loop the counter exists to close."""
    assert cmd_reset_fetch_attempts(_reset_args()) == 2


def test_reset_fetch_attempts_leaves_a_robots_disallowed_verdict_alone():
    """Politeness is not collateral damage of an outage cleanup: only the
    statuses named by --status (default permanently_failed/worker_error) are
    cleared."""
    marker = f"https://reset-robots-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=3)
        document.license_note = f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    assert cmd_reset_fetch_attempts(_reset_args(url_like=marker)) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0, "the counter is reset"
        assert doc.license_note == f"fulltext_status={STATUS_ROBOTS_DISALLOWED}", (
            "a robots.txt verdict must survive an operational reset"
        )
    finally:
        after.close()


def test_reset_fetch_attempts_requeues_robots_only_for_a_configured_exempt_host(monkeypatch):
    configured_marker = f"https://configured-{uuid.uuid4().hex}.gov/bill.pdf"
    unknown_marker = f"https://unknown-{uuid.uuid4().hex}.gov/bill.pdf"
    configured_host = configured_marker.split("/")[2]
    setup = get_session()
    try:
        configured = _make_bill_document(setup, url=configured_marker, fetch_attempts=2)
        unknown = _make_bill_document(setup, url=unknown_marker, fetch_attempts=2)
        configured.license_note = f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
        unknown.license_note = f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
        configured_id, unknown_id = configured.id, unknown.id
        setup.commit()
    finally:
        setup.close()

    monkeypatch.setattr(
        "billcommons_ingest.cli.host_auth_mod.robots_exempt_hosts", lambda: frozenset({configured_host})
    )
    assert cli_mod.host_auth_mod.robots_exempt_hosts() == frozenset({configured_host})
    precheck = get_session()
    try:
        assert precheck.execute(
            select(BillDocument.id).where(
                BillDocument.id == configured_id,
                BillDocument.license_note == f"fulltext_status={STATUS_ROBOTS_DISALLOWED}",
                cli_mod._robots_exempt_url_filter(frozenset({configured_host})),
            )
        ).scalar_one() == configured_id
    finally:
        precheck.close()
    assert cmd_reset_fetch_attempts(
        _reset_args(url_like=configured_marker, status=STATUS_ROBOTS_DISALLOWED)
    ) == 0
    assert cmd_reset_fetch_attempts(
        _reset_args(url_like=unknown_marker, status=STATUS_ROBOTS_DISALLOWED)
    ) == 0

    check = get_session()
    try:
        assert check.get(BillDocument, configured_id).license_note is None
        assert check.get(BillDocument, unknown_id).license_note == f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
    finally:
        check.close()


def test_reset_fetch_attempts_requeues_robots_for_a_bare_host_url_no_path_no_query(monkeypatch):
    """R2 fixlist #2: the LIKE filter used to require a literal `/` or `:`
    right after the host, so a stored URL with no path/query/port at all
    (`https://{host}` exactly) matched none of the four patterns even though
    `urlsplit(url).hostname` at runtime treats it as the exempt host -- such
    a row would stay terminally robots_disallowed forever."""
    configured_host = f"bare-host-{uuid.uuid4().hex}.gov"
    marker = f"https://{configured_host}"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=2)
        document.license_note = f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    monkeypatch.setattr(
        "billcommons_ingest.cli.host_auth_mod.robots_exempt_hosts", lambda: frozenset({configured_host})
    )
    assert cmd_reset_fetch_attempts(
        _reset_args(url_like=marker, status=STATUS_ROBOTS_DISALLOWED)
    ) == 0

    check = get_session()
    try:
        doc = check.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0
        assert doc.license_note is None
    finally:
        check.close()


def test_reset_fetch_attempts_leaves_an_http_url_note_robots_disallowed(monkeypatch):
    """R3 fixlist #4: the reset filter is https-only -- `host_auth.robots_exempt`
    always returns False for a non-https URL, so an http:// stored row for an
    otherwise-configured, exempt host must never satisfy the host-exemption
    filter: its `robots_disallowed` license_note must survive the reset (it
    would otherwise be cleared here and immediately re-marked
    robots_disallowed on the next fetch, wasting a cycle)."""
    configured_host = f"http-only-{uuid.uuid4().hex}.gov"
    marker = f"http://{configured_host}/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=2)
        document.license_note = f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    monkeypatch.setattr(
        "billcommons_ingest.cli.host_auth_mod.robots_exempt_hosts", lambda: frozenset({configured_host})
    )
    assert cmd_reset_fetch_attempts(
        _reset_args(url_like=marker, status=STATUS_ROBOTS_DISALLOWED)
    ) == 0

    check = get_session()
    try:
        doc = check.get(BillDocument, document_id)
        assert doc.license_note == f"fulltext_status={STATUS_ROBOTS_DISALLOWED}", (
            "an http:// row must never be treated as exempt-host-authorized"
        )
    finally:
        check.close()


def test_robots_exempt_url_filter_emits_no_http_pattern():
    """R3 fixlist #4: the reset filter's compiled SQL must contain no
    `http://` LIKE pattern at all -- only https:// belongs in an https-only
    exemption filter."""
    compiled = str(
        cli_mod._robots_exempt_url_filter(frozenset({"example.gov"})).compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    assert "http://" not in compiled
    assert "https://" in compiled


def test_reset_fetch_attempts_url_like_filter_does_not_over_match_underscore_host(monkeypatch):
    """R2 fixlist #2: `_` is a SQL LIKE wildcard; a configured host string
    must be escaped before being embedded in the LIKE pattern, or a
    differently-spelled host in a stored URL (any single char where the
    configured host has `_`) would over-match."""
    configured_host = f"under_score-{uuid.uuid4().hex}.gov"
    lookalike_host = configured_host.replace("_", "X", 1)
    marker = f"https://{lookalike_host}/bill.pdf"
    setup = get_session()
    try:
        # fetch_attempts=0: this row's scope-eligibility for the reset
        # depends ENTIRELY on the resettable_note_filter (robots status +
        # host LIKE filter) under test -- not on the always-true
        # `fetch_attempts > 0` branch of the reset's OR-scope, which would
        # mask the host-filter bug being proven here.
        document = _make_bill_document(setup, url=marker, fetch_attempts=0)
        document.license_note = f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    monkeypatch.setattr(
        "billcommons_ingest.cli.host_auth_mod.robots_exempt_hosts", lambda: frozenset({configured_host})
    )
    assert cmd_reset_fetch_attempts(
        _reset_args(document_id=[str(document_id)], status=STATUS_ROBOTS_DISALLOWED)
    ) == 0

    check = get_session()
    try:
        doc = check.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0, "a differently-spelled host must not over-match"
        assert doc.license_note == f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
    finally:
        check.close()


def test_reset_fetch_attempts_single_statement_closes_concurrent_note_race(monkeypatch):
    """R2 fixlist #3: a row OUTSIDE the reset's scope when it starts (spent
    counter == 0 with a non-resettable note) must never end up with a spent
    counter that looks fresh (fetch_attempts == 1) AND a silently-erased
    note, even if a concurrent worker session commits fetch_attempts=1 plus
    a resettable note for that same row in the middle of the reset. Folding
    both writes into one UPDATE closes the gap a two-statement reset left
    open."""
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.sql.dml import Update

    marker = f"https://reset-race-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=0)
        document.license_note = "manual_review"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    original_execute = OrmSession.execute
    injected = {"done": False}

    def racing_execute(self, statement, *args, **kwargs):
        if (
            not injected["done"]
            and isinstance(statement, Update)
            and statement.table.name == "bill_documents"
        ):
            injected["done"] = True
            concurrent = get_session()
            try:
                row = concurrent.get(BillDocument, document_id)
                row.fetch_attempts = 1
                row.license_note = "fulltext_status=worker_error"
                concurrent.commit()
            finally:
                concurrent.close()
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(OrmSession, "execute", racing_execute)

    assert cmd_reset_fetch_attempts(_reset_args(url_like=marker)) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert not (doc.fetch_attempts == 1 and doc.license_note is None), (
            "the row must never end up with a spent counter that looks "
            "fresh AND a silently-erased note"
        )
    finally:
        after.close()


def test_reset_fetch_attempts_dry_run_writes_nothing():
    marker = f"https://reset-dry-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=MAX_FETCH_ATTEMPTS)
        document.license_note = f"fulltext_status={STATUS_PERMANENTLY_FAILED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    assert cmd_reset_fetch_attempts(_reset_args(url_like=marker, dry_run=True)) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.fetch_attempts == MAX_FETCH_ATTEMPTS
        assert doc.license_note == f"fulltext_status={STATUS_PERMANENTLY_FAILED}"
    finally:
        after.close()


def test_reset_fetch_attempts_only_permanently_failed_branch_and_status_conflict():
    """T2-1/T2-7: the documented narrow reset works and cannot silently
    discard an explicit --status filter."""
    marker = f"https://reset-only-permanent-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=MAX_FETCH_ATTEMPTS)
        document.license_note = f"fulltext_status={STATUS_PERMANENTLY_FAILED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    assert cmd_reset_fetch_attempts(_reset_args(url_like=marker, only_permanently_failed=True)) == 0

    check = get_session()
    try:
        document = check.get(BillDocument, document_id)
        assert document.fetch_attempts == 0
        assert document.license_note is None
    finally:
        check.close()

    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            [
                "reset-fetch-attempts",
                "--all",
                "--status",
                STATUS_WORKER_ERROR,
                "--only-permanently-failed",
            ]
        )
    assert excinfo.value.code == 2


def test_reset_fetch_attempts_note_clear_re_evaluates_the_predicate_at_write_time(monkeypatch):
    """R1 fixlist #1: the note-clear step must be a single set-based UPDATE
    that reuses `resettable_note_filter` in its own WHERE clause, not a
    SELECT-then-`id IN (...)` pair -- the two-statement form re-checks
    nothing at write time (a fresh note written between the SELECT and the
    UPDATE gets silently clobbered) and materializes an unbounded id list in
    Python (exceeding the bind-parameter cap at "hundreds of thousands of
    rows" scale, per the command's own docstring)."""
    from sqlalchemy import event

    from billcommons_shared.db import get_engine

    marker = f"https://reset-race-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=2)
        document.license_note = f"fulltext_status={STATUS_WORKER_ERROR}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        assert cmd_reset_fetch_attempts(_reset_args(url_like=marker)) == 0
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    note_clear_updates = [
        s for s in statements if "SET license_note" in s or "license_note=" in s.replace(" ", "")
    ]
    assert note_clear_updates, "expected exactly one note-clearing UPDATE"
    for stmt in note_clear_updates:
        # The predicate must be re-checked IN THIS statement's WHERE clause
        # (license_note appears again outside the SET list) -- not a blind
        # `id IN (...)` scoped by a value snapshotted in an earlier SELECT.
        assert stmt.count("license_note") >= 2, stmt
        assert "bill_documents.id IN" not in stmt.replace("\n", " "), stmt

    # No separate id-snapshotting SELECT feeds this UPDATE: every SELECT this
    # command issues is either the breakdown-by-note aggregate or (with
    # --limit) an explicit id-capping query -- never a bare
    # `SELECT bill_documents.id ... WHERE <resettable_note_filter>` whose
    # result is later replayed into an `id IN (...)` bind list.
    bare_id_selects = [
        s
        for s in statements
        if s.strip().upper().startswith("SELECT")
        and "license_note" in s
        and "count(" not in s.lower()
        and "GROUP BY" not in s.upper()
    ]
    assert bare_id_selects == [], bare_id_selects

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0
        assert doc.license_note is None
    finally:
        after.close()


def test_reset_fetch_attempts_status_predicate_matches_exempt_suffixed_note(monkeypatch):
    """R1 fixlist #2: `_mark_status` appends ` robots=api_token_exempt` to a
    non-terminal status's note for a fetch that touched a configured exempt
    host. `worker_error` is non-terminal AND one of the two
    RESETTABLE_DEFAULT_STATUSES -- a worker_error on an exempt host must
    still be reachable by the default `reset-fetch-attempts` reset."""
    marker = f"https://reset-exempt-suffix-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=2)
        document.license_note = f"fulltext_status={STATUS_WORKER_ERROR} robots=api_token_exempt"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    assert cmd_reset_fetch_attempts(
        _reset_args(url_like=marker, status=STATUS_WORKER_ERROR)
    ) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0
        assert doc.license_note is None
    finally:
        after.close()


def test_reset_fetch_attempts_host_filter_matches_uppercase_document_url(monkeypatch):
    """R1 fixlist #4: the reset host filter must lowercase both sides -- the
    runtime exemption check (`host_auth._hostname`) lowercases the parsed
    hostname, so a stored URL with a different host case must still match or
    it stays terminally robots_disallowed forever even though the worker
    would fetch it fine."""
    host = f"reset-case-{uuid.uuid4().hex}.gov"
    marker = f"https://{host.upper()}/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=1)
        document.license_note = f"fulltext_status={STATUS_ROBOTS_DISALLOWED}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    monkeypatch.setattr(
        "billcommons_ingest.cli.host_auth_mod.robots_exempt_hosts", lambda: frozenset({host})
    )
    assert cmd_reset_fetch_attempts(
        _reset_args(url_like=marker, status=STATUS_ROBOTS_DISALLOWED)
    ) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.license_note is None
    finally:
        after.close()


# ---------------------------------------------------------------------------
# tsvector bound (review finding 2)
# ---------------------------------------------------------------------------


def test_hyphen_dense_document_at_the_guard_threshold_still_writes(db_session):
    """Review finding 2, made executable. The first version of migration 0018
    indexed the whole text up to 1,000,000 BYTES on the premise that lexeme
    storage <= input bytes. It is not: the default parser emits the whole
    hyphenated word AND each part, and Postgres also charges ~4 bytes of
    position data per lexeme. Measured on PG16: 2.69x for hyphen-dense text,
    so a 666,017-byte document produced 1,702,046 bytes and failed the write
    -- losing its extracted_text entirely, which is the ORIGINAL bug.

    This pins the guard: text at the threshold, in the worst shape we could
    construct, must still write."""
    document = _make_bill_document(db_session, url="https://example-legislature.gov/hyphen-dense.txt")
    # ~666KB of distinct hyphenated compounds: the exact shape that broke the
    # 1,000,000-byte guard.
    hyphen_dense = " ".join(f"aa{i}-bb{i}" for i in range(100_000, 137_000))
    assert len(hyphen_dense.encode()) > 600_000
    document.extracted_text = hyphen_dense
    db_session.flush()

    db_session.refresh(document)
    assert document.extracted_text == hyphen_dense, "extracted_text is never truncated"
    indexed = db_session.execute(
        text("select text_tsv is not null and length(text_tsv) > 0 from bill_documents where id = :id"),
        {"id": document.id},
    ).scalar()
    assert indexed is True


def test_multibyte_document_beyond_the_guard_still_writes(db_session):
    """The ELSE branch: 4-bytes-per-character text can blow the byte budget at
    a quarter of the character count, so it takes the 62,500-character window.
    Full text still stored."""
    document = _make_bill_document(db_session, url="https://example-legislature.gov/multibyte.txt")
    # 200k x 5 chars of astral-plane text = 1M chars / 3.6MB
    big = "\U0001d51e\U0001d51f-\U0001d520\U0001d521 " * 200_000
    document.extracted_text = big
    db_session.flush()

    db_session.refresh(document)
    assert document.extracted_text == big
    indexed = db_session.execute(
        text("select text_tsv is not null from bill_documents where id = :id"),
        {"id": document.id},
    ).scalar()
    assert indexed is True


def test_worker_charges_only_document_specific_failures_to_the_document():
    """The wiring, not just the classifier: what cmd_worker's generic handler
    records and whether it spends the budget."""

    class _ConnectionLost(Exception):
        sqlstate = "08006"

    class _TsvectorTooLong(Exception):
        sqlstate = "54000"

    # ours -> recorded, but free
    assert classify_job_failure("doc-1", _ConnectionLost()) == (STATUS_WORKER_ERROR, False)
    assert classify_job_failure("doc-1", RuntimeError("new bug")) == (STATUS_WORKER_ERROR, False)
    # the document's -> charged
    assert classify_job_failure("doc-1", _TsvectorTooLong()) == (STATUS_FETCH_ERROR, True)
    assert classify_job_failure("doc-1", DocumentFetchError("dead host", document_id="doc-1")) == (
        STATUS_FETCH_ERROR,
        True,
    )
    assert classify_job_failure(
        "doc-1", UnfetchableDocument("loop", status=STATUS_TOO_MANY_REDIRECTS)
    ) == (STATUS_TOO_MANY_REDIRECTS, True)
    # not a fetch_text job at all -> nothing to record
    assert classify_job_failure(None, RuntimeError("api_sync blew up")) == (None, False)


# ---------------------------------------------------------------------------
# _fetch_best_candidate: chain-continuation + first-non-empty-wins semantics
# ---------------------------------------------------------------------------


def _json_response(body: dict) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(body).encode())


def _ma_robots_allow_all() -> dict:
    return {"https://malegislature.gov/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n")}


def test_fetch_best_candidate_continues_past_an_http_error_to_the_next_candidate():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/a.pdf": httpx.Response(404),
        "https://malegislature.gov/b.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"real text from b"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, outcome = _fetch_best_candidate(
        fetcher, ["https://malegislature.gov/a.pdf", "https://malegislature.gov/b.pdf"]
    )
    assert url == "https://malegislature.gov/b.pdf"
    assert outcome.extracted_text == "real text from b"


def test_fetch_best_candidate_continues_past_an_unfetchable_document_to_the_next_candidate():
    """Previously an UnfetchableDocument (robots disallow, malformed URL,
    etc.) from one candidate aborted the whole chain immediately. It must
    now continue -- a per-candidate verdict does not necessarily hold for
    every candidate (e.g. only one rewritten candidate has a malformed
    path)."""
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/good.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"good text"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, outcome = _fetch_best_candidate(
        fetcher, ["not-a-url", "https://malegislature.gov/good.pdf"]
    )
    assert url == "https://malegislature.gov/good.pdf"
    assert outcome.extracted_text == "good text"


def test_fetch_best_candidate_skips_empty_200_and_returns_first_non_empty():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/empty.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b""
        ),
        "https://malegislature.gov/real.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"the actual text"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, outcome = _fetch_best_candidate(
        fetcher, ["https://malegislature.gov/empty.pdf", "https://malegislature.gov/real.pdf"]
    )
    assert url == "https://malegislature.gov/real.pdf"
    assert outcome.extracted_text == "the actual text"


def test_fetch_best_candidate_returns_the_empty_result_when_every_candidate_is_empty():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/empty1.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b""
        ),
        "https://malegislature.gov/empty2.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b""
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, outcome = _fetch_best_candidate(
        fetcher, ["https://malegislature.gov/empty1.pdf", "https://malegislature.gov/empty2.pdf"]
    )
    # R4 (T4-2, original-first precedence): when every candidate is empty
    # and none errored, the ORIGINAL (stored) URL's empty result is the one
    # reported -- the license_note/failure message must point ops at the
    # document's own URL, not at a speculative rewrite candidate.
    assert url == "https://malegislature.gov/empty1.pdf"
    assert not outcome.extracted_text


def test_fetch_best_candidate_prefers_an_error_over_a_later_empty_result():
    """T2-2: a stale original URL must not turn into a fake empty success
    merely because a rewrite returned an empty 200."""
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/stale.pdf": httpx.Response(404),
        "https://malegislature.gov/empty.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b""
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(httpx.HTTPStatusError):
        _fetch_best_candidate(
            fetcher, ["https://malegislature.gov/stale.pdf", "https://malegislature.gov/empty.pdf"]
        )


@pytest.mark.parametrize(
    ("case", "initial_exc", "results", "expected"),
    [
        (
            "original terminal without initial retryable",
            None,
            {"original": UnfetchableDocument("original terminal", status=STATUS_MALFORMED_URL)},
            "terminal",
        ),
        (
            "initial retryable suppresses original terminal",
            httpx.ReadTimeout("initial retryable"),
            {"original": UnfetchableDocument("original terminal", status=STATUS_MALFORMED_URL)},
            "initial",
        ),
        (
            "original retryable wins over last retryable",
            httpx.ReadTimeout("initial retryable"),
            {"original": httpx.ReadTimeout("original retryable"), "alternate": httpx.ReadTimeout("last retryable")},
            "original",
        ),
        (
            "original empty wins over alternate terminal",
            None,
            {
                "original": httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
                "alternate": UnfetchableDocument("alternate terminal", status=STATUS_MALFORMED_URL),
            },
            "empty",
        ),
        (
            "all empty returns original empty",
            None,
            {
                "original": httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
                "alternate": httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
            },
            "empty",
        ),
    ],
)
def test_fetch_best_candidate_exhaustion_precedence(case, initial_exc, results, expected):
    """T6-3: enumerate the single precedence order for exhausted candidates."""

    class StubFetcher:
        def fetch(self, url):
            result = results[url]
            if isinstance(result, BaseException):
                raise result
            return result

    if expected == "empty":
        _response, url, outcome = _fetch_best_candidate(
            StubFetcher(), list(results), initial_exc=initial_exc, original_url="original"
        )
        assert url == "original", case
        assert not outcome.extracted_text
    else:
        expected_message = f"{expected} retryable" if expected != "terminal" else "original terminal"
        with pytest.raises(Exception, match=expected_message) as excinfo:
            _fetch_best_candidate(
                StubFetcher(), list(results), initial_exc=initial_exc, original_url="original"
            )
        if expected == "original":
            context = "\n".join(excinfo.value.__notes__)
            assert "original" in context
            assert "alternate" in context


@pytest.mark.parametrize(
    "candidates",
    [
        ["https://malegislature.gov/original.pdf", "not-a-url"],
        ["not-a-url", "https://malegislature.gov/original.pdf"],
    ],
)
def test_fetch_best_candidate_keeps_original_empty_over_terminal_alternate(candidates):
    """T3-2: a speculative terminal rewrite cannot defeat a real original 200."""
    original_url = "https://malegislature.gov/original.pdf"
    routes = {
        **_ma_robots_allow_all(),
        original_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    _response, url, outcome = _fetch_best_candidate(fetcher, candidates, original_url=original_url)

    assert url == original_url
    assert not outcome.extracted_text


def test_fetch_best_candidate_raises_original_terminal_over_alternate_retryable():
    original_url = "not-a-url"
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/retryable.pdf": httpx.Response(500),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument) as excinfo:
        _fetch_best_candidate(
            fetcher, [original_url, "https://malegislature.gov/retryable.pdf"], original_url=original_url
        )
    assert excinfo.value.status == STATUS_MALFORMED_URL


def test_fetch_best_candidate_raises_initial_retryable_over_fallback_terminal():
    original_url = "not-a-url"
    client = httpx.Client(transport=_multi_host_transport(_ma_robots_allow_all()))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(httpx.ReadTimeout, match="MA API timed out"):
        _fetch_best_candidate(
            fetcher,
            [original_url],
            original_url=original_url,
            initial_exc=httpx.ReadTimeout("MA API timed out"),
        )


def test_fetch_best_candidate_raises_original_terminal_after_alternate_empty():
    original_url = "not-a-url"
    alternate_url = "https://malegislature.gov/empty.pdf"
    routes = {
        **_ma_robots_allow_all(),
        alternate_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(UnfetchableDocument) as excinfo:
        _fetch_best_candidate(fetcher, [alternate_url, original_url], original_url=original_url)
    assert excinfo.value.status == STATUS_MALFORMED_URL


def test_fetch_best_candidate_raises_the_most_informative_error_once_all_fail():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/a.pdf": httpx.Response(404),
        "https://malegislature.gov/b.pdf": httpx.Response(500),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(httpx.HTTPError):
        _fetch_best_candidate(fetcher, ["https://malegislature.gov/a.pdf", "https://malegislature.gov/b.pdf"])


def test_fetch_best_candidate_uses_the_last_retryable_error_without_an_original_error():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/a.pdf": httpx.Response(404),
        "https://malegislature.gov/b.pdf": httpx.Response(500),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        _fetch_best_candidate(
            fetcher,
            ["https://malegislature.gov/a.pdf", "https://malegislature.gov/b.pdf"],
            original_url="https://malegislature.gov/original.pdf",
        )
    assert excinfo.value.response.status_code == 500


def test_fetch_best_candidate_extraction_error_names_the_failing_url(monkeypatch):
    """T3-3: an earlier extraction error must not name the later empty URL."""
    failed_url = "https://malegislature.gov/failed.pdf"
    empty_url = "https://malegislature.gov/empty.pdf"

    def _boom(_content_type, raw):
        if raw == b"boom":
            raise TypeError("boom")
        return fulltext_mod.ExtractionOutcome(STATUS_FETCH_ERROR, None, "checksum")

    monkeypatch.setattr(fulltext_mod, "extract_document_text", _boom)
    routes = {
        **_ma_robots_allow_all(),
        failed_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"boom"),
        empty_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"empty"),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(DocumentFetchError, match=failed_url) as excinfo:
        _fetch_best_candidate(fetcher, [failed_url, empty_url])
    assert empty_url not in str(excinfo.value)


def test_fetch_best_candidate_wraps_an_all_candidates_extraction_crash_as_document_fetch_error(monkeypatch):
    """A parser crash on every candidate must still spend the document's
    fetch_attempts budget (item 3: "so fetch_attempts accounting still
    happens") -- never escape as a bare, unattributable exception."""

    def _boom(_content_type, _raw):
        raise TypeError("boom")

    monkeypatch.setattr(fulltext_mod, "extract_document_text", _boom)
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/a.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"whatever"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(DocumentFetchError) as excinfo:
        _fetch_best_candidate(fetcher, ["https://malegislature.gov/a.pdf"])
    assert excinfo.value.status == STATUS_FETCH_ERROR


def test_process_fetch_text_job_passes_document_url_as_original_candidate(db_session, rawstore, monkeypatch):
    """T4-1: resolver ordering must not let a rewrite's terminal verdict win."""
    original_url = "https://malegislature.gov/original.pdf"
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("zz", "HB 1"))
    monkeypatch.setattr(
        fulltext_mod, "resolve_fetch_url", lambda _jurisdiction, _url, _identifier: ["not-a-url", original_url]
    )
    routes = {
        **_ma_robots_allow_all(),
        original_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=original_url)

    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK
    db_session.refresh(document)
    assert document.license_note == f"fulltext_status={STATUS_OK}"


# ---------------------------------------------------------------------------
# MA docket -> authoritative bill-number resolution (_resolve_ma_document)
#
# The scenario these tests are built around is real and live-verified
# (2026-08-21): docket HD177 was never assigned a bill number, but the OLD
# code guessed its bill id was H177 by shape alone -- H177 IS a real,
# unrelated bill (an Act on a local cannabis transaction fee) whose OWN
# docket is HD4189. A guess-based resolver would have 200ed against H177
# and stored ITS text under HD177's document row. The new resolver never
# derives a bill id from a docket id's shape -- it reads the docket's own
# `BillNumber` field from the API and cross-checks the resolved bill's
# `DocketNumber` before ever accepting its text.
# ---------------------------------------------------------------------------


def _ma_document_url(doc_id: str, *, court: str = "194") -> MaDocumentUrl:
    parsed = ma_docket_from_url(f"https://malegislature.gov/Bills/{court}/{doc_id}.pdf")
    assert parsed is not None
    return parsed


def test_ma_docket_resolves_via_authoritative_bill_number_and_cross_checks_it():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/SD123": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": ""}
        ),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/S2045": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": "SECTION 1. Tuition deduction."}
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, resolver_name, outcome = _resolve_ma_document(fetcher, _ma_document_url("SD123"))
    assert url == "https://malegislature.gov/api/GeneralCourts/194/Documents/S2045"
    assert resolver_name == "ma_bill_json"
    assert outcome.extracted_text == "SECTION 1. Tuition deduction."


def test_ma_docket_never_derives_a_bill_id_from_the_dockets_shape():
    """The regression test for the actual bug: HD177 (never assigned a
    bill number) must NEVER cause a request to /Documents/H177 -- the
    derived-by-shape id. Registering NO route for it means the mock
    transport raises if the old guessing behavior ever came back."""
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/HD177": _json_response(
            {"DocketNumber": "HD177", "BillNumber": None, "DocumentText": ""}
        ),
        # Deliberately NO route for /Documents/H177 -- if the resolver ever
        # guesses and fetches it, this test fails with "no canned route".
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(DocumentFetchError) as excinfo:
        _resolve_ma_document(fetcher, _ma_document_url("HD177"))
    assert excinfo.value.status == STATUS_MA_DOCKET_NO_BILL_NUMBER
    assert STATUS_MA_DOCKET_NO_BILL_NUMBER not in TERMINAL_STATUSES, (
        "a docket may be assigned a bill number on a LATER day -- must stay retryable"
    )


def test_ma_docket_not_found_is_a_terminal_not_retried_status(db_session, rawstore, monkeypatch):
    """malegislature.gov's deterministic "docket does not exist" answer
    (400, body "The requested Document could not be found in General
    Court ...", verified live 2026-08-26 against 166 bill_documents rows)
    must become STATUS_MA_DOCKET_NOT_FOUND, terminal, and therefore never
    re-enqueued -- unlike STATUS_MA_DOCKET_NO_BILL_NUMBER, this docket will
    never start existing on a later retry."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "HD 9001"))
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/HD9001": httpx.Response(
            400, text='The requested Document could not be found in General Court "194"'
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url="https://malegislature.gov/Bills/194/HD9001.pdf")

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_MA_DOCKET_NOT_FOUND
    assert STATUS_MA_DOCKET_NOT_FOUND in TERMINAL_STATUSES
    db_session.refresh(document)
    assert document.license_note == f"fulltext_status={STATUS_MA_DOCKET_NOT_FOUND}"
    assert document.extracted_text is None
    assert enqueue_fulltext_jobs(db_session, document_ids=[document.id]) == 0, (
        "terminal status must not be re-enqueued"
    )


def test_ma_docket_lookup_500_is_generic_fetch_error_with_a_detail_token(db_session, rawstore, monkeypatch):
    """A genuine 500 on the same docket-lookup call is a normal, retryable
    fetch failure -- NOT ma_docket_not_found -- but the collapse into an
    undiagnosable bare `fulltext_status=fetch_error` (the 166-row bug this
    whole status was built to fix) is itself fixed by recording a
    `fetch_detail=<ExceptionClassName>[:<http status>]` token."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "HD 9002"))
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/HD9002": httpx.Response(500),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url="https://malegislature.gov/Bills/194/HD9002.pdf")

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_FETCH_ERROR
    assert STATUS_FETCH_ERROR not in TERMINAL_STATUSES
    db_session.refresh(document)
    assert document.license_note == f"fulltext_status={STATUS_FETCH_ERROR} fetch_detail=HTTPStatusError:500"


def test_reset_fetch_attempts_accepts_ma_docket_not_found():
    """R3-1-style operator requeue lever: an operator who is convinced a
    docket the API said didn't exist actually does now (e.g. malegislature.gov
    fixed something upstream) can explicitly requeue it -- the same recovery
    pattern already available for permanently_failed/worker_error."""
    marker = f"https://reset-ma-not-found-{uuid.uuid4().hex}.gov/bill.pdf"
    setup = get_session()
    try:
        document = _make_bill_document(setup, url=marker, fetch_attempts=MAX_FETCH_ATTEMPTS)
        document.license_note = f"fulltext_status={STATUS_MA_DOCKET_NOT_FOUND}"
        document_id = document.id
        setup.commit()
    finally:
        setup.close()

    check = get_session()
    try:
        assert enqueue_fulltext_jobs(check, document_ids=[document_id]) == 0, "precondition: excluded"
        check.rollback()
    finally:
        check.close()

    assert cmd_reset_fetch_attempts(_reset_args(url_like=marker, status=[STATUS_MA_DOCKET_NOT_FOUND])) == 0

    after = get_session()
    try:
        doc = after.get(BillDocument, document_id)
        assert doc.fetch_attempts == 0
        assert doc.license_note is None
        assert enqueue_fulltext_jobs(after, document_ids=[document_id]) == 1
        after.rollback()
    finally:
        after.close()


def test_ma_docket_with_no_bill_number_but_its_own_text_uses_the_docket_json():
    # A procedural filing/report (no bill number) can still carry its own
    # DocumentText -- SD3668, a real commission report, verified live.
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/SD3668": _json_response(
            {"DocketNumber": "SD3668", "BillNumber": None, "DocumentText": "A commission report."}
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, resolver_name, outcome = _resolve_ma_document(fetcher, _ma_document_url("SD3668"))
    assert resolver_name == "ma_docket_json"
    assert outcome.extracted_text == "A commission report."


def test_ma_cross_check_mismatch_never_stores_text_under_the_wrong_bill():
    """Belt-and-braces: even if a docket's API record names a BillNumber,
    the bill's OWN record must independently reference the SAME docket
    before its text is accepted -- an API inconsistency (or a bug) must
    fail loudly rather than silently store a mismatched bill's text."""
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/HD9999": _json_response(
            {"DocketNumber": "HD9999", "BillNumber": "H177", "DocumentText": ""}
        ),
        # H177's own record: a REAL bill, but its real docket is HD4189, not
        # HD9999 -- the cross-check must catch this mismatch.
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H177": _json_response(
            {"DocketNumber": "HD4189", "BillNumber": "H177", "DocumentText": "Unrelated cannabis fee bill text."}
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(DocumentFetchError, match="does not match expected"):
        _resolve_ma_document(fetcher, _ma_document_url("HD9999"))


def test_ma_malformed_docket_json_is_non_terminal():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/HD177": httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{not valid json"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    with pytest.raises(DocumentFetchError) as excinfo:
        _resolve_ma_document(fetcher, _ma_document_url("HD177"))
    assert excinfo.value.status == STATUS_FETCH_ERROR
    assert excinfo.value.status not in TERMINAL_STATUSES


def test_ma_bill_json_empty_falls_back_to_the_bills_pdf_page():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/SD123": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": ""}
        ),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/S2045": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": ""}
        ),
        "https://malegislature.gov/Bills/194/S2045.pdf": httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"bill text only on the page"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, resolver_name, outcome = _resolve_ma_document(fetcher, _ma_document_url("SD123"))
    assert url == "https://malegislature.gov/Bills/194/S2045.pdf"
    assert resolver_name == "ma_bill_pdf"
    assert outcome.extracted_text == "bill text only on the page"


def test_ma_already_bill_style_url_skips_the_docket_step_entirely():
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H177": _json_response(
            {"DocketNumber": "HD4189", "BillNumber": "H177", "DocumentText": "Cannabis fee bill text."}
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    response, url, resolver_name, outcome = _resolve_ma_document(fetcher, _ma_document_url("H177"))
    assert resolver_name == "ma_bill_json"
    assert outcome.extracted_text == "Cannabis fee bill text."


# ---------------------------------------------------------------------------
# process_fetch_text_job end-to-end via the MA path (jurisdiction_code
# faked via monkeypatch -- a real "MA" Jurisdiction row would collide with
# the live jurisdiction this same DB already carries; see conftest.py's
# ZZ_/ZQ_ test-abbreviation convention)
# ---------------------------------------------------------------------------


def test_process_fetch_text_job_resolves_ma_docket_end_to_end(db_session, rawstore, monkeypatch):
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "S 5"))
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/SD123": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": ""}
        ),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/S2045": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": "SECTION 1. Real bill text."}
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    document = _make_bill_document(db_session, url="https://malegislature.gov/Bills/194/SD123.pdf")
    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK
    db_session.refresh(document)
    assert document.extracted_text == "SECTION 1. Real bill text."
    assert document.license_note == f"fulltext_status={STATUS_OK} url_resolver=ma_bill_json"
    assert document.fetch_attempts == 0


def test_process_fetch_text_job_ma_docket_no_bill_number_is_retryable_not_terminal(db_session, rawstore, monkeypatch):
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "HD 177"))
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/HD177": _json_response(
            {"DocketNumber": "HD177", "BillNumber": None, "DocumentText": ""}
        ),
        # No route for /Documents/H177 -- proves the fix end-to-end through
        # the real process_fetch_text_job entry point, not just the
        # resolver in isolation.
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))

    document = _make_bill_document(db_session, url="https://malegislature.gov/Bills/194/HD177.pdf")
    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_MA_DOCKET_NO_BILL_NUMBER
    db_session.refresh(document)
    assert document.license_note == f"fulltext_status={STATUS_MA_DOCKET_NO_BILL_NUMBER}"
    assert document.extracted_text is None
    assert STATUS_MA_DOCKET_NO_BILL_NUMBER not in TERMINAL_STATUSES


def test_ma_bill_url_falls_back_to_its_stored_pdf_when_the_api_5xxs(db_session, rawstore, monkeypatch):
    """T3-1: a retryable API failure preserves the direct-URL fallback."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "H 177"))
    stored_url = "https://malegislature.gov/Bills/194/H177.pdf"
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H177": httpx.Response(500),
        stored_url: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"official bill text from stored PDF"
        ),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=stored_url)

    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK
    db_session.refresh(document)
    assert document.extracted_text == "official bill text from stored PDF"
    assert document.fetch_attempts == 0


def test_ma_bill_url_falls_back_to_its_stored_pdf_when_the_api_times_out(db_session, rawstore, monkeypatch):
    """T4-4: API-stage transport errors are retryable lookup errors."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "H 177"))
    stored_url = "https://malegislature.gov/Bills/194/H177.pdf"
    api_url = "https://malegislature.gov/api/GeneralCourts/194/Documents/H177"
    routes = {
        **_ma_robots_allow_all(),
        stored_url: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"official bill text from stored PDF"
        ),
    }

    def handler(request):
        if str(request.url) == api_url:
            raise httpx.ReadTimeout("MA API timed out", request=request)
        route = routes[str(request.url)]
        return httpx.Response(route.status_code, headers=route.headers, content=route.content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=stored_url)

    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK
    db_session.refresh(document)
    assert document.extracted_text == "official bill text from stored PDF"


def test_ma_bill_resolved_page_timeout_does_not_run_direct_url_fallback(db_session, rawstore, monkeypatch):
    """T4-4: a page-stage transport error stays a charged document failure."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "H 177"))
    stored_url = "https://malegislature.gov/Bills/194/H177.pdf"
    api_url = "https://malegislature.gov/api/GeneralCourts/194/Documents/H177"
    requests: list[str] = []
    routes = {
        **_ma_robots_allow_all(),
        api_url: _json_response({"BillNumber": "H177", "DocumentText": ""}),
    }

    def handler(request):
        url = str(request.url)
        requests.append(url)
        if url == stored_url:
            raise httpx.ReadTimeout("MA bill page timed out", request=request)
        route = routes[url]
        return httpx.Response(route.status_code, headers=route.headers, content=route.content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=stored_url)

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_FETCH_ERROR
    assert requests.count(stored_url) == 1


def test_ma_bill_api_5xx_and_empty_stored_url_preserves_existing_text(db_session, rawstore, monkeypatch):
    """T3-1: an empty fallback must re-raise the charged API error."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "H 177"))
    stored_url = "https://malegislature.gov/Bills/194/H177.pdf"
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H177": httpx.Response(500),
        stored_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=stored_url)
    document.extracted_text = "previously extracted official text"

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_FETCH_ERROR
    db_session.refresh(document)
    # fetch_detail names the WRAPPING exception (MaApiLookupError, no HTTP
    # status of its own -- the underlying 500 is inside its message, not
    # exposed as a `.response`), not the underlying httpx.HTTPStatusError.
    assert document.license_note == f"fulltext_status={STATUS_FETCH_ERROR} fetch_detail=MaApiLookupError"
    assert document.extracted_text == "previously extracted official text"


def test_ma_bill_api_5xx_and_malformed_stored_url_stays_retryable(db_session, rawstore, monkeypatch):
    """A terminal stored-URL verdict cannot dead-letter an API 5xx."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "H 177"))
    monkeypatch.setattr(
        fulltext_mod,
        "ma_docket_from_url",
        lambda _url: MaDocumentUrl("194", "H177", "https://malegislature.gov/Bills/", ".pdf"),
    )
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H177": httpx.Response(500),
    }
    client = httpx.Client(transport=_multi_host_transport(routes))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url="not-a-url")

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_FETCH_ERROR
    db_session.refresh(document)
    assert document.license_note == f"fulltext_status={STATUS_FETCH_ERROR} fetch_detail=MaApiLookupError"


def test_ma_resolved_page_robots_disallowed_does_not_run_direct_url_fallback(
    db_session, rawstore, monkeypatch
):
    """T6-2: terminal page-stage verdicts must not enter stored-URL fallback."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "SD 123"))
    stored_url = "https://malegislature.gov/Bills/194/SD123.pdf"
    resolved_page_url = "https://malegislature.gov/Bills/194/H177.pdf"
    requests: list[str] = []
    routes = {
        "https://malegislature.gov/robots.txt": httpx.Response(
            200, text="User-agent: *\nDisallow: /Bills/194/H177.pdf\n"
        ),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/SD123": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "H177", "DocumentText": ""}
        ),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H177": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "H177", "DocumentText": ""}
        ),
    }

    def handler(request):
        url = str(request.url)
        requests.append(url)
        route = routes[url]
        return httpx.Response(route.status_code, headers=route.headers, content=route.content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=stored_url)

    with pytest.raises(UnfetchableDocument) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_ROBOTS_DISALLOWED
    assert stored_url not in requests
    assert resolved_page_url not in requests, "robots must block before the page request"


def test_ma_resolved_page_timeout_falls_back_when_page_url_differs(db_session, rawstore, monkeypatch):
    """A retryable resolved-page error still tries a distinct stored URL."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "SD 123"))
    stored_url = "https://malegislature.gov/Bills/194/SD123.pdf"
    resolved_page_url = "https://malegislature.gov/Bills/194/S2045.pdf"
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/SD123": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": ""}
        ),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/S2045": _json_response(
            {"DocketNumber": "SD123", "BillNumber": "S2045", "DocumentText": ""}
        ),
        stored_url: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"official text from the stored URL"
        ),
    }

    def handler(request):
        if str(request.url) == resolved_page_url:
            raise httpx.ReadTimeout("MA resolved bill page timed out", request=request)
        route = routes[str(request.url)]
        return httpx.Response(route.status_code, headers=route.headers, content=route.content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=stored_url)

    result = process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert result.status == STATUS_OK
    db_session.refresh(document)
    assert document.extracted_text == "official text from the stored URL"


def test_ma_bill_resolved_page_empty_is_not_fetched_a_second_time(db_session, rawstore, monkeypatch):
    """T3-1: page no-text is final for this attempt, not an API fallback."""
    monkeypatch.setattr(fulltext_mod, "_jurisdiction_and_identifier", lambda db, document: ("ma", "H 177"))
    stored_url = "https://malegislature.gov/Bills/194/H177.pdf"
    requests: list[str] = []
    routes = {
        **_ma_robots_allow_all(),
        "https://malegislature.gov/api/GeneralCourts/194/Documents/H177": _json_response(
            {"BillNumber": "H177", "DocumentText": ""}
        ),
        stored_url: httpx.Response(200, headers={"content-type": "text/plain"}, content=b""),
    }

    def handler(request):
        url = str(request.url)
        requests.append(url)
        route = routes[url]
        return httpx.Response(route.status_code, headers=route.headers, content=route.content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = FullTextFetcher(client=client, robots_cache=RobotsCache(client=client))
    document = _make_bill_document(db_session, url=stored_url)

    with pytest.raises(DocumentFetchError) as excinfo:
        process_fetch_text_job(db_session, str(document.id), fetcher=fetcher, rawstore=rawstore)

    assert excinfo.value.status == STATUS_FETCH_ERROR
    assert requests.count(stored_url) == 1
