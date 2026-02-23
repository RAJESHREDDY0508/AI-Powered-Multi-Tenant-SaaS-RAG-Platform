#!/usr/bin/env bash
##############################################################
# worker-entrypoint.sh — Celery worker startup
# Waits for broker + DB, then starts celery worker
##############################################################
set -euo pipefail

MAX_RETRIES=30
RETRY_INTERVAL=2

echo "=== RAG Platform Celery Worker Startup ==="

# ── Wait for Redis broker ─────────────────────────────────────
echo "Waiting for Redis broker..."
for i in $(seq 1 $MAX_RETRIES); do
  if python -c "
import redis, os
url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
r = redis.from_url(url)
r.ping()
" 2>/dev/null; then
    echo "Redis is ready!"
    break
  fi
  echo "  Redis not ready (attempt $i/$MAX_RETRIES)..."
  sleep $RETRY_INTERVAL
done

# ── Wait for database ─────────────────────────────────────────
echo "Waiting for database..."
for i in $(seq 1 $MAX_RETRIES); do
  if python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
async def check():
    engine = create_async_engine('${DATABASE_URL}', pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.execute(__import__('sqlalchemy').text('SELECT 1'))
    await engine.dispose()
asyncio.run(check())
" 2>/dev/null; then
    echo "Database is ready!"
    break
  fi
  echo "  DB not ready (attempt $i/$MAX_RETRIES)..."
  sleep $RETRY_INTERVAL
done

# ── Start Celery worker ───────────────────────────────────────
echo "Starting Celery worker..."
exec python -m celery -A app.workers.celery_app worker \
  --loglevel="${LOG_LEVEL:-info}" \
  --concurrency="${CELERY_CONCURRENCY:-2}" \
  -Q ingestion \
  --without-gossip \
  --without-mingle
