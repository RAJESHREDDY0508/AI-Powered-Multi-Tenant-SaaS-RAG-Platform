"""
Celery Worker Tasks  —  High-Reliability Ingestion Pipeline
════════════════════════════════════════════════════════════

Task: process_document
───────────────────────
The single entry point for the async ingestion pipeline.
Triggered by IngestionService after a successful S3 upload.

Full pipeline (all in Celery worker — never in the API process):

  Step 1  │  Idempotency guard  — check doc status, skip if already processed
  Step 2  │  Mark status=processing  — visible to GET /status endpoint
  Step 3  │  Download PDF from S3  — raw bytes, never a temp file path
  Step 4  │  Text extraction  — PyMuPDF → Unstructured/Textract cascade
  Step 5  │  Semantic chunking  — NLP paragraph/sentence segmentation
  Step 6  │  Embedding pipeline  — batch OpenAI calls with retry
  Step 7  │  Vector upsert  — to tenant-scoped Pinecone/Weaviate namespace
  Step 8  │  Persist chunks  — saas.chunks rows in PostgreSQL
  Step 9  │  Mark status=ready  — document searchable by RAG queries
  Step 10 │  Audit log  — SOC2-compliant event record

On failure:
  Step F1 │  Mark status=failed with error message
  Step F2 │  Append failure audit log
  Step F3 │  Retry (max 3 times, exponential back-off)
  Step F4 │  Dead letter after max retries

Idempotency:
  • Chunk IDs are deterministic (sha256(tenant_id:doc_id:chunk_index))
  • Vector upserts are idempotent (overwrite-on-collision in both stores)
  • Re-processing a doc that's already 'ready' is a no-op (Step 1 guard)

Tenant isolation enforcement:
  • Vector store instance scoped to tenant_id (namespace/collection isolation)
  • DB session uses RLS SET LOCAL tenant context
  • S3 key validated against expected tenant prefix before download
  • Every audit log entry includes tenant_id

Observability (structured logging per step):
  • processing_time_ms  per step and total
  • chunk_count          after chunking
  • embedding_latency_ms after embedding
  • token_usage          from OpenAI response
  • strategy_used        pymupdf|unstructured|textract
  • used_ocr             boolean
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select, update as sa_update

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Import guard: SoftTimeLimitExceeded may not exist in all environments
try:
    from celery.exceptions import SoftTimeLimitExceeded
except ImportError:
    class SoftTimeLimitExceeded(Exception):  # type: ignore
        pass


# ---------------------------------------------------------------------------
# Event loop helper  (Celery workers are synchronous processes)
# ---------------------------------------------------------------------------

def _run_async(coro) -> Any:
    """
    Run an async coroutine from a synchronous Celery task.
    Creates a new event loop per task call and closes it on completion.
    This is the standard pattern for mixing async libraries (aioboto3, asyncpg)
    with Celery's sync worker model.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Main processing task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.workers.tasks.process_document",
    bind=True,                      # self = task instance (for self.retry())
    max_retries=3,
    acks_late=True,                 # ACK only after task completes → at-least-once delivery
    reject_on_worker_lost=True,     # re-queue if worker crashes mid-task
    soft_time_limit=270,            # SIGALRM at 4m30s → graceful shutdown hook
    time_limit=330,                 # SIGKILL at 5m30s → hard stop backstop
    default_retry_delay=30,
)
def process_document(
    self,
    *,
    document_id:  str,
    tenant_id:    str,
    s3_key:       str,
    content_type: str,
) -> dict:
    """
    Full async ingestion pipeline, dispatched from a synchronous Celery task.

    Args:
        document_id  : UUID string — the Document.id
        tenant_id    : UUID string — from JWT (never from client payload)
        s3_key       : Full S3 key: tenants/<tid>/documents/<doc_id>.ext
        content_type : Detected MIME type (application/pdf, etc.)
    """
    task_start  = time.monotonic()
    doc_uuid    = UUID(document_id)
    tenant_uuid = UUID(tenant_id)

    logger.info(
        "Pipeline start | task_id=%s doc=%s tenant=%s s3_key=%s",
        self.request.id, document_id, tenant_id, s3_key,
    )

    try:
        result = _run_async(
            _run_pipeline(
                task_id=self.request.id or "",
                doc_uuid=doc_uuid,
                tenant_uuid=tenant_uuid,
                s3_key=s3_key,
                content_type=content_type,
            )
        )
    except SoftTimeLimitExceeded:
        logger.error("Soft time limit exceeded | doc=%s", document_id)
        _mark_failed_sync(doc_uuid, tenant_uuid, "Task timed out (soft limit)")
        raise
    except Exception as exc:
        logger.exception("Pipeline error | doc=%s error=%s", document_id, exc)
        _mark_failed_sync(doc_uuid, tenant_uuid, str(exc))
        # Retry with exponential back-off (30s → 60s → 120s)
        retry_delay = min(30 * (2 ** self.request.retries), 300)
        raise self.retry(exc=exc, countdown=retry_delay)

    total_ms = (time.monotonic() - task_start) * 1000
    logger.info(
        "Pipeline complete | doc=%s tenant=%s chunks=%d vectors=%d "
        "total_ms=%.0f tokens=%d",
        document_id, tenant_id,
        result.get("chunk_count", 0),
        result.get("vector_count", 0),
        total_ms,
        result.get("total_tokens", 0),
    )
    return {**result, "total_pipeline_ms": round(total_ms)}


