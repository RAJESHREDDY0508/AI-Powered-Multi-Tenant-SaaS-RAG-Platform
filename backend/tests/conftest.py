"""
Root conftest.py — Shared fixtures for ALL tests (unit + integration)

Fixture hierarchy:
  session-scoped  : event_loop, test_settings, db_engine, db_tables
  function-scoped : db_session, s3_client, mock_jwt_token, sample_pdf_bytes

Environment strategy:
  - All tests use a dedicated test database (rag_platform_test).
  - S3 tests target LocalStack on localhost:4566 (or a mocked aioboto3 client).
  - JWT tokens are built with a test RSA key — no live auth provider needed.
  - Celery tasks are always executed eagerly (CELERY_TASK_ALWAYS_EAGER=True).

How to run:
  pytest                          # all tests
  pytest -m unit                  # unit tests only (fast, no I/O)
  pytest -m integration           # integration tests (requires docker-compose up)
  pytest tests/unit/test_auth.py  # single file
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import time
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient

# ─────────────────────────────────────────────────────────────────────────────
# Patch settings BEFORE any app imports so modules read test config
# ─────────────────────────────────────────────────────────────────────────────

os.environ.setdefault("DATABASE_URL",
    "postgresql+asyncpg://app_admin:changeme_in_production@localhost:5432/rag_platform_test")
os.environ.setdefault("AWS_REGION",            "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID",     "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("S3_BUCKET",             "test-bucket")
os.environ.setdefault("S3_DEFAULT_KMS_KEY_ARN","arn:aws:kms:us-east-1:000000000000:key/test-key")
os.environ.setdefault("VECTOR_STORE_BACKEND",  "weaviate")
os.environ.setdefault("WEAVIATE_HOST",         "localhost")
os.environ.setdefault("WEAVIATE_PORT",         "8080")
os.environ.setdefault("OPENAI_API_KEY",        "sk-test-key")
os.environ.setdefault("CELERY_BROKER_URL",     "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
os.environ.setdefault("APP_ENV",               "development")
os.environ.setdefault("DEBUG",                 "true")

# Placeholders — overridden per test by jwt_test_fixtures
os.environ.setdefault("AUTH_ISSUER",   "https://test.auth.example.com/")
os.environ.setdefault("AUTH_AUDIENCE", "test-api-audience")
os.environ.setdefault("AUTH0_NAMESPACE", "https://api.ragplatform.io")


# ─────────────────────────────────────────────────────────────────────────────
# RSA key pair for signing test JWTs (generated once per session)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def rsa_private_key():
    """Generate a 2048-bit RSA private key for test JWT signing."""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )


@pytest.fixture(scope="session")
def rsa_public_key(rsa_private_key):
    """Derive RSA public key from the test private key."""
    return rsa_private_key.public_key()


@pytest.fixture(scope="session")
def rsa_private_key_pem(rsa_private_key) -> bytes:
    """PEM-encoded private key bytes (used by jose.jwt.encode)."""
    return rsa_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(scope="session")
def rsa_public_key_pem(rsa_public_key) -> bytes:
    """PEM-encoded public key bytes."""
    return rsa_public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# ─────────────────────────────────────────────────────────────────────────────
# JWKS mock — patches _JWKSCache to return our test RSA public key
# ─────────────────────────────────────────────────────────────────────────────

TEST_KID = "test-key-id-2024"
TEST_ISSUER   = "https://test.auth.example.com/"
TEST_AUDIENCE = "test-api-audience"


@pytest.fixture(scope="session")
def test_jwks(rsa_public_key) -> dict:
    """
    Build a JWKS document containing the test RSA public key.
    This is what the real /.well-known/jwks.json returns.
    """
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    import base64

    pub_numbers: RSAPublicNumbers = rsa_public_key.public_key() if hasattr(rsa_public_key, 'public_key') else rsa_public_key.public_numbers()

    def _b64url(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(
            n.to_bytes(byte_length, "big")
        ).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": TEST_KID,
                "n":   _b64url(pub_numbers.n),
                "e":   _b64url(pub_numbers.e),
            }
        ]
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tenant and user fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_tenant_id() -> uuid.UUID:
    """A stable UUID used as tenant_id across all tests."""
    return uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def test_user_id() -> uuid.UUID:
    """A stable UUID used as user (sub) across all tests."""
    return uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture
def test_document_id() -> uuid.UUID:
    """A stable UUID for document references."""
    return uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ─────────────────────────────────────────────────────────────────────────────
# JWT token factory
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def make_token(rsa_private_key_pem, test_tenant_id, test_user_id):
    """
    Factory fixture: returns a function that builds signed test JWTs.

    Usage:
        token = make_token(role="member")
        token = make_token(role="admin", tenant_id=uuid.uuid4())
        token = make_token(expired=True)
    """
    from jose import jwt as jose_jwt

    def _build(
        role:       str = "member",
        tenant_id:  uuid.UUID | None = None,
        user_id:    uuid.UUID | None = None,
        expired:    bool = False,
        no_tenant:  bool = False,
        no_role:    bool = False,
        audience:   str  = TEST_AUDIENCE,
        issuer:     str  = TEST_ISSUER,
    ) -> str:
        now = int(time.time())
        tid = str(tenant_id or test_tenant_id)
        uid = str(user_id or test_user_id)

        claims: dict = {
            "sub":              uid,
            "email":            "test@tenant.example.com",
            "iss":              issuer,
            "aud":              audience,
            "exp":              now - 60 if expired else now + 3600,
            "iat":              now,
        }

        if not no_tenant:
            claims["custom:tenant_id"] = tid          # Cognito-style claim
        if not no_role:
            claims["custom:role"] = role              # Cognito-style claim

        return jose_jwt.encode(
            claims,
            rsa_private_key_pem,
            algorithm="RS256",
            headers={"kid": TEST_KID},
        )

    return _build


@pytest.fixture
def member_token(make_token) -> str:
    return make_token(role="member")


@pytest.fixture
def admin_token(make_token) -> str:
    return make_token(role="admin")


@pytest.fixture
def viewer_token(make_token) -> str:
    return make_token(role="viewer")


@pytest.fixture
def owner_token(make_token) -> str:
    return make_token(role="owner")


@pytest.fixture
def expired_token(make_token) -> str:
    return make_token(expired=True)


# ─────────────────────────────────────────────────────────────────────────────
# Sample file bytes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """Minimal valid PDF — passes magic-byte check (%PDF header)."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
        b"startxref\n195\n%%EOF"
    )


