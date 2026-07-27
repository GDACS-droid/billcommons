import { SITE_URL } from "@/lib/config";
import {
  STATIC_ROUTES,
  XML_HEADERS,
  billUrlsFor,
  getBillChunk,
  getStateAndSessionUrls,
  parseBillChunkFile,
  renderUrlset,
} from "@/lib/sitemap";

// See app/sitemap.xml/route.ts: these must not be prerendered against a build
// that cannot reach the API. Fetch-level caching keeps them cheap.
export const dynamic = "force-dynamic";

/**
 * Serves the individual sitemap files named by /sitemap.xml:
 *   /sitemaps/pages.xml     static routes
 *   /sitemaps/states.xml    every state hub and session index
 *   /sitemaps/bills-N.xml   one chunk of bill pages
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ file: string }> }
) {
  const { file } = await params;

  if (file === "pages.xml") {
    return xml(
      renderUrlset(
        STATIC_ROUTES.map((route) => ({
          loc: `${SITE_URL}${route}`,
          changefreq: route === "" ? "hourly" : "daily",
          priority: route === "" ? 1.0 : 0.6,
        }))
      )
    );
  }

  if (file === "states.xml") {
    return xml(renderUrlset(await getStateAndSessionUrls()));
  }

  const chunk = parseBillChunkFile(file);
  if (chunk === null) {
    return new Response("Not found", { status: 404 });
  }

  const result = await getBillChunk(chunk);
  if (!result.ok) {
    // 503, not an empty 200: an empty sitemap tells a crawler these URLs are
    // gone, which would drop a whole chunk of bills out of the index. A 5xx
    // just makes it retry.
    return new Response("Sitemap temporarily unavailable", { status: 503 });
  }

  return xml(renderUrlset(billUrlsFor(result.data.data)));
}

function xml(body: string) {
  return new Response(body, { headers: XML_HEADERS });
}
