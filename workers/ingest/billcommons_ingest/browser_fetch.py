"""CDP-assisted full-text fetch for explicitly approved robots-dark hosts.

This is deliberately separate from the normal full-text worker.  That worker
continues to honor ``robots.txt`` through ``RobotsCache`` for every request.
This path is Alberto's policy-approved, human-attended use of a Chrome profile
he runs and tunnels to this host over CDP: it fetches public records using his
browser's same-origin, logged-in state.  It consequently does *not* consult
``RobotsCache`` and must never be called by the normal worker loop.
"""
from __future__ import annotations

import base64
import random
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from billcommons_ingest import fulltext
from billcommons_schema.models import Bill, BillDocument, BillVersion
from billcommons_shared.rawstore import RawStore

ALLOWLIST = (
    "capitol.tn.gov",
    "lims.dccouncil.gov",
    "leginfo.legislature.ca.gov",
    "lobbying.wi.gov",
)
CDP_URL = "http://127.0.0.1:9222"
CDP_VERSION_URL = f"{CDP_URL}/json/version"

BrowserFetch = Callable[[str], tuple[int, str | None, bytes]]
BROWSER_DOCUMENT_TIMEOUT_SECONDS = 60.0
BROWSER_RESELECT_AFTER = timedelta(days=7)
# A BrowserTransientError (by design, per R2's BF-3) never charges the real
# fetch_attempts budget -- correct for one-off flakiness. But combined with
# the deterministic newest-first selection order, a document that fails
# this way on EVERY run would otherwise sit at the head of the queue
# forever, consuming a slot of every future invocation with zero forward
# progress (R3-4). After this many CONSECUTIVE transient failures, defer
# reselecting it for BROWSER_TRANSIENT_RESELECT_AFTER; a single flake still
# behaves exactly as before.
BROWSER_TRANSIENT_BACKOFF_AFTER = 3
BROWSER_TRANSIENT_RESELECT_AFTER = timedelta(hours=1)


class BrowserTransientError(RuntimeError):
    """A failure in our browser/CDP path, never a target-host HTTP result."""


class BrowserTunnelLost(BrowserTransientError):
    """The attended Chrome/CDP connection disappeared during this run."""


@dataclass
class BrowserFetchSummary:
    fetched: int = 0
    ok: int = 0
    scanned: int = 0
    errors: int = 0
    skipped: int = 0
    elapsed: float = 0.0
    wall_clock_capped: bool = False
    tunnel_lost: bool = False


def tunnel_is_up(*, get: Callable[..., Any] = httpx.get) -> bool:
    """Check the reverse SSH tunnel without opening a CDP browser session.

    Catches any exception the injected `get` callable can raise, not just
    `httpx.HTTPError`: this runs at `cli.py`'s `cmd_browser_fetch` call site
    OUTSIDE that function's own try/except, so anything narrower would
    escape as a raw traceback instead of the documented "tunnel down"
    no-op (R3-9).
    """
    try:
        response = get(CDP_VERSION_URL, timeout=3.0)
        return 200 <= response.status_code < 300
    except Exception:
        return False


def _host_url_clause(host: str):
    """Match this exact URL host, with HTTP retained for legacy rows."""
    return or_(
        BillDocument.url.like(f"https://{host}/%"),
        BillDocument.url.like(f"http://{host}/%"),
    )


def select_browser_documents(db: OrmSession, host: str, *, limit: int | None) -> list[BillDocument]:
    """Return fetchable browser candidates for one approved host, newest first."""
    if host not in ALLOWLIST:
        raise ValueError(f"browser-fetch host is not allowlisted: {host}")
    stmt = (
        select(BillDocument)
        .join(BillVersion, BillVersion.id == BillDocument.bill_version_id)
        .join(Bill, Bill.id == BillVersion.bill_id)
        .where(
            _host_url_clause(host),
            # license_note_matches_status tolerates decorated forms of any
            # of these statuses -- including this module's own
            # `browser_transient_count=`/`browser_transient_at=` suffix
            # (R3-4) and fulltext.py's `browser_attempted_at=` suffix on a
            # bounded-retry permanently_failed row -- so a document doesn't
            # silently drop out of its own queue the moment either gets
            # stamped onto it. `fetch_error` (R3-3) makes browser-fetch
            # self-sufficient for its own charged non-200s instead of
            # depending on the normal worker's robots-aware enqueue/cadence
            # to bounce it back here indirectly.
            fulltext.license_note_matches_status(
                BillDocument.license_note,
                (
                    fulltext.STATUS_ROBOTS_DISALLOWED,
                    fulltext.STATUS_FETCH_ERROR,
                    fulltext.STATUS_PERMANENTLY_FAILED,
                ),
            ),
            or_(BillDocument.extracted_text.is_(None), BillDocument.extracted_text == ""),
        )
        .order_by(Bill.updated_at.desc(), BillDocument.created_at.desc())
    )
    documents = [
        document
        for document in db.execute(stmt).scalars()
        if not _permanently_failed_recently_browser_attempted(document)
        and not _recently_transient_deferred(document)
    ]
    return documents if limit is None else documents[:limit]


