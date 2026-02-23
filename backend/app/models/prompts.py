"""
ORM Model — DB-Versioned Prompt Templates

Storing prompts in the database (not in source code) enables:

  1. Hot-patching   — update prompts without redeployment or git commit.
  2. A/B testing    — run multiple prompt variants concurrently with weighted
                      traffic splitting; measure quality via RAGAS metrics.
  3. Per-tenant     — each tenant can override the global default prompt.
  4. Rollback       — mark is_active=False to instantly deactivate a bad prompt.
  5. Auditability   — full change history via created_at + version number.

Table layout: saas.prompt_templates

A/B testing mechanics:
  Multiple rows sharing the same (tenant_id, name) can be active simultaneously.
  PromptManager selects one variant via weighted random sampling proportional
  to ab_weight.  Example:
    name="rag_system", version=3, ab_weight=80  →  gets 80% of traffic
    name="rag_system", version=4, ab_weight=20  →  gets 20% of traffic

Global vs tenant-scoped:
  tenant_id IS NULL  → global default (applies to all tenants without override)
  tenant_id IS SET   → per-tenant override (takes precedence over global)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.documents import Base


class PromptTemplate(Base):
    """
    A versioned system-prompt template stored in the database.

    The RAG pipeline loads the active template(s) for the tenant at query-time,
    with a short in-process TTL cache (60 s) to avoid per-request DB roundtrips.
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (
        CheckConstraint("ab_weight BETWEEN 0 AND 100", name="prompt_ab_weight_range"),
        CheckConstraint("version >= 1", name="prompt_version_positive"),
        UniqueConstraint(
            "tenant_id", "name", "version",
            name="uq_prompt_tenant_name_version",
        ),
        Index("idx_prompt_active",    "name", "is_active"),
        Index("idx_prompt_tenant_id", "tenant_id"),
        {"schema": "saas"},
    )

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )

    # NULL → global default; non-NULL → tenant-specific override
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="NULL for global prompts; tenant UUID for per-tenant overrides",
    )

    # Logical name — identifies what this prompt is used for
    # e.g. "rag_system", "rag_system_finance", "citation_reminder"
    name: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Logical prompt slot name, e.g. 'rag_system'",
    )

    # Monotonically increasing per (tenant_id, name)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Prompt version number; increments on each edit",
    )

    # The actual Jinja/f-string template.
    # Available interpolation variables (resolved at query-time):
    #   {tenant_name}  — human-readable org name from JWT claims
    #   {context}      — retrieved chunk texts (injected by pipeline)
    #   {question}     — user's query (injected by pipeline)
    template_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The full system prompt template. Supports {tenant_name}, {context}, {question}.",
    )

    # A/B test controls
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Only active templates are eligible for selection",
    )
    ab_weight: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        server_default="100",
        comment="Relative traffic weight for A/B split (0-100). "
                "Weights within a (tenant, name) group are normalised.",
    )

    # Optional description for humans reviewing the prompt catalog
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable note: what changed from the previous version.",
    )

    # Who created this version (audit trail)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.users.id", ondelete="SET NULL"),
        nullable=True,
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
            f"<PromptTemplate name={self.name!r} version={self.version} "
            f"tenant={self.tenant_id} active={self.is_active}>"
        )
