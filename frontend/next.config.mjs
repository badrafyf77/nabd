/** @type {import('next').NextConfig} */
// The browser only talks to this origin: /api/* is proxied to the NABD API, so the
// HttpOnly session cookie stays same-origin. Point NABD_API_ORIGIN elsewhere if needed.
const apiOrigin = process.env.NABD_API_ORIGIN || "http://127.0.0.1:8081";

const nextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
  experimental: {
    // Activity streams (SSE) stay open; the proxy's 30 s default would cut them.
    proxyTimeout: 60 * 60 * 1000,
  },
};

export default nextConfig;
