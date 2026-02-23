"""
Cost Tracker — Per-Tenant Token Usage Accounting

Records token consumption per (tenant, user, model, provider, month) using
PostgreSQL upsert semantics:

    INSERT ... ON CONFLICT (tenant_id, user_id, model, provider, month_year)
    DO UPDATE SET
        input_tokens  += EXCLUDED.input_tokens,
        output_tokens += EXCLUDED.output_tokens,
        request_count += 1,
        cost_usd      += EXCLUDED.cost_usd

This gives O(1) inserts without a separate nightly aggregation job, and a
single SELECT per month gives the full billing report.

Model pricing catalogue (USD per 1 000 tokens):
  All prices are public list prices. Update MODEL_PRICING when rates change.

Cost accuracy:
  - The token counts stored here are ESTIMATED (4 chars ≈ 1 token) because
    the actual counts from the OpenAI API response are not always accessible
    via LangChain's streaming interface.
  - For production billing, replace estimates with actual usage from the API
    response headers: x-ratelimit-remaining-tokens, usage.prompt_tokens, etc.

Admin API:
  See app/evaluation/dashboard.py  GET /admin/evaluation/cost
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_admin_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing catalogue (public list prices, USD per 1K tokens)
# ---------------------------------------------------------------------------

# (input_price_per_1k, output_price_per_1k)
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o":                                             (0.0050,  0.0150),
    "gpt-4o-mini":                                        (0.00015, 0.0006),
    "gpt-4-turbo":                                        (0.0100,  0.0300),
    "gpt-3.5-turbo":                                      (0.0005,  0.0015),
    # Anthropic via Bedrock
    "anthropic.claude-3-5-sonnet-20241022-v2:0":          (0.0030,  0.0150),
    "anthropic.claude-3-haiku-20240307-v1:0":             (0.00025, 0.00125),
    # Llama (local — zero cost)
    "llama3.1:8b":                                        (0.0,     0.0),
    "llama3.1:70b":                                       (0.0,     0.0),
}

_DEFAULT_PRICING = (0.001, 0.002)   # fallback for unknown models


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """
    Compute USD cost for a single LLM call.

    Returns a Decimal (exact arithmetic) to avoid floating-point errors
    when summing millions of micro-payments in billing reports.
    """
    price_in, price_out = MODEL_PRICING.get(model, _DEFAULT_PRICING)
    cost = (input_tokens / 1000.0 * price_in) + (output_tokens / 1000.0 * price_out)
    return Decimal(str(round(cost, 9)))


def _month_year() -> str:
    """Return the current month as 'YYYY-MM', e.g. '2025-01'."""
    return date.today().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Monthly usage dataclass (returned by get_monthly_usage)
# ---------------------------------------------------------------------------

from dataclasses import dataclass


@dataclass
class MonthlyUsageReport:
    """Aggregated token usage for one tenant-month."""
    tenant_id:      str
    month_year:     str
    total_input:    int
    total_output:   int
    total_requests: int
    total_cost_usd: float
    by_model:       list[dict]   # [{model, provider, input, output, cost_usd}]


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------

class CostTracker:
    """
    Records and queries per-tenant token usage.

    CostTracker uses get_admin_db() (bypasses RLS) because it needs to INSERT
    into saas.token_usage_logs without the tenant filter — the tenant_id is
    passed explicitly in the INSERT statement.

    Usage::

        tracker = CostTracker()
        await tracker.track_usage(
            tenant_id=tenant_uuid,
            model="gpt-4o",
            provider="openai",
            input_tokens=500,
            output_tokens=150,
        )
    """

    # -----------------------------------------------------------------------
    # Write path — called after every LLM call
    # -----------------------------------------------------------------------

    async def track_usage(
        self,
        tenant_id:     UUID,
        model:         str,
        provider:      str,
        input_tokens:  int,
        output_tokens: int,
        user_id:       Optional[UUID] = None,
        month_year:    Optional[str]  = None,
    ) -> None:
        """
        Upsert token usage into saas.token_usage_logs.

        Uses a PostgreSQL INSERT ... ON CONFLICT DO UPDATE for atomic
        increment — safe for concurrent workers.

        Args:
            tenant_id:     Tenant UUID (from JWT).
            model:         Model ID, e.g. "gpt-4o".
            provider:      Provider string, e.g. "openai", "aws_bedrock".
            input_tokens:  Prompt token count (estimated or actual).
            output_tokens: Completion token count (estimated or actual).
            user_id:       Optional — for per-user breakdowns.
            month_year:    Optional override, e.g. "2025-01". Defaults to current month.
        """
        period  = month_year or _month_year()
        cost    = _compute_cost(model, input_tokens, output_tokens)

        # Use raw SQL for the upsert — SQLAlchemy ORM doesn't natively
        # support DO UPDATE SET col += EXCLUDED.col
        sql = text("""
            INSERT INTO saas.token_usage_logs
                (tenant_id, user_id, model, provider, month_year,
                 input_tokens, output_tokens, request_count, cost_usd,
                 first_request_at, last_request_at)
            VALUES
                (:tenant_id, :user_id, :model, :provider, :month_year,
                 :input_tokens, :output_tokens, 1, :cost_usd,
                 now(), now())
            ON CONFLICT (tenant_id, user_id, model, provider, month_year)
            DO UPDATE SET
                input_tokens   = token_usage_logs.input_tokens  + EXCLUDED.input_tokens,
                output_tokens  = token_usage_logs.output_tokens + EXCLUDED.output_tokens,
                request_count  = token_usage_logs.request_count + 1,
                cost_usd       = token_usage_logs.cost_usd      + EXCLUDED.cost_usd,
                last_request_at = now()
        """)

        try:
            async for db in get_admin_db():
                await db.execute(sql, {
                    "tenant_id":     str(tenant_id),
                    "user_id":       str(user_id) if user_id else None,
                    "model":         model,
                    "provider":      provider,
                    "month_year":    period,
                    "input_tokens":  input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd":      cost,
                })
                await db.commit()
        except Exception as exc:
            # Cost tracking is non-critical — log and continue
            logger.error(
                "CostTracker | upsert failed (non-fatal) | tenant=%s model=%s: %s",
                tenant_id, model, exc,
            )

    # -----------------------------------------------------------------------
    # Read path — for billing / dashboard queries
    # -----------------------------------------------------------------------

    async def get_monthly_usage(
        self,
        tenant_id:  UUID,
        month_year: Optional[str] = None,
    ) -> MonthlyUsageReport:
        """
        Return aggregated token usage for a tenant-month.

        Args:
            tenant_id:  The tenant's UUID.
            month_year: Target month, e.g. "2025-01". Defaults to current month.

        Returns:
            MonthlyUsageReport with totals and per-model breakdown.
        """
        period = month_year or _month_year()

        sql = text("""
            SELECT model, provider,
                   SUM(input_tokens)  AS total_input,
                   SUM(output_tokens) AS total_output,
                   SUM(request_count) AS total_requests,
                   SUM(cost_usd)      AS total_cost
            FROM saas.token_usage_logs
            WHERE tenant_id = :tenant_id
              AND month_year = :month_year
            GROUP BY model, provider
            ORDER BY total_cost DESC
        """)

        rows_data: list[dict] = []
        total_in = total_out = total_req = 0
        total_cost = Decimal("0")

        try:
            async for db in get_admin_db():
                result = await db.execute(sql, {
                    "tenant_id":  str(tenant_id),
                    "month_year": period,
                })
                for row in result.mappings():
                    rows_data.append({
                        "model":        row["model"],
                        "provider":     row["provider"],
                        "input_tokens": int(row["total_input"] or 0),
                        "output_tokens":int(row["total_output"] or 0),
                        "requests":     int(row["total_requests"] or 0),
                        "cost_usd":     float(row["total_cost"] or 0),
                    })
                    total_in   += int(row["total_input"] or 0)
                    total_out  += int(row["total_output"] or 0)
                    total_req  += int(row["total_requests"] or 0)
                    total_cost += Decimal(str(row["total_cost"] or 0))
        except Exception as exc:
            logger.error("CostTracker | get_monthly_usage failed: %s", exc)

        return MonthlyUsageReport(
            tenant_id      = str(tenant_id),
            month_year     = period,
            total_input    = total_in,
            total_output   = total_out,
            total_requests = total_req,
            total_cost_usd = float(total_cost),
            by_model       = rows_data,
        )
