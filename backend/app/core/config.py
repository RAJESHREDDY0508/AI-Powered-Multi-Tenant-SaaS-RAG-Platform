"""
Application configuration via environment variables (12-factor).
Pydantic BaseSettings validates and coerces all values at startup.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str  # asyncpg DSN, e.g. postgresql+asyncpg://user:pass@host/db

    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo_sql: bool = False   # set True in local dev to log queries

    # ------------------------------------------------------------------
    # AWS — S3 + KMS
    # ------------------------------------------------------------------
    aws_region:     str = "us-east-1"
    aws_account_id: str = ""           # 12-digit account ID

    s3_bucket: str = "rag-platform-documents"

    # Local dev: set these; prod: use ECS task role / IRSA (no static keys)
    aws_access_key_id:     str = ""
    aws_secret_access_key: str = ""

    # ------------------------------------------------------------------
    # Vector Store
    # ------------------------------------------------------------------
    vector_store_backend: str = "weaviate"   # "pinecone" | "weaviate"

    # Pinecone
    pinecone_api_key:    str = ""
    pinecone_index_name: str = "rag-platform"

    # Weaviate
    weaviate_url:     str = "http://localhost:8080"
    weaviate_host:    str = "localhost"
    weaviate_port:    int = 8080
    weaviate_api_key: str = ""   # empty = local/Docker (no auth)

    # Embeddings
    embedding_model:      str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------
    openai_api_key:   str = ""
    llm_model:        str = "gpt-4o-mini"
    llm_temperature:  float = 0.0
    llm_max_tokens:   int = 2048

    # ------------------------------------------------------------------
    # Auth — OIDC (AWS Cognito or Auth0)
    # ------------------------------------------------------------------
    auth_issuer:   str = ""   # e.g. https://cognito-idp.us-east-1.amazonaws.com/<pool_id>
    auth_audience: str = ""   # Cognito App Client ID  /  Auth0 API Identifier

    # Auth0-specific (leave empty when using Cognito)
    auth0_namespace: str = "https://api.ragplatform.io"   # custom claim namespace

    # AWS Cognito-specific
    cognito_user_pool_id: str = ""
    cognito_client_id:    str = ""

    # S3 default KMS key (overridden per-tenant after provisioning)
    s3_default_kms_key_arn: str = ""

    # ------------------------------------------------------------------
    # Hybrid Retrieval (Phase 3.1)
    # ------------------------------------------------------------------
    cohere_api_key:        str = ""    # Cohere ReRank — leave empty to disable
    cohere_rerank_model:   str = "rerank-english-v3.0"
    hybrid_dense_k:        int = 20   # dense retrieval candidate count
    hybrid_bm25_k:         int = 20   # BM25 candidate count
    hybrid_rerank_top_n:   int = 5    # final output size after reranking

    # ------------------------------------------------------------------
    # LLM Gateway — provider fallback (Phase 4)
    # ------------------------------------------------------------------
    # Azure OpenAI (GDPR-compliant fallback)
    azure_openai_api_key:     str = ""
    azure_openai_endpoint:    str = ""
    azure_openai_deployment:  str = "gpt-4o"
    azure_openai_api_version: str = "2024-08-01-preview"

    # Ollama (local / air-gapped)
    ollama_base_url: str = "http://localhost:11434"

    # ------------------------------------------------------------------
    # Observability (Phase 5)
    # ------------------------------------------------------------------
    langsmith_api_key: str = ""
    langsmith_project: str = "rag-platform"

    phoenix_enabled:  bool = False
    phoenix_endpoint: str  = "http://localhost:6006/v1/traces"

    otel_enabled:              bool = False
    otel_exporter_otlp_endpoint: str = ""

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_env: str = "development"   # development | staging | production
    debug: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
