############################################################
# Outputs — printed after terraform apply
# Copy these values into your .env / GitHub Secrets
############################################################

output "alb_dns_name" {
  description = "ALB DNS name — use this as your app URL if no custom domain"
  value       = "https://${aws_lb.main.dns_name}"
}

output "api_url" {
  description = "Backend API base URL"
  value       = var.domain_name != "" ? "https://${var.api_subdomain}.${var.domain_name}" : "https://${aws_lb.main.dns_name}"
}

output "frontend_url" {
  description = "Frontend app URL"
  value       = var.domain_name != "" ? "https://${var.app_subdomain}.${var.domain_name}" : "https://${aws_lb.main.dns_name}"
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID — set as COGNITO_USER_POOL_ID in backend"
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "Cognito App Client ID — set as AUTH_AUDIENCE in backend"
  value       = aws_cognito_user_pool_client.api.id
}

output "cognito_issuer_url" {
  description = "Cognito JWKS issuer — set as AUTH_ISSUER in backend"
  value       = local.cognito_issuer
}

output "cognito_hosted_ui_url" {
  description = "Cognito Hosted UI for login (optional)"
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "rds_endpoint" {
  description = "RDS endpoint (private — only accessible from ECS)"
  value       = aws_db_instance.main.endpoint
  sensitive   = true
}

output "redis_endpoint" {
  description = "ElastiCache Redis primary endpoint"
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
  sensitive   = true
}

output "s3_bucket_name" {
  description = "S3 document storage bucket name"
  value       = aws_s3_bucket.documents.bucket
}

output "ecr_api_url" {
  description = "ECR repository URL for the API image"
  value       = aws_ecr_repository.main["api"].repository_url
}

output "ecr_frontend_url" {
  description = "ECR repository URL for the frontend image"
  value       = aws_ecr_repository.main["frontend"].repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name — used in GitHub Actions deploy"
  value       = aws_ecs_cluster.main.name
}

output "ecs_api_service_name" {
  description = "ECS API service name — used in GitHub Actions deploy"
  value       = aws_ecs_service.api.name
}

output "ecs_worker_service_name" {
  description = "ECS worker service name"
  value       = aws_ecs_service.worker.name
}

output "ecs_frontend_service_name" {
  description = "ECS frontend service name"
  value       = aws_ecs_service.frontend.name
}

output "ecs_migration_task_definition" {
  description = "Migration task definition ARN — used in CI/CD to run migrations"
  value       = aws_ecs_task_definition.migration.arn
}

output "secrets_arn" {
  description = "Secrets Manager ARN containing all app secrets"
  value       = aws_secretsmanager_secret.app_secrets.arn
  sensitive   = true
}

output "cloudwatch_dashboard_url" {
  description = "CloudWatch dashboard URL"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.main.dashboard_name}"
}
