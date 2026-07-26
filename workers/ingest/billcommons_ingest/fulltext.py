"""Full-bill-text pipeline: fetch official documents, extract text, store.

Per docs/SPEC.md ("Version diffing", "Refresh", GREEN criteria #5) and
docs/architecture/ARCHITECTURE.md: every `bill_documents` row carries an
official source URL (from the Open States bulk dump or, later, T1/T4
adapters). This module:

  1. `enqueue_fulltext_jobs` -- scans `bill_documents` lacking
     `extracted_text` with a non-null `url`, enqueues one `fetch_text`
     ingest_jobs row per document (idempotent: skips documents that already
     have a queued/running fetch_text job).
  2. `process_fetch_text_job` -- fetches the document (politely: per-host
     token bucket, robots.txt honored, honest UA, timeouts), archives the
     raw bytes to RawStore, sniffs content-type, extracts text, and writes
     `bill_documents.extracted_text` (+ provenance columns). The generated
     `text_tsv` column indexes the new text automatically -- no extra step.

Politeness is non-negotiable (SPEC "Refresh targets" / "Security" /
ARCHITECTURE ingestion-tiers T4): every fetch goes through the shared
`billcommons_shared.httpc` rate limiter, honors robots.txt for any host
that isn't a mirror we already have blanket ToS-covered access to (Open
States' own asset mirror), and treats every byte of fetched content as
UNTRUSTED DATA -- it is parsed for extraction only, never executed,
evaluated, or treated as instructions.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import Text, case, cast, exists, func, or_, select, text
from sqlalchemy.orm import Session as OrmSession

from billcommons_schema.models import Bill, BillDocument, BillVersion, IngestJob
from billcommons_shared.aia import AiaRepairCache, is_missing_issuer_error
from billcommons_shared.httpc import USER_AGENT, RateLimiter, new_client
from billcommons_shared.rawstore import RawStore

SOURCE_NAME = "fulltext_fetch"
FETCH_TEXT_KIND = "fetch_text"


def _env_flag(name: str, *, default: bool = False) -> bool:
    """Parse a boolean env var; malformed/absent values fall back to default."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


PARSER_VERSION = "fulltext/1"

# Politeness: 1 request per 2s per host by default (SPEC "Refresh" /
# "Security"), well under any published state-legislature rate limit.
DEFAULT_RATE_PER_SEC = 0.5
DEFAULT_TIMEOUT = 30.0

# Below this many extracted characters from a PDF that does have pages, we
# treat it as a scanned/no-text PDF rather than presenting near-empty text
# as if it were the real bill text (SPEC "Version diffing": "Scanned PDFs:
# detect extraction failure ... never presented as authoritative without
# warning"). OCR is explicitly out of scope for this round.
SCANNED_PDF_MIN_CHARS = 100

# Fetch-status values recorded in BillDocument.license_note as a compact
# machine-readable tag (schema has no dedicated fetch-status column; see
# `_set_status` below for the encoding convention).
STATUS_OK = "ok"
STATUS_ROBOTS_DISALLOWED = "robots_disallowed"
STATUS_SCANNED_PDF_NO_TEXT = "scanned_pdf_no_text"
STATUS_FETCH_ERROR = "fetch_error"
STATUS_UNSUPPORTED_TYPE = "unsupported_type"
STATUS_EMPTY_URL = "empty_url"
STATUS_UNSUPPORTED_REDIRECT_SCHEME = "unsupported_redirect_scheme"
STATUS_TOO_MANY_REDIRECTS = "too_many_redirects"
STATUS_MALFORMED_URL = "malformed_url"

# Statuses that will NEVER change on a retry -- the document's URL/robots.txt/
# content shape is a fixed fact about that source, not a transient condition.
# enqueue_fulltext_jobs skips documents already marked with one of these so a
# permanently-unfetchable document isn't re-enqueued forever (see the
# license_note-based skip in enqueue_fulltext_jobs below). STATUS_FETCH_ERROR
# and STATUS_TOO_MANY_REDIRECTS are deliberately excluded -- a network/HTTP
# error and a redirect loop are both transient conditions worth retrying (the
# target site's redirect chain today doesn't guarantee its redirect chain
# tomorrow).
TERMINAL_STATUSES = frozenset(
    {
        STATUS_ROBOTS_DISALLOWED,
        STATUS_EMPTY_URL,
        STATUS_SCANNED_PDF_NO_TEXT,
        STATUS_UNSUPPORTED_TYPE,
        STATUS_UNSUPPORTED_REDIRECT_SCHEME,
        STATUS_MALFORMED_URL,
    }
)


