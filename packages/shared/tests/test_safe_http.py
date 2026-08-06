"""Tests for the SSRF-guarded webhook transport.

These are the load-bearing tests for the whole webhooks feature: everything
else (the dispatcher, the API) trusts this module to make "reach an
attacker-controlled internal address" impossible. A test here that can't fail
when the guard breaks isn't doing its job -- see billcommons_shared.aia's
tests docstring for the same standard applied to a sibling security module.

`_local_https_server` spins up a real TLS server on 127.0.0.1 with a
self-signed CA generated per-test, and `_trusting_context` (monkeypatched over
`ssl.create_default_context`) is what lets the client's REAL certificate
verification pass against it -- so these tests exercise actual TLS
verification, not a stub that skips it.
"""
from __future__ import annotations

import datetime
import http.server
import socket
import ssl
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from billcommons_shared import safe_http

TEST_HOSTNAME = "webhook-test.billcommons.internal"


# ---------------------------------------------------------------------------
# §3.2 address policy -- table-driven, exactly the list the spec calls out
# ---------------------------------------------------------------------------

REJECTED_ADDRESSES = [
    "64:ff9b::7f00:1",  # NAT64-wrapped loopback -- the load-bearing case
    "224.0.0.1",  # multicast
    "ff02::1",  # multicast
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "2002:c0a8:0101::",  # 6to4-embedded private v4 (192.168.1.1)
    "10.1.2.3",
    "127.0.0.1",
    "169.254.1.1",
    "100.64.1.1",  # CGNAT shared address space
    "fc00::1",
    "fe80::1",
    "0.0.0.0",
]

ACCEPTED_ADDRESSES = ["8.8.8.8", "2001:4860:4860::8888"]


@pytest.mark.parametrize("ip", REJECTED_ADDRESSES)
def test_rejects_disallowed_address(ip):
    assert safe_http._is_publicly_routable(ip) is False


@pytest.mark.parametrize("ip", ACCEPTED_ADDRESSES)
def test_accepts_public_address(ip):
    assert safe_http._is_publicly_routable(ip) is True


# ---------------------------------------------------------------------------
# admit_url -- URL-level admission (§3.1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",  # not https
        "https://user:pass@example.com/hook",  # userinfo
        "https://example.com/hook#frag",  # fragment
        "https://example.com:8080/hook",  # non-default port
    ],
)
def test_admit_url_rejects(url):
    with pytest.raises(safe_http.SsrfRejected):
        safe_http.admit_url(url)


def test_admit_url_accepts_plain_https():
    admitted = safe_http.admit_url("https://example.com/hook?x=1")
    assert admitted.hostname == "example.com"
    assert admitted.path_and_query == "/hook?x=1"


def test_admit_url_accepts_explicit_default_port():
    admitted = safe_http.admit_url("https://example.com:443/hook")
    assert admitted.hostname == "example.com"


# ---------------------------------------------------------------------------
# Round-3 fix #5: a malformed bracketed-IPv6 url must 400 (SsrfRejected),
# never a raw ValueError propagating past this module as a 500.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1/hook",  # missing closing bracket
        "https://[::1]:99999/hook",  # port out of range -- also ValueError
    ],
)
def test_admit_url_rejects_malformed_bracket_url_as_ssrf_rejected_not_valueerror(url):
    with pytest.raises(safe_http.SsrfRejected):
        safe_http.admit_url(url)


# ---------------------------------------------------------------------------
# Round-3 fix #8: a lone UTF-16 surrogate in the path must 400
# (SsrfRejected), never a raw UnicodeEncodeError.
# ---------------------------------------------------------------------------


def test_admit_url_rejects_lone_surrogate_in_path_as_ssrf_rejected():
    with pytest.raises(safe_http.SsrfRejected):
        safe_http.admit_url("https://example.com/\ud800")


# ---------------------------------------------------------------------------
# Round-2 fix #3: path/query is percent-encoded to pure RFC-3986 ASCII at
# admission -- non-ASCII and every control char encoded, existing %-escapes
# preserved verbatim (no double-encoding).
# ---------------------------------------------------------------------------


