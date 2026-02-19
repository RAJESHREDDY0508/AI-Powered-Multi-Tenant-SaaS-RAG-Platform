"""
Composed FastAPI Dependencies

Combines auth + DB session + vector store into single injectable objects.
Route handlers import from here — never from auth/token, db/session, or
vectorstore/factory directly.

This is the single wiring point for the entire request context.
"""

from __future__ import annotations

from typing import Annotated, AsyncGenerator
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.token import TokenPayload, get_current_user
from app.db.session import get_db
from app.storage.s3 import S3StorageService, TenantStorageConfig
from app.vectorstore.base import VectorStoreBase
from app.vectorstore.factory import get_vector_store


# ---------------------------------------------------------------------------
# 1. Authenticated tenant DB session
#    Sets RLS context: app.current_tenant_id = user.tenant_id
# ---------------------------------------------------------------------------

async def get_tenant_db(
    user: Annotated[TokenPayload, Depends(get_current_user)],
) -> AsyncGenerator[AsyncSession, None]:
    """
    Yields a PostgreSQL session with RLS scoped to the authenticated tenant.
    Every query through this session automatically filters by tenant_id.
    """
    async for session in get_db(tenant_id=user.tenant_id):
        yield session


# ---------------------------------------------------------------------------
# 2. Tenant-scoped S3 service
#    Prefix + KMS key are resolved from app settings keyed by tenant_id.
#    In a future phase, kms_key_arn will be looked up from the tenants table.
# ---------------------------------------------------------------------------

async def get_tenant_storage(
    user: Annotated[TokenPayload, Depends(get_current_user)],
) -> S3StorageService:
    """
    Returns an S3StorageService pre-configured for the authenticated tenant.
    All S3 operations are automatically prefixed to tenants/<tenant_id>/.
    """
    from app.core.config import settings

    config = TenantStorageConfig(
        tenant_id=user.tenant_id,
        kms_key_arn=settings.s3_default_kms_key_arn,  # per-tenant key added in provisioner phase
    )
    return S3StorageService(tenant_config=config)


# ---------------------------------------------------------------------------
# 3. Tenant-scoped vector store
#    Namespace/collection is keyed to tenant_id — no cross-tenant access.
# ---------------------------------------------------------------------------

def get_tenant_vector_store(
    user: Annotated[TokenPayload, Depends(get_current_user)],
) -> VectorStoreBase:
    """
    Returns a VectorStoreBase implementation scoped to the authenticated tenant.
    Backend (Pinecone | Weaviate) is selected from settings.
    """
    return get_vector_store(tenant_id=user.tenant_id)


# ---------------------------------------------------------------------------
# Type aliases for cleaner route signatures
# ---------------------------------------------------------------------------

TenantDB      = Annotated[AsyncSession,    Depends(get_tenant_db)]
TenantStorage = Annotated[S3StorageService, Depends(get_tenant_storage)]
TenantVectors = Annotated[VectorStoreBase,  Depends(get_tenant_vector_store)]
CurrentUser   = Annotated[TokenPayload,     Depends(get_current_user)]
