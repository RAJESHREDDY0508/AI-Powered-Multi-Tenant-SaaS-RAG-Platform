"""
Hybrid Retriever — Dense + BM25 + Cohere ReRank + Permission Filtering

Full retrieval pipeline for production-grade RAG:

  ┌─────────────────────────────────────────────────────────────┐
  │  User Query                                                 │
  │       │                                                     │
  │       ▼                                                     │
  │  [1] Embed Query (OpenAI text-embedding-3-small)            │
  │       │                                                     │
  │       ├──────────────────┐                                  │
  │       ▼                  ▼                                  │
  │  [2] Dense Retrieval   [3] BM25 (over dense corpus)         │
  │   (vector store,          (rank_bm25, keyword match)        │
  │    top-20 results)                                          │
  │       │                  │                                  │
  │       └────────┬─────────┘                                  │
  │                ▼                                            │
  │       [4] RRF Fusion (Reciprocal Rank Fusion)               │
  │                │                                            │
  │                ▼                                            │
  │       [5] Permission Filter (document_permissions)          │
  │                │                                            │
  │                ▼                                            │
  │       [6] Cohere ReRank (cross-encoder, top-20 → top-K)     │
  │                │                                            │
  │                ▼                                            │
  │       Final Documents (ordered by cross-encoder score)      │
  └─────────────────────────────────────────────────────────────┘

Tenant isolation:
  - Dense query is ALWAYS tenant-scoped via vector store namespace.
  - BM25 is built from the dense corpus — also tenant-scoped by inheritance.
  - Permission filter provides defence-in-depth for document-level ACLs.

RRF constant (k=60):
  Recommended by Cormack et al. (2009) "Reciprocal Rank Fusion outperforms
  Condorcet and individual Rank Learning Methods". k=60 penalises lower-ranked
  documents moderately; higher k → more uniform weighting.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from app.core.config import settings
from app.rag.bm25 import TenantBM25Index
from app.rag.reranker import CohereReranker
from app.vectorstore.base import QueryResult, VectorStoreBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

_RRF_K: int = 60  # Cormack et al. 2009 recommended constant


def _rrf_score(rank: int, k: int = _RRF_K) -> float:
    """RRF contribution for a document at 1-based rank position."""
    return 1.0 / (k + rank)


# ---------------------------------------------------------------------------
# HybridRetriever
# ---------------------------------------------------------------------------

class HybridRetriever:
    """
    Production hybrid retriever: Dense × BM25 → RRF → PermFilter → ReRank.

    Instantiate once per request (lightweight — no network I/O at construction)::

        retriever = HybridRetriever(
            vector_store=tenant_vector_store,
            embedder=get_embedding_model(),
        )
        docs = await retriever.retrieve(
            query="What is our refund policy for Policy #882?",
            top_k=5,
            metadata_filter={"document_permissions": {"$in": user_roles}},
        )

    Parameters
    ----------
    vector_store:      Tenant-scoped vector store (from auth dependency injection).
    embedder:          OpenAIEmbeddings instance (shared across requests is fine).
    reranker:          CohereReranker — created from settings if not supplied.
    dense_candidates:  How many results to pull from the vector store (default 20).
    bm25_candidates:   How many BM25 hits to use for fusion (default 20).
    rerank_top_n:      Final cross-encoder output size (default 5).
    """

    def __init__(
        self,
        vector_store:    VectorStoreBase,
        embedder:        OpenAIEmbeddings,
        reranker:        CohereReranker | None = None,
        dense_candidates: int = 20,
        bm25_candidates:  int = 20,
        rerank_top_n:     int = 5,
    ) -> None:
        self._store        = vector_store
        self._embedder     = embedder
        self._reranker     = reranker or CohereReranker()
        self._dense_k      = dense_candidates
        self._bm25_k       = bm25_candidates
        self._rerank_top_n = rerank_top_n

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    async def retrieve(
        self,
        query:           str,
        top_k:           int              = 5,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[Document]:
        """
        Execute the full hybrid retrieval pipeline.

        Args:
            query:           Raw user query string.
            top_k:           Number of final documents to return (≤ rerank_top_n).
            metadata_filter: Hard metadata filter applied on the vector query.
                             Typical: {"document_permissions": {"$in": ["admin", "user"]}}

        Returns:
            LangChain Document list, best-to-worst cross-encoder relevance order.
            Each document's metadata includes:
              - vector_score:        original dense cosine similarity
              - rrf_score:           reciprocal rank fusion combined score
              - rerank_score:        Cohere cross-encoder score (if available)
              - rerank_original_rank: position before reranking
        """
        t0 = time.perf_counter()

        # ── Step 1: Embed query ──────────────────────────────────────────────
        query_vector = await self._embedder.aembed_query(query)

        # ── Step 2: Dense retrieval ──────────────────────────────────────────
        dense_results: list[QueryResult] = await self._store.query(
            vector=query_vector,
            top_k=self._dense_k,
            filter=metadata_filter,
        )

        if not dense_results:
            logger.info("HybridRetriever | no dense results | tenant=%s", self._store.tenant_id)
            return []

        # ── Step 3: BM25 keyword retrieval over the dense corpus ─────────────
        bm25_pairs = self._bm25_search(query, dense_results)

        # ── Step 4: Reciprocal Rank Fusion ───────────────────────────────────
        fused = self._rrf_merge(dense_results, bm25_pairs)

        # ── Step 5: Permission hard-filter ───────────────────────────────────
        if metadata_filter:
            fused = self._apply_permission_filter(fused, metadata_filter)
            if not fused:
                logger.warning(
                    "HybridRetriever | all results filtered by permissions | tenant=%s",
                    self._store.tenant_id,
                )
                return []

        # ── Step 6: Convert to LangChain Documents for the reranker ──────────
        # Feed up to dense_candidates items into the cross-encoder
        candidates: list[Document] = [
            Document(
                page_content=qr.text,
                metadata={
                    **qr.metadata,
                    "chunk_id":     qr.id,
                    "vector_score": round(qr.score, 4),
                    "rrf_score":    round(getattr(qr, "_rrf_score", 0.0), 6),
                },
            )
            for qr in fused[: self._dense_k]
        ]

        # ── Step 7: Cross-encoder reranking ──────────────────────────────────
        final_docs = await self._reranker.rerank(
            query=query,
            candidates=candidates,
            top_n=min(top_k, self._rerank_top_n),
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "HybridRetriever | dense=%d bm25=%d fused=%d candidates=%d "
            "returned=%d elapsed_ms=%.1f | tenant=%s",
            len(dense_results), len(bm25_pairs), len(fused),
            len(candidates), len(final_docs), elapsed_ms,
            self._store.tenant_id,
        )
        return final_docs

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _bm25_search(
        self,
        query:  str,
        corpus: list[QueryResult],
    ) -> list[tuple[QueryResult, float]]:
        """
        Build a transient BM25 index over the dense corpus and search it.

        Returns list of (QueryResult, bm25_score) sorted by score descending.
        On any error (e.g. rank_bm25 not installed), returns empty list so
        the pipeline degrades gracefully to dense-only.
        """
        try:
            index = TenantBM25Index.build(corpus)
            hits  = index.search(query, top_k=self._bm25_k)
            return [(h.query_result, h.bm25_score) for h in hits]
        except Exception as exc:
            logger.warning(
                "BM25 search failed — continuing with dense-only results: %s", exc
            )
            return []

    @staticmethod
    def _rrf_merge(
        dense_results: list[QueryResult],
        bm25_pairs:    list[tuple[QueryResult, float]],
    ) -> list[QueryResult]:
        """
        Merge two ranked lists with Reciprocal Rank Fusion.

        Each document accumulates:
            RRF(d) = Σ  1 / (k + rank_in_list_i)

        Documents appearing in both lists score higher than those in one only.
        Documents appearing in only one list still contribute their list's RRF score.
        """
        rrf_scores: dict[str, float]       = {}
        qr_by_id:   dict[str, QueryResult] = {}

        # Dense contributions
        for rank, qr in enumerate(dense_results, start=1):
            rrf_scores[qr.id] = rrf_scores.get(qr.id, 0.0) + _rrf_score(rank)
            qr_by_id[qr.id]   = qr

        # BM25 contributions
        for rank, (qr, _score) in enumerate(bm25_pairs, start=1):
            rrf_scores[qr.id] = rrf_scores.get(qr.id, 0.0) + _rrf_score(rank)
            qr_by_id.setdefault(qr.id, qr)

        # Sort by combined RRF score
        sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)

        result: list[QueryResult] = []
        for chunk_id in sorted_ids:
            qr = qr_by_id[chunk_id]
            qr._rrf_score = rrf_scores[chunk_id]  # type: ignore[attr-defined]
            result.append(qr)
        return result

    @staticmethod
    def _apply_permission_filter(
        results:         list[QueryResult],
        metadata_filter: dict,
    ) -> list[QueryResult]:
        """
        Defence-in-depth document permission check.

        The vector store query should already have applied this filter, but we
        verify here too to guard against misconfigured store filters.

        Only filters when "document_permissions" is a list of allowed roles.
        Chunks with no permissions set are treated as world-readable.
        """
        permitted: list[str] | set[str] = metadata_filter.get("document_permissions", [])
        if not permitted:
            return results

        permitted_set = set(permitted) if not isinstance(permitted, set) else permitted

        filtered: list[QueryResult] = []
        for qr in results:
            doc_perms: list[str] = qr.metadata.get("document_permissions", [])
            # No restriction → allowed; OR if user's role is in document's allowed list
            if not doc_perms or bool(permitted_set & set(doc_perms)):
                filtered.append(qr)
        return filtered
