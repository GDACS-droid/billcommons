"""The public API advertises a per-IP rate limit (docs + methodology page).
This proves the limiter is actually *enforced* globally, not just configured —
the class of bug where an advertised control silently does nothing.

These tests pin their OWN small limit rather than firing `production limit + 5`
requests. Coupling them to the real number made them both slow and wrong: when
the ceiling rose from 60 to 300/minute, issuing 305 live requests took longer
than the 60-second window, so the window rolled over, nothing was ever refused,
and the suite reported the limiter as a no-op while it was working correctly.
What matters is that the mechanism refuses traffic beyond whatever the limit
is, and that buckets are per-IP — neither claim depends on the number.
"""
import pytest
from fastapi.testclient import TestClient

from billcommons_api.app import create_app
from billcommons_api.settings import get_settings

TEST_LIMIT = 5


@pytest.fixture()
def limited_client(monkeypatch):
    """An app whose rate limit is small enough to exercise in a few requests."""
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_DEFAULT", f"{TEST_LIMIT}/minute")
    return TestClient(create_app())


def test_global_rate_limit_is_enforced_per_ip(limited_client):
    headers = {"X-Forwarded-For": "203.0.113.42"}
    statuses = [
        limited_client.get("/api/v1/jurisdictions", headers=headers).status_code
        for _ in range(TEST_LIMIT + 3)
    ]
    assert 429 in statuses, "advertised rate limit never triggered — control is a no-op"
    # Everything up to the limit must have been allowed: a limiter that refuses
    # traffic too early is its own bug.
    assert statuses[: TEST_LIMIT - 1] == [200] * (TEST_LIMIT - 1)


def test_distinct_ips_are_limited_independently(limited_client):
    """One noisy client must not be able to lock everyone else out."""
    for _ in range(TEST_LIMIT + 3):
        limited_client.get(
            "/api/v1/jurisdictions", headers={"X-Forwarded-For": "203.0.113.1"}
        )
    fresh = limited_client.get(
        "/api/v1/jurisdictions", headers={"X-Forwarded-For": "198.51.100.7"}
    )
    assert fresh.status_code == 200


def test_the_configured_limit_is_the_one_actually_applied(limited_client):
    """Guards the seam the tests above rely on. If create_app stopped reading
    the setting, they would still pass against a hardcoded default while the
    advertised number meant nothing — exactly the no-op this file exists to
    catch, one level up."""
    assert get_settings().rate_limit_default == f"{TEST_LIMIT}/minute"
    headers = {"X-Forwarded-For": "203.0.113.99"}
    allowed = sum(
        limited_client.get("/api/v1/jurisdictions", headers=headers).status_code == 200
        for _ in range(TEST_LIMIT + 3)
    )
    assert allowed == TEST_LIMIT, f"expected exactly {TEST_LIMIT} allowed, got {allowed}"


def test_rate_limit_headers_are_advertised_on_every_response(limited_client):
    """A consumer reported being unable to confirm the limit without hitting
    it. Discovering your budget by getting throttled is not a design; an
    integrator sizing a nightly sync needs the numbers up front."""
    headers = {"X-Forwarded-For": "203.0.113.55"}
    res = limited_client.get("/api/v1/jurisdictions", headers=headers)
    assert res.status_code == 200
    assert res.headers["X-RateLimit-Limit"] == str(TEST_LIMIT)
    assert res.headers["X-RateLimit-Remaining"] == str(TEST_LIMIT - 1)
    assert int(res.headers["X-RateLimit-Reset"]) > 0


def test_remaining_counts_down_and_bottoms_out_at_zero(limited_client):
    headers = {"X-Forwarded-For": "203.0.113.56"}
    seen = [
        int(limited_client.get("/api/v1/jurisdictions", headers=headers).headers["X-RateLimit-Remaining"])
        for _ in range(TEST_LIMIT + 2)
    ]
    assert seen[0] == TEST_LIMIT - 1
    assert seen == sorted(seen, reverse=True), "remaining must never increase within a window"
    assert seen[-1] == 0
    # The 429 itself must still carry the headers -- that is the response a
    # client most needs them on.
    refused = limited_client.get("/api/v1/jurisdictions", headers=headers)
    assert refused.status_code == 429
    assert refused.headers["X-RateLimit-Remaining"] == "0"
    assert refused.headers["Retry-After"]


