"""SSRF-guarded outbound HTTP for webhook delivery.

ONE module, used by workers/webhooks/dispatch_webhooks.py for both the
verification challenge and every event delivery. The API never imports this
-- it performs no outbound HTTP at all (see billcommons_api.routers.webhooks).

Transport choice: raw `socket` + `ssl` (stdlib), NOT httpx despite httpx being
an existing pinned dependency (packages/shared already depends on it, see
billcommons_shared.httpc). The reason is connection pinning: this module has
to connect to one address-family-resolved, explicitly-vetted IP while keeping
the ORIGINAL hostname for TLS SNI, the Host header, and full certificate
verification -- and re-vet on every single request, with no connection ever
reused across a re-resolution. httpx's synchronous transport delegates
connection establishment and pooling to httpcore, which does not expose a
public, version-stable hook for "connect to THIS IP but verify/SNI as THAT
hostname, and never let a pooled connection outlive its vetted peer". Reaching
into httpcore internals to force it would be more fragile than building
directly on `socket.create_connection` + `ssl.SSLContext.wrap_socket` +
`http.client.HTTPResponse` for parsing -- the same primitives
billcommons_shared.aia already uses for manual TLS control (see that module).
This also means proxy environment variables (HTTPS_PROXY/ALL_PROXY) are never
consulted, because nothing here reads them -- there is no "trust_env" flag to
turn off; the transport simply never looks.

Every connection is single-use: opened for exactly one request and closed
immediately after (see `SafeHttpClient.fetch`). There is no connection pool,
so "a pooled connection may only be reused if its peer IP is the vetted one"
is satisfied by the strictest possible reading -- it is NEVER reused, vetted
or not. This also means every call independently re-resolves and re-vets,
which is what defeats a TOCTOU resolver that flips its answer between calls.

Address admission (`_is_publicly_routable`) is deliberately NOT just
`ipaddress.ip_address(x).is_global`. A live probe on this box's CPython 3.12.3
found `64:ff9b::7f00:1` (the NAT64 well-known prefix wrapping 127.0.0.1)
reports `is_global=True` -- `is_global` does not know about NAT64 at all, and
an SSRF filter that trusted it alone would let an attacker reach loopback
through any NAT64 gateway. So NAT64 is denied explicitly and unconditionally,
and IPv4-mapped / 6to4 / Teredo IPv6 addresses have their EMBEDDED IPv4
address independently vetted too, on top of (not instead of) the address's
own is_global/is_multicast checks.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

#: Only port this module ever connects to. The URL admission step rejects any
#: URL naming a different port -- see `admit_url`.
DEFAULT_PORT = 443

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 5.0
#: Independent of the per-read timeout above. A receiver that sends one byte
#: every 4.9 seconds never trips a naive per-read timeout and would otherwise
#: hold a delivery thread open forever; this is checked before every single
#: socket operation regardless of how quickly each individual op completes.
TOTAL_BUDGET_SECONDS = 15.0
MAX_BODY_BYTES = 64 * 1024
READ_CHUNK_BYTES = 8192

USER_AGENT = "BillCommons-Webhooks/1.0"

#: hostname -> list of resolved IP address strings (v4 and/or v6). The
#: production default is `default_resolver` (real getaddrinfo); tests inject
#: a fake one mapping a test hostname to 127.0.0.1 to hit a local server, or
#: to something that flips between calls to prove re-vetting.
Resolver = Callable[[str], list[str]]

_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class SafeHttpError(Exception):
    """Base for every rejection this module raises.

    `error_class` is one of the taxonomy strings the dispatcher persists to
    `webhook_subscriptions.last_error` / `webhook_deliveries.error`
    ('timeout', 'tls', 'dns', 'ssrf_rejected', 'too_large', 'connection',
    'transport' -- the last two added round-2 fix #1) -- NEVER a
    free-text message, which could quote attacker- or receiver-controlled
    content (same ToolInvocation precedent as everywhere else in this repo:
    error CLASS only). `reason` is an internal-only short code for our own
    logs/tests; it is deliberately never surfaced to a subscriber.
    """

    error_class = "ssrf_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SsrfRejected(SafeHttpError):
    error_class = "ssrf_rejected"


class DnsFailure(SafeHttpError):
    error_class = "dns"


class TlsFailure(SafeHttpError):
    error_class = "tls"


class TimeoutFailure(SafeHttpError):
    error_class = "timeout"


class TooLarge(SafeHttpError):
    error_class = "too_large"


class ConnectionFailure(SafeHttpError):
    """Round-2 fix #1: a post-connect OSError (ConnectionResetError,
    BrokenPipeError, or any other plain OSError raised during the TLS wrap,
    sendall, response.begin(), or a body read) that is NOT a timeout. Kept
    distinct from `TimeoutFailure` so a subscriber's `last_error` can tell
    "your server dropped the connection" from "your server was slow" apart --
    same reasoning as every other entry in this taxonomy. Also covers a
    connect-phase OSError that is not a timeout (e.g. ECONNREFUSED), which
    previously mislabeled itself 'timeout' (grok LOW#3)."""

    error_class = "connection"


class TransportFailure(SafeHttpError):
    """Round-2 fix #1: belt-and-suspenders catch-all inside `fetch()` --
    nothing above this line is allowed to escape as a raw exception, so
    anything unanticipated (a stdlib surprise, a UnicodeEncodeError from
    request-line encoding, etc.) is still wrapped as a SafeHttpError instead
    of propagating and hitting the dispatcher's generic crash-isolation
    handler, which previously stamped `last_error='internal'` ONLY -- no
    backoff, no consecutive_failures, no failing_since, no WebhookDelivery
    row -- an invisible 2-minute retry forever."""

    error_class = "transport"


@dataclass(frozen=True)
class SafeResponse:
    status: int
    headers: dict[str, str]
    #: `None` only when `require_body=False` and the body read itself
    #: failed (timeout/connection) AFTER the status line + headers were
    #: already read successfully -- verify round-5 fix #9. A delivery's
    #: success is status-only, so nothing in this codebase inspects this
    #: field on that path; a challenge (`require_body=True`) always gets
    #: real `bytes` here (its own body-read failures still raise).
    body: bytes | None


@dataclass(frozen=True)
class _AdmittedUrl:
    hostname: str
    path_and_query: str


#: RFC-3986 characters legal, unescaped, in a path-and-query string: the
#: union of `unreserved` (ALPHA / DIGIT / "-._~"), `sub-delims`
#: ("!$&'()*+,;="), and the extra `pchar`/query-delimiter characters
#: (":@/?"). "%" is handled separately -- see `_normalize_path_and_query`.
_RFC3986_PATH_QUERY_UNESCAPED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~"
    "!$&'()*+,;="
    ":@/?"
)

