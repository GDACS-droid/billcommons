// Pure helpers for collapsing a client IP to its containing subnet --
// shared by apps/web/middleware.ts (best-effort edge limiter) and its unit
// test (subnet-bucket.test.mjs). Plain ESM, no TypeScript: this needs to
// run directly under `node --test` with zero build step, and importing it
// (rather than duplicating the logic inline in middleware.ts) is what keeps
// the test honest -- it exercises the exact code the middleware runs, not a
// hand-copied stand-in that can silently drift from it.
//
// Mirrors billcommons_api.rate_limit.subnet_bucket: /24 for IPv4, /48 for
// IPv6, with the SAME embedded-IPv4 unwrapping (IPv4-mapped, IPv4-compatible,
// 6to4, Teredo, NAT64) so a caller cannot mint a fresh "distinct" IPv6
// bucket per request just by varying which embedding form it's spelled in.
//
// Every parser here returns `null` on anything that fails to parse --
// callers must treat that as "don't guess" (skip rate limiting for this
// request entirely), never as "fall back to some other bucket". A wrong
// guess that COLLAPSES two unrelated callers into one bucket is the unsafe
// failure mode; under-collapsing (or not limiting a malformed input at all)
// is not.

const HEXTET_RE = /^[0-9a-fA-F]{1,4}$/;

/**
 * Expand a (possibly compressed) IPv6 address into its 8 hextet groups.
 * Handles:
 *   - the "::" compression (anywhere in the address, including "::1" and
 *     "1::"), NOT just leaving it uncollapsed -- a compressed and fully
 *     expanded spelling of the SAME address must produce the SAME groups.
 *   - a trailing zone ID ("%eth0"), stripped before parsing.
 *   - an IPv4-mapped/compatible tail ("::ffff:1.2.3.4"), converted to its
 *     two equivalent hex groups first.
 * Rejects (returns null): more than one "::", the wrong group count, any
 * octet > 255 in an embedded dotted-quad tail, or any group that isn't 1-4
 * hex digits.
 *
 * @param {string} addr
 * @returns {string[] | null}
 */
export function expandIPv6(addr) {
  const zoneIdx = addr.indexOf("%");
  const clean = zoneIdx === -1 ? addr : addr.slice(0, zoneIdx);

  // An embedded IPv4 tail ("::ffff:1.2.3.4") is only ever the LAST segment
  // -- convert it to two hex groups so everything below only ever handles
  // pure-hex groups.
  let head = clean;
  const lastColon = clean.lastIndexOf(":");
  if (lastColon !== -1 && clean.slice(lastColon + 1).includes(".")) {
    const octets = parseIPv4(clean.slice(lastColon + 1));
    if (!octets) return null;
    const hi = ((octets[0] << 8) | octets[1]).toString(16);
    const lo = ((octets[2] << 8) | octets[3]).toString(16);
    head = clean.slice(0, lastColon + 1) + hi + ":" + lo;
  }

  const halves = head.split("::");
  if (halves.length > 2) return null; // more than one "::" is never valid

  let groups;
  if (halves.length === 1) {
    // No "::" compression used at all -- a LONE leading or trailing colon
    // (":1:2:...:8" or "1:2:...:8:") is malformed, not a legal single-colon
    // form. split(":").filter(Boolean) would otherwise silently DROP the
    // resulting empty string and still land on exactly 8 groups, masking
    // the malformed input as if it were valid.
    if (head.startsWith(":") || head.endsWith(":")) return null;
    groups = head.split(":");
    if (groups.length !== 8) return null;
  } else {
    const left = halves[0] ? halves[0].split(":") : [];
    const right = halves[1] ? halves[1].split(":") : [];
    const missing = 8 - left.length - right.length;
    // missing <= 0 (not just < 0): "::" must represent AT LEAST ONE
    // elided all-zero group. An address that already spells out all 8
    // groups AND still has a "::" (missing === 0) is malformed -- "::"
    // representing zero groups is not valid IPv6 notation (matches
    // Python's ipaddress module, which raises on e.g.
    // "1:2:3:4:5:6:7:8::").
    if (missing <= 0) return null;
    groups = [...left, ...Array(missing).fill("0"), ...right];
  }

  // Non-hex groups (verify finding #6): a length-8 array of garbage
  // hextets would otherwise sail through as "valid" and get bucketed on
  // nonsense. Reject outright rather than guessing.
  return groups.every((g) => HEXTET_RE.test(g)) ? groups : null;
}

