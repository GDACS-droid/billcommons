import { apiGet } from "./api";
import type { ListEnvelope } from "./types";

/**
 * Fetch every page of a list endpoint.
 *
 * List endpoints clamp per_page to 50 server-side (pagination.MAX_PER_PAGE),
 * and asking for more does not fail -- it silently returns 50. That bit us
 * twice, both times invisibly:
 *
 *   * /states asked for 51 jurisdictions and rendered 50, so one state was
 *     absent from the site and from every crawl path through it;
 *   * the homepage summed `per_page: 51` coverage rows when there are 77 (they
 *     are per jurisdiction-SESSION, not per jurisdiction) and advertised
 *     "150,276 bills" against a real corpus of 209,612 -- understating the
 *     project by 28% on its most-read page.
 *
 * Neither surfaced as an error. Anything that needs a COMPLETE set must page.
 */
export async function fetchAllPages<T>(
  path: string,
  params: Record<string, string | number | undefined>,
  revalidate: number,
  { maxPages = 50 }: { maxPages?: number } = {}
): Promise<{ ok: true; items: T[] } | { ok: false; items: T[] }> {
  const first = await apiGet<ListEnvelope<T>>(
    path,
    { ...params, per_page: 50, page: 1 },
    { revalidate }
  );
  if (!first.ok) return { ok: false, items: [] };

  const items = [...first.data.data];
  const totalPages = Math.min(first.data.pagination.total_pages, maxPages);

  if (totalPages > 1) {
    const rest = await Promise.all(
      Array.from({ length: totalPages - 1 }, (_, i) =>
        apiGet<ListEnvelope<T>>(
          path,
          { ...params, per_page: 50, page: i + 2 },
          { revalidate }
        )
      )
    );
    for (const page of rest) {
      // A failed page means the set is incomplete; say so rather than quietly
      // returning a short list that reads as authoritative.
      if (!page.ok) return { ok: false, items };
      items.push(...page.data.data);
    }
  }

  return { ok: true, items };
}