#: Round-2 fix #3: validates the FULLY NORMALIZED path-and-query is pure
#: RFC-3986 ASCII -- every character is either one of the unescaped-legal
#: set above or part of a valid %XX escape. Purely a defensive final check
#: (`_normalize_path_and_query` itself is constructed to always produce a
#: string matching this), so a bug in that function fails loudly here
#: instead of silently admitting something it shouldn't.
_RFC3986_PATH_QUERY_RE = re.compile(
    r"^(?:[A-Za-z0-9\-._~!$&'()*+,;=:@/?]|%[0-9A-Fa-f]{2})*$"
)


def _normalize_path_and_query(raw: str) -> str:
    """Percent-encode `raw` to pure RFC-3986 ASCII (round-2 fix #3):
    non-ASCII characters (UTF-8 bytes, then %XX -- an emoji or an accented
    character in a path) and EVERY control character (this is what kills
    grok's \\r\\n request-line-injection finding dead -- even though
    `urlsplit` already strips a literal CR/LF from the raw url string
    before this function ever sees it, other control characters like NUL or
    vertical-tab do NOT get stripped, and this function is the only thing
    standing between those and the raw HTTP/1.1 request line this module
    later builds by hand) are percent-encoded. An EXISTING valid %XX escape
    (e.g. a caller-supplied `%C3%A9`) is preserved verbatim, never
    re-encoded -- `%` re-encoding the wrong way is exactly how a
    double-encoding bug would slip a byte past the allowed-char check below.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    hexdigits = "0123456789ABCDEFabcdef"
    while i < n:
        ch = raw[i]
        if ch == "%" and i + 2 < n and raw[i + 1] in hexdigits and raw[i + 2] in hexdigits:
            out.append(raw[i : i + 3])
            i += 3
            continue
        if ch in _RFC3986_PATH_QUERY_UNESCAPED:
            out.append(ch)
            i += 1
            continue
        try:
            # Verify round-3 fix #8: a LONE surrogate (e.g. U+D800, reachable
            # via a malformed %-escape decoded upstream, or a raw url string
            # containing one) cannot be UTF-8 encoded -- `.encode("utf-8")`
            # raises `UnicodeEncodeError`, previously unwrapped, so it
            # propagated past `admit_url` as a raw exception the router did
            # not catch, a 500 instead of the 400 every other malformed url
            # already gets.
            encoded = ch.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SsrfRejected("unencodable_path") from exc
        for b in encoded:
            out.append(f"%{b:02X}")
        i += 1
    normalized = "".join(out)
    if not _RFC3986_PATH_QUERY_RE.fullmatch(normalized):
        # Unreachable given the construction above -- belt-and-suspenders,
        # same "assert the contract, don't just hope it holds" standard the
        # rest of this module uses (see classify_transport_error's sibling
        # in dispatch_webhooks.py).
        raise SsrfRejected("path_query_invalid_after_normalization")
    return normalized


def admit_url(url: str, *, port: int = DEFAULT_PORT) -> _AdmittedUrl:
    """URL-level admission (§3.1): https, default port only, no userinfo, no
    fragment, IDN-normalized hostname. Does NOT touch the network -- address
    vetting happens separately, on the RESOLVED IPs, never on this string.

    `port` defaults to (and in production always is) 443 -- see
    `SafeHttpClient`'s own `port` parameter for why it is plumbed through
    rather than hardcoded: tests need a local HTTPS server on an unprivileged
    port, and this box's test runner is not root.

    `path_and_query` is percent-encoded to pure RFC-3986 ASCII (round-2 fix
    #3) here, at admission -- called from BOTH creation-time (the router
    stores `admitted.path_and_query` as part of the normalized, persisted
    url) and dispatch-time (`SafeHttpClient.fetch` re-admits the stored url
    on every single delivery/challenge attempt), so both call sites get the
    same guarantee from one implementation.
    """
    # Verify round-3 fix #5: `urlsplit` itself raises a bare `ValueError` on
    # a malformed bracketed-IPv6 literal (e.g. "https://[::1/hook", a
    # missing closing "]") -- previously unwrapped, so it propagated past
    # this function as a raw ValueError, which billcommons_api.routers.
    # webhooks' create_webhook did not catch (it only catches SsrfRejected),
    # turning one bad url into a 500 instead of the 400 every other
    # malformed url already gets. `.hostname`/`.port` access below can ALSO
    # raise ValueError on a malformed result even when urlsplit() itself
    # didn't -- wrapped in the same try for the identical reason.
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        url_port = parts.port
    except ValueError as exc:
        raise SsrfRejected("unparseable_url") from exc
    if parts.scheme != "https":
        raise SsrfRejected("non_https_scheme")
    if parts.username is not None or parts.password is not None:
        raise SsrfRejected("userinfo_present")
    if parts.fragment:
        raise SsrfRejected("fragment_present")
    if not hostname:
        raise SsrfRejected("missing_hostname")
    if url_port is not None and url_port != port:
        raise SsrfRejected("non_default_port")
    try:
        normalized_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SsrfRejected("idn_normalization_failed") from exc
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    path = _normalize_path_and_query(path)
    return _AdmittedUrl(hostname=normalized_host, path_and_query=path)


def _embedded_ipv4(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address embedded in an IPv4-mapped, 6to4, Teredo, or NAT64
    IPv6 address, or None if `addr` carries none of those. Checked
    independently of `addr`'s own is_global result -- see module docstring.

    Verify round d1357cd, finding #4: NAT64 (RFC 6052) was NOT covered here
    -- `_is_publicly_routable` (below) still rejects every NAT64 address
    outright via its own `_NAT64_NETWORKS` membership check, run BEFORE
    this function is ever called, so that caller's behavior is unchanged
    (a NAT64 address never reaches this function's NAT64 branch through
    that path). This is added for callers that need the embedded v4 ITSELF
    -- e.g. `billcommons_api.rate_limit.subnet_bucket`, which buckets a
    caller on its embedded v4's /24 rather than a raw IPv6 /48 -- and for
    this function to be a complete, correct utility on its own rather than
    one with a silent gap for exactly the address class this module's own
    module docstring calls out as needing special handling.
    """
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    if addr.teredo is not None:
        return addr.teredo[1]  # (server_ipv4, client_ipv4)
    packed = int(addr)
    # NAT64 Well-Known Prefix (RFC 6052 section 2.1), 64:ff9b::/96: the
    # embedded v4 is exactly the low 32 bits, no reserved byte to skip.
    if addr in _NAT64_NETWORKS[0]:
        return ipaddress.IPv4Address(packed & 0xFFFFFFFF)
    # NAT64 Local-Use prefix (RFC 6052 section 2.2, PL=48 row), 64:ff9b:1::/48:
    # v4 bits 0-15 occupy bits 48-63 (immediately after the /48 prefix), an
    # 8-bit reserved 'u' byte (bits 64-71, must be 0) is skipped, then v4
    # bits 16-31 occupy bits 72-87; the remaining suffix bits are reserved.
    if addr in _NAT64_NETWORKS[1]:
        v4_hi = (packed >> 64) & 0xFFFF
        v4_lo = (packed >> 40) & 0xFFFF
        return ipaddress.IPv4Address((v4_hi << 16) | v4_lo)
    return None