@pytest.fixture
def sample_docx_bytes() -> bytes:
    """Minimal DOCX — passes PK magic-byte check (ZIP signature)."""
    # DOCX is a ZIP file starting with PK\x03\x04
    return b"PK\x03\x04" + b"\x00" * 100 + b"word/document.xml" + b"\x00" * 200


@pytest.fixture
def sample_txt_bytes() -> bytes:
    """Plain text content."""
    return b"This is a test document for the RAG platform.\nIt has multiple lines.\n"


@pytest.fixture
def oversized_file_bytes() -> bytes:
    """File that exceeds MAX_FILE_SIZE_BYTES (50 MB)."""
    return b"x" * (52 * 1024 * 1024)  # 52 MB


@pytest.fixture
def exe_bytes() -> bytes:
    """Windows PE executable — should be rejected by MIME type check."""
    return b"MZ\x90\x00" + b"\x00" * 100   # PE magic bytes


# ─────────────────────────────────────────────────────────────────────────────
# Mock S3 storage service
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_storage(test_tenant_id):
    """
    Fully mocked S3StorageService.
    All methods are AsyncMock — no real AWS calls made.
    """
    from app.storage.s3 import S3StorageService, S3Object, TenantStorageConfig, ResourceType

    storage = MagicMock(spec=S3StorageService)

    # Configure _cfg for key construction
    config = TenantStorageConfig(
        tenant_id=test_tenant_id,
        kms_key_arn="arn:aws:kms:us-east-1:000000000000:key/test",
    )
    storage._cfg = config

    # put_object returns a realistic S3Object
    async def _put_object(resource, filename, body, content_type=None, metadata=None):
        return S3Object(
            tenant_id=test_tenant_id,
            resource=resource,
            key=config.prefix(resource, filename),
            bucket="test-bucket",
            size_bytes=len(body) if isinstance(body, bytes) else 1024,
            content_type=content_type or "application/octet-stream",
            etag="d41d8cd98f00b204e9800998ecf8427e",
        )

    storage.put_object    = AsyncMock(side_effect=_put_object)
    storage.get_object    = AsyncMock(return_value=b"file content")
    storage.delete_object = AsyncMock(return_value=None)
    storage.head_object   = AsyncMock(return_value={"ContentLength": 1024})
    storage.list_objects  = AsyncMock(return_value=[])

    return storage