/**
 * Canonicalize one hextet: lowercase, leading zeros stripped -- so
 * "2001:0DB8::1" and "2001:db8::1" (the same address, two spellings)
 * produce identical groups.
 *
 * @param {string} hextet
 * @returns {string}
 */
function canonicalHextet(hextet) {
  return parseInt(hextet, 16).toString(16);
}

function groupValue(hextet) {
  return parseInt(hextet, 16);
}

/**
 * Combine two 16-bit values into the 4 IPv4 octets they encode.
 * @returns {number[]}
 */
function hextetsToV4(hi16, lo16) {
  return [(hi16 >> 8) & 0xff, hi16 & 0xff, (lo16 >> 8) & 0xff, lo16 & 0xff];
}

/**
 * The IPv4 address embedded in an IPv4-mapped, IPv4-compatible, 6to4,
 * Teredo, or NAT64 IPv6 address -- or null if `groups` (already expanded,
 * NOT yet canonicalized) carries none of those. Mirrors
 * `billcommons_shared.safe_http._embedded_ipv4` (mapped/6to4/Teredo) plus
 * NAT64 (which that Python helper does NOT cover -- see its own module for
 * why that gap is harmless there; this best-effort edge limiter has no
 * equivalent upstream guard, so it unwraps NAT64 directly).
 *
 * @param {string[]} groups 8 expanded hextet strings
 * @returns {number[] | null} 4 IPv4 octets, or null
 */
function extractEmbeddedIPv4(groups) {
  const [g0, g1, g2, g3, g4, g5, g6, g7] = groups.map(groupValue);

  // IPv4-mapped ("::ffff:a.b.c.d") / IPv4-compatible, deprecated
  // ("::a.b.c.d"): groups 0-4 all zero, group 5 is 0x0000 (compatible) or
  // 0xffff (mapped); the embedded v4 is groups 6-7.
  if (g0 === 0 && g1 === 0 && g2 === 0 && g3 === 0 && g4 === 0 && (g5 === 0 || g5 === 0xffff)) {
    return hextetsToV4(g6, g7);
  }

  // 6to4 (RFC 3056): 2002:AABB:CCDD::/48 embeds the v4 address in groups
  // 1-2.
  if (g0 === 0x2002) {
    return hextetsToV4(g1, g2);
  }

  // Teredo (RFC 4380): 2001:0000::/32. The CLIENT's v4 (not the server's,
  // which lives in groups 2-3) is the last 32 bits, obscured by XOR with
  // 0xffffffff (applied per-hextet here, equivalent and avoids any 32-bit
  // signed-int surprises from combining first).
  if (g0 === 0x2001 && g1 === 0x0000) {
    return hextetsToV4(g6 ^ 0xffff, g7 ^ 0xffff);
  }

  // NAT64 Well-Known Prefix (RFC 6052 section 2.1), 64:ff9b::/96: the
  // embedded v4 is exactly the low 32 bits (groups 6-7), no reserved byte
  // to skip.
  if (g0 === 0x0064 && g1 === 0xff9b && g2 === 0 && g3 === 0 && g4 === 0 && g5 === 0) {
    return hextetsToV4(g6, g7);
  }

  // NAT64 Local-Use prefix (RFC 6052 section 2.2, PL=48 row), 64:ff9b:1::/48:
  // v4 bits 0-15 sit in group 3 whole, an 8-bit reserved 'u' byte (must be
  // 0) is the HIGH byte of group 4, then v4 bits 16-31 span the LOW byte
  // of group 4 and the HIGH byte of group 5.
  if (g0 === 0x0064 && g1 === 0xff9b && g2 === 0x0001) {
    const v4Hi = g3;
    const v4Lo = ((g4 & 0xff) << 8) | ((g5 >> 8) & 0xff);
    return hextetsToV4(v4Hi, v4Lo);
  }

  return null;
}

/**
 * Parse a dotted-quad IPv4 literal into its 4 octets, or null if any part
 * isn't a plain decimal integer 0-255 or there aren't exactly 4 parts.
 * Verify finding #6: an octet > 255 (or a non-numeric part) must be
 * rejected outright, not silently truncated or passed through.
 *
 * @param {string} str
 * @returns {number[] | null}
 */
