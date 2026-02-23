# Frontend Deployment Guide

## Local Development

```bash
# 1. Install dependencies
cd frontend
npm install

# 2. Copy env template
cp .env.example .env.local
# Edit .env.local — set BACKEND_URL to your FastAPI instance

# 3. Start dev server
npm run dev
# → http://localhost:3000
```

## Docker (single container)

```bash
# Build
docker build -t rag-platform/frontend:latest ./frontend

# Run
docker run -p 3000:3000 \
  -e BACKEND_URL=http://your-api:8000 \
  -e NODE_ENV=production \
  rag-platform/frontend:latest
```

## Full Stack (Docker Compose)

```bash
# From repo root
cp .env.example .env          # fill in OPENAI_API_KEY, AUTH_ISSUER, etc.

# Build + start all services
docker compose up --build -d

# Access
# Frontend:  http://localhost:3000
# API:       http://localhost:8000/docs
# MinIO UI:  http://localhost:9001
# Phoenix:   http://localhost:6006
```

## Production Kubernetes

The frontend can be deployed alongside the backend using the existing k8s manifests.
Create an additional `k8s/frontend-deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
  namespace: rag-platform
spec:
  replicas: 2
  selector:
    matchLabels: { app: frontend }
  template:
    metadata:
      labels: { app: frontend }
    spec:
      containers:
        - name: frontend
          image: your-registry/rag-platform/frontend:latest
          ports:
            - containerPort: 3000
          env:
            - name: BACKEND_URL
              value: "http://api-service:8000"
            - name: NODE_ENV
              value: production
          resources:
            requests: { cpu: 100m, memory: 256Mi }
            limits:   { cpu: 500m, memory: 512Mi }
          readinessProbe:
            httpGet: { path: /, port: 3000 }
            initialDelaySeconds: 10
```

## Environment Variables Reference

| Variable                | Where used      | Required | Description                                  |
|-------------------------|-----------------|----------|----------------------------------------------|
| `BACKEND_URL`           | Server-side     | ✅       | Internal URL of the FastAPI backend           |
| `NODE_ENV`              | Node.js         | prod     | Set to `production` in deployments            |
| `NEXT_PUBLIC_APP_URL`   | Client          | No       | Public URL of the frontend                    |
| `NEXT_PUBLIC_APP_VERSION` | Client        | No       | Git tag injected at build time                |
| `NEXT_PUBLIC_SENTRY_DSN` | Client         | No       | Sentry DSN for client-side errors             |
| `SENTRY_DSN`            | Server-side     | No       | Sentry DSN for server-side errors             |
| `SENTRY_AUTH_TOKEN`     | Build only      | No       | For source-map upload                         |

## Architecture Notes

### BFF (Backend For Frontend) Pattern
- `/api/auth/login`   → proxies to FastAPI, sets `refresh_token` as httpOnly cookie
- `/api/auth/refresh` → reads cookie, rotates token, returns new `access_token` in JSON
- `/api/auth/logout`  → clears cookies, calls backend revocation endpoint

### Token Security Model
- **Access token**: Zustand memory only (never localStorage/sessionStorage) — XSS-safe
- **Refresh token**: httpOnly, Secure, SameSite=Strict cookie — CSRF-safe + JS-inaccessible
- **Tenant ID**: Non-sensitive public cookie, read by Next.js middleware for header injection

### SSE Streaming
- Uses `fetch()` + `ReadableStream` (NOT `EventSource`) to support POST + custom headers
- `AbortController` used for stream cancellation (Stop button)
- Token buffer accumulates across chunk boundaries before dispatching events

### Middleware Route Protection
- Edge-compatible (`middleware.ts`) — reads `refresh_token` cookie existence
- JWT *not* verified at edge (no Node crypto) — actual validation happens on the backend
- Unauthenticated → redirect to `/login?next=<original-path>`
- Authenticated + login page → redirect to `/dashboard`