def test_admit_url_percent_encodes_a_crlf_injection_attempt():
    """A literal CR/LF embedded in the path -- even though `urlsplit` itself
    already strips a literal CR/LF from the raw url string before
    `admit_url` ever sees it (CPython's own hardening), this proves the
    OUTPUT never contains a raw \\r or \\n byte either way, so the request
    line this module builds by hand later can never be split by one."""
    admitted = safe_http.admit_url("https://example.com/hook\r\nX-Injected: evil")
    assert "\r" not in admitted.path_and_query
    assert "\n" not in admitted.path_and_query


def test_admit_url_percent_encodes_other_control_chars_urlsplit_does_not_strip():
    """Unlike CR/LF, `urlsplit` does NOT strip a NUL byte or a vertical tab
    -- these must still come out percent-encoded, not raw."""
    admitted = safe_http.admit_url("https://example.com/ho\x0bok\x00x")
    assert "\x0b" not in admitted.path_and_query
    assert "\x00" not in admitted.path_and_query
    assert "%0B" in admitted.path_and_query
    assert "%00" in admitted.path_and_query


def test_admit_url_percent_encodes_emoji_path():
    admitted = safe_http.admit_url("https://example.com/hook/\U0001f600")
    assert admitted.path_and_query == "/hook/%F0%9F%98%80"


def test_admit_url_percent_encodes_cafe_path():
    admitted = safe_http.admit_url("https://example.com/café")
    assert admitted.path_and_query == "/caf%C3%A9"


def test_admit_url_preserves_existing_percent_escapes_no_double_encoding():
    """A caller-supplied, already-percent-encoded %C3%A9 (also "cafe" with
    an accented e, pre-escaped) must round-trip UNCHANGED -- re-encoding the
    literal '%' would turn it into %25C3%25A9, a different (wrong) path."""
    admitted = safe_http.admit_url("https://example.com/caf%C3%A9")
    assert admitted.path_and_query == "/caf%C3%A9"


def test_admit_url_percent_encodes_a_lone_percent_not_part_of_a_valid_escape():
    admitted = safe_http.admit_url("https://example.com/100%off")
    assert admitted.path_and_query == "/100%25off"


# ---------------------------------------------------------------------------
# Local HTTPS server fixture
# ---------------------------------------------------------------------------


def _generate_cert(hostname: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def _generate_cert_with_ip_san(ip_literal: str):
    """Like `_generate_cert`, but with an `x509.IPAddress` SAN instead of a
    `DNSName` one -- required for `server_hostname=<IP literal>` to pass TLS
    verification at all (OpenSSL's `X509_check_ip`, not `X509_check_host`,
    is what runs for an IP-literal `server_hostname` since Python replaced
    the old `ssl.match_hostname` with OpenSSL's own hostname verification)."""
    import ipaddress as ip_mod

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip_literal)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_mod.ip_address(ip_literal))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 -- silence test server logging
        pass

    def do_POST(self):
        behavior = self.server.behavior
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        behavior(self)


def _local_https_server(hostname: str, behavior):
    """Start a background HTTPS server on 127.0.0.1:<ephemeral>, return
    (server, thread, port, cert_pem). Caller must call server.shutdown()."""
    key_pem, cert_pem = _generate_cert(hostname)
    import tempfile

    key_file = tempfile.NamedTemporaryFile(suffix=".key", delete=False)
    cert_file = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
    key_file.write(key_pem)
    cert_file.write(cert_pem)
    key_file.close()
    cert_file.close()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file.name, key_file.name)

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.behavior = behavior
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_port, cert_pem