class UnfetchableDocument(RuntimeError):
    """Raised for conditions that should NOT be retried by the job queue's
    backoff (robots.txt disallow, empty/invalid URL) -- these are permanent
    per-document outcomes, not transient failures.

    Carries `document_id`/`status` so a caller that must roll back the
    session this was raised in (see cli.py's worker loop, which rolls back
    to clear any partial job-processing state before dead-lettering) can
    durably re-apply the SAME terminal status in a fresh transaction --
    otherwise the rollback would silently undo the status write that
    `process_fetch_text_job` already flushed, leaving the document looking
    "never attempted" and causing `enqueue_fulltext_jobs` to re-enqueue it
    forever even though it is dead-lettered."""

    def __init__(self, message: str, *, document_id: str | None = None, status: str | None = None) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.status = status


# ---------------------------------------------------------------------------
# robots.txt cache
# ---------------------------------------------------------------------------


class RobotsCache:
    """Per-host robots.txt cache. Fetches + parses once per host, then
    answers `can_fetch` from memory. A host whose robots.txt itself cannot
    be fetched (404, timeout, etc.) is treated as allow-all, matching
    standard robots.txt semantics (absence of a robots.txt is not a
    disallow)."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        client_for: Callable[[str], httpx.Client] | None = None,
    ) -> None:
        self._client = client or new_client(timeout=10.0)
        # `client_for`, if given, resolves the client to use per origin. The
        # crawl passes one so that a host whose TLS intermediate had to be
        # recovered (see billcommons_shared.aia) has its robots.txt read over
        # the WORKING connection. Without it, such a host's robots.txt fetch
        # fails verification, falls through to the allow-all default below, and
        # we would crawl it having never actually read its rules -- CT
        # (www.cga.ct.gov) publishes a real robots.txt with Disallow paths, so
        # this failed open on a host that genuinely restricts parts of itself.
        self._client_for = client_for
        self._parsers: dict[str, RobotFileParser] = {}

    def invalidate(self, origin: str) -> None:
        """Drop the cached verdict for `origin` so it is re-read on next use.

        Called after a TLS repair makes a previously unreachable robots.txt
        readable, so the allow-all fallback cached during the broken period
        cannot outlive the breakage it was standing in for.
        """
        self._parsers.pop(origin, None)

    def _get_parser(self, origin: str) -> RobotFileParser:
        if origin in self._parsers:
            return self._parsers[origin]
        parser = RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        client = self._client_for(origin) if self._client_for else self._client
        try:
            response = client.get(f"{origin}/robots.txt")
            if response.status_code >= 400:
                parser.parse([])  # no robots.txt -> allow-all
            else:
                parser.parse(response.text.splitlines())
        except httpx.HTTPError:
            parser.parse([])  # unreachable -> allow-all (can't be blocked by a file we can't read)
        self._parsers[origin] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._get_parser(origin)
        return parser.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# HTML/XML text extraction (dependency-light: stdlib html.parser)
# ---------------------------------------------------------------------------

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "li", "ul", "ol", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "blockquote", "pre",
}
_SKIP_TAGS = {"script", "style", "head", "title"}


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML->text extractor preserving block-level line breaks (best
    -effort section/line structure per SPEC "Version diffing", without
    aggressive reflow). Deliberately does not execute/interpret any script
    or style content -- those tags' text is dropped entirely."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def extract_text_from_html(raw: bytes, encoding: str = "utf-8") -> str:
    parser = _HTMLTextExtractor()
    parser.feed(raw.decode(encoding, errors="replace"))
    parser.close()
    return _normalize_text(parser.get_text())


_XML_TAG_RE = re.compile(rb"<[^>]+>")


def extract_text_from_xml(raw: bytes, encoding: str = "utf-8") -> str:
    """Strip XML tags while preserving line structure: each element close
    becomes a line break so section/paragraph boundaries in bill XML
    (e.g. <section>, <p>) survive as line breaks for diffing."""
    with_breaks = _XML_TAG_RE.sub(lambda m: b"\n" if m.group(0).startswith(b"</") else b"", raw)
    decoded = with_breaks.decode(encoding, errors="replace")
    return _normalize_text(decoded)


def extract_text_from_plain(raw: bytes, encoding: str = "utf-8") -> str:
    return _normalize_text(raw.decode(encoding, errors="replace"))


@dataclass
class PdfExtractionResult:
    text: str
    page_count: int
    scanned_no_text: bool


def extract_text_from_pdf(raw: bytes) -> PdfExtractionResult:
    """Extract text per-page via pypdf. If the PDF has pages but yields
    fewer than SCANNED_PDF_MIN_CHARS total characters, flag it as a likely
    scanned/no-text PDF rather than fabricating near-empty "extracted"
    text (SPEC: never present garbage as authoritative text; OCR deferred)."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    page_count = len(reader.pages)
    page_texts = []
    for page in reader.pages:
        page_texts.append(page.extract_text() or "")
    joined = _normalize_text("\n".join(page_texts))
    scanned_no_text = page_count > 0 and len(joined.strip()) < SCANNED_PDF_MIN_CHARS
    return PdfExtractionResult(text=joined, page_count=page_count, scanned_no_text=scanned_no_text)


