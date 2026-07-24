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