# ---------------------------------------------------------------------------
# Async pipeline core
# ---------------------------------------------------------------------------

async def _run_pipeline(
    task_id:     str,
    doc_uuid:    UUID,
    tenant_uuid: UUID,
    s3_key:      str,
    content_type: str,
) -> dict:
    """
    Async pipeline — all I/O-bound steps use await.
    DB, S3, and OpenAI all run concurrently where possible.
    """
    from app.db.session import AsyncSessionLocal, _set_tenant_context
    from app.models.documents import AuditLog, Chunk, Document
    from app.processing.extractor import TextExtractorOrchestrator
    from app.processing.chunking import SemanticChunker
    from app.processing.embeddings import run_embedding_pipeline
    from app.vectorstore.factory import get_vector_store
    from app.vectorstore.base import VectorRecord
    from app.core.config import settings

    # Timing buckets (all in milliseconds)
    timings: dict[str, float] = {}

    async with AsyncSessionLocal() as db:
        async with db.begin():
            await _set_tenant_context(db, tenant_uuid)

            # ── Step 1: Idempotency guard ─────────────────────────────────
            row = await db.execute(select(Document).where(Document.id == doc_uuid))
            doc = row.scalars().first()

            if doc is None:
                logger.error("Document not found: %s", doc_uuid)
                return {"error": "document_not_found"}

            if doc.status in ("ready", "processing"):
                # Idempotent: already processed or currently being processed
                logger.info("Skipping %s document: %s", doc.status, doc_uuid)
                return {"skipped": True, "status": doc.status}

            # ── Step 2: Mark status=processing ───────────────────────────
            await db.execute(
                sa_update(Document).where(Document.id == doc_uuid).values(status="processing")
            )
            await db.flush()
            logger.info("Status → processing | doc=%s", doc_uuid)

            # ── Step 3: Download from S3 ──────────────────────────────────
            t = time.monotonic()
            pdf_bytes = await _download_from_s3(
                s3_key=s3_key,
                bucket=settings.s3_bucket,
                tenant_id=tenant_uuid,
            )
            timings["s3_ms"] = (time.monotonic() - t) * 1000
            logger.info(
                "S3 download | doc=%s size_bytes=%d ms=%.0f",
                doc_uuid, len(pdf_bytes), timings["s3_ms"],
            )

            # ── Step 4: Text extraction (OCR cascade) ─────────────────────
            t = time.monotonic()
            extractor = TextExtractorOrchestrator(
                s3_bucket=settings.s3_bucket,
                s3_key=s3_key,
            )
            extraction = await extractor.extract(pdf_bytes)
            timings["ocr_ms"] = (time.monotonic() - t) * 1000

            logger.info(
                "Extraction | doc=%s strategy=%s used_ocr=%s pages=%d "
                "chars=%d avg_confidence=%.2f ms=%.0f",
                doc_uuid, extraction.strategy_used, extraction.used_ocr,
                extraction.page_count, extraction.total_chars,
                extraction.avg_confidence, timings["ocr_ms"],
            )

            if not extraction.full_text.strip():
                await _mark_failed(db, doc_uuid, tenant_uuid, "No text extracted")
                return {"error": "empty_extraction"}

            # ── Step 5: Semantic chunking ──────────────────────────────────
            t = time.monotonic()
            chunker = SemanticChunker()
            chunks  = chunker.chunk(
                text=extraction.full_text,
                tenant_id=tenant_uuid,
                document_id=str(doc_uuid),
                source_key=s3_key,
                page_map=extraction.page_map,
                extra_meta={
                    "content_type":  content_type,
                    "strategy_used": extraction.strategy_used,
                    "used_ocr":      extraction.used_ocr,
                },
            )
            timings["chunk_ms"] = (time.monotonic() - t) * 1000

            logger.info(
                "Chunking | doc=%s chunks=%d avg_chars=%.0f ms=%.0f",
                doc_uuid, len(chunks),
                sum(c.char_count for c in chunks) / max(1, len(chunks)),
                timings["chunk_ms"],
            )

            if not chunks:
                await _mark_failed(db, doc_uuid, tenant_uuid, "No chunks produced")
                return {"error": "empty_chunks"}

            # ── Step 6: Batch embedding with retry ────────────────────────
            t = time.monotonic()
            emb = await run_embedding_pipeline(chunks=chunks, tenant_id=tenant_uuid)
            timings["embed_ms"] = (time.monotonic() - t) * 1000

            logger.info(
                "Embedding | doc=%s vectors=%d failed_chunks=%d tokens=%d ms=%.0f",
                doc_uuid, len(emb.vector_records),
                len(emb.failed_chunks), emb.total_tokens, timings["embed_ms"],
            )

            if not emb.vector_records:
                await _mark_failed(db, doc_uuid, tenant_uuid, "All embedding batches failed")
                return {"error": "embedding_failed"}

            # ── Step 7: Vector upsert (tenant-isolated namespace) ─────────
            t = time.monotonic()
            vector_store = get_vector_store(tenant_id=tenant_uuid)

            vrecords = [
                VectorRecord(id=r["id"], vector=r["vector"], metadata=r["metadata"])
                for r in emb.vector_records
            ]
            upserted = await vector_store.upsert(records=vrecords, batch_size=100)
            timings["vec_ms"] = (time.monotonic() - t) * 1000

            logger.info(
                "Vector upsert | doc=%s namespace=%s count=%d ms=%.0f",
                doc_uuid, vector_store._namespace(), upserted, timings["vec_ms"],
            )

            # ── Step 8: Persist chunk rows ────────────────────────────────
            t = time.monotonic()
            failed_set = set(emb.failed_chunks)
            chunk_rows = [
                Chunk(
                    id=_chunk_uuid(c.chunk_id),
                    tenant_id=tenant_uuid,
                    document_id=doc_uuid,
                    chunk_index=c.chunk_index,
                    text=c.text,
                    token_count=c.token_est,
                    vector_id=c.chunk_id,
                    vector_store=settings.vector_store_backend,
                )
                for c in chunks
                if c.chunk_index not in failed_set
            ]
            db.add_all(chunk_rows)
            timings["db_ms"] = (time.monotonic() - t) * 1000
            logger.info(
                "Chunk rows | doc=%s inserted=%d ms=%.0f",
                doc_uuid, len(chunk_rows), timings["db_ms"],
            )

            # ── Step 9: Mark document ready ───────────────────────────────
            await db.execute(
                sa_update(Document)
                .where(Document.id == doc_uuid)
                .values(
                    status="ready",
                    chunk_count=len(chunk_rows),
                    vector_count=upserted,
                )
            )

            # ── Step 10: SOC2 audit log ───────────────────────────────────
            db.add(AuditLog(
                tenant_id=tenant_uuid,
                user_id=None,
                action="document.processed",
                resource=f"document:{doc_uuid}",
                doc_metadata={
                    "task_id":       task_id,
                    "chunk_count":   len(chunk_rows),
                    "vector_count":  upserted,
                    "total_tokens":  emb.total_tokens,
                    "strategy_used": extraction.strategy_used,
                    "used_ocr":      extraction.used_ocr,
                    "page_count":    extraction.page_count,
                    **{k: round(v) for k, v in timings.items()},
                },
                success=True,
            ))
            # db.begin() block commits here on clean exit

    return {
        "document_id":   str(doc_uuid),
        "tenant_id":     str(tenant_uuid),
        "chunk_count":   len(chunk_rows),
        "vector_count":  upserted,
        "total_tokens":  emb.total_tokens,
        "strategy_used": extraction.strategy_used,
        "used_ocr":      extraction.used_ocr,
        "page_count":    extraction.page_count,
        **{k: round(v) for k, v in timings.items()},
    }


