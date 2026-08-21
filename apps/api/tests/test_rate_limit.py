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
from concurrent.futures import ThreadPoolExecutor

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
    process by rotating source addresses.

    2026-08-21 bleed-stop: the sweep/window logic this guards moved out of
    `RateLimitMiddleware` and into `_FixedWindowCounter` (now one of four
    instances -- default/heavy x ip/subnet -- the middleware holds), so this
    exercises that class directly rather than the middleware wrapper.
    """
    from billcommons_api.rate_limit import _FixedWindowCounter

    now = [0.0]
    counter = _FixedWindowCounter(limit=10, window=60.0, clock=lambda: now[0])
    for i in range(500):
        counter.allow(f"198.51.100.{i % 256}-{i}")
    assert len(counter._buckets) == 500

    now[0] = 601.0  # every window has long since expired
    counter.allow("203.0.113.1")
    assert len(counter._buckets) == 1, f"stale buckets not reclaimed: {len(counter._buckets)} left"


def test_max_keys_cap_enforced_mid_burst_not_only_at_periodic_sweep():
    """Verify round fd9997c, finding #4: `_sweep` only runs once per
    window -- a burst of many distinct NEW keys arriving entirely WITHIN
    one window (a scraper rotating source addresses fast) can blow past
    any cap well before the next scheduled sweep. The cap must also be
    enforced at the point a genuinely new key is inserted."""
    from billcommons_api.rate_limit import _FixedWindowCounter

    now = [0.0]
    counter = _FixedWindowCounter(limit=10, window=60.0, clock=lambda: now[0], max_keys=5)
    for i in range(5):
        counter.allow(f"198.51.100.{i}")
    assert len(counter._buckets) == 5

    # Still well within the window (nothing stale for a periodic sweep to
    # reclaim) -- a 6th distinct key must not be allowed to grow the dict
    # past the cap.
    now[0] = 1.0
    counter.allow("198.51.100.99")
    assert len(counter._buckets) <= 5, (
        f"max_keys cap not enforced mid-burst: {len(counter._buckets)} keys tracked"
    )


def test_max_keys_cap_does_not_evict_a_still_valid_key_reused_within_the_window():
    """The capacity check only fires for a NEW key -- re-hitting an
    existing key that's still within its window must not itself trigger
    eviction of unrelated keys."""
    from billcommons_api.rate_limit import _FixedWindowCounter

    now = [0.0]
    counter = _FixedWindowCounter(limit=10, window=60.0, clock=lambda: now[0], max_keys=5)
    for i in range(5):
        counter.allow(f"198.51.100.{i}")
    now[0] = 1.0
    allowed, *_ = counter.allow("198.51.100.0")  # re-hit an EXISTING key
    assert allowed
    assert len(counter._buckets) == 5, "re-hitting an existing key must not evict siblings"


def test_max_keys_cap_evicts_only_the_oldest_slice_keeping_a_hot_keys_count():
    """Verify r5 (round b93690a), finding #3: hitting the cap must evict
    only the OLDEST ~10% of tracked keys (by window start), never
    clear() the whole dict -- a recently-active, in-window "hot" key must
    keep its accumulated count, not get silently reset to zero."""
    from billcommons_api.rate_limit import _FixedWindowCounter

    now = [0.0]
    counter = _FixedWindowCounter(limit=100, window=60.0, clock=lambda: now[0], max_keys=10)
    # 9 keys, all inserted at t=0 -- the OLDEST cohort.
    for i in range(9):
        counter.allow(f"198.51.100.{i}")

    # A 10th, "hot" key inserted later and hit several times (still well
    # within the window) -- its window START is more recent than the
    # first 9's.
    now[0] = 30.0
    for _ in range(5):
        counter.allow("203.0.113.1")
    assert counter._buckets["203.0.113.1"][1] == 5  # sanity: count accumulated

    # Now AT the cap (10 keys). A brand-new 11th key forces eviction.
    now[0] = 31.0
    counter.allow("203.0.113.2")

    # The hot key must have SURVIVED (it is not among the oldest) with its
    # count intact -- a clear-everything eviction would have reset it.
    assert "203.0.113.1" in counter._buckets, "hot key was evicted -- eviction is not oldest-first"
    assert counter._buckets["203.0.113.1"][1] == 5, "hot key's count must survive eviction"


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


def test_client_ip_prefers_public_x_real_ip_over_the_xff_walk():
    """The live Railway edge (probed 2026-08-06) strips client-supplied
    X-Forwarded-For and writes "<client>, <edge node>" -- the rightmost-
    public walk lands on the edge's OWN rotating address, keying every
    per-IP quota to shared infrastructure. X-Real-Ip arrives overwritten
    with the true client and a forged value never survives the edge, so a
    public X-Real-Ip wins over the walk."""
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {
        "x-real-ip": "203.0.113.7",
        "x-forwarded-for": "203.0.113.7, 152.233.30.104",
    }
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_ignores_non_public_x_real_ip_and_falls_back_to_the_walk():
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {
        "x-real-ip": "10.1.2.3",
        "x-forwarded-for": "203.0.113.7",
    }
    assert client_ip(request) == "203.0.113.7"


def test_client_ip_canonicalizes_a_bracketed_ipv6_x_real_ip():
    from unittest.mock import MagicMock

    from billcommons_api.rate_limit import client_ip

    request = MagicMock()
    request.headers = {"x-real-ip": "[2001:db8::1]", "x-forwarded-for": ""}
    assert client_ip(request) == "2001:db8::1"


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


def test_quota_bucket_strips_brackets_before_bucketing():
    """Verify r5 (round b93690a), finding #2 (muse): a bracketed literal
    ("[2001:db8::1]", the form some proxies/URLs write) is not itself a
    valid ip_address() argument -- unstripped, it fell through to the
    "return ip as-is" fallback, so the bracketed and unbracketed spellings
    of the SAME address landed in two DIFFERENT buckets."""
    from billcommons_api.rate_limit import quota_bucket

    assert quota_bucket("[2001:db8:1234:5678::1]") == quota_bucket("2001:db8:1234:5678::1")


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


# ---------------------------------------------------------------------------
# 2026-08-21 bleed-stop: a bulk scraper spread ~500 req/min across 4 AWS
# addresses (~125/min each), each individually under the previous single
# per-IP 300/minute bucket. Fix adds (1) a subnet-keyed second bucket that a
# request must ALSO pass, and (2) a tighter heavy-route tier for the
# specific bill-detail/search endpoints the scraper was enumerating.
# ---------------------------------------------------------------------------

SUBNET_TEST_LIMIT = 12  # 3 per IP x 4 IPs in one /24
HEAVY_TEST_LIMIT = 3
HEAVY_SUBNET_TEST_LIMIT = 100


@pytest.fixture()
def subnet_limited_client(monkeypatch):
    """Generous per-IP/heavy ceilings, a small SUBNET ceiling -- isolates the
    new subnet bucket from the pre-existing per-IP one."""
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_DEFAULT", "1000/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_SUBNET", f"{SUBNET_TEST_LIMIT}/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY", "1000/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY_SUBNET", "1000/minute")
    return TestClient(create_app())


@pytest.fixture()
def heavy_limited_client(monkeypatch):
    """Generous per-IP/subnet ceilings, a small HEAVY-route ceiling -- proves
    the heavy tier throttles the expensive routes without touching light
    ones from the same caller."""
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_DEFAULT", "1000/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_SUBNET", "1000/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY", f"{HEAVY_TEST_LIMIT}/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY_SUBNET", f"{HEAVY_SUBNET_TEST_LIMIT}/minute")
    return TestClient(create_app())


def test_four_ips_in_one_24_stay_under_per_ip_but_trip_the_shared_subnet_bucket(
    subnet_limited_client,
):
    """4 distinct IPs in ONE /24, each individually well under its own
    (1000/minute) per-IP ceiling, must still trip the shared SUBNET bucket
    once their combined traffic crosses it -- exactly the scraper shape (4
    AWS addresses rotating within small blocks) the old per-IP-only limiter
    never caught."""
    ips = [f"203.0.113.{n}" for n in (10, 11, 12, 13)]  # all inside 203.0.113.0/24
    rounds = SUBNET_TEST_LIMIT // len(ips)  # 3 full rounds = SUBNET_TEST_LIMIT requests exactly

    for _ in range(rounds):
        for ip in ips:
            res = subnet_limited_client.get(
                "/api/v1/jurisdictions", headers={"X-Forwarded-For": ip}
            )
            assert res.status_code == 200, "each IP is individually far under its own limit"

    # The 4th batch (13th request into the shared /24 bucket) must be refused.
    fourth_batch = [
        subnet_limited_client.get("/api/v1/jurisdictions", headers={"X-Forwarded-For": ip})
        for ip in ips
    ]
    statuses = [r.status_code for r in fourth_batch]
    assert 429 in statuses, "4 IPs sharing one /24 never tripped the subnet bucket"
    refused = next(r for r in fourth_batch if r.status_code == 429)
    assert refused.headers["Retry-After"]


def test_heavy_route_per_ip_limit_trips_while_light_route_still_passes(heavy_limited_client):
    """The heavy tier (bill-detail/search/list routes) must throttle tighter
    than the general default -- and must NOT bleed into unrelated light
    routes from the same caller."""
    ip = {"X-Forwarded-For": "203.0.113.201"}
    statuses = [
        heavy_limited_client.get("/api/v1/bills", headers=ip).status_code
        for _ in range(HEAVY_TEST_LIMIT + 3)
    ]
    assert 429 in statuses, "heavy-route per-IP tier never tripped"

    still_light = heavy_limited_client.get("/api/v1/jurisdictions", headers=ip)
    assert still_light.status_code == 200, (
        "a light route from the SAME IP must not be throttled by the heavy tier"
    )


def test_heavy_route_first_request_reports_the_binding_heavy_limit():
    """X-RateLimit-* must report the BINDING tier -- whichever bucket is the
    tightest constraint on this request -- not always the default tier's.
    On a heavy route's very first request (against the REAL, unmodified
    settings -- `limited_client` overrides the per-IP default below 60,
    which would confound this), the smallest-limit bucket is the heavy-IP
    one (60/minute default), so that's the Limit this response must
    advertise, even though the same request also passed (and decremented)
    the much larger default per-IP/subnet buckets."""
    client = TestClient(create_app())
    res = client.get("/api/v1/bills", headers={"X-Forwarded-For": "203.0.113.203"})
    assert res.status_code == 200
    assert res.headers["X-RateLimit-Limit"] == "60"


def test_is_heavy_route_matches_an_optional_trailing_slash():
    """A trailing slash must not let a caller dodge the heavy tier -- FastAPI
    (and a scraper trying to evade this) both treat
    "/api/v1/bills/123/full/" as the same route as without the slash."""
    from billcommons_api.rate_limit import _is_heavy_route

    for path in (
        "/api/v1/bills/",
        "/api/v1/bills/123/full/",
        "/api/v1/bills/123/versions/",
        "/api/v1/bills/123/compare/",
        "/api/v1/search/",
    ):
        assert _is_heavy_route(path), f"{path} (trailing slash) must still be heavy"


def test_heavy_route_trailing_slash_trips_the_heavy_tier(heavy_limited_client):
    """Integration-level check for the same trailing-slash equivalence: the
    heavy tier must throttle a trailing-slash path exactly like the
    slash-less one."""
    ip = {"X-Forwarded-For": "203.0.113.204"}
    statuses = [
        heavy_limited_client.get("/api/v1/bills/", headers=ip).status_code
        for _ in range(HEAVY_TEST_LIMIT + 3)
    ]
    assert 429 in statuses, "trailing-slash heavy route never tripped the heavy tier"


def test_trusted_client_bypasses_the_subnet_and_heavy_tiers_too(
    subnet_limited_client, monkeypatch
):
    """The trusted-internal bypass must clear ALL tiers, not just the
    original per-IP one -- our own server-side renderer must never 429 no
    matter how many tiers a future fix adds."""
    monkeypatch.setenv("BILLCOMMONS_INTERNAL_CLIENT_SECRET", "s3cret-shared-value")
    headers = {
        "X-Forwarded-For": "203.0.113.55",
        "x-billcommons-internal": "s3cret-shared-value",
    }
    statuses = [
        subnet_limited_client.get("/api/v1/bills", headers=headers).status_code
        for _ in range(SUBNET_TEST_LIMIT + 5)
    ]
    assert 429 not in statuses, "trusted client was throttled by the subnet/heavy tiers"


def test_retry_after_ignores_a_merely_exhausted_sibling_that_still_allowed_this_request():
    """Verify r6 (round 88e289c), finding #2 (codex/muse): with
    check-all-THEN-increment, every bucket passed to this function is a
    PEEK, and when it returns a binding (a 429), NOTHING commits -- not
    even a bucket that peeked "allowed". Here heavy-ip FAILED (short
    reset) while default-subnet merely peeked remaining == 0 on this same
    hypothetical hit (it would have allowed it, longer reset) -- since
    NOTHING commits when the request is denied, default-subnet's real,
    persisted state is untouched and it does NOT actually block the next
    attempt. Retry-After must be the FAILED bucket's own reset (5), not
    inflated by the merely-exhausted sibling's longer one (an earlier
    round's now-superseded behavior)."""
    from billcommons_api.rate_limit import _FixedWindowCounter, _retry_after_and_binding

    heavy_ip = _FixedWindowCounter(limit=1, window=60.0, clock=lambda: 0.0)
    default_subnet = _FixedWindowCounter(limit=1, window=60.0, clock=lambda: 0.0)

    # Synthesized exactly as `dispatch` assembles its `results` list:
    # (name, counter, allowed, retry_after, remaining, reset_in).
    results = [
        ("heavy-ip", heavy_ip, False, 5, 0, 5),               # FAILED, short reset
        ("default-subnet", default_subnet, True, 0, 0, 55),  # ALLOWED (peeked), exhausted, long reset
    ]
    retry_after, binding = _retry_after_and_binding(results)
    assert retry_after == 5, f"a merely-exhausted sibling must not inflate Retry-After, got {retry_after}"
    assert binding is not None and binding[0] == "heavy-ip"


