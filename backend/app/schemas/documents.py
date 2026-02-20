"""
Document Ingestion — Pydantic Request/Response Schemas

Covers the full lifecycle of POST /api/v1/documents/upload:
  - Upload request validation
  - Success response (202 Accepted)
  - All structured error bodies (400, 401, 403, 409, 413, 422, 500)
  - Processing status used by worker callbacks and SSE stream events

Design decisions:
  - document_id is always server-generated (UUID4); never client-supplied.
  - checksum is MD5 of the raw file bytes, computed server-side.
  - processing_status is the async pipeline state, separate from HTTP status.
  - All timestamps are ISO-8601 UTC strings (no ambiguous timezone-naive datetimes).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Allowed MIME types — enforced before touching S3
# ---------------------------------------------------------------------------

ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
        "application/msword",                    # legacy .doc
        "text/plain",
        "text/markdown",
    }
)

ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".doc", ".txt", ".md"}
)

# 50 MB hard ceiling — enforced in FastAPI route before reading body
MAX_FILE_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Processing pipeline state machine
# ---------------------------------------------------------------------------

class ProcessingStatus(str, Enum):
    """
    Maps to saas.documents.status column.
    Transitions: queued → processing → completed | failed
    """
    QUEUED      = "queued"       # message sent to broker, not yet picked up
    PROCESSING  = "processing"   # worker actively chunking + embedding
    COMPLETED   = "completed"    # vectors indexed, document ready for RAG
    FAILED      = "failed"       # unrecoverable pipeline error


# ---------------------------------------------------------------------------
# Upload success response — 202 Accepted
# ---------------------------------------------------------------------------

class DocumentUploadResponse(BaseModel):
    """
    Returned immediately after a successful upload.
    HTTP 202 — the file is stored but processing is async.
    """
    document_id:       UUID            = Field(..., description="Server-generated document UUID")
    status:            str             = Field("uploaded", description="Upload phase status")
    checksum:          str             = Field(..., description="MD5 hex digest of the uploaded file")
    processing_status: ProcessingStatus = Field(
        ProcessingStatus.QUEUED,
        description="Async pipeline state — poll /documents/{id}/status for updates",
    )
    s3_key:            str             = Field(..., description="Object key in tenant-partitioned S3 bucket")
    tenant_id:         UUID            = Field(..., description="Owning tenant (from JWT — never client-supplied)")
    document_name:     str             = Field(..., description="Sanitized document display name")
    size_bytes:        int             = Field(..., description="File size in bytes")
    content_type:      str             = Field(..., description="Detected MIME type")
    created_at:        datetime        = Field(..., description="UTC timestamp of upload completion")

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}


# ---------------------------------------------------------------------------
# Document status response — GET /documents/{id}/status
# ---------------------------------------------------------------------------

class DocumentStatusResponse(BaseModel):
    """Polled by clients to track async processing progress."""
    document_id:       UUID
    processing_status: ProcessingStatus
    chunk_count:       int  = Field(0, description="Chunks created so far")
    vector_count:      int  = Field(0, description="Vectors indexed so far")
    error_message:     str | None = None
    updated_at:        datetime


# ---------------------------------------------------------------------------
# SSE progress event payload — streamed to EventSource clients
# ---------------------------------------------------------------------------

class UploadProgressEvent(BaseModel):
    """
    Emitted as Server-Sent Events during the upload phase.
    event: upload_progress
    data: <json of this model>
    """
    event:         str  = "upload_progress"
    bytes_received: int
    bytes_total:    int
    percent:        float = Field(0.0, ge=0.0, le=100.0)
    stage:          str  = Field("uploading", description="uploading | validating | storing | queuing")


# ---------------------------------------------------------------------------
# Structured error bodies
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    """Single structured error — may appear in a list."""
    field:   str | None = Field(None, description="Request field that caused the error, if applicable")
    message: str
    code:    str         = Field(..., description="Machine-readable error code for client handling")


class ErrorResponse(BaseModel):
    """
    Uniform error envelope for all 4xx/5xx responses.
    Clients should check `error_code` for programmatic handling.
    """
    error_code:    str              = Field(..., description="Stable machine-readable code")
    message:       str              = Field(..., description="Human-readable summary")
    details:       list[ErrorDetail] = Field(default_factory=list)
    request_id:    str | None       = Field(None, description="Trace ID for log correlation")
    doc_url:       str | None       = Field(
        None,
        description="Link to error documentation",
    )


# ---------------------------------------------------------------------------
# Pre-defined error factories (keeps route handlers thin)
# ---------------------------------------------------------------------------

class UploadErrors:
    """Factories for every documented error case."""

    @staticmethod
    def unsupported_file_type(filename: str, detected_type: str) -> ErrorResponse:
        return ErrorResponse(
            error_code="UNSUPPORTED_FILE_TYPE",
            message=f"File type '{detected_type}' is not supported.",
            details=[
                ErrorDetail(
                    field="file",
                    message=(
                        f"'{filename}' has an unsupported type '{detected_type}'. "
                        f"Allowed: PDF, DOCX, TXT, MD."
                    ),
                    code="UNSUPPORTED_FILE_TYPE",
                )
            ],
        )

    @staticmethod
    def file_too_large(size_bytes: int) -> ErrorResponse:
        max_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
        return ErrorResponse(
            error_code="FILE_TOO_LARGE",
            message=f"Uploaded file exceeds the {max_mb} MB limit.",
            details=[
                ErrorDetail(
                    field="file",
                    message=f"Received {size_bytes:,} bytes; limit is {MAX_FILE_SIZE_BYTES:,} bytes.",
                    code="FILE_TOO_LARGE",
                )
            ],
        )

    @staticmethod
    def missing_file() -> ErrorResponse:
        return ErrorResponse(
            error_code="MISSING_FILE",
            message="No file was provided in the request.",
            details=[
                ErrorDetail(
                    field="file",
                    message="The 'file' multipart field is required.",
                    code="MISSING_FILE",
                )
            ],
        )

    @staticmethod
    def invalid_document_name(name: str) -> ErrorResponse:
        return ErrorResponse(
            error_code="INVALID_DOCUMENT_NAME",
            message="The provided document_name contains invalid characters.",
            details=[
                ErrorDetail(
                    field="document_name",
                    message=f"'{name}' must be 1-255 characters and cannot contain path separators.",
                    code="INVALID_DOCUMENT_NAME",
                )
            ],
        )

    @staticmethod
    def unauthorized() -> ErrorResponse:
        return ErrorResponse(
            error_code="UNAUTHORIZED",
            message="Authentication required. Provide a valid Bearer token.",
            details=[
                ErrorDetail(
                    field=None,
                    message="Missing or invalid Authorization header.",
                    code="UNAUTHORIZED",
                )
            ],
        )

    @staticmethod
    def token_expired() -> ErrorResponse:
        return ErrorResponse(
            error_code="TOKEN_EXPIRED",
            message="Your access token has expired. Please re-authenticate.",
            details=[],
        )

    @staticmethod
    def forbidden(required_role: str) -> ErrorResponse:
        return ErrorResponse(
            error_code="FORBIDDEN",
            message=f"Insufficient permissions. Role '{required_role}' or above is required.",
            details=[
                ErrorDetail(
                    field=None,
                    message="Contact your tenant administrator to request elevated access.",
                    code="FORBIDDEN",
                )
            ],
        )

    @staticmethod
    def duplicate_document(checksum: str, existing_id: UUID) -> ErrorResponse:
        return ErrorResponse(
            error_code="DUPLICATE_DOCUMENT",
            message="This file has already been uploaded to your tenant.",
            details=[
                ErrorDetail(
                    field="file",
                    message=(
                        f"A document with checksum '{checksum}' already exists "
                        f"(document_id: {existing_id}). "
                        "To re-ingest, delete the existing document first."
                    ),
                    code="DUPLICATE_DOCUMENT",
                )
            ],
        )

    @staticmethod
    def storage_error(detail: str | None = None) -> ErrorResponse:
        return ErrorResponse(
            error_code="STORAGE_ERROR",
            message="Failed to store the document. Please retry.",
            details=(
                [ErrorDetail(field=None, message=detail, code="STORAGE_ERROR")]
                if detail
                else []
            ),
        )

    @staticmethod
    def queue_error() -> ErrorResponse:
        return ErrorResponse(
            error_code="QUEUE_ERROR",
            message="Document was stored but could not be queued for processing.",
            details=[
                ErrorDetail(
                    field=None,
                    message="The message broker may be temporarily unavailable. The document will be retried.",
                    code="QUEUE_ERROR",
                )
            ],
        )

    @staticmethod
    def internal_error(request_id: str | None = None) -> ErrorResponse:
        return ErrorResponse(
            error_code="INTERNAL_ERROR",
            message="An unexpected error occurred. Our team has been notified.",
            details=[],
            request_id=request_id,
        )

    @staticmethod
    def document_not_found(document_id: UUID) -> ErrorResponse:
        return ErrorResponse(
            error_code="DOCUMENT_NOT_FOUND",
            message=f"Document '{document_id}' was not found in your tenant.",
            details=[],
        )


# ---------------------------------------------------------------------------
# HTTP status code → error code mapping (for OpenAPI documentation)
# ---------------------------------------------------------------------------

HTTP_ERROR_MAP: dict[int, str] = {
    400: "INVALID_REQUEST",        # malformed body, unsupported type, file too large
    401: "UNAUTHORIZED",           # missing/invalid/expired JWT
    403: "FORBIDDEN",              # valid JWT, insufficient role
    409: "DUPLICATE_DOCUMENT",     # checksum collision within tenant
    413: "FILE_TOO_LARGE",         # body exceeds MAX_FILE_SIZE_BYTES
    422: "VALIDATION_ERROR",       # FastAPI Pydantic validation failure
    500: "INTERNAL_ERROR",         # unhandled exception
    503: "QUEUE_ERROR",            # broker unavailable
}
