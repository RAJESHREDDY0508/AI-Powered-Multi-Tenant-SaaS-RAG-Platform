"""
FastAPI Application — Entry Point

Multi-tenant RAG Platform API Gateway

Architecture:
  - All routes are versioned under /api/v1/
  - Authentication is JWT-based (AWS Cognito or Auth0) enforced per-route
  - Database RLS is set per-request via SQLAlchemy dependency
  - S3 and vector store are tenant-scoped via dependency injection
  - Structured JSON error responses on all 4xx/5xx

Middleware stack (innermost → outermost):
  1. CORS — restrict to configured origins
  2. Request ID injection — X-Request-ID header on every response
  3. Trusted host — reject unexpected Host headers in production
  4. Gzip — compress responses > 1 KB
  5. Request logging — structured log per request with latency

SOC2 compliance notes:
  - All uploads are audit-logged (see IngestionService)
  - JWT is verified on every authenticated request (no session state)
  - Tenant isolation is enforced at DB (RLS) and S3 (prefix + KMS) layers
  - /health and /metrics endpoints are excluded from auth
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.documents import router as documents_router
from app.api.v1.query import router as query_router
from app.evaluation.dashboard import router as eval_router
from app.core.config import settings
from app.db.session import check_db_health
from app.schemas.documents import ErrorResponse, ErrorDetail

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ---------------------------------------------------------------------------
# Application lifespan — startup / shutdown hooks
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Run on startup: validate DB connectivity, log config summary.
    Run on shutdown: clean up connection pools.
    """
    logger.info(
        "Starting RAG Platform | env=%s vector_store=%s",
        settings.app_env, settings.vector_store_backend,
    )

    db_health = await check_db_health()
    if db_health["status"] != "ok":
        logger.critical("Database health check failed at startup: %s", db_health)
        raise RuntimeError(f"DB unavailable: {db_health}")

    logger.info("Database: connected")
    logger.info("Auth issuer: %s", settings.auth_issuer)
    logger.info("S3 bucket: %s", settings.s3_bucket)

    yield

    logger.info("Shutting down RAG Platform")
    from app.db.session import engine
    await engine.dispose()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title="AI-Powered Multi-Tenant RAG Platform",
        description=(
            "Enterprise-grade document ingestion and retrieval-augmented generation API. "
            "Supports multi-tenant isolation, SSO authentication, and async processing."
        ),
        version="1.0.0",
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url="/api/redoc" if not settings.is_production else None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ----------------------------------------------------------------
    # Middleware (applied in reverse order — last added = outermost)
    # ----------------------------------------------------------------

    # GZip compression for responses > 1 KB
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # CORS — restrict to configured origins
    allowed_origins = (
        ["*"] if settings.app_env == "development"
        else [
            "https://app.ragplatform.io",
            "https://admin.ragplatform.io",
        ]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Tenant-ID"],
        expose_headers=["X-Request-ID", "X-Document-ID", "X-Tenant-ID", "Location"],
    )

    # Trusted host check — prevent Host header injection in production
    if settings.is_production:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=["api.ragplatform.io", "*.ragplatform.io"],
        )

    # ----------------------------------------------------------------
    # Request ID + structured logging middleware
    # ----------------------------------------------------------------

    @app.middleware("http")
    async def request_id_and_logging(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        start = time.perf_counter()

        response = await call_next(request)

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "HTTP %s %s %d %.1fms | tenant=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request.headers.get("X-Tenant-ID", "-"),
        )
        return response

    # ----------------------------------------------------------------
    # Exception handlers — uniform structured error responses
    # ----------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """Convert Pydantic/FastAPI validation errors to structured ErrorResponse."""
        details = [
            ErrorDetail(
                field=" → ".join(str(loc) for loc in err["loc"]),
                message=err["msg"],
                code="VALIDATION_ERROR",
            )
            for err in exc.errors()
        ]
        body = ErrorResponse(
            error_code="VALIDATION_ERROR",
            message="Request validation failed.",
            details=details,
            request_id=request.headers.get("X-Request-ID"),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=body.model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        """Catch-all for unhandled exceptions — never expose stack traces."""
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        logger.exception(
            "Unhandled exception | path=%s request_id=%s",
            request.url.path, request_id,
        )
        body = ErrorResponse(
            error_code="INTERNAL_ERROR",
            message="An unexpected error occurred. Our team has been notified.",
            request_id=request_id,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=body.model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    # ----------------------------------------------------------------
    # Routers
    # ----------------------------------------------------------------

    app.include_router(documents_router, prefix="/api/v1")
    app.include_router(query_router,     prefix="/api/v1")
    app.include_router(eval_router,      prefix="/api/v1")

    # Initialise observability tracing (LangSmith / Phoenix / OTEL)
    from app.observability.tracing import TracingConfig
    TracingConfig.init()

    # ----------------------------------------------------------------
    # Health & readiness endpoints (no auth — used by load balancer)
    # ----------------------------------------------------------------

    @app.get(
        "/health",
        tags=["Operations"],
        summary="Liveness probe",
        description="Returns 200 if the process is alive. No external checks.",
    )
    async def health() -> dict:
        return {"status": "ok", "service": "rag-platform-api"}

    @app.get(
        "/health/ready",
        tags=["Operations"],
        summary="Readiness probe (k8s alias)",
        description="Alias for /ready — used by Kubernetes readinessProbe.",
    )
    @app.get(
        "/ready",
        tags=["Operations"],
        summary="Readiness probe",
        description="Returns 200 only if the database is reachable.",
    )
    async def readiness() -> JSONResponse:
        db_status = await check_db_health()
        if db_status["status"] != "ok":
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "database": db_status},
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready", "database": db_status},
        )

    return app


# ---------------------------------------------------------------------------
# Application instance (imported by uvicorn)
# ---------------------------------------------------------------------------

app = create_app()


# ---------------------------------------------------------------------------
# Local development entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app_env == "development",
        log_level="debug" if settings.debug else "info",
        access_log=True,
    )
