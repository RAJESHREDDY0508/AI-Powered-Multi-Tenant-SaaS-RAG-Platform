"""
Unit Tests — S3 Streaming Multipart Upload
═══════════════════════════════════════════
Tests for app/storage/multipart.py

Coverage:
  ✅ Successful multipart upload (single part, multi-part)
  ✅ MD5 checksum is computed correctly from full file bytes
  ✅ Progress callback is called after each part
  ✅ Oversized file (exceeds 50 MB mid-stream) raises 413
  ✅ Empty file raises 400
  ✅ upload_part failure triggers abort_multipart_upload
  ✅ complete_multipart_upload failure triggers abort
  ✅ create_multipart_upload failure propagates
  ✅ SSE-KMS params are sent on create_multipart_upload
  ✅ Part numbers are sequential (1-based)
  ✅ StreamUploadResult has correct fields
"""

from __future__ import annotations

import hashlib
import io
import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import HTTPException, UploadFile
from botocore.exceptions import ClientError


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_upload(content: bytes, filename: str = "test.pdf") -> UploadFile:
    f = UploadFile(filename=filename, file=io.BytesIO(content))
    return f


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "operation")


def _build_s3_mock(upload_id: str = "test-upload-id", part_etag: str = "etag-123") -> MagicMock:
    """Build a mock S3 client context manager."""
    s3 = AsyncMock()
    s3.__aenter__ = AsyncMock(return_value=s3)
    s3.__aexit__  = AsyncMock(return_value=None)
    s3.create_multipart_upload = AsyncMock(return_value={"UploadId": upload_id})
    s3.upload_part              = AsyncMock(return_value={"ETag": f'"{part_etag}"'})
    s3.complete_multipart_upload = AsyncMock(return_value={"ETag": f'"{part_etag}"'})
    s3.abort_multipart_upload   = AsyncMock(return_value={})
    return s3


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.s3
class TestStreamingMultipartUpload:

    @pytest.fixture(autouse=True)
    def patch_aioboto3(self):
        """Patch aioboto3.Session to avoid real AWS calls in all tests in this class."""
        pass  # Individual tests manage their own patching

    async def test_single_chunk_upload_succeeds(self, sample_pdf_bytes):
        """A file smaller than CHUNK_SIZE uploads as a single part."""
        from app.storage.multipart import streaming_multipart_upload

        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            result = await streaming_multipart_upload(
                upload=_make_upload(sample_pdf_bytes),
                bucket="test-bucket",
                s3_key="tenants/aaa/documents/test.pdf",
                content_type="application/pdf",
                kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
            )

        assert result.size_bytes     == len(sample_pdf_bytes)
        assert result.s3_key         == "tenants/aaa/documents/test.pdf"
        assert result.bucket         == "test-bucket"
        assert result.part_count     == 1
        assert len(result.md5_checksum) == 32   # MD5 hex is always 32 chars

    async def test_md5_checksum_matches_file_bytes(self, sample_pdf_bytes):
        """MD5 computed by streaming_multipart_upload must match hashlib.md5(file_bytes)."""
        from app.storage.multipart import streaming_multipart_upload

        expected_md5 = hashlib.md5(sample_pdf_bytes, usedforsecurity=False).hexdigest()
        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            result = await streaming_multipart_upload(
                upload=_make_upload(sample_pdf_bytes),
                bucket="test-bucket",
                s3_key="tenants/aaa/documents/test.pdf",
                content_type="application/pdf",
                kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
            )

        assert result.md5_checksum == expected_md5

    async def test_multipart_splits_into_correct_number_of_parts(self):
        """File of 12 MB with 5 MB chunk size should produce 3 parts (5+5+2)."""
        from app.storage.multipart import streaming_multipart_upload, CHUNK_SIZE

        # 12 MB file → ceil(12/5) = 3 parts
        content = b"x" * (12 * 1024 * 1024)
        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            result = await streaming_multipart_upload(
                upload=_make_upload(content, "large.pdf"),
                bucket="test-bucket",
                s3_key="tenants/aaa/documents/large.pdf",
                content_type="application/pdf",
                kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
            )

        assert result.part_count == 3
        assert result.size_bytes == len(content)
        # upload_part should have been called 3 times
        assert s3_mock.upload_part.call_count == 3

    async def test_progress_callback_called_after_each_part(self, sample_pdf_bytes):
        """progress_cb is called once per uploaded part with (bytes_received, bytes_total)."""
        from app.storage.multipart import streaming_multipart_upload

        progress_calls: list[tuple[int, int]] = []

        async def _cb(received: int, total: int) -> None:
            progress_calls.append((received, total))

        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            await streaming_multipart_upload(
                upload=_make_upload(sample_pdf_bytes),
                bucket="test-bucket",
                s3_key="tenants/aaa/documents/test.pdf",
                content_type="application/pdf",
                kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
                size_hint=len(sample_pdf_bytes),
                progress_cb=_cb,
            )

        # At least one progress call for single-part upload
        assert len(progress_calls) >= 1
        # bytes_received should equal the file size on the final call
        last_received, last_total = progress_calls[-1]
        assert last_received == len(sample_pdf_bytes)
        assert last_total    == len(sample_pdf_bytes)

    async def test_sse_kms_params_sent_on_create(self, sample_pdf_bytes):
        """SSE-KMS encryption parameters must be sent on create_multipart_upload."""
        from app.storage.multipart import streaming_multipart_upload

        kms_arn = "arn:aws:kms:us-east-1:123456789:key/my-tenant-key"
        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            await streaming_multipart_upload(
                upload=_make_upload(sample_pdf_bytes),
                bucket="test-bucket",
                s3_key="tenants/aaa/documents/test.pdf",
                content_type="application/pdf",
                kms_key_arn=kms_arn,
            )

        create_kwargs = s3_mock.create_multipart_upload.call_args[1]
        assert create_kwargs["ServerSideEncryption"] == "aws:kms"
        assert create_kwargs["SSEKMSKeyId"]          == kms_arn

    async def test_oversized_file_raises_413_and_aborts(self):
        """
        A file exceeding 50 MB mid-stream raises HTTP 413 and calls
        abort_multipart_upload to prevent orphaned parts.
        """
        from app.storage.multipart import streaming_multipart_upload, CHUNK_SIZE

        # Build a file that exceeds the limit across multiple chunks
        chunk_count = 12   # 12 × 5 MB = 60 MB > 50 MB limit
        content = b"x" * (chunk_count * CHUNK_SIZE)
        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            with pytest.raises(HTTPException) as exc_info:
                await streaming_multipart_upload(
                    upload=_make_upload(content, "huge.pdf"),
                    bucket="test-bucket",
                    s3_key="tenants/aaa/documents/huge.pdf",
                    content_type="application/pdf",
                    kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
                )

        assert exc_info.value.status_code == 413
        # abort must have been called to clean up partial upload
        s3_mock.abort_multipart_upload.assert_called_once()

    async def test_empty_file_raises_400(self):
        """Zero-byte file raises HTTP 400 MISSING_FILE."""
        from app.storage.multipart import streaming_multipart_upload

        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            with pytest.raises(HTTPException) as exc_info:
                await streaming_multipart_upload(
                    upload=_make_upload(b""),
                    bucket="test-bucket",
                    s3_key="tenants/aaa/documents/empty.pdf",
                    content_type="application/pdf",
                    kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
                )

        assert exc_info.value.status_code == 400
        # abort must be called even on empty file
        s3_mock.abort_multipart_upload.assert_called_once()

    async def test_upload_part_failure_calls_abort(self, sample_pdf_bytes):
        """If upload_part raises ClientError, abort_multipart_upload is called."""
        from app.storage.multipart import streaming_multipart_upload

        s3_mock = _build_s3_mock()
        s3_mock.upload_part = AsyncMock(side_effect=_client_error("RequestTimeout"))

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            with pytest.raises(ClientError):
                await streaming_multipart_upload(
                    upload=_make_upload(sample_pdf_bytes),
                    bucket="test-bucket",
                    s3_key="tenants/aaa/documents/test.pdf",
                    content_type="application/pdf",
                    kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
                )

        s3_mock.abort_multipart_upload.assert_called_once()

    async def test_complete_failure_calls_abort(self, sample_pdf_bytes):
        """If complete_multipart_upload raises ClientError, abort is called."""
        from app.storage.multipart import streaming_multipart_upload

        s3_mock = _build_s3_mock()
        s3_mock.complete_multipart_upload = AsyncMock(
            side_effect=_client_error("InternalError")
        )

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            with pytest.raises(ClientError):
                await streaming_multipart_upload(
                    upload=_make_upload(sample_pdf_bytes),
                    bucket="test-bucket",
                    s3_key="tenants/aaa/documents/test.pdf",
                    content_type="application/pdf",
                    kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
                )

        s3_mock.abort_multipart_upload.assert_called_once()

    async def test_create_multipart_failure_propagates(self, sample_pdf_bytes):
        """If create_multipart_upload fails, error propagates without abort attempt."""
        from app.storage.multipart import streaming_multipart_upload

        s3_mock = _build_s3_mock()
        s3_mock.create_multipart_upload = AsyncMock(
            side_effect=_client_error("AccessDenied")
        )

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            with pytest.raises(ClientError):
                await streaming_multipart_upload(
                    upload=_make_upload(sample_pdf_bytes),
                    bucket="test-bucket",
                    s3_key="tenants/aaa/documents/test.pdf",
                    content_type="application/pdf",
                    kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
                )

        # No abort call — upload never started
        s3_mock.abort_multipart_upload.assert_not_called()

    async def test_part_numbers_are_sequential_and_1_based(self):
        """Each part sent to upload_part must have sequential PartNumber starting from 1."""
        from app.storage.multipart import streaming_multipart_upload, CHUNK_SIZE

        content = b"x" * (CHUNK_SIZE * 2 + 1024)   # 3 parts
        s3_mock = _build_s3_mock()

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            await streaming_multipart_upload(
                upload=_make_upload(content, "multi.pdf"),
                bucket="test-bucket",
                s3_key="tenants/aaa/documents/multi.pdf",
                content_type="application/pdf",
                kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
            )

        part_numbers = [
            call_args[1]["PartNumber"]
            for call_args in s3_mock.upload_part.call_args_list
        ]
        assert part_numbers == [1, 2, 3]

    async def test_result_contains_complete_fields(self, sample_pdf_bytes):
        """StreamUploadResult must contain all required fields."""
        from app.storage.multipart import streaming_multipart_upload, StreamUploadResult

        s3_mock = _build_s3_mock(upload_id="uid-123", part_etag="abc123")

        with patch("app.storage.multipart.aioboto3.Session") as mock_session:
            mock_session.return_value.client.return_value = s3_mock
            result = await streaming_multipart_upload(
                upload=_make_upload(sample_pdf_bytes),
                bucket="my-bucket",
                s3_key="tenants/test/documents/doc.pdf",
                content_type="application/pdf",
                kms_key_arn="arn:aws:kms:us-east-1:000:key/test",
            )

        assert isinstance(result, StreamUploadResult)
        assert result.s3_key      == "tenants/test/documents/doc.pdf"
        assert result.bucket      == "my-bucket"
        assert result.size_bytes  == len(sample_pdf_bytes)
        assert result.part_count  == 1
        assert result.etag        == "abc123"
        assert len(result.md5_checksum) == 32
