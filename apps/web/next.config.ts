import type { NextConfig } from "next";

const agentBase =
  process.env.AGENT_BASE_URL?.replace(/\/$/, "") ||
  process.env.NEXT_PUBLIC_AGENT_BASE_URL?.replace(/\/$/, "") ||
  "http://localhost:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Long LangGraph runs (Gemini + Overpass + RAG) often exceed 60s.
  experimental: {
    proxyTimeout: 300_000,
  },
  async rewrites() {
    return [
      {
        source: "/api/agent/:path*",
        destination: `${agentBase}/:path*`,
      },
    ];
  },
};

export default nextConfig;
