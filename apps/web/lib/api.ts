import { API_BASE } from "./config";

/**
 * Result wrapper so every page can render an honest "data unavailable"
 * state instead of throwing (the API may be down; see BRIEF-wave2.md).
 */
export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; status?: number };

export interface ApiGetOptions {
  /**
   * Seconds to cache this response for (Next ISR). Omit for `no-store`.
   *
   * Crawlable pages MUST pass this. A bill page fans out to seven API calls,
   * and the sitemaps expose ~200k of them; uncached, a single search-engine
   * crawl turns into well over a million requests against the API and its
   * Postgres. Anything personalized or query-driven (i.e. /search) stays
   * uncached, which is why no-store remains the default rather than the
   * exception.
   */
  revalidate?: number;
  /**
   * Override the default 8s abort timeout for a caller that is legitimately
   * slower than the rest of the site -- e.g. /bills/[id]/compare, which runs
   * difflib over two full bill texts on a cold cache. When set, the uncached
   * retry (see below) also uses this value rather than the default 4s, since
   * a genuinely slow upstream call needs a genuinely longer retry, not a
   * shorter one that's guaranteed to abort again.
   */
  timeoutMs?: number;
}

/**
 * Headers identifying this renderer to the API's rate limiter.
 *
 * Every server-rendered page reaches the API from one of a few Vercel egress
 * addresses, and the limiter keys on that address -- so without this the whole
 * public site shares ONE 300/minute bucket. At 7-10 API calls per bill page
 * that caps the entire site at ~30-43 bill pages a minute, and visitors start
 * 429ing each other.
 *
 * Read at call time, not module scope, so a rotated secret takes effect on
 * redeploy without a stale build-time capture. Unset => header omitted => the
 * request is simply rate limited as anonymous traffic (fails closed).
 */
function internalHeaders(): Record<string, string> {
  const secret = process.env.BILLCOMMONS_INTERNAL_CLIENT_SECRET;
  return secret ? { "x-billcommons-internal": secret } : {};
}

export async function apiGet<T>(
  path: string,
  searchParams?: Record<string, string | number | undefined>,
  options?: ApiGetOptions
): Promise<ApiResult<T>> {
  const url = new URL(path.replace(/^\//, ""), `${API_BASE}/`);
  if (searchParams) {
    for (const [key, value] of Object.entries(searchParams)) {
      if (value !== undefined && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }

  const timeoutMs = options?.timeoutMs ?? 8_000;
  // Shorter than the primary attempt by default: this retry only runs after
  // the first call already failed, and it's on the critical path of a
  // response that's already late -- don't let it eat another 8s. A caller
  // that explicitly opted into a longer primary timeout (compare's difflib
  // run) gets a matching longer retry instead, since a short retry there
  // would just abort again.
  const retryTimeoutMs = options?.timeoutMs ?? 4_000;

  try {
    const res = await fetch(url.toString(), {
      ...(options?.revalidate === undefined
        ? // Uncached path (e.g. /search): every hit is a live SSR call, so
          // this is exactly the request class that used to block a render
          // until Vercel's own function execution limit killed it.
          { cache: "no-store" as const, signal: AbortSignal.timeout(timeoutMs) }
        : // Cached/ISR path: deliberately NO signal here. Verified by
          // rebuilding with and without it: attaching an AbortSignal to a
          // `next.revalidate` fetch does not just risk a bad response
          // getting baked into static HTML, it makes Next.js treat the
          // fetch as uncacheable and disables static generation for the
          // WHOLE route -- the homepage and five other ISR routes flipped
          // from prerendered (revalidate 5m-6h) to fully dynamic (refetched
          // on every request) the moment this was added, which is the exact
          // uncached-crawl-storm this fix exists to prevent, just spread
          // across the whole site instead of one route. A slow/hung PRIMARY
          // fetch on a cached page is a real but different, much rarer
          // failure mode than /search's every-hit-is-live problem, and it
          // still self-heals via the retry below on the very next request.
          { next: { revalidate: options.revalidate } }),
      headers: { Accept: "application/json", ...internalHeaders() },
    });

    if (!res.ok) {
      // A FAILED response is cached exactly like a successful one, for the
      // whole revalidate window, and Next's Data Cache persists across
      // deployments -- so a single blip pins an error onto a page for an hour
      // and redeploying does not clear it. That is not hypothetical: pages
      // rendered against the API before /related and /subjects shipped cached
      // those 404s and kept serving "temporarily unavailable" long after the
      // endpoints were live.
      //
      // Retry once uncached so an error self-heals on the very next request
      // instead of persisting. Only the failure path pays for this.
      //
      // NOTE: this retry deliberately does NOT also run from the catch block
      // below for an aborted/thrown primary fetch on a cached call. It was
      // tried and reverted: a no-store fetch that actually EXECUTES during
      // `next build`'s prerender (which happens whenever the primary fetch
      // has a transient failure at build time -- this DB has observed
      // connection drops) makes Next mark the whole route dynamic for that
      // build -- the same static-generation regression described above. A
      // thrown/aborted primary fetch on a cached page is therefore left to
      // resolve on Next's normal ISR revalidation cadence instead.
      if (options?.revalidate !== undefined) {
        const retry = await fetch(url.toString(), {
          cache: "no-store",
          headers: { Accept: "application/json", ...internalHeaders() },
          signal: AbortSignal.timeout(retryTimeoutMs),
        });
        if (retry.ok) {
          return { ok: true, data: (await retry.json()) as T };
        }
        return {
          ok: false,
          error: `API returned ${retry.status} ${retry.statusText}`,
          status: retry.status,
        };
      }
      return {
        ok: false,
        error: `API returned ${res.status} ${res.statusText}`,
        status: res.status,
      };
    }

    const data = (await res.json()) as T;
    return { ok: true, data };
  } catch (err) {
    // AbortSignal.timeout() rejects with a DOMException named "TimeoutError"
    // -- surfaced here as the same ApiResult error shape as any other
    // unreachable-API failure, so every page's existing "data unavailable"
    // rendering path handles it with no page-level changes.
    return {
      ok: false,
      error:
        err instanceof Error
          ? `Could not reach the API: ${err.message}`
          : "Could not reach the API.",
    };
  }
}
