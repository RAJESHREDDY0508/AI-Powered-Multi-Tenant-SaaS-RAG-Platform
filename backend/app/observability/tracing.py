"""
Observability Tracing — LangSmith + Arize Phoenix Integration

Traces every RAG pipeline request end-to-end:
  User Query → Hybrid Retrieval → Prompt Build → LLM Call → Response

Why tracing matters:
  - Debug failures: "The LLM hallucinated" vs "The retriever missed the chunk"
  - Performance: measure TTFT per stage, find bottlenecks
  - Regression detection: compare traces across prompt versions (A/B test)

Supported backends:

  LangSmith (hosted — recommended for cloud-native):
    - Set LANGCHAIN_TRACING_V2=true, LANGCHAIN_API_KEY, LANGCHAIN_PROJECT
    - Automatic tracing for all LangChain LCEL chains via callback injection
    - No code changes needed — tracing is activated by environment variables

  Arize Phoenix (self-hosted — recommended for on-premise):
    - Runs a local OTEL collector: `docker run -p 6006:6006 arizephoenix/phoenix`
    - Instruments LangChain via OpenTelemetry
    - View traces at http://localhost:6006

  OTEL (OpenTelemetry) generic:
    - For Jaeger, Zipkin, Datadog APM — set OTEL_EXPORTER_OTLP_ENDPOINT

Decorator `@traced(name)`:
  Instruments any async function with timing, input/output capture,
  and error recording. Works regardless of backend.

Environment variables:
  LANGCHAIN_TRACING_V2=true
  LANGCHAIN_API_KEY=ls__...
  LANGCHAIN_PROJECT=rag-platform-prod

  PHOENIX_ENABLED=true
  PHOENIX_ENDPOINT=http://localhost:6006/v1/traces

  OTEL_ENABLED=false
  OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable, Coroutine, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])


# ---------------------------------------------------------------------------
# TracingConfig — initialise at app startup
# ---------------------------------------------------------------------------

class TracingConfig:
    """
    Initialise all active tracing backends from environment variables.

    Call once at application startup::

        from app.observability.tracing import TracingConfig
        TracingConfig.init()
    """

    _initialised: bool = False

    @classmethod
    def init(cls) -> None:
        """Initialise all enabled tracing backends."""
        if cls._initialised:
            return
        cls._initialised = True

        cls._init_langsmith()
        cls._init_phoenix()
        cls._init_otel()

    @staticmethod
    def _init_langsmith() -> None:
        """
        LangSmith activation — purely environment-variable-driven.
        LangChain automatically reads these env vars on import.
        We just set them from settings if not already set.
        """
        from app.core.config import settings

        langsmith_key     = getattr(settings, "langsmith_api_key", "")
        langsmith_project = getattr(settings, "langsmith_project", "rag-platform")

        if langsmith_key and not os.environ.get("LANGCHAIN_API_KEY"):
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            os.environ["LANGCHAIN_API_KEY"]     = langsmith_key
            os.environ["LANGCHAIN_PROJECT"]     = langsmith_project
            logger.info(
                "LangSmith tracing enabled | project=%s", langsmith_project
            )
        elif os.environ.get("LANGCHAIN_TRACING_V2") == "true":
            logger.info(
                "LangSmith tracing active (from env) | project=%s",
                os.environ.get("LANGCHAIN_PROJECT", "default"),
            )
        else:
            logger.debug("LangSmith tracing disabled (LANGCHAIN_TRACING_V2 not set)")

    @staticmethod
    def _init_phoenix() -> None:
        """
        Arize Phoenix — sets up an OTEL tracer that sends spans to Phoenix.
        Requires: pip install arize-phoenix-otel
        """
        phoenix_enabled  = os.environ.get("PHOENIX_ENABLED", "false").lower() == "true"
        phoenix_endpoint = os.environ.get("PHOENIX_ENDPOINT", "http://localhost:6006/v1/traces")

        if not phoenix_enabled:
            logger.debug("Arize Phoenix tracing disabled (PHOENIX_ENABLED != true)")
            return

        try:
            from phoenix.otel import register   # type: ignore[import]
            register(
                project_name="rag-platform",
                endpoint=phoenix_endpoint,
                auto_instrument_langchain=True,
            )
            logger.info("Arize Phoenix tracing enabled | endpoint=%s", phoenix_endpoint)
        except ImportError:
            logger.warning(
                "arize-phoenix-otel not installed — Phoenix tracing disabled. "
                "Run: pip install arize-phoenix-otel"
            )
        except Exception as exc:
            logger.warning("Phoenix tracing init failed: %s", exc)

    @staticmethod
    def _init_otel() -> None:
        """
        Generic OpenTelemetry export (Jaeger, Zipkin, Datadog, etc.)
        Requires: OTEL_ENABLED=true and OTEL_EXPORTER_OTLP_ENDPOINT set.
        """
        otel_enabled  = os.environ.get("OTEL_ENABLED", "false").lower() == "true"
        otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

        if not otel_enabled or not otel_endpoint:
            logger.debug("OTEL tracing disabled")
            return

        try:
            from opentelemetry import trace                                       # type: ignore
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # type: ignore
            from opentelemetry.sdk.trace import TracerProvider                    # type: ignore
            from opentelemetry.sdk.trace.export import BatchSpanProcessor        # type: ignore

            provider = TracerProvider()
            exporter = OTLPSpanExporter(endpoint=otel_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)

            logger.info("OTEL tracing enabled | endpoint=%s", otel_endpoint)
        except ImportError:
            logger.warning(
                "opentelemetry-sdk / opentelemetry-exporter-otlp not installed. "
                "Run: pip install opentelemetry-sdk opentelemetry-exporter-otlp"
            )
        except Exception as exc:
            logger.warning("OTEL tracing init failed: %s", exc)


# ---------------------------------------------------------------------------
# @traced decorator
# ---------------------------------------------------------------------------

def traced(name: str | None = None) -> Callable[[F], F]:
    """
    Decorator that instruments an async function with timing and error logging.

    Works with any backend (LangSmith, Phoenix, OTEL, or none) — it uses
    structlog / Python logging as the baseline, so it is always active.

    Usage::

        @traced("hybrid_retrieval")
        async def retrieve(query: str) -> list[Document]:
            ...

        @traced()   # uses function name as span name
        async def embed_query(text: str) -> list[float]:
            ...
    """
    def decorator(func: F) -> F:
        span_name = name or func.__qualname__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.debug("trace | span=%s elapsed_ms=%.1f ok", span_name, elapsed_ms)
                return result
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.error(
                    "trace | span=%s elapsed_ms=%.1f error=%s",
                    span_name, elapsed_ms, exc, exc_info=True,
                )
                raise

        return wrapper  # type: ignore[return-value]
    return decorator
