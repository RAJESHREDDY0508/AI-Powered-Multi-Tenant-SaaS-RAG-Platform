"""
Observability Package — Tracing + Cost Tracking

Provides:
  TracingConfig   — LangSmith / Arize Phoenix initialisation
  traced          — decorator for instrumenting async functions
  CostTracker     — per-tenant token usage accounting

Usage::

    # At app startup (in main.py lifespan):
    from app.observability.tracing import TracingConfig
    TracingConfig.init()

    # To track costs after an LLM call:
    from app.observability.cost_tracker import CostTracker
    await CostTracker().track_usage(
        tenant_id=tenant_id,
        model="gpt-4o",
        provider="openai",
        input_tokens=500,
        output_tokens=150,
    )
"""

from app.observability.cost_tracker import CostTracker
from app.observability.tracing import TracingConfig, traced

__all__ = ["CostTracker", "TracingConfig", "traced"]
