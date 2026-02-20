"""
Document Ingestion API Router
POST /api/v1/documents/upload

Implements:
  - Multipart file upload with strict validation
  - Multi-tenant isolation via JWT-extracted tenant_id
  - RBAC enforcement (minimum role: member)
  - Server-Sent Events (SSE) progress stream for upload phase
  - Async processing dispatch to Celery
  - Structured error responses for all 4xx/5xx cases
  - SOC2 audit logging (delegated to IngestionService)

Request lifecycle:
  ┌─────────────────────────────────────────────────────────┐
  │ 1. JWT verification → extract tenant_id + role (no      │
  │    client-supplied tenant)                               │
  │ 2. RBAC gate (member or above)                          │
  │ 3. File type validation (magic bytes + extension)        │
  │ 4. MD5 checksum + duplicate check (409 on collision)    │
  │ 5. S3 upload under tenants/<tenant_id>/documents/       │
  │ 6. DB insert (status=pending, RLS-enforced)              │
  │ 7. Celery task published → returns 202                  │
  └─────────────────────────────────────────────────────────┘

SSE stream (GET /upload-progress/{upload_token}):
  Emits JSON events: { stage, bytes_received, bytes_total, percent }
  Client EventSource connects before calling POST, receives real-time
  progress updates during the upload phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentUser, TenantDB, TenantStorage
from app.auth.rbac import require_role
from app.auth.token import TokenPayload
from app.models.documents import Document
from app.schemas.documents import (
    MAX_FILE_SIZE_BYTES,
    DocumentStatusResponse,
    DocumentUploadResponse,
    ErrorResponse,
    ProcessingStatus,
    UploadErrors,
    UploadProgressEvent,
)
from app.services.ingestion import IngestionService, TaskPublisher
from app.storage.s3 import S3StorageService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/documents",
    tags=["Document Ingestion"],
)


# ---------------------------------------------------------------------------
# In-memory SSE progress store
# Key: upload_token (str UUID), Value: asyncio.Queue of progress dicts
# In production, replace with Redis pub/sub for multi-instance deployments.
# ---------------------------------------------------------------------------

_PROGRESS_QUEUES: dict[str, asyncio.Queue] = {}
_UPLOAD_TOKEN_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# POST /documents/upload
# ---------------------------------------------------------------------------

@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for ingestion",
    description=(
        "Accepts PDF, DOCX, or TXT files up to 50 MB. "
        "Returns 202 immediately; processing is asynchronous. "
        "Poll GET /documents/{id}/status for pipeline progress."
    ),
    responses={
        202: {"model": DocumentUploadResponse, "description": "File accepted for processing"},
        400: {"model": ErrorResponse, "description": "Invalid file type, size, or request format"},
        401: {"model": ErrorResponse, "description": "Missing or invalid JWT"},
        403: {"model": ErrorResponse, "description": "Insufficient role (requires member+)"},
        409: {"model": ErrorResponse, "description": "Duplicate document (same checksum exists in tenant)"},
        413: {"model": ErrorResponse, "description": "File exceeds 50 MB limit"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
        503: {"model": ErrorResponse, "description": "Message broker unavailable"},
    },
)
async def upload_document(
    request:    Request,
    file:       UploadFile     = File(..., description="Document file (PDF, DOCX, TXT — max 50 MB)"),
    document_name: str         = Form(..., min_length=1, max_length=255, description="Display name for the document"),
    document_permissions: Optional[str] = Form(
        None,
        description="Optional JSON string: access permissions metadata",
    ),
    # --- Injected dependencies (never from request body) ---
    user:    TokenPayload  = Depends(require_role("member")),
    db:      AsyncSession  = Depends(lambda u=Depends(require_role("member")): _get_db_from_user(u)),
    storage: S3StorageService = Depends(lambda u=Depends(require_role("member")): _get_storage_from_user(u)),
) -> JSONResponse:
    """
    Upload endpoint with full ingestion pipeline.

    Security properties enforced here:
      - tenant_id is extracted from the verified JWT only (user.tenant_id)
      - File type detected from magic bytes, NOT client Content-Type
      - document_permissions are stored as opaque metadata; never evaluated server-side
      - client_ip extracted from X-Forwarded-For for audit log (sanitized)
    """
    request_id = str(uuid.uuid4())

    # Sanitize and parse optional permissions
    permissions: dict | None = None
    if document_permissions:
        try:
            permissions = json.loads(document_permissions)
            if not isinstance(permissions, dict):
                raise ValueError("document_permissions must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=ErrorResponse(
                    error_code="INVALID_PERMISSIONS_FORMAT",
                    message="document_permissions must be a valid JSON object string.",
                    details=[],
                    request_id=request_id,
                ).model_dump(mode="json"),
            )

    # Extract client IP for audit log (handle reverse proxy headers)
    client_ip: str | None = _extract_client_ip(request)

    # Guard: reject oversized requests before reading body
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_FILE_SIZE_BYTES + 4096:  # +4KB for form overhead
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content=UploadErrors.file_too_large(int(content_length)).model_dump(mode="json"),
        )

    # Run ingestion pipeline
    service = IngestionService(
        db=db,
        storage=storage,
        user=user,
        task_publisher=TaskPublisher(),
    )

    try:
        result = await service.ingest(
            file=file,
            document_name=document_name,
            permissions=permissions,
            client_ip=client_ip,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Unhandled ingestion error | tenant=%s request_id=%s",
            user.tenant_id, request_id,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=UploadErrors.internal_error(request_id).model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=result.model_dump(mode="json"),
        headers={
            "X-Request-ID":   request_id,
            "X-Document-ID":  str(result.document_id),
            "X-Tenant-ID":    str(result.tenant_id),
            "Location":       f"/api/v1/documents/{result.document_id}/status",
        },
    )


# ---------------------------------------------------------------------------
# GET /documents/{document_id}/status
# ---------------------------------------------------------------------------

@router.get(
    "/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="Poll async processing status",
    responses={
        200: {"model": DocumentStatusResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def get_document_status(
    document_id: UUID,
    user:  TokenPayload   = Depends(require_role("viewer")),
    db:    AsyncSession   = Depends(lambda u=Depends(require_role("viewer")): _get_db_from_user(u)),
) -> DocumentStatusResponse:
    """
    Returns the current processing status of a document.
    RLS ensures tenants can only query their own documents.
    """
    result = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = result.scalars().first()

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UploadErrors.document_not_found(document_id).model_dump(),
        )

    return DocumentStatusResponse(
        document_id=doc.id,
        processing_status=ProcessingStatus(doc.status)
        if doc.status in ProcessingStatus._value2member_map_
        else ProcessingStatus.FAILED,
        chunk_count=doc.chunk_count,
        vector_count=doc.vector_count,
        error_message=doc.error_message,
        updated_at=doc.updated_at,
    )


# ---------------------------------------------------------------------------
# GET /documents/upload-progress/{upload_token}  — SSE stream
# ---------------------------------------------------------------------------

@router.get(
    "/upload-progress/{upload_token}",
    summary="Stream upload progress via Server-Sent Events",
    description=(
        "Connect via EventSource before calling POST /upload. "
        "Receives progress events: { stage, bytes_received, bytes_total, percent }. "
        "Stream auto-closes after upload completes or after 5 minutes."
    ),
    response_class=StreamingResponse,
)
async def stream_upload_progress(
    upload_token: str,
    request: Request,
    user: TokenPayload = Depends(require_role("viewer")),
) -> StreamingResponse:
    """
    SSE endpoint — emits upload progress events for the given upload_token.
    Client creates an EventSource connection and receives JSON-encoded progress.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _PROGRESS_QUEUES[upload_token] = queue

    async def event_generator() -> AsyncGenerator[str, None]:
        """Yield SSE-formatted events until done or disconnected."""
        start = time.monotonic()
        try:
            # Send initial connection event
            yield _sse_event(
                "connected",
                {"message": "Upload progress stream connected", "token": upload_token},
            )

            while True:
                # Respect client disconnection
                if await request.is_disconnected():
                    logger.debug("SSE client disconnected | token=%s", upload_token)
                    break

                # Enforce TTL
                if time.monotonic() - start > _UPLOAD_TOKEN_TTL:
                    yield _sse_event("timeout", {"message": "Progress stream expired"})
                    break

                try:
                    event: dict = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent proxy from closing connection
                    yield ": keepalive\n\n"
                    continue

                yield _sse_event(event.get("event", "upload_progress"), event)

                if event.get("stage") in ("queuing", "complete", "error"):
                    yield _sse_event("done", {"message": "Upload complete"})
                    break

        finally:
            _PROGRESS_QUEUES.pop(upload_token, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "Connection":       "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )


# ---------------------------------------------------------------------------
# GET /documents/  — list documents (tenant-scoped)
# ---------------------------------------------------------------------------

@router.get(
    "/",
    summary="List documents in tenant",
    responses={
        200: {"description": "Paginated list of documents"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
    },
)
async def list_documents(
    page:  int = 1,
    limit: int = 20,
    user:  TokenPayload = Depends(require_role("viewer")),
    db:    AsyncSession = Depends(lambda u=Depends(require_role("viewer")): _get_db_from_user(u)),
) -> dict:
    """
    Returns paginated list of documents for the authenticated tenant.
    Excludes soft-deleted documents. RLS enforces tenant scope.
    """
    if limit > 100:
        limit = 100
    offset = (page - 1) * limit

    result = await db.execute(
        select(Document)
        .where(Document.status != "deleted")
        .order_by(Document.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    docs = result.scalars().all()

    return {
        "page":      page,
        "limit":     limit,
        "documents": [
            {
                "document_id":       str(d.id),
                "document_name":     d.document_name,
                "filename":          d.filename,
                "status":            d.status,
                "size_bytes":        d.size_bytes,
                "content_type":      d.content_type,
                "chunk_count":       d.chunk_count,
                "vector_count":      d.vector_count,
                "created_at":        d.created_at.isoformat(),
            }
            for d in docs
        ],
    }


# ---------------------------------------------------------------------------
# DELETE /documents/{document_id}  — soft delete
# ---------------------------------------------------------------------------

@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a document",
    responses={
        204: {"description": "Document deleted"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def delete_document(
    document_id: UUID,
    user:    TokenPayload     = Depends(require_role("admin")),
    db:      AsyncSession     = Depends(lambda u=Depends(require_role("admin")): _get_db_from_user(u)),
    storage: S3StorageService = Depends(lambda u=Depends(require_role("admin")): _get_storage_from_user(u)),
) -> None:
    """
    Soft-deletes a document: sets status='deleted', tags S3 object.
    Hard S3 deletion is handled by a scheduled lifecycle job.
    Requires admin role or above.
    """
    from sqlalchemy import update as sa_update
    from app.models.documents import AuditLog
    from app.storage.s3 import ResourceType

    result = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = result.scalars().first()

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UploadErrors.document_not_found(document_id).model_dump(),
        )

    # Soft-delete in DB
    await db.execute(
        sa_update(Document)
        .where(Document.id == document_id)
        .values(status="deleted")
    )

    # Soft-delete in S3 (tag object — lifecycle rule handles purge)
    filename = doc.s3_key.rsplit("/", 1)[-1]
    try:
        await storage.delete_object(ResourceType.DOCUMENT, filename, hard=False)
    except Exception as exc:
        logger.warning("S3 soft-delete failed | doc=%s error=%s", document_id, exc)

    # Audit log
    db.add(AuditLog(
        tenant_id=user.tenant_id,
        user_id=uuid.UUID(user.sub) if _is_uuid(user.sub) else None,
        action="document.deleted",
        resource=f"document:{document_id}",
        metadata={"s3_key": doc.s3_key, "filename": doc.filename},
        success=True,
    ))


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

def _sse_event(event_name: str, data: dict) -> str:
    """Format a Server-Sent Event with event name and JSON data."""
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Progress publisher — called by IngestionService to push SSE events
# (Used internally; not a route)
# ---------------------------------------------------------------------------

async def publish_progress(upload_token: str, event: dict) -> None:
    """Push a progress event to the SSE queue for the given upload token."""
    queue = _PROGRESS_QUEUES.get(upload_token)
    if queue:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("SSE queue full for token=%s, dropping event", upload_token)


# ---------------------------------------------------------------------------
# Dependency wiring helpers
# These bridge the require_role() user to the DB and storage dependencies.
# ---------------------------------------------------------------------------

async def _get_db_from_user(user: TokenPayload) -> AsyncGenerator[AsyncSession, None]:
    """Yield a tenant-scoped DB session for the given user."""
    from app.db.session import get_db
    async for session in get_db(tenant_id=user.tenant_id):
        yield session


async def _get_storage_from_user(user: TokenPayload) -> S3StorageService:
    """Return a tenant-scoped S3 storage service."""
    from app.core.config import settings
    from app.storage.s3 import TenantStorageConfig

    config = TenantStorageConfig(
        tenant_id=user.tenant_id,
        kms_key_arn=settings.s3_default_kms_key_arn,
    )
    return S3StorageService(tenant_config=config)


def _extract_client_ip(request: Request) -> str | None:
    """Extract real client IP from X-Forwarded-For, falling back to direct connection."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # X-Forwarded-For may be a comma-separated list; take the first (client IP)
        ip = forwarded.split(",")[0].strip()
        return ip if ip else None
    return request.client.host if request.client else None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False