def _round_robin_documents(
    db: OrmSession, hosts: tuple[str, ...], *, limit: int | None
) -> list[BillDocument]:
    if limit is None:
        queues = [select_browser_documents(db, host, limit=None) for host in hosts]
    else:
        per_host, remainder = divmod(limit, len(hosts))
        queues = [
            select_browser_documents(db, host, limit=per_host + (index < remainder))
            for index, host in enumerate(hosts)
        ]
    selected: list[BillDocument] = []
    while any(queues) and (limit is None or len(selected) < limit):
        for queue in queues:
            if queue and (limit is None or len(selected) < limit):
                selected.append(queue.pop(0))
    return selected


def _charge_fetch_error(document: BillDocument) -> None:
    """Apply the normal full-text document retry budget to a browser error."""
    document.fetch_attempts = (document.fetch_attempts or 0) + 1
    status = (
        fulltext.STATUS_PERMANENTLY_FAILED
        if document.fetch_attempts >= fulltext.MAX_FETCH_ATTEMPTS
        else fulltext.STATUS_FETCH_ERROR
    )
    fulltext._mark_status(
        document,
        status,
        browser_attempted_at=datetime.now(timezone.utc).isoformat()
        if status == fulltext.STATUS_PERMANENTLY_FAILED
        else None,
    )


def _permanently_failed_recently_browser_attempted(document: BillDocument) -> bool:
    """Keep browser retries of a dead-lettered row bounded but recoverable."""
    note = document.license_note or ""
    if not note.startswith(f"fulltext_status={fulltext.STATUS_PERMANENTLY_FAILED}"):
        return False
    marker = "browser_attempted_at="
    if marker not in note:
        return False
    value = note.split(marker, 1)[1].split()[0].split(";", 1)[0]
    try:
        attempted_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if attempted_at.tzinfo is None:
        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    return attempted_at >= datetime.now(timezone.utc) - BROWSER_RESELECT_AFTER


def _charge_transient_failure(document: BillDocument) -> None:
    """Track CONSECUTIVE browser-side (never-charged) failures on `document`
    without touching its real fetch_attempts budget (R3-4). A single flake
    changes nothing observable; only after BROWSER_TRANSIENT_BACKOFF_AFTER
    in a row does this defer reselection, so a document that always fails
    this way can't sit at the head of the deterministic queue order
    forever. Any subsequent real status write (`_mark_status`, called by
    `_charge_fetch_error` or a success) replaces `license_note` wholesale,
    so the counter resets the moment a real (charged) outcome happens.
    """
    note = document.license_note or ""
    base = note.split(" browser_transient_count=", 1)[0].split(" browser_transient_at=", 1)[0]
    count = 1
    marker = "browser_transient_count="
    if marker in note:
        try:
            count = int(note.split(marker, 1)[1].split()[0].split(";", 1)[0]) + 1
        except ValueError:
            count = 1
    note = f"{base} browser_transient_count={count}"
    if count >= BROWSER_TRANSIENT_BACKOFF_AFTER:
        note = f"{note} browser_transient_at={datetime.now(timezone.utc).isoformat()}"
    document.license_note = note


def _recently_transient_deferred(document: BillDocument) -> bool:
    """Companion check to `_charge_transient_failure`: true while a
    document is within its post-backoff defer window."""
    note = document.license_note or ""
    marker = "browser_transient_at="
    if marker not in note:
        return False
    value = note.split(marker, 1)[1].split()[0].split(";", 1)[0]
    try:
        deferred_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if deferred_at.tzinfo is None:
        deferred_at = deferred_at.replace(tzinfo=timezone.utc)
    return deferred_at >= datetime.now(timezone.utc) - BROWSER_TRANSIENT_RESELECT_AFTER


def _browser_url(url: str) -> tuple[str, str]:
    """Upgrade approved legacy HTTP rows without changing their stored URL."""
    parsed = urlparse(url)
    if parsed.scheme == "http" and parsed.hostname in ALLOWLIST:
        return urlunparse(parsed._replace(scheme="https")), "browser;scheme=https"
    return url, "browser"


