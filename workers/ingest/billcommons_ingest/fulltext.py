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
import json
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import events
from billcommons_ingest.url_resolvers import (
    MaDocumentUrl,
    is_ma_docket_id,
    ma_api_url,
    ma_docket_from_url,
    resolve_fetch_url,
    resolver_name_for_candidate,
)
from billcommons_schema.models import Bill, BillDocument, BillVersion, IngestJob, Jurisdiction
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
# A document whose text was fetched through Alberto's attended browser over
# CDP.  It is a successful text outcome, distinct from STATUS_OK so browser
# assistance remains auditable and the normal, robots-aware fetch worker can
# never produce it.
STATUS_OK_BROWSER = "ok_browser"
# Text was extracted, but one or more PDF pages crashed pypdf (malformed page
# internals) and contributed nothing. The salvaged text is real and searchable,
# but consumers must not treat it as the complete document -- a diff against a
# partial text can show a phantom "removed" section. Kept distinct from
# STATUS_OK so partial documents stay greppable/countable; the document is
# never re-enqueued (it has text) and a retry would not fix the broken pages.
STATUS_OK_PARTIAL_PDF = "ok_partial_pdf"
STATUS_ROBOTS_DISALLOWED = "robots_disallowed"
STATUS_SCANNED_PDF_NO_TEXT = "scanned_pdf_no_text"
STATUS_FETCH_ERROR = "fetch_error"
STATUS_UNSUPPORTED_TYPE = "unsupported_type"
STATUS_EMPTY_URL = "empty_url"
STATUS_UNSUPPORTED_REDIRECT_SCHEME = "unsupported_redirect_scheme"
STATUS_TOO_MANY_REDIRECTS = "too_many_redirects"
STATUS_MALFORMED_URL = "malformed_url"
STATUS_PERMANENTLY_FAILED = "permanently_failed"
# The document fetched fine (200) via the MA JSON document API, but its
# `DocumentText` field is empty. Reachable only via the generic
# `extract_document_text` "json" branch (see `sniff_content_type` /
# `_is_ma_document_api_url`) -- the actual MA docket/bill resolution path
# (`_resolve_ma_document`) never produces this status; a docket with no
# bill number and no text of its own gets STATUS_MA_DOCKET_NO_BILL_NUMBER
# instead (see below), which is explicitly NOT terminal, because a docket
# that hasn't been assigned a bill number today may be assigned one on a
# later day -- unlike this status, kept for a genuinely no-text JSON body
# reached some other way.
STATUS_NO_DOCUMENT_TEXT = "no_document_text"
# A MA docket has not yet been assigned a bill number (its `BillNumber`
# field is null upstream) AND its own record carries no `DocumentText`
# either (see `_resolve_ma_document`). This is NOT the same fact as
# STATUS_NO_DOCUMENT_TEXT -- a docket can be assigned a bill number (and
# therefore real text) on any later day, so treating "not assigned yet" as
# a permanent verdict (the bug this status replaces) would permanently
# dead-letter a document that is merely early in MA's legislative process.
# Deliberately excluded from TERMINAL_STATUSES; retried like any other
# fetch error, up to MAX_FETCH_ATTEMPTS.
STATUS_MA_DOCKET_NO_BILL_NUMBER = "ma_docket_no_bill_number"
# Recorded when a fetch_text job died of something that is OUR fault, not the
# document's: the database blinked, the object store 500ed, a bug in this
# worker. Non-terminal (the document is still worth fetching) and -- unlike
# STATUS_FETCH_ERROR -- it NEVER burns a fetch attempt, because an
# infrastructure outage would otherwise spend the entire retry budget of
# every document that happened to be in flight and exclude them permanently.
# Kept as its own greppable value so ops can tell the two apart:
#   select license_note, count(*) from bill_documents
#    where license_note like 'fulltext_status=%' group by 1;
STATUS_WORKER_ERROR = "worker_error"

# A docket that has not yet received a bill number is intentionally retried
# on later sweeps, but is not a failed fetch and therefore does not consume
# its retry budget while that upstream assignment is pending. The worker
# starts charging it after this grace period; see record_job_failure.
MA_DOCKET_NO_BILL_NUMBER_GRACE_DAYS = 180
NO_FETCH_ATTEMPT_CHARGE_STATUSES = frozenset({STATUS_MA_DOCKET_NO_BILL_NUMBER})

