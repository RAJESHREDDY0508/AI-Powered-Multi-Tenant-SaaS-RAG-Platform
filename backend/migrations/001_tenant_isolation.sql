-- =============================================================================
-- Migration 001: Tenant Isolation Foundation
-- Strategy: PostgreSQL Row-Level Security (RLS) — "Fortress" Model
--
-- Design:
--   - All tenant-scoped tables carry a tenant_id (FK to tenants).
--   - RLS is FORCED on every tenant-scoped table, meaning even the table
--     owner cannot bypass the policy unless they SET the app config var.
--   - The application sets app.current_tenant_id per-connection/transaction.
--   - A dedicated low-privilege role (app_user) is used by the API; it can
--     never disable RLS. The superuser/migration role is separate.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 0. Roles
-- ---------------------------------------------------------------------------

-- Application runtime role — least privilege, cannot alter RLS
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN PASSWORD 'changeme_in_production';
    END IF;
END
$$;

-- Migration/admin role (used only by Alembic / DBA)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_admin') THEN
        CREATE ROLE app_admin LOGIN PASSWORD 'changeme_in_production' CREATEROLE;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Schema
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS saas;
GRANT USAGE ON SCHEMA saas TO app_user;

-- ---------------------------------------------------------------------------
-- 2. Tenants table (not RLS-protected — the root of the hierarchy)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.tenants (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT        NOT NULL UNIQUE,          -- e.g. "acme-corp"
    name        TEXT        NOT NULL,
    plan        TEXT        NOT NULL DEFAULT 'free'   -- free | pro | enterprise
                            CHECK (plan IN ('free', 'pro', 'enterprise')),
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- app_user can only read tenants (needed for FK resolution & auth checks)
GRANT SELECT ON saas.tenants TO app_user;

-- ---------------------------------------------------------------------------
-- 3. Helper: current_tenant_id()
--    Reads the session-local GUC set by the application layer.
--    Returns NULL (not error) when the variable is absent so RLS
--    simply returns no rows — safe-fail, not crash.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION saas.current_tenant_id()
RETURNS UUID
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.current_tenant_id', TRUE), '')::UUID;
$$;

-- ---------------------------------------------------------------------------
-- 4. Users table (tenant-scoped)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.users (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES saas.tenants(id) ON DELETE CASCADE,
    email           TEXT        NOT NULL,
    hashed_password TEXT        NOT NULL,
    role            TEXT        NOT NULL DEFAULT 'member'
                                CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (tenant_id, email)   -- email unique per-tenant, not globally
);

-- RLS: enable + force (force = even table owner is subject to policy)
ALTER TABLE saas.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE saas.users FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON saas.users
    USING (tenant_id = saas.current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON saas.users TO app_user;

-- ---------------------------------------------------------------------------
-- 5. API Keys table (tenant-scoped, used for service-to-service auth)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.api_keys (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID        NOT NULL REFERENCES saas.tenants(id) ON DELETE CASCADE,
    user_id     UUID        NOT NULL REFERENCES saas.users(id)   ON DELETE CASCADE,
    key_hash    TEXT        NOT NULL UNIQUE,   -- SHA-256 of the raw key
    name        TEXT        NOT NULL,          -- human label, e.g. "CI pipeline"
    scopes      TEXT[]      NOT NULL DEFAULT '{}',
    last_used_at TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE saas.api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE saas.api_keys FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON saas.api_keys
    USING (tenant_id = saas.current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON saas.api_keys TO app_user;

-- ---------------------------------------------------------------------------
-- 6. Audit log (append-only, tenant-scoped)
--    Immutable record of every privileged action.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS saas.audit_logs (
    id          BIGSERIAL   PRIMARY KEY,
    tenant_id   UUID        NOT NULL REFERENCES saas.tenants(id) ON DELETE CASCADE,
    user_id     UUID        REFERENCES saas.users(id) ON DELETE SET NULL,
    action      TEXT        NOT NULL,   -- e.g. "document.delete"
    resource    TEXT,                   -- e.g. "doc_id=abc123"
    metadata    JSONB       NOT NULL DEFAULT '{}',
    ip_address  INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE saas.audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE saas.audit_logs FORCE ROW LEVEL SECURITY;

-- Audit logs: tenants can only INSERT and SELECT their own rows (no UPDATE/DELETE)
CREATE POLICY tenant_isolation ON saas.audit_logs
    USING (tenant_id = saas.current_tenant_id());

GRANT SELECT, INSERT ON saas.audit_logs TO app_user;   -- deliberately no UPDATE/DELETE

-- ---------------------------------------------------------------------------
-- 7. Updated_at trigger (shared utility)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION saas.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER set_updated_at
    BEFORE UPDATE ON saas.tenants
    FOR EACH ROW EXECUTE FUNCTION saas.set_updated_at();

CREATE TRIGGER set_updated_at
    BEFORE UPDATE ON saas.users
    FOR EACH ROW EXECUTE FUNCTION saas.set_updated_at();

-- ---------------------------------------------------------------------------
-- 8. Indexes
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_users_tenant_id      ON saas.users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_email          ON saas.users(email);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_id   ON saas.api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash    ON saas.api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_id ON saas.audit_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON saas.audit_logs(created_at DESC);

-- ---------------------------------------------------------------------------
-- Done
-- ---------------------------------------------------------------------------
COMMENT ON SCHEMA saas IS 'All application tables live here. RLS enforces tenant isolation at the DB engine level.';
