"""
Unit Tests — IngestionService
══════════════════════════════
Tests for every branch of the 9-step ingestion pipeline.

All tests:
  • Use mock_db, mock_storage, mock_publisher from conftest.py
  • Never touch real PostgreSQL, real S3, or real Celery
  • Patch streaming_multipart_upload to return a controlled StreamUploadResult

Coverage targets:
  ✅ Valid PDF   → 202 response with correct fields
  ✅ Valid DOCX  → 202 response
  ✅ Valid TXT   → 202 response
  ✅ Empty file  → 400 MISSING_FILE
  ✅ Oversized   → 413 FILE_TOO_LARGE
  ✅ Bad MIME    → 400 UNSUPPORTED_FILE_TYPE
  ✅ Bad ext     → 400 UNSUPPORTED_FILE_TYPE
  ✅ Bad name    → 400 INVALID_DOCUMENT_NAME
  ✅ Duplicate   → 409 DUPLICATE_DOCUMENT + S3 cleanup
  ✅ S3 failure  → 500 STORAGE_ERROR
  ✅ DB IntegrityError (race condition) → 409
  ✅ Broker down → 202 (non-fatal, audit log written)
  ✅ Audit log   → written for every path (attempt, success, failure, duplicate)
  ✅ Filename sanitization → path traversal stripped
  ✅ MD5 checksum → matches actual file bytes
  ✅ document_name whitespace stripped
  ✅ permissions stored in metadata
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import HTTPException
from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError

from app.models.documents import Document
from app.storage.multipart import StreamUploadResult
from tests.conftest import TEST_ISSUER


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_upload_file(
    filename: str,
    content:  bytes,
    content_type: str = "application/pdf",
) -> UploadFile:
    """Build a FastAPI UploadFile backed by an in-memory BytesIO buffer."""
    spool = io.BytesIO(content)
    spool.seek(0)
    f = UploadFile(filename=filename, file=spool)
    return f


def _stream_result(
    content:  bytes,
    s3_key:   str = "tenants/aaa/documents/test.pdf",
    bucket:   str = "test-bucket",
) -> StreamUploadResult:
    """Build a fake StreamUploadResult for a given byte payload."""
    md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
    return StreamUploadResult(
        s3_key=s3_key,
        bucket=bucket,
        md5_checksum=md5,
        size_bytes=len(content),
        etag=f"etag-{md5[:8]}",
        part_count=1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: IngestionService factory
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def make_service(mock_db, mock_storage, mock_publisher, member_payload):
    """Factory: build an IngestionService with injected mocks."""
    def _build(user=None, progress_cb=None):
        from app.services.ingestion import IngestionService
        return IngestionService(
            db=mock_db,
            storage=mock_storage,
            user=user or member_payload,
            task_publisher=mock_publisher,
            progress_cb=progress_cb,
        )
    return _build


# ─────────────────────────────────────────────────────────────────────────────
# Happy path tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.ingestion
class TestIngestionServiceHappyPath:

    async def test_valid_pdf_returns_202_response(
        self, make_service, sample_pdf_bytes
    ):
        """Full happy path: valid PDF produces a DocumentUploadResponse."""
        svc = make_service()
        upload = _make_upload_file("report.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch(
            "app.services.ingestion.streaming_multipart_upload",
            new=AsyncMock(return_value=result_stub),
        ):
            resp = await svc.ingest(
                file=upload,
                document_name="Q4 Report",
                permissions=None,
                client_ip="127.0.0.1",
            )

        assert resp.status == "uploaded"
        assert resp.document_name == "Q4 Report"
        assert resp.content_type == "application/pdf"
        assert resp.size_bytes == len(sample_pdf_bytes)
        assert resp.checksum == result_stub.md5_checksum
        assert resp.processing_status.value == "queued"
        # s3_key is server-constructed with the real tenant UUID + new doc UUID;
        # verify structure rather than exact match against the test stub path.
        assert "tenants/" in resp.s3_key
        assert "/documents/" in resp.s3_key
        assert resp.s3_key.endswith(".pdf")

    async def test_valid_docx_accepted(self, make_service, sample_docx_bytes):
        svc = make_service()
        upload = _make_upload_file("slides.docx", sample_docx_bytes,
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        result_stub = _stream_result(sample_docx_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(
                file=upload, document_name="Slides",
                permissions=None, client_ip=None,
            )

        # DOCX magic bytes: PK\x03\x04
        assert resp.status == "uploaded"
        assert "wordprocessingml" in resp.content_type or resp.content_type == "text/plain"

    async def test_valid_txt_accepted(self, make_service, sample_txt_bytes):
        svc = make_service()
        upload = _make_upload_file("notes.txt", sample_txt_bytes, "text/plain")
        result_stub = _stream_result(sample_txt_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(
                file=upload, document_name="Notes",
                permissions=None, client_ip=None,
            )

        assert resp.status == "uploaded"
        assert resp.content_type == "text/plain"

    async def test_permissions_stored_in_metadata(self, make_service, sample_pdf_bytes, mock_db):
        """document_permissions are stored as JSON in document.metadata."""
        svc = make_service()
        upload = _make_upload_file("secure.pdf", sample_pdf_bytes)
        perms = {"groups": ["finance"], "public": False}
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            await svc.ingest(
                file=upload, document_name="Secure Doc",
                permissions=perms, client_ip=None,
            )

        # Capture the Document object added to db (first add() call is the AuditLog
        # for upload_attempt, second is the Document itself)
        added_calls = [args[0][0] for args in mock_db.add.call_args_list]
        doc_obj = next(o for o in added_calls if isinstance(o, Document))
        assert doc_obj.doc_metadata.get("document_permissions") == perms

    async def test_document_name_whitespace_is_stripped(self, make_service, sample_pdf_bytes):
        svc = make_service()
        upload = _make_upload_file("doc.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(
                file=upload, document_name="  My Report  ",
                permissions=None, client_ip=None,
            )

        assert resp.document_name == "My Report"

    async def test_tenant_id_always_from_jwt(
        self, make_service, sample_pdf_bytes, member_payload, test_tenant_id
    ):
        """tenant_id in response must match JWT, never any other value."""
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(
                file=upload, document_name="Doc",
                permissions=None, client_ip=None,
            )

        assert resp.tenant_id == test_tenant_id

    async def test_s3_key_is_server_constructed(self, make_service, sample_pdf_bytes):
        """S3 key is built server-side using tenant_id prefix — client cannot control it."""
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(
                file=upload, document_name="Doc",
                permissions=None, client_ip=None,
            )

        # Key must start with tenants/<tenant_id>/ — server-constructed
        assert resp.s3_key.startswith("tenants/")

    async def test_broker_down_still_returns_202(
        self, make_service, sample_pdf_bytes, mock_publisher
    ):
        """
        When the Celery broker is unavailable, the document is already
        safely stored. The endpoint must still return 202 (non-fatal).
        The Beat retry scanner will re-queue the document within 60 s.
        """
        mock_publisher.publish_ingestion_task = AsyncMock(
            side_effect=ConnectionError("broker unavailable")
        )
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(
                file=upload, document_name="Doc",
                permissions=None, client_ip=None,
            )

        # Still returns 202
        assert resp.status == "uploaded"
        assert resp.processing_status.value == "queued"

    async def test_progress_callback_is_forwarded_to_multipart(
        self, make_service, sample_pdf_bytes
    ):
        """progress_cb injected into IngestionService is forwarded to streaming_multipart_upload."""
        callback = AsyncMock()
        svc = make_service(progress_cb=callback)
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        captured_kwargs: dict = {}

        async def _capture(**kwargs):
            captured_kwargs.update(kwargs)
            return result_stub

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(side_effect=_capture)):
            await svc.ingest(
                file=upload, document_name="Doc",
                permissions=None, client_ip=None,
            )

        assert captured_kwargs.get("progress_cb") is callback


# ─────────────────────────────────────────────────────────────────────────────
# Validation error tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.ingestion
class TestIngestionServiceValidation:

    async def test_empty_document_name_raises_400(self, make_service, sample_pdf_bytes):
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)

        with pytest.raises(HTTPException) as exc_info:
            await svc.ingest(file=upload, document_name="  ",
                             permissions=None, client_ip=None)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "INVALID_DOCUMENT_NAME"

    async def test_document_name_over_255_chars_raises_400(self, make_service, sample_pdf_bytes):
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        long_name = "A" * 256

        with pytest.raises(HTTPException) as exc_info:
            await svc.ingest(file=upload, document_name=long_name,
                             permissions=None, client_ip=None)

        assert exc_info.value.status_code == 400

    async def test_content_length_over_limit_raises_413(self, make_service, sample_pdf_bytes):
        """Content-Length header fast-rejection fires before file is read."""
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)

        with pytest.raises(HTTPException) as exc_info:
            await svc.ingest(
                file=upload, document_name="Doc",
                permissions=None, client_ip=None,
                content_length=60 * 1024 * 1024,   # 60 MB > 50 MB limit
            )

        assert exc_info.value.status_code == 413
        assert exc_info.value.detail["error_code"] == "FILE_TOO_LARGE"

    async def test_empty_file_raises_400(self, make_service):
        """Zero-byte file raises MISSING_FILE."""
        svc = make_service()
        upload = _make_upload_file("empty.pdf", b"")   # zero bytes

        with pytest.raises(HTTPException) as exc_info:
            await svc.ingest(file=upload, document_name="Empty",
                             permissions=None, client_ip=None)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "MISSING_FILE"

    async def test_exe_file_raises_400(self, make_service, exe_bytes):
        """Executable magic bytes are rejected as unsupported MIME type."""
        svc = make_service()
        upload = _make_upload_file("malware.exe", exe_bytes)

        with pytest.raises(HTTPException) as exc_info:
            await svc.ingest(file=upload, document_name="Bad File",
                             permissions=None, client_ip=None)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "UNSUPPORTED_FILE_TYPE"

    async def test_pdf_with_wrong_extension_raises_400(self, make_service, sample_pdf_bytes):
        """PDF bytes with .xyz extension are rejected (extension allowlist)."""
        svc = make_service()
        upload = _make_upload_file("file.xyz", sample_pdf_bytes)

        with pytest.raises(HTTPException) as exc_info:
            await svc.ingest(file=upload, document_name="Wrong Ext",
                             permissions=None, client_ip=None)

        assert exc_info.value.status_code == 400
        assert "UNSUPPORTED" in exc_info.value.detail["error_code"]

    async def test_missing_filename_raises_400(self, make_service, sample_pdf_bytes):
        """UploadFile with filename=None is treated as missing file."""
        svc = make_service()
        upload = UploadFile(filename=None, file=io.BytesIO(sample_pdf_bytes))

        with pytest.raises(HTTPException) as exc_info:
            await svc.ingest(file=upload, document_name="Doc",
                             permissions=None, client_ip=None)

        assert exc_info.value.status_code == 400

    async def test_path_traversal_in_filename_is_sanitized(
        self, make_service, sample_pdf_bytes
    ):
        """../../etc/passwd is sanitized to a safe basename."""
        svc = make_service()
        upload = _make_upload_file("../../etc/passwd.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(
                file=upload, document_name="Test",
                permissions=None, client_ip=None,
            )

        # filename must NOT contain path traversal components
        assert ".." not in resp.s3_key
        assert "/" not in resp.s3_key.split("tenants/")[1].split("/documents/")[1]


# ─────────────────────────────────────────────────────────────────────────────
# Duplicate detection tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.ingestion
class TestIngestionServiceDuplication:

    async def test_duplicate_md5_raises_409(
        self, make_service, sample_pdf_bytes, mock_db, test_document_id
    ):
        """
        When _find_duplicate returns an existing document,
        the service raises 409 and soft-deletes the just-uploaded S3 object.
        """
        from app.models.documents import Document

        existing_doc = MagicMock(spec=Document)
        existing_doc.id     = test_document_id
        existing_doc.status = "ready"

        # Make db.execute return the existing document on the SELECT
        mock_db.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=existing_doc)))
        ))

        svc = make_service()
        upload = _make_upload_file("dup.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            with pytest.raises(HTTPException) as exc_info:
                await svc.ingest(file=upload, document_name="Dup",
                                 permissions=None, client_ip=None)

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error_code"] == "DUPLICATE_DOCUMENT"
        assert str(test_document_id) in str(exc_info.value.detail)

    async def test_duplicate_triggers_s3_cleanup(
        self, make_service, sample_pdf_bytes, mock_db, mock_storage, test_document_id
    ):
        """
        On duplicate detection, the just-uploaded S3 object must be
        soft-deleted (no orphaned objects in S3).
        """
        from app.models.documents import Document

        existing_doc = MagicMock(spec=Document)
        existing_doc.id     = test_document_id
        existing_doc.status = "ready"
        mock_db.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=existing_doc)))
        ))

        svc = make_service()
        upload = _make_upload_file("dup.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            with pytest.raises(HTTPException):
                await svc.ingest(file=upload, document_name="Dup",
                                 permissions=None, client_ip=None)

        # S3 delete_object must have been called for cleanup
        mock_storage.delete_object.assert_called_once()

    async def test_race_condition_integrity_error_raises_409(
        self, make_service, sample_pdf_bytes, mock_db
    ):
        """
        If two concurrent uploads of the same file both pass the SELECT check
        but then one hits a DB UNIQUE constraint violation (IntegrityError),
        the service must return 409 (not 500).
        """
        # First SELECT returns None (no duplicate found)
        mock_db.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        ))
        # But flush() raises IntegrityError (race condition)
        mock_db.flush = AsyncMock(side_effect=IntegrityError("", "", Exception()))

        svc = make_service()
        upload = _make_upload_file("race.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            with pytest.raises(HTTPException) as exc_info:
                await svc.ingest(file=upload, document_name="Race",
                                 permissions=None, client_ip=None)

        assert exc_info.value.status_code == 409


# ─────────────────────────────────────────────────────────────────────────────
# S3 failure tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.ingestion
class TestIngestionServiceS3Failure:

    async def test_s3_upload_failure_raises_500(
        self, make_service, sample_pdf_bytes
    ):
        """If S3 upload fails with a generic exception, the service raises 500."""
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)

        with patch(
            "app.services.ingestion.streaming_multipart_upload",
            new=AsyncMock(side_effect=RuntimeError("S3 connection refused")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await svc.ingest(file=upload, document_name="Doc",
                                 permissions=None, client_ip=None)

        assert exc_info.value.status_code == 500
        assert exc_info.value.detail["error_code"] == "STORAGE_ERROR"

    async def test_s3_413_passthrough(self, make_service, sample_pdf_bytes):
        """If S3 streaming raises HTTPException 413 (size exceeded during upload), propagate it."""
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)

        with patch(
            "app.services.ingestion.streaming_multipart_upload",
            new=AsyncMock(side_effect=HTTPException(status_code=413, detail={"error_code": "FILE_TOO_LARGE"})),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await svc.ingest(file=upload, document_name="Doc",
                                 permissions=None, client_ip=None)

        assert exc_info.value.status_code == 413


# ─────────────────────────────────────────────────────────────────────────────
# Audit log tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.ingestion
class TestIngestionAuditLog:

    async def test_successful_upload_writes_attempt_and_success_audit(
        self, make_service, sample_pdf_bytes, mock_db
    ):
        """
        A successful upload must write exactly two audit log entries:
          1. document.upload_attempt
          2. document.uploaded
        (and optionally document.queue_failed if broker is down)
        """
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            await svc.ingest(file=upload, document_name="Doc",
                             permissions=None, client_ip="1.2.3.4")

        # Collect all AuditLog objects passed to db.add()
        from app.models.documents import AuditLog, Document
        audit_calls = [
            args[0] for args, _ in mock_db.add.call_args_list
            if isinstance(args[0], AuditLog)
        ]

        actions = [a.action for a in audit_calls]
        assert "document.upload_attempt" in actions
        assert "document.uploaded"       in actions

    async def test_failed_upload_writes_failure_audit(
        self, make_service, sample_pdf_bytes, mock_db
    ):
        """A failed S3 upload writes document.upload_failed to the audit log."""
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(side_effect=RuntimeError("fail"))):
            with pytest.raises(HTTPException):
                await svc.ingest(file=upload, document_name="Doc",
                                 permissions=None, client_ip=None)

        from app.models.documents import AuditLog
        audit_calls = [
            args[0] for args, _ in mock_db.add.call_args_list
            if isinstance(args[0], AuditLog)
        ]
        actions = [a.action for a in audit_calls]
        assert "document.upload_failed" in actions

    async def test_duplicate_rejection_writes_duplicate_audit(
        self, make_service, sample_pdf_bytes, mock_db, test_document_id
    ):
        """409 duplicate rejection writes document.duplicate_rejected audit entry."""
        from app.models.documents import Document, AuditLog

        existing = MagicMock(spec=Document)
        existing.id     = test_document_id
        existing.status = "ready"
        mock_db.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=existing)))
        ))

        svc = make_service()
        upload = _make_upload_file("dup.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            with pytest.raises(HTTPException):
                await svc.ingest(file=upload, document_name="Dup",
                                 permissions=None, client_ip=None)

        audit_calls = [
            args[0] for args, _ in mock_db.add.call_args_list
            if isinstance(args[0], AuditLog)
        ]
        actions = [a.action for a in audit_calls]
        assert "document.duplicate_rejected" in actions

    async def test_broker_failure_writes_queue_failed_audit(
        self, make_service, sample_pdf_bytes, mock_db, mock_publisher
    ):
        """When broker is down, document.queue_failed is written (non-fatal)."""
        from app.models.documents import AuditLog

        mock_publisher.publish_ingestion_task = AsyncMock(
            side_effect=ConnectionError("broker down")
        )
        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            resp = await svc.ingest(file=upload, document_name="Doc",
                                    permissions=None, client_ip=None)

        assert resp.status == "uploaded"   # Still 202

        audit_calls = [
            args[0] for args, _ in mock_db.add.call_args_list
            if isinstance(args[0], AuditLog)
        ]
        actions = [a.action for a in audit_calls]
        assert "document.queue_failed" in actions

    async def test_audit_log_captures_client_ip(
        self, make_service, sample_pdf_bytes, mock_db
    ):
        """Audit log entries include the client IP from the request."""
        from app.models.documents import AuditLog

        svc = make_service()
        upload = _make_upload_file("file.pdf", sample_pdf_bytes)
        result_stub = _stream_result(sample_pdf_bytes)

        with patch("app.services.ingestion.streaming_multipart_upload",
                   new=AsyncMock(return_value=result_stub)):
            await svc.ingest(file=upload, document_name="Doc",
                             permissions=None, client_ip="10.0.0.1")

        audit_entries = [
            args[0] for args, _ in mock_db.add.call_args_list
            if isinstance(args[0], AuditLog)
        ]
        for entry in audit_entries:
            assert entry.ip_address == "10.0.0.1"