# Statuses that will NEVER change on a retry -- the document's URL/robots.txt/
# content shape is a fixed fact about that source, not a transient condition.
# enqueue_fulltext_jobs skips documents already marked with one of these so a
# permanently-unfetchable document isn't re-enqueued forever (see the
# license_note-based skip in enqueue_fulltext_jobs below). STATUS_FETCH_ERROR
# and STATUS_TOO_MANY_REDIRECTS are deliberately excluded -- a network/HTTP
# error and a redirect loop are both transient conditions worth retrying (the
# target site's redirect chain today doesn't guarantee its redirect chain
# tomorrow); fetch errors instead stay retryable but are now BOUNDED by
# `BillDocument.fetch_attempts` -- once a document accumulates
# MAX_FETCH_ATTEMPTS recorded failures it is marked STATUS_PERMANENTLY_FAILED
# (which IS terminal) instead of retrying forever.
TERMINAL_STATUSES = frozenset(
    {
        # Browser-fetched documents must never be handed back to the normal
        # worker merely because a legacy row has no extracted_text yet.
        STATUS_OK_BROWSER,
        STATUS_ROBOTS_DISALLOWED,
        STATUS_EMPTY_URL,
        STATUS_SCANNED_PDF_NO_TEXT,
        STATUS_UNSUPPORTED_TYPE,
        STATUS_UNSUPPORTED_REDIRECT_SCHEME,
        STATUS_MALFORMED_URL,
        STATUS_PERMANENTLY_FAILED,
        STATUS_NO_DOCUMENT_TEXT,
    }
)

# Every status in this family represents a successful text acquisition and
# therefore clears the bounded retry budget.  Keep this centralized so a new
# successful fetch path cannot accidentally leave stale charged attempts.
SUCCESS_STATUSES = frozenset({STATUS_OK, STATUS_OK_BROWSER, STATUS_OK_PARTIAL_PDF})


