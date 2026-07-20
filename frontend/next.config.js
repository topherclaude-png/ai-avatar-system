/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  images: {
    dangerouslyAllowSVG: true,
    unoptimized: process.env.NODE_ENV === 'development',
    remotePatterns: [
      {
        protocol: 'http',
        hostname: 'localhost',
      },
      {
        protocol: 'https',
        hostname: '*.s3.*.amazonaws.com',
      },
      {
        protocol: 'https',
        hostname: 's3.amazonaws.com',
      },
      {
        // RunPod HTTP proxy — backend-served /uploads/ images when the POC
        // runs on a pod (local storage mode).
        protocol: 'https',
        hostname: '*.proxy.runpod.net',
      },
      ...(process.env.NEXT_PUBLIC_S3_BUCKET_DOMAIN
        ? [{
            protocol: 'https',
            hostname: process.env.NEXT_PUBLIC_S3_BUCKET_DOMAIN,
          }]
        : []),
      ...(process.env.NEXT_PUBLIC_CLOUDFRONT_DOMAIN
        ? [{
            protocol: 'https',
            hostname: process.env.NEXT_PUBLIC_CLOUDFRONT_DOMAIN,
          }]
        : []),
    ],
  },
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000',
    // Follow the API URL unless a WS endpoint is explicitly set. The old
    // 'ws://localhost:8000' default silently pinned every non-local build's
    // WebSocket to localhost (buildSessionWsUrl prefers WS_URL over API_URL),
    // which reads as "Disconnected" with zero server-side evidence.
    NEXT_PUBLIC_WS_URL:
      process.env.NEXT_PUBLIC_WS_URL || process.env.NEXT_PUBLIC_API_URL || 'ws://localhost:8000',
  },
};

module.exports = nextConfig;
