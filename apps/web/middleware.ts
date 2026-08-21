import { NextResponse, type NextRequest } from "next/server";
import { CappedBucketMap } from "./lib/capped-bucket-map.mjs";
import { checkDualBuckets } from "./lib/dual-bucket-check.mjs";
import { canonicalIp, subnetBucket } from "./lib/subnet-bucket.mjs";

// NOTE ON WHAT THIS FILE IS: this edge layer is best-effort and
// PER-INSTANCE (see the module-scope caveat below) -- it is NOT the
// authoritative scraper control. That's the API's own heavy-route tier
// (60/minute/IP, billcommons_api/rate_limit.py), which is durable and
// route-aware. This layer exists purely to cut Vercel function
// invocations from page-loop scraping (SSR pages the API limiter never
// sees at all) before they ever reach a serverless function -- treat any
// limit here as a floor-raiser, not a guarantee.
//
// 2026-08-21 bleed-stop: the API's own per-IP/subnet limiter (see
// billcommons_api/rate_limit.py) is the primary control. This is a cheap,
// best-effort second line in front of the SSR pages themselves, which the
// API limiter never sees at all.
//
// KNOWN LIMITATION, documented rather than hidden: this counter lives in
// module scope, which on Vercel means one counter PER SERVERLESS/EDGE
// INSTANCE, not one globally. A caller spread across instances (or hitting a
// cold start) gets a fresh budget per instance -- this is not a durable
// limit (that needs a shared store, e.g. Redis/Upstash, out of scope here),
// it just raises the floor for a single-instance bulk crawl.
//
// Keyed on the /24 (IPv4) / /48 (IPv6) subnet, not the exact address --
// mirrors the API's own `subnet_bucket` and catches the same
// rotate-within-a-small-block shape the 2026-08-21 scraper used.
//
// Verify round fd9997c, finding #1: a Next-Router-Prefetch/RSC exemption
// was REMOVED here -- both headers are ordinary client-sent request headers
// with no server-side verification, so a scraper trivially sets either one
// on every request and zeros this limiter entirely. Every request counts.
//
// Verify round 8155c04, finding #3 (opus): with every request now counting
// (including prefetch/RSC), 300/minute per /24 turned out tight enough that
// prefetch-heavy browsing from a single office/NAT block could hit it on
// ordinary traffic, not just a scraper. Raised to 600/minute per subnet.
//
// Verify r6 (round 88e289c), finding #3: the real 2026-08-21 scraper's 4
// IPs were in 3 DIFFERENT /18s (i.e. different /24s too) -- a /24-only
// bucket never fires on that shape at all, since no single /24 ever saw
// more than one IP's share of the traffic. A per-IP bucket (300/minute)
// now runs ALONGSIDE the per-subnet one; a request must pass BOTH. The
// per-IP bucket is what actually would have caught each of those 4
// addresses individually running well over 300/minute; the per-subnet
// bucket remains for the "many IPs in ONE small block" shape this file's
// other fixes already cover.
const WINDOW_MS = 60_000;
const PER_IP_LIMIT = 300;
const PER_SUBNET_LIMIT = 600;
const BULK_ACCESS_MESSAGE =
  "Rate limit exceeded. Buy higher limits or a full-corpus snapshot (from $299/mo or $499 one-time): https://billcommons.org/docs/bulk";
const BULK_ACCESS_DOCS_URL = "https://billcommons.org/docs/bulk";

// Verify round c4400ea, finding #1: an unbounded Map growing once per
// distinct subnet key is both a memory leak and a cheap way to exhaust a
// single instance by rotating source addresses -- mirrors
// `_FixedWindowCounter` (billcommons_api/rate_limit.py) and the MCP
// server's own MAX_TRACKED_IPS cap (billcommons_mcp/rate_limit.py).
//
// Verify r5 (round b93690a), finding #3: the sweep/insert-time capacity
// logic (evict the OLDEST ~10% of tracked keys by window start, never
// clear() the whole map) now lives in `CappedBucketMap` -- extracted so
// it can be unit-tested directly with `node --test`, same reasoning as
// `subnet-bucket.mjs`.
//
// Verify r6 (round 88e289c), finding #3: two SEPARATE maps -- one keyed
// on the exact IP, one on its /24 (or /48) subnet -- so an IP can trip
// its own bucket independently of how many (or few) neighbors share its
// block.
const ipState = new CappedBucketMap({ windowMs: WINDOW_MS, maxKeys: 50_000 });
const subnetState = new CappedBucketMap({ windowMs: WINDOW_MS, maxKeys: 50_000 });

// Verify round c4400ea, finding #2: only X-Real-Ip (Vercel's own, which a
// forged client value never survives past the edge) is trusted as an exact
// address. X-Forwarded-For is attacker-appendable at the LEFT end, so if
// X-Real-Ip is absent the RIGHTMOST entry is taken, mirroring the API's own
// `client_ip` walk -- never the leftmost, which a spoofer fully controls.
// Absent both, this must NOT collapse every headerless caller into one
// shared bucket -- return null and skip limiting entirely for that request.
function resolveClientIp(req: NextRequest): string | null {
  const realIp = req.headers.get("x-real-ip")?.trim();
  return realIp || rightmostForwardedFor(req);
}

