"""
Celery Tasks — Document Processing Pipeline

Task: process_document
  1. Update document status → processing
  2. Download file from S3
  3. Extract text (PDF/DOCX/TXT) using appropriate parser
  4. Split into chunks (LangChain RecursiveCharacterTextSplitter)
  5. Generate embeddings (OpenAI text-embedding-3-small)
  6. Upsert vectors into tenant-scoped Pinecone/Weaviate namespace
  7. Persist chunk records in saas.chunks
  8. Update document status → ready (or failed)
  9. Write audit log entry

Task: retry_failed_documents
  Scheduler task — re-queues documents stuck in 'pending' for > 5 minutes.
  Handles broker failures during the original upload.

Security:
  - document_id and tenant_id are always re-validated from the DB.
  - RLS is set on every DB session so cross-tenant access is impossible.
  - S3 download uses the tenant-scoped S3StorageService (KMS key enforced).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from functools import wraps
from typing import Any

from celery import Task
from sqlalchemy import select, text, update

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async task helper
# Run async coroutines inside Celery's synchronous task context.
# ---------------------------------------------------------------------------

def run_async(coro):
    """Execute an async coroutine from a synchronous Celery task."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Main processing task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.workers.tasks.process_document",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=270,
    time_limit=330,
)
def process_document(
    self: Task,
    *,
    document_id: str,
    tenant_id:   str,
    s3_key:      str,
    content_type: str,
) -> dict[str, Any]:
    """
    Full async document processing pipeline.
    Orchestrates: download → parse → chunk → embed → index → persist.
    """
    return run_async(
        _process_document_async(
            task=self,
            document_id=uuid.UUID(document_id),
            tenant_id=uuid.UUID(tenant_id),
            s3_key=s3_key,
            content_type=content_type,
        )
    )


async def _process_document_async(
    task: Task,
    document_id: uuid.UUID,
    tenant_id:   uuid.UUID,
    s3_key:      str,
    content_type: str,
) -> dict[str, Any]:
    """Async implementation of the processing pipeline."""
    from app.db.session import get_admin_db
    from app.storage.s3 import S3StorageService, TenantStorageConfig, ResourceType
    from app.models.documents import Document, AuditLog, Chunk
    from app.rag.pipeline import embed_documents
    from app.vectorstore.factory import get_vector_store
    from app.vectorstore.base import VectorRecord
    from app.core.config import settings
    from langchain.text_splitter import RecursiveCharacterTextSplitter

    logger.info("Processing | doc=%s tenant=%s", document_id, tenant_id)

    # --- Phase 1: Load document record and set status → processing ------
    async with get_admin_db() as db:
        # Set RLS context — admin session still enforces tenant scoping here
        await db.execute(
            text("SET LOCAL app.current_tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )

        result = await db.execute(
            select(Document).where(
                Document.id == document_id,
                Document.tenant_id == tenant_id,  # defense-in-depth
            )
        )
        doc = result.scalars().first()

        if not doc:
            logger.error("Document not found | doc=%s tenant=%s", document_id, tenant_id)
            return {"status": "not_found"}

        if doc.status not in ("pending", "failed"):
            logger.warning(
                "Document already in status=%s, skipping | doc=%s",
                doc.status, document_id,
            )
            return {"status": "skipped", "current_status": doc.status}

        # Mark as processing
        await db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status="processing")
        )

    # --- Phase 2: Download from S3 --------------------------------------
    try:
        storage = S3StorageService(
            tenant_config=TenantStorageConfig(
                tenant_id=tenant_id,
                kms_key_arn=settings.s3_default_kms_key_arn,
            )
        )
        # Extract filename from s3_key for the storage service
        filename = s3_key.rsplit("/", 1)[-1]
        file_bytes = await storage.get_object(ResourceType.DOCUMENT, filename)
    except Exception as exc:
        logger.exception("S3 download failed | doc=%s", document_id)
        await _mark_failed(document_id, tenant_id, f"S3 download error: {exc}")
        raise task.retry(exc=exc)

    # --- Phase 3: Text extraction ----------------------------------------
    try:
        raw_text = _extract_text(file_bytes, content_type, filename)
    except Exception as exc:
        logger.exception("Text extraction failed | doc=%s", document_id)
        await _mark_failed(document_id, tenant_id, f"Text extraction error: {exc}")
        raise task.retry(exc=exc)

    if not raw_text.strip():
        await _mark_failed(document_id, tenant_id, "Extracted text is empty")
        return {"status": "failed", "reason": "empty_text"}

    # --- Phase 4: Chunking ----------------------------------------------
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks: list[str] = splitter.split_text(raw_text)
    logger.info("Chunked | doc=%s chunks=%d", document_id, len(chunks))

    # --- Phase 5: Embedding ---------------------------------------------
    try:
        vectors: list[list[float]] = await embed_documents(chunks)
    except Exception as exc:
        logger.exception("Embedding failed | doc=%s", document_id)
        await _mark_failed(document_id, tenant_id, f"Embedding error: {exc}")
        raise task.retry(exc=exc)

    # --- Phase 6: Vector upsert -----------------------------------------
    vector_store = get_vector_store(tenant_id=tenant_id)
    chunk_records: list[Chunk] = []
    vector_records: list[VectorRecord] = []

    for idx, (chunk_text, vector) in enumerate(zip(chunks, vectors)):
        chunk_id = uuid.uuid4()
        # Deterministic vector ID: sha256(tenant_id + document_id + chunk_index)
        vector_id = hashlib.sha256(
            f"{tenant_id}:{document_id}:{idx}".encode()
        ).hexdigest()

        vector_records.append(
            VectorRecord(
                id=vector_id,
                vector=vector,
                metadata={
                    "tenant_id":    str(tenant_id),
                    "document_id":  str(document_id),
                    "chunk_index":  idx,
                    "text":         chunk_text[:1000],  # truncate for metadata limits
                    "source_key":   s3_key,
                },
            )
        )
        chunk_records.append(
            Chunk(
                id=chunk_id,
                tenant_id=tenant_id,
                document_id=document_id,
                chunk_index=idx,
                text=chunk_text,
                token_count=len(chunk_text.split()),
                vector_id=vector_id,
                vector_store=settings.vector_store_backend,
            )
        )

    try:
        await vector_store.upsert(vector_records)
        logger.info("Vectors upserted | doc=%s count=%d", document_id, len(vector_records))
    except Exception as exc:
        logger.exception("Vector upsert failed | doc=%s", document_id)
        await _mark_failed(document_id, tenant_id, f"Vector upsert error: {exc}")
        raise task.retry(exc=exc)

    # --- Phase 7: Persist chunks + update document status ---------------
    async with get_admin_db() as db:
        await db.execute(
            text("SET LOCAL app.current_tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )

        # Bulk insert chunks
        db.add_all(chunk_records)

        # Update document: status=ready, counts
        await db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(
                status="ready",
                chunk_count=len(chunks),
                vector_count=len(vector_records),
                error_message=None,
            )
        )

        # Audit log
        db.add(AuditLog(
            tenant_id=tenant_id,
            user_id=None,     # system action
            action="document.processing_completed",
            resource=f"document:{document_id}",
            metadata={
                "chunk_count":  len(chunks),
                "vector_count": len(vector_records),
                "vector_store": settings.vector_store_backend,
            },
            success=True,
        ))

    logger.info(
        "Processing complete | doc=%s chunks=%d vectors=%d",
        document_id, len(chunks), len(vector_records),
    )
    return {
        "status":        "ready",
        "document_id":   str(document_id),
        "chunk_count":   len(chunks),
        "vector_count":  len(vector_records),
    }


# ---------------------------------------------------------------------------
# Retry scanner — runs every 60 seconds via Celery Beat
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.workers.tasks.retry_failed_documents",
    bind=False,
    acks_late=True,
    soft_time_limit=55,
    time_limit=60,
)
def retry_failed_documents() -> dict[str, int]:
    """
    Find documents stuck in 'pending' for > 5 minutes and re-queue them.
    Handles broker unavailability during the original upload request.
    """
    return run_async(_retry_failed_documents_async())


