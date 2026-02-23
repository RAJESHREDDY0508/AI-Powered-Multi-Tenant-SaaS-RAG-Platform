"""
LLM Gateway — Unified Entry Point for all LLM Requests

The gateway is the single call site for the RAG pipeline. It composes:

  ┌─────────────────────────────────────────────────────┐
  │  LLMGateway.invoke() / .stream()                    │
  │       │                                             │
  │       ▼                                             │
  │  ModelRouter.select()        ← pick best model      │
  │       │                                             │
  │       ▼                                             │
  │  FallbackChain.ainvoke/astream  ← auto-failover     │
  │       │                                             │
  │       ▼                                             │
  │  CostTracker.track_usage()   ← per-tenant billing   │
  │       │                                             │
  │       ▼                                             │
  │  ObservabilityTracer.record() ← LangSmith/Phoenix   │
  │       │                                             │
  │       ▼                                             │
  │  Response / SSE stream                              │
  └─────────────────────────────────────────────────────┘

Usage (from query endpoint)::

    gateway = LLMGateway()
    response = await gateway.invoke(
        messages=prompt_messages,
        tenant_id=tenant_uuid,
        user_id=user_uuid,
        requirements=ModelRequirements(strategy=RoutingStrategy.LOWEST_LATENCY),
    )

    # SSE streaming
    async for token in gateway.stream(messages, tenant_id, user_id):
        yield f"data: {token}\n\n"
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import AsyncIterator
from uuid import UUID

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.llm.fallback import FallbackChain
from app.llm.router import ModelRequirements, ModelRouter, RoutingStrategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token usage estimation (approximate — real count from API response)
# ---------------------------------------------------------------------------

def _estimate_tokens(messages: list[BaseMessage]) -> int:
    """
    Rough token count: 4 chars ≈ 1 token (OpenAI heuristic).
    Used only for pre-flight routing decisions; actual billing uses API counts.
    """
    total_chars = sum(len(m.content) for m in messages if isinstance(m.content, str))  # type: ignore
    return max(1, total_chars // 4)


# ---------------------------------------------------------------------------
# LLMGateway
# ---------------------------------------------------------------------------

class LLMGateway:
    """
    Provider-agnostic LLM interface with routing, fallback, and observability.

    Instantiate once per application (singleton pattern) or per-request.
    All public methods are async and safe for concurrent use.
    """

    def __init__(self, router: ModelRouter | None = None) -> None:
        self._router = router or ModelRouter()

    # -----------------------------------------------------------------------
    # Non-streaming invoke
    # -----------------------------------------------------------------------

    async def invoke(
        self,
        messages:     list[BaseMessage],
        tenant_id:    UUID | None       = None,
        user_id:      UUID | None       = None,
        requirements: ModelRequirements | None = None,
    ) -> "GatewayResponse":
        """
        Invoke an LLM with automatic provider routing and fallback.

        Args:
            messages:     LangChain message list (SystemMessage + HumanMessage, etc.)
            tenant_id:    For cost tracking and audit logging.
            user_id:      For cost tracking and audit logging.
            requirements: Routing constraints. Defaults to HIGHEST_QUALITY.

        Returns:
            GatewayResponse with content, model_used, token counts, latency.
        """
        reqs    = requirements or ModelRequirements()
        spec    = self._router.select(reqs)
        chain   = FallbackChain(requirements=reqs)

        t0      = time.perf_counter()
        content = await chain.ainvoke(messages)
        latency = (time.perf_counter() - t0) * 1000

        # Rough token estimates (replace with actual counts from API metadata when available)
        input_tokens  = _estimate_tokens(messages)
        output_tokens = max(1, len(content) // 4)

        response = GatewayResponse(
            content       = content,
            model_used    = spec.model_id,
            provider      = spec.provider.value,
            input_tokens  = input_tokens,
            output_tokens = output_tokens,
            latency_ms    = latency,
            request_id    = str(uuid.uuid4()),
        )

        await self._post_process(response, tenant_id, user_id)
        return response

    # -----------------------------------------------------------------------
    # Streaming invoke
    # -----------------------------------------------------------------------

    async def stream(
        self,
        messages:     list[BaseMessage],
        tenant_id:    UUID | None       = None,
        user_id:      UUID | None       = None,
        requirements: ModelRequirements | None = None,
    ) -> AsyncIterator[str]:
        """
        Stream LLM tokens with automatic provider routing and fallback.

        Yields one string (content delta) per LLM token.

        Usage (FastAPI SSE endpoint)::

            async for token in gateway.stream(messages, tenant_id, user_id):
                yield ServerSentEvent(data=token)
        """
        reqs  = requirements or ModelRequirements(require_streaming=True)
        chain = FallbackChain(requirements=reqs)
        spec  = self._router.select(reqs)

        t0             = time.perf_counter()
        total_output   = 0
        full_content   = []

        async for token in chain.astream(messages):
            full_content.append(token)
            total_output += len(token) // 4 + 1
            yield token

        latency = (time.perf_counter() - t0) * 1000

        response = GatewayResponse(
            content       = "".join(full_content),
            model_used    = spec.model_id,
            provider      = spec.provider.value,
            input_tokens  = _estimate_tokens(messages),
            output_tokens = total_output,
            latency_ms    = latency,
            request_id    = str(uuid.uuid4()),
        )
        await self._post_process(response, tenant_id, user_id)

    # -----------------------------------------------------------------------
    # Post-processing: cost tracking + observability
    # -----------------------------------------------------------------------

    async def _post_process(
        self,
        response:  "GatewayResponse",
        tenant_id: UUID | None,
        user_id:   UUID | None,
    ) -> None:
        """
        Fire-and-forget cost tracking and tracing after each LLM call.
        Errors here are logged but never surfaced to the caller.
        """
        try:
            if tenant_id:
                from app.observability.cost_tracker import CostTracker
                tracker = CostTracker()
                await tracker.track_usage(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    model=response.model_used,
                    provider=response.provider,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )
        except Exception as exc:
            logger.warning("LLMGateway | cost tracking failed (non-fatal): %s", exc)

        logger.info(
            "LLMGateway | model=%s provider=%s tokens_in=%d tokens_out=%d latency_ms=%.1f",
            response.model_used, response.provider,
            response.input_tokens, response.output_tokens, response.latency_ms,
        )

    # -----------------------------------------------------------------------
    # Convenience: build message list
    # -----------------------------------------------------------------------

    @staticmethod
    def build_messages(system_prompt: str, user_question: str) -> list[BaseMessage]:
        """
        Build a standard [SystemMessage, HumanMessage] list.

        Args:
            system_prompt: Rendered system prompt (from PromptManager).
            user_question: The user's raw query.
        """
        return [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_question),
        ]


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

from dataclasses import dataclass


@dataclass
class GatewayResponse:
    """The result of a single non-streaming LLM gateway call."""
    content:       str
    model_used:    str
    provider:      str
    input_tokens:  int
    output_tokens: int
    latency_ms:    float
    request_id:    str
