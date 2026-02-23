"""
Cross-Encoder Re-Ranking — Cohere ReRank v3

Takes the merged candidate list from hybrid retrieval (top-20) and applies
a cross-encoder model to produce fine-grained relevance scores.

Why cross-encoder AFTER bi-encoder retrieval?

  Bi-encoder (dense embedding):
    - Encodes query and document SEPARATELY, computes cosine similarity.
    - Fast (pre-computed vectors), suitable for full-corpus search.
    - Misses nuanced relevance (the model never sees query + doc together).

  Cross-encoder:
    - Encodes (query, document) JOINTLY via self-attention across both.
    - Far more accurate but O(n) LLM forward passes — too slow for full search.
    - Perfect for reranking a small candidate set (top-20 → top-5).

Pipeline:
  1. Dense bi-encoder → fast candidate retrieval (top-20)
  2. BM25             → keyword match (merged with RRF)
  3. Cross-encoder    → accurate reranking → final top-K          ← this file

Cohere ReRank v3 models:
  - rerank-english-v3.0      : best English accuracy
  - rerank-multilingual-v3.0 : 100+ languages, ~5% lower English accuracy

Graceful degradation:
  If the Cohere API key is absent or the call fails, the reranker returns
  the input documents in their original (RRF-fused) order without raising.

Dependencies:
  pip install cohere>=5.0.0
"""

from __future__ import annotations

import logging
import time

from langchain_core.documents import Document

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CohereReranker
# ---------------------------------------------------------------------------

class CohereReranker:
    """
    Wraps Cohere's async ReRank API for cross-encoder relevance scoring.

    Usage::

        reranker  = CohereReranker()
        final_docs = await reranker.rerank(
            query="What is the refund policy?",
            candidates=top_20_docs,
            top_n=5,
        )

    Thread/async-safe: each .rerank() call is independent.
    """

    def __init__(
        self,
        model:   str        = "rerank-english-v3.0",
        api_key: str | None = None,
    ) -> None:
        self._model   = model
        self._api_key = api_key or getattr(settings, "cohere_api_key", "")
        self._client  = None

        if self._api_key:
            try:
                import cohere  # lazy import — optional dependency
                self._client = cohere.AsyncClient(api_key=self._api_key)
                logger.info("CohereReranker initialised | model=%s", model)
            except ImportError:
                logger.warning(
                    "cohere package not installed. "
                    "Run: pip install cohere>=5.0.0. "
                    "Reranker will pass through results unchanged."
                )
        else:
            logger.warning(
                "COHERE_API_KEY not configured — reranker disabled. "
                "Results will be returned in RRF-fusion order."
            )

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True if the Cohere client was successfully initialised."""
        return self._client is not None

    async def rerank(
        self,
        query:      str,
        candidates: list[Document],
        top_n:      int = 5,
    ) -> list[Document]:
        """
        Rerank candidate documents by cross-encoder relevance.

        Args:
            query:      The user's raw query string.
            candidates: Documents from dense + BM25 hybrid fusion (≤ 20).
            top_n:      Number of top documents to return after reranking.

        Returns:
            Top-n Documents sorted by cross-encoder relevance score
            (most relevant first).  Each document gains a "rerank_score"
            and "rerank_original_rank" field in its metadata.

        Never raises — falls back to pass-through on any error.
        """
        if not candidates:
            return []

        top_n = min(top_n, len(candidates))

        # --- Graceful fallback: no Cohere ---
        if not self.available:
            logger.debug("Reranker unavailable — returning candidates in original RRF order")
            return candidates[:top_n]

        t0 = time.perf_counter()
        try:
            response = await self._client.rerank(   # type: ignore[union-attr]
                model=self._model,
                query=query,
                documents=[doc.page_content for doc in candidates],
                top_n=top_n,
                return_documents=False,   # we own the Document objects already
            )
        except Exception as exc:
            logger.warning(
                "CohereReranker API error — falling back to RRF order: %s",
                exc, exc_info=True,
            )
            return candidates[:top_n]

        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Re-materialise Documents in reranked order
        reranked: list[Document] = []
        for result in response.results:
            original   = candidates[result.index]
            reranked_doc = Document(
                page_content=original.page_content,
                metadata={
                    **original.metadata,
                    "rerank_score":         result.relevance_score,
                    "rerank_original_rank": result.index + 1,
                },
            )
            reranked.append(reranked_doc)

        logger.info(
            "CohereReranker | model=%s candidates=%d top_n=%d elapsed_ms=%.1f",
            self._model, len(candidates), top_n, elapsed_ms,
        )
        return reranked
