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
import uuid

import httpx
import pytest
from pypdf import PdfWriter

from billcommons_ingest.fulltext import (
    STATUS_OK,
    STATUS_ROBOTS_DISALLOWED,
    STATUS_SCANNED_PDF_NO_TEXT,
    FETCH_TEXT_KIND,
    FullTextFetcher,
    RobotsCache,
    UnfetchableDocument,
    enqueue_fulltext_jobs,
    extract_document_text,
    extract_text_from_html,
    extract_text_from_pdf,
    extract_text_from_plain,
    extract_text_from_xml,
    process_fetch_text_job,
    sniff_content_type,
)
from billcommons_schema.models import Bill, BillDocument, BillVersion, IngestJob, Jurisdiction, Session as SessionModel


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_bill_document(db_session, *, url="https://example-legislature.gov/bill.pdf", abbr=None):
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
    document = BillDocument(bill_version_id=version.id, url=url, media_type=None)
    db_session.add(document)
    db_session.flush()
    return document


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


def test_process_fetch_text_job_html_end_to_end(db_session, rawstore):
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