# ---------------------------------------------------------------------------
# S3 download
# ---------------------------------------------------------------------------

async def _download_from_s3(s3_key: str, bucket: str, tenant_id: UUID) -> bytes:
    """
    Download document bytes from S3 with tenant prefix validation.
    The prefix check is defence-in-depth: the key was server-constructed
    at upload time, but we verify again here to prevent injection.
    """
    expected_prefix = f"tenants/{tenant_id}/"
    if not s3_key.startswith(expected_prefix):
        raise ValueError(
            f"S3 key '{s3_key}' does not match tenant prefix '{expected_prefix}'. "
            "Possible key tampering."
        )

    import aioboto3
    from app.core.config import settings

    async with aioboto3.Session().client("s3", region_name=settings.aws_region) as s3:
        resp = await s3.get_object(Bucket=bucket, Key=s3_key)
        return await resp["Body"].read()


# ---------------------------------------------------------------------------
# Failure helpers
# ---------------------------------------------------------------------------

async def _mark_failed(db, doc_uuid: UUID, tenant_uuid: UUID, reason: str) -> None:
    """Async: mark document failed + audit log."""
    from app.models.documents import AuditLog, Document

    await db.execute(
        sa_update(Document).where(Document.id == doc_uuid).values(status="failed")
    )
    db.add(AuditLog(
        tenant_id=tenant_uuid,
        user_id=None,
        action="document.processing_failed",
        resource=f"document:{doc_uuid}",
        doc_metadata={"reason": reason},
        success=False,
    ))
    logger.error("Document marked failed | doc=%s reason=%s", doc_uuid, reason)