def test_retry_after_and_binding_is_a_noop_when_nothing_failed():
    from billcommons_api.rate_limit import _FixedWindowCounter, _retry_after_and_binding

    counter = _FixedWindowCounter(limit=10, window=60.0, clock=lambda: 0.0)
    results = [("default-ip", counter, True, 0, 5, 30)]
    retry_after, binding = _retry_after_and_binding(results)
    assert retry_after == 0
    assert binding is None


def test_retry_after_ignores_a_bucket_with_remaining_greater_than_zero():
    """Verify r5 (round b93690a), finding #4 (codex): only buckets that
    actually FAILED or are sitting at remaining == 0 may contribute to
    Retry-After. A bucket that still has headroom (remaining > 0) -- even
    one with a much longer reset_in than the bucket that actually failed --
    must never inflate it."""
    from billcommons_api.rate_limit import _FixedWindowCounter, _retry_after_and_binding

    counter = _FixedWindowCounter(limit=10, window=60.0, clock=lambda: 0.0)
    results = [
        ("default-ip", counter, False, 5, 0, 5),  # FAILED, short reset
        # ALLOWED, plenty of headroom (remaining=7), but a much LONGER
        # reset_in -- this must be ignored, not used to inflate Retry-After.
        ("default-subnet", counter, True, 0, 7, 99),
    ]
    retry_after, binding = _retry_after_and_binding(results)
    assert retry_after == 5, f"a remaining>0 bucket must never inflate Retry-After, got {retry_after}"
    assert binding is not None and binding[0] == "default-ip"