def license_note_matches_status(column, statuses):
    """SQLAlchemy predicate: true when `column` (a `bill_documents.license_note`)
    encodes `fulltext_status=<s>` for any `s` in `statuses`.

    `_mark_status` appends decoration for some outcomes (` url_resolver=...`,
    ` via=...`, ` browser_attempted_at=...`), so an exact string/IN match
    alone misses every decorated row -- e.g. a browser success is stored as
    `fulltext_status=ok_browser via=browser`, never the bare
    `fulltext_status=ok_browser`, and a bounded-retry `permanently_failed`
    row carries a trailing `browser_attempted_at=...`. One shared helper
    keeps every consumer (`enqueue_fulltext_jobs`, `coverage.py`,
    `reset-fetch-attempts`) from drifting out of sync with `_mark_status`'s
    note shape again (R3-1).
    """
    notes = [f"fulltext_status={status}" for status in statuses]
    return or_(
        column.in_(notes),
        *(column.like(f"{note} %") for note in notes),
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


class DocumentFetchError(RuntimeError):
    """A failure that is a fact about THIS DOCUMENT -- its URL is dead, its
    host 500s, its bytes crash the parser.

    Retryable (a host that is down today may be up tomorrow), so NOT terminal,
    but every occurrence spends one of the document's MAX_FETCH_ATTEMPTS: that
    is the whole mechanism that stops a permanently-broken URL from being
    re-fetched forever (observed: 220 attempts on one document, 84,344 dead
    fetch_text rows over 36,085 documents).

    It exists as its own type precisely so the worker can tell it apart from a
    bare Exception escaping the job. Those two used to be indistinguishable, so
    charging the document for either meant an hour of expired object-store
    credentials or a flapping DB connection could spend the full budget of
    every in-flight document and mark them permanently_failed -- unrecoverable
    without hand-written UPDATEs. See is_document_specific_failure and
    `reset-fetch-attempts`.
    """

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        status: str = STATUS_FETCH_ERROR,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.status = status


# SQLSTATE classes that mean "this row's DATA is the problem" rather than "the
# database is having a bad day": 22 data exception (invalid byte sequence, NUL
# in text), 23 integrity constraint violation, 54 program limit exceeded --
# which is the class of `string is too long for tsvector`, the failure that
# cost 309 documents their extracted_text. Everything else (08 connection
# failure, 53 insufficient resources, 57 operator intervention, 58 system
# error, XX internal error) is infrastructure and must never be charged to a
# document's retry budget.
DOCUMENT_SPECIFIC_SQLSTATE_CLASSES = ("22", "23", "54")


def is_document_specific_failure(exc: BaseException) -> bool:
    """Does this failure justify spending one of the document's fetch attempts?

    True only when the failure is attributable to the document itself. The
    default answer is False: an unrecognized exception is treated as OUR bug or
    OUR outage, which costs a retry (cheap, self-healing) instead of a
    document (permanent, and previously unrecoverable).
    """
    if isinstance(exc, (DocumentFetchError, UnfetchableDocument)):
        return True
    # psycopg3 exposes .sqlstate, psycopg2 .pgcode; SQLAlchemy wraps both and
    # keeps the driver exception on .orig.
    candidates = [exc]
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        candidates.append(exc.orig)
    for candidate in candidates:
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(code, str) and code[:2] in DOCUMENT_SPECIFIC_SQLSTATE_CLASSES:
            return True
    return False


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


def extract_text_from_ma_document_json(raw: bytes) -> str:
    """Extract the `DocumentText` field from a malegislature.gov
    `/api/GeneralCourts/{court}/Documents/{id}` response (see
    `_resolve_ma_document`).

    Raises `ValueError` for a MALFORMED body (not valid JSON, or a JSON
    value that isn't an object) -- a body that fails to even parse says
    nothing about whether the SOURCE document has text, so it must never
    be treated as a permanent "no text" verdict (see `extract_document_text`
    below, which converts this into a non-terminal, retryable outcome
    rather than STATUS_NO_DOCUMENT_TEXT).

    Returns `""` (never raises) for a well-formed object whose
    `DocumentText` is absent or not a string -- that IS a real, stable
    fact the caller may treat as "no text", same as every other extractor
    in this module never fabricating text."""
    data = json.loads(raw.decode("utf-8", errors="replace"))  # json.JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("MA document API body is valid JSON but not an object")
    text_value = data.get("DocumentText")
    if not isinstance(text_value, str):
        return ""
    return _normalize_text(text_value)


@dataclass
class PdfExtractionResult:
    text: str
    page_count: int
    scanned_no_text: bool
    broken_pages: int = 0


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
    broken_pages = 0
    for page in reader.pages:
        try:
            page_texts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            # pypdf raises on malformed page internals (seen in prod:
            # "unsupported operand type(s) for +: 'float' and 'IndirectObject'").
            # One broken page must not discard the readable rest of the bill;
            # count it so the caller can downgrade the outcome to
            # STATUS_OK_PARTIAL_PDF. If EVERY page is broken the joined text
            # stays empty and the scanned_no_text flag downgrades the whole
            # document, so garbage is still never presented as text.
            broken_pages += 1
            page_texts.append("")
    joined = _normalize_text("\n".join(page_texts))
    scanned_no_text = page_count > 0 and len(joined.strip()) < SCANNED_PDF_MIN_CHARS
    return PdfExtractionResult(
        text=joined, page_count=page_count, scanned_no_text=scanned_no_text, broken_pages=broken_pages
    )


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


def _is_ma_document_api_url(url: str) -> bool:
    """True for the malegislature.gov JSON document-API endpoint (see
    `_resolve_ma_document` / https://malegislature.gov/api/swagger).

    Deliberately scoped to this one host+path shape rather than sniffing
    "json" generically for every fetch: this pipeline has never needed a
    general JSON extractor, and a generic one could silently misclassify an
    unrelated JSON response from some other jurisdiction's site as
    full bill text.
    """
    parsed = urlparse(url)
    return (
        parsed.hostname in {"malegislature.gov", "www.malegislature.gov"}
        and "/api/" in parsed.path
        and "/Documents/" in parsed.path
    )


def sniff_content_type(content_type_header: str | None, url: str, raw: bytes) -> str:
    """Return one of "html", "xml", "pdf", "text", "json" based on the
    response Content-Type header (preferred), falling back to URL extension,
    falling back to magic-byte sniffing of the body. "json" is only ever
    returned for the MA document-API URL shape (see
    `_is_ma_document_api_url`) -- every other host keeps its prior
    behavior unchanged."""
    header = (content_type_header or "").lower()
    is_ma_api = _is_ma_document_api_url(url)
    if "json" in header and is_ma_api:
        return "json"
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
    if is_ma_api and stripped.startswith(b"{"):
        return "json"
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
        if result.broken_pages:
            return ExtractionOutcome(
                status=STATUS_OK_PARTIAL_PDF,
                extracted_text=result.text,
                checksum=checksum,
                error=f"{result.broken_pages}/{result.page_count} pages unreadable (malformed PDF internals)",
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
    if content_type == "json":
        try:
            text_value = extract_text_from_ma_document_json(raw)
        except ValueError as exc:
            # Malformed/non-object JSON is a fact about THIS FETCH, not the
            # document -- never no_document_text (item 2: that would
            # permanently dead-letter a document over what might be a
            # one-off truncated/garbled response). Raising here (rather
            # than returning a status) means this flows through the same
            # extraction-exception handling every other parser crash in
            # this module already gets in process_fetch_text_job -- it
            # spends one of the document's MAX_FETCH_ATTEMPTS and stays
            # retryable, never terminal.
            raise DocumentFetchError(
                f"malformed MA document API JSON: {exc}", status=STATUS_FETCH_ERROR
            ) from exc
        if not text_value.strip():
            return ExtractionOutcome(status=STATUS_NO_DOCUMENT_TEXT, extracted_text=None, checksum=checksum)
        return ExtractionOutcome(status=STATUS_OK, extracted_text=text_value, checksum=checksum)
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

    # license_note_matches_status tolerates every decorated form _mark_status
    # can produce (` via=...`, ` browser_attempted_at=...`, ...), not just
    # the bare `fulltext_status=<status>` string (R3-1).
    terminal_note_filters = [license_note_matches_status(BillDocument.license_note, TERMINAL_STATUSES)]

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
                ~or_(*terminal_note_filters),
            ),
            BillDocument.fetch_attempts < MAX_FETCH_ATTEMPTS,
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

    # Belt-and-braces: parallel query is already disabled for every session in
    # billcommons_shared.db (see the /dev/shm DiskFull incident documented
    # there), but this is the query whose failure emptied the queue and stopped
    # the crawl for two hours, so it does not rely on connection setup to stay
    # serial. Costs nothing -- EXPLAIN ANALYZE puts the statement at 2.2s in
    # production for a 5,000-row batch.
    db.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))

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

