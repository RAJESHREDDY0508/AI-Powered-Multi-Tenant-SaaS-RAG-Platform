"""
ORM Models — Evaluation Metrics and Token Usage

Two tables:

  saas.evaluation_results
    Stores per-query RAGAS-style metric scores produced by the LLM-as-judge
    evaluator. Powers the internal quality dashboard.

  saas.token_usage_logs
    Append-only log of token consumption per (tenant, user, model, month).
    Used for per-tenant billing reports and cost dashboards.
    A monthly-aggregated view can be built on top for fast billing queries.

Design notes:
  - evaluation_results is written asynchronously (async=True in Celery) so
    it does NOT block the query response path.
  - token_usage_logs uses an UPSERT pattern in CostTracker:
      INSERT ... ON CONFLICT DO UPDATE SET tokens += excluded.tokens
    This gives monthly roll-ups cheaply without a separate aggregation job.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.documents import Base


# ---------------------------------------------------------------------------
# EvaluationResult — per-query RAGAS metric scores
# ---------------------------------------------------------------------------

class EvaluationResult(Base):
    """
    RAGAS-style quality metrics for one RAG response.

    Written asynchronously by the background evaluator after each query.
    Three core metrics (all in range 0.0 – 1.0):

      faithfulness       — Is the answer grounded in the retrieved context?
                           (LLM-as-judge checks if every claim can be traced
                           to a passage in the context chunks.)

      answer_relevance   — Does the answer actually address the question?
                           (Embed the answer, compare to the original question.)

      context_precision  — Are the retrieved chunks truly relevant to the question?
                           (LLM-as-judge rates each chunk individually.)
    """

    __tablename__ = "evaluation_results"
    __table_args__ = (
        CheckConstraint("faithfulness     BETWEEN 0.0 AND 1.0", name="eval_faithfulness_range"),
        CheckConstraint("answer_relevance BETWEEN 0.0 AND 1.0", name="eval_answer_rel_range"),
        CheckConstraint("context_precision BETWEEN 0.0 AND 1.0", name="eval_ctx_prec_range"),
        Index("idx_eval_tenant_id",    "tenant_id"),
        Index("idx_eval_created_at",   "created_at"),
        Index("idx_eval_model",        "model_used"),
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
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("saas.users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Correlation back to the API request
    request_id: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
        comment="UUID from GatewayResponse.request_id — links to audit log",
    )

    # Input / output stored for replay and debugging
    question:     Mapped[str]           = mapped_column(Text, nullable=False)
    answer:       Mapped[str]           = mapped_column(Text, nullable=False)
    context_docs: Mapped[list]          = mapped_column(   # list of chunk texts
        JSONB, nullable=False, default=list, server_default="[]",
        comment="Serialised list of retrieved chunk texts used as context",
    )

    # RAGAS metrics (NULL = not yet evaluated)
    faithfulness:      Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    answer_relevance:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    context_precision: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Composite score: simple average of the three metrics
    composite_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="(faithfulness + answer_relevance + context_precision) / 3",
    )

    # Infrastructure metadata
    model_used:    Mapped[str]           = mapped_column(Text, nullable=False)
    provider:      Mapped[str]           = mapped_column(Text, nullable=False)
    latency_ms:    Mapped[float]         = mapped_column(Float, nullable=False)
    chunks_used:   Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    input_tokens:  Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int]           = mapped_column(Integer, nullable=False, default=0)

    # Status of the evaluation itself
    eval_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending",
        comment="pending | evaluating | done | failed",
    )
    eval_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"<EvaluationResult id={self.id} faithfulness={self.faithfulness} "
            f"relevance={self.answer_relevance} precision={self.context_precision}>"
        )


# ---------------------------------------------------------------------------
# TokenUsageLog — monthly per-tenant billing aggregation
# ---------------------------------------------------------------------------

class TokenUsageLog(Base):
    """
    Append-only token usage log for billing and cost dashboards.

    One row per (tenant_id, user_id, model, provider, month_year).
    CostTracker uses INSERT ... ON CONFLICT DO UPDATE to upsert-aggregate.

    The cost_usd is stored as an exact decimal (NUMERIC) to avoid floating
    point errors in billing calculations.
    """

    __tablename__ = "token_usage_logs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "model", "provider", "month_year",
            name="uq_token_usage_period",
        ),
        Index("idx_token_usage_tenant_month", "tenant_id", "month_year"),
        Index("idx_token_usage_model",        "model"),
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

    model:      Mapped[str] = mapped_column(Text, nullable=False)
    provider:   Mapped[str] = mapped_column(Text, nullable=False)

    # "YYYY-MM"  e.g. "2025-01"
    month_year: Mapped[str] = mapped_column(String(7), nullable=False)

    # Accumulated counts (upserted incrementally)
    input_tokens:  Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    request_count: Mapped[int] = mapped_column(Integer,    nullable=False, default=0, server_default="0")

    # Exact cost in USD (NUMERIC avoids float imprecision)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(precision=12, scale=6),
        nullable=False,
        default=0.0,
        server_default="0.000000",
        comment="USD cost = (input_tokens/1000 × price_in) + (output_tokens/1000 × price_out)",
    )

    first_request_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    last_request_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<TokenUsageLog tenant={self.tenant_id} model={self.model} "
            f"month={self.month_year} cost=${self.cost_usd}>"
        )
