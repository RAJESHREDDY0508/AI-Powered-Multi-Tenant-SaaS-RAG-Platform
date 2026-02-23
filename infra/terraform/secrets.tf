############################################################
# AWS Secrets Manager — Application Secrets
# ECS tasks inject these as environment variables at runtime
############################################################

resource "aws_secretsmanager_secret" "app_secrets" {
  name                    = "${local.name_prefix}/app-secrets"
  description             = "All sensitive config for ${local.name_prefix}"
  recovery_window_in_days = var.environment == "prod" ? 7 : 0

  tags = { Name = "${local.name_prefix}-app-secrets" }
}

resource "aws_secretsmanager_secret_version" "app_secrets" {
  secret_id = aws_secretsmanager_secret.app_secrets.id

  secret_string = jsonencode({
    # Database
    DATABASE_URL = "postgresql+asyncpg://${var.db_username}:${random_password.db_password.result}@${aws_db_instance.main.endpoint}/${var.db_name}?ssl=require"

    # Redis — use rediss:// (TLS) because transit encryption is enabled
    REDIS_URL = "rediss://:${random_password.redis_auth_token.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"

    # S3
    S3_BUCKET     = aws_s3_bucket.documents.bucket
    AWS_REGION    = var.aws_region

    # Vector Store (Weaviate Cloud)
    WEAVIATE_URL     = var.weaviate_url
    WEAVIATE_API_KEY = var.weaviate_api_key

    # Auth (Cognito)
    AUTH_ISSUER   = local.cognito_issuer
    AUTH_AUDIENCE = aws_cognito_user_pool_client.api.id

    # Cognito pool info (for user management)
    COGNITO_USER_POOL_ID = aws_cognito_user_pool.main.id
    COGNITO_CLIENT_ID    = aws_cognito_user_pool_client.api.id

    # AI
    OPENAI_API_KEY    = var.openai_api_key
    COHERE_API_KEY    = var.cohere_api_key
    LANGSMITH_API_KEY = var.langsmith_api_key
  })
}
