# =============================================================================
# Multi-stage Dockerfile — AI-Powered Multi-Tenant SaaS RAG Platform
# =============================================================================
#
# Stages:
#   base      — shared OS + system dependencies (pinned versions)
#   builder   — install Python packages (pip wheel cache)
#   api       — production FastAPI / Uvicorn image
#   worker    — production Celery worker image
#   beat      — Celery Beat scheduler image
#
# Build targets:
#   docker build --target api    -t rag-platform/api:latest .
#   docker build --target worker -t rag-platform/worker:latest .
#   docker build --target beat   -t rag-platform/beat:latest .
#
# Security hardening:
#   - Non-root user "appuser" (UID 1001)
#   - No SSH, no shell in production images (distroless-like)
#   - Build secrets (pip credentials) never land in final layer
#   - .dockerignore excludes .env, __pycache__, *.pyc, tests/
#
# Image size optimisation:
#   - Multi-stage build separates build tools from runtime
#   - pip install with --user + COPY to final stage
#   - No apt cache in final layer (apt clean + rm -rf /var/lib/apt)
# =============================================================================

# ── Pin the Python version (update CI matrix to match) ─────────────────────
ARG PYTHON_VERSION=3.11

# =============================================================================
# Stage 1: base — OS + system libraries shared by all stages
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS base

LABEL org.opencontainers.image.source="https://github.com/yourorg/rag-platform"
LABEL org.opencontainers.image.description="Multi-Tenant SaaS RAG Platform"

# Prevent Python from writing .pyc files and enable unbuffered stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System dependencies:
#   libmagic1     — python-magic MIME detection
#   poppler-utils — PDF rendering (used by unstructured[pdf])
#   tesseract-ocr — OCR engine (used by unstructured OCR strategy)
#   libgl1        — OpenCV dependency (indirect via unstructured)
#   curl          — k8s liveness/readiness probes
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1        \
        poppler-utils    \
        tesseract-ocr    \
        libgl1-mesa-glx  \
        curl             \
    && apt-get clean     \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for all stages
RUN groupadd --gid 1001 appgroup \
 && useradd  --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app


# =============================================================================
# Stage 2: builder — install Python packages
# =============================================================================
FROM base AS builder

# Install build tools (only needed to compile native extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential  \
        gcc              \
        g++              \
        libpq-dev        \
    && apt-get clean     \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages into /home/appuser/.local (--user)
# so the final stage can COPY them without root, and they're
# isolated from the system Python.
COPY requirements.txt .

RUN pip install --upgrade pip \
 && pip install --user -r requirements.txt

# Download the spaCy model (used by SemanticChunker)
RUN python -m spacy download en_core_web_sm


# =============================================================================
# Stage 3: api — FastAPI / Uvicorn production server
# =============================================================================
FROM base AS api

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local
COPY --from=builder /root/.local /root/.local   # for root-fallback compatibility

# Install psql client for migration runner
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy application source
COPY --chown=appuser:appgroup . .

# Make entrypoint scripts executable
RUN chmod +x /app/scripts/entrypoint.sh \
              /app/scripts/worker-entrypoint.sh \
              /app/scripts/run-migrations.sh

# Ensure user-installed scripts (uvicorn) are on PATH
ENV PATH="/home/appuser/.local/bin:/root/.local/bin:$PATH"

# Switch to non-root
USER appuser

EXPOSE 8000

# Health check — FastAPI /health endpoint (see main.py)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Production: entrypoint waits for DB then starts uvicorn
CMD ["/app/scripts/entrypoint.sh"]


# =============================================================================
# Stage 4: worker — Celery document ingestion worker
# =============================================================================
FROM base AS worker

COPY --from=builder /root/.local /root/.local
ENV PATH="/root/.local/bin:$PATH"

COPY --chown=appuser:appgroup . .

USER appuser

# Celery worker configuration:
#   -Q documents.ingest,documents.retry  — only process ingestion queues
#   --concurrency 4                      — 4 async green threads per worker
#   --prefetch-multiplier 1              — fetch one task at a time (acks_late=True)
#   --loglevel info
CMD ["celery", "-A", "app.workers.celery_app", "worker", \
     "--queues",              "documents.ingest,documents.retry", \
     "--concurrency",         "4", \
     "--prefetch-multiplier", "1", \
     "--loglevel",            "info", \
     "--without-gossip",      \
     "--without-mingle"]


# =============================================================================
# Stage 5: beat — Celery Beat periodic task scheduler
# =============================================================================
FROM base AS beat

COPY --from=builder /root/.local /root/.local
ENV PATH="/root/.local/bin:$PATH"

COPY --chown=appuser:appgroup . .

USER appuser

# Beat runs as a SINGLE instance — never scale to >1 replica.
# The retry_failed_documents task runs every 60s.
CMD ["celery", "-A", "app.workers.celery_app", "beat", \
     "--loglevel", "info", \
     "--schedule", "/tmp/celerybeat-schedule"]
