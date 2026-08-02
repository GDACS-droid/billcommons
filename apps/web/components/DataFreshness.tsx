import Link from "next/link";

/**
 * Freshness stamp for a CACHED page.
 *
 * Crawlable pages are served from Next's Data Cache for up to an hour, which
 * means a page rendering perfectly is not evidence that the API is healthy
 * right now -- during the 2026-08-02 outage billcommons.org returned HTTP 200
 * throughout, because every page was cached over a dead API. Nothing on the
 * page said so.
 *
 * Stale data is acceptable. Stale data that silently presents itself as live is
 * not. This states the age of what is shown and links to the uncached health
 * endpoint, so a reader can always distinguish "this is a bit old" from "this
 * is broken".
 */
export default function DataFreshness({
  timestamp,
  maxAgeSeconds,
}: {
  /** ISO 8601 instant the underlying data was last confirmed, or null. */
  timestamp: string | null;
  /** The page's `revalidate`, so the stated worst-case age is the real one. */
  maxAgeSeconds: number;
}) {
  const maxAgeLabel =
    maxAgeSeconds >= 86_400
      ? `${Math.round(maxAgeSeconds / 86_400)}d`
      : maxAgeSeconds >= 3_600
        ? `${Math.round(maxAgeSeconds / 3_600)}h`
        : `${Math.round(maxAgeSeconds / 60)}m`;

  return (
    <p className="mt-3 text-xs text-slate-500">
      {timestamp ? (
        <>
          Data last confirmed{" "}
          <time dateTime={timestamp}>
            {new Date(timestamp).toISOString().replace("T", " ").slice(0, 16)} UTC
          </time>
          .{" "}
        </>
      ) : null}
      This page is cached for up to {maxAgeLabel}, so it may lag the live data.
      For current API status see{" "}
      <Link
        href="https://api.billcommons.org/api/v1/health"
        className="underline"
        prefetch={false}
      >
        the health endpoint
      </Link>
      , which is never cached.
    </p>
  );
}
