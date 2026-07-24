import type { NextConfig } from "next";

const nextConfig: NextConfig = {
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
