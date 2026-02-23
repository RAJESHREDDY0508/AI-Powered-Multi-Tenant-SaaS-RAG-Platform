#!/usr/bin/env bash
##############################################################
# entrypoint.sh — API container startup
# Waits for DB, then starts uvicorn
##############################################################
set -euo pipefail

MAX_RETRIES=30
RETRY_INTERVAL=2

echo "=== RAG Platform API Startup ==="
echo "Environment: ${APP_ENV:-development}"

# ── Wait for PostgreSQL ───────────────────────────────────────
echo "Waiting for database..."
for i in $(seq 1 $MAX_RETRIES); do
  if python -c "
import asyncio, sys
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

# ── Wait for Redis ────────────────────────────────────────────
echo "Waiting for Redis..."
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

# ── Start API ─────────────────────────────────────────────────
echo "Starting Uvicorn..."
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${UVICORN_WORKERS:-2}" \
  --timeout-keep-alive 75 \
  --access-log \
  --log-level "${LOG_LEVEL:-info}"