@pytest.fixture()
def https_server():
    """Parametrized per-test via a factory closure -- yields a `make(behavior)`
    helper so each test controls exactly what the server does with a request."""
    servers = []

    def make(behavior):
        server, thread, port, cert_pem = _local_https_server(TEST_HOSTNAME, behavior)
        servers.append(server)
        return port, cert_pem

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def trust_test_ca(monkeypatch):
    """Monkeypatches ssl.create_default_context (as safe_http calls it) to
    return a context trusting the given cert_pem as its ONLY root -- so the
    client's real chain-of-trust verification actually runs against our
    throwaway per-test CA instead of being stubbed out.

    Returns a `apply(cert_pem)` setter so each test supplies its own server's
    certificate; safe_http's real `create_default_context` call is what gets
    intercepted, so ALPN restriction and TLS-version floor set on the
    returned context by `_make_ssl_context` are still exercised for real.
    """
    state: dict = {"cert_pem": None}
    real_create_default_context = ssl.create_default_context

    def fake_create_default_context(*args, **kwargs):
        context = real_create_default_context(*args, **kwargs)
        if state["cert_pem"] is not None:
            context.load_verify_locations(cadata=state["cert_pem"].decode("ascii"))
        return context

    monkeypatch.setattr(safe_http.ssl, "create_default_context", fake_create_default_context)

    def apply(cert_pem: bytes) -> None:
        state["cert_pem"] = cert_pem

    return apply


def _resolver_for(port_holder=None):
    """A resolver mapping TEST_HOSTNAME -> 127.0.0.1, everything else -> DNS
    failure (this module must never fall back to a real lookup for something
    it doesn't recognize in a test)."""

    def resolver(hostname: str) -> list[str]:
        if hostname == TEST_HOSTNAME:
            return ["127.0.0.1"]
        raise safe_http.DnsFailure("unexpected_hostname_in_test")

    return resolver


def _allow_all_policy(_ip: str) -> bool:
    """Test-only address_policy override: 127.0.0.1 (the local test server's
    only option without root) is genuinely disallowed by the real policy, so
    exercising the transport end-to-end against a local server has to bypass
    IT specifically -- not the resolver, not any real vetting logic, which
    stays exercised for real by the table-driven tests above."""
    return True


# ---------------------------------------------------------------------------
# End-to-end: challenge-style POST against a real local TLS server
# ---------------------------------------------------------------------------


def test_successful_post_round_trips_status_and_body(https_server, trust_test_ca):
    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")
    assert resp.status == 200
    assert resp.body == b"ok"


def test_redirect_is_a_failure_not_followed(https_server, trust_test_ca):
    def behavior(handler):
        handler.send_response(302)
        handler.send_header("Location", "https://attacker.example/steal")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.SsrfRejected):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")


def test_non_identity_encoding_is_rejected(https_server, trust_test_ca):
    def behavior(handler):
        body = b"\x1f\x8b" + b"x" * 20  # fake gzip magic, contents irrelevant
        handler.send_response(200)
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.TooLarge):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")


def test_response_over_64kb_decoded_is_rejected(https_server, trust_test_ca):
    def behavior(handler):
        body = b"a" * (safe_http.MAX_BODY_BYTES + 1024)
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.TooLarge):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")


def test_drip_feed_trips_the_total_wall_clock_budget_not_just_the_read_timeout(
    https_server, trust_test_ca, monkeypatch
):
    """A receiver that sends one byte well inside the per-read timeout,
    repeatedly, never trips a naive read timeout -- only the independent
    total-budget check does. Shrinks both the budget and the drip interval so
    the test runs fast while still proving the mechanism."""
    monkeypatch.setattr(safe_http, "TOTAL_BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(safe_http, "READ_TIMEOUT_SECONDS", 5.0)

    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "50")
        handler.end_headers()
        for _ in range(50):
            handler.wfile.write(b"a")
            handler.wfile.flush()
            time.sleep(0.05)

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.TimeoutFailure):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")


def test_proxy_env_vars_are_never_consulted(https_server, trust_test_ca, monkeypatch):
    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    # Point both proxy env vars at a closed port -- if this module consulted
    # them at all, the request would try to speak to a dead proxy and fail;
    # a direct connect (the required behaviour) ignores them entirely and
    # succeeds exactly as the previous test did.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{dead_port}")
    monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{dead_port}")

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Pin-vs-TOCTOU: a resolver that flips its answer between calls
# ---------------------------------------------------------------------------


