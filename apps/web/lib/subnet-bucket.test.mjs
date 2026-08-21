// Run with: node --test apps/web/lib/subnet-bucket.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { canonicalIp, expandIPv6, subnetBucket } from "./subnet-bucket.mjs";

test("expandIPv6 expands a fully-compressed loopback", () => {
  assert.deepEqual(expandIPv6("::1"), ["0", "0", "0", "0", "0", "0", "0", "1"]);
});

test("expandIPv6 expands leading compression", () => {
  assert.deepEqual(
    expandIPv6("::db8:1234:1:0:0:0:1"),
    ["0", "db8", "1234", "1", "0", "0", "0", "1"]
  );
});

test("expandIPv6 expands trailing compression", () => {
  assert.deepEqual(
    expandIPv6("2001:db8:1234:1::1"),
    ["2001", "db8", "1234", "1", "0", "0", "0", "1"]
  );
});

test("expandIPv6 expands a fully-written address unchanged", () => {
  assert.deepEqual(
    expandIPv6("2001:0db8:1234:0001:0000:0000:0000:0001"),
    ["2001", "0db8", "1234", "0001", "0000", "0000", "0000", "0001"]
  );
});

test("expandIPv6 rejects more than one '::'", () => {
  assert.equal(expandIPv6("2001::db8::1"), null);
});

test("expandIPv6 rejects a wrong group count with no compression", () => {
  assert.equal(expandIPv6("2001:db8:1234:1"), null);
});

test("expandIPv6 rejects a lone leading colon", () => {
  // split(":").filter(Boolean) would otherwise silently drop the leading
  // empty string and still land on exactly 8 groups.
  assert.equal(expandIPv6(":1:2:3:4:5:6:7:8"), null);
});

test("expandIPv6 rejects a lone trailing colon", () => {
  assert.equal(expandIPv6("1:2:3:4:5:6:7:8:"), null);
});

test("expandIPv6 rejects a lone trailing colon adjacent to a '::'", () => {
  assert.equal(expandIPv6("1::2:"), null);
});

test("expandIPv6 rejects '::' that replaces zero hextets when already 8 groups", () => {
  // "::" must represent AT LEAST ONE elided all-zero group -- an address
  // that already spells out all 8 and still has a "::" is malformed.
  assert.equal(expandIPv6("1:2:3:4:5:6:7:8::"), null);
});

test("expandIPv6 still accepts a legitimate trailing '::' eliding exactly one group", () => {
  assert.deepEqual(
    expandIPv6("1:2:3:4:5:6:7::"),
    ["1", "2", "3", "4", "5", "6", "7", "0"]
  );
});

test("expandIPv6 strips a zone ID before parsing", () => {
  assert.deepEqual(
    expandIPv6("fe80::1%eth0"),
    ["fe80", "0", "0", "0", "0", "0", "0", "1"]
  );
});

test("expandIPv6 converts an embedded IPv4 tail to two hex groups", () => {
  assert.deepEqual(
    expandIPv6("::ffff:1.2.3.4"),
    ["0", "0", "0", "0", "0", "ffff", "102", "304"]
  );
});

test("expandIPv6 rejects a non-hex group", () => {
  assert.equal(expandIPv6("2001:db8:zzzz:1:0:0:0:1"), null);
});

test("expandIPv6 rejects an embedded IPv4 tail with an octet over 255", () => {
  assert.equal(expandIPv6("::ffff:1.2.3.999"), null);
});

test("subnetBucket: two compressed IPv6 addresses in the same /48 collapse to one key", () => {
  // The exact case the verify finding named: a naive split(":").length check
  // (the PREVIOUS implementation) never expanded "::" at all, so these two
  // -- same /48, different trailing group -- fell through to the
  // "leave as-is" branch and got two DIFFERENT bucket keys.
  const a = subnetBucket("2001:db8:1234:1::1");
  const b = subnetBucket("2001:db8:1234:2::1");
  assert.equal(a, b, `expected same bucket, got ${a} vs ${b}`);
});

test("subnetBucket: a genuinely different /48 gets a different key", () => {
  const a = subnetBucket("2001:db8:1234:1::1");
  const b = subnetBucket("2001:db9:5678:1::1");
  assert.notEqual(a, b);
});

test("subnetBucket: canonicalizes case and leading zeros to the same key", () => {
  const upperWithZeros = subnetBucket("2001:0DB8:1234::2");
  const lowerNoZeros = subnetBucket("2001:db8:1234::1");
  assert.equal(
    upperWithZeros,
    lowerNoZeros,
    `expected same /48 bucket regardless of case/leading zeros, got ${upperWithZeros} vs ${lowerNoZeros}`
  );
});

test("subnetBucket: bracketed IPv6 is unwrapped before bucketing", () => {
  const bracketed = subnetBucket("[2001:db8:1234:1::1]");
  const plain = subnetBucket("2001:db8:1234:1::1");
  assert.equal(bracketed, plain);
});

// ---------------------------------------------------------------------------
// Verify finding #1: embedded IPv4 forms must bucket on the EMBEDDED v4's
// /24, not a /48 derived from the wrapping IPv6 text -- otherwise a scraper
// (or just a dual-stack edge) varying the wrapping form mints a fresh
// "distinct" bucket per request for what is really one IPv4 caller.
// ---------------------------------------------------------------------------

