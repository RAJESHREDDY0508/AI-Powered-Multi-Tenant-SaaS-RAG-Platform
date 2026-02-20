"""
Document Ingestion Router  —  Production Rewrite
═════════════════════════════════════════════════

Endpoints
─────────
  POST   /api/v1/documents/upload                     Upload + ingest a document
  GET    /api/v1/documents/{id}/status                Poll async processing status
  GET    /api/v1/documents/upload-progress/{token}    SSE real-time upload progress
  GET    /api/v1/documents/                           Paginated tenant document list
  DELETE /api/v1/documents/{id}                       Soft-delete a document

Dependency injection chain (no lambda hacks)
────────────────────────────────────────────
  Every route receives its dependencies through proper FastAPI Depends()
  classes/functions. The chain is:

    ┌─ require_member ──▶ get_current_user ──▶ HTTPBearer ──▶ verify_token
    │                                           (RS256, JWKS, Cognito/Auth0)
    │
    ├─ get_tenant_db  ──▶ get_current_user ──▶ get_db(tenant_id)
    │                     (extracts tenant_id from JWT, sets RLS GUC)
    │
    └─ get_tenant_storage ──▶ get_current_user ──▶ S3StorageService(kms_key_arn)

  This is strictly single-evaluation per request — FastAPI caches Depends()
  results within a single request scope, so get_current_user is called once
  even when it appears in multiple dependency trees.

SSE upload progress
───────────────────
  1. Client calls GET /upload-progress/{token}  →  EventSource connected
  2. Client calls POST /upload?upload_token={token}
  3. IngestionService receives a progress_cb that pushes to the SSE queue
  4. Each 5 MB part upload fires a progress event
  5. On completion, a "stage": "queuing" event closes the stream

Security properties (verified per-request)
──────────────────────────────────────────
  • tenant_id    extracted from JWT exclusively — never from body/query/header
  • MIME type    detected from magic bytes  — never from Content-Type
  • File size    rejected by Content-Length before reading the first byte
  • Checksum     computed server-side during streaming
  • S3 key       server-constructed; client never controls the path
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Annotated, AsyncGenerator, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_tenant_db, get_tenant_storage
from app.auth.rbac import require_role
from app.auth.token import TokenPayload, get_current_user
from app.models.documents import AuditLog, Document
from app.schemas.documents import (
    MAX_FILE_SIZE_BYTES,
    DocumentStatusResponse,
    DocumentUploadResponse,
    ErrorResponse,
    ProcessingStatus,
    UploadErrors,
)
from app.services.ingestion import IngestionService, TaskPublisher
from app.storage.s3 import ResourceType, S3StorageService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/documents",
    tags=["Document Ingestion"],
)


# ─────────────────────────────────────────────────────────────────────────────
# SSE progress store
# Key:   upload_token (str)
# Value: asyncio.Queue[dict]
#
# In production with multiple API instances, replace with Redis pub/sub:
#   await redis.publish(f"upload:progress:{token}", json.dumps(event))
# ─────────────────────────────────────────────────────────────────────────────

_SSE_QUEUES: dict[str, asyncio.Queue] = {}
_SSE_TTL_SECS = 300   # 5 minutes


# ─────────────────────────────────────────────────────────────────────────────
# Re-usable dependency shortcuts with proper RBAC
# These are named callables (not lambdas) so FastAPI can cache them correctly
# and they appear cleanly in OpenAPI docs.
# ─────────────────────────────────────────────────────────────────────────────

# member+ — for upload and write operations
RequireMember = Depends(require_role("member"))
# viewer+  — for read operations
RequireViewer = Depends(require_role("viewer"))
# admin+   — for destructive operations
RequireAdmin  = Depends(require_role("admin"))


async def _member_user(
    user: Annotated[TokenPayload, Depends(require_role("member"))],
) -> TokenPayload:
    return user


async def _viewer_user(
    user: Annotated[TokenPayload, Depends(require_role("viewer"))],
) -> TokenPayload:
    return user


async def _admin_user(
    user: Annotated[TokenPayload, Depends(require_role("admin"))],
) -> TokenPayload:
    return user


# ─────────────────────────────────────────────────────────────────────────────
# POST /documents/upload
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for async ingestion",
    description=(
        "Streams file directly to S3 via multipart upload. "
        "Returns 202 immediately; chunking + embedding runs asynchronously. "
        "Supports PDF, DOCX, TXT, MD — max 50 MB. "
        "Duplicate files (same MD5 within tenant) return 409."
    ),
    responses={
        202: {"model": DocumentUploadResponse,
              "description": "File stored; processing queued"},
        400: {"model": ErrorResponse,
              "description": "Invalid file type, empty file, or bad document_name"},
        401: {"model": ErrorResponse,
              "description": "Missing, expired, or invalid Bearer JWT"},
        403: {"model": ErrorResponse,
              "description": "Insufficient role — requires 'member' or above"},
        409: {"model": ErrorResponse,
              "description": "Duplicate document — same MD5 already exists in tenant"},
        413: {"model": ErrorResponse,
              "description": "File exceeds the 50 MB limit"},
        500: {"model": ErrorResponse,
              "description": "S3 failure or unhandled server error"},
        503: {"model": ErrorResponse,
              "description": "Message broker unavailable (file still stored; will retry)"},
    },
)
async def upload_document(
    request:    Request,

    # ── Multipart form fields ──────────────────────────────────────────
    file: UploadFile = File(
        ...,
        description="Document file (PDF, DOCX, TXT, MD). Max 50 MB.",
    ),
    document_name: str = Form(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable display name for this document.",
    ),
    document_permissions: Optional[str] = Form(
        None,
        description='Optional JSON object: e.g. {"groups": ["finance"], "public": false}',
    ),
    upload_token: Optional[str] = Form(
        None,
        description="SSE upload_token obtained from GET /upload-progress. "
                    "Enables real-time progress streaming.",
    ),

    # ── Auth dependencies (correctly chained, no lambdas) ─────────────
    user:    TokenPayload  = Depends(_member_user),
    db:      AsyncSession  = Depends(get_tenant_db),
    storage: S3StorageService = Depends(get_tenant_storage),
) -> JSONResponse:
    """
    Multipart upload handler.

    Security:
      • user.tenant_id comes from the verified JWT — never from the request body.
      • file type is detected from magic bytes before any DB/S3 operation.
      • Content-Length header is checked first to reject oversized requests early.
      • upload_token is optional; absence means no SSE progress stream.
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    # ── Early size rejection from Content-Length header ────────────────
    # This fires before reading a single byte of the body.
    try:
        raw_cl = request.headers.get("content-length")
        content_length: int | None = int(raw_cl) if raw_cl else None
    except (ValueError, TypeError):
        content_length = None

    if content_length and content_length > MAX_FILE_SIZE_BYTES + 8192:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content=UploadErrors.file_too_large(content_length).model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    # ── Parse optional permissions JSON ───────────────────────────────
    permissions: dict | None = None
    if document_permissions:
        try:
            permissions = json.loads(document_permissions)
            if not isinstance(permissions, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=ErrorResponse(
                    error_code="INVALID_PERMISSIONS_FORMAT",
                    message="document_permissions must be a valid JSON object.",
                    request_id=request_id,
                ).model_dump(mode="json"),
                headers={"X-Request-ID": request_id},
            )

    # ── Build SSE progress callback (if upload_token provided) ────────
    progress_cb = _make_progress_cb(upload_token, content_length) if upload_token else None

    # ── Build and run ingestion pipeline ──────────────────────────────
    service = IngestionService(
        db=db,
        storage=storage,
        user=user,
        task_publisher=TaskPublisher(),
        progress_cb=progress_cb,
    )

    try:
        result = await service.ingest(
            file=file,
            document_name=document_name,
            permissions=permissions,
            client_ip=_client_ip(request),
            content_length=content_length,
        )
    except HTTPException:
        # Re-raise FastAPI HTTPExceptions with their structured detail intact
        raise
    except Exception:
        logger.exception(
            "Unhandled ingestion error | tenant=%s request_id=%s",
            user.tenant_id, request_id,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=UploadErrors.internal_error(request_id).model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    # ── Emit final SSE event ───────────────────────────────────────────
    if upload_token:
        await _sse_push(upload_token, {
            "event": "upload_progress",
            "stage": "queuing",
            "bytes_received": result.size_bytes,
            "bytes_total":    result.size_bytes,
            "percent":        100.0,
        })

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=result.model_dump(mode="json"),
        headers={
            "X-Request-ID":  request_id,
            "X-Document-ID": str(result.document_id),
            "X-Tenant-ID":   str(result.tenant_id),
            "Location":      f"/api/v1/documents/{result.document_id}/status",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /documents/upload-progress/{upload_token}  — SSE stream
# Must be registered BEFORE /{document_id}/... routes to avoid shadowing.
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/upload-progress/{upload_token}",
    summary="Stream upload progress via Server-Sent Events",
    description=(
        "Connect via EventSource BEFORE calling POST /upload. "
        "Pass the same upload_token as a form field in the POST request. "
        "Receives JSON progress events; stream closes when upload is queued or times out."
    ),
    response_class=StreamingResponse,
    include_in_schema=True,
)
async def stream_upload_progress(
    upload_token: str,
    request:      Request,
    user: TokenPayload = Depends(_viewer_user),   # auth required even for SSE
) -> StreamingResponse:
    """
    SSE endpoint for real-time upload progress.

    Event format:
        event: upload_progress
        data: {"stage": "uploading", "bytes_received": 10485760,
                "bytes_total": 52428800, "percent": 20.0}

    Stages: uploading → validating → storing → queuing → done
    """
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
    _SSE_QUEUES[upload_token] = queue

    async def generate() -> AsyncGenerator[str, None]:
        start = time.monotonic()
        try:
            # Initial handshake
            yield _sse("connected", {
                "message":      "Progress stream ready",
                "upload_token": upload_token,
            })

            while True:
                # Honour client disconnect
                if await request.is_disconnected():
                    break

                # Enforce TTL
                if time.monotonic() - start > _SSE_TTL_SECS:
                    yield _sse("timeout", {"message": "Upload progress stream expired"})
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"   # prevent proxy timeouts
                    continue

                yield _sse(event.get("event", "upload_progress"), event)

                # Terminal stages close the stream
                if event.get("stage") in ("queuing", "error"):
                    yield _sse("done", {"message": "Upload pipeline complete"})
                    break

        finally:
            _SSE_QUEUES.pop(upload_token, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "Connection":        "keep-alive",
            "X-Accel-Buffering": "no",    # nginx: disable proxy buffering for SSE
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /documents/{document_id}/status
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="Poll async processing status of a document",
    responses={
        200: {"model": DocumentStatusResponse},
        401: {"model": ErrorResponse, "description": "Invalid JWT"},
        403: {"model": ErrorResponse, "description": "Insufficient role"},
        404: {"model": ErrorResponse, "description": "Document not found in this tenant"},
    },
)
async def get_document_status(
    document_id: UUID,
    user: TokenPayload = Depends(_viewer_user),
    db:   AsyncSession = Depends(get_tenant_db),
) -> DocumentStatusResponse:
    """
    Returns current processing pipeline state for a document.
    RLS on saas.documents automatically scopes the query to the tenant.
    """
    row = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = row.scalars().first()

    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UploadErrors.document_not_found(document_id).model_dump(),
        )

    # Map DB status to ProcessingStatus enum (graceful fallback)
    try:
        ps = ProcessingStatus(doc.status)
    except ValueError:
        ps = ProcessingStatus.FAILED

    return DocumentStatusResponse(
        document_id=doc.id,
        processing_status=ps,
        chunk_count=doc.chunk_count,
        vector_count=doc.vector_count,
        error_message=doc.error_message,
        updated_at=doc.updated_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /documents/
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/",
    summary="List documents in the authenticated tenant",
    response_description="Paginated document list",
    responses={
        200: {"description": "Document list"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
    },
)
async def list_documents(
    page:   int = Query(default=1,  ge=1,   description="Page number (1-based)"),
    limit:  int = Query(default=20, ge=1, le=100, description="Items per page"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Filter by status: pending | processing | ready | failed",
    ),
    user: TokenPayload = Depends(_viewer_user),
    db:   AsyncSession = Depends(get_tenant_db),
) -> dict:
    """
    Returns paginated, tenant-scoped document list.
    Soft-deleted documents are excluded by default.
    RLS enforces tenant isolation — no manual WHERE tenant_id clause needed.
    """
    offset = (page - 1) * limit

    query = select(Document).where(Document.status != "deleted")

    if status_filter and status_filter in ("pending", "processing", "ready", "failed"):
        query = query.where(Document.status == status_filter)

    query = query.order_by(Document.created_at.desc()).offset(offset).limit(limit)
    rows = await db.execute(query)
    docs = rows.scalars().all()

    return {
        "page":   page,
        "limit":  limit,
        "documents": [
            {
                "document_id":   str(d.id),
                "document_name": d.document_name,
                "filename":      d.filename,
                "status":        d.status,
                "size_bytes":    d.size_bytes,
                "content_type":  d.content_type,
                "chunk_count":   d.chunk_count,
                "vector_count":  d.vector_count,
                "created_at":    d.created_at.isoformat(),
                "updated_at":    d.updated_at.isoformat(),
            }
            for d in docs
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /documents/{document_id}
# ─────────────────────────────────────────────────────────────────────────────

@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a document",
    description=(
        "Sets status='deleted'; tags the S3 object (lifecycle rule purges after 30 days). "
        "Requires 'admin' role or above."
    ),
    responses={
        204: {"description": "Document soft-deleted"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def delete_document(
    document_id: UUID,
    user:    TokenPayload     = Depends(_admin_user),
    db:      AsyncSession     = Depends(get_tenant_db),
    storage: S3StorageService = Depends(get_tenant_storage),
) -> None:
    """
    Soft-delete: marks document as deleted in the DB and tags the S3 object.
    Vectors in Pinecone/Weaviate are purged by a background job watching
    for status='deleted' documents (outside the scope of this request).
    Requires admin role — viewers and members cannot delete.
    """
    row = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = row.scalars().first()

    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UploadErrors.document_not_found(document_id).model_dump(),
        )

    # Update status in DB
    await db.execute(
        sa_update(Document)
        .where(Document.id == document_id)
        .values(status="deleted")
    )

    # Tag the S3 object (soft delete — lifecycle rule expires it after 30 days)
    s3_filename = doc.s3_key.rsplit("/", 1)[-1]
    try:
        await storage.delete_object(ResourceType.DOCUMENT, s3_filename, hard=False)
    except Exception as exc:
        logger.warning(
            "S3 soft-delete tagging failed (non-fatal) | doc=%s error=%s",
            document_id, exc,
        )

    # Append audit log entry
    db.add(AuditLog(
        tenant_id=user.tenant_id,
        user_id=_safe_uuid(user.sub),
        action="document.deleted",
        resource=f"document:{document_id}",
        doc_metadata={
            "s3_key":   doc.s3_key,
            "filename": doc.filename,
            "size_bytes": doc.size_bytes,
        },
        success=True,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sse(event_name: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"


async def _sse_push(token: str, event: dict) -> None:
    """Non-blocking push to an SSE queue. Silently drops if queue is full or absent."""
    q = _SSE_QUEUES.get(token)
    if q:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug("SSE queue full for token=%s — event dropped", token)


def _make_progress_cb(upload_token: str, total_bytes: int | None):
    """
    Factory: returns an async progress callback that pushes SSE events.
    Captured in the IngestionService and called after each 5 MB S3 part.
    """
    async def _cb(bytes_received: int, bytes_total: int) -> None:
        total = bytes_total or total_bytes or bytes_received
        pct = round((bytes_received / total * 100), 1) if total else 0.0
        await _sse_push(upload_token, {
            "event":          "upload_progress",
            "stage":          "uploading",
            "bytes_received": bytes_received,
            "bytes_total":    total,
            "percent":        pct,
        })
    return _cb


def _client_ip(request: Request) -> str | None:
    """
    Extract real client IP.
    Trusts the first entry in X-Forwarded-For (set by the load balancer).
    Falls back to the direct TCP connection's remote address.
    """
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        ip = fwd.split(",")[0].strip()
        return ip or None
    return request.client.host if request.client else None


def _safe_uuid(value: str) -> uuid.UUID | None:
    """Safely parse a UUID string; returns None if invalid."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