# queue.DEFAULT_MAX_ATTEMPTS = 5, so 15 = 3 full dead-letter cycles. In-cycle
# backoff is 60/120/240/480s; three cycles spread across the enqueue loop's
# cadence spans hours-to-days -- ample for a real transient outage -- while
# bounding worst case at 15 fetches instead of the observed 220.
MAX_FETCH_ATTEMPTS = 15


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

    jurisdiction_code, bill_identifier = _jurisdiction_and_identifier(db, document)

    # A MA docket/bill-shaped URL takes a dedicated resolution path
    # (`_resolve_ma_document`): the real bill number for a docket is only
    # ever known by calling MA's JSON document API and reading it -- it is
    # NOT derivable from the docket id's shape (docket and bill numbers are
    # independent sequences; see url_resolvers module docstring) -- so it
    # cannot be expressed as the static candidate-URL list every other
    # jurisdiction uses (`resolve_fetch_url` / `_fetch_best_candidate`).
    ma_url = ma_docket_from_url(document.url) if jurisdiction_code == "ma" else None

    try:
        if ma_url is not None:
            try:
                response, fetched_url, resolver_name, outcome = _resolve_ma_document(fetcher, ma_url)
            except MaApiLookupError as exc:
                if is_ma_docket_id(ma_url.doc_id):
                    raise
                # A bill-shaped URL already identifies a real bill, so an
                # unavailable MA API must not prevent a working stored PDF
                # from being fetched directly. Docket-shaped URLs remain
                # API-only because their stored page is not authoritative bill text.
                response, fetched_url, outcome = _fetch_best_candidate(
                    fetcher, [document.url], initial_exc=exc
                )
                resolver_name = None
        else:
            candidate_urls = resolve_fetch_url(jurisdiction_code, document.url, bill_identifier)
            response, fetched_url, outcome = _fetch_best_candidate(
                fetcher, candidate_urls, original_url=document.url
            )
            resolver_name = resolver_name_for_candidate(
                jurisdiction_code, document.url, fetched_url, bill_identifier
            )
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
    except (httpx.HTTPError, DocumentFetchError) as exc:
        # DocumentFetchError, not a bare RuntimeError: this is the document's
        # own host/data failing, so it SHOULD spend one of its
        # MAX_FETCH_ATTEMPTS. A bare RuntimeError is indistinguishable from
        # our own worker crashing, which must not cost the document
        # anything. `exc.status`, when the failure already carries one (a
        # DocumentFetchError raised by `_resolve_ma_document` for e.g. a
        # docket with no bill number yet, or a cross-check mismatch),
        # is preserved rather than collapsed to the generic
        # STATUS_FETCH_ERROR -- every OTHER raise site in this function
        # already follows that rule (see the UnfetchableDocument branch
        # above).
        status = getattr(exc, "status", None) or STATUS_FETCH_ERROR
        _mark_status(document, status)
        db.flush()
        raise DocumentFetchError(
            f"fetch failed for {document.url}: {exc}",
            document_id=str(document.id),
            status=status,
        ) from exc

    # ``persist_extraction_outcome`` records the same events.record_event(...,
    # events.TEXT, ...) transition this function historically owned. Its
    # ``had_text_before`` guard remains the transition check, so keeping that
    # event in the shared tail ensures browser-assisted and ordinary fetches
    # announce new searchable text identically without subscriber noise.
    return persist_extraction_outcome(
        db,
        document,
        raw=response.content,
        content_type=response.headers.get("content-type"),
        url=fetched_url,
        outcome=outcome,
        rawstore=rawstore,
        resolver=resolver_name,
    )