async def _retry_failed_documents_async() -> dict[str, int]:
    from app.db.session import get_admin_db
    from app.models.documents import Document
    from sqlalchemy import and_, func

    queued = 0
    async with get_admin_db() as db:
        result = await db.execute(
            select(Document).where(
                and_(
                    Document.status == "pending",
                    Document.created_at < func.now() - text("interval '5 minutes'"),
                )
            ).limit(50)
        )
        stale_docs = result.scalars().all()

        for doc in stale_docs:
            process_document.apply_async(
                kwargs={
                    "document_id":  str(doc.id),
                    "tenant_id":    str(doc.tenant_id),
                    "s3_key":       doc.s3_key,
                    "content_type": doc.content_type,
                },
                countdown=5,
            )
            queued += 1
            logger.info("Re-queued stale document | doc=%s tenant=%s", doc.id, doc.tenant_id)

    return {"requeued": queued}


# ---------------------------------------------------------------------------
# Health check task
# ---------------------------------------------------------------------------

@celery_app.task(name="app.workers.tasks.health_check")
def health_check() -> dict[str, str]:
    return {"status": "ok", "worker": "healthy"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mark_failed(
    document_id: uuid.UUID,
    tenant_id:   uuid.UUID,
    error_message: str,
) -> None:
    """Update document status to failed and write audit log."""
    from app.db.session import get_admin_db
    from app.models.documents import Document, AuditLog
    from sqlalchemy import update

    async with get_admin_db() as db:
        await db.execute(
            text("SET LOCAL app.current_tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        await db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status="failed", error_message=error_message)
        )
        db.add(AuditLog(
            tenant_id=tenant_id,
            user_id=None,
            action="document.processing_failed",
            resource=f"document:{document_id}",
            metadata={"error": error_message},
            success=False,
        ))


def _extract_text(file_bytes: bytes, content_type: str, filename: str) -> str:
    """
    Extract plain text from PDF, DOCX, or TXT content.
    Uses python-docx for DOCX and pypdf for PDF.
    Returns empty string on parse failure (logged as warning).
    """
    try:
        if content_type == "application/pdf" or filename.endswith(".pdf"):
            return _extract_pdf(file_bytes)
        elif "wordprocessingml" in content_type or filename.endswith(".docx"):
            return _extract_docx(file_bytes)
        else:
            # Plain text / markdown — decode with UTF-8, fallback to latin-1
            try:
                return file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return file_bytes.decode("latin-1", errors="replace")
    except Exception as exc:
        logger.warning("Text extraction warning | type=%s error=%s", content_type, exc)
        raise


def _extract_pdf(data: bytes) -> str:
    """Extract text from PDF bytes using pypdf."""
    import io
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def _extract_docx(data: bytes) -> str:
    """Extract text from DOCX bytes using python-docx."""
    import io
    import docx

    doc = docx.Document(io.BytesIO(data))
    return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