def test_peek_reports_the_same_decision_allow_would_without_mutating_state():
    """Verify round 8155c04, finding #1: `peek` must predict exactly what
    `allow` would decide for a hit against the key's CURRENT state (i.e.
    `count + 1` vs. the limit -- the same math `allow` does after its own
    increment), without ever persisting that increment. A bucket already
    AT its limit (one real `allow` hit, limit 1) must have `peek` report
    the NEXT hit would be DENIED -- not "still allowed, just exhausted"
    (that was round d1357cd's narrower, off-by-one peek; superseded)."""
    from billcommons_api.rate_limit import _FixedWindowCounter

    now = [0.0]
    counter = _FixedWindowCounter(limit=1, window=60.0, clock=lambda: now[0])
    counter.allow("k")  # count=1, AT the limit

    allowed, retry_after, remaining, reset_in = counter.peek("k")
    assert (allowed, retry_after, remaining, reset_in) == (False, 60, 0, 60)

    # A second peek must report the identical thing -- it must not have
    # mutated anything.
    assert counter.peek("k") == (False, 60, 0, 60)

    # A REAL allow() call now (still the same window) ALSO denies -- proving
    # peek's prediction matched allow's actual decision.
    allowed3, *_ = counter.allow("k")
    assert allowed3 is False