def run_browser_fetch(
    db: OrmSession,
    *,
    hosts: tuple[str, ...],
    limit: int | None,
    pace: float,
    dry_run: bool,
    rawstore: RawStore,
    fetch_via_browser: BrowserFetch,
    sleep: Callable[[float], None] = time.sleep,
    uniform: Callable[[float, float], float] = random.uniform,
    max_seconds: float = 1500.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> BrowserFetchSummary:
    """Fetch selected documents through an injected browser fetch callable.

    The injected callable keeps the database-facing behavior testable without
    Playwright or a CDP endpoint.  Caller owns the transaction and commits the
    recorded outcomes.
    """
    started = monotonic()
    if pace < 0:
        raise ValueError("browser-fetch --pace must be non-negative")
    if limit is not None and limit <= 0:
        raise ValueError("browser-fetch --limit must be positive")
    if max_seconds <= 0:
        raise ValueError("browser-fetch --max-seconds must be positive")
    if not hosts or any(host not in ALLOWLIST for host in hosts):
        raise ValueError("browser-fetch requires one or more allowlisted hosts")

    active_hosts = getattr(fetch_via_browser, "available_hosts", hosts)
    documents = _round_robin_documents(db, active_hosts, limit=limit) if active_hosts else []
    summary = BrowserFetchSummary()
    if dry_run:
        # R3-8: the CLI help/docs promise queue CONTENTS, not just a count.
        for document in documents:
            host = urlparse(document.url or "").hostname
            print(f"browser-fetch: [dry-run] {document.id} host={host} url={document.url}", flush=True)
        summary.skipped = len(documents)
        summary.elapsed = monotonic() - started
        return summary

    for index, document in enumerate(documents):
        if monotonic() - started >= max_seconds:
            summary.wall_clock_capped = True
            print(f"browser-fetch: wall-clock cap reached after {summary.fetched} docs", flush=True)
            break
        attempted = False
        try:
            # A second worker may have populated text after the queue query.
            # Re-read inside this per-document transaction so an operational
            # database error cannot consume this document's retry budget.
            db.refresh(document)
            if document.extracted_text:
                summary.skipped += 1
                continue
            # The query's exact-host predicate is deliberately repeated as a
            # guard against malformed legacy URLs ever being passed to CDP.
            host = urlparse(document.url or "").hostname
            if host not in active_hosts:
                summary.skipped += 1
                continue
            browser_url, provenance = _browser_url(document.url)
            attempted = True
            summary.fetched += 1
            status_code, content_type, raw = fetch_via_browser(browser_url)
            if status_code != 200:
                _charge_fetch_error(document)
                db.flush()
                db.commit()
                summary.errors += 1
                continue
            sniffed_type = fulltext.sniff_content_type(content_type, document.url, raw)
            outcome = fulltext.extract_document_text(sniffed_type, raw)
            result = fulltext.persist_extraction_outcome(
                db,
                document,
                raw=raw,
                content_type=content_type,
                url=document.url,
                outcome=outcome,
                rawstore=rawstore,
                success_status=fulltext.STATUS_OK_BROWSER,
                provenance=provenance,
            )
            if result.status == fulltext.STATUS_SCANNED_PDF_NO_TEXT:
                summary.scanned += 1
            else:
                summary.ok += 1
            # RawStore archival and the relational document transition become
            # durable together before the next pacing sleep.
            db.commit()
        except BrowserTunnelLost:
            db.rollback()
            summary.tunnel_lost = True
            print(f"browser-fetch: tunnel lost after {summary.fetched} docs", flush=True)
            break
        except Exception as exc:  # Browser/parser/DB failures are never target HTTP failures.
            db.rollback()
            summary.errors += 1
            print(f"browser-fetch: transient browser error for {document.id}: {exc}", flush=True)
            try:
                # Never charges fetch_attempts (BF-3); only defers
                # reselection after repeated consecutive failures (R3-4).
                _charge_transient_failure(document)
                db.commit()
            except Exception:
                db.rollback()
        finally:
            # A tunnel-lost break already knows it's exiting the loop; the
            # pacing sleep would otherwise hold this DB session open for up
            # to pace * 1.4 extra, pointless seconds after that's known
            # (R3-5).
            if attempted and index < len(documents) - 1 and not summary.tunnel_lost:
                sleep(pace * uniform(0.6, 1.4))
    summary.elapsed = monotonic() - started
    return summary


class _HostBrowserFetcher:
    """Own one fresh page for one host; existing tabs are never inspected."""

    def __init__(self, host: str, *, document_timeout: float = BROWSER_DOCUMENT_TIMEOUT_SECONDS) -> None:
        self.host = host
        self.document_timeout = document_timeout
        self._playwright = None
        self._browser = None
        self._page = None

    def __enter__(self) -> BrowserFetch:
        # Playwright is intentionally lazy: the ingest test environment does
        # not install it, and all unit tests inject ``fetch_via_browser``.
        from playwright.sync_api import sync_playwright

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)
            if not self._browser.contexts:
                raise BrowserTunnelLost("browser-fetch: CDP browser has no contexts")
            self._page = self._browser.contexts[0].new_page()
            # Playwright's real `Page.evaluate(self, expression, arg=None)`
            # takes no `timeout` kwarg (verified against installed
            # playwright==1.61.0); a hard per-call bound goes through
            # `set_default_timeout` instead. The in-page AbortController
            # already bounds the fetch() itself.
            self._page.set_default_timeout((self.document_timeout + 5) * 1000)
            self._page.goto(f"https://{self.host}/")
        except Exception as exc:
            self.__exit__(None, None, None)
            # `connect_over_cdp` has no browser object when the reverse
            # tunnel disappeared between its health check and connect.
            if self._browser is None or _cdp_disconnected(exc):
                raise BrowserTunnelLost(f"browser-fetch: CDP disconnected: {exc}") from exc
            raise
        return self.fetch

    def fetch(self, url: str) -> tuple[int, str | None, bytes]:
        assert self._page is not None
        try:
            result = self._page.evaluate(
            """async ({ url, timeoutMs }) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                const response = await fetch(url, { credentials: 'include', signal: controller.signal });
                const bytes = new Uint8Array(await response.arrayBuffer());
                let binary = '';
                for (const byte of bytes) binary += String.fromCharCode(byte);
                return {
                    status: response.status,
                    contentType: response.headers.get('content-type'),
                    base64: btoa(binary),
                };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {"url": url, "timeoutMs": int(self.document_timeout * 1000)},
            )
        except Exception as exc:
            if _cdp_disconnected(exc):
                raise BrowserTunnelLost(f"browser-fetch: CDP disconnected: {exc}") from exc
            raise BrowserTransientError(f"browser-fetch evaluate failed: {exc}") from exc
        return int(result["status"]), result.get("contentType"), base64.b64decode(result["base64"])

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._page is not None:
                self._page.close()
        finally:
            # Do not call Browser.close(): this Browser object is attached to
            # Alberto's live Chrome, and the contract here is to touch only
            # the page we created above. Stopping Playwright drops our CDP
            # client connection without closing existing browser pages.
            if self._playwright is not None:
                self._playwright.stop()


@contextmanager
def connected_browser_fetcher(hosts: tuple[str, ...]) -> Iterator[BrowserFetch]:
    """Open one new, same-origin page per host and route fetches by URL host."""
    with ExitStack() as stack:
        fetchers: dict[str, BrowserFetch] = {}
        for host in hosts:
            try:
                fetchers[host] = stack.enter_context(_HostBrowserFetcher(host))
            except BrowserTunnelLost:
                raise
            except ImportError as exc:
                # `_HostBrowserFetcher.__enter__` imports Playwright OUTSIDE
                # its own try/except, so a missing/broken install raises
                # here for every host identically. Folding that into the
                # generic per-host skip below made it print/behave exactly
                # like a genuinely empty queue (fetched=0 ok=0 ... errors=0)
                # -- fail loudly and distinctly instead (R3-6).
                raise RuntimeError(
                    f"browser-fetch: Playwright is not installed/importable in this interpreter: {exc}"
                ) from exc
            except Exception as exc:
                print(f"browser-fetch: skipping {host}; landing page unavailable: {exc}", flush=True)

        def fetch(url: str) -> tuple[int, str | None, bytes]:
            host = urlparse(url).hostname
            if host not in fetchers:
                raise BrowserTransientError(f"browser-fetch URL host is unavailable: {host}")
            return fetchers[host](url)

        fetch.available_hosts = tuple(fetchers)  # type: ignore[attr-defined]
        yield fetch


def _cdp_disconnected(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "browser has been closed",
            "browser has disconnected",
            "connection closed",
            "target page, context or browser has been closed",
            "websocket",
            "not connected",
        )
    )


def print_summary(summary: BrowserFetchSummary) -> None:
    print(
        "browser-fetch: "
        f"fetched={summary.fetched} ok={summary.ok} scanned={summary.scanned} "
        f"errors={summary.errors} skipped={summary.skipped} elapsed={summary.elapsed:.1f}s"
    )