def test_expired_buckets_are_reclaimed():
    """The bucket dict grew once per distinct client IP and was never
    reclaimed -- an unbounded memory leak, and a cheap way to exhaust the
    process by rotating source addresses."""
    from billcommons_api.rate_limit import RateLimitMiddleware

    now = [0.0]
    mw = RateLimitMiddleware(None, limit=10, window=60.0, clock=lambda: now[0])
    for i in range(500):
        mw._allow(f"198.51.100.{i % 256}-{i}")
    assert len(mw._buckets) == 500

    now[0] = 601.0  # every window has long since expired
    mw._allow("203.0.113.1")
    assert len(mw._buckets) == 1, f"stale buckets not reclaimed: {len(mw._buckets)} left"


def test_trusted_internal_client_bypasses_the_limit(limited_client, monkeypatch):
    """Our own server-side renderer must not be throttled by its own visitors.

    The website is server-rendered, so every page view reaches the API from one
    of a handful of Vercel egress addresses -- and the limiter keys on that
    address. Without a bypass the entire public site shares ONE bucket, so at
    7-10 API calls per bill page the whole site is capped at ~30-43 pages per
    minute and visitors 429 each other.
    """
    monkeypatch.setenv("BILLCOMMONS_INTERNAL_CLIENT_SECRET", "s3cret-shared-value")
    headers = {
        "X-Forwarded-For": "203.0.113.99",
        "x-billcommons-internal": "s3cret-shared-value",
    }
    statuses = [
        limited_client.get("/api/v1/jurisdictions", headers=headers).status_code
        for _ in range(TEST_LIMIT + 5)
    ]
    assert 429 not in statuses, "trusted internal client was throttled"


def test_wrong_or_missing_secret_is_still_throttled(limited_client, monkeypatch):
    """The bypass must fail CLOSED.

    A blank secret, a missing header, or a wrong value must all be treated as
    ordinary public traffic -- otherwise a misconfigured deploy silently
    removes the rate limit for everyone.
    """
    monkeypatch.setenv("BILLCOMMONS_INTERNAL_CLIENT_SECRET", "s3cret-shared-value")
    wrong = {
        "X-Forwarded-For": "203.0.113.100",
        "x-billcommons-internal": "not-the-secret",
    }
    statuses = [
        limited_client.get("/api/v1/jurisdictions", headers=wrong).status_code
        for _ in range(TEST_LIMIT + 3)
    ]
    assert 429 in statuses, "a wrong secret bypassed the limiter"


def test_bypass_is_inert_when_no_secret_is_configured(limited_client, monkeypatch):
    """With no secret set, presenting the header must grant nothing."""
    monkeypatch.delenv("BILLCOMMONS_INTERNAL_CLIENT_SECRET", raising=False)
    headers = {
        "X-Forwarded-For": "203.0.113.101",
        "x-billcommons-internal": "",
    }
    statuses = [
        limited_client.get("/api/v1/jurisdictions", headers=headers).status_code
        for _ in range(TEST_LIMIT + 3)
    ]
    assert 429 in statuses, "empty secret + empty header bypassed the limiter"


# ---------------------------------------------------------------------------
# Round-7 fix #1: client_ip walks X-Forwarded-For from the RIGHT and skips
# any infra hop (private/RFC1918, CGNAT 100.64/10, loopback, link-local, or
# unparseable), returning the first PUBLIC entry found. The literal-rightmost
# rule (round-3 fix #6) only holds with exactly one appending proxy hop; a
# second hop (e.g. an internal CGNAT-addressed load balancer) pins every
# request to that hop's own fixed address, collapsing the per-IP limiter and
# the webhooks per-IP creation quota to a single global bucket.
# ---------------------------------------------------------------------------


def test_client_ip_single_hop_returns_the_only_entry():
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {"x-forwarded-for": "203.0.113.7"}
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_strips_whitespace_around_the_matched_entry():
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {"x-forwarded-for": "9.9.9.9,  203.0.113.7  "}
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_skips_a_trailing_cgnat_infra_hop():
    """Two appending hops: Railway's edge appends the caller's real public
    IP, then an internal CGNAT-addressed load balancer appends its own
    address after that. The literal-rightmost entry (100.64.x.x) is
    infra-only -- the true caller is the public entry one hop to its left."""
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {"x-forwarded-for": "203.0.113.7, 100.64.3.9"}
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_skips_spoofed_leading_entries_and_returns_the_appended_real_ip():
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8, 203.0.113.7"}
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_falls_back_to_peer_when_every_entry_is_private():
    """A chain of nothing but infra hops (no public entry anywhere) must not
    be trusted as the caller's address -- fall back to the socket peer."""
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {"x-forwarded-for": "10.0.0.5, 172.16.0.1, 100.64.0.1"}
    request.client.host = "198.51.100.200"
    assert client_ip(request) == "198.51.100.200"