def test_peek_on_a_never_seen_key_matches_what_allow_would_return():
    """`peek` on a fresh key must return EXACTLY what `allow` would (a
    dry run, not a "fully available, no hit simulated" report) -- remaining
    already reflects the hypothetical hit, same as `allow`'s own return."""
    from billcommons_api.rate_limit import _FixedWindowCounter

    counter = _FixedWindowCounter(limit=10, window=60.0, clock=lambda: 0.0)
    allowed, retry_after, remaining, reset_in = counter.peek("never-seen")
    assert allowed is True
    assert retry_after == 0
    assert remaining == 9, "remaining must reflect the HYPOTHETICAL hit, like allow() would"
    assert reset_in == 60
    # Still never seen by allow() -- peek must not have inserted it.
    assert "never-seen" not in counter._buckets


def test_retry_after_scenario_default_ip_fails_short_while_heavy_subnet_fails_long():
    """Verify round 8155c04, finding #1+#2, the exact scenario named:
    default-IP fails with 5s left on its own window; heavy-subnet -- a
    SEPARATE, already-saturated bucket with 30s left on ITS window (started
    later, so more time remains) -- ALSO fails this same hit (peek's
    corrected count+1 math: an already-full bucket always fails the next
    hit, not "still allows it, just exhausted" -- see the peek test above)
    -- Retry-After must be 30, the longer of the two, via the REAL
    `allow`/`peek` primitives (not just synthesized tuples) with a
    controlled clock."""
    from billcommons_api.rate_limit import _FixedWindowCounter, _retry_after_and_binding

    now = [0.0]
    clock = lambda: now[0]

    default_ip = _FixedWindowCounter(limit=1, window=60.0, clock=clock)
    heavy_subnet = _FixedWindowCounter(limit=1, window=60.0, clock=clock)

    # default-ip's window starts at t=0 with its only slot used.
    default_ip.allow("1.2.3.4")

    # heavy-subnet's window starts 25s later, with its only slot used --
    # started LATER than default-ip's, so more time remains on it at any
    # shared "now" from here on.
    now[0] = 25.0
    heavy_subnet.allow("1.2.3.0/24")

    # 30s after THAT (t=55): default-ip's window (started t=0) has 5s left;
    # heavy-subnet's window (started t=25) has 30s left.
    now[0] = 55.0
    ip_result = ("default-ip", default_ip, *default_ip.peek("1.2.3.4"))  # already full -- FAILS
    assert ip_result[2] is False, "sanity: default-ip must fail this hit"
    assert ip_result[5] == 5, f"expected default-ip to have 5s left, got {ip_result[5]}"

    subnet_result = ("heavy-subnet", heavy_subnet, *heavy_subnet.peek("1.2.3.0/24"))
    assert subnet_result[2] is False, "sanity: heavy-subnet (already full) must also fail this hit"
    assert subnet_result[5] == 30, f"expected heavy-subnet to have 30s left, got {subnet_result[5]}"

    retry_after, binding = _retry_after_and_binding([ip_result, subnet_result])
    assert retry_after == 30, f"Retry-After must cover the longer-resetting failed bucket, got {retry_after}"
    assert binding is not None and binding[0] == "heavy-subnet"