def _mark_failed_sync(doc_uuid: UUID, tenant_uuid: UUID, reason: str) -> None:
    """
    Sync: mark document failed from the outermost exception handler.
    Uses a raw psycopg2 connection to avoid any async dependency.
    Called when the async pipeline itself raises before yielding.
    """
    try:
        from sqlalchemy import create_engine, text
        from app.core.config import settings

        sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
        engine   = create_engine(sync_url, pool_pre_ping=True, pool_size=1)
        with engine.connect() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant_id = :tid"),
                {"tid": str(tenant_uuid)},
            )
            conn.execute(
                text("UPDATE saas.documents SET status='failed' WHERE id=:doc_id"),
                {"doc_id": str(doc_uuid)},
            )
            conn.commit()
        logger.info("Sync fail-mark applied | doc=%s", doc_uuid)
    except Exception as exc:
        logger.error("Could not sync-mark document as failed: %s", exc)


# ---------------------------------------------------------------------------
# Beat task: retry scanner
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.workers.tasks.retry_failed_documents",
    bind=False,
    max_retries=0,      # scanner itself must not retry — it's a loop
    acks_late=False,    # scanner is idempotent; early ACK OK
)
def retry_failed_documents() -> dict:
    """
    Celery Beat task — runs every 60 seconds (configured in celery_app.py).

    Scans for documents stuck in 'pending' for > 5 minutes and re-queues them.
    This recovers from:
      • Broker failures during the original upload
      • Worker crashes that left docs in 'pending'
      • RabbitMQ message expiry (x-message-ttl)

    Uses a direct SQL query without RLS (crosses tenants intentionally).
    Each re-queued task runs with the document's own tenant context via SET LOCAL.
    """
    from sqlalchemy import create_engine, text
    from app.core.config import settings

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine   = create_engine(sync_url, pool_pre_ping=True, pool_size=1)
    requeued = 0

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, tenant_id, s3_key, content_type
                FROM   saas.documents
                WHERE  status     = 'pending'
                  AND  created_at < NOW() - INTERVAL '5 minutes'
                ORDER  BY created_at
                LIMIT  50
            """)
        ).fetchall()

        for row in rows:
            try:
                process_document.apply_async(
                    kwargs={
                        "document_id":  str(row.id),
                        "tenant_id":    str(row.tenant_id),
                        "s3_key":       row.s3_key,
                        "content_type": row.content_type,
                    },
                    queue="documents.retry",
                    countdown=5,
                )
                requeued += 1
                logger.info("Re-queued | doc=%s tenant=%s", row.id, row.tenant_id)
            except Exception as exc:
                logger.error("Re-queue failed | doc=%s error=%s", row.id, exc)

    logger.info("Retry scanner | requeued=%d", requeued)
    return {"requeued": requeued}


# ---------------------------------------------------------------------------
# Worker liveness check
# ---------------------------------------------------------------------------

@celery_app.task(name="app.workers.tasks.health_check", queue="system.health")
def health_check() -> dict:
    """Lightweight task — verifies worker is alive and processing."""
    return {"status": "ok", "worker": "celery"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk_uuid(chunk_id_hex: str) -> UUID:
    """
    Convert a 32-char hex chunk_id (sha256 prefix) to a UUID.
    The hex string is guaranteed to be 32 chars by _make_chunk_id() in chunking.py.
    """
    try:
        return UUID(hex=chunk_id_hex.ljust(32, "0")[:32])
    except ValueError:
        return uuid.uuid4()