function parseIPv4(str) {
  const parts = str.split(".");
  if (parts.length !== 4) return null;
  const octets = [];
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const n = Number(part);
    if (n > 255) return null;
    octets.push(n);
  }
  return octets;
}

function ipv4NetworkKey(octets) {
  return octets ? `${octets[0]}.${octets[1]}.${octets[2]}.0/24` : null;
}

/**
 * Collapse `ip` to its containing subnet: /24 (network address) for IPv4,
 * or for IPv6 -- after unwrapping any embedded IPv4 form (mapped,
 * compatible, 6to4, Teredo, NAT64) to ITS /24 -- otherwise /48 (first 3 of
 * the 8 canonicalized groups). Returns null for anything that fails to
 * parse (verify finding #6): the caller (`clientSubnetKey` in
 * middleware.ts) already treats null as "skip rate limiting for this
 * request", exactly like having no IP at all -- guessing a bucket for
 * malformed input is the unsafe failure mode, not skipping it.
 *
 * @param {string} ip
 * @returns {string | null}
 */
export function subnetBucket(ip) {
  const unbracketed =
    ip.startsWith("[") && ip.endsWith("]") ? ip.slice(1, -1) : ip;
  if (unbracketed.includes(":")) {
    const groups = expandIPv6(unbracketed);
    if (!groups) return null;
    const embeddedV4 = extractEmbeddedIPv4(groups);
    if (embeddedV4) return ipv4NetworkKey(embeddedV4);
    const canon = groups.map(canonicalHextet);
    return canon.slice(0, 3).join(":") + "::/48";
  }
  return ipv4NetworkKey(parseIPv4(unbracketed));
}

/**
 * Compress a fully-expanded, already-canonicalized (lowercase, no leading
 * zeros) 8-group IPv6 address into its RFC 5952 canonical shorthand: the
 * single LONGEST run of two-or-more consecutive "0" groups is replaced
 * with "::" (the leftmost run wins a tie, matching RFC 5952 section 4.2.3
 * and every mainstream stdlib implementation). A run shorter than 2 is
 * never compressed -- "::" eliding a single group is legal to parse but
 * not the canonical form.
 *
 * @param {string[]} canonGroups
 * @returns {string}
 */
function compressIPv6(canonGroups) {
  let bestStart = -1;
  let bestLen = 0;
  let i = 0;
  while (i < canonGroups.length) {
    if (canonGroups[i] !== "0") {
      i++;
      continue;
    }
    let j = i;
    while (j < canonGroups.length && canonGroups[j] === "0") j++;
    const len = j - i;
    if (len > bestLen) {
      bestLen = len;
      bestStart = i;
    }
    i = j;
  }

  if (bestLen < 2) return canonGroups.join(":");

  const left = canonGroups.slice(0, bestStart).join(":");
  const right = canonGroups.slice(bestStart + bestLen).join(":");
  return `${left}::${right}`;
}

/**
 * Canonicalize a client IP literal into one stable string key, so that
 * every equivalent spelling of the SAME address -- bracketed/unbracketed,
 * with/without a zone ID, any hextet case, any legal IPv6 compression --
 * mints the SAME per-IP rate-limit bucket key. Without this, a scraper
 * (or just an inconsistent proxy) rotating spelling alone would get a
 * fresh budget per spelling, same failure mode `subnetBucket` above
 * already guards against for the subnet key.
 *
 * Returns the original (bracket/zone-stripped, lowercased) string for
 * anything that fails to expand as IPv6 -- including plain IPv4, which
 * has no case or compression variance to normalize away.
 *
 * @param {string} ip
 * @returns {string}
 */
export function canonicalIp(ip) {
  const unbracketed =
    ip.startsWith("[") && ip.endsWith("]") ? ip.slice(1, -1) : ip;
  const zoneIdx = unbracketed.indexOf("%");
  const unzoned = zoneIdx === -1 ? unbracketed : unbracketed.slice(0, zoneIdx);
  const lower = unzoned.toLowerCase();

  if (!lower.includes(":")) return lower;

  const groups = expandIPv6(lower);
  if (!groups) return lower;

  return compressIPv6(groups.map(canonicalHextet));
}
