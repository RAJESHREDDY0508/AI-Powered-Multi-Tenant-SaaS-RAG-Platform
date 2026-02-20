# Testing Guide — AI-Powered Multi-Tenant SaaS RAG Platform

## Table of Contents
1. [Quick Start](#1-quick-start)
2. [Test Architecture](#2-test-architecture)
3. [Environment Setup](#3-environment-setup)
4. [Running Unit Tests (no infrastructure)](#4-running-unit-tests)
5. [Running Integration Tests (mocked infrastructure)](#5-running-integration-tests)
6. [Running End-to-End Tests (real infrastructure)](#6-running-end-to-end-tests)
7. [Manual API Testing with curl](#7-manual-api-testing-with-curl)
8. [Test Coverage](#8-test-coverage)
9. [CI/CD Pipeline](#9-cicd-pipeline)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Quick Start

```bash
# 1. Install dependencies
cd backend
pip install -r requirements.txt

# 2. Run all unit tests (zero infrastructure needed)
pytest -m unit -v

# 3. Run integration tests (zero infrastructure needed — all mocked)
pytest -m integration -v

# 4. Run everything
pytest -v
```

Expected: **~50 tests, all green, under 10 seconds.**

---

## 2. Test Architecture

```
backend/tests/
├── conftest.py                        ← Shared fixtures (RSA keys, JWT factory, mocks)
├── unit/
│   ├── test_auth.py                   ← JWTDecoder, RoleChecker, _JWKSCache (22 tests)
│   ├── test_ingestion.py              ← IngestionService pipeline, 9-step (27 tests)
│   └── test_s3_multipart.py           ← streaming_multipart_upload (12 tests)
└── integration/
    └── test_upload_api.py             ← Full HTTP request/response cycle (35 tests)
```

### What each layer tests

| Layer | Real components | Mocked |
|-------|----------------|--------|
| **Unit** | Pure Python logic | Everything I/O (S3, DB, JWT network) |
| **Integration** | FastAPI routing, Pydantic validation, IngestionService | JWT verification, PostgreSQL, S3, Celery |
| **E2E** (manual) | Everything | Nothing — requires `docker-compose up` |

### Key design decisions

- **RSA key pair is generated once per session** — no live Auth0/Cognito needed. The test
  `make_token` fixture builds valid RS256 JWTs using `python-jose`.
- **`_JWKSCache._fetch` is always patched** — zero HTTP calls in unit tests.
- **`streaming_multipart_upload` is patched in integration tests** — prevents actual S3 I/O
  while still exercising the full pipeline orchestration (validation, dedup, audit, queue).
- **`app.dependency_overrides`** replaces auth + DB + storage in integration tests.
  The real FastAPI routing, schema validation, and service logic runs without modification.

---

## 3. Environment Setup

### 3.1 Python environment

```bash
cd backend

# Create a virtual environment
python -m venv .venv

# Activate it
# macOS / Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# Install all dependencies (including test extras)
pip install -r requirements.txt
```

### 3.2 Test-specific packages (already in requirements.txt)

| Package | Purpose |
|---------|---------|
| `pytest` | Test runner |
| `pytest-asyncio` | async test support |
| `pytest-cov` | coverage reporting |
| `httpx` | async HTTP test client |
| `cryptography` | RSA key generation for test JWTs |
| `python-jose[cryptography]` | JWT encode/decode in tests |
| `aioboto3` | async S3 client (patched in tests) |

### 3.3 Environment variables for testing

The `conftest.py` sets all required env vars automatically via `os.environ.setdefault()`.
You do **not** need a `.env` file to run unit or integration tests.

For manual API testing and E2E tests, create `backend/.env`:

```bash
# backend/.env  (copy from backend/.env.example)
DATABASE_URL=postgresql+asyncpg://app_admin:changeme_in_production@localhost:5432/rag_platform
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
S3_BUCKET=rag-documents
S3_DEFAULT_KMS_KEY_ARN=arn:aws:kms:us-east-1:000000000000:key/test-key
S3_ENDPOINT_URL=http://localhost:4566      # LocalStack
VECTOR_STORE_BACKEND=weaviate
WEAVIATE_HOST=localhost
WEAVIATE_PORT=8080
OPENAI_API_KEY=sk-your-key-here
CELERY_BROKER_URL=amqp://guest:guest@localhost:5672//
CELERY_RESULT_BACKEND=redis://localhost:6379/1
AUTH_ISSUER=https://your-cognito-pool.auth.us-east-1.amazoncognito.com/
AUTH_AUDIENCE=your-app-client-id
APP_ENV=development
DEBUG=true
```

---

## 4. Running Unit Tests

Unit tests have **zero external dependencies** — no Docker, no network, no database.
They run in seconds and are safe to run anywhere.

### Run all unit tests

```bash
cd backend
pytest -m unit -v
```

### Run a specific test module

```bash
pytest tests/unit/test_auth.py -v
pytest tests/unit/test_ingestion.py -v
pytest tests/unit/test_s3_multipart.py -v
```

### Run a single test

```bash
pytest tests/unit/test_auth.py::TestJWTDecoder::test_expired_token_raises_401 -v
```

### Run by marker combinations

```bash
# All auth-related tests
pytest -m "unit and auth" -v

# All S3-related tests
pytest -m "unit and s3" -v

# All ingestion tests
pytest -m "unit and ingestion" -v
```

### Expected output

```
tests/unit/test_auth.py::TestJWKSCache::test_get_signing_key_returns_public_key PASSED
tests/unit/test_auth.py::TestJWKSCache::test_unknown_kid_triggers_force_refresh PASSED
tests/unit/test_auth.py::TestJWKSCache::test_unknown_kid_after_refresh_raises_401 PASSED
...
tests/unit/test_s3_multipart.py::TestStreamingMultipartUpload::test_single_chunk_upload_succeeds PASSED
tests/unit/test_s3_multipart.py::TestStreamingMultipartUpload::test_md5_checksum_matches_file_bytes PASSED
...
============================== 61 passed in 4.23s ==============================
```

---

## 5. Running Integration Tests

Integration tests exercise the full FastAPI request/response cycle.
Auth, DB, S3, and Celery are all **mocked** — no Docker needed.

### Run all integration tests

```bash
cd backend
pytest -m integration -v
```

### Run with output capture disabled (see print statements)

```bash
pytest -m integration -v -s
```

### What integration tests verify

- **HTTP routing**: correct URL matching, path parameter extraction
- **Multipart form parsing**: `file`, `document_name`, `document_permissions`, `upload_token`
- **Request validation**: FastAPI + Pydantic enforcing required fields, type constraints
- **Status codes**: 202, 400, 404, 409, 413, 422 all verified
- **Response body schema**: every field in `DocumentUploadResponse` is present and correctly typed
- **Response headers**: `X-Document-ID`, `X-Tenant-ID`, `Location`
- **Security**: `tenant_id` comes from JWT fixture, never from request body
- **RBAC**: member can upload, viewer cannot delete, admin can delete

### Expected output

```
tests/integration/test_upload_api.py::TestUploadEndpoint::test_upload_pdf_returns_202 PASSED
tests/integration/test_upload_api.py::TestUploadEndpoint::test_upload_docx_returns_202 PASSED
...
tests/integration/test_upload_api.py::TestErrorResponseSchema::test_409_response_schema PASSED
============================== 35 passed in 2.87s ==============================
```

---

## 6. Running End-to-End Tests (Real Infrastructure)

E2E tests hit real PostgreSQL, LocalStack S3, and RabbitMQ.
They verify the full stack end-to-end including RLS, S3 multipart, and Celery dispatch.

### Step 1: Start the infrastructure

```bash
cd infra

# Start only the infrastructure services (not the app or workers)
docker-compose up -d postgres weaviate localstack rabbitmq redis

# Wait for health checks (typically 30-60 seconds)
docker-compose ps
```

All services should show `(healthy)` status:

```
NAME              STATUS          PORTS
rag_postgres      running (healthy)   0.0.0.0:5432->5432/tcp
rag_weaviate      running (healthy)   0.0.0.0:8080->8080/tcp
rag_localstack    running (healthy)   0.0.0.0:4566->4566/tcp
rag_rabbitmq      running (healthy)   0.0.0.0:5672->5672/tcp
rag_redis         running (healthy)   0.0.0.0:6379->6379/tcp
```

### Step 2: Create the test database

```bash
# Connect to PostgreSQL and create the test database
docker exec -it rag_postgres psql -U app_admin -c "CREATE DATABASE rag_platform_test;"
```

### Step 3: Create the LocalStack S3 bucket and KMS key

```bash
# Create the S3 bucket
aws --endpoint-url=http://localhost:4566 s3 mb s3://rag-documents

# Create a KMS key (LocalStack returns a fake ARN)
aws --endpoint-url=http://localhost:4566 kms create-key --description "test-key"
# Note the KeyMetadata.KeyId from the output

# Update .env with the real ARN
# S3_DEFAULT_KMS_KEY_ARN=arn:aws:kms:us-east-1:000000000000:key/<your-key-id>
```

### Step 4: Run database migrations

```bash
cd backend

# Apply all migrations to the test database
for f in migrations/*.sql; do
  docker exec -i rag_postgres psql -U app_admin -d rag_platform_test < "$f"
done
```

### Step 5: Run E2E tests

```bash
# Mark your tests with @pytest.mark.e2e or run them directly
pytest -m "integration" -v --no-header

# Or start the FastAPI server and test with curl (see Section 7)
```

### Step 6: Start the full stack (optional — for full E2E with Celery)

```bash
cd infra

# Build the backend image (first time only)
docker-compose build celery_worker celery_beat

# Start everything
docker-compose up -d

# Stream all logs
docker-compose logs -f

# Check worker is ready
docker exec rag_celery_worker celery -A app.workers.celery_app:celery_app inspect active
```

---

## 7. Manual API Testing with curl

Start the FastAPI development server:

```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API docs available at: http://localhost:8000/docs

### 7.1 Health checks

```bash
# Application health
curl http://localhost:8000/health

# Readiness (checks DB connectivity)
curl http://localhost:8000/ready
```

### 7.2 Get a test JWT token

For local testing, generate a token with the test RSA key:

```python
# generate_test_token.py
import time, uuid
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from jose import jwt

# Generate key pair (or load from your test fixtures)
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)

now = int(time.time())
claims = {
    "sub":              str(uuid.uuid4()),
    "email":            "test@example.com",
    "iss":              "https://test.auth.example.com/",
    "aud":              "test-api-audience",
    "exp":              now + 3600,
    "iat":              now,
    "custom:tenant_id": str(uuid.uuid4()),
    "custom:role":      "member",
}

token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "test-key-id-2024"})
print(f"Bearer {token}")
```

```bash
python generate_test_token.py
# Copy the token and use it below
TOKEN="eyJ..."
```

### 7.3 Upload a document

```bash
TOKEN="your-jwt-token-here"

# Upload a PDF
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/your/document.pdf" \
  -F "document_name=Q4 Financial Report" \
  -F 'document_permissions={"groups": ["finance"], "public": false}' \
  -v

# Expected response (HTTP 202):
# {
#   "document_id": "550e8400-e29b-41d4-a716-446655440000",
#   "status": "uploaded",
#   "checksum": "d41d8cd98f00b204e9800998ecf8427e",
#   "processing_status": "queued",
#   "s3_key": "tenants/<tenant_id>/documents/<uuid>.pdf",
#   "tenant_id": "<your-tenant-id>",
#   "document_name": "Q4 Financial Report",
#   "size_bytes": 204800,
#   "content_type": "application/pdf",
#   "created_at": "2026-02-19T12:00:00.000Z"
# }
```

### 7.4 Upload with SSE progress streaming

```bash
TOKEN="your-jwt-token-here"
UPLOAD_TOKEN=$(uuidgen)  # or: python -c "import uuid; print(uuid.uuid4())"

# In terminal 1: connect SSE stream FIRST
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/documents/upload-progress/$UPLOAD_TOKEN"

# In terminal 2: start the upload (pass same UPLOAD_TOKEN)
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/large.pdf" \
  -F "document_name=Large Document" \
  -F "upload_token=$UPLOAD_TOKEN"

# Terminal 1 will receive:
# event: connected
# data: {"message": "Progress stream ready", "upload_token": "..."}
#
# event: upload_progress
# data: {"stage": "uploading", "bytes_received": 5242880, "bytes_total": 52428800, "percent": 10.0}
#
# event: upload_progress
# data: {"stage": "uploading", "bytes_received": 10485760, "bytes_total": 52428800, "percent": 20.0}
#
# ... (one event per 5 MB S3 part)
#
# event: upload_progress
# data: {"stage": "queuing", "bytes_received": 52428800, "bytes_total": 52428800, "percent": 100.0}
#
# event: done
# data: {"message": "Upload pipeline complete"}
```

### 7.5 Poll processing status

```bash
DOCUMENT_ID="550e8400-e29b-41d4-a716-446655440000"

curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/documents/$DOCUMENT_ID/status"

# Response:
# {
#   "document_id": "550e8400-e29b-41d4-a716-446655440000",
#   "processing_status": "processing",
#   "chunk_count": 12,
#   "vector_count": 8,
#   "error_message": null,
#   "updated_at": "2026-02-19T12:00:15.000Z"
# }
```

### 7.6 List documents

```bash
# All documents (default: page 1, 20 per page)
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/documents/"

# With pagination
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/documents/?page=2&limit=10"

# Filter by status
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/documents/?status=pending"
```

### 7.7 Test error cases

```bash
# 413: Oversized file (indicate 60 MB via Content-Length)
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Length: 62914560" \
  -F "file=@smallfile.pdf" \
  -F "document_name=Test"

# 400: Unsupported file type
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@script.exe;type=application/octet-stream" \
  -F "document_name=Test"

# 409: Duplicate document (upload the same file twice)
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@report.pdf" \
  -F "document_name=First Upload"

curl -X POST http://localhost:8000/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@report.pdf" \
  -F "document_name=Duplicate Upload"
# → HTTP 409 DUPLICATE_DOCUMENT

# 401: No token
curl -X POST http://localhost:8000/api/v1/documents/upload \
  -F "file=@report.pdf" \
  -F "document_name=Test"
# → HTTP 401 or 403
```

---

## 8. Test Coverage

### Generate HTML coverage report

```bash
cd backend

# Run all tests with coverage
pytest --cov=app --cov-report=html --cov-report=term-missing

# Open the report
open htmlcov/index.html     # macOS
xdg-open htmlcov/index.html  # Linux
start htmlcov/index.html     # Windows
```

### Coverage targets by module

| Module | Target | What's covered |
|--------|--------|----------------|
| `app/auth/middleware.py` | ≥ 95% | All paths in `_JWKSCache`, `JWTDecoder`, `RoleChecker` |
| `app/services/ingestion.py` | ≥ 90% | All 9 pipeline steps, all error paths |
| `app/storage/multipart.py` | ≥ 95% | Success, abort, empty file, oversize |
| `app/api/v1/documents.py` | ≥ 85% | All endpoints, all status codes |
| `app/schemas/documents.py` | ≥ 80% | Schema validation, error factories |

### Run coverage for a specific module

```bash
# Coverage report for the ingestion service only
pytest tests/unit/test_ingestion.py \
  --cov=app.services.ingestion \
  --cov-report=term-missing \
  -v
```

---

## 9. CI/CD Pipeline

### GitHub Actions workflow example

```yaml
# .github/workflows/test.yml
name: Test Suite

on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install dependencies
        run: pip install -r backend/requirements.txt

      - name: Run unit tests
        working-directory: backend
        run: pytest -m unit -v --tb=short --junitxml=test-results/unit.xml

      - name: Run integration tests
        working-directory: backend
        run: pytest -m integration -v --tb=short --junitxml=test-results/integration.xml

      - name: Upload test results
        uses: actions/upload-artifact@v4
        with:
          name: test-results
          path: backend/test-results/

  coverage:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r backend/requirements.txt
      - name: Run with coverage
        working-directory: backend
        run: |
          pytest -m "unit or integration" \
            --cov=app \
            --cov-report=xml \
            --cov-fail-under=80
      - uses: codecov/codecov-action@v4
        with:
          file: backend/coverage.xml
```

### Running the full test matrix locally (like CI)

```bash
cd backend

# Unit only (fastest — runs in CI on every commit)
pytest -m unit -v --tb=short

# Unit + Integration (medium — runs on PR)
pytest -m "unit or integration" -v --tb=short

# Full suite with coverage (runs on merge to main)
pytest --cov=app --cov-report=term --cov-fail-under=80 -v
```

---

## 10. Troubleshooting

### `ModuleNotFoundError: No module named 'app'`

```bash
# Make sure you're running pytest from the backend/ directory
cd backend
pytest -v
```

### `pytest-asyncio` mode errors

```
PytestUnraisableExceptionWarning: asyncio
```

Ensure `pytest.ini` has:
```ini
[pytest]
asyncio_mode = auto
```

And you're using `pytest-asyncio >= 0.21`:
```bash
pip install "pytest-asyncio>=0.21"
```

### `ImportError: cannot import name 'TokenPayload' from 'app.auth.token'`

The app imports happen at test collection time. Ensure all environment variables
are set before pytest starts — they're set in `conftest.py` at the module level,
which runs before any imports.

If the error persists, check `conftest.py` is in `backend/tests/` and `pytest.ini`
has `testpaths = tests`.

### `ConnectionRefusedError` during unit tests

Unit tests should **never** make real network calls. If you see connection errors:
1. Check that `_JWKSCache._fetch` is being patched in the test
2. Ensure `CELERY_BROKER_URL=memory://` is set (done automatically in `conftest.py`)
3. Verify you're using `AsyncMock` where `await` is needed

### `asyncio.TimeoutError` in SSE tests

SSE tests use streaming HTTP clients. Ensure you're using the `async with client.stream(...)` context manager pattern and breaking out of the loop after receiving expected events (don't wait for the full 5-minute timeout).

### Docker compose issues

```bash
# Check service health
docker-compose ps

# View logs for a specific service
docker-compose logs rabbitmq
docker-compose logs postgres

# Restart a single service
docker-compose restart rabbitmq

# Full reset (removes volumes — destructive!)
docker-compose down -v
docker-compose up -d
```

### RabbitMQ not accepting connections

```bash
# Check the management UI
open http://localhost:15672
# Login: guest / guest

# Test AMQP connectivity
docker exec rag_rabbitmq rabbitmq-diagnostics check_port_connectivity
```

### LocalStack S3 bucket not found

```bash
# List buckets
aws --endpoint-url=http://localhost:4566 s3 ls

# Create the bucket
aws --endpoint-url=http://localhost:4566 s3 mb s3://rag-documents

# Verify
aws --endpoint-url=http://localhost:4566 s3 ls s3://rag-documents
```

---

## Appendix: Pytest Markers Reference

| Marker | Description | When to run |
|--------|-------------|-------------|
| `unit` | Pure Python, no I/O | Every commit, every PR |
| `integration` | FastAPI stack, mocked I/O | Every PR |
| `auth` | JWT auth tests | On auth changes |
| `ingestion` | Upload pipeline tests | On service changes |
| `s3` | S3 multipart tests | On storage changes |
| `celery` | Task queue tests | On worker changes |
| `slow` | Tests > 1 second | Pre-merge only |
| `e2e` | Real infrastructure | Nightly / staging |

### Combining markers

```bash
pytest -m "unit and not slow"          # fast unit tests
pytest -m "auth or ingestion"          # two subsystems
pytest -m "integration" --timeout=30   # integration with timeout
pytest -k "test_upload" -v             # keyword filter (any test with 'test_upload')
```
