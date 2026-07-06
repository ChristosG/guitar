import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  output: "standalone",
  // Pin the workspace root: the host has unrelated lockfiles in parent
  // directories, which makes Turbopack mis-infer the project root otherwise.
  turbopack: {
    root: process.cwd(),
  },
};

export default withNextIntl(nextConfig);
