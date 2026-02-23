"""
Evaluation Dashboard API — Quality Metrics Endpoints

Provides read-only access to RAGAS metric scores and usage summaries
for tenant admins and platform operators.

Endpoints:
  GET /api/v1/admin/evaluation/summary     — aggregate metrics for the tenant
  GET /api/v1/admin/evaluation/results     — paginated individual eval results
  GET /api/v1/admin/evaluation/cost        — monthly token usage / cost report
  POST /api/v1/admin/evaluation/trigger    — manually trigger re-evaluation of a query

All endpoints require role >= admin (tenant-scoped).
The /cost endpoint aggregates from saas.token_usage_logs.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_tenant_db
from app.auth.rbac import require_role
from app.auth.token import TokenPayload
from app.models.evaluation import EvaluationResult, TokenUsageLog

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/evaluation",
    tags=["Evaluation Dashboard"],
)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class MetricsSummary(BaseModel):
    """Aggregate RAGAS metrics for a tenant over a time window."""
    tenant_id:              str
    period_from:            datetime
    period_to:              datetime
    total_queries:          int
    evaluated_queries:      int
    avg_faithfulness:       Optional[float]
    avg_answer_relevance:   Optional[float]
    avg_context_precision:  Optional[float]
    avg_composite:          Optional[float]
    avg_latency_ms:         Optional[float]
    p95_latency_ms:         Optional[float]


class EvalResultSchema(BaseModel):
    """One evaluation result row."""
    id:                str
    request_id:        str
    question:          str
    answer:            str
    faithfulness:      Optional[float]
    answer_relevance:  Optional[float]
    context_precision: Optional[float]
    composite_score:   Optional[float]
    model_used:        str
    latency_ms:        float
    eval_status:       str
    created_at:        datetime

    class Config:
        from_attributes = True


class MonthlyUsageRow(BaseModel):
    """One monthly token usage row for the dashboard."""
    month_year:     str
    model:          str
    provider:       str
    input_tokens:   int
    output_tokens:  int
    request_count:  int
    cost_usd:       float


class CostReport(BaseModel):
    """Monthly cost breakdown for a tenant."""
    tenant_id:      str
    rows:           list[MonthlyUsageRow]
    total_cost_usd: float
    total_requests: int


# ---------------------------------------------------------------------------
# GET /summary  — aggregate metrics
# ---------------------------------------------------------------------------

@router.get(
    "/summary",
    response_model=MetricsSummary,
    summary="Aggregate RAGAS metrics for the tenant",
)
async def get_metrics_summary(
    token:     TokenPayload = Depends(require_role("admin")),
    db:        AsyncSession = Depends(get_tenant_db),
    days:      int          = Query(default=30, ge=1, le=365, description="Lookback window in days"),
) -> MetricsSummary:
    """
    Return aggregate quality metrics for the authenticated tenant
    over the last N days.

    Uses PostgreSQL aggregate functions for efficiency — no Python-side pagination.
    """
    tenant_id = UUID(str(token.tenant_id))

    # Window boundaries
    from datetime import timedelta, timezone
    now        = datetime.now(timezone.utc)
    period_from = now - timedelta(days=days)

    stmt = select(
        func.count(EvaluationResult.id).label("total"),
        func.count(EvaluationResult.faithfulness).label("evaluated"),
        func.avg(EvaluationResult.faithfulness).label("avg_faith"),
        func.avg(EvaluationResult.answer_relevance).label("avg_rel"),
        func.avg(EvaluationResult.context_precision).label("avg_prec"),
        func.avg(EvaluationResult.composite_score).label("avg_comp"),
        func.avg(EvaluationResult.latency_ms).label("avg_lat"),
        func.percentile_cont(0.95).within_group(
            EvaluationResult.latency_ms.asc()
        ).label("p95_lat"),
    ).where(
        and_(
            EvaluationResult.tenant_id == tenant_id,
            EvaluationResult.created_at >= period_from,
        )
    )

    row = (await db.execute(stmt)).one()

    def _round(v: float | None, n: int = 3) -> Optional[float]:
        return round(float(v), n) if v is not None else None

    return MetricsSummary(
        tenant_id             = str(tenant_id),
        period_from           = period_from,
        period_to             = now,
        total_queries         = row.total or 0,
        evaluated_queries     = row.evaluated or 0,
        avg_faithfulness      = _round(row.avg_faith),
        avg_answer_relevance  = _round(row.avg_rel),
        avg_context_precision = _round(row.avg_prec),
        avg_composite         = _round(row.avg_comp),
        avg_latency_ms        = _round(row.avg_lat, 1),
        p95_latency_ms        = _round(row.p95_lat, 1),
    )


# ---------------------------------------------------------------------------
# GET /results  — paginated individual results
# ---------------------------------------------------------------------------

@router.get(
    "/results",
    response_model=list[EvalResultSchema],
    summary="Paginated individual evaluation results",
)
async def get_eval_results(
    token:  TokenPayload = Depends(require_role("admin")),
    db:     AsyncSession = Depends(get_tenant_db),
    limit:  int          = Query(default=50, ge=1, le=200),
    offset: int          = Query(default=0, ge=0),
    model:  Optional[str] = Query(default=None, description="Filter by model name"),
    status: Optional[str] = Query(default=None, description="Filter by eval_status"),
) -> list[EvalResultSchema]:
    """
    Return individual evaluation results for the authenticated tenant.
    Sorted by created_at descending (most recent first).
    """
    tenant_id = UUID(str(token.tenant_id))

    conditions = [EvaluationResult.tenant_id == tenant_id]
    if model:
        conditions.append(EvaluationResult.model_used == model)
    if status:
        conditions.append(EvaluationResult.eval_status == status)

    stmt = (
        select(EvaluationResult)
        .where(and_(*conditions))
        .order_by(EvaluationResult.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()

    return [
        EvalResultSchema(
            id                = str(r.id),
            request_id        = r.request_id,
            question          = r.question,
            answer            = r.answer[:500],    # truncate for API response
            faithfulness      = r.faithfulness,
            answer_relevance  = r.answer_relevance,
            context_precision = r.context_precision,
            composite_score   = r.composite_score,
            model_used        = r.model_used,
            latency_ms        = r.latency_ms,
            eval_status       = r.eval_status,
            created_at        = r.created_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /cost  — monthly token usage and billing report
# ---------------------------------------------------------------------------

@router.get(
    "/cost",
    response_model=CostReport,
    summary="Monthly token usage and cost report",
)
async def get_cost_report(
    token:  TokenPayload  = Depends(require_role("admin")),
    db:     AsyncSession  = Depends(get_tenant_db),
    months: int           = Query(default=6, ge=1, le=24, description="Number of past months to include"),
) -> CostReport:
    """
    Return per-model monthly token usage and USD cost for the tenant.

    Use this to generate billing reports:
      - total cost per month
      - breakdown by model / provider
      - request volume trends
    """
    tenant_id = UUID(str(token.tenant_id))

    from datetime import timedelta, timezone, date
    now        = date.today()
    cutoff_str = (now.replace(day=1) - timedelta(days=months * 28)).strftime("%Y-%m")

    stmt = (
        select(TokenUsageLog)
        .where(
            and_(
                TokenUsageLog.tenant_id  == tenant_id,
                TokenUsageLog.month_year >= cutoff_str,
            )
        )
        .order_by(TokenUsageLog.month_year.desc(), TokenUsageLog.cost_usd.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()

    usage_rows = [
        MonthlyUsageRow(
            month_year    = r.month_year,
            model         = r.model,
            provider      = r.provider,
            input_tokens  = r.input_tokens,
            output_tokens = r.output_tokens,
            request_count = r.request_count,
            cost_usd      = float(r.cost_usd),
        )
        for r in rows
    ]

    total_cost     = sum(r.cost_usd for r in usage_rows)
    total_requests = sum(r.request_count for r in usage_rows)

    return CostReport(
        tenant_id      = str(tenant_id),
        rows           = usage_rows,
        total_cost_usd = round(total_cost, 6),
        total_requests = total_requests,
    )