def test_resolver_flip_between_calls_is_independently_revetted_each_time():
    """Simulates DNS rebinding: the first resolution returns a public address
    (passes vetting); by the very next call the same hostname resolves to a
    private one. Exercises `_resolve_and_vet` directly (no real socket/network
    involved -- this is purely about whether the SECOND call trusts anything
    from the first) to prove there is no cross-call caching of "this hostname
    was fine last time": every call must independently resolve and vet."""
    calls = {"n": 0}

    def flipping_resolver(hostname: str) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return ["8.8.8.8"]  # public -- passes vetting
        return ["127.0.0.1"]  # rebinds to loopback on the very next call

    first = safe_http._resolve_and_vet(TEST_HOSTNAME, flipping_resolver)
    assert first == "8.8.8.8"
    with pytest.raises(safe_http.SsrfRejected):
        safe_http._resolve_and_vet(TEST_HOSTNAME, flipping_resolver)


def test_no_connection_reuse_across_fetch_calls(https_server, trust_test_ca):
    """There is no pool: two calls to the same target open two independent
    sockets. Proven by counting accepted connections server-side."""
    accepted = {"n": 0}
    lock = threading.Lock()

    def behavior(handler):
        with lock:
            accepted["n"] += 1
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")
    client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")
    assert accepted["n"] == 2


# ---------------------------------------------------------------------------
# Fix #11: response headers are lowercased so a case-insensitive lookup like
# the dispatcher's `response.headers.get("retry-after")` actually finds a
# server that sent "Retry-After" (the conventional casing).
# ---------------------------------------------------------------------------


def test_response_headers_are_lowercased(https_server, trust_test_ca):
    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Retry-After", "30")
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")
    assert resp.headers.get("retry-after") == "30"
    assert "Retry-After" not in resp.headers


# ---------------------------------------------------------------------------
# Fix #12: the SSL socket, not the raw one, owns the fd once wrap_socket
# succeeds -- exactly one of them gets close()d, never both, never neither.
# ---------------------------------------------------------------------------


def test_ssl_socket_is_closed_exactly_once_after_a_successful_wrap(https_server, trust_test_ca, monkeypatch):
    """Instance-level `sock.close = ...` isn't possible (socket.socket's
    `close` is a read-only slot), so this identifies OUR client-side raw and
    SSL sockets by id() -- recorded at the moment `safe_http` itself creates
    them (via `socket.create_connection` and `ssl.SSLContext.wrap_socket`) --
    and patches `socket.socket.close` at the CLASS level (which `ssl.SSLSocket`
    inherits/uses too) to log which of those two specific objects actually
    gets close()d, regardless of what the local HTTPS test server's own
    sockets do concurrently in another thread."""
    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    ids: dict[int, str] = {}
    closed_order: list[str] = []

    real_create_connection = socket.create_connection

    def recording_create_connection(*a, **kw):
        raw = real_create_connection(*a, **kw)
        ids[id(raw)] = "raw"
        return raw

    monkeypatch.setattr(safe_http.socket, "create_connection", recording_create_connection)

    real_wrap_socket = ssl.SSLContext.wrap_socket

    def recording_wrap_socket(self, sock, *a, **kw):
        wrapped = real_wrap_socket(self, sock, *a, **kw)
        ids[id(wrapped)] = "ssl"
        return wrapped

    monkeypatch.setattr(safe_http.ssl.SSLContext, "wrap_socket", recording_wrap_socket)

    real_close = socket.socket.close

    def recording_close(self):
        label = ids.get(id(self))
        if label is not None:
            closed_order.append(label)
        return real_close(self)

    monkeypatch.setattr(socket.socket, "close", recording_close)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")
    assert resp.status == 200
    # `http.client.HTTPResponse` itself may also close the socket once as
    # part of finishing the read (harmless, idempotent) -- what this test
    # pins is that OUR `finally` block never touches the raw socket once
    # wrap_socket has succeeded, i.e. no leaked/never-closed raw fd and no
    # double-close racing a live raw reference.
    assert "raw" not in closed_order, "the raw socket must never be close()d once wrap_socket succeeded (fix #12)"
    assert "ssl" in closed_order