def _is_publicly_routable(ip_str: str) -> bool:
    """The address policy from §3.2, applied to ONE resolved IP.

    Order matters: NAT64 and the embedded-IPv4 checks run unconditionally,
    not gated behind `is_global` -- `is_global` does not know NAT64 exists at
    all (see module docstring), so gating on it first would skip the check
    for exactly the address it is meant to catch.
    """
    addr = ipaddress.ip_address(ip_str)

    if isinstance(addr, ipaddress.IPv6Address):
        if any(addr in net for net in _NAT64_NETWORKS):
            return False
        embedded = _embedded_ipv4(addr)
        if embedded is not None and not _is_publicly_routable(str(embedded)):
            return False

    if addr.is_multicast:
        return False
    if not addr.is_global:
        return False
    return True


def default_resolver(hostname: str) -> list[str]:
    """getaddrinfo-based resolver returning every distinct A/AAAA answer."""
    try:
        infos = socket.getaddrinfo(hostname, DEFAULT_PORT, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DnsFailure(f"getaddrinfo_failed:{exc.errno}") from exc
    seen: set[str] = set()
    addrs: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in seen:
            seen.add(ip)
            addrs.append(ip)
    if not addrs:
        raise DnsFailure("no_answers")
    return addrs


#: hostname -> "is this resolved IP address allowed". Production is always
#: `_is_publicly_routable`; see `SafeHttpClient`'s `address_policy` parameter
#: for the one place this is ever overridden (tests only).
AddressPolicy = Callable[[str], bool]


def _resolve_and_vet(
    hostname: str, resolver: Resolver, policy: AddressPolicy = _is_publicly_routable
) -> str:
    """Resolve `hostname`, collect ALL answers, and return ONE address that
    passes `policy`. Raises SsrfRejected if every resolved address is
    disallowed -- a hostname with a mix of public and private answers (DNS
    rebinding staged across records) is treated as hostile, not "use the
    public one"."""
    addrs = resolver(hostname)
    if not addrs:
        raise DnsFailure("no_answers")
    for candidate in addrs:
        try:
            allowed = policy(candidate)
        except ValueError as exc:
            # Verify round-3 fix #9: `policy` (production: `_is_publicly_
            # routable`) calls `ipaddress.ip_address(...)`, which can raise
            # `ValueError` on an address string `getaddrinfo` handed back
            # that it can't parse as plain IPv4/IPv6 (e.g. a zone-id suffix
            # on an interpreter/libc combination where the stdlib doesn't
            # accept it). Left uncaught, that ValueError would propagate
            # past this function and eventually hit `fetch()`'s generic
            # catch-all, mislabeling a VETTING failure as `TransportFailure`
            # ('transport') -- a retryable-looking class for something that
            # must never be treated as retryable-and-innocent.
            raise SsrfRejected("unparseable_address") from exc
        if not allowed:
            raise SsrfRejected("address_not_publicly_routable")
    return addrs[0]


#: Round-2 fix #12 (codex HIGH, accepted at reduced severity -- glibc's own
#: resolver timeouts bound the leak in practice, but cap it anyway): a
#: bounded, REUSED pool for `getaddrinfo` resolution, replacing the previous
#: "spawn one `threading.Thread(daemon=True)` per `fetch()` call and abandon
#: it on timeout" scheme, which had no cap on how many live-but-abandoned
#: threads could accumulate under sustained slow/hostile-DNS traffic.
#: Module-level (one pool for the whole process, not per-client). "bounded
#: and reused", not "one worker per concurrent delivery" -- a resolution
#: that can't get a pool slot within its remaining budget is itself a
#: `DnsFailure`, exactly as if the DNS server were slow.
#:
#: Verify round-6 fix #3 (opus MED #2): sized at 32, not merely equal to
#: `DELIVERY_CONCURRENCY` (8, in workers/webhooks/dispatch_webhooks.py) --
#: a plain `getaddrinfo()` call has NO timeout parameter of its own and can
#: hang for the full glibc resolver timeout (10-40s across a chain of
#: unresponsive/blackholed nameservers) even though this module's own
#: per-call budget gives up on it after 15s and reports `DnsFailure` back to
#: the caller; the abandoned thread just keeps running in the pool,
#: occupying a slot until glibc itself gives up. At exactly 8 pool slots for
#: 8 concurrent deliveries, a handful of subs pointed at blackholed
#: resolvers can occupy EVERY slot at once -- every OTHER, perfectly healthy
#: delivery due that tick then can't get a pool slot either, so it too
#: records a 'dns' failure it did nothing to deserve, and third-party DNS
#: misconfiguration on a FEW subs can push MANY unrelated, healthy subs
#: toward the 72h auto-disable. Sizing rule: >=4x `DELIVERY_CONCURRENCY` --
#: even if every one of the 8 concurrent deliveries this tick hits a
#: blackholed resolver simultaneously (worst case), there are still 24
#: spare slots for the NEXT tick's 8 (and the one after that) to resolve
#: through while the first 8 abandoned threads are still waiting out
#: glibc's own timeout in the background; those abandoned threads
#: self-terminate (and free their slot) the moment glibc gives up on them,
#: they are never killed early. Deviation from the fix's literal "(daemon
#: threads)" parenthetical:
#: `concurrent.futures.ThreadPoolExecutor` does not expose a way to mark its
#: worker threads OS-daemon (it creates them lazily, inside private,
#: version-unstable internals, and only ever as non-daemon `threading.Thread`
#: instances) -- reaching into those internals to force it would be exactly
#: the kind of fragile reach-in this module's own docstring already
#: rejected for httpcore (see the top of this file). The substance the fix
#: actually wants -- bounded, reused, no unbounded per-call thread growth --
#: is fully delivered by the pool itself; daemon-ness only matters at a
#: CLEAN interpreter shutdown, and this module runs inside a Railway worker
#: that is killed (SIGTERM/SIGKILL), never cleanly `sys.exit()`-ed, so the
#: gap this leaves is negligible in practice.
_DNS_POOL_MAX_WORKERS = 32
_dns_resolver_pool = ThreadPoolExecutor(
    max_workers=_DNS_POOL_MAX_WORKERS, thread_name_prefix="safe_http_dns"
)

#: r12 fix #7 (codex MED): bounds how many resolutions can be QUEUED on
#: `_dns_resolver_pool` at once, on top of the pool's own
#: `_DNS_POOL_MAX_WORKERS` (32) running-at-a-time cap.
#: `ThreadPoolExecutor`'s own internal work queue has no size limit of its
#: own -- a burst of slow/attacker-controlled-DNS targets whose
#: resolutions are each individually abandoned at the 15s
#: `TOTAL_BUDGET_SECONDS` mark (see `_resolve_and_vet_within_budget`) does
#: not stop the abandoned thread from still running to completion in the
#: background, occupying its pool slot the whole time -- and nothing
#: previously stopped MORE submissions from queuing up behind them
#: without limit while they do. Sized at 2x `_DNS_POOL_MAX_WORKERS` (64):
#: enough headroom for a full pool's worth of already-running resolutions
#: PLUS one more full pool's worth freshly queued behind them. This
#: module's only production caller (workers/webhooks/dispatch_webhooks.py)
#: submits at most DELIVERY_CONCURRENCY=8 concurrent deliveries' worth at
#: a time, so 64 outstanding is several ticks' worth of simultaneous
#: submissions, never a number ordinary traffic approaches -- past it, a
#: submission is refused immediately as a DnsFailure, the SAME outcome a
#: caller already gets from a resolver that is simply down, rather than
#: queuing behind an unbounded pile of someone else's hung resolutions.
_DNS_BACKLOG_MAX = _DNS_POOL_MAX_WORKERS * 2
_dns_backlog_lock = threading.Lock()
_dns_backlog_count = 0


def _resolve_and_vet_within_budget(
    hostname: str, resolver: Resolver, policy: AddressPolicy, timeout_seconds: float
) -> str:
    """Same as `_resolve_and_vet`, but run on `_dns_resolver_pool` and
    bounded by `timeout_seconds` (fix #13, hardened by round-2 fix #12):
    `resolver` -- `default_resolver` in production, i.e.
    `socket.getaddrinfo` -- has no timeout parameter of its own and can
    block well past the 15s wall-clock budget on a slow/hostile DNS server,
    defeating `TOTAL_BUDGET_SECONDS` entirely. On timeout this raises
    DnsFailure (the 'dns' error class, not 'timeout' -- a subscriber reading
    `last_error` should be able to tell "your DNS is slow" from "your
    server is slow" apart). A hung resolution is simply abandoned on the
    pool (the submitted work item keeps running to completion in the
    background and its result is discarded) -- the pool itself is bounded
    to 8 concurrent resolutions process-wide, so the worst case is 8 slow
    resolutions tying up 8 workers, never an unbounded pile of threads.

    r12 fix #7 (codex MED): two additions, both against the same
    "attacker-controlled slow DNS accumulates unbounded work" class. (1)
    `_DNS_BACKLOG_MAX` bounds how many resolutions can be outstanding
    (queued or running) on the pool at once -- see that constant's own
    comment; a submission past the bound fails fast instead of queuing.
    (2) on timeout, `future.cancel()` is called before raising: if the
    work item has not yet been picked up by a pool worker, this prevents
    it from EVER running at all, freeing that queue slot immediately
    rather than leaving it to eventually run (and occupy a worker) only to
    have its result discarded anyway. If the work item is ALREADY running,
    `cancel()` is a documented no-op (a plain `getaddrinfo()` call cannot
    be interrupted mid-syscall) -- the thread still runs to completion in
    the background, exactly as before; only the “still queued, never
    started” case is newly avoided.
    """
    global _dns_backlog_count
    with _dns_backlog_lock:
        if _dns_backlog_count >= _DNS_BACKLOG_MAX:
            raise DnsFailure("resolver_backlog_saturated")
        _dns_backlog_count += 1

    def _release_backlog_slot(_future) -> None:
        global _dns_backlog_count
        with _dns_backlog_lock:
            _dns_backlog_count -= 1

    try:
        future = _dns_resolver_pool.submit(_resolve_and_vet, hostname, resolver, policy)
    except RuntimeError as exc:
        # The pool would only ever be shut down at interpreter exit --
        # production code never calls `.shutdown()` on it -- but fail safe
        # (a DnsFailure, not a bare RuntimeError) rather than assume that.
        with _dns_backlog_lock:
            _dns_backlog_count -= 1
        raise DnsFailure("resolver_pool_unavailable") from exc
    # Fires on EVERY outcome (result, exception, or cancellation), exactly
    # once, however long the work item takes to actually finish -- the one
    # place the backlog slot this call reserved above is reliably freed.
    future.add_done_callback(_release_backlog_slot)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise DnsFailure("resolution_exceeded_budget") from exc


def _make_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Restrict ALPN to HTTP/1.1 only. Not merely "we happen to speak 1.1" --
    # this makes HTTP/2 unnegotiable even if the receiver would otherwise
    # prefer it, closing the connection-coalescing bypass §3.4 calls out
    # (a coalesced h2 connection can serve a request for one origin over a
    # connection whose certificate covers a DIFFERENT, attacker-chosen one).
    context.set_alpn_protocols(["http/1.1"])
    return context


class SafeHttpClient:
    """SSRF-guarded HTTPS client. One instance is reusable across many
    `fetch()` calls (it holds no per-target state); every call opens its own
    connection and re-resolves/re-vets from scratch.

    `resolver`/`address_policy` are the test hooks the spec calls for (§3.5:
    "the resolver/allowlist override is a CONSTRUCTOR argument") -- never an
    environment variable, so a test cannot leak into another process or
    another test by accident. Production code (the dispatcher, via
    `new_safe_http_client()`) never overrides `address_policy`, so the real
    SSRF vetting is always live outside tests; it exists ONLY because the
    vetting correctly rejects 127.0.0.1 and every other loopback/private
    address, which is otherwise the one address a local test server can bind
    to without root. `port` is a third, narrower test accommodation: also
    never passed by production code, which is always 443 in the app that
    ships; it lets the test suite point a real TLS handshake at a local
    server bound to an unprivileged port instead of needing root to bind 443.
    """

    def __init__(
        self,
        resolver: Resolver = default_resolver,
        port: int = DEFAULT_PORT,
        address_policy: AddressPolicy = _is_publicly_routable,
    ) -> None:
        self._resolver = resolver
        self._port = port
        self._address_policy = address_policy

    def fetch(
        self,
        url: str,
        *,
        method: str = "POST",
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        require_body: bool = True,
    ) -> SafeResponse:
        """Public entrypoint. Round-2 fix #1: a final belt-and-suspenders
        catch-all -- every `SafeHttpError` raised by `_fetch` propagates
        unchanged (it already carries the right taxonomy class), but ANY
        other exception (a stdlib surprise, a `UnicodeEncodeError` from
        encoding the request line, ...) is wrapped as a `TransportFailure`
        ('transport') instead. Without this, a raw exception hits the
        dispatcher's generic crash-isolation `except Exception` and gets
        stamped `last_error='internal'` ONLY -- no backoff, no
        consecutive_failures, no failing_since (so the 72h auto-disable
        never fires), no WebhookDelivery audit row -- an invisible 2-minute
        retry forever. Nothing below this method is allowed to escape raw.
        """
        try:
            return self._fetch(
                url, method=method, body=body, headers=headers, require_body=require_body
            )
        except SafeHttpError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the whole point is "catch everything else"
            raise TransportFailure(f"unexpected:{exc.__class__.__name__}") from exc

    def _fetch(
        self,
        url: str,
        *,
        method: str = "POST",
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        require_body: bool = True,
    ) -> SafeResponse:
        """`require_body` (fix #15): the verification challenge needs the
        exact response body (the token-echo comparison); a delivery does
        not -- success is 2xx status alone (§4.6/§5 define no delivery-side
        use of the response body at all). Challenges (the default) keep the
        strict read: non-identity Content-Encoding and an over-cap body both
        raise TooLarge. Deliveries pass `require_body=False`: the body is
        still read (respecting every timeout) and discarded up to the cap,
        but neither condition raises -- an echoing receiver that gzips its
        ack or sends one back oversized must never fail a delivery that
        already succeeded at the HTTP level. Verify round-5 fix #9: a
        body-READ failure itself (timeout, or the connection dropping mid-
        body) is ALSO swallowed for `require_body=False`, once the status
        line and headers are already read -- see `SafeResponse.body`'s own
        docstring.
        """
        admitted = admit_url(url, port=self._port)
        start = time.monotonic()

        def remaining() -> float:
            left = TOTAL_BUDGET_SECONDS - (time.monotonic() - start)
            if left <= 0:
                raise TimeoutFailure("wall_clock_budget_exceeded")
            return left

        # DNS outside the budget (fix #13): `self._resolver` -- real
        # getaddrinfo in production -- has no timeout of its own and can
        # block past TOTAL_BUDGET_SECONDS on a slow/hostile server. Run it
        # in a worker thread joined against the remaining budget.
        pinned_ip = _resolve_and_vet_within_budget(
            admitted.hostname, self._resolver, self._address_policy, remaining()
        )

        try:
            raw_sock = socket.create_connection(
                (pinned_ip, self._port), timeout=min(CONNECT_TIMEOUT_SECONDS, remaining())
            )
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutFailure("connect_timeout") from exc
        except OSError as exc:
            # Round-2 fix #1 (grok LOW#3): a connect-phase OSError that is
            # NOT a timeout (e.g. ECONNREFUSED) is a connection failure, not
            # a timeout -- it was previously mislabeled 'timeout'.
            raise ConnectionFailure(f"connect_failed:{exc.__class__.__name__}") from exc

        # SSL socket leak (fix #12): once `wrap_socket` succeeds, the
        # returned SSLSocket -- not `raw_sock` -- owns the underlying fd.
        # `sock` starts None and is set only on a successful wrap; the
        # `finally` below closes whichever one actually ended up owning the
        # connection, exactly once, never both.
        sock = None
        try:
            raw_sock.settimeout(min(READ_TIMEOUT_SECONDS, remaining()))
            try:
                # server_hostname preserves SNI + is what full certificate
                # verification checks against -- the ORIGINAL hostname, never
                # the pinned IP. This is the entire pinning mechanism: connect
                # by IP, verify by name.
                sock = _make_ssl_context().wrap_socket(
                    raw_sock, server_hostname=admitted.hostname
                )
            except (TimeoutError, socket.timeout) as exc:
                raise TimeoutFailure("tls_handshake_timeout") from exc
            except ssl.SSLError as exc:
                raise TlsFailure(exc.__class__.__name__) from exc
            except OSError as exc:
                # Round-2 fix #1: ConnectionResetError/BrokenPipeError/plain
                # OSError during the TLS wrap itself (below the ssl.SSLError
                # layer -- e.g. the peer resets the TCP connection mid-
                # handshake) previously propagated raw.
                raise ConnectionFailure(f"tls_wrap_failed:{exc.__class__.__name__}") from exc

            # Verify round-3 fix #4 (RFC 9110 §7.2): `admitted.hostname` is
            # the bare address for an IPv6 literal target (`urlsplit`
            # strips the "[...]" brackets -- see `admit_url`'s own docstring
            # on the identical stripping for `.path_and_query`'s
            # reconstruction). A bare "Host: 2001:db8::1" is ambiguous with
            # a Host:port separator and is not a valid Host header value at
            # all; RFC 9110 requires re-bracketing. Live-probed on this
            # box's CPython 3.12.3: `'2001:db8::1'.encode('idna')` does NOT
            # raise, so `admit_url` genuinely admits IPv6 literals -- this
            # is reachable, not theoretical. ":" can never appear in a DNS
            # hostname/IDNA label, so this is an unambiguous IPv6-literal
            # test, not a heuristic (same test `create_webhook` already uses
            # for the identical reason when reconstructing the stored url).
            host_header = f"[{admitted.hostname}]" if ":" in admitted.hostname else admitted.hostname
            request_headers = {
                "Host": host_header,
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
                "Content-Length": str(len(body)),
                "Connection": "close",
                **(headers or {}),
            }
            lines = [f"{method} {admitted.path_and_query} HTTP/1.1\r\n"]
            lines.extend(f"{k}: {v}\r\n" for k, v in request_headers.items())
            lines.append("\r\n")
            request_bytes = "".join(lines).encode("latin-1") + body

            try:
                sock.settimeout(min(READ_TIMEOUT_SECONDS, remaining()))
                sock.sendall(request_bytes)

                response = http.client.HTTPResponse(sock, method=method)
                sock.settimeout(min(READ_TIMEOUT_SECONDS, remaining()))
                response.begin()
            except (TimeoutError, socket.timeout) as exc:
                raise TimeoutFailure("request_timeout") from exc
            except http.client.HTTPException as exc:
                # Verify round-3 fix #15: a malformed status line or invalid
                # chunked encoding is a PROTOCOL violation by the receiver,
                # not "no answer within the budget" -- `TimeoutFailure`
                # ('timeout') told a subscriber their endpoint was slow when
                # it actually responded, just with garbage. `TransportFailure`
                # ('transport') is the taxonomy class this round-2-added
                # exception type already exists for (see its own docstring).
                raise TransportFailure(f"bad_response:{exc.__class__.__name__}") from exc
            except OSError as exc:
                # Round-2 fix #1: ConnectionResetError/BrokenPipeError/plain
                # OSError during sendall or response.begin() -- the receiver
                # dropped the connection mid-request/mid-response-line --
                # previously propagated raw.
                raise ConnectionFailure(f"send_or_begin_failed:{exc.__class__.__name__}") from exc

            # Redirects are failures (§3.4) -- a receiver's 3xx is never
            # followed, closing the SSRF-via-redirect-to-a-private-IP class.
            if 300 <= response.status < 400:
                raise SsrfRejected(f"redirect_status_{response.status}")

            content_encoding = (response.getheader("Content-Encoding") or "identity").lower()
            if require_body and content_encoding != "identity":
                # We asked for identity; a receiver that compresses anyway
                # could exceed the decoded-size cap while looking small on
                # the wire, so it is rejected outright rather than decoded.
                # Only enforced when the body is actually needed (fix #15).
                raise TooLarge("non_identity_encoding")

            chunks: list[bytes] = []
            total = 0
            body_bytes: bytes | None = b""
            try:
                while True:
                    sock.settimeout(min(READ_TIMEOUT_SECONDS, remaining()))
                    try:
                        chunk = response.read(READ_CHUNK_BYTES)
                    except (TimeoutError, socket.timeout) as exc:
                        raise TimeoutFailure("body_read_timeout") from exc
                    except http.client.IncompleteRead as exc:
                        # Incomplete-read cap bypass (fix #14): `exc.partial`
                        # must count toward the 64KB total BEFORE it is
                        # appended, same as a normal chunk -- otherwise a
                        # connection that dies mid-body after already sending
                        # well over the cap slips through uncounted.
                        total += len(exc.partial)
                        if total > MAX_BODY_BYTES:
                            if require_body:
                                raise TooLarge("response_body_too_large") from exc
                            break
                        chunks.append(exc.partial)
                        break
                    except OSError as exc:
                        # Round-2 fix #1: ConnectionResetError/BrokenPipeError/
                        # plain OSError mid-body-read -- the receiver dropped
                        # the connection partway through the body -- previously
                        # propagated raw.
                        raise ConnectionFailure(f"body_read_failed:{exc.__class__.__name__}") from exc
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        if require_body:
                            raise TooLarge("response_body_too_large")
                        # Delivery, not a challenge: the body was never needed
                        # in the first place (fix #15) -- stop collecting and
                        # discard the rest rather than erroring a delivery that
                        # already succeeded at the HTTP level.
                        break
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
            except (TimeoutFailure, ConnectionFailure):
                if require_body:
                    raise
                # Verify round-5 fix #9 (deepseek #2): the status line and
                # headers are ALREADY read successfully by this point
                # (`response.begin()` above succeeded) -- a delivery's
                # documented success contract is "2xx status alone" (§5/§4.6
                # define no delivery-side use of the response body at all),
                # so a receiver that answers 200 and then stalls or resets
                # mid-body must not fail a delivery that already succeeded
                # at the HTTP level. Only reachable for `require_body=False`
                # (deliveries); a challenge (`require_body=True`, the
                # default) needs the exact body for its token-echo
                # comparison and keeps the strict raise. `body_bytes` stays
                # `None` -- distinct from `b""` (a genuinely empty body),
                # signaling "never fully read", though nothing in this
                # codebase inspects a delivery's response body either way.
                body_bytes = None

            return SafeResponse(
                status=response.status,
                headers={k.lower(): v for k, v in response.getheaders()},
                body=body_bytes,
            )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            else:
                try:
                    raw_sock.close()
                except OSError:
                    pass


def new_safe_http_client(
    resolver: Resolver | None = None,
    *,
    port: int = DEFAULT_PORT,
    address_policy: AddressPolicy = _is_publicly_routable,
) -> SafeHttpClient:
    """Production entrypoint. The dispatcher calls this with no arguments,
    which wires in `default_resolver`, the real `_is_publicly_routable`
    policy, and port 443 -- real getaddrinfo plus full address vetting,
    connecting only to the standard HTTPS port. All three parameters exist
    only so tests can override them the same way `SafeHttpClient(...)` does;
    there is no other way to bypass the guard, and in particular no
    environment variable does it.
    """
    return SafeHttpClient(
        resolver=resolver if resolver is not None else default_resolver,
        port=port,
        address_policy=address_policy,
    )
