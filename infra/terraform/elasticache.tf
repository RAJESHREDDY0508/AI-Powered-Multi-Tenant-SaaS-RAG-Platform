############################################################
# ElastiCache — Redis 7 (Celery broker + result backend)
############################################################

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name_prefix}-redis-subnet-group"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${local.name_prefix}-redis-subnet-group" }
}

resource "aws_elasticache_parameter_group" "redis7" {
  name   = "${local.name_prefix}-redis7"
  family = "redis7"

  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${local.name_prefix}-redis"
  description          = "Redis for Celery broker and result backend"

  node_type            = var.redis_node_type
  port                 = 6379
  parameter_group_name = aws_elasticache_parameter_group.redis7.name
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]

  engine_version       = "7.1"
  num_cache_clusters   = var.environment == "prod" ? 2 : 1  # 2 for HA

  # Encryption in transit + at rest
  at_rest_encryption_enabled  = true
  transit_encryption_enabled  = true

  # Auth token (stored in Secrets Manager)
  auth_token = random_password.redis_auth_token.result

  automatic_failover_enabled = var.environment == "prod"
  multi_az_enabled           = var.environment == "prod"

  snapshot_retention_limit = var.environment == "prod" ? 3 : 0
  snapshot_window          = "05:00-06:00"
  maintenance_window       = "sun:06:00-sun:07:00"

  apply_immediately = var.environment != "prod"

  tags = { Name = "${local.name_prefix}-redis" }
}

resource "random_password" "redis_auth_token" {
  length  = 32
  special = false  # Redis auth token cannot contain special chars
}