def test_per_ip_denial_does_not_consume_the_shared_subnet_budget_for_a_sibling_ip(monkeypatch):
    """Verify round fd9997c, finding #6: per-IP buckets are checked FIRST,
    and the shared subnet buckets are only touched at all if every
    applicable per-IP bucket allows the request. One IP hammering far past
    its OWN per-IP ceiling must not ALSO burn down the shared /24 budget a
    sibling IP on the same block depends on."""
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_DEFAULT", "5/minute")
    # Subnet ceiling has room for exactly the hammer IP's 5 legitimate
    # successes PLUS the sibling's one request (6) -- if the hammering IP's
    # 25 DENIED requests also consumed subnet quota (the bug this guards),
    # the subnet bucket would already be exhausted well before the sibling
    # ever gets a turn.
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_SUBNET", "6/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY", "1000/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY_SUBNET", "1000/minute")
    client = TestClient(create_app())

    hammer_ip = {"X-Forwarded-For": "203.0.113.30"}  # same /24 as the sibling below
    statuses = [
        client.get("/api/v1/jurisdictions", headers=hammer_ip).status_code
        for _ in range(30)  # far past the 5/minute per-IP ceiling
    ]
    assert statuses.count(200) == 5, "sanity: exactly the per-IP ceiling's worth should succeed"
    assert 429 in statuses

    sibling_ip = {"X-Forwarded-For": "203.0.113.31"}  # same /24, untouched so far
    sibling = client.get("/api/v1/jurisdictions", headers=sibling_ip)
    assert sibling.status_code == 200, (
        "a sibling IP on the same /24 was throttled -- the hammering IP's "
        "DENIED requests must not have consumed the shared subnet budget, "
        "only its 5 successes should have"
    )