# ---------------------------------------------------------------------------
# Fix #13: DNS resolution is bounded by the wall-clock budget, not left to
# block on a slow/hostile resolver past it.
# ---------------------------------------------------------------------------


def test_dns_resolver_pool_is_bounded_across_many_concurrent_fetches():
    """Round-2 fix #12: no matter how many `fetch()` calls are in flight at
    once, the module-level DNS resolver pool never grows past
    `_DNS_POOL_MAX_WORKERS` live threads -- replacing the old "spawn one
    `threading.Thread` per call" scheme, which had no such cap."""
    release = threading.Event()

    def slow_resolver(hostname: str) -> list[str]:
        release.wait(5)
        # Short-circuits before any real network I/O -- this test only
        # cares about how many pool threads are alive while blocked, not
        # about what happens after resolution "succeeds".
        raise safe_http.DnsFailure("test_stop_here")

    client = safe_http.SafeHttpClient(resolver=slow_resolver, address_policy=_allow_all_policy)

    driver_threads = []
    for _ in range(safe_http._DNS_POOL_MAX_WORKERS + 4):
        t = threading.Thread(
            target=lambda: _swallow(lambda: client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")),
            daemon=True,
        )
        driver_threads.append(t)
        t.start()

    time.sleep(0.3)
    try:
        assert len(safe_http._dns_resolver_pool._threads) <= safe_http._DNS_POOL_MAX_WORKERS, (
            "the DNS resolver pool must never grow past its configured cap"
        )
    finally:
        release.set()
        for t in driver_threads:
            t.join(5)


def _swallow(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 -- driver thread, only the pool size assertion matters
        pass


def test_slow_resolver_trips_dns_failure_not_a_hang(monkeypatch):
    monkeypatch.setattr(safe_http, "TOTAL_BUDGET_SECONDS", 0.3)

    def hanging_resolver(hostname: str) -> list[str]:
        time.sleep(5)
        return ["8.8.8.8"]

    client = safe_http.SafeHttpClient(resolver=hanging_resolver, address_policy=_allow_all_policy)
    start = time.monotonic()
    with pytest.raises(safe_http.DnsFailure):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, "must fail near the budget, not wait out the hanging resolver"


# ---------------------------------------------------------------------------
# r12 fix #7 (codex MED): the DNS resolver pool's own work QUEUE has no
# size limit of its own (only the pool's `max_workers` does) -- a burst of
# slow/attacker-controlled-DNS targets could pile up unbounded queued work
# behind whatever is already running. `_resolve_and_vet_within_budget` now
# (1) bounds total outstanding (queued + running) work at `_DNS_BACKLOG_MAX`
# and (2) cancels a timed-out future so a still-QUEUED one never runs at all.
# ---------------------------------------------------------------------------


def test_dns_timeout_cancels_a_still_queued_future_and_frees_its_backlog_slot():
    release = threading.Event()

    def blocking_resolver(hostname: str) -> list[str]:
        release.wait(5)
        raise safe_http.DnsFailure("test_stop_here")

    # Saturate every pool worker so the call under test is GUARANTEED to
    # sit queued, never picked up by a worker, when it times out.
    blockers = [
        safe_http._dns_resolver_pool.submit(blocking_resolver, "blocker")
        for _ in range(safe_http._DNS_POOL_MAX_WORKERS)
    ]
    time.sleep(0.2)  # let the pool actually pick all of them up

    try:
        baseline = safe_http._dns_backlog_count
        with pytest.raises(safe_http.DnsFailure) as excinfo:
            safe_http._resolve_and_vet_within_budget(
                "queued-hostname", blocking_resolver, _allow_all_policy, timeout_seconds=0.1
            )
        assert excinfo.value.reason == "resolution_exceeded_budget"
        assert safe_http._dns_backlog_count == baseline, (
            "a timed-out, STILL-QUEUED resolution must free its backlog slot "
            "immediately -- it must never be left to run to completion "
            "pointlessly (its result is discarded either way) while holding "
            "a backlog slot"
        )
    finally:
        release.set()
        for f in blockers:
            _swallow(lambda: f.result(5))


def test_dns_backlog_bound_rejects_immediately_once_saturated(monkeypatch):
    """Once `_DNS_BACKLOG_MAX` resolutions are already outstanding, a
    further submission must be refused immediately -- not queued behind
    them without limit, and not left to wait out its own timeout."""
    monkeypatch.setattr(safe_http, "_DNS_BACKLOG_MAX", 2)
    release = threading.Event()
    results: list[Exception] = []

    def blocking_resolver(hostname: str) -> list[str]:
        release.wait(5)
        raise safe_http.DnsFailure("test_stop_here")

    def driver():
        try:
            safe_http._resolve_and_vet_within_budget(
                "backlog-hostname", blocking_resolver, _allow_all_policy, timeout_seconds=5.0
            )
        except Exception as exc:  # noqa: BLE001 -- collected, not asserted on
            results.append(exc)

    driver_threads = [threading.Thread(target=driver, daemon=True) for _ in range(2)]
    for t in driver_threads:
        t.start()
    time.sleep(0.3)  # let both actually reserve a backlog slot and start running

    try:
        start = time.monotonic()
        with pytest.raises(safe_http.DnsFailure) as excinfo:
            safe_http._resolve_and_vet_within_budget(
                "backlog-hostname-3", blocking_resolver, _allow_all_policy, timeout_seconds=5.0
            )
        elapsed = time.monotonic() - start
        assert excinfo.value.reason == "resolver_backlog_saturated"
        assert elapsed < 1.0, "a saturated backlog must fail FAST, not wait out its own timeout"
    finally:
        release.set()
        for t in driver_threads:
            t.join(5)


# ---------------------------------------------------------------------------
# Fix #14: IncompleteRead's partial bytes count toward the 64KB cap too.
# ---------------------------------------------------------------------------


def test_incomplete_read_over_cap_is_too_large_for_a_challenge(https_server, trust_test_ca):
    def behavior(handler):
        # Announce a body bigger than we'll actually send, then close early
        # -- triggers http.client.IncompleteRead with a large `.partial`.
        body = b"a" * (safe_http.MAX_BODY_BYTES + 4096)
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body) + 100))  # lie: promise more than we send
        handler.end_headers()
        handler.wfile.write(body)
        handler.close_connection = True

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.TooLarge):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}", require_body=True)


