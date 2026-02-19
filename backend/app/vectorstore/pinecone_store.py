"""
Pinecone Vector Store — Tenant Namespace Isolation

Isolation model:
  Pinecone supports first-class namespaces within an index.
  Every tenant maps to its own namespace: "tenant_<tenant_id>"

  - All upserts go to namespace=tenant_<id>
  - All queries are forced to namespace=tenant_<id>
  - No cross-namespace query API is exposed
  - Metadata filter always includes tenant_id as a secondary guard
    (defence-in-depth: even if namespace were somehow omitted, the
     metadata filter blocks cross-tenant results)

Architecture:
  One shared Pinecone index, many namespaces.
  Namespace creation is implicit — Pinecone creates it on first upsert.
  No provisioning step required.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from pinecone import Pinecone, ServerlessSpec
from pinecone.core.client.exceptions import PineconeException

from app.core.config import settings
from app.vectorstore.base import QueryResult, VectorRecord, VectorStoreBase

logger = logging.getLogger(__name__)


class PineconeVectorStore(VectorStoreBase):
    """
    Tenant-scoped Pinecone vector store.

    One instance per tenant per request (created via FastAPI dependency).
    The namespace is derived from tenant_id at construction time and cannot
    be changed after instantiation.
    """

    def __init__(self, tenant_id: UUID) -> None:
        super().__init__(tenant_id)
        self._pc    = Pinecone(api_key=settings.pinecone_api_key)
        self._index = self._pc.Index(settings.pinecone_index_name)

    # ------------------------------------------------------------------
    # Namespace
    # ------------------------------------------------------------------

    def _namespace(self) -> str:
        """
        Pinecone namespace for this tenant.
        Pattern: tenant_<uuid>
        Example: tenant_3fa85f64-5717-4562-b3fc-2c963f66afa6
        """
        return f"tenant_{self._tenant_id}"

    # ------------------------------------------------------------------
    # Metadata guard
    # ------------------------------------------------------------------

    def _tenant_filter(self, extra: dict | None = None) -> dict:
        """
        Build a Pinecone metadata filter that ALWAYS scopes to this tenant.
        Any caller-supplied filters are ANDed in — they cannot remove the
        tenant_id constraint.
        """
        base = {"tenant_id": {"$eq": str(self._tenant_id)}}
        if extra:
            return {"$and": [base, extra]}
        return base

    def _validate_record(self, record: VectorRecord) -> None:
        """Reject records whose metadata tenant_id doesn't match this instance."""
        rec_tid = record.metadata.get("tenant_id")
        if rec_tid != str(self._tenant_id):
            raise ValueError(
                f"Record tenant_id mismatch: expected {self._tenant_id}, got {rec_tid}"
            )

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def upsert(self, records: list[VectorRecord], batch_size: int = 100) -> int:
        """
        Upsert vectors into the tenant's namespace.
        Validates every record's tenant_id before sending to Pinecone.
        Batches to stay within Pinecone's 2MB request limit.
        """
        total = 0
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]

            vectors = []
            for rec in batch:
                self._validate_record(rec)
                vectors.append({
                    "id":       rec.id,
                    "values":   rec.vector,
                    "metadata": rec.metadata,
                })

            self._index.upsert(
                vectors=vectors,
                namespace=self._namespace(),
            )
            total += len(batch)
            logger.debug(
                "Pinecone upsert | tenant=%s namespace=%s batch=%d total=%d",
                self._tenant_id, self._namespace(), len(batch), total,
            )

        return total

    async def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[QueryResult]:
        """
        Similarity search scoped to tenant namespace + metadata filter.
        top_k is capped at 100 (Pinecone limit for metadata-filtered queries).
        """
        top_k = min(top_k, 100)

        resp = self._index.query(
            vector=vector,
            top_k=top_k,
            namespace=self._namespace(),
            filter=self._tenant_filter(filter),
            include_metadata=True,
            include_values=False,   # save bandwidth — values not needed for RAG
        )

        results = []
        for match in resp.get("matches", []):
            meta = match.get("metadata", {})
            results.append(QueryResult(
                id=match["id"],
                score=match["score"],
                metadata=meta,
                text=meta.get("text", ""),
            ))

        logger.debug(
            "Pinecone query | tenant=%s top_k=%d results=%d",
            self._tenant_id, top_k, len(results),
        )
        return results

    async def delete(self, ids: list[str]) -> None:
        """Delete specific vector IDs from the tenant's namespace."""
        if not ids:
            return
        self._index.delete(ids=ids, namespace=self._namespace())
        logger.info(
            "Pinecone delete | tenant=%s count=%d", self._tenant_id, len(ids)
        )

    async def delete_by_document(self, document_id: str) -> None:
        """
        Delete all chunks for a document by metadata filter.
        Pinecone supports delete-by-metadata on paid tiers (p1/p2/s1).
        Falls back to list+delete on starter tier.
        """
        try:
            self._index.delete(
                namespace=self._namespace(),
                filter={
                    "$and": [
                        {"tenant_id":   {"$eq": str(self._tenant_id)}},
                        {"document_id": {"$eq": document_id}},
                    ]
                },
            )
            logger.info(
                "Pinecone delete_by_document | tenant=%s doc=%s",
                self._tenant_id, document_id,
            )
        except PineconeException as exc:
            # Starter tier: fall back to list-then-delete
            logger.warning("Metadata delete unavailable, using list fallback: %s", exc)
            await self._list_delete_by_document(document_id)

    async def _list_delete_by_document(self, document_id: str) -> None:
        """Fallback: paginate through namespace and delete matching IDs."""
        ids_to_delete: list[str] = []
        for id_batch in self._index.list(namespace=self._namespace()):
            for vec_id in id_batch:
                if document_id in vec_id:   # IDs are prefixed with document_id
                    ids_to_delete.append(vec_id)
        if ids_to_delete:
            await self.delete(ids_to_delete)

    async def count(self) -> int:
        """Return vector count in the tenant's namespace."""
        stats = self._index.describe_index_stats()
        ns_stats = stats.get("namespaces", {}).get(self._namespace(), {})
        return ns_stats.get("vector_count", 0)

    # ------------------------------------------------------------------
    # Class-level: index provisioning (run once at platform setup)
    # ------------------------------------------------------------------

    @classmethod
    def ensure_index(cls) -> None:
        """
        Create the shared Pinecone index if it doesn't exist.
        Called at application startup, not per-request.
        Serverless spec (AWS us-east-1) — adjust region for your deployment.
        """
        pc = Pinecone(api_key=settings.pinecone_api_key)
        existing = [i.name for i in pc.list_indexes()]
        if settings.pinecone_index_name in existing:
            logger.info("Pinecone index '%s' already exists", settings.pinecone_index_name)
            return

        pc.create_index(
            name=settings.pinecone_index_name,
            dimension=settings.embedding_dimensions,     # 1536 for text-embedding-3-small
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region=settings.aws_region),
        )
        logger.info("Pinecone index '%s' created", settings.pinecone_index_name)
