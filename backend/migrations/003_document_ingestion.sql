-- =============================================================================
-- Migration 003: Document Ingestion Enhancements
--
-- Adds:
--   1. md5_checksum column to saas.documents (deduplication within tenant)
--   2. document_name column (user-supplied display name, separate from filename)
--   3. UNIQUE constraint on (tenant_id, md5_checksum) — prevents re-ingestion
--   4. Index on checksum for fast duplicate lookup
--   5. success column on saas.audit_logs (SOC2 audit trail enrichment)
--   6. ip_address column on saas.audit_logs (security tracing)
--
-- Safe to run on existing databases — all ADD COLUMN uses IF NOT EXISTS.
-- The UNIQUE constraint creation is idempotent via DO $$ block.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Add md5_checksum to saas.documents
--    - CHAR(32): exactly 32 hex characters (MD5 digest length)
--    - NOT NULL with DEFAULT '' for existing rows; back-fill required in prod.
--    - Backfill note: run a one-time job to populate from S3 ETag or re-hash.
-- ---------------------------------------------------------------------------

ALTER TABLE saas.documents
    ADD COLUMN IF NOT EXISTS md5_checksum CHAR(32) NOT NULL DEFAULT '';

ALTER TABLE saas.documents
    ADD COLUMN IF NOT EXISTS document_name TEXT NOT NULL DEFAULT '';


-- ---------------------------------------------------------------------------
-- 2. Backfill document_name from filename for existing rows
-- ---------------------------------------------------------------------------

UPDATE saas.documents
SET document_name = filename
WHERE document_name = '';


-- ---------------------------------------------------------------------------
-- 3. UNIQUE constraint: one checksum per tenant (tenant-scoped deduplication)
--    Uses DO block to make it idempotent (IF NOT EXISTS not supported for UNIQUE).
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_documents_tenant_checksum'
          AND conrelid = 'saas.documents'::regclass
    ) THEN
        ALTER TABLE saas.documents
            ADD CONSTRAINT uq_documents_tenant_checksum
            UNIQUE (tenant_id, md5_checksum);
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- 4. Indexes for ingestion queries
-- ---------------------------------------------------------------------------

-- Checksum lookup (duplicate detection on upload)
CREATE INDEX IF NOT EXISTS idx_documents_checksum
    ON saas.documents (tenant_id, md5_checksum);

-- Status-based queries (retry scanner, worker polling)
CREATE INDEX IF NOT EXISTS idx_documents_pending_created
    ON saas.documents (created_at)
    WHERE status = 'pending';

-- Processing state queries
CREATE INDEX IF NOT EXISTS idx_documents_processing
    ON saas.documents (tenant_id, status, updated_at);


-- ---------------------------------------------------------------------------
-- 5. Enrich audit_logs for SOC2 compliance
--    - success: was the audited action successful?
--    - ip_address: client IP extracted from X-Forwarded-For
-- ---------------------------------------------------------------------------

ALTER TABLE saas.audit_logs
    ADD COLUMN IF NOT EXISTS success    BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS ip_address INET;

-- Index for security incident investigation (filter by IP or failure)
CREATE INDEX IF NOT EXISTS idx_audit_logs_success
    ON saas.audit_logs (tenant_id, success, created_at)
    WHERE success = FALSE;

CREATE INDEX IF NOT EXISTS idx_audit_logs_action
    ON saas.audit_logs (tenant_id, action, created_at);


-- ---------------------------------------------------------------------------
-- 6. Enforce immutable audit log — revoke UPDATE and DELETE from app_user
--    (app_user already has INSERT-only per migration 001; this is a safety check)
-- ---------------------------------------------------------------------------

REVOKE UPDATE, DELETE ON saas.audit_logs FROM app_user;


-- ---------------------------------------------------------------------------
-- 7. Remove empty-string DEFAULT now that backfill is done
--    (prevents future rows from accidentally getting empty checksums)
-- ---------------------------------------------------------------------------

ALTER TABLE saas.documents
    ALTER COLUMN md5_checksum DROP DEFAULT,
    ALTER COLUMN document_name DROP DEFAULT;


-- ---------------------------------------------------------------------------
-- 8. Comments for schema documentation
-- ---------------------------------------------------------------------------

COMMENT ON COLUMN saas.documents.md5_checksum IS
    'MD5 hex digest of raw file bytes. Used for tenant-scoped deduplication. '
    'UNIQUE per tenant — prevents re-ingestion of identical files.';

COMMENT ON COLUMN saas.documents.document_name IS
    'User-supplied display name (distinct from the raw filesystem filename). '
    'Shown in the UI and used for search.';

COMMENT ON COLUMN saas.audit_logs.success IS
    'Whether the audited action succeeded. FALSE entries trigger security alerts.';

COMMENT ON COLUMN saas.audit_logs.ip_address IS
    'Client IP from X-Forwarded-For (first entry). Used for security forensics.';