# ---------------------------------------------------------------------------
# Fix #15: a delivery (require_body=False) never fails on a body issue that
# only matters for a challenge -- oversize or non-identity encoding.
# ---------------------------------------------------------------------------


def test_delivery_does_not_error_on_oversized_body(https_server, trust_test_ca):
    def behavior(handler):
        body = b"a" * (safe_http.MAX_BODY_BYTES + 1024)
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}", require_body=False)
    assert resp.status == 200, "delivery success is status-only -- an oversized echoed body must not fail it"


def test_delivery_does_not_error_on_non_identity_encoding(https_server, trust_test_ca):
    def behavior(handler):
        body = b"\x1f\x8b" + b"x" * 20  # fake gzip magic, contents irrelevant
        handler.send_response(200)
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}", require_body=False)
    assert resp.status == 200, "an echoing receiver's gzip body must not fail a delivery (fix #15)"


def test_challenge_still_errors_on_oversized_or_encoded_body(https_server, trust_test_ca):
    """The require_body=True (default, challenge) path is UNCHANGED --
    still strict, since the challenge's response body is the whole point of
    the request."""
    def oversized(handler):
        body = b"a" * (safe_http.MAX_BODY_BYTES + 1024)
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    port, cert_pem = https_server(oversized)
    trust_test_ca(cert_pem)
    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.TooLarge):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")  # require_body defaults True


