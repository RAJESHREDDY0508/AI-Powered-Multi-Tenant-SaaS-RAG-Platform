"""
RAG Pipeline — LangChain LCEL Orchestration (v2 — Hybrid + Versioned Prompts)

Full query pipeline:

  User Query
    │
    ▼
  HybridRetriever          ← Dense + BM25 + RRF + Cohere ReRank
    │
    ▼
  LongContextReorder       ← Combat "Lost in the Middle" LLM bias
    │
    ▼
  PromptManager            ← DB-versioned system prompt with A/B routing
    │
    ▼
  LLMGateway               ← Model router + fallback chain (GPT-4o → Azure → Bedrock)
    │
    ▼
  Streaming Response       ← SSE token-by-token to the client

Tenant guarantee:
  - HybridRetriever is bound to a tenant-scoped VectorStoreBase.
  - PromptManager always injects: "You are a private assistant for {tenant_name}."
  - The LLM NEVER receives chunks from another tenant.
"""

from __future__ import annotations

import logging
from uuid import UUID

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.prompt_manager import PromptManager
from app.rag.reranker import CohereReranker
from app.vectorstore.base import VectorStoreBase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared singletons (one per process — thread-safe for reads)
# ---------------------------------------------------------------------------

_reranker: CohereReranker | None = None


def _get_reranker() -> CohereReranker:
    """Lazily initialise the Cohere reranker singleton."""
    global _reranker
    if _reranker is None:
        _reranker = CohereReranker()
    return _reranker


# ---------------------------------------------------------------------------
# Embedding service
# ---------------------------------------------------------------------------

def get_embedding_model() -> OpenAIEmbeddings:
    """
    Return the configured OpenAI embedding model.

    text-embedding-3-small → 1536 dims  (default, cost-efficient)
    text-embedding-3-large → 3072 dims  (higher accuracy, 2× cost)
    """
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
        dimensions=settings.embedding_dimensions,
    )


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def get_llm(streaming: bool = False) -> ChatOpenAI:
    """
    Return the configured primary LLM.

    streaming=True enables token-by-token SSE delivery to the client.
    For multi-provider fallback, use LLMGateway instead.
    """
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=settings.llm_temperature,
        streaming=streaming,
        max_tokens=settings.llm_max_tokens,
    )


# ---------------------------------------------------------------------------
# RAG chain factory (production version with hybrid + versioned prompts)
# ---------------------------------------------------------------------------

async def build_rag_chain(
    vector_store: VectorStoreBase,
    tenant_id:    UUID,
    tenant_name:  str,
    db:           AsyncSession,
    streaming:    bool  = False,
    top_k:        int   = 5,
    score_threshold: float = 0.3,
):
    """
    Build and return a fully-async RAG chain for one tenant.

    Chain signature::
        Input:  {"question": str}
        Output: str   (or AsyncIterator[str] if streaming=True)

    Improvements over v1 pipeline:
      - HybridRetriever (Dense + BM25 + Cohere ReRank) replaces plain vector search.
      - DB-versioned system prompts with A/B testing via PromptManager.
      - LongContextReorder applied before prompt injection.

    Args:
        vector_store:     Tenant-scoped vector store (from DI).
        tenant_id:        UUID from authenticated JWT.
        tenant_name:      Human-readable org name for prompt injection.
        db:               Async DB session for prompt loading.
        streaming:        Enable token streaming (for SSE endpoints).
        top_k:            Final number of context chunks to inject.
        score_threshold:  Minimum similarity score (soft filter on dense results).

    Returns:
        LangChain Runnable (LCEL chain).
    """
    embedder    = get_embedding_model()
    reranker    = _get_reranker()
    pm          = PromptManager(prompt_name="rag_system")
    llm         = get_llm(streaming=streaming)

    retriever = HybridRetriever(
        vector_store=vector_store,
        embedder=embedder,
        reranker=reranker,
        dense_candidates=max(top_k * 4, 20),   # pull 4× more candidates for reranking
        bm25_candidates=max(top_k * 4, 20),
        rerank_top_n=top_k,
    )

    # Load the system prompt from DB (with A/B variant selection)
    system_template = await pm.get_system_prompt(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        db=db,
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_template),
        ("human",  "{question}"),
    ])

    async def retrieve_and_format(inputs: dict) -> str:
        """Hybrid retrieval → LongContextReorder → context string."""
        docs = await retriever.retrieve(
            query=inputs["question"],
            top_k=top_k,
        )
        # Filter by score threshold (applied on rerank_score or vector_score)
        docs = [
            d for d in docs
            if (d.metadata.get("rerank_score") or d.metadata.get("vector_score", 1.0))
            >= score_threshold
        ]
        # LongContextReorder — highest relevance at start & end
        docs = pm.reorder_context(docs)
        return pm.format_context(docs)

    # LCEL pipeline
    chain = (
        {
            "context":  retrieve_and_format,
            "question": RunnablePassthrough() | (lambda x: x["question"]),
        }
        | prompt
        | llm
        | StrOutputParser()
    )

    return chain


# ---------------------------------------------------------------------------
# Convenience embedding helpers (used by ingestion pipeline)
# ---------------------------------------------------------------------------

async def embed_query(text: str) -> list[float]:
    """Embed a single query string. Used during retrieval."""
    return await get_embedding_model().aembed_query(text)


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts. Used during ingestion."""
    return await get_embedding_model().aembed_documents(texts)