function rightmostForwardedFor(req: NextRequest): string | null {
  const forwarded = req.headers.get("x-forwarded-for");
  if (!forwarded) return null;
  const parts = forwarded.split(",").map((p) => p.trim()).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
}

function wantsHtml(req: NextRequest): boolean {
  return (req.headers.get("accept") || "").includes("text/html");
}

function rateLimitedResponse(req: NextRequest, retryAfter: number): NextResponse {
  const headers = { "Retry-After": String(retryAfter), "Cache-Control": "no-store" };
  if (wantsHtml(req)) {
    const html = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Rate limited — Bill Commons</title></head>
<body>
<h1>Rate limit exceeded</h1>
<p>${BULK_ACCESS_MESSAGE.replace(
      BULK_ACCESS_DOCS_URL,
      `<a href="${BULK_ACCESS_DOCS_URL}">${BULK_ACCESS_DOCS_URL}</a>`
    )}</p>
</body>
</html>`;
    return new NextResponse(html, {
      status: 429,
      headers: { ...headers, "Content-Type": "text/html; charset=utf-8" },
    });
  }
  // Same house error envelope the API returns (see rate_limit.py's 429 --
  // {"error": {"code", "message", "retry_after", "docs", "request_id"}}) so
  // a client parses one shape regardless of which layer refused it. No
  // request_id here: this runs before any request-ID is minted for the
  // request (there is no upstream request-ID middleware in front of this).
  return NextResponse.json(
    {
      error: {
        code: "rate_limited",
        message: BULK_ACCESS_MESSAGE,
        retry_after: retryAfter,
        docs: BULK_ACCESS_DOCS_URL,
      },
    },
    { status: 429, headers }
  );
}

export function middleware(req: NextRequest) {
  const ip = resolveClientIp(req);
  if (ip === null) {
    // No public IP found on either header -- never share one bucket across
    // every headerless caller (that bucket would trip on ordinary
    // aggregate traffic, throttling everyone at once).
    return NextResponse.next();
  }
  const subnetKey = subnetBucket(ip);
  if (subnetKey === null) {
    // Malformed IP literal (subnetBucket's own contract, see
    // subnet-bucket.mjs) -- same "don't guess a bucket" reasoning as the
    // no-IP case above.
    return NextResponse.next();
  }

  // Verify r6 (round 88e289c), finding #3: `performance.now()` (monotonic,
  // never affected by an NTP/system-clock adjustment) instead of
  // `Date.now()` (wall clock, can jump -- a backward jump would corrupt
  // every live window's remaining-time math).
  const now = performance.now();
  // Per-IP key is fully canonicalized (brackets/zone-id stripped,
  // lowercased, IPv6 expanded to its canonical compressed form) so no
  // spelling variant of the same address (`[2001:DB8::1]`,
  // `2001:db8::1%eth0`, `2001:0db8:0:0::1`, ...) mints a distinct bucket.
  const ipKey = canonicalIp(ip);

  // Check-all-THEN-increment: peek BOTH buckets first. If either would
  // deny this request, return 429 and commit NEITHER -- a request that
  // fails the per-IP check must not still burn a slot in the per-subnet
  // bucket (or vice versa). Only once both peeks pass do we commit both.
  const ipPeek = ipState.peek(ipKey, now);
  const subnetPeek = subnetState.peek(subnetKey, now);

  const { exceeded, retryAfter } = checkDualBuckets({
    ipBucket: ipPeek,
    subnetBucket: subnetPeek,
    now,
    perIpLimit: PER_IP_LIMIT,
    perSubnetLimit: PER_SUBNET_LIMIT,
    windowMs: WINDOW_MS,
  });

  if (exceeded) {
    return rateLimitedResponse(req, retryAfter);
  }

  ipState.commit(ipKey, now);
  subnetState.commit(subnetKey, now);
  return NextResponse.next();
}

export const config = {
  // Standard Next.js exclusion pattern: Next's own asset pipeline
  // (_next/static, _next/image), favicons, and common static-asset
  // extensions (images/fonts). /api/* is also excluded -- no /api routes
  // exist in this app (the public API is a separate service) -- named here
  // so it stays excluded if that changes. Crawler-infrastructure routes
  // (robots.txt, sitemap.xml, sitemaps/*, llms.txt) are ALSO excluded: a 429
  // on robots.txt makes Google pause crawling the whole host, a far worse
  // outcome than the traffic this limiter is trying to shed.
  matcher: [
    "/((?!_next/static|_next/image|api/|favicon.ico|apple-icon.png|icon.svg|robots.txt|sitemap.xml|sitemaps/|llms.txt|.*\\.(?:png|jpg|jpeg|gif|webp|css|js|woff2?)$).*)",
  ],
};