# ---------------------------------------------------------------------------
# Verify round-5 fix #9 (deepseek #2): a delivery's success is "2xx status
# alone" -- a body-READ failure (timeout, or the connection dropping) AFTER
# the status line + headers are already read must not turn a delivery that
# already succeeded at the HTTP level into a recorded failure. Only for
# require_body=False; a challenge (require_body=True, the strict default)
# keeps raising, tested by both companions below.
# ---------------------------------------------------------------------------


def test_delivery_swallows_a_body_read_timeout_as_success(https_server, trust_test_ca, monkeypatch):
    """Same drip-feed-past-the-budget mechanism as
    test_drip_feed_trips_the_total_wall_clock_budget_not_just_the_read_timeout
    (which proves the require_body=True/strict side already raises
    TimeoutFailure on this exact scenario) -- here with require_body=False,
    the identical drip must instead come back as an ordinary 2xx success."""
    monkeypatch.setattr(safe_http, "TOTAL_BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(safe_http, "READ_TIMEOUT_SECONDS", 5.0)

    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "50")
        handler.end_headers()
        for _ in range(50):
            handler.wfile.write(b"a")
            handler.wfile.flush()
            time.sleep(0.05)

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}", require_body=False)
    assert resp.status == 200, "a body-read timeout must not fail a delivery whose status was already 200 (fix #9)"
    assert resp.body is None, "the body was never fully read -- distinct from a genuinely empty b''"


def test_delivery_swallows_a_connection_reset_mid_body_as_success(https_server, trust_test_ca, monkeypatch):
    """Simulates a genuine OSError (ConnectionResetError) from
    `response.read` directly -- reliably forcing a real TLS socket to RST
    mid-body is timing-fragile (same reason
    test_malformed_http_response_is_transport_not_timeout patches
    `.begin()` instead of trying to trigger a real malformed response), so
    this patches the one call that reads the body to raise the exact
    exception class a real dropped connection would produce, while
    connect, TLS handshake, request send, and status-line+header parsing
    are all still exercised for real over a real socket."""
    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    import http.client

    def raising_read(self, amt=None):
        raise ConnectionResetError("simulated: peer reset the connection mid-body")

    monkeypatch.setattr(http.client.HTTPResponse, "read", raising_read)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    resp = client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}", require_body=False)
    assert resp.status == 200, "a dropped connection mid-body must not fail a delivery whose status was already 200 (fix #9)"
    assert resp.body is None


def test_challenge_still_raises_on_connection_reset_mid_body(https_server, trust_test_ca, monkeypatch):
    """The require_body=True (default, challenge) path is UNCHANGED by fix
    #9 -- a dropped connection mid-body is still a hard ConnectionFailure,
    since the challenge's response body is the entire point of the
    request. Same `.read()`-patching technique as the companion test above,
    for the same reliability reason."""
    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    import http.client

    def raising_read(self, amt=None):
        raise ConnectionResetError("simulated: peer reset the connection mid-body")

    monkeypatch.setattr(http.client.HTTPResponse, "read", raising_read)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.ConnectionFailure):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")  # require_body defaults True


# ---------------------------------------------------------------------------
# Production factory guard
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Round-3 fix #4: an IPv6-literal hostname must be RE-BRACKETED in the Host
# header (RFC 9110) -- the bare address alone is ambiguous with a
# Host:port separator.
# ---------------------------------------------------------------------------


def test_admit_url_accepts_and_returns_bare_ipv6_hostname():
    """`urlsplit.hostname` strips the "[...]" brackets -- `admit_url` returns
    the bare address, which is exactly why `_fetch`'s Host-header
    construction has to re-bracket it (see the test below)."""
    admitted = safe_http.admit_url("https://[2001:db8::1]/hook")
    assert admitted.hostname == "2001:db8::1"


