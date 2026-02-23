"""
LLM Model Router — Provider Selection by Cost / Latency / Privacy

The router is the decision engine that answers:
  "Which model should I use for this request?"

Routing axes:

  1. Privacy level (most restrictive constraint — applied first):
       STANDARD  → any provider allowed
       SENSITIVE → no third-party APIs; prefer Azure OpenAI (GDPR) or Bedrock
       PRIVATE   → local/on-premise ONLY (Ollama/Llama 3, air-gapped)

  2. Routing strategy (secondary):
       LOWEST_COST    → choose cheapest model that fits token budget
       LOWEST_LATENCY → choose model with smallest p50 TTFT
       HIGHEST_QUALITY → choose most capable model regardless of cost

  3. Hard constraints:
       max_input_tokens   → must fit in model's context window
       response_json      → only models supporting JSON mode

Design principles:
  - The router is pure Python (no I/O, no network) — fast and testable.
  - Provider credentials are resolved at instantiation from settings/env.
  - The LangChain model object is returned, not a raw string — the gateway
    can call .invoke() or .stream() directly.

Adding a new model:
  Add a ModelSpec to _REGISTERED_MODELS and it becomes immediately eligible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PrivacyLevel(str, Enum):
    """Data privacy requirement for the request."""
    STANDARD  = "standard"   # any provider
    SENSITIVE = "sensitive"  # GDPR-compliant providers only (Azure, Bedrock)
    PRIVATE   = "private"    # local inference only (Ollama)


class RoutingStrategy(str, Enum):
    """Model selection optimisation objective."""
    LOWEST_COST     = "lowest_cost"
    LOWEST_LATENCY  = "lowest_latency"
    HIGHEST_QUALITY = "highest_quality"


class Provider(str, Enum):
    OPENAI        = "openai"
    AZURE_OPENAI  = "azure_openai"
    AWS_BEDROCK   = "aws_bedrock"
    OLLAMA        = "ollama"


# ---------------------------------------------------------------------------
# ModelSpec — metadata for each registered model
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    """
    Static metadata for one LLM model/provider combination.

    cost_input_per_1k:   USD per 1 000 input tokens  (0.0 for local)
    cost_output_per_1k:  USD per 1 000 output tokens (0.0 for local)
    context_window:      Maximum total tokens (input + output)
    p50_latency_ms:      Approximate median time-to-first-token in ms
    quality_score:       Subjective 0-10 quality ranking (for HIGHEST_QUALITY)
    privacy_levels:      Set of PrivacyLevel values this provider satisfies
    supports_streaming:  Whether .stream() / astream() is implemented
    supports_json_mode:  Whether structured JSON output is guaranteed
    """
    model_id:            str
    provider:            Provider
    context_window:      int
    cost_input_per_1k:   float
    cost_output_per_1k:  float
    p50_latency_ms:      int
    quality_score:       float
    privacy_levels:      frozenset[PrivacyLevel]  = field(default_factory=lambda: frozenset({PrivacyLevel.STANDARD}))
    supports_streaming:  bool                      = True
    supports_json_mode:  bool                      = False


# ---------------------------------------------------------------------------
# Registered model catalogue
# ---------------------------------------------------------------------------

_REGISTERED_MODELS: list[ModelSpec] = [
    # OpenAI
    ModelSpec(
        model_id           = "gpt-4o",
        provider           = Provider.OPENAI,
        context_window     = 128_000,
        cost_input_per_1k  = 0.005,
        cost_output_per_1k = 0.015,
        p50_latency_ms     = 900,
        quality_score      = 9.5,
        privacy_levels     = frozenset({PrivacyLevel.STANDARD}),
        supports_streaming = True,
        supports_json_mode = True,
    ),
    ModelSpec(
        model_id           = "gpt-4o-mini",
        provider           = Provider.OPENAI,
        context_window     = 128_000,
        cost_input_per_1k  = 0.00015,
        cost_output_per_1k = 0.0006,
        p50_latency_ms     = 400,
        quality_score      = 8.0,
        privacy_levels     = frozenset({PrivacyLevel.STANDARD}),
        supports_streaming = True,
        supports_json_mode = True,
    ),
    # Azure OpenAI — same models, different endpoint (GDPR-compliant EU region)
    ModelSpec(
        model_id           = "gpt-4o",             # deployment name on Azure
        provider           = Provider.AZURE_OPENAI,
        context_window     = 128_000,
        cost_input_per_1k  = 0.005,
        cost_output_per_1k = 0.015,
        p50_latency_ms     = 1_100,                 # slightly higher (EU region)
        quality_score      = 9.5,
        privacy_levels     = frozenset({PrivacyLevel.STANDARD, PrivacyLevel.SENSITIVE}),
        supports_streaming = True,
        supports_json_mode = True,
    ),
    # AWS Bedrock — Claude 3.5 Sonnet
    ModelSpec(
        model_id           = "anthropic.claude-3-5-sonnet-20241022-v2:0",
        provider           = Provider.AWS_BEDROCK,
        context_window     = 200_000,
        cost_input_per_1k  = 0.003,
        cost_output_per_1k = 0.015,
        p50_latency_ms     = 1_200,
        quality_score      = 9.3,
        privacy_levels     = frozenset({PrivacyLevel.STANDARD, PrivacyLevel.SENSITIVE}),
        supports_streaming = True,
        supports_json_mode = False,
    ),
    # Ollama — local Llama 3.1 8B (for PRIVATE / air-gapped deployments)
    ModelSpec(
        model_id           = "llama3.1:8b",
        provider           = Provider.OLLAMA,
        context_window     = 128_000,
        cost_input_per_1k  = 0.0,               # self-hosted
        cost_output_per_1k = 0.0,
        p50_latency_ms     = 2_000,             # depends on GPU
        quality_score      = 7.0,
        privacy_levels     = frozenset({
            PrivacyLevel.STANDARD,
            PrivacyLevel.SENSITIVE,
            PrivacyLevel.PRIVATE,
        }),
        supports_streaming = True,
        supports_json_mode = False,
    ),
]


# ---------------------------------------------------------------------------
# ModelRequirements — caller-specified constraints
# ---------------------------------------------------------------------------

@dataclass
class ModelRequirements:
    """
    Constraints provided by the caller to influence model selection.

    All fields are optional with sensible defaults.
    """
    privacy:            PrivacyLevel    = PrivacyLevel.STANDARD
    strategy:           RoutingStrategy = RoutingStrategy.HIGHEST_QUALITY
    max_input_tokens:   int             = 4_096
    require_json_mode:  bool            = False
    require_streaming:  bool            = True


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------

class ModelRouter:
    """
    Pure-Python routing logic.  No I/O — fast and fully unit-testable.

    Usage::

        router = ModelRouter()
        spec   = router.select(ModelRequirements(privacy=PrivacyLevel.SENSITIVE))
        llm    = router.build_llm(spec, streaming=True)
    """

    def select(self, requirements: ModelRequirements) -> ModelSpec:
        """
        Select the best ModelSpec for the given requirements.

        Filters:
          1. Privacy level  — only models whose privacy_levels include the requirement
          2. Context window — model.context_window >= requirements.max_input_tokens
          3. JSON mode      — if required, only models with supports_json_mode=True
          4. Streaming      — if required, only models with supports_streaming=True

        Then sorts by strategy:
          LOWEST_COST     → cost_input_per_1k ascending
          LOWEST_LATENCY  → p50_latency_ms ascending
          HIGHEST_QUALITY → quality_score descending

        Raises:
            RuntimeError: If no registered model satisfies all constraints.
        """
        candidates = [
            spec for spec in _REGISTERED_MODELS
            if (
                requirements.privacy in spec.privacy_levels
                and spec.context_window >= requirements.max_input_tokens
                and (not requirements.require_json_mode or spec.supports_json_mode)
                and (not requirements.require_streaming  or spec.supports_streaming)
            )
        ]

        if not candidates:
            raise RuntimeError(
                f"No LLM satisfies constraints: privacy={requirements.privacy}, "
                f"tokens={requirements.max_input_tokens}, json={requirements.require_json_mode}"
            )

        if requirements.strategy == RoutingStrategy.LOWEST_COST:
            candidates.sort(key=lambda s: (s.cost_input_per_1k, s.cost_output_per_1k))
        elif requirements.strategy == RoutingStrategy.LOWEST_LATENCY:
            candidates.sort(key=lambda s: s.p50_latency_ms)
        else:   # HIGHEST_QUALITY
            candidates.sort(key=lambda s: s.quality_score, reverse=True)

        selected = candidates[0]
        logger.info(
            "ModelRouter | selected model_id=%s provider=%s strategy=%s privacy=%s",
            selected.model_id, selected.provider,
            requirements.strategy, requirements.privacy,
        )
        return selected

    def build_llm(self, spec: ModelSpec, streaming: bool = False) -> BaseChatModel:
        """
        Instantiate the LangChain LLM for a given ModelSpec.

        Returns a BaseChatModel — the gateway calls .ainvoke() or .astream().
        """
        if spec.provider == Provider.OPENAI:
            return self._build_openai(spec, streaming)

        if spec.provider == Provider.AZURE_OPENAI:
            return self._build_azure_openai(spec, streaming)

        if spec.provider == Provider.AWS_BEDROCK:
            return self._build_bedrock(spec, streaming)

        if spec.provider == Provider.OLLAMA:
            return self._build_ollama(spec, streaming)

        raise ValueError(f"Unsupported provider: {spec.provider}")   # pragma: no cover

    # -----------------------------------------------------------------------
    # Provider-specific builders
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_openai(spec: ModelSpec, streaming: bool) -> BaseChatModel:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=spec.model_id,
            api_key=settings.openai_api_key,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            streaming=streaming,
        )

    @staticmethod
    def _build_azure_openai(spec: ModelSpec, streaming: bool) -> BaseChatModel:
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(
            azure_deployment=getattr(settings, "azure_openai_deployment", spec.model_id),
            azure_endpoint=getattr(settings, "azure_openai_endpoint", ""),
            api_key=getattr(settings, "azure_openai_api_key", ""),   # type: ignore
            api_version=getattr(settings, "azure_openai_api_version", "2024-08-01-preview"),
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            streaming=streaming,
        )

    @staticmethod
    def _build_bedrock(spec: ModelSpec, streaming: bool) -> BaseChatModel:
        from langchain_aws import ChatBedrock
        return ChatBedrock(
            model_id=spec.model_id,
            region_name=settings.aws_region,
            model_kwargs={
                "temperature": settings.llm_temperature,
                "max_tokens":  settings.llm_max_tokens,
            },
            streaming=streaming,
        )

    @staticmethod
    def _build_ollama(spec: ModelSpec, streaming: bool) -> BaseChatModel:
        from langchain_community.chat_models import ChatOllama
        return ChatOllama(
            model=spec.model_id,
            base_url=getattr(settings, "ollama_base_url", "http://localhost:11434"),
            temperature=settings.llm_temperature,
        )
