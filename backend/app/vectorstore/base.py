"""
Vector Store — Abstract Base

Every concrete vector store backend (Pinecone, Weaviate, FAISS) implements
this interface. The rest of the application only speaks this protocol,
so backends are swappable without changing RAG or API code.

Tenant isolation contract (enforced by ALL implementations):
  - Every upsert/query/delete MUST scope to the tenant namespace/partition.
  - The namespace is derived ONLY from the authenticated tenant_id,
    never from user-supplied input.
  - Cross-namespace operations are not exposed on this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from uuid import UUID


# ---------------------------------------------------------------------------
# Shared data types
# ---------------------------------------------------------------------------

@dataclass
class VectorRecord:
    """A single embedding record to upsert into the vector store."""
    id:        str              # deterministic: sha256(tenant_id + chunk_id)
    vector:    list[float]      # embedding from OpenAI / Llama / Mistral
    metadata:  dict             # filterable payload stored alongside the vector
    # Required fields inside metadata (enforced at upsert time):
    # - tenant_id: str
    # - document_id: str
    # - chunk_index: int
    # - text: str               (the raw chunk text — returned in results)
    # - source_key: str         (S3 key of the originating document)


@dataclass
class QueryResult:
    """One result returned from a similarity search."""
    id:         str
    score:      float           # cosine similarity (0–1 for normalized vectors)
    metadata:   dict
    text:       str = field(default="")   # convenience alias for metadata["text"]

    def __post_init__(self) -> None:
        if not self.text and "text" in self.metadata:
            self.text = self.metadata["text"]


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class VectorStoreBase(ABC):
    """
    Tenant-scoped vector store interface.

    Each instance is bound to a single tenant_id at construction time.
    There is no method to query across tenants — that operation does not exist.
    """

    def __init__(self, tenant_id: UUID) -> None:
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> UUID:
        return self._tenant_id

    @abstractmethod
    def _namespace(self) -> str:
        """
        Return the backend-specific isolation key for this tenant.
        Pinecone  → namespace string
        Weaviate  → class/collection name
        FAISS     → index file prefix
        """

    @abstractmethod
    async def upsert(self, records: list[VectorRecord], batch_size: int = 100) -> int:
        """
        Insert or update embedding records.
        Returns the number of vectors upserted.
        Implementations MUST verify record.metadata["tenant_id"] == str(self.tenant_id).
        """

    @abstractmethod
    async def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[QueryResult]:
        """
        Nearest-neighbour search within the tenant's namespace ONLY.
        `filter` applies additional metadata filters on top of the namespace scope.
        """

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        """Delete vectors by ID within the tenant's namespace."""

    @abstractmethod
    async def delete_by_document(self, document_id: str) -> None:
        """Delete ALL chunks belonging to a document (used when a doc is removed)."""

    @abstractmethod
    async def count(self) -> int:
        """Return total vectors in this tenant's namespace."""
