import { SITE_URL } from "@/lib/config";
import {
  SITEMAP_REVALIDATE,
  XML_HEADERS,
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
  // Deliberately small. Bill chunks are NOT listed here any more.
  //
  // This index used to advertise 209,932 URLs across 21 bill chunks. Google
  // indexed almost none of it: URL Inspection reports canonical bill URLs as
  // "URL is unknown to Google" -- not blocked, not rejected, simply never
  // reached -- while genuinely useful pages (the mortality report, coverage,
  // topics) sat at "Discovered - currently not indexed". A new domain gets a
  // crawl budget, and spending it on 200k largely-repetitive records starves
  // the handful of pages actually worth citing.
  //
  // The bill pages remain publicly addressable and are marked noindex at the
  // page level; the /sitemaps/bills-N.xml routes also remain, because API
  // consumers use them to enumerate the corpus. They are just no longer
  // submitted for indexing.
  const entries = [
    { loc: `${SITE_URL}/sitemaps/pages.xml` },
    { loc: `${SITE_URL}/sitemaps/states.xml` },
  ];

  return new Response(renderSitemapIndex(entries), {
    headers: {
      ...XML_HEADERS,
      // Was X-Sitemap-Chunks, a count of the bill chunks this index advertised.
      // The index no longer lists them, so the header would only ever report 0.
      "X-Sitemap-Sections": String(entries.length),
      "X-Sitemap-Revalidate": String(SITEMAP_REVALIDATE),
    },
  });
}
