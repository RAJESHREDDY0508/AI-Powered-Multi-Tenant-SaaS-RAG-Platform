#!/usr/bin/env bash
##############################################################
# run-migrations.sh
# Runs all SQL migration files in order against the DATABASE_URL.
# Called by the ECS migration task during CI/CD.
##############################################################
set -euo pipefail

echo "=== RAG Platform DB Migration Runner ==="
echo "Database: ${DATABASE_URL%%@*}@***"  # Mask password in logs

# Extract psql-compatible DSN from asyncpg URL
# postgresql+asyncpg://user:pass@host:5432/db → postgresql://user:pass@host:5432/db
PSQL_URL="${DATABASE_URL/postgresql+asyncpg/postgresql}"

MIGRATIONS_DIR="$(dirname "$0")/../migrations"

echo "Migration directory: $MIGRATIONS_DIR"

# Track which migrations have been applied (use a simple tracking table)
psql "$PSQL_URL" --no-password -c "
  CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
  );
" 2>&1 || true

echo "Checking pending migrations..."

for migration_file in "$MIGRATIONS_DIR"/*.sql; do
  filename=$(basename "$migration_file")

  # Check if already applied
  ALREADY_APPLIED=$(psql "$PSQL_URL" --no-password -t -c \
    "SELECT COUNT(*) FROM schema_migrations WHERE filename = '$filename';" 2>/dev/null | tr -d ' ')

  if [ "$ALREADY_APPLIED" = "1" ]; then
    echo "  [SKIP] $filename (already applied)"
    continue
  fi

  echo "  [RUN]  $filename"
  psql "$PSQL_URL" --no-password -f "$migration_file"

  # Mark as applied
  psql "$PSQL_URL" --no-password -c \
    "INSERT INTO schema_migrations (filename) VALUES ('$filename') ON CONFLICT DO NOTHING;"

  echo "  [DONE] $filename"
done

echo ""
echo "=== All migrations complete ==="

# List applied migrations
psql "$PSQL_URL" --no-password -c \
  "SELECT filename, applied_at FROM schema_migrations ORDER BY applied_at;"