def persist_extraction_outcome(
    db: OrmSession,
    document: BillDocument,
    *,
    raw: bytes,
    content_type: str | None,
    url: str,
    outcome: ExtractionOutcome,
    rawstore: RawStore,
    success_status: str | None = None,
    resolver: str | None = None,
    provenance: str | None = None,
) -> FetchTextResult:
    """Persist one extracted document using the shared full-text success tail.

    Both the ordinary, robots-aware worker and the explicitly separate
    browser-assisted path arrive here only after bytes have been acquired.
    ``success_status`` permits the latter to record its auditable
    ``ok_browser`` provenance while retaining the ordinary extractor,
    archival, event, and retry-reset behavior.
    """
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
                    "url": url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except OSError as exc:  # e.g. ENOSPC — never fail extraction over archival
            print(f"fulltext: raw archival skipped for {document.id}: {exc}", flush=True)

    had_text_before = bool(document.extracted_text)
    document.extracted_text = outcome.extracted_text
    document.media_type = document.media_type or content_type
    document.source_name = SOURCE_NAME
    document.retrieved_at = datetime.now(timezone.utc)
    document.raw_ref = raw_ref
    document.checksum = outcome.checksum
    document.parser_version = PARSER_VERSION
    status = success_status if success_status and outcome.status == STATUS_OK else outcome.status
    if status in SUCCESS_STATUSES:
        document.fetch_attempts = 0
    _mark_status(document, status, resolver=resolver, provenance=provenance)
    db.flush()

    # A bill going from "we have no text" to "text available" is the moment it
    # becomes searchable and diffable -- for a policy tracker, usually the most
    # valuable event this system produces. It used to be completely invisible
    # to consumers: this path writes a document row and never touched the bill,
    # so nothing marked the bill as changed.
    #
    # Only on the transition. Re-fetching a document that already had text is
    # crawler bookkeeping, not news.
    if outcome.extracted_text and not had_text_before:
        bill_id = db.execute(
            select(BillVersion.bill_id).where(BillVersion.id == document.bill_version_id)
        ).scalar_one_or_none()
        if bill_id is not None:
            events.record_event(db, bill_id, events.TEXT, "document text available")

    return FetchTextResult(
        document_id=str(document.id),
        status=status,
        extracted_chars=len(outcome.extracted_text) if outcome.extracted_text else 0,
        raw_ref=raw_ref,
    )


def _mark_status(
    document: BillDocument,
    status: str,
    *,
    resolver: str | None = None,
    provenance: str | None = None,
    browser_attempted_at: str | None = None,
) -> None:
    """Record the fetch/extraction outcome. The schema has no dedicated
    fetch-status column, so we encode it in `license_note` with a stable
    prefix -- kept human-readable and greppable, never overloaded with
    anything license-related for this row type (bill_documents rows never
    otherwise use license_note).

    `resolver` and browser `provenance`, if given, are appended only for a
    successful (STATUS_OK / STATUS_OK_BROWSER / STATUS_OK_PARTIAL_PDF)
    outcome.  A browser retry timestamp is the sole terminal-status suffix:
    `enqueue_fulltext_jobs` recognizes its permanently_failed form as
    terminal, while browser-fetch uses it to defer re-selection for seven
    days.
    """
    note = f"fulltext_status={status}"
    if resolver and status in SUCCESS_STATUSES:
        note = f"{note} url_resolver={resolver}"
    if provenance and status in SUCCESS_STATUSES:
        note = f"{note} via={provenance}"
    if browser_attempted_at and status == STATUS_PERMANENTLY_FAILED:
        note = f"{note} browser_attempted_at={browser_attempted_at}"
    document.license_note = note


def _jurisdiction_and_identifier(db: OrmSession, document: BillDocument) -> tuple[str | None, str | None]:
    """Look up the jurisdiction abbreviation (lowercased, for
    `resolve_fetch_url`) and bill identifier for `document`'s bill, or
    `(None, None)` if the chain can't be resolved (never expected in
    practice -- every bill_documents row has a bill_version_id -> bill_id ->
    jurisdiction_id -- but resolve_fetch_url treats `None` the same as "no
    matching rule", so this degrades to today's unresolved-URL behavior
    rather than raising."""
    row = db.execute(
        select(Jurisdiction.abbreviation, Bill.identifier)
        .select_from(BillDocument)
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .join(Bill, Bill.id == BillVersion.bill_id)
        .join(Jurisdiction, Jurisdiction.id == Bill.jurisdiction_id)
        .where(BillDocument.id == document.id)
    ).first()
    if row is None:
        return None, None
    abbreviation, identifier = row
    return (abbreviation.lower() if abbreviation else None), identifier


