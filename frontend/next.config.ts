import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  agentRules: false,
  experimental: {
    useTypeScriptCli: false,
    webpackBuildWorker: false,
  },
};

export default nextConfig;