def test_concurrent_requests_never_exceed_the_limit_check_all_then_increment_is_atomic(
    monkeypatch,
):
    """Verify r5 (round b93690a), finding #1: the peek-all-then-increment-
    all sequence in `dispatch` must run under ONE lock spanning every
    counter it touches. Without it, two concurrent requests can both PEEK
    "allowed" against the same counter(s) (neither has committed yet) and
    both then commit, letting more than `limit` through. 50 concurrent
    requests against a 10/minute limit must yield EXACTLY 10 successes,
    every single run -- not "usually 10, occasionally 11+"."""
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_DEFAULT", "10/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_SUBNET", "1000/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY", "1000/minute")
    monkeypatch.setenv("BILLCOMMONS_API_RATE_LIMIT_HEAVY_SUBNET", "1000/minute")
    client = TestClient(create_app())
    headers = {"X-Forwarded-For": "203.0.113.77"}

    def fire(_):
        return client.get("/api/v1/jurisdictions", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=25) as pool:
        statuses = list(pool.map(fire, range(50)))

    allowed = statuses.count(200)
    assert allowed == 10, (
        f"expected EXACTLY 10 allowed under concurrent hammering, got {allowed} "
        f"-- the peek-then-commit sequence let more than the limit through"
    )
    assert statuses.count(429) == 40


def test_429_body_points_at_bulk_access(limited_client):
    """A scraper hitting the limiter should be told where the door to bulk
    access is, not just refused. Same house envelope every other error uses
    (see errors.py's ErrorResponse -- {"error": {"code", "message",
    "request_id", ...}}), with retry_after/docs added alongside."""
    headers = {"X-Forwarded-For": "203.0.113.202"}
    responses = [
        limited_client.get("/api/v1/jurisdictions", headers=headers)
        for _ in range(TEST_LIMIT + 3)
    ]
    refused = next(r for r in responses if r.status_code == 429)
    body = refused.json()["error"]
    assert body["code"] == "rate_limited"
    assert body["docs"] == "https://billcommons.org/docs/bulk"
    assert "billcommons.org/docs/bulk" in body["message"]
    assert body["retry_after"] == int(refused.headers["Retry-After"])