def _fetch_best_candidate(
    fetcher: "FullTextFetcher",
    candidates: list[str],
    *,
    initial_exc: httpx.HTTPError | DocumentFetchError | None = None,
    original_url: str | None = None,
) -> tuple[httpx.Response, str, "ExtractionOutcome"]:
    """Try each candidate URL in order (see `url_resolvers.resolve_fetch_url`)
    -- the first one that fetches AND yields non-empty extracted text wins.

    Neither an `httpx.HTTPError` (e.g. a 404 from a stale docket-style URL)
    NOR an `UnfetchableDocument` (robots disallow, malformed URL, unsupported
    scheme, too-many-redirects) from ONE candidate aborts the chain -- both
    continue to the next candidate. (Previously an `UnfetchableDocument` was
    raised immediately on the theory that every candidate for a jurisdiction
    shares a host, so a host-level verdict would just repeat -- but a
    redirect loop or a URL malformed only in ONE candidate's specific
    rewritten form doesn't hold for every candidate, so stopping early could
    give up on a candidate that would have worked.) A candidate that fetches
    fine (200) but whose extracted text comes back empty (e.g. a stale
    docket's placeholder/error page, or a MA JSON document whose text
    field is empty) ALSO continues to the next candidate rather than
    accepting the empty result -- first candidate with NON-EMPTY text wins.

    Only once EVERY candidate is exhausted without ever producing non-empty
    text does this raise -- the MOST INFORMATIVE failure seen (the last
    exception, if any candidate raised one; otherwise the empty-but-200
    result from the last candidate, as a `DocumentFetchError`) -- as one of
    `UnfetchableDocument` / `httpx.HTTPError` / `DocumentFetchError`, so
    `process_fetch_text_job`'s existing exception handling (and, for a bare
    extraction crash, its own `except Exception` -> `DocumentFetchError`
    conversion) still spends the document's fetch_attempts budget exactly
    as before -- this function never silently swallows a failure into a
    result that looks like success.
    """
    original_url = original_url or candidates[0]
    outcomes: dict[str, list[tuple[str, BaseException | tuple[httpx.Response, str, "ExtractionOutcome"]]]] = {}
    retryable_outcomes: list[tuple[str, BaseException]] = []

    if initial_exc is not None:
        outcomes.setdefault(original_url, []).append(("retryable", initial_exc))
        retryable_outcomes.append((original_url, initial_exc))

    for url in candidates:
        try:
            response = fetcher.fetch(url)
        except (httpx.HTTPError, UnfetchableDocument) as exc:
            if isinstance(exc, httpx.HTTPError):
                outcomes.setdefault(url, []).append(("retryable", exc))
                retryable_outcomes.append((url, exc))
            else:
                outcomes.setdefault(url, []).append(("terminal", exc))
            continue
        try:
            content_type = sniff_content_type(response.headers.get("content-type"), url, response.content)
            outcome = extract_document_text(content_type, response.content)
        except Exception as exc:  # noqa: BLE001 - try the next candidate; classified below if none work
            outcomes.setdefault(url, []).append(("retryable", exc))
            retryable_outcomes.append((url, exc))
            continue
        if outcome.extracted_text and outcome.extracted_text.strip():
            return response, url, outcome
        empty_outcome = (response, url, outcome)
        outcomes.setdefault(url, []).append(("empty", empty_outcome))

    original_outcomes = outcomes.get(original_url, [])
    for outcome_type, outcome_value in original_outcomes:
        if outcome_type == "terminal":
            raise outcome_value

    original_retryable = next(
        (outcome_value for outcome_type, outcome_value in original_outcomes if outcome_type == "retryable"),
        None,
    )
    if original_retryable is not None:
        retryable_exc = (original_url, original_retryable)
    elif retryable_outcomes:
        retryable_exc = retryable_outcomes[0]
    else:
        retryable_exc = None
    if retryable_exc is not None:
        _error_url, exc = retryable_exc
        if isinstance(exc, (httpx.HTTPError, DocumentFetchError)):
            raise exc
        # Failing candidates' bytes are deliberately not archived; only the
        # winning candidate's raw document is retained below.
        raise DocumentFetchError(
            f"text extraction failed for {_error_url}: {exc}", status=STATUS_FETCH_ERROR
        ) from exc
    original_empty = next(
        (outcome_value for outcome_type, outcome_value in original_outcomes if outcome_type == "empty"),
        None,
    )
    if original_empty is not None:
        return original_empty

    terminal_outcomes = [
        outcome_value
        for candidate_outcomes in outcomes.values()
        for outcome_type, outcome_value in candidate_outcomes
        if outcome_type == "terminal"
    ]
    if terminal_outcomes:
        raise terminal_outcomes[-1]
    empty_outcomes = [
        outcome_value
        for candidate_outcomes in outcomes.values()
        for outcome_type, outcome_value in candidate_outcomes
        if outcome_type == "empty"
    ]
    assert empty_outcomes  # candidates is never empty (original url always present)
    return empty_outcomes[-1]


