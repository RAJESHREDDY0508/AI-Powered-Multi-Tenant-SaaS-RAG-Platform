"""
SQLAlchemy ORM Models — Documents & Audit Logs

These models map directly to the tables created in migrations 001 and 002.
Using SQLAlchemy Core mapped classes (2.x style) for full async support.

RLS note: Row-Level Security is enforced at the PostgreSQL level via the
app.current_tenant_id GUC set by db/session.py. The ORM models do NOT
add WHERE tenant_id clauses — RLS handles that transparently.

Schema: saas (set via __table_args__)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Declarative base — shared across all models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Document model — saas.documents
# ---------------------------------------------------------------------------

class Document(Base):
    """
    Tracks a single uploaded file from upload → chunking → vector indexing.

    State machine (status column):
        pending    — file stored in S3, processing not yet started
        processing — Celery worker actively chunking + embedding
        ready      — vectors indexed in Pinecone/Weaviate, available for RAG
        failed     — unrecoverable pipeline error (see error_message)
        deleted    — soft-deleted; S3 object tagged, vectors purged

    Checksum (md5_checksum) is used for deduplication within a tenant:
        UNIQUE(tenant_id, md5_checksum) prevents re-ingestion of identical files.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed', 'deleted')",
            name="documents_status_check",
        ),
        UniqueConstraint("tenant_id", "md5_checksum", name="uq_documents_tenant_checksum"),
        Index("idx_documents_tenant_id",  "tenant_id"),
        Index("idx_documents_status",     "tenant_id", "status"),
        Index("idx_documents_checksum",   "tenant_id", "md5_checksum"),
        {"schema": "saas"},
    )

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Tenant scope — never supplied by the client; always taken from JWT
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Uploader — the internal user UUID from saas.users
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.users.id", ondelete="SET NULL"),
        nullable=False,
    )

    # S3 reference
    s3_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Full S3 object key: tenants/<tenant_id>/documents/<doc_id>.<ext>",
    )
    filename: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Original sanitized filename provided by the client",
    )
    content_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="MIME type detected server-side (never trusted from client Content-Type)",
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="File size in bytes — validated against MAX_FILE_SIZE_BYTES",
    )

    # Deduplication — MD5 hex digest of the raw file content
    md5_checksum: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="MD5 hex digest for tenant-scoped deduplication",
    )

    # Ingestion state machine
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
        server_default="pending",
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Populated only when status='failed'",
    )

    # Chunk and vector tracking — updated incrementally by the worker
    chunk_count: Mapped[int]  = mapped_column(Integer, nullable=False, default=0, server_default="0")
    vector_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Client-supplied metadata (permissions, tags, display name, etc.)
    document_name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="User-provided display name (distinct from raw filename)",
    )
    doc_metadata: Mapped[dict] = mapped_column(
        "metadata",                 # PostgreSQL column name stays 'metadata'
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="document_permissions and any other client-supplied JSON metadata",
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<Document id={self.id} tenant={self.tenant_id} "
            f"status={self.status} file={self.filename!r}>"
        )


# ---------------------------------------------------------------------------
# Chunk model — saas.chunks
# ---------------------------------------------------------------------------

class Chunk(Base):
    """
    One text chunk extracted from a Document.
    vector_id references the record in Pinecone or Weaviate.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint(
            "vector_store IN ('pinecone', 'weaviate', 'faiss')",
            name="chunks_vector_store_check",
        ),
        UniqueConstraint("tenant_id", "document_id", "chunk_index", name="uq_chunks_position"),
        Index("idx_chunks_document_id", "document_id"),
        Index("idx_chunks_tenant_id",   "tenant_id"),
        Index("idx_chunks_vector_id",   "vector_id"),
        {"schema": "saas"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str]        = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    vector_id: Mapped[str]   = mapped_column(Text, nullable=False)
    vector_store: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="weaviate",
        server_default="weaviate",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# ---------------------------------------------------------------------------
# AuditLog model — saas.audit_logs
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """
    Append-only audit trail required for SOC2 compliance.

    The app_user PostgreSQL role has INSERT-only on this table (no UPDATE/DELETE),
    making the log tamper-evident at the database level.

    Written by the ingestion service for every upload attempt, including failures.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_logs_tenant_id",  "tenant_id"),
        Index("idx_audit_logs_user_id",    "user_id"),
        Index("idx_audit_logs_created_at", "created_at"),
        {"schema": "saas"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # What happened
    action: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="e.g. document.upload, document.delete, document.processing_failed",
    )
    resource: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="e.g. document:<uuid>",
    )

    # Structured payload — stores request metadata, checksums, S3 keys, etc.
    doc_metadata: Mapped[dict] = mapped_column(
        "metadata",                 # PostgreSQL column name stays 'metadata'
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    # Network context for security audits
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)

    # Outcome
    success: Mapped[bool]  = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} tenant={self.tenant_id} "
            f"action={self.action!r} success={self.success}>"
        )
