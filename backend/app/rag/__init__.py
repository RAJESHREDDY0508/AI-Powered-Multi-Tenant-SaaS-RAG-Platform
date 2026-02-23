"""
RAG package — lazy public API.

Heavy dependencies (rank_bm25, cohere, spacy) are only imported when the
specific class is first used, so tests that mock these components don't
require all optional packages to be installed.
"""

from app.rag.retriever import TenantScopedRetriever

__all__ = [
    "TenantScopedRetriever",
    # build_rag_chain, embed_query, embed_documents — import directly from app.rag.pipeline
    # HybridRetriever                              — import directly from app.rag.hybrid_retriever
    # PromptManager                                — import directly from app.rag.prompt_manager
]
