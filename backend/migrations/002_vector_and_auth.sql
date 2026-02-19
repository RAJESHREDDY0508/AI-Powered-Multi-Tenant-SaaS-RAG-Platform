-- =============================================================================
-- Migration 002: Vector Metadata + Auth Provider Tables
-- Tracks document ingestion state and links external OIDC identities to users.
-- All tables are tenant-scoped with RLS.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Documents table — tracks ingested files
--    The actual file lives in S3; this is the metadata + ingestion state.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.documents (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES saas.tenants(id) ON DELETE CASCADE,
    uploaded_by     UUID        NOT NULL REFERENCES saas.users(id)   ON DELETE SET NULL,

    -- S3 reference
    s3_key          TEXT        NOT NULL,             -- e.g. tenants/<id>/documents/report.pdf
    filename        TEXT        NOT NULL,             -- original filename
    content_type    TEXT        NOT NULL,
    size_bytes      BIGINT      NOT NULL,

    -- Ingestion pipeline state machine
    status          TEXT        NOT NULL DEFAULT 'pending'
                                CHECK (status IN (
                                    'pending',        -- uploaded, not yet processed
                                    'processing',     -- chunking + embedding in progress
                                    'ready',          -- vectors indexed, available for RAG
                                    'failed',         -- pipeline error
                                    'deleted'         -- soft-deleted
                                )),
    error_message   TEXT,                             -- populated on 'failed'

    -- Chunk tracking
    chunk_count     INT         NOT NULL DEFAULT 0,   -- total chunks created
    vector_count    INT         NOT NULL DEFAULT 0,   -- vectors successfully indexed

    -- Metadata
    metadata        JSONB       NOT NULL DEFAULT '{}',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE saas.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE saas.documents FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON saas.documents
    USING (tenant_id = saas.current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON saas.documents TO app_user;

CREATE TRIGGER set_updated_at
    BEFORE UPDATE ON saas.documents
    FOR EACH ROW EXECUTE FUNCTION saas.set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. Chunks table — tracks individual text chunks and their vector IDs
--    Vector payloads live in Pinecone/Weaviate; this table is the index.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.chunks (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES saas.tenants(id) ON DELETE CASCADE,
    document_id     UUID        NOT NULL REFERENCES saas.documents(id) ON DELETE CASCADE,

    chunk_index     INT         NOT NULL,             -- 0-based position within the document
    text            TEXT        NOT NULL,             -- raw chunk text
    token_count     INT         NOT NULL DEFAULT 0,

    -- Vector store reference
    vector_id       TEXT        NOT NULL,             -- ID in Pinecone/Weaviate
    vector_store    TEXT        NOT NULL DEFAULT 'weaviate'  -- which backend stored it

                                CHECK (vector_store IN ('pinecone', 'weaviate', 'faiss')),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, document_id, chunk_index)
);

ALTER TABLE saas.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE saas.chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON saas.chunks
    USING (tenant_id = saas.current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON saas.chunks TO app_user;

-- ---------------------------------------------------------------------------
-- 3. Identity providers — links external OIDC/SAML identities to internal users
--    Supports Cognito, Auth0, and future enterprise SSO providers.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.identity_providers (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES saas.tenants(id) ON DELETE CASCADE,

    provider        TEXT        NOT NULL
                                CHECK (provider IN ('cognito', 'auth0', 'okta', 'azure_ad')),
    issuer_url      TEXT        NOT NULL,             -- OIDC issuer
    audience        TEXT        NOT NULL,             -- client_id / API identifier
    jwks_uri        TEXT        NOT NULL,             -- public key endpoint

    -- Provider-specific config (stored encrypted in prod via KMS)
    config          JSONB       NOT NULL DEFAULT '{}',

    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, provider)
);

ALTER TABLE saas.identity_providers ENABLE ROW LEVEL SECURITY;
ALTER TABLE saas.identity_providers FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON saas.identity_providers
    USING (tenant_id = saas.current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON saas.identity_providers TO app_user;

-- ---------------------------------------------------------------------------
-- 4. External identities — maps provider sub → internal user
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.external_identities (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES saas.tenants(id) ON DELETE CASCADE,
    user_id         UUID        NOT NULL REFERENCES saas.users(id)   ON DELETE CASCADE,
    provider_id     UUID        NOT NULL REFERENCES saas.identity_providers(id) ON DELETE CASCADE,

    provider_sub    TEXT        NOT NULL,             -- subject claim from JWT
    provider_email  TEXT,

    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (provider_id, provider_sub)               -- one user per sub per provider
);

ALTER TABLE saas.external_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE saas.external_identities FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON saas.external_identities
    USING (tenant_id = saas.current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON saas.external_identities TO app_user;

-- ---------------------------------------------------------------------------
-- 5. Tenant storage config — stores provisioned AWS ARNs per tenant
--    Populated by the TenantStorageProvisioner after new tenant signup.
-- ---------------------------------------------------------------------------

ALTER TABLE saas.tenants
    ADD COLUMN IF NOT EXISTS kms_key_arn    TEXT,
    ADD COLUMN IF NOT EXISTS iam_role_arn   TEXT,
    ADD COLUMN IF NOT EXISTS s3_prefix      TEXT;

-- ---------------------------------------------------------------------------
-- 6. Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_documents_tenant_id  ON saas.documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_documents_status     ON saas.documents(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id   ON saas.chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant_id     ON saas.chunks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_chunks_vector_id     ON saas.chunks(vector_id);
CREATE INDEX IF NOT EXISTS idx_ext_ids_provider_sub ON saas.external_identities(provider_id, provider_sub);