def test_fetch_sends_a_bracketed_host_header_for_an_ipv6_target(trust_test_ca):
    """End-to-end: resolve an IPv6-literal HOSTNAME to the local test
    server's loopback address, and assert the Host header the server
    actually received is bracketed (RFC 9110) -- per the fix's own
    instruction, this is what "unit-test the built header dict" means in
    practice for a header this module builds inline inside `_fetch` rather
    than in a separately-callable function."""
    received = {}

    def behavior(handler):
        received["host"] = handler.headers.get("Host")
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    key_pem, cert_pem = _generate_cert_with_ip_san("2001:db8::1")
    import tempfile

    key_file = tempfile.NamedTemporaryFile(suffix=".key", delete=False)
    cert_file = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
    key_file.write(key_pem)
    cert_file.write(cert_pem)
    key_file.close()
    cert_file.close()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file.name, key_file.name)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.behavior = behavior
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_port = server.server_port
    trust_test_ca(cert_pem)

    def resolver(hostname: str) -> list[str]:
        assert hostname == "2001:db8::1"
        return ["127.0.0.1"]

    client = safe_http.SafeHttpClient(resolver=resolver, port=real_port, address_policy=_allow_all_policy)
    try:
        resp = client.fetch("https://[2001:db8::1]/hook", body=b"{}")
        assert resp.status == 200
        assert received["host"] == "[2001:db8::1]", (
            "the Host header must be re-bracketed (RFC 9110) -- a bare "
            "'2001:db8::1' is ambiguous with a Host:port separator"
        )
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Round-3 fix #9: an address the address_policy can't even PARSE (a
# ValueError from ipaddress.ip_address, e.g. a zone-id suffix) must be an
# SsrfRejected vetting failure, never surface as a retryable TransportFailure.
# ---------------------------------------------------------------------------


def test_unparseable_resolved_address_is_ssrf_rejected_not_transport_failure():
    def policy_that_raises(_ip: str) -> bool:
        raise ValueError("simulated: address this policy cannot parse")

    def resolver(hostname: str) -> list[str]:
        return ["fe80::1%eth0"]

    client = safe_http.SafeHttpClient(resolver=resolver, address_policy=policy_that_raises)
    with pytest.raises(safe_http.SsrfRejected):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")


def test_resolve_and_vet_wraps_policy_valueerror_as_ssrf_rejected():
    def policy_that_raises(_ip: str) -> bool:
        raise ValueError("simulated unparseable address")

    with pytest.raises(safe_http.SsrfRejected):
        safe_http._resolve_and_vet(TEST_HOSTNAME, lambda h: ["1.2.3.4"], policy_that_raises)


# ---------------------------------------------------------------------------
# Round-3 fix #15: a malformed HTTP response (bad status line / invalid
# chunking) is a 'transport' failure, not 'timeout' -- the receiver DID
# answer, just not intelligibly.
# ---------------------------------------------------------------------------


def test_malformed_http_response_is_transport_not_timeout(https_server, trust_test_ca, monkeypatch):
    """Simulates `http.client.HTTPException` at `response.begin()` directly
    -- reliably triggering a genuinely malformed status line/chunking over a
    real socket is timing-fragile, so this patches the one call site that
    parses the response to raise the same exception class real malformed
    input would produce, while everything else (connect, TLS handshake,
    request send) is exercised for real."""

    def behavior(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")

    port, cert_pem = https_server(behavior)
    trust_test_ca(cert_pem)

    import http.client

    real_begin = http.client.HTTPResponse.begin

    def raising_begin(self):
        raise http.client.BadStatusLine("garbage status line")

    monkeypatch.setattr(http.client.HTTPResponse, "begin", raising_begin)

    client = safe_http.SafeHttpClient(resolver=_resolver_for(), port=port, address_policy=_allow_all_policy)
    with pytest.raises(safe_http.TransportFailure):
        client.fetch(f"https://{TEST_HOSTNAME}/hook", body=b"{}")


def test_production_factory_has_the_guard_enabled():
    """`new_safe_http_client()` with no arguments -- the exact call the
    dispatcher makes -- must reject a loopback target end to end, proving the
    real getaddrinfo-backed resolver and the vetting are both live, not just
    importable."""
    client = safe_http.new_safe_http_client()
    with pytest.raises(safe_http.SsrfRejected):
        client.fetch("https://localhost/hook", body=b"{}")

