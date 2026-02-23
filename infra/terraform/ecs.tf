############################################################
# ECS Fargate — Cluster + Task Definitions + Services
############################################################

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"  # CloudWatch Container Insights
  }

  tags = { Name = "${local.name_prefix}-cluster" }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = var.environment == "prod" ? "FARGATE" : "FARGATE_SPOT"
    weight            = 1
    base              = 1
  }
}

# ── Shared: secret injection helper ─────────────────────────
# Each task reads from Secrets Manager at startup
locals {
  secret_arn = aws_secretsmanager_secret.app_secrets.arn

  # Common env vars (non-secret)
  common_env = [
    { name = "APP_ENV",                value = var.environment },
    { name = "DEBUG",                  value = "false" },
    { name = "VECTOR_STORE_BACKEND",   value = "weaviate" },
    { name = "EMBEDDING_MODEL",        value = "text-embedding-3-small" },
    { name = "EMBEDDING_DIMENSIONS",   value = "1536" },
    { name = "LLM_MODEL",              value = "gpt-4o-mini" },
    { name = "LLM_TEMPERATURE",        value = "0.0" },
    { name = "LLM_MAX_TOKENS",         value = "2048" },
    { name = "DB_POOL_SIZE",           value = "10" },
    { name = "DB_MAX_OVERFLOW",        value = "20" },
    { name = "PHOENIX_ENABLED",        value = "false" },
    { name = "LANGSMITH_PROJECT",      value = "${local.name_prefix}-${var.environment}" },
  ]

  # Secrets injected from Secrets Manager (JSON keys map to env var names)
  secret_env = [
    { name = "DATABASE_URL",       valueFrom = "${local.secret_arn}:DATABASE_URL::" },
    { name = "REDIS_URL",          valueFrom = "${local.secret_arn}:REDIS_URL::" },
    { name = "S3_BUCKET",          valueFrom = "${local.secret_arn}:S3_BUCKET::" },
    { name = "AWS_REGION",         valueFrom = "${local.secret_arn}:AWS_REGION::" },
    { name = "WEAVIATE_HOST",      valueFrom = "${local.secret_arn}:WEAVIATE_URL::" },
    { name = "WEAVIATE_API_KEY",   valueFrom = "${local.secret_arn}:WEAVIATE_API_KEY::" },
    { name = "AUTH_ISSUER",        valueFrom = "${local.secret_arn}:AUTH_ISSUER::" },
    { name = "AUTH_AUDIENCE",      valueFrom = "${local.secret_arn}:AUTH_AUDIENCE::" },
    { name = "OPENAI_API_KEY",     valueFrom = "${local.secret_arn}:OPENAI_API_KEY::" },
    { name = "COHERE_API_KEY",     valueFrom = "${local.secret_arn}:COHERE_API_KEY::" },
    { name = "LANGSMITH_API_KEY",  valueFrom = "${local.secret_arn}:LANGSMITH_API_KEY::" },
  ]
}

# ── Log Groups ────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "ecs_api" {
  name              = "/ecs/${local.name_prefix}/api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "ecs_worker" {
  name              = "/ecs/${local.name_prefix}/worker"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "ecs_frontend" {
  name              = "/ecs/${local.name_prefix}/frontend"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "ecs_migration" {
  name              = "/ecs/${local.name_prefix}/migration"
  retention_in_days = 7
}

# ── API Task Definition ───────────────────────────────────────
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_api_task.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.main["api"].repository_url}:latest"
    essential = true

    portMappings = [{
      containerPort = 8000
      hostPort      = 8000
      protocol      = "tcp"
    }]

    environment = local.common_env
    secrets     = local.secret_env

    command = [
      "sh", "-c",
      "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --timeout-keep-alive 75"
    ]

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs_api.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }

    # Read-only root filesystem for security
    readonlyRootFilesystem = false  # uvicorn needs /tmp

    linuxParameters = {
      initProcessEnabled = true
    }
  }])

  tags = { Name = "${local.name_prefix}-api-task" }
}

# ── API ECS Service ───────────────────────────────────────────
resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count

  capacity_provider_strategy {
    capacity_provider = var.environment == "prod" ? "FARGATE" : "FARGATE_SPOT"
    weight            = 1
    base              = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_controller { type = "ECS" }

  # Rolling deploy — keep capacity during updates
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  health_check_grace_period_seconds = 30

  enable_execute_command = var.environment != "prod"  # SSM shell access in non-prod

  depends_on = [aws_lb_listener.https]

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  tags = { Name = "${local.name_prefix}-api-service" }
}

