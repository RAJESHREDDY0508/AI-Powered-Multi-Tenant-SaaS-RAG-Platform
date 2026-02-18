"""
S3 Storage Service — Tenant-Isolated

Isolation model (two reinforcing layers):

  Layer 1 — Prefix partitioning
    Every object is stored under:
        s3://<BUCKET>/tenants/<tenant_id>/<resource_type>/<object_key>
    A tenant can NEVER reference another tenant's prefix because the
    prefix is constructed server-side, never accepted from the client.

  Layer 2 — KMS key per tenant
    Each tenant has a dedicated Customer Managed Key (CMK).
    Even if a bug leaked a cross-tenant S3 URL, the requester's IAM
    role cannot call kms:Decrypt with the wrong tenant's key.
    The S3 object is cryptographically inaccessible.

Object lifecycle:
  - All uploads go through put_object() which enforces SSE-KMS.
  - Pre-signed URLs are scoped to the exact object key (no prefix wildcards).
  - Pre-signed URLs have a configurable short TTL (default 15 min).
  - Deletion is soft by default (marks object tag deleted=true);
    hard delete requires explicit flag.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from enum import Enum
from typing import BinaryIO
from uuid import UUID

import aioboto3
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resource types — used to partition the S3 prefix
# ---------------------------------------------------------------------------

class ResourceType(str, Enum):
    DOCUMENT    = "documents"    # raw uploaded files (PDF, DOCX, TXT …)
    CHUNK       = "chunks"       # processed text chunks (JSONL)
    EMBEDDING   = "embeddings"   # serialized vector dumps (backup/export)
    EXPORT      = "exports"      # user-requested data exports


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class S3Object:
    """Represents a stored object — returned by put_object / head_object."""
    tenant_id:    UUID
    resource:     ResourceType
    key:          str          # full S3 key including prefix
    bucket:       str
    size_bytes:   int
    content_type: str
    etag:         str
    version_id:   str | None = None


@dataclass(frozen=True)
class PresignedUrl:
    url:        str
    expires_in: int   # seconds
    method:     str   # GET | PUT


# ---------------------------------------------------------------------------
# Tenant storage config (loaded at tenant provision time)
# ---------------------------------------------------------------------------

@dataclass
class TenantStorageConfig:
    """Per-tenant storage settings resolved from DB / secrets manager."""
    tenant_id:   UUID
    kms_key_arn: str      # arn:aws:kms:<region>:<account>:key/<id>
    bucket:      str = field(default_factory=lambda: settings.s3_bucket)

    def prefix(self, resource: ResourceType, filename: str) -> str:
        """
        Build a tenant-scoped S3 key.
        Pattern:  tenants/<tenant_id>/<resource>/<filename>

        The tenant_id and resource come from server-controlled values —
        never from user input — preventing path traversal.
        """
        # Sanitize filename: strip directory components
        safe_name = filename.replace("/", "_").replace("..", "_")
        return f"tenants/{self.tenant_id}/{resource.value}/{safe_name}"


# ---------------------------------------------------------------------------
# S3 Service
# ---------------------------------------------------------------------------

class S3StorageService:
    """
    Async S3 operations scoped to a single tenant.

    One instance is created per request (via FastAPI dependency) so
    the tenant_config is immutably bound. There is no way to call
    methods on behalf of another tenant through this object.
    """

    def __init__(self, tenant_config: TenantStorageConfig) -> None:
        self._cfg = tenant_config
        self._session = aioboto3.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _client(self):
        """Return a scoped async S3 client context manager."""
        return self._session.client(
            "s3",
            region_name=settings.aws_region,
            # In production: IAM role assumed via ECS task role / IRSA.
            # In local dev: reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.
        )

    def _sse_params(self) -> dict:
        """
        SSE-KMS parameters required on every PutObject call.
        The Deny-if-not-KMS IAM policy makes unencrypted uploads impossible,
        but we add this client-side as defence-in-depth.
        """
        return {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": self._cfg.kms_key_arn,
        }

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def put_object(
        self,
        resource: ResourceType,
        filename: str,
        body: bytes | BinaryIO,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> S3Object:
        """
        Upload an object to the tenant's prefix with SSE-KMS encryption.

        Args:
            resource:     Which sub-partition (documents, chunks, …).
            filename:     Original filename — sanitized server-side.
            body:         Raw bytes or file-like object.
            content_type: MIME type; auto-detected from filename if omitted.
            metadata:     Optional string key/value pairs stored in S3 metadata.

        Returns:
            S3Object with full key, size, etag, etc.
        """
        key = self._cfg.prefix(resource, filename)
        ct  = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        raw = body if isinstance(body, bytes) else body.read()

        extra: dict = {
            **self._sse_params(),
            "ContentType": ct,
            "Metadata": {
                "tenant_id":  str(self._cfg.tenant_id),
                "resource":   resource.value,
                **(metadata or {}),
            },
            "Tagging": f"tenant_id={self._cfg.tenant_id}&resource={resource.value}",
        }

        async with self._client() as s3:
            resp = await s3.put_object(
                Bucket=self._cfg.bucket,
                Key=key,
                Body=raw,
                **extra,
            )

        logger.info(
            "S3 upload ok | tenant=%s resource=%s key=%s size=%d",
            self._cfg.tenant_id, resource.value, key, len(raw),
        )

        return S3Object(
            tenant_id=self._cfg.tenant_id,
            resource=resource,
            key=key,
            bucket=self._cfg.bucket,
            size_bytes=len(raw),
            content_type=ct,
            etag=resp.get("ETag", "").strip('"'),
            version_id=resp.get("VersionId"),
        )

    async def get_object(
        self,
        resource: ResourceType,
        filename: str,
    ) -> bytes:
        """
        Download an object from the tenant's prefix.
        Key is reconstructed server-side — client never supplies a raw S3 key.
        """
        key = self._cfg.prefix(resource, filename)
        async with self._client() as s3:
            try:
                resp = await s3.get_object(Bucket=self._cfg.bucket, Key=key)
                return await resp["Body"].read()
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("NoSuchKey", "404"):
                    raise FileNotFoundError(f"Object not found: {key}") from exc
                raise

    async def delete_object(
        self,
        resource: ResourceType,
        filename: str,
        hard: bool = False,
    ) -> None:
        """
        Soft delete (default): tags the object as deleted=true.
        Hard delete: permanently removes the object (requires explicit flag).
        Lifecycle rules on the bucket expire soft-deleted objects after N days.
        """
        key = self._cfg.prefix(resource, filename)
        async with self._client() as s3:
            if hard:
                await s3.delete_object(Bucket=self._cfg.bucket, Key=key)
                logger.warning("S3 hard delete | tenant=%s key=%s", self._cfg.tenant_id, key)
            else:
                await s3.put_object_tagging(
                    Bucket=self._cfg.bucket,
                    Key=key,
                    Tagging={"TagSet": [{"Key": "deleted", "Value": "true"}]},
                )
                logger.info("S3 soft delete | tenant=%s key=%s", self._cfg.tenant_id, key)

    async def generate_presigned_get(
        self,
        resource: ResourceType,
        filename: str,
        expires_in: int = 900,   # 15 minutes default
    ) -> PresignedUrl:
        """
        Generate a short-lived presigned GET URL for direct browser download.
        The URL is scoped to the exact object key — no wildcard access.
        """
        key = self._cfg.prefix(resource, filename)
        async with self._client() as s3:
            url = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._cfg.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        return PresignedUrl(url=url, expires_in=expires_in, method="GET")

    async def generate_presigned_put(
        self,
        resource: ResourceType,
        filename: str,
        content_type: str,
        expires_in: int = 300,   # 5 minutes for uploads
    ) -> PresignedUrl:
        """
        Generate a short-lived presigned PUT URL for direct browser upload.
        The client must include the exact content-type and x-amz-server-side-encryption
        headers — the IAM Deny policy rejects unencrypted uploads even from presigned URLs.
        """
        key = self._cfg.prefix(resource, filename)
        async with self._client() as s3:
            url = await s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket":                  self._cfg.bucket,
                    "Key":                     key,
                    "ContentType":             content_type,
                    **self._sse_params(),
                },
                ExpiresIn=expires_in,
            )
        return PresignedUrl(url=url, expires_in=expires_in, method="PUT")

    async def list_objects(
        self,
        resource: ResourceType,
        max_keys: int = 1000,
    ) -> list[dict]:
        """
        List objects in the tenant's resource partition.
        Prefix is always tenant-scoped — no cross-tenant listing possible.
        """
        prefix = f"tenants/{self._cfg.tenant_id}/{resource.value}/"
        async with self._client() as s3:
            resp = await s3.list_objects_v2(
                Bucket=self._cfg.bucket,
                Prefix=prefix,
                MaxKeys=max_keys,
            )
        return resp.get("Contents", [])

    async def head_object(
        self,
        resource: ResourceType,
        filename: str,
    ) -> dict:
        """Return metadata for an object without downloading it."""
        key = self._cfg.prefix(resource, filename)
        async with self._client() as s3:
            try:
                return await s3.head_object(Bucket=self._cfg.bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "404":
                    raise FileNotFoundError(f"Object not found: {key}") from exc
                raise
