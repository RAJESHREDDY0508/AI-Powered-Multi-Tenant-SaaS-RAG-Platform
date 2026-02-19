"""
RAG Retriever — LangChain Integration

Wraps the tenant-scoped VectorStoreBase inside a LangChain-compatible
retriever so that LangChain chains (RetrievalQA, ConversationalRetrievalChain,
LCEL pipelines) can consume it without knowing the underlying backend.

Tenant isolation is inherited from VectorStoreBase — the retriever can
ONLY return chunks from the authenticated tenant's namespace/collection.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from app.vectorstore.base import VectorStoreBase

logger = logging.getLogger(__name__)


class TenantScopedRetriever(BaseRetriever):
    """
    LangChain BaseRetriever backed by our VectorStoreBase.

    Exposes only _get_relevant_documents() (sync wrapper over async query).
    The async variant _aget_relevant_documents() is used when the chain
    runs in an async context (FastAPI streaming endpoints).

    Every call is automatically scoped to the tenant — no configuration
    needed beyond passing the tenant-scoped store at construction.
    """

    vector_store: VectorStoreBase
    top_k:        int  = 5
    score_threshold: float = 0.0   # filter results below this similarity score

    class Config:
        arbitrary_types_allowed = True   # needed for VectorStoreBase

    def _get_relevant_documents(
        self,
        query_vector: list[float],
        *,
        run_manager: CallbackManagerForRetrieverRun,
        metadata_filter: dict | None = None,
    ) -> list[Document]:
        """Sync entrypoint (LangChain calls this in non-async contexts)."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self._aget_relevant_documents(
                query_vector,
                run_manager=run_manager,
                metadata_filter=metadata_filter,
            )
        )

    async def _aget_relevant_documents(
        self,
        query_vector: list[float],
        *,
        run_manager: CallbackManagerForRetrieverRun,
        metadata_filter: dict | None = None,
    ) -> list[Document]:
        """
        Async entrypoint — used by FastAPI streaming endpoints.
        Queries the tenant vector store, filters by score, returns LangChain Documents.
        """
        results = await self.vector_store.query(
            vector=query_vector,
            top_k=self.top_k,
            filter=metadata_filter,
        )

        docs = []
        for result in results:
            if result.score < self.score_threshold:
                continue
            docs.append(Document(
                page_content=result.text,
                metadata={
                    **result.metadata,
                    "score":    result.score,
                    "chunk_id": result.id,
                },
            ))

        logger.debug(
            "Retriever | tenant=%s query_results=%d above_threshold=%d",
            self.vector_store.tenant_id, len(results), len(docs),
        )
        return docs