def test_429_headers_and_body_agree_on_retry_after_one_source_of_truth(limited_client):
    """Verify round 8155c04, finding #2: Retry-After, X-RateLimit-Reset, and
    the body's retry_after must all be the SAME number -- one source of
    truth for how long a client should wait, not three independently
    computed values that can drift apart."""
    headers = {"X-Forwarded-For": "203.0.113.205"}
    responses = [
        limited_client.get("/api/v1/jurisdictions", headers=headers)
        for _ in range(TEST_LIMIT + 3)
    ]
    refused = next(r for r in responses if r.status_code == 429)
    assert refused.headers["X-RateLimit-Reset"] == refused.headers["Retry-After"]
    assert refused.json()["error"]["retry_after"] == int(refused.headers["Retry-After"])
    assert refused.headers["X-RateLimit-Remaining"] == "0"


def test_retry_after_rounds_up_a_fractional_window_remainder_not_down():
    """Verify round 8155c04, finding #2 (codex): an int() TRUNCATION of a
    fractional remaining-window would tell a client to retry a fraction of
    a second BEFORE the window has actually rolled over -- landing it
    right back in a second refusal. `math.ceil` must round UP instead."""
    from billcommons_api.rate_limit import _FixedWindowCounter

    now = [0.1]  # 0.1s already elapsed when the window "starts" for this key
    counter = _FixedWindowCounter(limit=1, window=60.0, clock=lambda: now[0])
    counter.allow("k")  # count=1, at the limit; window "started" at t=0.1

    now[0] = 0.2  # 0.1s further elapsed -- 59.9s of the window remain
    allowed, retry_after, remaining, reset_in = counter.peek("k")
    assert allowed is False
    # int(59.9) truncates to 59 (retrying at t=59.2 would be BEFORE the
    # window actually rolls over at t=60.1); ceil(59.9) is 60, the correct,
    # never-premature answer.
    assert reset_in == 60, f"expected ceil'd 60, got {reset_in} (int() truncation regressed)"
    assert retry_after == 60



def test_subnet_bucket_collapses_ipv6_addresses_in_the_same_48_to_one_key():
    from billcommons_api.rate_limit import subnet_bucket

    same_48 = {
        subnet_bucket("2001:db8:1234:8888::1"),
        subnet_bucket("2001:db8:1234:9999::1"),
    }
    assert len(same_48) == 1, f"addresses in the same /48 must share one subnet bucket: {same_48}"


def test_subnet_bucket_keeps_distinct_48s_apart():
    from billcommons_api.rate_limit import subnet_bucket

    assert subnet_bucket("2001:db8:1234::1") != subnet_bucket("2001:db9:5678::1")


def test_subnet_bucket_collapses_ipv4_addresses_in_the_same_24_to_one_key():
    from billcommons_api.rate_limit import subnet_bucket

    same_24 = {subnet_bucket(f"203.0.113.{n}") for n in (1, 2, 254)}
    assert len(same_24) == 1, f"addresses in the same /24 must share one subnet bucket: {same_24}"


def test_subnet_bucket_keeps_distinct_24s_apart():
    from billcommons_api.rate_limit import subnet_bucket

    assert subnet_bucket("203.0.113.1") != subnet_bucket("198.51.100.1")


def test_subnet_bucket_strips_a_zone_id_instead_of_raising():
    """Verify round 8155c04, finding #6 (deepseek): a zone id (RFC 4007,
    "%eth0") parses fine on an ADDRESS, but building an "ip_network" string
    from a literal with an EMPTY zone id ("fe80::1%") raises ValueError --
    uncaught, that was a 500 from a single malformed header value. Must be
    stripped before building the network string, not merely tolerated by
    accident for the zone ids that happen not to trip it."""
    from billcommons_api.rate_limit import subnet_bucket

    # The crashing case: a bare trailing "%" with nothing after it.
    assert subnet_bucket("fe80::1%") == "fe80::"

    # A normal, non-empty zone id must still bucket sanely (and the same
    # as the same address without one).
    assert subnet_bucket("fe80::1%eth0") == subnet_bucket("fe80::1")