# ---------------------------------------------------------------------------
# Massachusetts: docket -> authoritative bill-number resolution
# ---------------------------------------------------------------------------


def _parse_ma_document(response: httpx.Response, url: str) -> dict:
    """Parse a malegislature.gov JSON document-API response body into its
    dict, for field access (`BillNumber`/`DocketNumber`) beyond the
    `DocumentText` that `extract_text_from_ma_document_json` reads.

    Raises `DocumentFetchError` (STATUS_FETCH_ERROR, non-terminal -- see
    item 2 of the review this resolver was written for) for a malformed
    body: a response that fails to even parse says nothing about whether
    the SOURCE document has text, so it must never be treated as a
    permanent verdict."""
    try:
        data = json.loads(response.content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise DocumentFetchError(
            f"malformed JSON from MA document API {url}: {exc}", status=STATUS_FETCH_ERROR
        ) from exc
    if not isinstance(data, dict):
        raise DocumentFetchError(f"MA document API {url} did not return a JSON object", status=STATUS_FETCH_ERROR)
    return data


def _assert_ma_field(data: dict, field: str, expected: str, url: str) -> None:
    """Cross-check that `data[field]` (a `DocketNumber` or `BillNumber`
    from a malegislature.gov API response) matches `expected` before its
    `DocumentText` is ever accepted as real bill text.

    This is the check that catches storing the WRONG bill's text: MA
    docket (HD/SD) and bill (H/S) numbers are independent sequences, so a
    derived-from-shape guess (the bug this resolver replaces) can 200 and
    hand back a real, completely unrelated bill's text -- e.g. docket
    HD177 was never assigned a bill number at all, but a naive `HD177 ->
    H177` guess would 200 against H177, a real bill whose OWN docket is
    HD4189 (verified live 2026-08-21). Raises `DocumentFetchError` (never
    terminal -- a mismatch here is either a transient API inconsistency or
    a bug in this resolver, not a fact about the document) rather than
    silently accepting a text mismatch."""
    actual = data.get(field)
    if not isinstance(actual, str) or actual.strip().upper() != expected.strip().upper():
        raise DocumentFetchError(
            f"MA document API {url} {field}={actual!r} does not match expected "
            f"{expected!r} -- refusing to store text under the wrong bill/docket",
            status=STATUS_FETCH_ERROR,
        )


def _clean_ma_document_text(data: dict) -> str | None:
    text_value = data.get("DocumentText")
    if not isinstance(text_value, str):
        return None
    normalized = _normalize_text(text_value)
    return normalized if normalized.strip() else None


def _resolve_ma_document(
    fetcher: "FullTextFetcher", ma_url: "MaDocumentUrl"
) -> tuple[httpx.Response, str, str | None, "ExtractionOutcome"]:
    """Resolve a MA docket/bill-shaped `bill_documents.url` to real,
    authoritative text via malegislature.gov's keyless JSON document API
    (https://malegislature.gov/api/swagger).

    NEVER derives a bill number from the docket id's shape (the bug this
    replaces: `HD177 -> H177` is a guess, and docket/bill numbers are
    independent sequences -- see `_assert_ma_field`'s docstring for the
    live-verified counterexample). Instead: call the API with the DOCKET id
    first, read the AUTHORITATIVE `BillNumber` field, and only then fetch
    the bill's own record (cross-checking it references the SAME docket
    before accepting its text) -- falling back to the bill's PDF/HTML page
    only if the bill's JSON record itself has no text.

    Returns `(response, fetched_url, resolver_name, outcome)` for the
    request that ended up carrying real text. Raises `DocumentFetchError`,
    always non-terminal, for:
      * a docket that has not been assigned a bill number yet AND has no
        text of its own (STATUS_MA_DOCKET_NO_BILL_NUMBER -- a real,
        time-bound fact about the source, not a fetch failure, but NOT a
        permanent one either: the docket may be assigned a bill number on
        a later day, so this must stay retryable, never STATUS_NO_DOCUMENT_
        TEXT);
      * a malformed API body at either step;
      * a cross-check mismatch between the resolved bill and the original
        docket;
      * no usable text via EITHER the bill's JSON record or its PDF page.
    Raises `httpx.HTTPError` / `UnfetchableDocument` untouched from
    `fetcher.fetch` for a genuine transport-level failure (404, robots
    disallow, etc.) at any step -- `process_fetch_text_job`'s existing
    handling for those types applies unchanged.
    """
    if not is_ma_docket_id(ma_url.doc_id):
        # Already bill-style (e.g. "H177", no "D") -- nothing to resolve
        # from a docket; fetch it directly as the target bill.
        return _resolve_ma_bill(
            fetcher,
            ma_url.court,
            ma_url.doc_id,
            expected_docket=None,
            path_prefix=ma_url.path_prefix,
            path_suffix=ma_url.path_suffix,
        )

    docket_url = ma_api_url(ma_url.court, ma_url.doc_id)
    docket_response = fetcher.fetch(docket_url)
    docket_data = _parse_ma_document(docket_response, docket_url)
    _assert_ma_field(docket_data, "DocketNumber", ma_url.doc_id, docket_url)

    bill_number = docket_data.get("BillNumber")
    if not bill_number:
        own_text = _clean_ma_document_text(docket_data)
        if own_text is not None:
            checksum = hashlib.sha256(docket_response.content).hexdigest()
            outcome = ExtractionOutcome(status=STATUS_OK, extracted_text=own_text, checksum=checksum)
            return docket_response, docket_url, "ma_docket_json", outcome
        raise DocumentFetchError(
            f"MA docket {ma_url.doc_id} (court {ma_url.court}) has not been assigned a bill "
            "number yet -- docket not yet assigned a bill number, nothing to fetch",
            status=STATUS_MA_DOCKET_NO_BILL_NUMBER,
        )

    if not isinstance(bill_number, str):
        raise DocumentFetchError(
            f"MA document API {docket_url} BillNumber={bill_number!r} is not a string",
            status=STATUS_FETCH_ERROR,
        )

    return _resolve_ma_bill(
        fetcher,
        ma_url.court,
        bill_number,
        expected_docket=ma_url.doc_id,
        path_prefix=ma_url.path_prefix,
        path_suffix=ma_url.path_suffix,
    )


class MaApiLookupError(DocumentFetchError):
    """A retryable DocumentFetchError produced while querying MA's API.

    Bill-shaped stored URLs may safely fall back to direct fetching after
    these failures; errors from the resolved bill page must not do so.
    """


def _resolve_ma_bill(
    fetcher: "FullTextFetcher",
    court: str,
    bill_number: str,
    *,
    expected_docket: str | None,
    path_prefix: str,
    path_suffix: str,
) -> tuple[httpx.Response, str, str | None, "ExtractionOutcome"]:
    bill_url = ma_api_url(court, bill_number)
    try:
        bill_response = fetcher.fetch(bill_url)
        bill_data = _parse_ma_document(bill_response, bill_url)
        _assert_ma_field(bill_data, "BillNumber", bill_number, bill_url)
        if expected_docket is not None:
            _assert_ma_field(bill_data, "DocketNumber", expected_docket, bill_url)
    except (httpx.HTTPError, DocumentFetchError) as exc:
        raise MaApiLookupError(
            str(exc), status=getattr(exc, "status", None) or STATUS_FETCH_ERROR
        ) from exc

    bill_text = _clean_ma_document_text(bill_data)
    if bill_text is not None:
        checksum = hashlib.sha256(bill_response.content).hexdigest()
        outcome = ExtractionOutcome(status=STATUS_OK, extracted_text=bill_text, checksum=checksum)
        return bill_response, bill_url, "ma_bill_json", outcome

    # The bill's own JSON record 200ed with no text -- fall back to its
    # PDF/HTML page (never the docket's page: the docket-style path is what
    # went stale in the first place).
    page_url = f"{path_prefix}{court}/{bill_number}{path_suffix}"
    page_response = fetcher.fetch(page_url)
    try:
        content_type = sniff_content_type(page_response.headers.get("content-type"), page_url, page_response.content)
        page_outcome = extract_document_text(content_type, page_response.content)
    except Exception as exc:  # noqa: BLE001 - malformed page bytes are document-specific
        raise DocumentFetchError(
            f"text extraction failed for MA bill page {page_url}: {exc}", status=STATUS_FETCH_ERROR
        ) from exc
    if page_outcome.extracted_text and page_outcome.extracted_text.strip():
        return page_response, page_url, "ma_bill_pdf", page_outcome
    raise DocumentFetchError(
        f"MA bill {bill_number} (docket {expected_docket or bill_number}) has no usable text "
        f"via its JSON API record or page {page_url} (page status={page_outcome.status})",
        status=STATUS_FETCH_ERROR,
    )


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
