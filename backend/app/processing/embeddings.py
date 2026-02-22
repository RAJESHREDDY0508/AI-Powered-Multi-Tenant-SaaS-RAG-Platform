"""
Embedding Pipeline  —  Batch Embeddings with Retry & Observability
══════════════════════════════════════════════════════════════════════

Design goals:
  • Batch efficiency: one API call per BATCH_SIZE chunks (default 100)
  • Retry logic: exponential back-off on rate limits and transient errors
  • Token accounting: logs token usage per batch for cost monitoring
  • Idempotency: deterministic chunk IDs mean re-runs upsert, not duplicate
  • Tenant isolation: VectorRecord always carries tenant_id in metadata

OpenAI embedding model selection:
  text-embedding-3-small  → 1536 dims, ~$0.00002/1K tokens  (default)
  text-embedding-3-large  → 3072 dims, ~$0.00013/1K tokens  (higher accuracy)

Batching strategy:
  OpenAI API: max 8191 tokens per input, max 2048 inputs per batch call.
  We use 100 texts per batch (well within both limits) and issue concurrent
  batch calls up to MAX_CONCURRENT_BATCHES to saturate network I/O.

Retry policy:
  On RateLimitError  → wait RETRY_BASE_DELAY × 2^attempt (exponential)
  On APIError (5xx)  → wait RETRY_BASE_DELAY × 2^attempt
  On APIConnectionError → immediate retry up to MAX_RETRIES
  On AuthenticationError → fail immediately (not transient)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Sequence
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EMBEDDING_BATCH_SIZE     = 100    # texts per OpenAI API call
MAX_CONCURRENT_BATCHES   = 4      # concurrent embedding requests
MAX_RETRIES              = 3      # per-batch retry limit
RETRY_BASE_DELAY         = 2.0    # seconds — doubles each retry
RETRY_MAX_DELAY          = 60.0   # cap

# Approximate tokens per character for cost estimation
# GPT tokenizer averages ~0.25 tokens/char for English text
CHARS_PER_TOKEN_EST = 4


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EmbeddingResult:
    """
    Full output of the embedding pipeline for one document.

    vector_records : list of (chunk_id, vector, metadata) ready for vector upsert
    total_chunks   : number of chunks processed
    total_tokens   : estimated token count (for cost monitoring)
    elapsed_ms     : total pipeline wall time
    failed_chunks  : indices of chunks that could not be embedded after retries
    """
    vector_records: list[dict]    # [{id, vector, metadata}, ...]
    total_chunks:   int
    total_tokens:   int
    elapsed_ms:     float
    failed_chunks:  list[int] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total_chunks == 0:
            return 1.0
        return (self.total_chunks - len(self.failed_chunks)) / self.total_chunks


# ---------------------------------------------------------------------------
# Core embedding pipeline
# ---------------------------------------------------------------------------

class EmbeddingPipeline:
    """
    Stateless embedding pipeline.

    One instance per worker task (created inside the Celery task function).

    Usage:
        from app.processing.chunking import ChunkResult
        from app.processing.embeddings import EmbeddingPipeline

        pipeline = EmbeddingPipeline(tenant_id=tid, model="text-embedding-3-small")
        result   = await pipeline.embed_chunks(chunks)

        # result.vector_records is ready for VectorStoreBase.upsert()
    """

    def __init__(
        self,
        tenant_id:  UUID,
        model:      str   = "text-embedding-3-small",
        dimensions: int   = 1536,
        api_key:    str   = "",
    ) -> None:
        self._tenant_id  = tenant_id
        self._model      = model
        self._dimensions = dimensions
        self._api_key    = api_key or self._get_api_key()

    def _get_api_key(self) -> str:
        from app.core.config import settings
        return settings.openai_api_key

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def embed_chunks(
        self,
        chunks: list,    # list[ChunkResult] — avoiding circular import
    ) -> EmbeddingResult:
        """
        Embed all chunks using batched OpenAI API calls.

        Flow:
          1. Split chunks into batches of EMBEDDING_BATCH_SIZE
          2. Issue up to MAX_CONCURRENT_BATCHES requests concurrently
          3. Retry failed batches with exponential back-off
          4. Build VectorRecord list from successful embeddings
          5. Return EmbeddingResult with full observability metrics

        Args:
            chunks: list of ChunkResult from SemanticChunker

        Returns:
            EmbeddingResult with vector_records ready for upsert
        """
        if not chunks:
            return EmbeddingResult(
                vector_records=[], total_chunks=0, total_tokens=0,
                elapsed_ms=0.0,
            )

        t0 = time.monotonic()

        # Split into batches
        batches = [
            chunks[i : i + EMBEDDING_BATCH_SIZE]
            for i in range(0, len(chunks), EMBEDDING_BATCH_SIZE)
        ]

        logger.info(
            "EmbeddingPipeline | tenant=%s chunks=%d batches=%d model=%s",
            self._tenant_id, len(chunks), len(batches), self._model,
        )

        # Issue batches concurrently (bounded by semaphore)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)
        tasks = [
            self._embed_batch_with_retry(batch, batch_idx, semaphore)
            for batch_idx, batch in enumerate(batches)
        ]

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        vector_records: list[dict] = []
        failed_chunks:  list[int]  = []
        total_tokens = 0

        chunk_offset = 0
        for batch_idx, result in enumerate(batch_results):
            batch = batches[batch_idx]

            if isinstance(result, Exception):
                logger.error(
                    "Batch %d permanently failed: %s", batch_idx, result
                )
                failed_chunks.extend(
                    chunk_offset + i for i in range(len(batch))
                )
                chunk_offset += len(batch)
                continue

            records, tokens = result
            vector_records.extend(records)
            total_tokens += tokens
            chunk_offset += len(batch)

        elapsed_ms = (time.monotonic() - t0) * 1000

        logger.info(
            "EmbeddingPipeline done | tenant=%s vectors=%d failed=%d "
            "tokens_est=%d elapsed_ms=%.0f",
            self._tenant_id, len(vector_records),
            len(failed_chunks), total_tokens, elapsed_ms,
        )

        return EmbeddingResult(
            vector_records=vector_records,
            total_chunks=len(chunks),
            total_tokens=total_tokens,
            elapsed_ms=elapsed_ms,
            failed_chunks=failed_chunks,
        )

    # ------------------------------------------------------------------
    # Batch processing with retry
    # ------------------------------------------------------------------

    async def _embed_batch_with_retry(
        self,
        batch:     list,
        batch_idx: int,
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[dict], int]:
        """
        Embed a single batch with exponential back-off retry.

        Returns:
            (vector_records, estimated_tokens)

        Raises:
            Exception if all retries are exhausted.
        """
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
                logger.warning(
                    "Embedding retry | batch=%d attempt=%d delay=%.1fs error=%s",
                    batch_idx, attempt, delay, last_error,
                )
                await asyncio.sleep(delay)

            async with semaphore:
                try:
                    return await self._call_openai(batch, batch_idx)
                except Exception as exc:
                    last_error = exc
                    error_name = type(exc).__name__

                    # Non-retryable errors — fail immediately
                    if "AuthenticationError" in error_name or "InvalidRequestError" in error_name:
                        logger.error(
                            "Non-retryable embedding error batch=%d: %s", batch_idx, exc
                        )
                        raise

                    # Retryable errors (rate limit, 5xx, network)
                    logger.warning(
                        "Retryable embedding error batch=%d attempt=%d: %s %s",
                        batch_idx, attempt, error_name, exc,
                    )

        raise last_error or RuntimeError(f"Embedding batch {batch_idx} failed after {MAX_RETRIES} retries")

    async def _call_openai(
        self,
        batch:     list,
        batch_idx: int,
    ) -> tuple[list[dict], int]:
        """
        Single OpenAI embeddings API call for a batch of chunks.

        Uses openai.AsyncOpenAI for async I/O — does not block the event loop.

        Returns:
            (vector_records, estimated_tokens)
        """
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._api_key)
        texts  = [chunk.text for chunk in batch]

        t_api = time.monotonic()

        response = await client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dimensions if self._dimensions != 1536 else None,
            # Note: dimensions param only works for text-embedding-3-* models
        )

        api_ms = (time.monotonic() - t_api) * 1000

        # Extract usage (OpenAI returns actual token count)
        tokens_used = response.usage.total_tokens if response.usage else sum(
            len(t) // CHARS_PER_TOKEN_EST for t in texts
        )

        logger.debug(
            "OpenAI embeddings | batch=%d size=%d tokens=%d api_ms=%.0f",
            batch_idx, len(batch), tokens_used, api_ms,
        )

        # Build VectorRecord dicts
        records: list[dict] = []
        for chunk, embedding_obj in zip(batch, response.data):
            vector = embedding_obj.embedding

            records.append({
                "id":     chunk.chunk_id,    # deterministic sha256 ID
                "vector": vector,
                "metadata": {
                    # ── Required fields for VectorStoreBase ──────────
                    "tenant_id":   str(self._tenant_id),   # MUST match store namespace
                    "document_id": chunk.document_id,
                    "chunk_index": chunk.chunk_index,
                    "text":        chunk.text,              # stored for retrieval without DB
                    "source_key":  chunk.source_key,        # S3 key for citations
                    # ── Searchable enrichment ─────────────────────────
                    "page_number": chunk.page_number,
                    "heading":     chunk.heading,
                    "char_count":  chunk.char_count,
                    "token_est":   chunk.token_est,
                    # ── Filterable fields for RAG metadata filters ────
                    **chunk.metadata,    # includes any extra fields from extractor
                },
            })

        return records, tokens_used

    # ------------------------------------------------------------------
    # Utility: embed a single query string (for RAG retrieval)
    # ------------------------------------------------------------------

    async def embed_query(self, text: str) -> list[float]:
        """
        Embed a single text string for RAG query-time use.
        Uses the same model as document ingestion for consistency.
        """
        from openai import AsyncOpenAI
        client   = AsyncOpenAI(api_key=self._api_key)
        response = await client.embeddings.create(
            model=self._model,
            input=[text],
        )
        return response.data[0].embedding


# ---------------------------------------------------------------------------
# Module-level convenience function (used by Celery task)
# ---------------------------------------------------------------------------

async def run_embedding_pipeline(
    chunks:    list,
    tenant_id: UUID,
) -> EmbeddingResult:
    """
    Convenience wrapper — creates an EmbeddingPipeline and runs it.
    Reads model config from application settings.
    """
    from app.core.config import settings

    pipeline = EmbeddingPipeline(
        tenant_id=tenant_id,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        api_key=settings.openai_api_key,
    )
    return await pipeline.embed_chunks(chunks)
