import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/config";

// Only statically-known routes are enumerated here; per-bill/person/committee
// pages number in the hundreds of thousands and are discoverable via the
// coverage/search pages + robots.txt rather than a static sitemap entry each.
export default function sitemap(): MetadataRoute.Sitemap {
  const staticRoutes = [
    "",
    "/states",
    "/hearings",
    "/search",
    "/coverage",
    "/docs/api",
    "/docs/mcp",
    "/methodology",
    "/about",
  ];

  const now = new Date();

  return staticRoutes.map((route) => ({
    url: `${SITE_URL}${route}`,
    lastModified: now,
    changeFrequency: route === "" ? "hourly" : "daily",
    priority: route === "" ? 1 : 0.6,
  }));
}