def _normalize_text(raw: str) -> str:
    """Normalize line endings + collapse excessive blank lines while
    preserving section/line structure as best-effort (no aggressive
    reflow), per SPEC "Version diffing" -- diffs must stay meaningful.

    Also strips NUL (0x00). Postgres text columns reject NUL outright, so a
    document whose extracted text contains one failed its
    `UPDATE bill_documents SET extracted_text = ...` with
    `psycopg.DataError`, retried, and eventually dead-lettered -- turning a
    perfectly fetchable document into a permanently dead one and, because its
    bill then never counts toward full-text coverage, capping the
    jurisdiction below the GREEN bar forever. NULs appear in real bill text
    from mis-encoded state HTML and from pypdf output on some PDFs; they
    carry no meaning, so dropping them is lossless for search and diffing.

    Every extractor (HTML/XML/plain/PDF) funnels through here, so this is the
    single chokepoint where the guarantee "text we hand Postgres is
    storable" belongs.
    """
    normalized = raw.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of 3+ blank lines to a single blank line; strip
    # trailing whitespace per line. Never merges/reflows separate lines.
    lines = [line.rstrip() for line in normalized.split("\n")]
    out_lines: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 1:
                out_lines.append(line)
        else:
            blank_run = 0
            out_lines.append(line)
    return "\n".join(out_lines).strip("\n")


def sniff_content_type(content_type_header: str | None, url: str, raw: bytes) -> str:
    """Return one of "html", "xml", "pdf", "text" based on the response
    Content-Type header (preferred), falling back to URL extension, falling
    back to magic-byte sniffing of the body."""
    header = (content_type_header or "").lower()
    if "pdf" in header:
        return "pdf"
    if "html" in header:
        return "html"
    if "xml" in header:
        return "xml"
    if header.startswith("text/"):
        return "text"

    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith((".html", ".htm")):
        return "html"
    if path.endswith(".xml"):
        return "xml"
    if path.endswith(".txt"):
        return "text"

    if raw[:5] == b"%PDF-":
        return "pdf"
    stripped = raw.lstrip()[:200].lower()
    if stripped.startswith(b"<?xml"):
        return "xml"
    if stripped.startswith(b"<!doctype html") or b"<html" in stripped:
        return "html"
    return "text"


@dataclass
class ExtractionOutcome:
    status: str
    extracted_text: str | None
    checksum: str
    raw_ref: str | None = None
    error: str | None = None


