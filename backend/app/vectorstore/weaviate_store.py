"""
Weaviate Vector Store — Tenant Collection (Class) Isolation

Isolation model:
  Weaviate organises data into "classes" (collections).
  Each tenant gets its own class: "Tenant_<sanitized_tenant_id>"

  Weaviate v4 also supports native multi-tenancy within a single class
  (tenant shards). We use the COLLECTION-per-tenant model here because:
    1. It provides the strongest isolation (separate HNSW graphs).
    2. Per-collection RBAC can be applied in Weaviate Enterprise.
    3. It avoids the 10,000-tenant-per-class soft limit for large deployments.

  For deployments with 1,000+ tenants, switch to Weaviate's built-in
  multi-tenancy (MT) mode by setting WEAVIATE_USE_MT=true in .env —
  the interface stays identical; only _namespace() and collection setup change.
"""

from __future__ import annotations

import logging
from uuid import UUID

import weaviate
import weaviate.classes as wvc
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import MetadataQuery

from app.core.config import settings
from app.vectorstore.base import QueryResult, VectorRecord, VectorStoreBase

logger = logging.getLogger(__name__)

# Weaviate class names must be PascalCase and start with a letter
_CLASS_PREFIX = "Tenant"


class WeaviateVectorStore(VectorStoreBase):
    """
    Tenant-scoped Weaviate vector store.

    Each instance is bound to one tenant. The collection name is derived
    from the tenant_id at construction and cannot be changed.
    """

    def __init__(self, tenant_id: UUID, client: weaviate.WeaviateClient) -> None:
        super().__init__(tenant_id)
        self._client = client
        self._ensure_collection()

    # ------------------------------------------------------------------
    # Namespace / collection name
    # ------------------------------------------------------------------

    def _namespace(self) -> str:
        """
        Weaviate collection name for this tenant.
        UUIDs contain hyphens which are invalid in Weaviate class names,
        so we strip them.
        Pattern: Tenant_<uuid_no_hyphens>
        Example: Tenant_3fa85f645717456...
        """
        safe_id = str(self._tenant_id).replace("-", "")
        return f"{_CLASS_PREFIX}_{safe_id}"

    # ------------------------------------------------------------------
    # Collection provisioning (idempotent, called at __init__)
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> None:
        """
        Create the tenant's Weaviate collection if it doesn't exist.
        Called at construction time — cheap if collection already exists.
        """
        name = self._namespace()
        if self._client.collections.exists(name):
            return

        self._client.collections.create(
            name=name,
            description=f"Document chunks for tenant {self._tenant_id}",
            vectorizer_config=Configure.Vectorizer.none(),   # we supply our own vectors
            vector_index_config=Configure.VectorIndex.hnsw(
                distance_metric=wvc.config.VectorDistances.COSINE,
                ef_construction=128,
                max_connections=64,
            ),
            properties=[
                Property(name="tenant_id",    data_type=DataType.TEXT,   index_filterable=True),
                Property(name="document_id",  data_type=DataType.TEXT,   index_filterable=True),
                Property(name="chunk_index",  data_type=DataType.INT,    index_filterable=True),
                Property(name="text",         data_type=DataType.TEXT,   index_searchable=True),
                Property(name="source_key",   data_type=DataType.TEXT,   index_filterable=True),
            ],
        )
        logger.info("Weaviate collection created: %s", name)

    def _collection(self):
        """Return the tenant's Weaviate collection handle."""
        return self._client.collections.get(self._namespace())

    # ------------------------------------------------------------------
    # Validation guard
    # ------------------------------------------------------------------

    def _validate_record(self, record: VectorRecord) -> None:
        if record.metadata.get("tenant_id") != str(self._tenant_id):
            raise ValueError(
                f"Record tenant_id mismatch: expected {self._tenant_id}, "
                f"got {record.metadata.get('tenant_id')}"
            )

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def upsert(self, records: list[VectorRecord], batch_size: int = 100) -> int:
        """
        Batch upsert into tenant's collection using Weaviate's batch API.
        Uses insert_many for new objects; existing IDs are overwritten.
        """
        collection = self._collection()
        total = 0

        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            objects = []
            for rec in batch:
                self._validate_record(rec)
                objects.append(
                    wvc.data.DataObject(
                        uuid=rec.id,
                        properties={
                            "tenant_id":   str(self._tenant_id),
                            "document_id": rec.metadata.get("document_id", ""),
                            "chunk_index": rec.metadata.get("chunk_index", 0),
                            "text":        rec.metadata.get("text", ""),
                            "source_key":  rec.metadata.get("source_key", ""),
                        },
                        vector=rec.vector,
                    )
                )

            result = collection.data.insert_many(objects)
            if result.has_errors:
                for err in result.errors.values():
                    logger.error("Weaviate upsert error: %s", err)

            total += len(batch)
            logger.debug(
                "Weaviate upsert | tenant=%s batch=%d total=%d",
                self._tenant_id, len(batch), total,
            )

        return total

    async def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter: dict | None = None,
    ) -> list[QueryResult]:
        """
        Near-vector search within the tenant's collection.
        The collection itself is the isolation boundary.
        Additional metadata filters are applied on top.
        """
        collection = self._collection()

        # Build Weaviate filter from caller-supplied dict
        wv_filter = self._build_filter(filter) if filter else None

        response = collection.query.near_vector(
            near_vector=vector,
            limit=top_k,
            return_metadata=MetadataQuery(distance=True, score=True),
            return_properties=["tenant_id", "document_id", "chunk_index", "text", "source_key"],
            filters=wv_filter,
        )

        results = []
        for obj in response.objects:
            props = obj.properties
            score = 1.0 - (obj.metadata.distance or 0.0)  # convert distance → similarity
            results.append(QueryResult(
                id=str(obj.uuid),
                score=round(score, 4),
                metadata=dict(props),
                text=props.get("text", ""),
            ))

        logger.debug(
            "Weaviate query | tenant=%s top_k=%d results=%d",
            self._tenant_id, top_k, len(results),
        )
        return results

    def _build_filter(self, filter_dict: dict):
        """
        Convert simple {field: value} dict to a Weaviate Filter object.
        Extend as needed for complex AND/OR filters.
        """
        from weaviate.classes.query import Filter
        clauses = [
            Filter.by_property(k).equal(v)
            for k, v in filter_dict.items()
        ]
        if len(clauses) == 1:
            return clauses[0]
        return Filter.all_of(clauses)

    async def delete(self, ids: list[str]) -> None:
        """Delete specific objects by UUID."""
        if not ids:
            return
        collection = self._collection()
        for obj_id in ids:
            collection.data.delete_by_id(obj_id)
        logger.info("Weaviate delete | tenant=%s count=%d", self._tenant_id, len(ids))

    async def delete_by_document(self, document_id: str) -> None:
        """Delete all chunks for a document using a metadata filter."""
        from weaviate.classes.query import Filter
        collection = self._collection()
        collection.data.delete_many(
            where=Filter.by_property("document_id").equal(document_id)
        )
        logger.info(
            "Weaviate delete_by_document | tenant=%s doc=%s",
            self._tenant_id, document_id,
        )

    async def count(self) -> int:
        """Return number of objects in the tenant's collection."""
        agg = self._collection().aggregate.over_all(total_count=True)
        return agg.total_count or 0


# ---------------------------------------------------------------------------
# Client factory — call once at startup and share via app state
# ---------------------------------------------------------------------------

def create_weaviate_client() -> weaviate.WeaviateClient:
    """
    Create and return a connected Weaviate client.
    Supports both local (Docker) and Weaviate Cloud (WCS) modes.
    """
    if settings.weaviate_api_key:
        # Weaviate Cloud Service
        return weaviate.connect_to_wcs(
            cluster_url=settings.weaviate_url,
            auth_credentials=weaviate.auth.AuthApiKey(settings.weaviate_api_key),
        )
    # Local / Docker
    return weaviate.connect_to_local(
        host=settings.weaviate_host,
        port=settings.weaviate_port,
    )