def test_client_ip_falls_back_to_peer_when_no_header_present():
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {}
    request.client.host = "198.51.100.201"
    assert client_ip(request) == "198.51.100.201"


def test_client_ip_canonicalizes_ipv6_bucket_key():
    """r11 fix #4 (muse U + grok E): the matched entry was returned
    verbatim. One address has many equally-valid spellings, and the
    bracketed form some proxies write ("[2001:db8::1]") isn't even a valid
    `ip_address()` literal -- each distinct spelling landed in its own
    bucket for what is really one caller."""
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    compact = MagicMock()
    compact.headers = {"x-forwarded-for": "2001:db8::1"}

    exploded = MagicMock()
    exploded.headers = {
        "x-forwarded-for": "2001:0db8:0000:0000:0000:0000:0000:0001"
    }

    bracketed = MagicMock()
    bracketed.headers = {"x-forwarded-for": "[2001:db8::1]"}

    keys = {client_ip(compact), client_ip(exploded), client_ip(bracketed)}
    assert len(keys) == 1, f"expected one shared bucket key, got {keys}"
    assert keys == {"2001:db8::1"}


def test_spoofed_leading_xff_entries_do_not_change_the_quota_bucket(limited_client):
    """A caller prepending fake entries ahead of its real address must land
    in the SAME per-IP bucket as a caller sending no forged entries at all
    -- both share their real, rightmost IP's bucket, so spoofing extra
    leading hops buys no extra quota."""
    real_ip = "203.0.113.88"
    spoofed_headers = {"X-Forwarded-For": f"1.2.3.4, 5.6.7.8, {real_ip}"}
    plain_headers = {"X-Forwarded-For": real_ip}

    # Exhaust the bucket via the SPOOFED header...
    for _ in range(TEST_LIMIT):
        limited_client.get("/api/v1/jurisdictions", headers=spoofed_headers)
    # ...then prove the PLAIN header (same real, rightmost IP) is already
    # throttled too -- they share one bucket, not two.
    refused = limited_client.get("/api/v1/jurisdictions", headers=plain_headers)
    assert refused.status_code == 429, (
        "a caller varying only its FORGED leading XFF entries must not get "
        "a fresh quota bucket per variation"
    )


def test_429_is_never_cacheable(limited_client):
    """A cached 429 is a self-inflicted outage that outlives its cause.

    Next's Data Cache is deployment-persistent and a CDN cache-everything rule
    would pin this refusal at the edge for every client behind it.
    """
    headers = {"X-Forwarded-For": "203.0.113.77"}
    responses = [
        limited_client.get("/api/v1/jurisdictions", headers=headers)
        for _ in range(TEST_LIMIT + 3)
    ]
    throttled = [r for r in responses if r.status_code == 429]
    assert throttled, "limiter never triggered — cannot assert on the 429"
    for r in throttled:
        assert r.headers.get("cache-control") == "no-store", (
            "429 is cacheable — an edge or Data Cache can pin the refusal"
        )


def test_is_public_unmaps_embedded_ipv4_forms():
    """Verify round-8 (agy HIGH): an IPv4-mapped/6to4/NAT64 IPv6 string
    parses as an IPv6Address, so the RFC1918/CGNAT checks were skipped and
    `is_loopback` is False (only ::1 is) -- "::ffff:10.0.0.1" counted as a
    PUBLIC hop. An attacker varying the embedded address got unlimited
    distinct quota buckets, and an internal proxy hop in mapped form would
    collapse every caller into one bucket. `_is_public` must judge the
    EMBEDDED IPv4 address (transport's own extractor) and refuse NAT64
    prefixes outright."""
    from billcommons_api.rate_limit import _is_public

    for private_form in (
        "::ffff:10.0.0.1",      # IPv4-mapped RFC1918
        "::ffff:127.0.0.1",     # IPv4-mapped loopback
        "::ffff:100.64.0.2",    # IPv4-mapped CGNAT (Railway's own peer range)
        "64:ff9b::7f00:1",      # NAT64 well-known prefix, embedded loopback
        "2002:0a00:0001::1",    # 6to4, embedded 10.0.0.1
    ):
        assert not _is_public(private_form), private_form
    assert _is_public("::ffff:8.8.8.8"), (
        "an embedded PUBLIC IPv4 must still count as public"
    )
    assert _is_public("2001:4860:4860::8888"), "plain public IPv6 must pass"


