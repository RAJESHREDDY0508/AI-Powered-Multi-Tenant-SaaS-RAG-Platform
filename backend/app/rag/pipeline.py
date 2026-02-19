"""
RAG Pipeline — LangChain LCEL Orchestration

Implements the full Retrieve → Augment → Generate flow.

Architecture:
  User Query
    │
    ▼
  EmbeddingService        ← embed the query (OpenAI / Llama)
    │
    ▼
  TenantScopedRetriever   ← vector search (Pinecone / Weaviate namespace)
    │
    ▼
  PromptBuilder           ← inject retrieved chunks into system prompt
    │
    ▼
  LLMService              ← OpenAI Chat / local Llama3 / Mistral
    │
    ▼
  Streaming Response      ← SSE stream to the client

Tenant guarantee:
  - Retriever is instantiated with a tenant-scoped VectorStoreBase.
  - System prompt always injects: "You are a private assistant for {tenant_name}."
  - The LLM never receives chunks from another tenant — retriever enforces this.
"""

from __future__ import annotations

import logging
from uuid import UUID

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableParallel, RunnablePassthrough
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.core.config import settings
from app.rag.retriever import TenantScopedRetriever
from app.vectorstore.base import VectorStoreBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are a private AI assistant for {tenant_name}.
You answer questions ONLY using the provided context from the company's documents.
If the answer is not in the context, say "I don't have enough information to answer that."
Do not fabricate information. Do not reference information outside the provided context.

Context:
{context}
"""

_HUMAN_TEMPLATE = "{question}"


# ---------------------------------------------------------------------------
# Embedding service
# ---------------------------------------------------------------------------

def get_embedding_model() -> OpenAIEmbeddings:
    """
    Returns the configured embedding model.
    text-embedding-3-small  → 1536 dims  (default, cost-efficient)
    text-embedding-3-large  → 3072 dims  (higher accuracy)
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
    Returns the configured LLM.
    Streaming=True enables token-by-token SSE streaming to the client.
    """
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=settings.llm_temperature,
        streaming=streaming,
        max_tokens=settings.llm_max_tokens,
    )


# ---------------------------------------------------------------------------
# RAG chain factory
# ---------------------------------------------------------------------------

def build_rag_chain(
    vector_store: VectorStoreBase,
    tenant_name: str,
    streaming: bool = False,
    top_k: int = 5,
    score_threshold: float = 0.3,
):
    """
    Build and return a LangChain LCEL RAG chain scoped to one tenant.

    The chain signature:
        Input:  {"question": str}
        Output: str  (or async stream of str tokens if streaming=True)

    Args:
        vector_store:     Tenant-scoped vector store (from dependency injection).
        tenant_name:      Human-readable tenant name (injected into system prompt).
        streaming:        Enable token streaming.
        top_k:            Number of chunks to retrieve.
        score_threshold:  Minimum similarity score to include a chunk.

    Returns:
        A LangChain Runnable (LCEL chain).
    """
    embedder  = get_embedding_model()
    retriever = TenantScopedRetriever(
        vector_store=vector_store,
        top_k=top_k,
        score_threshold=score_threshold,
    )
    llm       = get_llm(streaming=streaming)

    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_TEMPLATE),
        ("human",  _HUMAN_TEMPLATE),
    ])

    def format_docs(docs) -> str:
        """Concatenate retrieved chunk texts with separators."""
        return "\n\n---\n\n".join(
            f"[Source: {d.metadata.get('source_key', 'unknown')} | "
            f"Score: {d.metadata.get('score', 0):.3f}]\n{d.page_content}"
            for d in docs
        )

    async def embed_and_retrieve(inputs: dict) -> list:
        """Embed the question, then retrieve relevant chunks."""
        question = inputs["question"]
        vector   = await embedder.aembed_query(question)
        return await retriever._aget_relevant_documents(
            vector,
            run_manager=None,
        )

    # LCEL pipeline
    chain = (
        RunnableParallel({
            "context":  embed_and_retrieve | format_docs,
            "question": RunnablePassthrough() | (lambda x: x["question"]),
            "tenant_name": RunnablePassthrough() | (lambda _: tenant_name),
        })
        | prompt
        | llm
        | StrOutputParser()
    )

    return chain


# ---------------------------------------------------------------------------
# Convenience: embed a single query string
# ---------------------------------------------------------------------------

async def embed_query(text: str) -> list[float]:
    """Embed a single text string. Used by the ingestion pipeline."""
    embedder = get_embedding_model()
    return await embedder.aembed_query(text)


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts. Used during document ingestion."""
    embedder = get_embedding_model()
    return await embedder.aembed_documents(texts)
