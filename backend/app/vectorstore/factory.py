"""
Vector Store Factory

Selects the correct backend (Pinecone | Weaviate) based on config.
The rest of the app only imports get_vector_store() — never touches
the concrete classes directly.

Usage in a FastAPI route (via dependency):
    store: VectorStoreBase = Depends(get_vector_store_dep)
"""

from __future__ import annotations

from uuid import UUID

from app.core.config import settings
from app.vectorstore.base import VectorStoreBase


def get_vector_store(tenant_id: UUID) -> VectorStoreBase:
    """
    Return a tenant-scoped vector store for the configured backend.
    Called per-request from the FastAPI dependency layer.
    """
    backend = settings.vector_store_backend.lower()

    if backend == "pinecone":
        from app.vectorstore.pinecone_store import PineconeVectorStore
        return PineconeVectorStore(tenant_id=tenant_id)

    if backend == "weaviate":
        from app.vectorstore.weaviate_store import WeaviateVectorStore, create_weaviate_client
        # In production: client is created once at startup and stored in app.state
        # Here we create it fresh for simplicity — wire app.state in main.py
        client = create_weaviate_client()
        return WeaviateVectorStore(tenant_id=tenant_id, client=client)

    raise ValueError(
        f"Unknown vector store backend: '{backend}'. "
        f"Valid options: 'pinecone', 'weaviate'"
    )