# ── Celery Worker Task Definition ────────────────────────────
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_worker_task.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.main["api"].repository_url}:latest"
    essential = true

    environment = local.common_env
    secrets     = local.secret_env

    command = [
      "python", "-m", "celery",
      "-A", "app.workers.celery_app",
      "worker",
      "--loglevel=info",
      "--concurrency=2",
      "-Q", "ingestion"
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs_worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }

    linuxParameters = {
      initProcessEnabled = true
    }
  }])

  tags = { Name = "${local.name_prefix}-worker-task" }
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name_prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"  # Workers can tolerate interruptions
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_worker.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  enable_execute_command = var.environment != "prod"

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  tags = { Name = "${local.name_prefix}-worker-service" }
}

# ── Celery Beat Task Definition ───────────────────────────────
resource "aws_ecs_task_definition" "beat" {
  family                   = "${local.name_prefix}-beat"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_worker_task.arn

  container_definitions = jsonencode([{
    name      = "beat"
    image     = "${aws_ecr_repository.main["api"].repository_url}:latest"
    essential = true

    environment = local.common_env
    secrets     = local.secret_env

    command = [
      "python", "-m", "celery",
      "-A", "app.workers.celery_app",
      "beat",
      "--loglevel=info"
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs_worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "beat"
      }
    }
  }])

  tags = { Name = "${local.name_prefix}-beat-task" }
}

resource "aws_ecs_service" "beat" {
  name            = "${local.name_prefix}-beat"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.beat.arn
  desired_count   = 1  # MUST be 1 — never scale beat

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_worker.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [task_definition]
  }

  tags = { Name = "${local.name_prefix}-beat-service" }
}

# ── Frontend Task Definition ──────────────────────────────────
resource "aws_ecs_task_definition" "frontend" {
  family                   = "${local.name_prefix}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.frontend_cpu
  memory                   = var.frontend_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn

  container_definitions = jsonencode([{
    name      = "frontend"
    image     = "${aws_ecr_repository.main["frontend"].repository_url}:latest"
    essential = true

    portMappings = [{
      containerPort = 3000
      hostPort      = 3000
      protocol      = "tcp"
    }]

    environment = [
      { name = "NODE_ENV",              value = "production" },
      { name = "BACKEND_URL",           value = "http://${aws_lb.main.dns_name}" },
      { name = "NEXT_PUBLIC_APP_VERSION", value = "1.0.0" },
    ]

    healthCheck = {
      command     = ["CMD-SHELL", "wget -qO- http://localhost:3000 || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs_frontend.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "frontend"
      }
    }
  }])

  tags = { Name = "${local.name_prefix}-frontend-task" }
}

resource "aws_ecs_service" "frontend" {
  name            = "${local.name_prefix}-frontend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = 2

  capacity_provider_strategy {
    capacity_provider = var.environment == "prod" ? "FARGATE" : "FARGATE_SPOT"
    weight            = 1
    base              = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_frontend.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = "frontend"
    container_port   = 3000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  health_check_grace_period_seconds = 30

  depends_on = [aws_lb_listener.https]

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  tags = { Name = "${local.name_prefix}-frontend-service" }
}

# ── Migration Task Definition (run-once ECS task) ────────────
resource "aws_ecs_task_definition" "migration" {
  family                   = "${local.name_prefix}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_api_task.arn

  container_definitions = jsonencode([{
    name      = "migration"
    image     = "${aws_ecr_repository.main["api"].repository_url}:latest"
    essential = true

    environment = local.common_env
    secrets     = local.secret_env

    command = ["bash", "/app/scripts/run-migrations.sh"]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ecs_migration.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "migration"
      }
    }
  }])

  tags = { Name = "${local.name_prefix}-migration-task" }
}

# ── Auto-Scaling ──────────────────────────────────────────────
resource "aws_appautoscaling_target" "api" {
  max_capacity       = 10
  min_capacity       = var.api_desired_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name_prefix}-api-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 65.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = 20
  min_capacity       = var.worker_desired_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_cpu" {
  name               = "${local.name_prefix}-worker-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 600
    scale_out_cooldown = 30
  }
}
