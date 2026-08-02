import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async redirects() {
    return [
      {
        source: "/:path*",
        has: [{ type: "host", value: "www.billcommons.org" }],
        destination: "https://billcommons.org/:path*",
        permanent: true,
      },
      {
        // /services was removed on 2026-08-02: a consulting page on a
        // public-good data utility reads as lead generation, and it sat in a
        // domain overlapping the maintainer's employment. It had no traffic to
        // lose -- 64 unique visitors site-wide in 14 days, nearly all crawler.
        //
        // Redirected rather than 404'd because a published URL should not
        // simply die: the page was in the sitemap, and sent email and posts are
        // immutable even when nothing here currently links it.
        source: "/services",
        destination: "/about",
        permanent: true,
      },
    ];
  },
  async rewrites() {
    return {
      beforeFiles: [
        {
          source: "/",
          has: [{ type: "host", value: "status.billcommons.org" }],
          destination: "/coverage",
        },
        {
          source: "/:path*",
          has: [{ type: "host", value: "status.billcommons.org" }],
          destination: "/coverage",
        },
      ],
      afterFiles: [],
      fallback: [],
    };
  },
};

export default nextConfig;
