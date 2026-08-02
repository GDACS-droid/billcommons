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

  try {
    const res = await fetch(url.toString(), {
      ...(options?.revalidate === undefined
        ? { cache: "no-store" as const }
        : { next: { revalidate: options.revalidate } }),
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
      if (options?.revalidate !== undefined) {
        const retry = await fetch(url.toString(), {
          cache: "no-store",
          headers: { Accept: "application/json", ...internalHeaders() },
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
    return {
      ok: false,
      error:
        err instanceof Error
          ? `Could not reach the API: ${err.message}`
          : "Could not reach the API.",
    };
  }
}