test("subnetBucket: IPv4-mapped IPv6 buckets on the embedded v4's /24, not a /48", () => {
  const a = subnetBucket("::ffff:1.2.3.4");
  const b = subnetBucket("::ffff:1.2.3.5");
  assert.equal(a, "1.2.3.0/24");
  assert.equal(a, b, "same-/24 mapped addresses must share a bucket");
});

test("subnetBucket: IPv4-mapped addresses in different /24s stay apart", () => {
  const a = subnetBucket("::ffff:1.2.3.4");
  const b = subnetBucket("::ffff:9.9.9.9");
  assert.notEqual(a, b);
});

test("subnetBucket: IPv4-compatible (deprecated ::a.b.c.d) unwraps the same way", () => {
  assert.equal(subnetBucket("::1.2.3.4"), "1.2.3.0/24");
});

test("subnetBucket: 6to4 (2002:AABB:CCDD::/48) unwraps to the embedded v4's /24", () => {
  // 2002:c000:022d:: embeds 192.0.2.45 (RFC 3056 worked example).
  assert.equal(subnetBucket("2002:c000:022d::"), "192.0.2.0/24");
});

test("subnetBucket: Teredo (2001:0::/32) unwraps to the CLIENT v4's /24", () => {
  // 2001:0:4136:e378:8000:63bf:3fff:fdd2 is the standard Teredo worked
  // example (Wikipedia/RFC 4380); the obscured (XOR 0xffffffff) client v4
  // is 192.0.2.45.
  assert.equal(
    subnetBucket("2001:0:4136:e378:8000:63bf:3fff:fdd2"),
    "192.0.2.0/24"
  );
});

test("subnetBucket: NAT64 Well-Known Prefix (64:ff9b::/96) unwraps to the embedded v4's /24", () => {
  assert.equal(subnetBucket("64:ff9b::c000:22d"), "192.0.2.0/24");
});

test("subnetBucket: NAT64 Local-Use prefix (64:ff9b:1::/48) unwraps to the embedded v4's /24", () => {
  // Constructed per RFC 6052 section 2.2's PL=48 row to embed 192.0.2.45:
  // group3 = v4 hi16 (0xc000), group4's low byte + group5's high byte =
  // v4 lo16 (0x022d), with the reserved 'u' byte (group4's high byte) 0.
  assert.equal(subnetBucket("64:ff9b:1:c000:2:2d00::"), "192.0.2.0/24");
});

// ---------------------------------------------------------------------------
// IPv4 (verify finding #6): malformed input must return null so the caller
// (`clientSubnetKey`) skips rate limiting entirely, never guess a bucket.
// ---------------------------------------------------------------------------

test("subnetBucket: IPv4 collapses to its /24 network address", () => {
  assert.equal(subnetBucket("203.0.113.10"), "203.0.113.0/24");
  assert.equal(subnetBucket("203.0.113.10"), subnetBucket("203.0.113.254"));
});

test("subnetBucket: distinct IPv4 /24s stay apart", () => {
  assert.notEqual(subnetBucket("203.0.113.10"), subnetBucket("198.51.100.10"));
});

test("subnetBucket: rejects an IPv4 octet over 255", () => {
  assert.equal(subnetBucket("203.0.113.999"), null);
});

test("subnetBucket: rejects a non-numeric IPv4 octet", () => {
  assert.equal(subnetBucket("203.0.113.abc"), null);
});

test("subnetBucket: rejects the wrong number of IPv4 octets", () => {
  assert.equal(subnetBucket("203.0.113"), null);
});

test("subnetBucket: rejects a malformed IPv6 literal (non-hex group)", () => {
  assert.equal(subnetBucket("2001:db8:zzzz:1::1"), null);
});

test("subnetBucket: rejects a malformed IPv6 literal (double '::')", () => {
  assert.equal(subnetBucket("2001::db8::1"), null);
});

// ---------------------------------------------------------------------------
// canonicalIp
// ---------------------------------------------------------------------------

test("canonicalIp: bracketed, zone-id, and already-compressed spellings of the same address all match", () => {
  const bracketed = canonicalIp("[2001:DB8::1]");
  const zoned = canonicalIp("2001:db8::1%eth0");
  const explicitZeros = canonicalIp("2001:0db8:0:0::1");
  assert.equal(bracketed, zoned);
  assert.equal(zoned, explicitZeros);
  assert.equal(bracketed, "2001:db8::1");
});

test("canonicalIp: fully expanded and fully compressed spellings match", () => {
  assert.equal(
    canonicalIp("2001:0db8:0000:0000:0000:0000:0000:0001"),
    canonicalIp("2001:db8::1")
  );
});

test("canonicalIp: IPv4 is lowercased/trimmed but otherwise passed through", () => {
  assert.equal(canonicalIp("203.0.113.10"), "203.0.113.10");
});

test("canonicalIp: distinct IPv6 addresses stay distinct", () => {
  assert.notEqual(canonicalIp("2001:db8::1"), canonicalIp("2001:db8::2"));
});

test("canonicalIp: a bracketed IPv6 literal with a zone ID canonicalizes correctly", () => {
  assert.equal(canonicalIp("[2001:db8::1%eth0]"), "2001:db8::1");
});

test("canonicalIp: an unparseable IPv6-shaped input still returns a stable (lowercased) string, not a crash", () => {
  assert.equal(canonicalIp("2001::db8::1"), "2001::db8::1");
});
