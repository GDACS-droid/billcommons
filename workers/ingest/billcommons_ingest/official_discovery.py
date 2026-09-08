"""Bounded official landing-page observation and material-link discovery.

The reviewed source inventory is the checked-in 50-state-plus-DC registry.
This adapter discovers links, not legislative facts. Neither a successful
homepage fetch nor an empty link list proves a jurisdiction's corpus current.
Fetched text is data only and never supplies instructions or fetch authority.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from billcommons_shared.data_health import PUBLIC_JURISDICTION_CODES
from billcommons_shared.httpc import USER_AGENT
from billcommons_shared.official_tls import reviewed_context_for_host
from billcommons_shared.safe_http import SafeHttpError, SafeResponse, admit_url, new_safe_http_client
from billcommons_shared.source_budget import consume_request


ADAPTER_NAME = "official_link_discovery"
ADAPTER_VERSION = "official-link-discovery/1"
INVENTORY_VERSION = "2026-09-08"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_ROBOTS_BYTES = 256 * 1024
MAX_LINKS = 40
MAX_SCANNED_ANCHORS = 2000
MAX_URL_LENGTH = 1024
MAX_LABEL_LENGTH = 160
_MATERIAL_WORDS = re.compile(
    r"bill|legislation|journal|calendar|committee|report|analysis|analyses|amendment|download|data|feed",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MaterialLink:
    url: str
    label: str
    kind: str


@dataclass(frozen=True)
class OfficialDiscoveryCapture:
    source_url: str
    retrieved_at: datetime
    http_status: int | None = None
    raw_bytes: bytes | None = None
    content_type: str | None = None
    upstream_modified: str | None = None
    robots_url: str | None = None
    robots_status: int | None = None
    robots_bytes: bytes | None = None
    error_class: str | None = None
    links: tuple[MaterialLink, ...] = ()
    truncated: bool = False


def official_source_inventory() -> dict[str, str]:
    path = Path(__file__).resolve().parents[3] / "data/registry/sessions-2026.json"
    payload = json.loads(path.read_text())
    entries = payload["jurisdictions"]
    result = {entry["state_code"]: entry["official_legislature_homepage"] for entry in entries}
    if len(entries) != 51 or set(result) != PUBLIC_JURISDICTION_CODES:
        raise ValueError("official source inventory must contain exactly 50 states and DC")
    for url in result.values():
        if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
            raise ValueError("invalid official source inventory URL")
    overrides = json.loads((path.parent / "official-source-overrides.json").read_text())
    if overrides["inventory_version"] != INVENTORY_VERSION:
        raise ValueError("official inventory override version does not match this adapter")
    for code, entry in overrides["overrides"].items():
        if code not in result or result[code] != entry["replaces"]:
            raise ValueError("official inventory override does not match its reviewed predecessor")
        admit_url(entry["source_url"])
        if len(entry["source_url"]) > MAX_URL_LENGTH:
            raise ValueError("official inventory override exceeds URL cap")
        result[code] = entry["source_url"]
    return result


def _canonical_same_origin(base_url: str, href: str) -> str | None:
    if not href or len(href) > MAX_URL_LENGTH:
        return None
    try:
        target = urlsplit(urljoin(base_url, href.strip()))
        base = urlsplit(base_url)
        if (target.scheme != "https" or target.hostname != base.hostname
                or target.port not in (None, 443) or target.username is not None
                or target.password is not None):
            return None
        result = urlunsplit((target.scheme, target.netloc, target.path or "/", target.query, ""))
        if len(result.encode("utf-8")) > MAX_URL_LENGTH:
            return None
        admit_url(result)
        return result
    except (ValueError, SafeHttpError):
        return None


class _LinkLimit(Exception):
    pass


class _MaterialLinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.href: str | None = None
        self.label = ""
        self.links: dict[str, MaterialLink] = {}
        self.scanned = 0
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "a":
            return
        self._finish_link()
        self.scanned += 1
        if self.scanned > MAX_SCANNED_ANCHORS:
            self.truncated = True
            raise _LinkLimit
        href = next((value for key, value in attrs if key.casefold() == "href"), None)
        self.href = _canonical_same_origin(self.base_url, href) if isinstance(href, str) else None
        self.label = ""

    def handle_data(self, data):
        if self.href:
            self.label = (self.label + data)[:MAX_LABEL_LENGTH]

    def handle_endtag(self, tag):
        if tag.casefold() == "a":
            self._finish_link()

    def _finish_link(self):
        if self.href:
            path = urlsplit(self.href).path.casefold()
            label = " ".join(self.label.split()).encode("utf-8")[:MAX_LABEL_LENGTH].decode("utf-8", errors="ignore")
            if path.endswith(".pdf"):
                kind = "document_link"
            elif path.endswith((".csv", ".zip", ".json", ".xml", ".rss", ".atom")):
                kind = "data_or_feed_link"
            elif _MATERIAL_WORDS.search(path + " " + label):
                kind = "legislative_navigation_link"
            else:
                kind = None
            if kind and self.href not in self.links:
                if len(self.links) == MAX_LINKS:
                    self.truncated = True
                    self.href = None
                    raise _LinkLimit
                self.links[self.href] = MaterialLink(self.href, label, kind)
        self.href = None
        self.label = ""


def discover_material_links(raw: bytes, *, source_url: str) -> tuple[tuple[MaterialLink, ...], bool]:
    if len(raw) > MAX_PAGE_BYTES:
        raise ValueError("official discovery page exceeds byte cap")
    parser = _MaterialLinkParser(source_url)
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
        parser._finish_link()
    except _LinkLimit:
        pass
    return tuple(parser.links.values()), parser.truncated


def _fetch(url: str, max_body_bytes: int) -> SafeResponse:
    return new_safe_http_client(
        max_body_bytes=max_body_bytes,
        ssl_context_factory=reviewed_context_for_host,
    ).fetch(
        url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*;q=0.1"},
        require_body=True,
    )


def _budget(url: str) -> None:
    # Separate from OpenStates. Every instance shares this public host label.
    consume_request(scope="official:" + urlsplit(url).hostname,
                    daily_limit=24, minimum_interval_seconds=2, maximum_wait_seconds=30)


def capture_official_landing_page(
    jurisdiction: str, source_url: str, *,
    fetch: Callable[[str, int], SafeResponse] = _fetch,
    budget: Callable[[str], None] = _budget,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> OfficialDiscoveryCapture:
    """Read robots first, then one exact reviewed page; never follow redirects.

    All source failures return structured evidence. Registry/config errors are
    rejected before HTTP. The shared transport pins a vetted public address,
    enforces TLS, and bounds every complete request to fifteen seconds.
    """
    inventory = official_source_inventory()
    if inventory.get(jurisdiction) != source_url:
        raise ValueError("source URL is not the reviewed jurisdiction homepage")
    retrieved_at = now or datetime.now(timezone.utc)
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieval timestamp must be timezone-aware")
    evidence = {"source_url": source_url, "retrieved_at": retrieved_at}
    if urlsplit(source_url).scheme != "https":
        return OfficialDiscoveryCapture(**evidence, error_class="https_source_review_required")
    robots_url = urljoin(source_url, "/robots.txt")
    evidence["robots_url"] = robots_url
    try:
        budget(robots_url)
        robots = fetch(robots_url, MAX_ROBOTS_BYTES)
        evidence.update(robots_status=robots.status, robots_bytes=robots.body)
        if robots.body is None or len(robots.body) > MAX_ROBOTS_BYTES:
            return OfficialDiscoveryCapture(**evidence, error_class="robots_body_invalid")
        if robots.status == 200:
            policy = RobotFileParser()
            policy.parse(robots.body.decode("utf-8", errors="replace").splitlines())
            if not policy.can_fetch(USER_AGENT, source_url):
                return OfficialDiscoveryCapture(**evidence, error_class="robots_disallowed")
            crawl_delay = float(policy.crawl_delay(USER_AGENT) or 0)
            rate = policy.request_rate(USER_AGENT)
            if rate and rate.requests > 0:
                crawl_delay = max(crawl_delay, rate.seconds / rate.requests)
            if crawl_delay > 60:
                return OfficialDiscoveryCapture(**evidence, error_class="robots_slow_cadence_review_required")
            if crawl_delay > 0:
                sleep(crawl_delay)
        elif robots.status not in (404, 410):
            return OfficialDiscoveryCapture(**evidence, error_class="robots_unavailable")
        budget(source_url)
        page = fetch(source_url, MAX_PAGE_BYTES)
        evidence.update(http_status=page.status, raw_bytes=page.body,
                        content_type=page.headers.get("content-type", "")[:128],
                        upstream_modified=page.headers.get("last-modified", "")[:128] or None)
        if page.body is None or not 1 <= len(page.body) <= MAX_PAGE_BYTES:
            return OfficialDiscoveryCapture(**evidence, error_class="source_body_invalid")
        if page.status != 200:
            return OfficialDiscoveryCapture(**evidence, error_class="source_http_failure")
        if "text/html" not in str(evidence["content_type"]).casefold():
            return OfficialDiscoveryCapture(**evidence, error_class="source_not_html")
        links, truncated = discover_material_links(page.body, source_url=source_url)
        if not links and b"enable javascript" in page.body.lower():
            return OfficialDiscoveryCapture(**evidence, error_class="javascript_rendering_required")
        return OfficialDiscoveryCapture(**evidence, links=links, truncated=truncated)
    except Exception as exc:
        # Only a fixed class label survives; never a response body, DSN or URL
        # copied from exception text. Bytes already captured remain available.
        return OfficialDiscoveryCapture(**evidence, error_class=type(exc).__name__)
