"""
LLM Fallback Chain — Automatic Provider Failover

When the primary LLM provider returns a 5xx error (server error, rate limit,
or service outage), the FallbackChain tries the next provider in order until
one succeeds or all are exhausted.

Fallback chain (default order):
  1. OpenAI GPT-4o               (primary — best quality)
  2. Azure OpenAI GPT-4o          (first fallback — same model, different endpoint)
  3. AWS Bedrock Claude 3.5       (second fallback — different provider)
  4. Ollama Llama 3.1 8B          (last resort — local, always available if deployed)

Retry policy:
  - Retryable:     HTTP 5xx, RateLimitError, ServiceUnavailableError, TimeoutError
  - Non-retryable: HTTP 4xx (bad request, auth failure) — fail immediately
  - Per-attempt timeout: 30 s  (configurable)
  - Total timeout: 90 s across all fallbacks

Circuit breaker pattern:
  If a provider fails N consecutive times within a window, it's skipped until
  the window resets. This prevents repeated slow-path timeouts.
  (Implemented as a simple in-process counter — production would use Redis.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.llm.router import ModelRouter, ModelRequirements, ModelSpec, Provider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retryable exception detection
# ---------------------------------------------------------------------------

_RETRYABLE_EXCEPTION_TYPES = (
    # openai
    "RateLimitError",
    "ServiceUnavailableError",
    "APITimeoutError",
    "APIConnectionError",
    "InternalServerError",
    # httpx / generic
    "ConnectTimeout",
    "ReadTimeout",
    "RemoteProtocolError",
)


def _is_retryable(exc: Exception) -> bool:
    """True if the exception class name suggests a transient provider error."""
    name = type(exc).__name__
    return any(name.endswith(r) for r in _RETRYABLE_EXCEPTION_TYPES)


# ---------------------------------------------------------------------------
# Circuit breaker (in-process)
# ---------------------------------------------------------------------------

@dataclass
class _CircuitState:
    failures:       int   = 0
    open_until:     float = 0.0        # monotonic time after which to retry
    OPEN_THRESHOLD: int   = 3          # failures within window before opening
    RESET_SECONDS:  int   = 60         # how long circuit stays open


_CIRCUIT_STATES: dict[Provider, _CircuitState] = {
    p: _CircuitState() for p in Provider
}


def _is_circuit_open(provider: Provider) -> bool:
    state = _CIRCUIT_STATES[provider]
    if state.failures < state.OPEN_THRESHOLD:
        return False
    if time.monotonic() >= state.open_until:
        state.failures = 0     # reset — let it try again
        return False
    return True                # still open


def _record_failure(provider: Provider) -> None:
    state = _CIRCUIT_STATES[provider]
    state.failures  += 1
    state.open_until = time.monotonic() + state.RESET_SECONDS
    logger.warning(
        "Circuit breaker | provider=%s failures=%d open_until=+%ds",
        provider, state.failures, state.RESET_SECONDS,
    )


def _record_success(provider: Provider) -> None:
    _CIRCUIT_STATES[provider].failures = 0


# ---------------------------------------------------------------------------
# FallbackChain
# ---------------------------------------------------------------------------

class FallbackChain:
    """
    Ordered chain of LLM providers with automatic failover.

    Usage::

        chain = FallbackChain(requirements=ModelRequirements())

        # Non-streaming
        result = await chain.ainvoke(messages)

        # Streaming
        async for token in chain.astream(messages):
            yield token

    The chain is stateless per request and safe to reuse across requests.
    """

    def __init__(
        self,
        requirements:       ModelRequirements | None = None,
        per_attempt_timeout: float = 30.0,    # seconds per provider
    ) -> None:
        self._requirements       = requirements or ModelRequirements()
        self._per_attempt_timeout = per_attempt_timeout
        self._router              = ModelRouter()
        self._specs               = self._build_fallback_list()

    def _build_fallback_list(self) -> list[ModelSpec]:
        """
        Build an ordered list of ModelSpecs for the fallback chain.

        Primary is selected by the router from the full registered catalogue.
        Remaining specs are ordered by: primary provider first, then Azure,
        Bedrock, Ollama — so we always try the best available next.
        """
        from app.llm.router import _REGISTERED_MODELS

        primary = self._router.select(self._requirements)
        specs   = [primary]

        # Add all OTHER specs that satisfy the requirements, sorted by quality
        others = [
            s for s in _REGISTERED_MODELS
            if s.model_id != primary.model_id or s.provider != primary.provider
            if self._requirements.privacy in s.privacy_levels
            if s.context_window >= self._requirements.max_input_tokens
        ]
        others.sort(key=lambda s: s.quality_score, reverse=True)
        specs.extend(others)

        return specs

    # -----------------------------------------------------------------------
    # Non-streaming invoke
    # -----------------------------------------------------------------------

    async def ainvoke(self, messages: list[BaseMessage]) -> str:
        """
        Invoke the chain with automatic fallback.

        Returns the LLM's response as a plain string.

        Raises:
            RuntimeError: If all providers fail.
        """
        errors: list[str] = []

        for spec in self._specs:
            if _is_circuit_open(spec.provider):
                logger.debug("Skipping provider=%s (circuit open)", spec.provider)
                continue

            llm = self._router.build_llm(spec, streaming=False)
            try:
                logger.debug("FallbackChain | trying provider=%s model=%s", spec.provider, spec.model_id)
                result = await asyncio.wait_for(
                    llm.ainvoke(messages),
                    timeout=self._per_attempt_timeout,
                )
                _record_success(spec.provider)
                return result.content  # type: ignore[union-attr]

            except asyncio.TimeoutError:
                err = f"{spec.provider}/{spec.model_id}: timed out after {self._per_attempt_timeout}s"
                logger.warning("FallbackChain | %s", err)
                _record_failure(spec.provider)
                errors.append(err)

            except Exception as exc:
                if not _is_retryable(exc):
                    raise   # 4xx, auth failure — non-retryable, surface immediately
                err = f"{spec.provider}/{spec.model_id}: {type(exc).__name__}: {exc}"
                logger.warning("FallbackChain | retryable error — %s", err)
                _record_failure(spec.provider)
                errors.append(err)

        raise RuntimeError(
            f"All LLM providers failed. Errors:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    # -----------------------------------------------------------------------
    # Streaming invoke
    # -----------------------------------------------------------------------

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[str]:
        """
        Stream tokens with automatic provider fallback.

        Falls back BEFORE producing any output — if the primary provider errors
        on the first chunk, we switch to the next provider transparently.
        If fallback also streams, we yield from its stream.

        Yields:
            Individual token strings (content deltas).
        """
        errors: list[str] = []

        for spec in self._specs:
            if _is_circuit_open(spec.provider):
                continue

            llm = self._router.build_llm(spec, streaming=True)
            try:
                logger.debug(
                    "FallbackChain.stream | trying provider=%s model=%s",
                    spec.provider, spec.model_id,
                )
                async for chunk in llm.astream(messages):
                    yield chunk.content  # type: ignore[union-attr]
                _record_success(spec.provider)
                return  # ← normal exit after successful stream

            except asyncio.TimeoutError:
                err = f"{spec.provider}/{spec.model_id}: stream timed out"
                logger.warning("FallbackChain.stream | %s", err)
                _record_failure(spec.provider)
                errors.append(err)

            except Exception as exc:
                if not _is_retryable(exc):
                    raise
                err = f"{spec.provider}/{spec.model_id}: {type(exc).__name__}: {exc}"
                logger.warning("FallbackChain.stream | retryable — %s", err)
                _record_failure(spec.provider)
                errors.append(err)

        raise RuntimeError(
            "All LLM streaming providers failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