def test_is_public_rejects_multicast_and_other_non_global_reserved_space():
    """Verify round-9 fix #1: `_is_public` checked RFC1918/CGNAT/loopback/
    link-local explicitly but had no multicast (or other reserved-but-
    unenumerated) guard -- a spoofed XFF entry like "224.0.0.1" or
    "ff02::1" parsed clean, tripped none of the checks, and counted as
    PUBLIC, letting an attacker shard into its own quota bucket at will.
    """
    from billcommons_api.rate_limit import _is_public

    for non_public in (
        "224.0.0.1",             # IPv4 multicast (all-hosts)
        "239.255.255.250",       # IPv4 multicast (SSDP)
        "ff02::1",                # IPv6 link-local multicast (all-nodes)
        "ff05::1:3",              # IPv6 site-local multicast
        "0.0.0.0",                # IPv4 unspecified
        "::",                     # IPv6 unspecified
        "240.0.0.1",              # IPv4 reserved (Class E)
    ):
        assert not _is_public(non_public), non_public

    # This repo's own test suites stand in for "a real public caller IP"
    # using the RFC5737 IPv4 documentation ranges and the IPv6 documentation
    # range -- both must keep passing, or every test using them (and any
    # production caller whose XFF legitimately contains one) starts failing.
    for still_public in ("203.0.113.42", "198.51.100.7", "192.0.2.1", "2001:db8::1"):
        assert _is_public(still_public), still_public


def test_is_public_rejects_ula_and_ipv4_compatible_ipv6():
    """r11 fix #3 (finding A, claimed by all seven verify legs): ULA
    (fc00::/7) is IPv6's RFC1918 analogue -- an org's own internal space,
    never a real internet caller. The deprecated IPv4-compatible form
    ("::a.b.c.d") embeds an arbitrary v4 address the transport's own
    extractor does not unwrap. Neither tripped any prior check."""
    from billcommons_api.rate_limit import _is_public

    for non_public in ("fc00::1", "fd00::1", "::10.0.0.1", "::192.168.1.1"):
        assert not _is_public(non_public), non_public

    # The deliberate RFC5737/2001:db8 acceptance (see fix #3's comment) must
    # not regress from this change.
    for still_public in ("203.0.113.9", "2001:db8::1"):
        assert _is_public(still_public), still_public


def test_is_public_rejects_deprecated_site_local_and_benchmarking_and_protocol_ranges():
    """r12 fix #5 (opus 5): three more reserved-but-not-yet-enumerated
    ranges -- deprecated IPv6 site-local (fec0::/10), IPv4 RFC 2544
    benchmarking space (198.18.0.0/15), and IPv4 IETF protocol assignments
    (192.0.0.0/24, includes the NAT64/DNS64 discovery block) -- none of
    which trip any prior check."""
    from billcommons_api.rate_limit import _is_public

    for non_public in (
        "fec0::1", "fec0:0:0:1::1",   # deprecated IPv6 site-local
        "198.18.0.1", "198.19.255.254",  # RFC 2544 benchmarking
        "192.0.0.1", "192.0.0.8",  # IETF protocol assignments (incl. NAT64/DNS64 discovery)
    ):
        assert not _is_public(non_public), non_public

    # Must not regress anything already accepted.
    for still_public in ("203.0.113.9", "2001:db8::1", "198.51.100.7"):
        assert _is_public(still_public), still_public


# ---------------------------------------------------------------------------
# r12 fix #4 (opus 4, MED but gutting): `quota_bucket` collapses an IPv6
# caller to its /64 network for every per-IP QUOTA (the rate limiter's own
# bucket, and the webhooks router's per-IP creation quota / (host,
# creator_ip) unverified cap) -- a caller with a routed /64 can otherwise
# mint a fresh /128 per request and bypass every one of those caps.
# `client_ip` itself stays address-exact (see `quota_bucket`'s own comment).
# ---------------------------------------------------------------------------


