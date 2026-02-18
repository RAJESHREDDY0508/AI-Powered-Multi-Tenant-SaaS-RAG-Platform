"""
Database session management with tenant context injection.

Flow:
  1. FastAPI dependency resolves the current tenant_id from the JWT.
  2. get_db() is called — it opens a connection, sets the PostgreSQL GUC
     `app.current_tenant_id` for the lifetime of that transaction, then
     yields the session to the route handler.
  3. After the route completes (or raises), the session is closed and the
     connection is returned to the pool — GUC is reset automatically.

Security guarantee:
  Every SQL statement issued through this session is automatically filtered
  by RLS at the database engine level. The app layer never needs to manually
  add WHERE tenant_id = ? clauses.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,          # detect stale connections before use
    pool_recycle=3600,           # recycle connections every hour
    echo=settings.db_echo_sql,   # log SQL in dev; disable in prod
)

# Session factory — expire_on_commit=False keeps ORM objects usable after commit
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# ---------------------------------------------------------------------------
# Tenant context helper
# ---------------------------------------------------------------------------

async def _set_tenant_context(session: AsyncSession, tenant_id: UUID) -> None:
    """
    Set the PostgreSQL session-local variable that RLS policies read.

    Uses SET LOCAL so the GUC is automatically cleared when the transaction
    ends — no manual cleanup required.
    """
    await session.execute(
        text("SET LOCAL app.current_tenant_id = :tid"),
        {"tid": str(tenant_id)},
    )
    logger.debug("Tenant context set: %s", tenant_id)


async def _clear_tenant_context(session: AsyncSession) -> None:
    """Explicitly clear the tenant context (defensive; SET LOCAL handles it)."""
    await session.execute(text("RESET app.current_tenant_id"))


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def get_db(tenant_id: UUID) -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a tenant-scoped database session.

    Usage in a route:
        @router.get("/documents")
        async def list_docs(
            db: AsyncSession = Depends(get_tenant_db),
        ): ...

    The tenant_id is injected by the auth middleware before this is called.
    See app.core.dependencies for the composed dependency.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await _set_tenant_context(session, tenant_id)
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            # Transaction commits automatically on context exit (begin() block)


# ---------------------------------------------------------------------------
# Admin / migration session (NO tenant context — bypasses RLS intentionally)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_admin_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Session without RLS tenant context.

    ONLY for:
      - Database migrations
      - Tenant provisioning (creating a new tenant row)
      - Background system jobs that operate across tenants

    Never expose this to request handlers directly.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            yield session


# ---------------------------------------------------------------------------
# Health check helper
# ---------------------------------------------------------------------------

async def check_db_health() -> dict:
    """Ping the database; used by /health endpoint."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        return {"status": "error", "detail": str(exc)}
