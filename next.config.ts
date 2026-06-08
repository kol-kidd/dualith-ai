import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  distDir: process.env.DUALITH_NEXT_DIST_DIR ?? ".next"
};

export default nextConfig;
