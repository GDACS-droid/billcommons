import { SITE_URL } from "@/lib/config";
import {
  SITEMAP_REVALIDATE,
  XML_HEADERS,
  getSitemapStats,
  renderSitemapIndex,
} from "@/lib/sitemap";

/**
 * Never prerendered at build time.
 *
 * This route was originally allowed to prerender, and the first build baked a
 * sitemap index containing ZERO bill chunks -- the API was not reachable from
 * the build, the chunk count came back 0, and the empty result was cached as
 * though it were the truth. On a hosted build that is a silent outage: every
 * bill page drops out of the index until something forces a rebuild.
 *
 * The route now runs per request. That is not expensive: the API calls it makes
 * carry their own `revalidate`, so they are served from the data cache and only
 * the XML assembly is repeated.
 */
export const dynamic = "force-dynamic";

/**
 * Sitemap index. The chunk count comes from the API at request time, so bills
 * added since the last deploy still get a sitemap file pointing at them.
 */
export async function GET() {
  const stats = await getSitemapStats();
  if (!stats.ok) {
    // Fail loud. An index that silently omits the bill sitemaps looks to a
    // crawler like 200k pages were deliberately withdrawn; a 503 just gets
    // retried.
    return new Response("Sitemap index temporarily unavailable", {
      status: 503,
      headers: { "Cache-Control": "no-store" },
    });
  }
  const chunks = stats.data.bills.chunks;

  const entries = [
    { loc: `${SITE_URL}/sitemaps/pages.xml` },
    { loc: `${SITE_URL}/sitemaps/states.xml` },
    ...Array.from({ length: chunks }, (_, i) => ({
      loc: `${SITE_URL}/sitemaps/bills-${i}.xml`,
    })),
  ];

  return new Response(renderSitemapIndex(entries), {
    headers: {
      ...XML_HEADERS,
      "X-Sitemap-Chunks": String(chunks),
      "X-Sitemap-Revalidate": String(SITEMAP_REVALIDATE),
    },
  });
}
