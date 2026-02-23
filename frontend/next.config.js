/** @type {import('next').NextConfig} */

const { withSentryConfig } = require('@sentry/nextjs');

const nextConfig = {
  // ---------------------------------------------------------------------------
  // Output — standalone for Docker / ECS deployment
  // ---------------------------------------------------------------------------
  output: 'standalone',

  // ---------------------------------------------------------------------------
  // Security headers on every response
  // ---------------------------------------------------------------------------
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: [
          { key: 'X-Frame-Options',          value: 'DENY' },
          { key: 'X-Content-Type-Options',   value: 'nosniff' },
          { key: 'X-XSS-Protection',         value: '1; mode=block' },
          { key: 'Referrer-Policy',           value: 'strict-origin-when-cross-origin' },
          {
            key: 'Permissions-Policy',
            value: 'camera=(), microphone=(), geolocation=()',
          },
          {
            key: 'Content-Security-Policy',
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-eval' 'unsafe-inline'",  // unsafe-eval needed by Next.js dev
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob:",
              "font-src 'self'",
              "connect-src 'self' " + (process.env.NEXT_PUBLIC_API_URL || ''),
              "frame-ancestors 'none'",
            ].join('; '),
          },
        ],
      },
    ];
  },

  // ---------------------------------------------------------------------------
  // Rewrites — proxy /api/* to backend (avoids CORS in browser)
  // Only used in production; dev uses Next.js API routes as BFF
  // ---------------------------------------------------------------------------
  async rewrites() {
    const apiUrl = process.env.BACKEND_URL || 'http://localhost:8000';
    return process.env.PROXY_BACKEND === 'true'
      ? [
          {
            source: '/backend/:path*',
            destination: `${apiUrl}/:path*`,
          },
        ]
      : [];
  },

  // ---------------------------------------------------------------------------
  // Environment variables exposed to the browser (safe to be public)
  // ---------------------------------------------------------------------------
  env: {
    NEXT_PUBLIC_APP_VERSION:  process.env.npm_package_version || '1.0.0',
    NEXT_PUBLIC_ENVIRONMENT:  process.env.NODE_ENV,
  },

  // ---------------------------------------------------------------------------
  // Images — allow S3 presigned URL domains
  // ---------------------------------------------------------------------------
  images: {
    remotePatterns: [
      {
        protocol: 'https',
        hostname: '*.amazonaws.com',
      },
    ],
  },

  // ---------------------------------------------------------------------------
  // Webpack — bundle optimisation
  // ---------------------------------------------------------------------------
  webpack: (config, { isServer }) => {
    if (!isServer) {
      // Prevent server-only modules from being bundled for the client
      config.resolve.fallback = {
        ...config.resolve.fallback,
        fs: false,
        net: false,
        tls: false,
      };
    }
    return config;
  },

  // ---------------------------------------------------------------------------
  // Experimental — optimise React server components
  // ---------------------------------------------------------------------------
  experimental: {
    optimizePackageImports: ['lucide-react', 'recharts'],
  },
};

// Sentry integration — only active when SENTRY_DSN is set
const sentryWebpackPluginOptions = {
  silent: true,
  org:     process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
};

module.exports =
  process.env.SENTRY_DSN
    ? withSentryConfig(nextConfig, sentryWebpackPluginOptions)
    : nextConfig;
