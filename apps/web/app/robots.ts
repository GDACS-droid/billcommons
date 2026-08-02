import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/config";

export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      // /search is query-driven and uncacheable by design.
      //
      // /bills/*/compare runs a difflib diff over two full bill texts and is
      // linked from every bill page, so crawlers walk into it -- the single
      // most expensive page on the site, reached ~200k ways, with no search
      // value whatsoever. Caching it is not enough; it should not be crawled.
      disallow: ["/search", "/bills/*/compare"],
    },
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