def test_quota_bucket_collapses_ipv6_addresses_in_the_same_64_to_one_key():
    from billcommons_api.rate_limit import quota_bucket

    same_64 = {
        quota_bucket("2001:db8:1234:5678::1"),
        quota_bucket("2001:db8:1234:5678::ffff"),
        quota_bucket("2001:db8:1234:5678:aaaa:bbbb:cccc:dddd"),
    }
    assert len(same_64) == 1, f"addresses in the same /64 must share one bucket key: {same_64}"


def test_quota_bucket_keeps_distinct_64s_apart():
    from billcommons_api.rate_limit import quota_bucket

    assert quota_bucket("2001:db8:1234:5678::1") != quota_bucket("2001:db8:1234:5679::1")


def test_quota_bucket_leaves_ipv4_unchanged():
    from billcommons_api.rate_limit import quota_bucket

    for ip in ("203.0.113.9", "198.51.100.7", "192.0.2.1"):
        assert quota_bucket(ip) == ip


def test_quota_bucket_leaves_unparseable_input_unchanged():
    """`quota_bucket` is always applied to `client_ip(request)`'s OWN
    output, which is always a valid address or the "unknown" sentinel --
    this is a defensive no-op for anything that somehow fails to parse."""
    from billcommons_api.rate_limit import quota_bucket

    assert quota_bucket("unknown") == "unknown"


# ---------------------------------------------------------------------------
# r13 fix #2 (deepseek HIGH + opus MED): an IPv4-mapped IPv6 literal must
# bucket on its EMBEDDED v4 address, not collapse to the shared `::` /64
# every mapped address shares.
# ---------------------------------------------------------------------------


def test_quota_bucket_unwraps_ipv4_mapped_addresses():
    from billcommons_api.rate_limit import quota_bucket

    assert quota_bucket("::ffff:8.8.8.8") == "8.8.8.8"


def test_quota_bucket_keeps_distinct_mapped_v4s_apart():
    from billcommons_api.rate_limit import quota_bucket

    assert quota_bucket("::ffff:8.8.8.8") != quota_bucket("::ffff:8.8.4.4")


def test_quota_bucket_plain_ipv6_64_collapse_unchanged():
    """Non-mapped IPv6 addresses still collapse to their /64 exactly as
    before -- only the mapped/6to4/Teredo embedded-v4 forms take the new
    branch."""
    from billcommons_api.rate_limit import quota_bucket

    same_64 = {
        quota_bucket("2001:db8:1234:5678::1"),
        quota_bucket("2001:db8:1234:5678::ffff"),
    }
    assert len(same_64) == 1
    assert quota_bucket("2001:db8:1234:5678::1") != quota_bucket("2001:db8:1234:5679::1")


def test_quota_bucket_plain_ipv4_still_unchanged():
    from billcommons_api.rate_limit import quota_bucket

    assert quota_bucket("203.0.113.9") == "203.0.113.9"


def test_rate_limiter_shares_one_bucket_across_a_routed_64(limited_client):
    """The rate limiter itself must key on the /64-collapsed bucket, not
    the raw address -- two distinct IPv6 addresses in the SAME /64 must
    share one limiter bucket, exactly like two requests from the same IPv4
    caller do."""
    same_64_a = {"X-Forwarded-For": "2001:db8:1234:5678::1"}
    same_64_b = {"X-Forwarded-For": "2001:db8:1234:5678::2"}

    for _ in range(TEST_LIMIT):
        limited_client.get("/api/v1/jurisdictions", headers=same_64_a)
    refused = limited_client.get("/api/v1/jurisdictions", headers=same_64_b)
    assert refused.status_code == 429, (
        "two addresses in the same /64 must share the SAME rate-limit bucket"
    )


def test_rate_limiter_does_not_share_buckets_across_different_64s(limited_client):
    different_64 = {"X-Forwarded-For": "2001:db8:1234:9999::1"}
    for _ in range(TEST_LIMIT):
        limited_client.get(
            "/api/v1/jurisdictions", headers={"X-Forwarded-For": "2001:db8:1234:8888::1"}
        )
    fresh = limited_client.get("/api/v1/jurisdictions", headers=different_64)
    assert fresh.status_code == 200, "a DIFFERENT /64 must not share the exhausted bucket"