def test_subnet_bucket_zone_id_does_not_change_which_48_it_lands_in():
    from billcommons_api.rate_limit import subnet_bucket

    assert subnet_bucket("2001:db8:1234::1%eth0") == subnet_bucket("2001:db8:1234::1")


def test_subnet_bucket_strips_brackets_before_bucketing():
    """Same finding as quota_bucket's own bracket-stripping test above --
    the bracketed and unbracketed spellings of the same address must
    collapse to the SAME subnet bucket."""
    from billcommons_api.rate_limit import subnet_bucket

    assert subnet_bucket("[2001:db8:1234:5678::1]") == subnet_bucket("2001:db8:1234:5678::1")


def test_quota_bucket_strips_zone_id_instead_of_raising():
    """Verify r6 (round 88e289c), finding #1 (codex/muse): same zone-id
    fix as subnet_bucket's own -- an empty zone id ("fe80::1%") must not
    raise building the /64 network string."""
    from billcommons_api.rate_limit import quota_bucket

    assert quota_bucket("fe80::1%") == quota_bucket("fe80::1")
    assert quota_bucket("fe80::1%eth0") == quota_bucket("fe80::1")


def test_quota_bucket_never_raises_on_malformed_input():
    """`quota_bucket` must fall back to the raw string, never raise, for
    anything that fails to parse -- garbage, an empty-zone-id literal
    missing its brackets, whatever."""
    from billcommons_api.rate_limit import quota_bucket

    for garbage in ("not-an-ip", "[fe80::1", "2001:db8:zzzz::1", "999.999.999.999", "1.2.3"):
        assert quota_bucket(garbage) == garbage  # falls back to raw string, no exception


def test_subnet_bucket_never_raises_on_malformed_input():
    from billcommons_api.rate_limit import subnet_bucket

    for garbage in ("not-an-ip", "[fe80::1", "2001:db8:zzzz::1", "999.999.999.999", "1.2.3"):
        assert subnet_bucket(garbage) == garbage


def test_zone_scoped_and_bracketed_x_real_ip_never_500s(limited_client):
    """Verify r6 (round 88e289c), finding #1: an end-to-end round trip
    through the real middleware (client_ip -> quota_bucket -> subnet_bucket)
    for a zone-scoped or bracketed X-Real-Ip must return an ordinary
    200/429 -- never a 500. Both of these are non-public (link-local /
    loopback), so `client_ip` falls through to the socket peer rather than
    treating either as the caller's own address -- the crash-safety this
    guards is in `quota_bucket`/`subnet_bucket` themselves, which remain
    directly-callable, independently-tested utilities regardless of what
    `client_ip` ends up handing them."""
    for header_value in ("fe80::1%eth0", "[::1]"):
        res = limited_client.get(
            "/api/v1/jurisdictions", headers={"X-Real-Ip": header_value}
        )
        assert res.status_code in (200, 429), (
            f"X-Real-Ip: {header_value} produced {res.status_code}, expected 200 or 429"
        )


def test_rate_limiter_shares_one_subnet_bucket_across_a_routed_48(subnet_limited_client):
    """The subnet bucket itself must key on the /48-collapsed value for
    IPv6 -- two addresses in the same /48 (but different /64s) share one
    subnet bucket, same as the /24 case for IPv4."""
    rounds = SUBNET_TEST_LIMIT  # exhaust the shared /48 bucket from one address
    for _ in range(rounds):
        subnet_limited_client.get(
            "/api/v1/jurisdictions", headers={"X-Forwarded-For": "2001:db8:aaaa:1111::1"}
        )
    refused = subnet_limited_client.get(
        "/api/v1/jurisdictions", headers={"X-Forwarded-For": "2001:db8:aaaa:2222::1"}
    )
    assert refused.status_code == 429, "a different /64 in the SAME /48 must share the bucket"
