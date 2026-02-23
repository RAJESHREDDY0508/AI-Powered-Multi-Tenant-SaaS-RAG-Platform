# ============================================================================
# Multi-stage Dockerfile for Next.js 14 (standalone output)
# ============================================================================
# Stage 1 — deps: install node_modules
# Stage 2 — builder: compile + export standalone bundle
# Stage 3 — runner: minimal production image
# ============================================================================

# ── Stage 1: deps ────────────────────────────────────────────────────────────
FROM node:20-alpine AS deps
LABEL stage="deps"

RUN apk add --no-cache libc6-compat

WORKDIR /app

# Copy only manifests first for better layer caching
COPY package.json package-lock.json* ./

RUN npm ci --omit=dev


# ── Stage 2: builder ─────────────────────────────────────────────────────────
FROM node:20-alpine AS builder
LABEL stage="builder"

WORKDIR /app

# Copy deps from stage 1
COPY --from=deps /app/node_modules ./node_modules

# Copy full source
COPY . .

# Build-time env vars (values are public — no secrets here)
ARG NEXT_PUBLIC_APP_VERSION=1.0.0
ENV NEXT_PUBLIC_APP_VERSION=$NEXT_PUBLIC_APP_VERSION

# Disable telemetry
ENV NEXT_TELEMETRY_DISABLED=1

# Build the app (outputs to .next/standalone)
RUN npm run build


# ── Stage 3: runner ──────────────────────────────────────────────────────────
FROM node:20-alpine AS runner
LABEL stage="runner"

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# Non-root user for security
RUN addgroup --system --gid 1001 nodejs && \
    adduser  --system --uid 1001 nextjs

# Copy standalone bundle + static assets
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static    ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public          ./public

USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD wget -qO- http://localhost:3000/api/auth/refresh || exit 1

CMD ["node", "server.js"]
