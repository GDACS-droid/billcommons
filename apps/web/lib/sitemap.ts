import { apiGet } from "./api";
import { billPath, slugify } from "./billUrl";
import { fetchAllPages } from "./collections";
import { SITE_URL } from "./config";
import type { Jurisdiction } from "./types";

/**
 * XML sitemap construction.
 *
 * The site is ~200k bill pages that no crawler can reach by following links --
 * bills are behind search and paginated session indexes -- so the sitemaps are
 * the entire discovery path. They are built at request time from the API's bulk
 * feed and cached, rather than enumerated at build time, because the corpus
 * grows every day: a chunk count frozen at deploy would leave every bill added
 * afterwards undiscoverable until the next deploy.
 */

/** Seconds. Bills are added continuously; a day-old sitemap is fine. */
export const SITEMAP_REVALIDATE = 86_400;

export interface SitemapUrl {
  loc: string;
  lastmod?: string | null;
  changefreq?: string;
  priority?: number;
}

/** XML text escaping. Bill URLs contain & only via query strings we don't emit,
 * but escaping is not optional in XML and a single stray & breaks the whole file. */
function escapeXml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export function renderUrlset(urls: SitemapUrl[]): string {
  const body = urls
    .map((url) => {
      const parts = [`    <loc>${escapeXml(url.loc)}</loc>`];
      if (url.lastmod) parts.push(`    <lastmod>${escapeXml(url.lastmod)}</lastmod>`);
      if (url.changefreq) parts.push(`    <changefreq>${url.changefreq}</changefreq>`);
      if (url.priority !== undefined)
        parts.push(`    <priority>${url.priority}</priority>`);
      return `  <url>\n${parts.join("\n")}\n  </url>`;
    })
    .join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${body}\n</urlset>\n`;
}

export function renderSitemapIndex(entries: { loc: string; lastmod?: string }[]): string {
  const body = entries
    .map((entry) => {
      const parts = [`    <loc>${escapeXml(entry.loc)}</loc>`];
      if (entry.lastmod) parts.push(`    <lastmod>${escapeXml(entry.lastmod)}</lastmod>`);
      return `  <sitemap>\n${parts.join("\n")}\n  </sitemap>`;
    })
    .join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${body}\n</sitemapindex>\n`;
}

export const XML_HEADERS = {
  "Content-Type": "application/xml; charset=utf-8",
  "Cache-Control": "public, max-age=3600, s-maxage=86400",
} as const;

export const STATIC_ROUTES = [
  "",
  "/states",
  "/topics",
  "/topics/artificial-intelligence",
  "/topics/data-privacy",
  "/topics/cryptocurrency",
  "/reports/2026-bill-mortality",
  "/hearings",
  "/coverage",
  "/docs/api",
  "/docs/mcp",
  "/docs/agents",
  "/quality",
  "/changelog",
  "/methodology",
  "/about",
  "/feedback",
];

interface SitemapStats {
  bills: { total: number; chunk_size: number; chunks: number };
}

export async function getSitemapStats() {
  return apiGet<SitemapStats>("/api/v1/sitemap/stats", undefined, {
    revalidate: SITEMAP_REVALIDATE,
  });
}

export interface SitemapBillRow {
  id: string;
  identifier_norm: string;
  jurisdiction: string;
  session: string;
  updated_at: string | null;
}

export async function getBillChunk(chunk: number) {
  return apiGet<{ data: SitemapBillRow[] }>(
    "/api/v1/sitemap/bills",
    { chunk },
    { revalidate: SITEMAP_REVALIDATE }
  );
}

export function billUrlsFor(rows: SitemapBillRow[]): SitemapUrl[] {
  return rows
    .filter((row) => row.jurisdiction && row.session && row.identifier_norm)
    .map((row) => ({
      loc: `${SITE_URL}${billPath(row.jurisdiction, row.session, row.identifier_norm)}`,
      lastmod: row.updated_at,
      changefreq: "weekly",
      priority: 0.7,
    }));
}

/**
 * The 51 state hub pages. Session indexes are deliberately NOT included.
 *
 * A state hub answers a question that no official site answers: what we hold
 * for this jurisdiction, how fresh it is, how it validates, and where the
 * authoritative source lives. A session index is a thin paginated list of bill
 * links, and now that bill pages are noindex it exists only as a crawl path --
 * useful to follow, not worth submitting for indexing.
 *
 * This is the whole strategy in one function: submit the pages that deserve to
 * be cited, not every page that exists. See app/sitemap.xml/route.ts.
 */
export async function getStateAndSessionUrls(): Promise<SitemapUrl[]> {
  // 51 jurisdictions against a per_page clamped to 50 -- this MUST be paged in
  // full or the directory silently loses a state (it has, twice).
  const jurisdictionsResult = await fetchAllPages<Jurisdiction>(
    "/api/v1/jurisdictions",
    {},
    SITEMAP_REVALIDATE
  );

  return jurisdictionsResult.items.map((j) => ({
    loc: `${SITE_URL}/states/${j.abbreviation.toUpperCase()}`,
    changefreq: "daily",
    priority: 0.8,
  }));
}

/** Parse "bills-12.xml" -> 12; anything else -> null. */
export function parseBillChunkFile(file: string): number | null {
  const match = /^bills-(\d+)\.xml$/.exec(file);
  if (!match) return null;
  const chunk = Number(match[1]);
  return Number.isSafeInteger(chunk) && chunk >= 0 ? chunk : null;
}

export { slugify };
