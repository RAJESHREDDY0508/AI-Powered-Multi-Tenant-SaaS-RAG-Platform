"""
Document Ingestion Service  —  Production Rewrite
══════════════════════════════════════════════════

Full 9-step pipeline with TRUE streaming:
  No `await file.read()` — the file is never loaded entirely into memory.
  Instead, the UploadFile is forwarded chunk-by-chunk to S3 via Multipart Upload.

Pipeline
─────────
  Step 1 │ Validate document_name
  Step 2 │ Reject by Content-Length header BEFORE reading a single byte
  Step 3 │ Read ONLY the first 8 bytes for magic-byte MIME detection
  Step 4 │ Validate MIME type + extension (allowlist)
  Step 5 │ Stream file to S3 via multipart (5 MB parts, MD5 computed en-route)
           │   → abort_multipart_upload called on any error (no orphaned S3 parts)
  Step 6 │ Duplicate check (tenant-scoped MD5 UNIQUE constraint in PostgreSQL)
           │   → 409 if match found; aborts and deletes just-uploaded S3 object
  Step 7 │ DB INSERT into saas.documents (status=pending, RLS-enforced)
           │   → IntegrityError on race condition → treated as duplicate
  Step 8 │ Append SOC2 audit log entry (INSERT-only table)
  Step 9 │ Publish Celery task (non-fatal if broker down; retry scanner recovers)

Why streaming?
  50 MB files × N concurrent uploads = N×50 MB memory pressure with full reads.
  With multipart streaming, peak memory per upload = 1× CHUNK_SIZE (5 MB).

Security invariants
───────────────────
  • tenant_id  always from verified JWT  — never from request body
  • S3 key     server-constructed        — never from client input
  • MIME type  detected from magic bytes — never from client Content-Type header
  • Checksum   computed from actual bytes — not trusted from client
  • DB UNIQUE constraint is the FINAL guard against race-condition duplicates

SOC2 audit events emitted
─────────────────────────
  document.upload_attempt      — always, before any action
  document.uploaded            — on full success
  document.duplicate_rejected  — on 409
  document.upload_failed       — on S3 or DB error
  document.queue_failed        — broker down (non-fatal, document still stored)
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from typing import Callable, Awaitable

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.token import TokenPayload
from app.models.documents import AuditLog, Document
from app.schemas.documents import (
    ALLOWED_CONTENT_TYPES,
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    DocumentUploadResponse,
    ProcessingStatus,
    UploadErrors,
)
from app.storage.multipart import CHUNK_SIZE, streaming_multipart_upload
from app.storage.s3 import ResourceType, S3StorageService, TenantStorageConfig
from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File type helpers
# ---------------------------------------------------------------------------

# Magic byte → MIME type map (checked against first 8 bytes only)
_MAGIC_MAP: dict[bytes, str] = {
    b"%PDF":                            "application/pdf",
    b"PK\x03\x04":                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1": "application/msword",   # legacy .doc (OLE2 compound)
}

# Regex: valid document_name characters
_SAFE_NAME_RE = re.compile(r'^[^/\\<>:"|?*\x00-\x1f]{1,255}$')


def _detect_mime(filename: str, head_8: bytes) -> str:
    """
    Detect MIME type from the first 8 bytes of the file (magic bytes).
    Falls back to extension if no magic signature matches.
    NEVER uses the client-supplied Content-Type header.
    """
    for magic, mime in _MAGIC_MAP.items():
        if head_8.startswith(magic):
            return mime

    ext = _file_ext(filename)
    if ext in (".txt", ".md"):
        return "text/plain"

    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _file_ext(filename: str) -> str:
    """Return lowercased extension with dot, e.g. '.pdf'."""
    parts = filename.rsplit(".", 1)
    return f".{parts[-1].lower()}" if len(parts) == 2 else ""


def _sanitize_filename(raw: str) -> str:
    """
    Strip directory traversal components and replace S3-unsafe characters.
    Returns only the basename, max 200 characters.
    """
    basename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    safe = re.sub(r"[^a-zA-Z0-9._\-]", "_", basename)
    return safe[:200] or "upload"


def _parse_user_id(sub: str) -> uuid.UUID | None:
    """Safely convert JWT `sub` claim to UUID. Returns None if not UUID-shaped."""
    try:
        return uuid.UUID(sub)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------

async def _audit(
    db: AsyncSession,
    *,
    tenant_id:  uuid.UUID,
    user_id:    uuid.UUID | None,
    action:     str,
    resource:   str | None,
    metadata:   dict,
    ip_address: str | None,
    success:    bool,
) -> None:
    """
    Append an immutable audit record.
    The app_user PG role has INSERT-only on saas.audit_logs — no UPDATE/DELETE.
    Never flushes independently; the outer session transaction commits it atomically.
    """
    db.add(AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource=resource,
        doc_metadata=metadata,
        ip_address=ip_address,
        success=success,
    ))


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

async def _find_duplicate(
    db:           AsyncSession,
    md5_checksum: str,
) -> Document | None:
    """
    Find an existing non-deleted document with the same checksum.
    RLS on saas.documents guarantees this query NEVER crosses tenant boundaries;
    no manual tenant_id filter is needed here.
    """
    result = await db.execute(
        select(Document).where(
            Document.md5_checksum == md5_checksum,
            Document.status != "deleted",
        )
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Core ingestion service
# ---------------------------------------------------------------------------

class IngestionService:
    """
    Stateless, dependency-injected orchestrator.
    One instance per request. All side-effects are injected — fully testable.

    Constructor args
    ────────────────
      db             AsyncSession  — tenant-scoped (RLS active via SET LOCAL)
      storage        S3StorageService — pre-configured for this tenant's KMS key
      user           TokenPayload  — verified JWT payload (tenant_id, sub, role)
      task_publisher TaskPublisher — dispatches async Celery task
      progress_cb    Optional async callable(bytes_received, bytes_total) for SSE
    """

    def __init__(
        self,
        db:             AsyncSession,
        storage:        S3StorageService,
        user:           TokenPayload,
        task_publisher: "TaskPublisher",
        progress_cb:    Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        self._db        = db
        self._storage   = storage
        self._user      = user
        self._publisher = task_publisher
        self._progress  = progress_cb

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def ingest(
        self,
        file:           UploadFile,
        document_name:  str,
        permissions:    dict | None,
        client_ip:      str | None,
        content_length: int | None = None,   # from request Content-Length header
    ) -> DocumentUploadResponse:
        """
        Execute the full 9-step ingestion pipeline.

        Returns:  DocumentUploadResponse (HTTP 202 payload)
        Raises:   HTTPException with structured ErrorResponse detail on all errors
        """
        tenant_id = self._user.tenant_id
        user_id   = _parse_user_id(self._user.sub)

        # ── Step 1: Validate document_name ────────────────────────────────
        document_name = document_name.strip()
        if not document_name or len(document_name) > 255:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.invalid_document_name(document_name).model_dump(),
            )

        if not _SAFE_NAME_RE.match(document_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.invalid_document_name(document_name).model_dump(),
            )

        # ── Step 2: Fast-reject oversized uploads before reading ──────────
        # Content-Length is advisory, but we use it to fail fast on obvious overages.
        if content_length and content_length > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=UploadErrors.file_too_large(content_length).model_dump(),
            )

        # Guard: file field must be present
        if not file or not file.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.missing_file().model_dump(),
            )

        raw_filename = file.filename or "upload"
        safe_filename = _sanitize_filename(raw_filename)
        ext = _file_ext(raw_filename)

        # ── Step 3: Read ONLY first 8 bytes for magic-byte MIME detection ─
        #   We read 8 bytes, detect the type, then the stream continues normally
        #   inside streaming_multipart_upload via _iter_chunks.
        #   IMPORTANT: We must seek back to position 0 after peeking, so the
        #   full file is available for the multipart upload.
        file_head: bytes = await asyncio.get_event_loop().run_in_executor(
            None, file.file.read, 8
        )
        if len(file_head) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.missing_file().model_dump(),
            )
        # Seek back to start so the multipart uploader reads the complete file
        await asyncio.get_event_loop().run_in_executor(None, file.file.seek, 0)

        # ── Step 4: Validate MIME type + extension ────────────────────────
        detected_mime = _detect_mime(raw_filename, file_head)

        if detected_mime not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.unsupported_file_type(
                    raw_filename, detected_mime
                ).model_dump(),
            )

        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.unsupported_file_type(
                    raw_filename, f"extension: {ext}"
                ).model_dump(),
            )

        # Generate document_id before upload (used as S3 filename for determinism)
        document_id = uuid.uuid4()
        s3_filename = f"{document_id}{ext}"   # e.g. <uuid>.pdf

        # Emit pre-upload audit entry
        await _audit(
            self._db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="document.upload_attempt",
            resource=None,
            metadata={
                "filename":      safe_filename,
                "document_name": document_name,
                "content_type":  detected_mime,
                "size_hint":     content_length,
            },
            ip_address=client_ip,
            success=True,
        )

        logger.info(
            "Ingest start | tenant=%s user=%s file=%s doc_id=%s",
            tenant_id, user_id, safe_filename, document_id,
        )

        # ── Step 5: Stream upload to S3 (multipart, 5 MB parts) ──────────
        #   MD5 checksum is computed en-route (streaming_multipart_upload
        #   maintains a running hashlib.md5 across all chunks).
        s3_key = self._storage._cfg.prefix(ResourceType.DOCUMENT, s3_filename)

        try:
            upload_result = await streaming_multipart_upload(
                upload=file,
                bucket=self._storage._cfg.bucket,
                s3_key=s3_key,
                content_type=detected_mime,
                kms_key_arn=self._storage._cfg.kms_key_arn,
                size_hint=content_length,
                progress_cb=self._progress,
            )
        except HTTPException:
            await _audit(
                self._db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="document.upload_failed",
                resource=f"document:{document_id}",
                metadata={"stage": "s3_streaming", "content_type": detected_mime},
                ip_address=client_ip,
                success=False,
            )
            raise
        except Exception as exc:
            logger.exception(
                "S3 streaming upload failed | tenant=%s doc=%s", tenant_id, document_id
            )
            await _audit(
                self._db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="document.upload_failed",
                resource=f"document:{document_id}",
                metadata={"stage": "s3_streaming", "error": str(exc)},
                ip_address=client_ip,
                success=False,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=UploadErrors.storage_error(str(exc)).model_dump(),
            )

        md5 = upload_result.md5_checksum
        size_bytes = upload_result.size_bytes

        logger.info(
            "S3 upload complete | doc=%s key=%s size=%d md5=%s parts=%d",
            document_id, s3_key, size_bytes, md5, upload_result.part_count,
        )

        # ── Step 6: Duplicate check (post-upload) ─────────────────────────
        #   We check AFTER upload because we need the checksum, which is only
        #   known after reading the full file. The UNIQUE DB constraint is the
        #   authoritative guard — this SELECT is an early-exit optimization.
        #   If a duplicate is found, we soft-delete the just-uploaded S3 object.
        existing = await _find_duplicate(self._db, md5)
        if existing:
            # Soft-delete the S3 object we just uploaded (no orphans)
            try:
                await self._storage.delete_object(
                    ResourceType.DOCUMENT, s3_filename, hard=False
                )
            except Exception:
                pass  # best-effort; S3 lifecycle rules handle cleanup

            await _audit(
                self._db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="document.duplicate_rejected",
                resource=f"document:{existing.id}",
                metadata={
                    "md5_checksum":         md5,
                    "existing_document_id": str(existing.id),
                    "s3_key_discarded":     s3_key,
                },
                ip_address=client_ip,
                success=False,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=UploadErrors.duplicate_document(md5, existing.id).model_dump(),
            )

        # ── Step 7: Insert document record into saas.documents ────────────
        metadata_payload: dict = {}
        if permissions:
            metadata_payload["document_permissions"] = permissions

        doc = Document(
            id=document_id,
            tenant_id=tenant_id,
            uploaded_by=user_id,
            s3_key=s3_key,
            filename=safe_filename,
            document_name=document_name,
            content_type=detected_mime,
            size_bytes=size_bytes,
            md5_checksum=md5,
            status="pending",
            doc_metadata=metadata_payload,
        )

        try:
            self._db.add(doc)
            await self._db.flush()   # assigns server-side defaults, surfaces UNIQUE violation
        except IntegrityError:
            # Race condition: two concurrent uploads of the same file.
            # The DB UNIQUE constraint is the final arbiter — treat as duplicate.
            await self._db.rollback()
            # Clean up the S3 object
            try:
                await self._storage.delete_object(
                    ResourceType.DOCUMENT, s3_filename, hard=True
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=UploadErrors.duplicate_document(md5, document_id).model_dump(),
            )

        # ── Step 8: Append SOC2 audit log (success) ───────────────────────
        await _audit(
            self._db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="document.uploaded",
            resource=f"document:{document_id}",
            metadata={
                "document_id":   str(document_id),
                "document_name": document_name,
                "filename":      safe_filename,
                "s3_key":        s3_key,
                "size_bytes":    size_bytes,
                "md5_checksum":  md5,
                "content_type":  detected_mime,
                "part_count":    upload_result.part_count,
                "s3_etag":       upload_result.etag,
            },
            ip_address=client_ip,
            success=True,
        )

        # ── Step 9: Publish async processing task to Celery ───────────────
        try:
            await self._publisher.publish_ingestion_task(
                document_id=document_id,
                tenant_id=tenant_id,
                s3_key=s3_key,
                content_type=detected_mime,
            )
        except Exception as exc:
            # NON-FATAL: document is durably stored in S3 + DB.
            # The Beat retry scanner re-queues status=pending docs every 60 s.
            logger.error(
                "Broker publish failed (non-fatal) | doc=%s error=%s", document_id, exc
            )
            await _audit(
                self._db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="document.queue_failed",
                resource=f"document:{document_id}",
                metadata={"error": str(exc), "recovery": "beat-retry-scanner"},
                ip_address=client_ip,
                success=False,
            )

        return DocumentUploadResponse(
            document_id=document_id,
            status="uploaded",
            checksum=md5,
            processing_status=ProcessingStatus.QUEUED,
            s3_key=s3_key,
            tenant_id=tenant_id,
            document_name=document_name,
            size_bytes=size_bytes,
            content_type=detected_mime,
            created_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Task publisher
# ---------------------------------------------------------------------------

class TaskPublisher:
    """
    Thin wrapper over Celery's apply_async.

    Import of the Celery task is deferred to publish time so that:
      - Module load does NOT attempt a broker connection.
      - Tests can mock this class without touching Celery.

    The Celery call is dispatched in a thread executor to avoid blocking
    the asyncio event loop (kombu uses blocking socket I/O).
    """

    async def publish_ingestion_task(
        self,
        document_id:  uuid.UUID,
        tenant_id:    uuid.UUID,
        s3_key:       str,
        content_type: str,
    ) -> None:
        from app.workers.tasks import process_document

        payload = {
            "document_id":  str(document_id),
            "tenant_id":    str(tenant_id),
            "s3_key":       s3_key,
            "content_type": content_type,
        }

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: process_document.apply_async(
                kwargs=payload,
                countdown=2,          # 2-second delay lets the DB transaction commit
                retry=True,
                retry_policy={
                    "max_retries":  3,
                    "interval_start": 5,
                    "interval_step":  10,
                    "interval_max":   60,
                },
            ),
        )
        logger.info("Task published | doc=%s tenant=%s", document_id, tenant_id)