def extract_document_text(content_type: str, raw: bytes) -> ExtractionOutcome:
    """Dispatch to the right extractor by sniffed content type. Never
    raises for a recognized-but-unextractable body -- returns a status
    outcome instead (per SPEC: never fabricate/misrepresent text)."""
    checksum = hashlib.sha256(raw).hexdigest()
    if content_type == "pdf":
        result = extract_text_from_pdf(raw)
        if result.scanned_no_text:
            return ExtractionOutcome(
                status=STATUS_SCANNED_PDF_NO_TEXT, extracted_text=None, checksum=checksum
            )
        return ExtractionOutcome(status=STATUS_OK, extracted_text=result.text, checksum=checksum)
    if content_type == "html":
        return ExtractionOutcome(
            status=STATUS_OK, extracted_text=extract_text_from_html(raw), checksum=checksum
        )
    if content_type == "xml":
        return ExtractionOutcome(
            status=STATUS_OK, extracted_text=extract_text_from_xml(raw), checksum=checksum
        )
    if content_type == "text":
        return ExtractionOutcome(
            status=STATUS_OK, extracted_text=extract_text_from_plain(raw), checksum=checksum
        )
    return ExtractionOutcome(status=STATUS_UNSUPPORTED_TYPE, extracted_text=None, checksum=checksum)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def enqueue_fulltext_jobs(
    db: OrmSession, *, limit: int | None = None, document_ids: list | None = None
) -> int:
    """Enqueue one `fetch_text` ingest_jobs row per bill_documents row that
    has a non-null `url` and no `extracted_text` yet, skipping documents
    that already have a queued/running fetch_text job (idempotent -- safe
    to re-run any number of times without ever double-enqueuing). Caller
    commits. Returns the number of jobs enqueued.

    `document_ids`, if given, restricts the scan to those specific
    bill_documents rows instead of the whole table -- used by tests to
    avoid scanning/enqueuing the entire live bill_documents table (see
    tests/test_fulltext.py); production callers (cli.py/autoboot.py) never
    pass it, so behavior there is unchanged.
    """
    already_queued = (
        select(IngestJob.id)
        .where(
            IngestJob.kind == FETCH_TEXT_KIND,
            IngestJob.status.in_(("queued", "running")),
            IngestJob.payload["document_id"].astext == cast(BillDocument.id, Text),
        )
    )

    terminal_license_notes = [f"fulltext_status={status}" for status in TERMINAL_STATUSES]

    # A bill counts as covered once ANY of its documents has text, so a bill
    # with no text at all is worth far more than the 2nd/3rd version of one
    # already covered. Bills average ~3.6 documents, so draining them in
    # created_at order spends ~3.6 fetches per bill of coverage gained.
    #
    # Computed ONCE as a set and LEFT JOINed, not as a correlated EXISTS per
    # candidate row: the correlated form took >2min against ~676k pending
    # documents, and this runs inside the worker's top-up loop, where a
    # multi-minute query would hold a session open and stall the crawl (the
    # exact `idle in transaction` failure this pipeline has hit twice). The
    # join form measures ~2.6s.
    covered_bills = (
        select(BillVersion.bill_id.label("bill_id"))
        .join(BillDocument, BillDocument.bill_version_id == BillVersion.id)
        .where(BillDocument.extracted_text.is_not(None))
        .distinct()
        .subquery()
    )

    pending = (
        select(
            BillDocument.id.label("doc_id"),
            Bill.jurisdiction_id.label("jurisdiction_id"),
            BillDocument.created_at.label("created_at"),
            case((covered_bills.c.bill_id.is_(None), 0), else_=1).label("bill_covered"),
            func.row_number()
            .over(partition_by=BillVersion.bill_id, order_by=BillDocument.created_at)
            .label("rn_in_bill"),
        )
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .join(Bill, Bill.id == BillVersion.bill_id)
        .outerjoin(covered_bills, covered_bills.c.bill_id == BillVersion.bill_id)
        .where(
            BillDocument.url.is_not(None),
            BillDocument.url != "",
            BillDocument.extracted_text.is_(None),
            # NOT IN would evaluate to NULL (excluding the row) for a
            # never-attempted document whose license_note IS NULL, so guard
            # with an explicit OR-is-NULL instead of relying on SQL's
            # NULL-propagating NOT IN semantics.
            or_(
                BillDocument.license_note.is_(None),
                BillDocument.license_note.not_in(terminal_license_notes),
            ),
            ~exists(already_queued),
        )
    )
    if document_ids is not None:
        pending = pending.where(BillDocument.id.in_(document_ids))
    pending = pending.subquery()

    # Round-robin across jurisdictions: rank each pending document within its
    # jurisdiction, then order by that rank. A limited batch therefore pulls
    # the 1st pending doc of every jurisdiction before the 2nd of any -- so
    # full-text (and GREEN progress) spreads across all 51 states in parallel
    # instead of draining one state entirely first (which the old global
    # created_at ordering did).
    #
    # Within a jurisdiction the rank takes ONE document per not-yet-covered
    # bill first (bill_covered, then rn_in_bill), so a pass converts the most
    # bills per fetch. Nothing is skipped -- remaining versions simply sort
    # after, and later passes pick them up.
    rn = (
        func.row_number()
        .over(
            partition_by=pending.c.jurisdiction_id,
            order_by=(pending.c.bill_covered, pending.c.rn_in_bill, pending.c.created_at),
        )
        .label("rn")
    )
    stmt = select(pending.c.doc_id, rn).order_by(rn, pending.c.jurisdiction_id)
    if limit is not None:
        stmt = stmt.limit(limit)

    # Run this one query without parallel workers.
    #
    # The window function above sorts every pending document, and once the
    # corpus passed ~600k pending rows Postgres started choosing a parallel
    # plan for it. Each parallel worker allocates a dynamic shared memory
    # segment under /dev/shm, which on the managed Postgres container is
    # small (64 MB by default); the allocation then fails with
    #
    #   psycopg.errors.DiskFull: could not resize shared memory segment
    #   ... No space left on device
    #
    # That is NOT a full disk -- the volume had plenty of room. It killed
    # every top-up on 2026-07-26, which drained the fetch_text queue to zero
    # and stopped the crawl for two hours while the service still reported
    # Online. Serial execution needs no DSM segment at all and costs ~6s for
    # a 5,000-row batch, which is irrelevant next to the fetches it feeds.
    db.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))

    # ...and give it more than the 300s default statement_timeout that
    # billcommons_shared.db sets on every session. Serial execution of this
    # sort costs ~400s at 730k documents and grows with the corpus, so the
    # default would simply trade DiskFull for QueryCanceled -- same outcome,
    # an empty queue and a dead crawl. Safe to extend here specifically: this
    # runs in the worker's own short-lived top-up session, it is a read-only
    # SELECT that takes no row locks, and the session is ACTIVE throughout
    # rather than idle-in-transaction (the failure mode the 300s cap exists
    # to bound).
    db.execute(text("SET LOCAL statement_timeout = '900s'"))

    document_ids = db.execute(stmt).scalars().all()
    for document_id in document_ids:
        job = IngestJob(
            kind=FETCH_TEXT_KIND,
            payload={"document_id": str(document_id)},
            status="queued",
            run_after=datetime.now(timezone.utc),
        )
        db.add(job)
    db.flush()
    return len(document_ids)