# ─────────────────────────────────────────────────────────────────────────────
# Mock task publisher
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_publisher():
    """Mocked TaskPublisher — records calls without touching Celery/broker."""
    from app.services.ingestion import TaskPublisher
    publisher = MagicMock(spec=TaskPublisher)
    publisher.publish_ingestion_task = AsyncMock(return_value=None)
    return publisher


# ─────────────────────────────────────────────────────────────────────────────
# Mock DB session
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db():
    """
    Mocked AsyncSession.
    execute() returns an empty result by default (override per test).
    add() / flush() are no-ops.
    """
    from sqlalchemy.ext.asyncio import AsyncSession
    db = MagicMock(spec=AsyncSession)
    db.execute  = AsyncMock(return_value=MagicMock(scalars=MagicMock(
        return_value=MagicMock(first=MagicMock(return_value=None))
    )))
    db.add      = MagicMock(return_value=None)
    db.flush    = AsyncMock(return_value=None)
    db.rollback = AsyncMock(return_value=None)
    db.commit   = AsyncMock(return_value=None)
    return db


# ─────────────────────────────────────────────────────────────────────────────
# TokenPayload fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def member_payload(test_tenant_id, test_user_id):
    """Pre-built TokenPayload for a 'member' role user."""
    from app.auth.token import TokenPayload
    return TokenPayload(
        sub=str(test_user_id),
        email="member@tenant.example.com",
        tenant_id=test_tenant_id,
        role="member",
        exp=int(time.time()) + 3600,
        iss=TEST_ISSUER,
    )


@pytest.fixture
def admin_payload(test_tenant_id, test_user_id):
    """Pre-built TokenPayload for an 'admin' role user."""
    from app.auth.token import TokenPayload
    return TokenPayload(
        sub=str(test_user_id),
        email="admin@tenant.example.com",
        tenant_id=test_tenant_id,
        role="admin",
        exp=int(time.time()) + 3600,
        iss=TEST_ISSUER,
    )


@pytest.fixture
def viewer_payload(test_tenant_id, test_user_id):
    """Pre-built TokenPayload for a 'viewer' role user."""
    from app.auth.token import TokenPayload
    return TokenPayload(
        sub=str(test_user_id),
        email="viewer@tenant.example.com",
        tenant_id=test_tenant_id,
        role="viewer",
        exp=int(time.time()) + 3600,
        iss=TEST_ISSUER,
    )


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI test client with auth dependency override
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def app_with_overrides(member_payload, mock_db, mock_storage):
    """
    FastAPI app with ALL external dependencies overridden:
      - get_current_user → returns member_payload (no JWT verification)
      - get_tenant_db    → yields mock_db (no PostgreSQL)
      - get_tenant_storage → returns mock_storage (no S3)

    Use this for fast API-level tests that don't need real infrastructure.
    """
    from app.main import app
    from app.auth.token import get_current_user
    from app.auth.dependencies import get_tenant_db, get_tenant_storage

    async def _fake_db():
        yield mock_db

    app.dependency_overrides[get_current_user]   = lambda: member_payload
    app.dependency_overrides[get_tenant_db]      = _fake_db
    app.dependency_overrides[get_tenant_storage] = lambda: mock_storage

    yield app

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def async_client(app_with_overrides) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client using the overridden app.

    httpx >= 0.28 removed the 'app=' shortcut; use ASGITransport explicitly.
    """
    from httpx import ASGITransport
    transport = ASGITransport(app=app_with_overrides)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
