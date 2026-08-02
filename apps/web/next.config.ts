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
        // status.billcommons.org used to land on /coverage, which reports how
        // much DATA we hold per state and says nothing about whether the
        // service is answering. On 2026-08-02 the API was down for hours while
        // /coverage rendered perfectly from cache. /status actually probes.
        {
          source: "/",
          has: [{ type: "host", value: "status.billcommons.org" }],
          destination: "/status",
        },
        {
          source: "/:path*",
          has: [{ type: "host", value: "status.billcommons.org" }],
          destination: "/status",
        },
      ],
      afterFiles: [],
      fallback: [],
    };
  },
};

export default nextConfig;