# ---------------------------------------------------------------------------
# Fetch + process a single fetch_text job
# ---------------------------------------------------------------------------


@dataclass
class FetchTextResult:
    document_id: str
    status: str
    extracted_chars: int = 0
    raw_ref: str | None = None


MAX_REDIRECT_HOPS = 5


class FullTextFetcher:
    """Stateful fetcher holding the shared rate limiter + robots cache
    across calls to `process_fetch_text_job` (so a long-lived worker
    process reuses per-host state instead of re-fetching robots.txt or
    resetting the rate-limit bucket on every job)."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        robots_cache: RobotsCache | None = None,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        aia_cache: AiaRepairCache | None = None,
    ) -> None:
        self.client = client or new_client(timeout=DEFAULT_TIMEOUT)
        self.rate_limiter = rate_limiter or RateLimiter(rate_per_sec=rate_per_sec, burst=1)
        self.aia_cache = aia_cache or AiaRepairCache()
        # Hosts whose TLS intermediate we had to recover get a dedicated client
        # holding the repaired SSL context; every other host keeps using the
        # shared one.
        self._repaired_clients: dict[str, httpx.Client] = {}
        self.robots_cache = robots_cache or RobotsCache(
            client=self.client, client_for=self._client_for_origin
        )

    def _client_for_origin(self, origin: str) -> httpx.Client:
        return self._repaired_clients.get(urlparse(origin).netloc) or self.client

    def _get(self, url: str, parsed) -> httpx.Response:
        """GET `url`, transparently repairing a server that omitted its TLS
        intermediate certificate.

        Several state legislature hosts send only their leaf certificate.
        Browsers paper over that by chasing the leaf's AIA extension; Python's
        SSL stack does not, so every document on such a host fails verification
        permanently -- this silently cost MI, MS and CT their ENTIRE full-text
        corpus (0 of 3,884 / 0 of 4,006 / 7 of 1,283 bills) until it was
        diagnosed. The repair never downgrades verification: the recovered
        intermediate has to independently chain to a shipped root before it is
        used at all (see billcommons_shared.aia), and an unrepairable host
        keeps failing with its original error.
        """
        netloc = parsed.netloc
        client = self._repaired_clients.get(netloc) or self.client
        try:
            return client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            already_repaired = netloc in self._repaired_clients
            if already_repaired or parsed.scheme != "https" or not is_missing_issuer_error(exc):
                raise
            context = self.aia_cache.get(parsed.hostname, parsed.port or 443)
            if context is None:
                raise
            repaired = new_client(timeout=DEFAULT_TIMEOUT, verify=context)
            self._repaired_clients[netloc] = repaired
            # The allow-all robots verdict cached while this host was
            # unreachable was a fallback for a file we could not read; now that
            # we can, re-read it before fetching anything else here.
            self.robots_cache.invalidate(f"{parsed.scheme}://{netloc}")
            print(f"fulltext: recovered missing TLS intermediate for {netloc}", flush=True)
            if not self.robots_cache.can_fetch(url):
                raise UnfetchableDocument(
                    f"robots.txt disallows fetching {url}", status=STATUS_ROBOTS_DISALLOWED
                )
            return repaired.get(url, follow_redirects=False)

    def fetch(self, url: str) -> httpx.Response:
        """Fetch `url`, following redirects HOP-BY-HOP (not via httpx's own
        `follow_redirects=True`, which transparently chases a redirect chain
        across hosts inside a single client.get() call). Politeness must be
        re-checked for EVERY hop's own host -- a redirect from an allowed
        origin to a DIFFERENT host (a real pattern: many state legislature
        sites route bill-document URLs through a CDN or a short-link
        redirector before landing on the actual host) must not silently
        skip that second host's robots.txt or consume none of its rate-limit
        budget just because the first hop was cleared."""
        current_url = url
        for _ in range(MAX_REDIRECT_HOPS + 1):
            parsed = urlparse(current_url)
            scheme = parsed.scheme
            host = parsed.netloc
            if not scheme or not host:
                raise UnfetchableDocument(
                    f"malformed/no-scheme URL {current_url!r}", status=STATUS_MALFORMED_URL
                )
            if scheme not in ("http", "https"):
                raise UnfetchableDocument(
                    f"unsupported redirect scheme {scheme!r} for {current_url}",
                    status=STATUS_UNSUPPORTED_REDIRECT_SCHEME,
                )

            if not self.robots_cache.can_fetch(current_url):
                raise UnfetchableDocument(
                    f"robots.txt disallows fetching {current_url}", status=STATUS_ROBOTS_DISALLOWED
                )
            self.rate_limiter.acquire(host)

            response = self._get(current_url, parsed)
            if not response.is_redirect:
                response.raise_for_status()
                return response

            location = response.headers.get("location")
            if not location:
                response.raise_for_status()
                return response
            current_url = str(response.request.url.join(location))

        raise UnfetchableDocument(
            f"too many redirects (> {MAX_REDIRECT_HOPS}) fetching {url}",
            status=STATUS_TOO_MANY_REDIRECTS,
        )


def process_fetch_text_job(
    db: OrmSession,
    document_id: str,
    *,
    fetcher: FullTextFetcher,
    rawstore: RawStore,
) -> FetchTextResult:
    """Process one `fetch_text` job: fetch the document's URL, extract
    text, persist `extracted_text` + provenance. Raises `UnfetchableDocument`
    for permanent per-document outcomes (robots disallow, empty URL) so the
    caller can dead-letter rather than retry; raises other exceptions for
    transient failures so the queue's normal backoff applies.
    """
    document = db.get(BillDocument, document_id)
    if document is None:
        raise UnfetchableDocument(f"no bill_documents row for id={document_id!r}")

    if not document.url:
        _mark_status(document, STATUS_EMPTY_URL)
        db.flush()
        raise UnfetchableDocument(
            f"document {document_id} has no url", document_id=str(document.id), status=STATUS_EMPTY_URL
        )

    try:
        response = fetcher.fetch(document.url)
    except UnfetchableDocument as exc:
        # `fetcher.fetch` sets `.status` to the SPECIFIC condition it hit
        # (robots_disallowed / malformed_url / unsupported_redirect_scheme /
        # too_many_redirects) -- persist and re-raise that ACTUAL status
        # rather than collapsing every raise site into
        # STATUS_ROBOTS_DISALLOWED. Mislabeling a transient too-many-redirects
        # loop as a terminal robots-disallow would both misreport the reason
        # AND wrongly make it eligible for permanent dead-lettering (see
        # TERMINAL_STATUSES: too_many_redirects is deliberately NOT terminal).
        status = exc.status or STATUS_ROBOTS_DISALLOWED
        _mark_status(document, status)
        db.flush()
        raise UnfetchableDocument(
            str(exc),
            document_id=str(document.id),
            status=status,
        )
    except httpx.HTTPError as exc:
        _mark_status(document, STATUS_FETCH_ERROR)
        db.flush()
        raise RuntimeError(f"fetch failed for {document.url}: {exc}") from exc

    raw = response.content
    # Raw-byte archival is best-effort. The full-document corpus (~730k docs)
    # far exceeds a single Railway volume, so archival must never block text
    # extraction: on a full/failed volume we keep extracted_text + source_url +
    # checksum (sufficient, re-fetchable provenance) and move on. Set
    # FULLTEXT_ARCHIVE_RAW=0 to skip archival entirely (recommended at scale;
    # full raw archival belongs in S3-compatible object storage).
    raw_ref: str | None = None
    if _env_flag("FULLTEXT_ARCHIVE_RAW", default=False):
        try:
            raw_ref = rawstore.put(
                raw,
                meta={
                    "source_name": SOURCE_NAME,
                    "document_id": str(document.id),
                    "url": document.url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except OSError as exc:  # e.g. ENOSPC — never fail extraction over archival
            print(f"fulltext: raw archival skipped for {document.id}: {exc}", flush=True)

    content_type = sniff_content_type(response.headers.get("content-type"), document.url, raw)
    outcome = extract_document_text(content_type, raw)

    document.extracted_text = outcome.extracted_text
    document.media_type = document.media_type or response.headers.get("content-type")
    document.source_name = SOURCE_NAME
    document.retrieved_at = datetime.now(timezone.utc)
    document.raw_ref = raw_ref
    document.checksum = outcome.checksum
    document.parser_version = PARSER_VERSION
    _mark_status(document, outcome.status)
    db.flush()

    return FetchTextResult(
        document_id=str(document.id),
        status=outcome.status,
        extracted_chars=len(outcome.extracted_text) if outcome.extracted_text else 0,
        raw_ref=raw_ref,
    )


def _mark_status(document: BillDocument, status: str) -> None:
    """Record the fetch/extraction outcome. The schema has no dedicated
    fetch-status column, so we encode it in `license_note` with a stable
    prefix -- kept human-readable and greppable, never overloaded with
    anything license-related for this row type (bill_documents rows never
    otherwise use license_note)."""
    document.license_note = f"fulltext_status={status}"


def run_worker_batch(
    db: OrmSession,
    jobs: list[IngestJob],
    *,
    fetcher: FullTextFetcher,
    rawstore: RawStore,
) -> list[FetchTextResult]:
    """Process a batch of already-claimed fetch_text jobs sequentially
    (politeness is per-host anyway, so batch-of-one-worker concurrency
    doesn't help throughput against a single legislature host). Exposed
    mainly for the CLI smoke-test path; the queue worker loop in cli.py
    calls `process_fetch_text_job` per-job via the standard claim/complete/
    fail cycle instead."""
    results = []
    for job in jobs:
        document_id = job.payload.get("document_id")
        result = process_fetch_text_job(db, document_id, fetcher=fetcher, rawstore=rawstore)
        results.append(result)
    return results
