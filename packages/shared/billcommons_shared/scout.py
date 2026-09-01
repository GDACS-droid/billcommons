"""Pure contracts and security primitives shared by the Scout API and worker.

Nothing in this module fetches a URL, opens a browser, or interprets fetched
text as an instruction.  Keeping those decisions here prevents the API and
worker from slowly growing different cache keys or URL policy.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Callable, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from billcommons_shared.safe_http import SsrfRejected, admit_url

FLORIDA = "FL"
OFFICIAL_FLORIDA_HOSTS = frozenset({
    "www.flsenate.gov", "flsenate.gov", "www.myfloridahouse.gov",
    "myfloridahouse.gov", "www.leg.state.fl.us", "leg.state.fl.us",
})
BROWSER_REQUIRED_FLORIDA_HOSTS = frozenset({"www.myfloridahouse.gov", "myfloridahouse.gov"})
_DIRECT_BROWSER_SHELL_MARKERS = (b"request rejected", b"enable javascript", b"javascript challenge")
# These must identify an interstitial, not merely a normal navigation link.
# Government pages commonly include `/login` and maintenance-policy links in
# their header while the actual legislative body remains fully retrievable.
_UNUSABLE_DIRECT_MARKERS = (
    b"captcha",
    b"<title>login",
    b"login required",
    b"please log in",
    b"authentication required",
    b"enable javascript login",
    b"maintenance mode",
    b"temporarily unavailable for maintenance",
    b"enable javascript maintenance",
)
_SPACE_RE = re.compile(r"\s+")
_BILL_RE = re.compile(r"\b(?:H\.?\s*B\.?|S\.?\s*B\.?|HB|SB)\s*(\d{1,6})\b", re.I)
_TOPICAL_STOPWORDS = frozenset({
    "about", "and", "bill", "bills", "find", "florida", "for",
    "in", "involving", "is", "law", "laws", "legislation", "legislative", "of",
    "on", "research", "scout", "state", "the", "to", "what", "with",
})
_TOPICAL_STEMS = {
    "advertising": "advertis",
    "advertisement": "advertis",
    "advertisements": "advertis",
}


class ScoutPolicyError(ValueError):
    """A deterministic policy rejection suitable for a non-sensitive error class."""


@dataclass(frozen=True)
class ScoutSettings:
    """All Scout operational ceilings, sourced once per process from the environment."""

    enabled: bool = False
    max_query_chars: int = 500
    max_direct_bytes: int = 256 * 1024
    max_external_requests: int = 5
    max_retries: int = 1
    cache_ttl_seconds: int = 3600
    max_pdf_pages: int = 20
    max_pdf_text_chars: int = 20_000
    lease_seconds: int = 90
    max_pages: int = 4
    max_actions: int = 12
    browser_wall_seconds: int = 60
    browser_cleanup_seconds: int = 10
    replay_probe_window_seconds: int = 600
    replay_probe_attempts: int = 5
    max_concurrent_browser_sessions: int = 2
    per_customer_active_jobs: int = 2
    per_customer_daily_jobs: int = 20
    per_customer_daily_browser_seconds: int = 600
    max_browser_routed_requests: int = 40

    @classmethod
    def from_env(cls) -> "ScoutSettings":
        def positive(name: str, default: int) -> int:
            try:
                value = int(os.environ.get(name, str(default)))
            except ValueError:
                return default
            return value if value > 0 else default

        return cls(
            enabled=os.environ.get("BILLCOMMONS_SCOUT_ENABLED", "").lower() in {"1", "true", "yes"},
            max_query_chars=positive("BILLCOMMONS_SCOUT_MAX_QUERY_CHARS", 500),
            max_direct_bytes=positive("BILLCOMMONS_SCOUT_MAX_DIRECT_BYTES", 256 * 1024),
            max_external_requests=positive("BILLCOMMONS_SCOUT_MAX_EXTERNAL_REQUESTS", 5),
            max_retries=positive("BILLCOMMONS_SCOUT_MAX_RETRIES", 1),
            cache_ttl_seconds=positive("BILLCOMMONS_SCOUT_CACHE_TTL_SECONDS", 3600),
            max_pdf_pages=positive("BILLCOMMONS_SCOUT_MAX_PDF_PAGES", 20),
            max_pdf_text_chars=positive("BILLCOMMONS_SCOUT_MAX_PDF_TEXT_CHARS", 20_000),
            lease_seconds=positive("BILLCOMMONS_SCOUT_LEASE_SECONDS", 90),
            max_pages=positive("BILLCOMMONS_SCOUT_MAX_PAGES", 4),
            max_actions=positive("BILLCOMMONS_SCOUT_MAX_ACTIONS", 12),
            browser_wall_seconds=positive("BILLCOMMONS_SCOUT_BROWSER_WALL_SECONDS", 60),
            browser_cleanup_seconds=positive("BILLCOMMONS_SCOUT_BROWSER_CLEANUP_SECONDS", 10),
            replay_probe_window_seconds=positive("BILLCOMMONS_SCOUT_REPLAY_WINDOW_SECONDS", 600),
            replay_probe_attempts=positive("BILLCOMMONS_SCOUT_REPLAY_ATTEMPTS", 5),
            max_concurrent_browser_sessions=positive("BILLCOMMONS_SCOUT_MAX_BROWSER_SESSIONS", 2),
            per_customer_active_jobs=positive("BILLCOMMONS_SCOUT_MAX_ACTIVE_JOBS", 2),
            per_customer_daily_jobs=positive("BILLCOMMONS_SCOUT_MAX_DAILY_JOBS", 20),
            per_customer_daily_browser_seconds=positive("BILLCOMMONS_SCOUT_MAX_DAILY_BROWSER_SECONDS", 600),
            max_browser_routed_requests=positive("BILLCOMMONS_SCOUT_MAX_BROWSER_ROUTED_REQUESTS", 40),
        )


def normalize_query(query: str, *, max_chars: int = 500) -> str:
    normalized = _SPACE_RE.sub(" ", query.strip())
    if not normalized:
        raise ScoutPolicyError("empty_query")
    if len(normalized) > max_chars:
        raise ScoutPolicyError("query_too_long")
    return normalized.casefold()


def normalize_jurisdiction(jurisdiction: str) -> str:
    value = jurisdiction.strip().upper()
    if value != FLORIDA:
        raise ScoutPolicyError("unsupported_jurisdiction")
    return value


def scout_cache_key(query: str, jurisdiction: str, *, freshness_bucket: str = "p0") -> str:
    normalized_jurisdiction = normalize_jurisdiction(jurisdiction)
    normalized_query = normalize_query(query)
    return hashlib.sha256(
        f"{normalized_jurisdiction}\0{normalized_query}\0{freshness_bucket}".encode()
    ).hexdigest()


def canonicalize_url(url: str) -> str:
    """Canonicalize an already-admitted official URL without weakening SSRF policy."""
    try:
        admitted = admit_url(url)
    except SsrfRejected as exc:
        raise ScoutPolicyError("url_rejected") from exc
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname not in OFFICIAL_FLORIDA_HOSTS:
        raise ScoutPolicyError("non_official_host")
    # ``admit_url`` rejects explicit non-default ports, userinfo and fragments;
    # it also produces a RFC3986-safe path/query representation.
    return urlunsplit(("https", hostname, admitted.path_and_query.partition("?")[0], admitted.path_and_query.partition("?")[2], ""))


def is_official_url(url: str) -> bool:
    try:
        canonicalize_url(url)
    except ScoutPolicyError:
        return False
    return True


def browser_required(
    url: str,
    *,
    status: int | None = None,
    mime_type: str | None = None,
    body: bytes = b"",
) -> bool:
    """Allow only the narrowly documented MyFloridaHouse browser fallback.

    Direct 200 responses are ordinarily usable retrievals.  The only exception
    is a small, bounded set of known JavaScript-block shell markers on the
    existing MyFloridaHouse allowlist.  Login, maintenance, and CAPTCHA pages
    remain terminal failures rather than an invitation to drive a browser.
    """
    try:
        host = urlsplit(canonicalize_url(url)).hostname
    except ScoutPolicyError:
        return False
    if host not in BROWSER_REQUIRED_FLORIDA_HOSTS:
        return False
    marker = body[:4096].lower()
    if any(unusable in marker for unusable in _UNUSABLE_DIRECT_MARKERS):
        return False
    if status in {301, 302, 303, 307, 308}:
        # Direct retrieval never follows redirects. The browser may do so only
        # for this existing host allowlist, re-admitting each destination in
        # its route callback before any request is continued.
        return not marker
    if status in {403, 451}:
        # Preserve the existing explicit rejection/challenge behavior.
        return b"javascript" in marker or b"challenge" in marker or not marker
    if status != 200:
        return False
    # The direct-response classifier supplies this value and only labels an
    # HTML shell browser-required.  Keep ``None`` compatible with the existing
    # two-step caller, which has already made that classification.
    if mime_type is not None and mime_type.split(";", 1)[0].strip().lower() != "text/html":
        return False
    return any(shell_marker in marker for shell_marker in _DIRECT_BROWSER_SHELL_MARKERS)


def extract_florida_bill_identifier(query: str) -> str | None:
    match = _BILL_RE.search(query)
    if match is None:
        return None
    prefix = query[match.start() : match.end()].upper().replace(".", "")
    chamber = "HB" if "HB" in prefix.replace(" ", "") else "SB"
    return f"{chamber} {int(match.group(1))}"


def topical_search_terms(query: str, *, maximum: int = 5) -> tuple[str, ...]:
    """Meaningful, bounded corpus terms for Florida topical lookup.

    P0 deliberately removes request framing/domain words ("research Florida
    legislation involving") so a natural-language request is not turned into
    an impossible title conjunction.  Terms remain ordinary SQL data.
    """
    terms = []
    for term in re.findall(r"[a-z0-9]{3,}", normalize_query(query)):
        term = _TOPICAL_STEMS.get(term, term)
        if term not in _TOPICAL_STOPWORDS and term not in terms:
            terms.append(term)
        if len(terms) == maximum:
            break
    return tuple(terms)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_changed(previous_hash: str | None, data: bytes) -> bool:
    return previous_hash != content_hash(data)


@dataclass(frozen=True)
class ContentChange:
    """A bounded, deliberately non-semantic description of a source change.

    The summary contains measurements, not upstream text.  That keeps hostile
    fetched content out of progress/events while making a raw-hash change
    auditable.  ``cosmetic`` is intentionally narrow: only decoded-text
    whitespace normalization may erase a difference.  HTML tag/attribute,
    encoding, and any visible-text differences remain ``material``.
    """

    kind: Literal["unchanged", "cosmetic", "material"]
    summary: str


def summarize_content_change(previous: bytes | None, current: bytes, *, maximum: int = 180) -> ContentChange:
    """Classify a byte change without claiming semantic understanding.

    This is a FETCH → NORMALIZE → HASH comparison primitive, not a legal or
    semantic diff.  It is deterministic and bounded for API storage/display.
    """
    if previous is not None and content_hash(previous) == content_hash(current):
        result = ContentChange("unchanged", "Exact content hash matches the prior source.")
    elif previous is None:
        result = ContentChange("material", "Prior source bytes are unavailable; raw content hashes differ.")
    else:
        prior_text = previous.decode("utf-8", "replace")
        current_text = current.decode("utf-8", "replace")
        prior_normalized = _SPACE_RE.sub(" ", prior_text).strip()
        current_normalized = _SPACE_RE.sub(" ", current_text).strip()
        if prior_normalized == current_normalized:
            result = ContentChange("cosmetic", "Normalized decoded text is unchanged; raw payload differs.")
        else:
            first_difference = next(
                (
                    index
                    for index, pair in enumerate(zip(prior_normalized, current_normalized))
                    if pair[0] != pair[1]
                ),
                min(len(prior_normalized), len(current_normalized)),
            )
            result = ContentChange(
                "material",
                "Normalized text changed "
                f"({len(prior_normalized)}→{len(current_normalized)} chars; "
                f"first difference at {first_difference}).",
            )
    return ContentChange(result.kind, result.summary[:maximum])


def classify_direct_response(
    status: int,
    mime_type: str | None,
    body: bytes,
) -> Literal["usable", "browser_required", "failed"]:
    """Classify direct bytes; ``browser_required`` still requires URL admission.

    This function cannot know the URL.  Its caller must pair a tentative
    browser classification with :func:`browser_required`, which owns the
    explicit MyFloridaHouse allowlist and fails every other source closed.
    """
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if status in {301, 302, 303, 307, 308}:
        return "browser_required"
    if status in {404, 410, 429}:
        return "failed"
    if status in {403, 451}:
        return "browser_required"
    if not 200 <= status < 300 or mime not in {"text/html", "application/pdf", "text/plain"}:
        return "failed"
    if not body.strip() or any(unusable in body[:8192].lower() for unusable in _UNUSABLE_DIRECT_MARKERS):
        return "failed"
    if mime == "text/html" and any(shell_marker in body[:4096].lower() for shell_marker in _DIRECT_BROWSER_SHELL_MARKERS):
        return "browser_required"
    return "usable"


@dataclass(frozen=True)
class BrowserRequest:
    url: str
    max_pages: int
    max_actions: int
    wall_seconds: int
    max_bytes: int
    # Includes the document navigation and every admitted routed subrequest.
    # Kept optional at the tail for third-party/mock provider compatibility.
    max_routed_requests: int = 40


@dataclass(frozen=True)
class BrowserCapture:
    provider_session_id: str
    url: str
    mime_type: str
    body: bytes
    pages: int
    actions: int
    replay_url: str | None = None
    routed_requests: int = 0


class ResearchBrowserProvider(Protocol):
    """Worker-only browser contract. Implementations must enforce URL admission themselves."""

    def capture(self, request: BrowserRequest, *, on_started: Callable[[str], None]) -> BrowserCapture:
        """Invoke ``on_started`` immediately after the remote session exists."""
        ...

    def release(self, provider_session_id: str) -> str | None:
        """Release the provider session and optionally return its replay URL."""
        ...

    def probe_replay(self, provider_session_id: str) -> str | None:
        """Return a replay URL without issuing another remote release."""
        ...
