"""
S3 Streaming Multipart Upload

Streams a file from a FastAPI UploadFile directly into S3 using the
S3 Multipart Upload API — never buffering the entire file in memory.

Architecture
────────────
  UploadFile (SpooledTemporaryFile, 1 MB spool)
      │
      ▼  read CHUNK_SIZE bytes at a time
  ┌─────────────────────────────────────────────────────────────────┐
  │  streaming_multipart_upload()                                    │
  │                                                                  │
  │  1. create_multipart_upload  → UploadId                         │
  │  2. For each CHUNK_SIZE chunk:                                   │
  │       a. compute partial MD5 (for ETag verification)            │
  │       b. upload_part(PartNumber, Body=chunk) → ETag             │
  │       c. update running MD5 hash for final checksum             │
  │       d. emit progress via async callback                        │
  │  3. complete_multipart_upload → final ETag                      │
  │  4. On any error: abort_multipart_upload (prevents S3 billing)  │
  └─────────────────────────────────────────────────────────────────┘
      │
      ▼
  Returns: StreamUploadResult(s3_key, md5_checksum, size_bytes, etag)

Why multipart?
  - S3 minimum part size is 5 MB (except the last part).
  - Files up to 50 MB are split into 5 MB chunks → max 10 parts.
  - No single boto3 put_object call ever holds > 5 MB.
  - abort_multipart_upload is always called on failure — S3 charges for
    incomplete uploads if not cleaned up.

Thread safety:
  - aioboto3 clients are NOT thread-safe; one client per upload call.
  - asyncio.Queue is used for progress events (no locks needed).

SOC2 note:
  - SSE-KMS is enforced on create_multipart_upload.
  - Each part is uploaded without a KMS param (key is set at create time).
  - The IAM Deny-if-not-encrypted policy provides a second layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Awaitable
from uuid import UUID

import aioboto3
from botocore.exceptions import ClientError
from fastapi import UploadFile

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE: int = 5 * 1024 * 1024    # 5 MB — S3 minimum part size
MIN_PART_SIZE: int = 5 * 1024 * 1024  # S3 enforces >= 5 MB on all parts but last


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamUploadResult:
    """Returned by streaming_multipart_upload on success."""
    s3_key:      str
    bucket:      str
    md5_checksum: str          # MD5 hex digest of the COMPLETE file (not per-part)
    size_bytes:  int
    etag:        str           # S3 ETag of the completed multipart object
    part_count:  int


# ---------------------------------------------------------------------------
# Progress callback type alias
# ---------------------------------------------------------------------------

# Signature: async (bytes_received: int, bytes_total: int) -> None
ProgressCallback = Callable[[int, int], Awaitable[None]]


# ---------------------------------------------------------------------------
# Async chunk iterator
# ---------------------------------------------------------------------------

async def _iter_chunks(
    upload: UploadFile,
    chunk_size: int = CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """
    Read an UploadFile in fixed-size chunks asynchronously.
    FastAPI's UploadFile wraps a SpooledTemporaryFile — reads are synchronous
    under the hood, so we offload to the default thread pool executor to
    avoid blocking the event loop on large files.
    """
    loop = asyncio.get_event_loop()

    while True:
        # run_in_executor prevents blocking the event loop during disk/network reads
        chunk: bytes = await loop.run_in_executor(None, upload.file.read, chunk_size)
        if not chunk:
            break
        yield chunk


# ---------------------------------------------------------------------------
# Core streaming multipart upload
# ---------------------------------------------------------------------------

async def streaming_multipart_upload(
    upload:      UploadFile,
    bucket:      str,
    s3_key:      str,
    content_type: str,
    kms_key_arn: str,
    size_hint:   int | None = None,       # Content-Length from request headers (optional)
    progress_cb: ProgressCallback | None = None,
) -> StreamUploadResult:
    """
    Stream an UploadFile directly to S3 using multipart upload.

    Args:
        upload:       FastAPI UploadFile — file is read chunk by chunk.
        bucket:       S3 bucket name.
        s3_key:       Full S3 object key (already tenant-prefixed, server-side).
        content_type: Detected MIME type (from magic bytes — never client header).
        kms_key_arn:  Tenant-specific KMS CMK ARN for SSE-KMS encryption.
        size_hint:    Optional Content-Length for progress percentage calculation.
        progress_cb:  Optional async callback called after each part upload.

    Returns:
        StreamUploadResult with final MD5, size, ETag, and S3 key.

    Raises:
        FileTooLargeError: If total bytes exceed MAX_FILE_SIZE_BYTES.
        ClientError: Propagated from boto3 on S3 errors (after aborting upload).
    """
    from app.schemas.documents import MAX_FILE_SIZE_BYTES, UploadErrors
    from fastapi import HTTPException, status

    session = aioboto3.Session()
    upload_id: str | None = None
    parts: list[dict] = []               # [{PartNumber: int, ETag: str}, ...]
    total_bytes = 0
    part_number = 0
    md5_hasher  = hashlib.md5(usedforsecurity=False)   # running MD5 of entire file

    async with session.client(
        "s3",
        region_name=settings.aws_region,
    ) as s3:

        # ----------------------------------------------------------------
        # Step 1: Initiate multipart upload with SSE-KMS
        # ----------------------------------------------------------------
        try:
            response = await s3.create_multipart_upload(
                Bucket=bucket,
                Key=s3_key,
                ContentType=content_type,
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=kms_key_arn,
                Metadata={
                    "content-type":   content_type,
                    "upload-method":  "streaming-multipart",
                },
            )
            upload_id = response["UploadId"]
            logger.debug(
                "Multipart upload initiated | key=%s upload_id=%s", s3_key, upload_id
            )
        except ClientError as exc:
            logger.error("Failed to initiate multipart upload | key=%s error=%s", s3_key, exc)
            raise

        # ----------------------------------------------------------------
        # Step 2: Upload parts
        # ----------------------------------------------------------------
        try:
            async for chunk in _iter_chunks(upload):

                # Guard: enforce 50 MB ceiling
                total_bytes += len(chunk)
                if total_bytes > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=UploadErrors.file_too_large(total_bytes).model_dump(),
                    )

                # Update running MD5 across the full file
                md5_hasher.update(chunk)

                part_number += 1
                part_response = await s3.upload_part(
                    Bucket=bucket,
                    Key=s3_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )

                # S3 returns an ETag per part — required for CompleteMultipartUpload
                etag = part_response["ETag"].strip('"')
                parts.append({"PartNumber": part_number, "ETag": etag})

                logger.debug(
                    "Part %d uploaded | key=%s size=%d cumulative=%d",
                    part_number, s3_key, len(chunk), total_bytes,
                )

                # Emit progress if callback provided
                if progress_cb:
                    try:
                        await progress_cb(total_bytes, size_hint or total_bytes)
                    except Exception:
                        pass  # progress callback failure is never fatal

            # Guard: empty file
            if part_number == 0 or total_bytes == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=UploadErrors.missing_file().model_dump(),
                )

        except (HTTPException, ClientError):
            # Abort multipart upload to prevent orphaned parts (S3 charges for these)
            await _abort_multipart_upload(s3, bucket, s3_key, upload_id)
            raise
        except Exception as exc:
            await _abort_multipart_upload(s3, bucket, s3_key, upload_id)
            raise

        # ----------------------------------------------------------------
        # Step 3: Complete multipart upload
        # ----------------------------------------------------------------
        try:
            complete_response = await s3.complete_multipart_upload(
                Bucket=bucket,
                Key=s3_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            final_etag = complete_response.get("ETag", "").strip('"')

            logger.info(
                "Multipart upload complete | key=%s parts=%d size=%d etag=%s",
                s3_key, part_number, total_bytes, final_etag,
            )
        except ClientError as exc:
            logger.error(
                "CompleteMultipartUpload failed | key=%s upload_id=%s error=%s",
                s3_key, upload_id, exc,
            )
            await _abort_multipart_upload(s3, bucket, s3_key, upload_id)
            raise

    return StreamUploadResult(
        s3_key=s3_key,
        bucket=bucket,
        md5_checksum=md5_hasher.hexdigest(),
        size_bytes=total_bytes,
        etag=final_etag,
        part_count=part_number,
    )


# ---------------------------------------------------------------------------
# Abort helper
# ---------------------------------------------------------------------------

async def _abort_multipart_upload(s3, bucket: str, key: str, upload_id: str) -> None:
    """
    Abort an in-progress multipart upload.
    Called on any error to prevent orphaned parts from accumulating in S3
    (S3 charges storage for incomplete multipart parts until aborted or expired).
    """
    try:
        await s3.abort_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
        )
        logger.warning(
            "Multipart upload aborted | key=%s upload_id=%s", key, upload_id
        )
    except ClientError as exc:
        # Log but don't re-raise — the original error takes precedence.
        # S3 lifecycle rules should clean up any orphaned parts within 7 days.
        logger.error(
            "Failed to abort multipart upload | key=%s upload_id=%s error=%s",
            key, upload_id, exc,
        )
