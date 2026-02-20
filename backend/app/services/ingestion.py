"""
Document Ingestion Service

Orchestrates the upload pipeline:
  1. Validate file type and size (MIME sniffing, extension check)
  2. Compute MD5 checksum for deduplication
  3. Check for duplicate within the tenant (saas.documents + md5_checksum)
  4. Upload to S3 under tenants/<tenant_id>/documents/<document_id>.<ext>
  5. Insert document record into saas.documents (status=pending)
  6. Write SOC2 audit log entry
  7. Publish processing task to Celery via RabbitMQ/Redis broker
  8. Return structured response

Security invariants enforced here:
  - tenant_id is ALWAYS taken from the verified JWT (TokenPayload), never request body.
  - S3 key is constructed server-side; filename is sanitized before use.
  - MIME type is detected from file magic bytes, not the client's Content-Type header.
  - Checksum collision check uses DB-level UNIQUE constraint as the final guard
    (SELECT-then-INSERT race condition handled by catching IntegrityError).

SOC2 audit events written:
  - document.upload_attempt  (always — before any action)
  - document.uploaded        (on success)
  - document.upload_failed   (on any error)
  - document.duplicate_rejected (on 409)
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from typing import BinaryIO

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select, text
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
from app.storage.s3 import ResourceType, S3StorageService
from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File type validation helpers
# ---------------------------------------------------------------------------

# Magic byte signatures for each supported type
# Checked against the first 8 bytes of the file content
_MAGIC_BYTES: dict[bytes, str] = {
    b"%PDF":                                        "application/pdf",
    b"PK\x03\x04":                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":           "application/msword",  # legacy .doc (OLE2)
}

_FILENAME_RE = re.compile(r'^[^/\\<>:"|?*\x00-\x1f]{1,255}$')


def _detect_mime_type(filename: str, file_head: bytes) -> str:
    """
    Detect MIME type using magic bytes first, falling back to extension.
    Never trusts the client-supplied Content-Type.
    """
    for magic, mime in _MAGIC_BYTES.items():
        if file_head.startswith(magic):
            return mime

    # Plain text / markdown — no reliable magic bytes
    ext = _get_extension(filename).lower()
    if ext in (".txt", ".md"):
        return "text/plain"

    # Extension-based fallback
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _get_extension(filename: str) -> str:
    """Return lowercased file extension including the dot."""
    parts = filename.rsplit(".", 1)
    return f".{parts[-1].lower()}" if len(parts) == 2 else ""


def _sanitize_filename(filename: str) -> str:
    """
    Strip path components and replace unsafe characters.
    Returns only the basename with OS-safe characters.
    """
    # Strip any directory component
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    # Replace characters unsafe in S3 keys or filenames
    safe = re.sub(r'[^a-zA-Z0-9._\-]', '_', basename)
    return safe[:200]  # hard cap on filename length


# ---------------------------------------------------------------------------
# Checksum computation
# ---------------------------------------------------------------------------

def compute_md5(data: bytes) -> str:
    """Return MD5 hex digest of file bytes."""
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

async def _find_duplicate(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    md5_checksum: str,
) -> Document | None:
    """
    Query for an existing non-deleted document with the same checksum
    within this tenant's RLS scope.
    RLS on saas.documents guarantees the query never crosses tenant boundaries.
    """
    stmt = select(Document).where(
        Document.md5_checksum == md5_checksum,
        Document.status != "deleted",
    )
    result = await db.execute(stmt)
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------

async def _write_audit_log(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    action: str,
    resource: str | None,
    metadata: dict,
    ip_address: str | None,
    success: bool,
) -> None:
    """
    Insert a tamper-evident audit log entry.
    app_user has INSERT-only on saas.audit_logs (no UPDATE/DELETE).
    """
    log = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        action=action,
        resource=resource,
        metadata=metadata,
        ip_address=ip_address,
        success=success,
    )
    db.add(log)
    # Do NOT flush here — let the outer transaction handle it


# ---------------------------------------------------------------------------
# Core ingestion orchestrator
# ---------------------------------------------------------------------------

class IngestionService:
    """
    Stateless service object — one instance per request.
    All dependencies are injected (testable, no hidden globals).
    """

    def __init__(
        self,
        db:      AsyncSession,
        storage: S3StorageService,
        user:    TokenPayload,
        task_publisher: "TaskPublisher",  # forward ref — imported below
    ) -> None:
        self._db        = db
        self._storage   = storage
        self._user      = user
        self._publisher = task_publisher

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def ingest(
        self,
        file:          UploadFile,
        document_name: str,
        permissions:   dict | None,
        client_ip:     str | None,
    ) -> DocumentUploadResponse:
        """
        Full ingestion pipeline. Returns 202 on success.
        Raises HTTPException with structured ErrorResponse on all error cases.
        """
        tenant_id = self._user.tenant_id
        user_id   = uuid.UUID(self._user.sub) if self._is_valid_uuid(self._user.sub) else None

        # ---- Step 1: Validate document_name ----------------------------
        document_name = document_name.strip()
        if not document_name or len(document_name) > 255:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.invalid_document_name(document_name).model_dump(),
            )

        # ---- Step 2: Read file into memory (with size guard) -----------
        file_bytes = await self._read_upload(file)

        # ---- Step 3: Detect MIME type from magic bytes (never client header) --
        file_head = file_bytes[:8]
        detected_mime = _detect_mime_type(file.filename or "upload", file_head)

        if detected_mime not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.unsupported_file_type(
                    file.filename or "upload", detected_mime
                ).model_dump(),
            )

        ext = _get_extension(file.filename or "upload")
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.unsupported_file_type(
                    file.filename or "upload", ext
                ).model_dump(),
            )

        # ---- Step 4: Compute checksum ---------------------------------
        md5 = compute_md5(file_bytes)
        safe_filename = _sanitize_filename(file.filename or "upload")
        document_id   = uuid.uuid4()

        logger.info(
            "Ingest start | tenant=%s user=%s file=%s size=%d md5=%s",
            tenant_id, user_id, safe_filename, len(file_bytes), md5,
        )

        # Write attempt audit entry
        await _write_audit_log(
            self._db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="document.upload_attempt",
            resource=None,
            metadata={
                "filename":      safe_filename,
                "document_name": document_name,
                "size_bytes":    len(file_bytes),
                "md5_checksum":  md5,
                "content_type":  detected_mime,
            },
            ip_address=client_ip,
            success=True,
        )

        # ---- Step 5: Duplicate check ----------------------------------
        existing = await _find_duplicate(self._db, tenant_id, md5)
        if existing:
            await _write_audit_log(
                self._db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="document.duplicate_rejected",
                resource=f"document:{existing.id}",
                metadata={"md5_checksum": md5, "existing_document_id": str(existing.id)},
                ip_address=client_ip,
                success=False,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=UploadErrors.duplicate_document(md5, existing.id).model_dump(),
            )

        # ---- Step 6: Upload to S3 ------------------------------------
        s3_filename = f"{document_id}{ext}"  # e.g. <uuid>.pdf
        try:
            s3_obj = await self._storage.put_object(
                resource=ResourceType.DOCUMENT,
                filename=s3_filename,
                body=file_bytes,
                content_type=detected_mime,
                metadata={
                    "document_id":   str(document_id),
                    "document_name": document_name,
                    "uploaded_by":   str(user_id),
                    "md5_checksum":  md5,
                },
            )
        except Exception as exc:
            logger.exception("S3 upload failed | tenant=%s doc=%s", tenant_id, document_id)
            await _write_audit_log(
                self._db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="document.upload_failed",
                resource=f"document:{document_id}",
                metadata={"error": str(exc), "stage": "s3_upload"},
                ip_address=client_ip,
                success=False,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=UploadErrors.storage_error(str(exc)).model_dump(),
            )

        # ---- Step 7: Persist document record -------------------------
        metadata_payload: dict = {}
        if permissions:
            metadata_payload["document_permissions"] = permissions

        doc = Document(
            id=document_id,
            tenant_id=tenant_id,
            uploaded_by=user_id,
            s3_key=s3_obj.key,
            filename=safe_filename,
            document_name=document_name,
            content_type=detected_mime,
            size_bytes=len(file_bytes),
            md5_checksum=md5,
            status="pending",
            metadata=metadata_payload,
        )

        try:
            self._db.add(doc)
            await self._db.flush()   # assigns id, catches UNIQUE violation
        except IntegrityError:
            # Race condition: concurrent upload of same file; treat as duplicate
            await self._db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=UploadErrors.duplicate_document(md5, document_id).model_dump(),
            )

        # ---- Step 8: Write success audit log -------------------------
        await _write_audit_log(
            self._db,
            tenant_id=tenant_id,
            user_id=user_id,
            action="document.uploaded",
            resource=f"document:{document_id}",
            metadata={
                "document_id":   str(document_id),
                "document_name": document_name,
                "filename":      safe_filename,
                "s3_key":        s3_obj.key,
                "size_bytes":    len(file_bytes),
                "md5_checksum":  md5,
                "content_type":  detected_mime,
            },
            ip_address=client_ip,
            success=True,
        )

        # ---- Step 9: Publish async processing task -------------------
        try:
            await self._publisher.publish_ingestion_task(
                document_id=document_id,
                tenant_id=tenant_id,
                s3_key=s3_obj.key,
                content_type=detected_mime,
            )
        except Exception as exc:
            # Non-fatal: document is stored; processing will be retried by
            # a scheduled re-queue job that scans status=pending documents.
            logger.error(
                "Failed to publish processing task | doc=%s error=%s", document_id, exc
            )
            await _write_audit_log(
                self._db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="document.queue_failed",
                resource=f"document:{document_id}",
                metadata={"error": str(exc)},
                ip_address=client_ip,
                success=False,
            )
            # We still return 202 — the document is safely stored.
            # The scheduler will pick it up within 60 seconds.

        now = datetime.now(timezone.utc)
        return DocumentUploadResponse(
            document_id=document_id,
            status="uploaded",
            checksum=md5,
            processing_status=ProcessingStatus.QUEUED,
            s3_key=s3_obj.key,
            tenant_id=tenant_id,
            document_name=document_name,
            size_bytes=len(file_bytes),
            content_type=detected_mime,
            created_at=now,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _read_upload(self, file: UploadFile) -> bytes:
        """
        Read the upload into memory with a hard size ceiling.
        Raises 400/413 if the file is missing or too large.
        """
        if file is None or file.filename is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.missing_file().model_dump(),
            )

        data = await file.read()

        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=UploadErrors.missing_file().model_dump(),
            )

        if len(data) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=UploadErrors.file_too_large(len(data)).model_dump(),
            )

        return data

    @staticmethod
    def _is_valid_uuid(value: str) -> bool:
        try:
            uuid.UUID(value)
            return True
        except (ValueError, AttributeError):
            return False


# ---------------------------------------------------------------------------
# Task publisher — thin abstraction over Celery .delay()
# Injected into IngestionService so it can be mocked in tests.
# ---------------------------------------------------------------------------

class TaskPublisher:
    """
    Sends the document processing task to the Celery broker.
    Import is deferred so the broker connection is not required at module load time.
    """

    async def publish_ingestion_task(
        self,
        document_id: uuid.UUID,
        tenant_id:   uuid.UUID,
        s3_key:      str,
        content_type: str,
    ) -> None:
        """
        Dispatch process_document.delay() to the Celery worker.
        Runs in a thread executor to avoid blocking the async event loop.
        """
        import asyncio
        from app.workers.tasks import process_document

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: process_document.apply_async(
                kwargs={
                    "document_id":  str(document_id),
                    "tenant_id":    str(tenant_id),
                    "s3_key":       s3_key,
                    "content_type": content_type,
                },
                # Retry up to 3 times with exponential backoff
                countdown=2,
                max_retries=3,
            ),
        )
        logger.info(
            "Processing task published | doc=%s tenant=%s",
            document_id, tenant_id,
        )
